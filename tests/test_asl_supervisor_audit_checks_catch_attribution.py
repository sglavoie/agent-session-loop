"""Deterministic checks attributing caught scope violations."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    run_commit,
    supervisor_audit_checks,
    supervisor_audit_evidence_constants,
    supervisor_audit_evidence_models,
)
from agent_session_loop.assurance_models_findings import DeterministicResult
from tests import support_supervisor_audit_checks as checks_support
from tests.support_supervisor_audit import RUN_ID


class DeterministicCheckTests(checks_support.DeterministicCheckFixture):
    """Known failure modes reproduce as findings; a clean run passes every lane."""

    def _write_commit(
        self, run_dir: Path, *, session_id: str, repository_alias: str = "docs"
    ) -> None:
        """Record one commit, by default in the repository the violation names.

        A commit only defeats a catch when it could have carried the write, so
        the default is the repository under test; passing another alias is how
        a test says the commit landed somewhere the violation cannot be in.
        """
        commit_dir = run_dir / f"commit-0001-session-01-{repository_alias}"
        commit_dir.mkdir()
        self._write_result(
            commit_dir,
            {
                "success": True,
                "returncode": 0,
                "repository_alias": repository_alias,
                "session_id": session_id,
            },
        )

    def _write_commit_group(
        self, state_dir: Path, *, session_id: str, aliases: list[str]
    ) -> None:
        """Record one commit group the way the runner's state records it.

        The group names no repository of its own; the aliases it committed live
        in the entries beneath it, each keyed the way the runner keys them.
        """
        payload = self._read_state(state_dir)
        payload["commit_groups"] = [
            {
                "group_id": "group-0001",
                "scope": "session",
                "session": int(session_id),
                "session_id": session_id,
                "status": "complete",
                "ordered_aliases": list(aliases),
                "repositories": [{"alias": alias, "key": alias} for alias in aliases],
            }
        ]
        self._write_state(state_dir, payload)

    def _shed_events(
        self, bundle: supervisor_audit_evidence_models.AuditInput
    ) -> dict[str, DeterministicResult]:
        """Drop the bundle's event records the way the size cap drops them."""
        bundle.payload["events"]["records"] = []
        bundle.payload.setdefault("omissions", []).append(
            {
                "section": "events",
                "reason": supervisor_audit_evidence_constants.REASON_SIZE_CAP,
                "count": 2,
            }
        )
        return {
            result.check: result
            for result in supervisor_audit_checks.run_deterministic_checks(bundle)
        }

    def _record_pause_in_state(
        self, history: tuple[Path, Path, Path, Path], *, reverted: bool = True
    ) -> None:
        """Mirror the scope pause into state the way ``run_loop`` persists it."""
        payload = self._read_state(history[2])
        payload.update(
            paused=True,
            pause_reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            pause_details={
                "session": 1,
                "workspace_write_guard": self._violating_guard(reverted=reverted),
            },
        )
        self._write_state(history[2], payload)

    def _record_offending_head_move(self, run_dir: Path) -> None:
        """Let the offending turn's own guard see the violated repository move."""
        payload = self._read_result(run_dir)
        payload["workspace_write_guard"]["changed"] = {
            "docs": {
                "head_changed": True,
                "head_before": "a" * 40,
                "head_after": "b" * 40,
                "head_transition": {
                    "before": "a" * 40,
                    "after": "b" * 40,
                    "relation": "fast-forward",
                    "commits_added": 1,
                    "commits_removed": 0,
                    "history_rewrite": False,
                },
            }
        }
        self._write_result(run_dir / "turn-0001-01", payload)

    def _mixed_scope_violations(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        paused_violations: list[dict[str, Any]],
        turn_violations: list[dict[str, Any]] | None = None,
    ) -> None:
        """Record two out-of-scope writes, only one of which names a session.

        The turn half is left unattributed the way a guard record that omits
        ``session_id`` leaves it, and the pause half is attributed through its
        own envelope, so the two records can only be tied together by what they
        say was written.
        """
        guard = self._violating_guard(reverted=True, attributed=False)
        if turn_violations is not None:
            guard["violations"] = turn_violations
        payload = self._read_result(history[3])
        payload["workspace_write_guard"] = guard
        self._write_result(history[3] / "turn-0001-01", payload)
        paused_guard = self._violating_guard(reverted=True, attributed=False)
        paused_guard["violations"] = paused_violations
        self._write_events(
            history[2],
            [
                ("run_started", {"run_id": RUN_ID}),
                (
                    events.RUN_PAUSED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "reason": events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                        "details": {
                            "session": 1,
                            "workspace_write_guard": paused_guard,
                        },
                    },
                ),
            ],
        )

    def test_a_pause_the_size_cap_dropped_is_not_a_pause_that_never_happened(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)

            bundle, _ = self._audit(history)
            results = self._shed_events(bundle)

            # The pause is the one fact that lives only in the event log, so a
            # bundle that shed those records cannot report its absence as proof.
            self.assertEqual("unavailable", results["scope-escape"].status)
            self.assertIn(
                "the pause that would settle it", results["scope-escape"].detail
            )
            self.assertEqual("pass", results["scope-guard-catch"].status)

    def test_shed_events_do_not_excuse_a_violation_that_also_went_unreverted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, reverted=False)

            bundle, _ = self._audit(history)
            results = self._shed_events(bundle)

            # The revert is recorded in the turn result, which the cap did not
            # touch, and a pause could not have made up for its absence.
            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("not reverted to pending", results["scope-escape"].detail)

    def test_a_pause_the_status_kept_settles_a_catch_the_log_shed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)
            self._record_pause_in_state(history)

            bundle, _ = self._audit(history)
            results = self._shed_events(bundle)

            # The persisted status holds the same pause the size cap dropped
            # from the log, so the catch is proven rather than left blind.
            self.assertEqual("pass", results["scope-escape"].status)
            self.assertEqual("fail", results["scope-guard-catch"].status)
            self.assertIn(
                "paused the run before anything was committed",
                results["scope-guard-catch"].detail,
            )

    def test_a_status_pause_does_not_excuse_an_unreverted_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, reverted=False)
            self._record_pause_in_state(history, reverted=False)

            bundle, _ = self._audit(history)
            results = self._shed_events(bundle)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("not reverted to pending", results["scope-escape"].detail)

    def test_the_offending_turns_own_head_move_does_not_defeat_the_catch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)
            self._record_offending_head_move(history[3])

            bundle, results = self._audit(history)

            # The audit derives an observed commit from that very head move, and
            # it is the violation as the guard saw it, not a later commit that
            # carried the write forward (ASI-9).
            self.assertIn(
                supervisor_audit_evidence_constants.OBSERVED_COMMIT_ORIGIN,
                [c.get("origin") for c in bundle.payload["commits"]["commits"]],
            )
            self.assertEqual("pass", results["scope-escape"].status)
            self.assertEqual("fail", results["scope-guard-catch"].status)

    def test_a_later_turns_observed_commit_still_defeats_the_catch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)
            self._record_offending_head_move(history[3])
            later = self._read_result(history[3])
            later["workspace_write_guard"] = {
                "passed": True,
                "session_id": "01",
                "write_aliases": ["app"],
                "authorized_aliases": ["app"],
                "violations": [],
                "unavailable": {},
                "changed": {
                    "docs": {
                        "head_changed": True,
                        "head_before": "b" * 40,
                        "head_after": "c" * 40,
                        "head_transition": {
                            "before": "b" * 40,
                            "after": "c" * 40,
                            "relation": "fast-forward",
                            "commits_added": 1,
                            "commits_removed": 0,
                            "history_rewrite": False,
                        },
                    }
                },
            }
            later_dir = history[3] / "turn-0002-01"
            later_dir.mkdir()
            self._write_result(later_dir, later)

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("could have carried", results["scope-escape"].detail)

    def test_the_recovered_message_directory_is_not_a_commit_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            _, baseline = self._audit(history)
            # `run_commit` keeps recovered paused-work messages in a sibling of
            # the attempt directories whose name also starts with `commit-`,
            # and reading it as an attempt reported an attempt with no result
            # (ASI-117).
            recovery = (
                history[3] / run_commit.RECOVERED_PAUSED_WORK_MESSAGE_DIR_NAME / "g1"
            )
            recovery.mkdir(parents=True)
            (recovery / "message.txt").write_text("subject\n", encoding="utf-8")

            bundle, results = self._audit(history)

            names = [a.get("name") for a in bundle.payload["commits"]["attempts"]]
            self.assertNotIn(run_commit.RECOVERED_PAUSED_WORK_MESSAGE_DIR_NAME, names)
            self.assertEqual(
                baseline["commit-failure-accounting"].status,
                results["commit-failure-accounting"].status,
            )

    def test_a_numbered_commit_directory_without_a_result_is_still_an_attempt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            paths = run_commit._commit_paths(
                history[3], commit_index=1, session_id="1", repository_alias="docs"
            )
            paths["artifact_dir"].mkdir()

            bundle, _ = self._audit(history)

            attempts = {a.get("name"): a for a in bundle.payload["commits"]["attempts"]}
            self.assertIn(paths["artifact_dir"].name, attempts)
            self.assertIs(
                False, attempts[paths["artifact_dir"].name]["result"]["available"]
            )

    def test_a_commit_for_the_offending_session_undoes_the_catch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)
            self._write_commit(history[3], session_id="01")

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("a commit for that session", results["scope-escape"].detail)
            self.assertEqual("pass", results["scope-guard-catch"].status)

    def test_a_commit_in_another_repository_leaves_the_catch_standing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)
            # Same session, but a commit writes one repository's history and
            # this one is not the repository the violation names, so it cannot
            # be what carried the out-of-scope write.
            self._write_commit(history[3], session_id="01", repository_alias="app")

            _, results = self._audit(history)

            self.assertEqual("pass", results["scope-escape"].status)
            self.assertEqual("fail", results["scope-guard-catch"].status)

    def test_a_commit_naming_no_repository_still_undoes_the_catch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)
            commit_dir = history[3] / "commit-0001-session-01-unknown"
            commit_dir.mkdir()
            self._write_result(
                commit_dir, {"success": True, "returncode": 0, "session_id": "01"}
            )

            _, results = self._audit(history)

            # A commit whose record does not say where it landed cannot be
            # ruled out of the violated repository.
            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("could have carried", results["scope-escape"].detail)

    def test_a_commit_group_is_read_by_the_repositories_beneath_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)
            self._write_commit_group(history[2], session_id="01", aliases=["app"])

            _, results = self._audit(history)

            # The group names no repository of its own; reading it whole would
            # make it an unlocatable commit that no violation can be cleared of.
            self.assertEqual("pass", results["scope-escape"].status)
            self.assertEqual("fail", results["scope-guard-catch"].status)

    def test_a_commit_group_naming_the_violated_repository_undoes_the_catch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)
            self._write_commit_group(history[2], session_id="01", aliases=["docs"])

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("could have carried", results["scope-escape"].detail)

    def test_a_commit_group_that_committed_nothing_is_not_a_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, attributed=False, pause_session=None)
            self._write_commit_group(history[2], session_id="01", aliases=[])

            _, results = self._audit(history)

            # An empty group is barrier bookkeeping for a session that committed
            # nothing, so even the unattributed reading has nothing to concede.
            self.assertEqual("pass", results["scope-escape"].status)
            self.assertEqual("fail", results["scope-guard-catch"].status)
            self.assertIn(
                "before anything was committed", results["scope-guard-catch"].detail
            )

    def test_a_commit_for_another_session_leaves_the_catch_standing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)
            self._write_commit(history[3], session_id="02")

            _, results = self._audit(history)

            self.assertEqual("pass", results["scope-escape"].status)
            self.assertEqual("fail", results["scope-guard-catch"].status)

    def test_unnamed_scope_violation_without_commits_is_still_a_catch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, attributed=False, pause_session=None)

            _, results = self._audit(history)

            # Nothing was committed, so "before anything was committed" is a
            # claim the bundle actually supports even with no session named.
            self.assertEqual("pass", results["scope-escape"].status)
            self.assertEqual("fail", results["scope-guard-catch"].status)
            self.assertIn("an unnamed session", results["scope-guard-catch"].detail)
            self.assertIn(
                "before anything was committed", results["scope-guard-catch"].detail
            )

    def test_unnamed_scope_violation_with_any_commit_is_not_reported_as_caught(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, attributed=False, pause_session=None)
            # No session to match this commit against, so it cannot be excused
            # the way another session's commit is when the offender is named.
            self._write_commit(history[3], session_id="02")

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("cannot be ruled out", results["scope-escape"].detail)
            self.assertEqual("pass", results["scope-guard-catch"].status)
            self.assertNotIn(
                "before anything was committed", results["scope-guard-catch"].detail
            )
            findings = {
                finding.invariant_key: finding
                for finding in supervisor_audit_checks.findings_from_results(
                    results.values()
                )
            }
            self.assertNotIn("packet.write-scope-guard-catch", findings)
            self.assertEqual(
                "critical", findings["packet.write-scope-containment"].severity
            )

    def test_a_violation_named_only_by_its_pause_is_attributed_to_that_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, attributed=False)

            _, results = self._audit(history)

            # The turn half is unnamed and the pause half names session 1;
            # correlating them keeps one write one claim instead of reporting
            # it twice, once as an unnamed ambiguity.
            self.assertEqual("fail", results["scope-guard-catch"].status)
            self.assertIn("session 1", results["scope-guard-catch"].detail)
            self.assertNotIn("an unnamed session", results["scope-guard-catch"].detail)
            self.assertEqual(
                1, results["scope-guard-catch"].detail.count("wrote outside")
            )
            self.assertEqual("pass", results["scope-escape"].status)

    def test_a_commit_for_the_pause_named_session_undoes_the_correlated_catch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, attributed=False)
            self._write_commit(history[3], session_id="1")

            _, results = self._audit(history)

            # Correlation is real attribution, so the commit is matched by
            # session rather than swept in as an unresolvable ambiguity.
            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("a commit for that session", results["scope-escape"].detail)
            self.assertEqual("pass", results["scope-guard-catch"].status)

    def test_distinct_unnamed_violation_is_not_folded_into_a_named_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._mixed_scope_violations(
                history,
                paused_violations=[{"alias": "notes", "repository": "notes"}],
            )
            # Another session's commit, which says nothing about the write the
            # guard left unattributed and so cannot excuse it.
            self._write_commit(history[3], session_id="02")

            _, results = self._audit(history)

            # The pause names one violation; the turn records a different one.
            # Merging them would delete the unattributed write, whose catch the
            # bundle's commit evidence cannot support.
            escape = results["scope-escape"]
            self.assertEqual("fail", escape.status)
            self.assertEqual(1, escape.detail.count("wrote outside"))
            self.assertIn("an unnamed session", escape.detail)
            self.assertIn("docs", escape.detail)
            self.assertIn("cannot be ruled out", escape.detail)
            self.assertNotIn("notes", escape.detail)
            caught = results["scope-guard-catch"]
            self.assertEqual("fail", caught.status)
            self.assertEqual(1, caught.detail.count("wrote outside"))
            self.assertIn("session 1", caught.detail)
            self.assertIn("notes", caught.detail)
            self.assertNotIn("docs", caught.detail)
            findings = {
                finding.invariant_key: finding
                for finding in supervisor_audit_checks.findings_from_results(
                    results.values()
                )
            }
            self.assertEqual(
                "critical", findings["packet.write-scope-containment"].severity
            )
            self.assertEqual(
                "medium", findings["packet.write-scope-guard-catch"].severity
            )

    def test_a_pause_naming_other_paths_does_not_attribute_an_unnamed_violation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # One alias, two writes: sameness is decided on the paths too, so
            # neither record is evidence for the other.
            self._mixed_scope_violations(
                history,
                turn_violations=[
                    {
                        "alias": "docs",
                        "repository": "docs",
                        "qualified_paths": ["docs:guide.md"],
                    }
                ],
                paused_violations=[
                    {
                        "alias": "docs",
                        "repository": "docs",
                        "qualified_paths": ["docs:reference.md"],
                    }
                ],
            )

            _, results = self._audit(history)

            # Nothing was committed, so both readings stay catches; what this
            # pins is that they stay two.
            caught = results["scope-guard-catch"]
            self.assertEqual("fail", caught.status)
            self.assertEqual(2, caught.detail.count("wrote outside"))
            self.assertIn("an unnamed session", caught.detail)
            self.assertIn("session 1", caught.detail)
            self.assertEqual("pass", results["scope-escape"].status)

    def test_a_repair_that_still_lists_violations_escaped_rather_than_caught(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                        {
                            "run_id": RUN_ID,
                            "details": {
                                "session_id": "01",
                                "workspace_write_guard": {
                                    "passed": False,
                                    "write_aliases": ["app"],
                                    "authorized_aliases": ["app"],
                                    "violations": [{"alias": "docs"}],
                                    "unavailable": {},
                                },
                            },
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn(
                "still excludes retained changes", results["scope-escape"].detail
            )
            self.assertEqual("pass", results["scope-guard-catch"].status)

    def test_a_guard_that_could_not_look_is_never_read_as_a_catch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["workspace_write_guard"] = {
                "passed": False,
                "session_id": "01",
                "write_aliases": ["app"],
                "authorized_aliases": ["app"],
                "violations": [],
                "unavailable": {"app": {"reason": "fingerprint-failed"}},
            }
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            # Blindness stays on the critical check; the catch check owns only
            # writes the guard demonstrably held.
            self.assertEqual("unavailable", results["scope-escape"].status)
            self.assertEqual("pass", results["scope-guard-catch"].status)
            self.assertEqual(
                "unavailable",
                supervisor_audit_checks.lane_rollup(
                    [results["scope-escape"], results["scope-guard-catch"]]
                )["task-packet-coherence"],
            )


if __name__ == "__main__":
    unittest.main()
