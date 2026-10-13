"""Harness skill, next-steps, plan snapshot cache, and review verdict repair tests.

Split from tests/test_asl_core.py."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    events,
    harness_claude,
    harness_codex,
    harness_copilot,
    harnesses,
    plans_errors,
    plans_files,
    plans_snapshot,
    responses,
    review,
    review_transitions,
)
from tests.support import (
    write_plan,
)


class HarnessSkillInvocationTests(unittest.TestCase):
    def test_skill_prefixes_derive_from_adapter_classes(self) -> None:
        codex = harness_codex.CodexAdapter
        claude = harness_claude.ClaudeAdapter
        copilot = harness_copilot.CopilotAdapter
        prefixes = harnesses.skill_prefixes()
        self.assertEqual(
            {
                codex.name: codex.skill_prefix,
                claude.name: claude.skill_prefix,
                copilot.name: copilot.skill_prefix,
            },
            prefixes,
        )
        self.assertEqual("$", prefixes["codex"])
        self.assertEqual("/", prefixes["claude"])
        self.assertEqual("/", prefixes["copilot"])

    def test_detect_invoking_harness_matches_single_marker(self) -> None:
        self.assertEqual(
            "claude", harnesses.detect_invoking_harness({"CLAUDECODE": "1"})
        )
        self.assertEqual(
            "codex", harnesses.detect_invoking_harness({"CODEX_SANDBOX": "1"})
        )
        self.assertEqual(
            "copilot",
            harnesses.detect_invoking_harness({"COPILOT_AGENT": "1"}),
        )
        self.assertEqual(
            "copilot",
            harnesses.detect_invoking_harness({"GITHUB_COPILOT_TOKEN": "x"}),
        )

    def test_detect_invoking_harness_none_when_ambiguous_or_empty(self) -> None:
        self.assertIsNone(harnesses.detect_invoking_harness({}))
        self.assertIsNone(
            harnesses.detect_invoking_harness({"CLAUDECODE": "1", "CODEX_SANDBOX": "1"})
        )
        self.assertIsNone(harnesses.detect_invoking_harness({"CLAUDECODE": ""}))

    def test_skill_invocations_applies_prefix_and_argument(self) -> None:
        invocations = harnesses.skill_invocations("session-run", "/plans/p/", env={})
        self.assertEqual(
            {
                "codex": "$session-run /plans/p/",
                "claude": "/session-run /plans/p/",
                "copilot": "/session-run /plans/p/",
            },
            invocations,
        )

    def test_skill_invocations_orders_detected_harness_first(self) -> None:
        invocations = harnesses.skill_invocations(
            "session-run", "/plans/p/", env={"CLAUDECODE": "1"}
        )
        self.assertEqual("claude", next(iter(invocations)))
        # Ordering never drops harnesses.
        self.assertEqual({"codex", "claude", "copilot"}, set(invocations))

    def test_skill_invocations_without_argument_omits_suffix(self) -> None:
        self.assertEqual(
            {"codex": "$foo", "claude": "/foo", "copilot": "/foo"},
            harnesses.skill_invocations("foo", env={}),
        )


class NextStepsPayloadBuilderTests(unittest.TestCase):
    def test_invocation_and_command_steps_wrap_in_envelope(self) -> None:
        payload = responses.next_steps_payload(
            [
                responses.invocation_step(
                    "run it", {"codex": "$foo", "claude": "/foo"}
                ),
                responses.command_step("asl status"),
            ]
        )
        self.assertEqual(
            {
                "next_steps": [
                    {
                        "description": "run it",
                        "invocations": {"codex": "$foo", "claude": "/foo"},
                    },
                    {"command": "asl status"},
                ]
            },
            payload,
        )

    def test_invocation_step_copies_invocations_mapping(self) -> None:
        source = {"codex": "$foo"}
        step = responses.invocation_step("d", source)
        source["codex"] = "mutated"
        self.assertEqual("$foo", step["invocations"]["codex"])


class PlanSnapshotCacheTests(unittest.TestCase):
    @contextmanager
    def _counting_inspect(self):
        calls: list[Path] = []
        real_inspect_plan = plans_snapshot.inspect_plan

        def counting(plan_dir):
            calls.append(plans_files.canonicalize_path(plan_dir))
            return real_inspect_plan(plan_dir)

        with mock.patch.object(plans_snapshot, "inspect_plan", counting):
            yield calls

    @contextmanager
    def _seeded_cache(self, statuses=("complete", "pending")):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, statuses)
            cache = plans_snapshot.PlanSnapshotCache()
            cache.get(plan_dir)
            yield plan_dir, cache

    def test_get_reuses_snapshot_while_plan_files_are_untouched(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            with self._counting_inspect() as calls:
                first = cache.get(plan_dir)
                second = cache.get(plan_dir)

            self.assertEqual([], calls)
            self.assertIs(first, second)

    def test_get_rereads_after_mtime_change_with_identical_size(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            session_path = plan_dir / "session-2.md"
            before = session_path.stat()
            os.utime(
                session_path,
                ns=(before.st_atime_ns + 10**9, before.st_mtime_ns + 10**9),
            )

            with self._counting_inspect() as calls:
                cache.get(plan_dir)

            self.assertEqual(before.st_size, session_path.stat().st_size)
            self.assertEqual(1, len(calls))

    def test_get_rereads_after_size_change_and_serves_new_content(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            session_path = plan_dir / "session-2.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete"
                ),
                encoding="utf-8",
            )

            with self._counting_inspect() as calls:
                refreshed = cache.get(plan_dir)

            self.assertEqual(1, len(calls))
            self.assertEqual(
                ["complete", "complete"],
                [session.status for session in refreshed.sessions],
            )

    def test_get_rereads_when_a_session_file_is_added_or_removed(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            write_plan(plan_dir, ("complete", "pending", "pending"))

            with self._counting_inspect() as calls:
                grown = cache.get(plan_dir)
            self.assertEqual(1, len(calls))
            self.assertEqual(3, len(grown.sessions))

            (plan_dir / "session-3.md").unlink()
            with self._counting_inspect() as calls:
                shrunk = cache.get(plan_dir)
            self.assertEqual(1, len(calls))
            self.assertEqual(2, len(shrunk.sessions))

    def test_get_rereads_when_an_expansion_file_is_added_or_removed(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            expansion_path = plan_dir / "session-2.expand.md"
            expansion_path.write_text("# Notes\n", encoding="utf-8")

            with self._counting_inspect() as calls:
                with_expansion = cache.get(plan_dir)
            self.assertEqual(1, len(calls))

            expansion_path.unlink()
            with self._counting_inspect() as calls:
                without_expansion = cache.get(plan_dir)
            self.assertEqual(1, len(calls))
            self.assertNotEqual(
                with_expansion.content_sha256, without_expansion.content_sha256
            )

    def test_get_rereads_for_a_different_plan_directory(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            other_dir = plan_dir.parent / "other-plan"
            write_plan(other_dir, ("pending",))

            with self._counting_inspect() as calls:
                other = cache.get(other_dir)

            self.assertEqual(1, len(calls))
            self.assertEqual(plans_files.canonicalize_path(other_dir), other.plan_dir)

    def test_get_fresh_always_rereads(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            with self._counting_inspect() as calls:
                cache.get_fresh(plan_dir)
                cache.get_fresh(plan_dir)

            self.assertEqual(2, len(calls))

    def test_invalidate_forces_the_next_get_to_reread(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            cache.invalidate()

            with self._counting_inspect() as calls:
                cache.get(plan_dir)
                cache.get(plan_dir)

            self.assertEqual(1, len(calls))

    def test_unreadable_plan_raises_instead_of_serving_the_cached_snapshot(
        self,
    ) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            (plan_dir / "manifest.md").unlink()

            with self.assertRaises(plans_errors.PlanError):
                cache.get(plan_dir)

            (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            with self._counting_inspect() as calls:
                recovered = cache.get(plan_dir)
            self.assertEqual(1, len(calls))
            self.assertEqual(2, len(recovered.sessions))

    def test_concurrent_readers_never_observe_a_stale_snapshot(self) -> None:
        with self._seeded_cache() as (plan_dir, cache):
            session_path = plan_dir / "session-2.md"
            template = session_path.read_text(encoding="utf-8")
            stop = threading.Event()
            errors: list[BaseException] = []

            def hammer(fetch) -> None:
                try:
                    while not stop.is_set():
                        snapshot = fetch(plan_dir)
                        self.assertEqual(2, len(snapshot.sessions))
                except BaseException as error:  # pragma: no cover - failure path
                    errors.append(error)

            readers = [
                threading.Thread(target=hammer, args=(cache.get,)),
                threading.Thread(target=hammer, args=(cache.get_fresh,)),
            ]
            for reader in readers:
                reader.start()
            try:
                for index in range(40):
                    marker = "complete" if index % 2 else "pending"
                    session_path.write_text(
                        template.replace(
                            "- **Status**: pending", f"- **Status**: {marker}"
                        ),
                        encoding="utf-8",
                    )
            finally:
                stop.set()
                for reader in readers:
                    reader.join(timeout=10)

            self.assertEqual([], errors)
            self.assertFalse(any(reader.is_alive() for reader in readers))
            self.assertEqual(
                plans_snapshot.inspect_plan(plan_dir).content_sha256,
                cache.get(plan_dir).content_sha256,
            )


class ReviewVerdictRepairHelperTests(unittest.TestCase):
    """Unit coverage for the bounded in-run review-verdict repair loop."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.event_path = Path(self._tmp.name) / events.EVENTS_FILE_NAME

    def _append(self, name: str, payload: dict[str, object]) -> None:
        events.append_event(self.event_path, events.Event(name, payload))

    def _append_retry(self, session: str, error: str) -> None:
        self._append(
            events.REVIEW_VERDICT_RETRY_EVENT,
            {
                "session": session,
                "fingerprint": review_transitions.review_verdict_fingerprint(
                    {"reason": "malformed-review-artifact", "error": error}
                ),
                "failure_reason": "malformed-review-artifact",
                "error": error,
            },
        )

    def test_fingerprint_folds_reason_and_error(self) -> None:
        first = review_transitions.review_verdict_fingerprint(
            {"reason": "malformed-review", "error": "bad status line"}
        )
        self.assertEqual(
            first,
            review_transitions.review_verdict_fingerprint(
                {"reason": "malformed-review", "error": "bad status line"}
            ),
        )
        self.assertTrue(first.startswith("review-verdict:"))
        self.assertNotEqual(
            first,
            review_transitions.review_verdict_fingerprint(
                {"reason": "malformed-review", "error": "missing status line"}
            ),
        )
        self.assertNotEqual(
            first,
            review_transitions.review_verdict_fingerprint(
                {"reason": "review-missing", "error": "bad status line"}
            ),
        )

    def test_fingerprint_is_none_without_reason_or_error(self) -> None:
        self.assertIsNone(review_transitions.review_verdict_fingerprint({}))
        self.assertIsNone(
            review_transitions.review_verdict_fingerprint({"reason": "", "error": ""})
        )

    def test_prior_retries_reset_at_each_review_cycle(self) -> None:
        self._append_retry("3", "stale error")
        self._append(events.REVIEW_CYCLE_EVENT, {"cycle": 1})
        self._append_retry("3", "fresh error")

        priors = review_transitions.read_prior_review_verdict_retries(
            self.event_path, "3"
        )

        self.assertEqual(["fresh error"], [p["error"] for p in priors])

    def test_prior_retries_are_scoped_to_one_session(self) -> None:
        self._append_retry("3", "other session error")
        self._append_retry("4", "target error")

        priors = review_transitions.read_prior_review_verdict_retries(
            self.event_path, "4"
        )

        self.assertEqual(["target error"], [p["error"] for p in priors])

    def test_prior_retries_tolerate_a_missing_or_corrupt_log(self) -> None:
        self.assertEqual(
            [],
            review_transitions.read_prior_review_verdict_retries(self.event_path, "3"),
        )
        self.event_path.write_text("not json\n\n", encoding="utf-8")
        self.assertEqual(
            [],
            review_transitions.read_prior_review_verdict_retries(self.event_path, "3"),
        )

    def test_exhaustion_on_repeat_fingerprint_or_over_budget(self) -> None:
        self.assertFalse(
            review_transitions.is_review_verdict_repair_exhausted("fp-1", (), 1)
        )
        self.assertFalse(
            review_transitions.is_review_verdict_repair_exhausted(
                "fp-2",
                ("fp-1",),
                review_transitions.MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS,
            )
        )
        self.assertTrue(
            review_transitions.is_review_verdict_repair_exhausted("fp-1", ("fp-1",), 1)
        )
        self.assertTrue(
            review_transitions.is_review_verdict_repair_exhausted(
                "fp-3",
                ("fp-1", "fp-2"),
                review_transitions.MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS + 1,
            )
        )
        # An unfingerprintable verdict still respects the attempt budget.
        self.assertTrue(
            review_transitions.is_review_verdict_repair_exhausted(
                None, (), review_transitions.MAX_REVIEW_VERDICT_REPAIR_ATTEMPTS + 1
            )
        )

    def test_repair_references_quote_the_latest_error(self) -> None:
        self.assertEqual(
            (),
            review_transitions.review_verdict_repair_references(self.event_path, "3"),
        )
        self._append_retry("3", "first error")
        self._append_retry("3", "second error")

        references = review_transitions.review_verdict_repair_references(
            self.event_path, "3"
        )

        joined = "\n".join(references)
        self.assertIn("attempt 2", joined)
        self.assertIn("second error", joined)
        self.assertNotIn("first error", joined)
        self.assertIn(review.FINAL_REVIEW_FILENAME, joined)
        self.assertIn(review.FINAL_REVIEW_ARTIFACT_FILENAME, joined)
