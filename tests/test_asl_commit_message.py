"""Diff, prompt, and message-formatting tests for `asl commit`.

The formatting expectations are golden strings rather than a live comparison
against `skills/gitsummary/scripts/format-commit.py`: that script is gone now
that the skill drives `asl commit`, so the parity it used to guarantee is
pinned here instead of imported from there.
"""

from __future__ import annotations

import unittest

from agent_session_loop import commit_message
from tests import support_asl_commit_message as fixtures


class DiffParsingTests(unittest.TestCase):
    def test_parses_each_file_section_with_its_line_counts(self) -> None:
        raw = fixtures.build_file_diff("src/alpha.py", hunks=1, lines=3) + (
            "diff --git a/src/beta.py b/src/beta.py\n"
            "index 3333333..4444444 100644\n"
            "--- a/src/beta.py\n"
            "+++ b/src/beta.py\n"
            "@@ -1,2 +1,1 @@\n"
            "-removed one\n"
            "-removed two\n"
            "+added one\n"
        )

        files = commit_message._parse_diff_into_files(raw)

        self.assertEqual([f.path for f in files], ["src/alpha.py", "src/beta.py"])
        self.assertEqual(files[0].additions, 3)
        self.assertEqual(files[0].deletions, 0)
        self.assertEqual(files[1].additions, 1)
        self.assertEqual(files[1].deletions, 2)
        self.assertTrue(all(f.original_size > 0 for f in files))
        self.assertFalse(any(f.is_binary for f in files))
        # Each section carries its own header, so the sections partition the diff.
        self.assertEqual(sum(len(f.content) for f in files), len(raw))

    def test_parses_empty_and_headerless_diffs_as_no_files(self) -> None:
        self.assertEqual(commit_message._parse_diff_into_files(""), [])
        self.assertEqual(commit_message._parse_diff_into_files("   \n\n"), [])
        self.assertEqual(commit_message._parse_diff_into_files("not a diff\n"), [])

    def test_classifies_both_binary_markers_and_skips_line_counting(self) -> None:
        raw = fixtures.build_binary_diff("assets/logo.png") + (
            "diff --git a/assets/icon.ico b/assets/icon.ico\n"
            "index 5555555..6666666 100644\n"
            "GIT binary patch\n"
            "literal 12\n"
            "+not a real addition\n"
        )

        files = commit_message._parse_diff_into_files(raw)

        self.assertEqual(len(files), 2)
        self.assertTrue(all(f.is_binary for f in files))
        self.assertEqual([f.additions for f in files], [0, 0])
        self.assertEqual([f.deletions for f in files], [0, 0])


class TruncationTests(unittest.TestCase):
    def test_truncation_cuts_at_a_hunk_boundary_and_reports_the_remainder(
        self,
    ) -> None:
        raw = fixtures.build_file_diff("src/wide.py", hunks=4, lines=6)
        parsed = commit_message._parse_diff_into_files(raw)[0]
        hunk_size = len(fixtures.build_hunk(1, lines=6).encode("utf-8"))
        header_size = parsed.original_size - 4 * hunk_size
        # Room for two hunks plus the reserve, but not a third.
        max_size = header_size + 2 * hunk_size + commit_message._TRUNCATION_RESERVE + 1

        truncated = commit_message._truncate_file_diff(parsed, max_size)

        self.assertTrue(truncated.is_truncated)
        self.assertEqual(truncated.original_size, parsed.original_size)
        self.assertIn("hunk 1 line 1", truncated.content)
        self.assertIn("hunk 2 line 6", truncated.content)
        self.assertNotIn("hunk 3 line 1", truncated.content)
        self.assertNotIn("hunk 4 line 1", truncated.content)
        # The cut lands on the boundary: the third hunk's header is absent too.
        self.assertNotIn("context 3", truncated.content)
        self.assertIn("[...2 more hunks: +12, -0 lines...]", truncated.content)

    def test_content_within_budget_is_returned_untouched(self) -> None:
        parsed = commit_message._parse_diff_into_files(
            fixtures.build_file_diff("src/small.py", hunks=1, lines=2)
        )[0]

        result = commit_message._truncate_file_diff(parsed, parsed.original_size)

        self.assertIs(result, parsed)
        self.assertFalse(result.is_truncated)

    def test_hunkless_content_falls_back_to_a_flat_slice(self) -> None:
        parsed = commit_message.FileDiff(
            path="src/blob.py",
            content="diff --git a/src/blob.py b/src/blob.py\n" + "y" * 500,
            original_size=538,
        )

        result = commit_message._truncate_file_diff(parsed, 100)

        self.assertTrue(result.is_truncated)
        self.assertTrue(result.content.endswith("\n[...truncated...]\n"))
        self.assertEqual(len(result.content), 100 + len("\n[...truncated...]\n"))


