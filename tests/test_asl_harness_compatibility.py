"""Tests for the pinned harness support window in `harness_compatibility`.

The subject here is an asymmetry, so the tests are organised around it: an
unrecognised harness version must never stop a run, and a known-incompatible
one must always stop it. Everything else — parsing three different vendors'
``--version`` sentences, memoising the probe, degrading every failed probe to
``unknown`` — exists to keep that asymmetry from being decided by accident.

No test here spawns a real harness. Probes run against shell scripts written
into a temporary directory, which is also what makes the "no model use" claim
checkable: the only argv this module ever builds is ``<exe> --version``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import stat
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

from agent_session_loop import harness_compatibility as compat


def _write_fake_cli(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _version_script(text: str) -> str:
    return f"#!/bin/sh\nprintf '%s\\n' {text!r}\n"


class VersionParsingTests(unittest.TestCase):
    def test_parses_each_vendor_version_sentence(self) -> None:
        # The three real shapes, captured from the installed CLIs. Each vendor
        # puts the number in a different place, which is why the parser matches
        # the dotted-number shape rather than the surrounding prose.
        self.assertEqual("0.149.1", compat.parse_version("codex-cli 0.149.1"))
        self.assertEqual("2.1.231", compat.parse_version("2.1.231 (Claude Code)"))
        self.assertEqual(
            "1.0.80",
            compat.parse_version(
                "GitHub Copilot CLI 1.0.80.\nRun 'copilot update' to check for updates."
            ),
        )

    def test_rejects_output_with_no_dotted_version(self) -> None:
        self.assertIsNone(compat.parse_version(""))
        self.assertIsNone(compat.parse_version("version 7"))
        self.assertIsNone(compat.parse_version("no version here"))

    def test_version_key_orders_numerically_not_lexically(self) -> None:
        self.assertLess(compat.version_key("1.0.9"), compat.version_key("1.0.10"))
        self.assertLess(compat.version_key("0.99.0"), compat.version_key("1.0.0"))

    def test_version_key_truncates_at_a_non_numeric_segment(self) -> None:
        # A prerelease orders as its release. Refusing a specific prerelease is
        # the blocked-list's job, not the comparator's.
        self.assertEqual((1, 2, 3), compat.version_key("1.2.3-beta.1"))
        self.assertEqual((1, 2), compat.version_key("1.2.x.9"))


class ClassificationTests(unittest.TestCase):
    def test_pinned_latest_and_previous_are_supported(self) -> None:
        for harness, policy in compat.POLICY.items():
            for version in policy.pinned_versions():
                result = compat.classify(harness, version)
                self.assertEqual("supported", result.status, f"{harness} {version}")
                self.assertEqual("ok", result.severity)
                self.assertFalse(result.blocks)

    def test_version_between_previous_and_latest_is_supported(self) -> None:
        result = compat.classify("claude", "2.1.0")
        self.assertEqual("supported", result.status)

    def test_newer_than_tested_warns_and_never_blocks(self) -> None:
        result = compat.classify("codex", "99.0.0")
        self.assertEqual("unknown", result.status)
        self.assertEqual("warn", result.severity)
        self.assertEqual("newer-than-tested", result.reason)
        self.assertFalse(result.blocks)

    def test_older_than_tested_but_above_minimum_warns(self) -> None:
        result = compat.classify("codex", "0.141.0")
        self.assertEqual("unknown", result.status)
        self.assertEqual("older-than-tested", result.reason)
        self.assertFalse(result.blocks)

    def test_below_minimum_blocks(self) -> None:
        result = compat.classify("codex", "0.100.0")
        self.assertEqual("incompatible", result.status)
        self.assertEqual("block", result.severity)
        self.assertEqual("below-minimum", result.reason)
        self.assertTrue(result.blocks)

    def test_explicitly_blocked_version_blocks_even_inside_the_window(self) -> None:
        policy = compat.POLICY["claude"]
        blocked = compat.HarnessPolicy(
            harness="claude",
            version_argv=policy.version_argv,
            latest=policy.latest,
            previous=policy.previous,
            minimum=policy.minimum,
            blocked=("2.1.0",),
        )
        with unittest.mock.patch.dict(compat.POLICY, {"claude": blocked}, clear=False):
            result = compat.classify("claude", "2.1.0")
        self.assertEqual("incompatible", result.status)
        self.assertEqual("blocked-version", result.reason)

    def test_absent_version_warns_rather_than_blocking(self) -> None:
        result = compat.classify("copilot", None)
        self.assertEqual("unknown", result.status)
        self.assertEqual("version-unavailable", result.reason)
        self.assertFalse(result.blocks)

    def test_every_policy_window_is_internally_ordered(self) -> None:
        for harness, policy in compat.POLICY.items():
            with self.subTest(harness=harness):
                self.assertLessEqual(
                    compat.version_key(policy.minimum),
                    compat.version_key(policy.previous),
                )
                self.assertLessEqual(
                    compat.version_key(policy.previous),
                    compat.version_key(policy.latest),
                )
                self.assertEqual(harness, policy.harness)

    def test_policy_covers_exactly_the_supported_harness_names(self) -> None:
        from agent_session_loop.harnesses import HARNESS_NAMES

        self.assertEqual(set(HARNESS_NAMES), set(compat.POLICY))


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        compat.clear_probe_cache()
        self.addCleanup(compat.clear_probe_cache)
        self.addCleanup(self._tmp.cleanup)

    def test_probe_reads_a_real_version_string_from_a_fake_cli(self) -> None:
        exe = _write_fake_cli(
            self.tmp_path, "codex", _version_script("codex-cli 0.149.1")
        )
        probe = compat.probe_executable("codex", str(exe), use_cache=False)
        self.assertEqual("0.149.1", probe.version)
        self.assertEqual("probed", probe.reason)

    def test_probe_only_ever_asks_for_the_version(self) -> None:
        # The no-model-use guarantee is exactly this: the argv is the harness
        # plus `--version` and nothing else. A CLI that recorded a prompt would
        # record it here.
        argv_log = self.tmp_path / "argv.log"
        exe = _write_fake_cli(
            self.tmp_path,
            "claude",
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> {str(argv_log)!r}\n'
            'printf "2.1.231 (Claude Code)\\n"\n',
        )
        compat.probe_executable("claude", str(exe), use_cache=False)
        self.assertEqual(["--version"], argv_log.read_text().split())

    def test_nonzero_exit_degrades_to_unknown(self) -> None:
        exe = _write_fake_cli(self.tmp_path, "codex", "#!/bin/sh\nexit 7\n")
        result = compat.check_executable("codex", str(exe), use_cache=False)
        self.assertEqual("unknown", result.status)
        self.assertEqual("probe-exit-7", result.reason)

    def test_unparseable_output_degrades_to_unknown_and_quotes_it(self) -> None:
        exe = _write_fake_cli(self.tmp_path, "codex", _version_script("who knows"))
        result = compat.check_executable("codex", str(exe), use_cache=False)
        self.assertEqual("unknown", result.status)
        self.assertEqual("unparseable-version", result.reason)
        self.assertIn("who knows", result.detail)

    def test_timeout_degrades_to_unknown(self) -> None:
        exe = _write_fake_cli(self.tmp_path, "codex", "#!/bin/sh\nsleep 30\n")
        result = compat.check_executable(
            "codex", str(exe), timeout_seconds=0.5, use_cache=False
        )
        self.assertEqual("unknown", result.status)
        self.assertEqual("probe-timeout", result.reason)

    def test_unrunnable_executable_degrades_to_unknown(self) -> None:
        missing = self.tmp_path / "absent"
        result = compat.check_executable("codex", str(missing), use_cache=False)
        self.assertEqual("unknown", result.status)
        self.assertFalse(result.blocks)

    def test_disabled_probe_reports_unknown_without_spawning(self) -> None:
        marker = self.tmp_path / "spawned"
        exe = _write_fake_cli(
            self.tmp_path,
            "codex",
            f"#!/bin/sh\n: > {str(marker)!r}\nprintf 'codex-cli 0.149.1\\n'\n",
        )
        with unittest.mock.patch.dict(
            os.environ, {compat.PROBE_ENV_VAR: "0"}, clear=False
        ):
            result = compat.check_executable("codex", str(exe), use_cache=False)
        self.assertEqual("unknown", result.status)
        self.assertEqual("probe-disabled", result.reason)
        self.assertFalse(marker.exists())

    def test_probe_is_memoized_per_executable_identity(self) -> None:
        counter = self.tmp_path / "runs"
        exe = _write_fake_cli(
            self.tmp_path,
            "codex",
            f"#!/bin/sh\nprintf 'x' >> {str(counter)!r}\n"
            "printf 'codex-cli 0.149.1\\n'\n",
        )
        for _ in range(3):
            compat.probe_executable("codex", str(exe))
        self.assertEqual(1, len(counter.read_text()))

    def test_rewriting_the_executable_invalidates_the_memo(self) -> None:
        exe = _write_fake_cli(
            self.tmp_path, "codex", _version_script("codex-cli 0.148.0")
        )
        first = compat.probe_executable("codex", str(exe))
        _write_fake_cli(self.tmp_path, "codex", _version_script("codex-cli 0.149.1"))
        second = compat.probe_executable("codex", str(exe))
        self.assertEqual("0.148.0", first.version)
        self.assertEqual("0.149.1", second.version)


class ResolutionTests(unittest.TestCase):
    def test_unresolvable_harness_is_unknown_not_incompatible(self) -> None:
        # preflight already refuses a missing harness with its own red finding.
        # If this module also called it incompatible, "not installed" and
        # "known broken" would be indistinguishable in the release evidence.
        result = compat.check_harness("codex", resolver=lambda _name: None)
        self.assertEqual("unknown", result.status)
        self.assertEqual("executable-not-found", result.reason)
        self.assertFalse(result.blocks)

    def test_check_all_reports_every_harness_in_a_stable_order(self) -> None:
        from agent_session_loop.harnesses import HARNESS_NAMES

        results = compat.check_all(resolver=lambda _name: None)
        self.assertEqual(list(HARNESS_NAMES), [r.harness for r in results])


class ReportTests(unittest.TestCase):
    def _results(self, *statuses: str) -> tuple[compat.Compatibility, ...]:
        versions = {
            "supported": compat.POLICY["codex"].latest,
            "unknown": "99.0.0",
            "incompatible": "0.1.0",
        }
        return tuple(compat.classify("codex", versions[status]) for status in statuses)

    def test_payload_declares_no_model_use_and_tallies_statuses(self) -> None:
        payload = compat.report_payload(
            self._results("supported", "unknown", "incompatible")
        )
        self.assertEqual(compat.SCHEMA_VERSION, payload["schema_version"])
        self.assertIs(False, payload["model_use"])
        self.assertIs(False, payload["ok"])
        self.assertEqual(
            {"supported": 1, "unknown": 1, "incompatible": 1}, payload["counts"]
        )

    def test_payload_is_json_serializable_and_carries_the_policy(self) -> None:
        payload = compat.report_payload(self._results("supported"))
        decoded = json.loads(json.dumps(payload))
        entry = decoded["harnesses"][0]
        self.assertEqual(compat.POLICY["codex"].latest, entry["policy"]["latest"])
        self.assertEqual(compat.POLICY["codex"].minimum, entry["policy"]["minimum"])

    def test_exit_code_blocks_only_on_incompatible_by_default(self) -> None:
        self.assertEqual(0, compat.exit_code(self._results("supported"), strict=False))
        self.assertEqual(0, compat.exit_code(self._results("unknown"), strict=False))
        self.assertEqual(
            1, compat.exit_code(self._results("incompatible"), strict=False)
        )

    def test_strict_rejects_unknown_with_its_own_status(self) -> None:
        self.assertEqual(0, compat.exit_code(self._results("supported"), strict=True))
        self.assertEqual(3, compat.exit_code(self._results("unknown"), strict=True))
        # A blocker outranks the strict warning so the code always names the
        # worst outcome, never the one that happened to be checked last.
        self.assertEqual(
            1,
            compat.exit_code(self._results("unknown", "incompatible"), strict=True),
        )


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        compat.clear_probe_cache()
        self.addCleanup(compat.clear_probe_cache)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, **overrides: object) -> tuple[int, str]:
        args = argparse.Namespace(harness=None, strict=False, json=True)
        for key, value in overrides.items():
            setattr(args, key, value)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = compat.harness_compat_from_args(args)
        return code, buffer.getvalue()

    def test_json_output_is_a_single_schema_stamped_document(self) -> None:
        _write_fake_cli(self.tmp_path, "codex", _version_script("codex-cli 0.149.1"))
        with unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.tmp_path)}, clear=False
        ):
            code, out = self._run(harness=["codex"])
        payload = json.loads(out)
        self.assertEqual(0, code)
        self.assertEqual(compat.SCHEMA_VERSION, payload["schema_version"])
        self.assertEqual(1, len(payload["harnesses"]))
        self.assertEqual("supported", payload["harnesses"][0]["status"])

    def test_known_incompatible_installation_exits_one(self) -> None:
        _write_fake_cli(self.tmp_path, "codex", _version_script("codex-cli 0.1.0"))
        with unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.tmp_path)}, clear=False
        ):
            code, out = self._run(harness=["codex"])
        self.assertEqual(1, code)
        self.assertIs(False, json.loads(out)["ok"])

    def test_unknown_installation_exits_zero_but_three_under_strict(self) -> None:
        _write_fake_cli(self.tmp_path, "codex", _version_script("codex-cli 99.0.0"))
        with unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.tmp_path)}, clear=False
        ):
            lenient, out = self._run(harness=["codex"])
            compat.clear_probe_cache()
            strict, _ = self._run(harness=["codex"], strict=True)
        self.assertEqual(0, lenient)
        self.assertEqual(3, strict)
        # Warned, not hidden: the document still says the pairing is untested.
        self.assertIs(True, json.loads(out)["ok"])
        self.assertEqual("unknown", json.loads(out)["harnesses"][0]["status"])

    def test_text_output_names_the_consequence_of_each_status(self) -> None:
        _write_fake_cli(self.tmp_path, "codex", _version_script("codex-cli 0.1.0"))
        with unittest.mock.patch.dict(
            os.environ, {"PATH": str(self.tmp_path)}, clear=False
        ):
            code, out = self._run(harness=["codex"], json=False)
        self.assertEqual(1, code)
        self.assertIn("BLOCK", out)
        self.assertIn("below-minimum", out)


if __name__ == "__main__":
    unittest.main()