class BudgetAllocationTests(unittest.TestCase):
    def test_budget_constants_match_the_ported_source(self) -> None:
        self.assertEqual(commit_message.MAX_TOTAL_DIFF_SIZE, 150 * 1024)
        self.assertEqual(commit_message.MAX_FILE_DIFF_SIZE, 30 * 1024)

    def test_fair_share_cap_and_surplus_redistribution(self) -> None:
        small = commit_message.FileDiff(path="a.py", content="", original_size=100)
        large_one = commit_message.FileDiff(
            path="b.py", content="", original_size=90000
        )
        large_two = commit_message.FileDiff(
            path="c.py", content="", original_size=90000
        )
        binary = commit_message.FileDiff(
            path="d.png", content="", is_binary=True, original_size=4000
        )

        budgets = commit_message._compute_file_budgets(
            [small, large_one, large_two, binary]
        )

        text_budget = commit_message.MAX_TOTAL_DIFF_SIZE - (
            1 * commit_message.BINARY_SUMMARY_OVERHEAD
        )
        cap = min(commit_message.MAX_FILE_DIFF_SIZE, text_budget // 3)
        self.assertNotIn("d.png", budgets)
        self.assertEqual(budgets["a.py"], cap)
        # The small file's unused share is split between the two large files.
        expected_extra = (cap - 100) // 2
        expected_large = min(commit_message.MAX_FILE_DIFF_SIZE, cap + expected_extra)
        self.assertEqual(budgets["b.py"], expected_large)
        self.assertEqual(budgets["c.py"], expected_large)

    def test_binary_only_diff_allocates_no_text_budget(self) -> None:
        binary = commit_message.FileDiff(path="x.png", content="", is_binary=True)

        self.assertEqual(commit_message._compute_file_budgets([binary]), {})


class ProcessRawDiffTests(unittest.TestCase):
    def test_empty_diff_reports_is_empty(self) -> None:
        for raw in ("", "   \n", "no diff headers here\n"):
            with self.subTest(raw=raw):
                result = commit_message.process_raw_diff(raw)
                self.assertTrue(result.is_empty)
                self.assertEqual(result.diff, "")
                self.assertEqual(result.total_size, 0)
                self.assertFalse(result.is_truncated)
                self.assertEqual(result.files_included, [])

    def test_small_diff_is_passed_through_whole(self) -> None:
        raw = fixtures.build_file_diff(
            "src/alpha.py", hunks=1, lines=2
        ) + fixtures.build_file_diff("src/beta.py", hunks=1, lines=2)

        result = commit_message.process_raw_diff(raw)

        self.assertFalse(result.is_empty)
        self.assertFalse(result.is_truncated)
        self.assertEqual(sorted(result.files_included), ["src/alpha.py", "src/beta.py"])
        self.assertEqual(result.omitted_files, [])
        self.assertEqual(result.truncated_files, [])
        self.assertEqual(result.total_size, len(result.diff.encode("utf-8")))
        self.assertIn("hunk 1 line 1", result.diff)

    def test_binary_files_are_summarized_rather_than_inlined(self) -> None:
        raw = fixtures.build_file_diff(
            "src/alpha.py", hunks=1, lines=2
        ) + fixtures.build_binary_diff("assets/logo.png")

        result = commit_message.process_raw_diff(raw)

        self.assertEqual(result.binary_files, ["assets/logo.png"])
        self.assertEqual(result.files_included, ["src/alpha.py"])
        self.assertTrue(result.is_truncated)
        self.assertIn(
            "diff --git a/assets/logo.png b/assets/logo.png\n[Binary file]\n",
            result.diff,
        )
        self.assertNotIn("Binary files a/assets/logo.png", result.diff)

    def test_oversized_file_is_truncated_and_recorded(self) -> None:
        raw = fixtures.build_file_diff("src/huge.py", hunks=900, lines=6)
        self.assertGreater(
            len(raw.encode("utf-8")), commit_message.MAX_FILE_DIFF_SIZE * 4
        )

        result = commit_message.process_raw_diff(raw)

        self.assertEqual(result.truncated_files, ["src/huge.py"])
        self.assertTrue(result.is_truncated)
        self.assertLessEqual(result.total_size, commit_message.MAX_FILE_DIFF_SIZE)
        self.assertIn("more hunks:", result.diff)

    def test_total_budget_overflow_demotes_files_to_stat_only(self) -> None:
        # Five oversized text files consume the whole per-file budget; the
        # binary summaries then overrun the flat 50-byte-per-binary
        # reservation, so the last text files fall back to stat lines.
        long_path = "vendor/" + "d" * 140
        raw = "".join(
            fixtures.build_file_diff(f"src/module_{index}.py", hunks=900, lines=6)
            for index in range(5)
        ) + "".join(
            fixtures.build_binary_diff(f"{long_path}/{index}.bin")
            for index in range(30)
        )

        result = commit_message.process_raw_diff(raw)

        self.assertTrue(result.is_truncated)
        self.assertTrue(result.omitted_files)
        self.assertEqual(len(result.binary_files), 30)
        self.assertEqual(
            len(result.files_included) + len(result.omitted_files),
            5,
        )
        self.assertLessEqual(result.total_size, commit_message.MAX_TOTAL_DIFF_SIZE)
        self.assertIn(
            "# Stat-only summaries (diff too large for full content):", result.diff
        )
        for path in result.omitted_files:
            self.assertIn(f"{path} | +", result.diff)


class ReportingHelperTests(unittest.TestCase):
    def test_format_size_renders_bytes_kilobytes_and_megabytes(self) -> None:
        self.assertEqual(commit_message.format_size(0), "0 B")
        self.assertEqual(commit_message.format_size(1023), "1023 B")
        self.assertEqual(commit_message.format_size(1024), "1.0 KB")
        self.assertEqual(commit_message.format_size(1536), "1.5 KB")
        self.assertEqual(commit_message.format_size(150 * 1024), "150.0 KB")
        self.assertEqual(commit_message.format_size(1024 * 1024), "1.0 MB")
        self.assertEqual(commit_message.format_size(3 * 1024 * 1024), "3.0 MB")

    def test_truncation_notice_is_empty_when_nothing_was_reduced(self) -> None:
        result = commit_message.DiffResult(diff="x", files_included=["a.py"])

        self.assertEqual(commit_message.build_truncation_notice(result), "")

    def test_truncation_notice_counts_each_reduction_kind(self) -> None:
        result = commit_message.DiffResult(
            diff="x",
            binary_files=["a.png", "b.png"],
            truncated_files=["big.py"],
            omitted_files=["c.py", "d.py", "e.py", "f.py", "g.py"],
            is_truncated=True,
        )

        notice = commit_message.build_truncation_notice(result)

        self.assertEqual(
            notice,
            "2 binary file(s) summarized: a.png, b.png; "
            "1 large file(s) truncated: big.py; "
            "5 file(s) reduced to stat summary: c.py, d.py, e.py, ... (+2 more)",
        )


class DequalifyPathsTests(unittest.TestCase):
    def test_strips_alias_and_legacy_repository_prefixes(self) -> None:
        self.assertEqual(
            commit_message.dequalify_paths(
                ["SVC:src/app.py", ".:README.md", "OWL:nested/dir/file.txt"]
            ),
            ["src/app.py", "README.md", "nested/dir/file.txt"],
        )

    def test_splits_on_the_first_colon_only(self) -> None:
        self.assertEqual(
            commit_message.dequalify_paths(["SVC:weird:name.txt"]),
            ["weird:name.txt"],
        )

    def test_drops_empty_remainders_and_deduplicates(self) -> None:
        self.assertEqual(
            commit_message.dequalify_paths(
                ["SVC:a.py", ".:a.py", "SVC:", "", "OWL:b.py"]
            ),
            ["a.py", "b.py"],
        )

    def test_unqualified_entries_pass_through(self) -> None:
        self.assertEqual(commit_message.dequalify_paths(["plain.py"]), ["plain.py"])


if __name__ == "__main__":
    unittest.main()
