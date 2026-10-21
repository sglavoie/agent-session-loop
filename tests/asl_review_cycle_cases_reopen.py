# mypy: ignore-errors
from .asl_review_cycle_support import *
from .asl_review_cycle_support import _resume


class ReopenBaselineTests(unittest.TestCase):
    """The tree a reopen says was reviewed, and when it refuses to say.

    A reopen record supersedes the cycle record it descends from as the newest
    repair evidence ``review_delta`` scopes a rereview against. Written without
    the reviewed tree it names no base at all, so every rereview that followed a
    reopen re-read the whole repository — and a baseline copied from the wrong
    cycle would be worse than that, because it would name a tree the reviewer
    never read and narrow the rereview to a diff against it.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.event_path = Path(tmp.name) / "events.jsonl"

    def record_cycle(self, **overrides: Any) -> None:
        record: dict[str, Any] = {
            "cycle": 1,
            "review_session_id": "2",
            "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            "repo_head": "a" * 40,
            "worktree_dirty": True,
            "dirty_paths": ["src/a.py"],
        }
        record.update(overrides)
        events.append_event(
            self.event_path, events.Event(events.REVIEW_CYCLE_EVENT, record)
        )

    def record(self, **overrides: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "event_path": self.event_path,
            "payload": {"run_id": "run-1"},
            "resume": _resume(),
            "max_review_fix_cycles": 3,
            "materialized": True,
            "result": "materialized",
        }
        fields.update(overrides)
        return review_cycle._review_reopen_record(**fields)

    def assert_names_no_baseline(self, record: dict[str, Any]) -> None:
        for key in review_continuation.REVIEW_BASELINE_FIELDS:
            self.assertNotIn(key, record, msg=key)

    def test_a_reopen_carries_the_tree_its_source_review_read(self) -> None:
        self.record_cycle()
        record = self.record()
        self.assertEqual("a" * 40, record["repo_head"])
        self.assertTrue(record["worktree_dirty"])
        self.assertEqual(["src/a.py"], record["dirty_paths"])

    def test_a_clean_reviewed_tree_is_copied_as_the_fact_it_is(self) -> None:
        # `False` is evidence — it says the recorded commit *is* the tree that
        # was reviewed — and dropping it as falsy would turn the strongest
        # baseline a cycle can record into a missing one.
        self.record_cycle(worktree_dirty=False, dirty_paths=[])
        record = self.record()
        self.assertIs(False, record["worktree_dirty"])
        self.assertEqual([], record["dirty_paths"])

    def test_a_registered_reopen_carries_the_ordered_base_vector(self) -> None:
        bases = [{"alias": "SVC", "head": "b" * 40, "worktree_dirty": False}]
        self.record_cycle(repo_head=None, repositories=bases)
        record = self.record()
        self.assertEqual(bases, record["repositories"])
        self.assertNotIn("repo_head", record)

    def test_a_refused_reopen_records_the_baseline_it_was_refused_from(self) -> None:
        # A refusal is lineage too, and the operator reading it to find out why
        # a repair never appeared needs the same account of the reviewed tree a
        # success would have left.
        self.record_cycle()
        record = self.record(materialized=False, result="refused", error="no-budget")
        self.assertEqual("a" * 40, record["repo_head"])

    def test_no_history_at_all_names_no_baseline(self) -> None:
        self.assert_names_no_baseline(self.record())

    def test_a_recovered_backfill_without_a_cycle_event_names_no_baseline(
        self,
    ) -> None:
        # The crash window this backfill describes is exactly the one where the
        # children became durable and the cycle event never did, so there is no
        # record of the reviewed tree to copy and none is invented.
        self.assert_names_no_baseline(
            self.record(recovered=True, resume=_resume(historical_materializations=1))
        )

    def test_another_cycle_is_never_borrowed_as_a_baseline(self) -> None:
        # The convenient answer — the newest cycle that happens to carry a base
        # — is the one that silently narrows a rereview against a tree nobody
        # reviewed.
        self.record_cycle(cycle=2, review_session_id="4")
        self.assert_names_no_baseline(self.record())

    def test_a_cycle_missing_the_identity_asked_for_is_not_a_match(self) -> None:
        # Absence is not agreement: a legacy cycle that recorded no session id
        # cannot be shown to be the review this reopen descends from.
        for omitted in ("cycle", "review_session_id"):
            with self.subTest(omitted=omitted):
                self.event_path.unlink(missing_ok=True)
                self.record_cycle(**{omitted: None})
                self.assert_names_no_baseline(self.record())

    def test_a_cycle_whose_digests_disagree_is_a_different_review(self) -> None:
        self.record_cycle(
            review_output_sha256="c" * 64, review_artifact_sha256="c" * 64
        )
        for field in (
            "source_review_output_sha256",
            "source_review_artifact_sha256",
        ):
            with self.subTest(field=field):
                self.assert_names_no_baseline(
                    self.record(resume=_resume(**{field: "d" * 64}))
                )

    def test_digests_the_cycle_never_recorded_do_not_refuse_the_match(
        self,
    ) -> None:
        # A cycle written before the digests were recorded is silent about
        # them, and silence is not disagreement.
        self.record_cycle()
        record = self.record(resume=_resume(source_review_output_sha256="d" * 64))
        self.assertEqual("a" * 40, record["repo_head"])

    def test_duplicate_cycles_that_disagree_name_no_baseline(self) -> None:
        self.record_cycle()
        self.record_cycle(repo_head="e" * 40)
        self.assert_names_no_baseline(self.record())

    def test_duplicate_cycles_that_agree_still_name_their_baseline(self) -> None:
        self.record_cycle()
        self.record_cycle()
        self.assertEqual("a" * 40, self.record()["repo_head"])


class TerminalReviewReasonTests(unittest.TestCase):
    """The reason is the adjudicator's; this boundary only records it."""

    def test_a_cleared_review_leaves_the_payload_alone(self) -> None:
        payload: dict[str, Any] = {
            "review_fix_disposition": (
                review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
            ),
            "review_verdict": {"outcome": "all-clear", "total_findings": 0},
        }
        reason = review_cycle._terminal_review_reason(payload)
        self.assertEqual(review_transitions.REVIEW_ALL_COMPLETE_REASON, reason)
        self.assertNotIn("ok", payload)
        self.assertNotIn("action_required", payload)

    def test_an_uncleared_review_stops_the_run_claiming_success(self) -> None:
        payload: dict[str, Any] = {
            "review_fix_disposition": (
                review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED
            ),
            "review_verdict": {"outcome": "findings", "total_findings": 2},
        }
        reason = review_cycle._terminal_review_reason(payload)
        self.assertEqual(review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, reason)
        self.assertIs(False, payload["ok"])
        self.assertEqual(2, payload["action_required"]["unresolved_findings"])


class VerdictAttestationTests(unittest.TestCase):
    """What a review left on disk, projected without the reader moving yet."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plan_dir = Path(tmp.name)
        self.calls: list[tuple[Path, bool]] = []

    def reader(self, verdict: dict[str, Any]) -> review_delta.ReviewVerdictReader:
        def read(plan_dir: Path, *, require_artifact: bool = False) -> dict[str, Any]:
            self.calls.append((plan_dir, require_artifact))
            return verdict

        return read

    def test_a_verdict_the_reader_refused_is_not_projected(self) -> None:
        payload: dict[str, Any] = {}
        review_cycle._set_existing_review_verdict(
            payload,
            self.plan_dir,
            read_review_verdict=self.reader({"ok": False, "outcome": "all-clear"}),
        )
        self.assertNotIn("review_verdict", payload)

    def test_an_accepted_verdict_is_projected_field_by_field(self) -> None:
        payload: dict[str, Any] = {}
        review_cycle._set_existing_review_verdict(
            payload,
            self.plan_dir,
            read_review_verdict=self.reader(
                {
                    "ok": True,
                    "status_line": "REVIEW: all-clear",
                    "outcome": "all-clear",
                    "total_findings": 0,
                    "findings_by_severity": {},
                    "unrelated": "dropped",
                }
            ),
        )
        self.assertEqual(
            {
                "status_line",
                "outcome",
                "total_findings",
                "findings_by_severity",
            },
            set(payload["review_verdict"]),
        )

    def test_attestation_mirrors_what_the_run_itself_demanded(self) -> None:
        # `require_artifact` is what keeps a verdict the run refused as invalid
        # from being re-projected here as if it stood.
        payload: dict[str, Any] = {}
        review_cycle._attest_executed_review(
            payload,
            self.plan_dir,
            require_artifact=True,
            read_review_verdict=self.reader({"ok": False}),
        )
        self.assertEqual([(self.plan_dir, True)], self.calls)

    def test_digests_are_recorded_even_when_the_verdict_does_not_hold(self) -> None:
        # The digests describe what the session left on disk, which stays
        # honest evidence about an unattestable review.
        payload: dict[str, Any] = {}
        review_cycle._attest_executed_review(
            payload,
            self.plan_dir,
            require_artifact=False,
            read_review_verdict=self.reader({"ok": False}),
        )
        self.assertNotIn("review_verdict", payload)
        self.assertIsNone(payload["review_output_sha256"])
        self.assertIsNone(payload["review_artifact_sha256"])

    def test_an_already_attested_verdict_is_not_read_a_second_time(self) -> None:
        payload: dict[str, Any] = {"review_verdict": {"outcome": "findings"}}
        review_cycle._attest_executed_review(
            payload,
            self.plan_dir,
            require_artifact=False,
            read_review_verdict=self.reader({"ok": True, "outcome": "all-clear"}),
        )
        self.assertEqual([], self.calls)
        self.assertEqual({"outcome": "findings"}, payload["review_verdict"])

    def test_the_recorder_factory_binds_the_reader_into_the_two_argument_seam(
        self,
    ) -> None:
        """``ExistingReviewVerdictRecorder`` has no room for a reader argument.

        The stop-before-review-work path takes a two-positional callable, so
        the binding happens once here rather than at every stop that reports
        the verdict it already has.
        """
        record = review_cycle.existing_review_verdict_recorder(
            self.reader({"ok": True, "outcome": "all-clear", "total_findings": 0})
        )
        payload: dict[str, Any] = {}
        record(payload, self.plan_dir)
        self.assertEqual([(self.plan_dir, False)], self.calls)
        self.assertEqual("all-clear", payload["review_verdict"]["outcome"])


class RecordReviewCycleTests(unittest.TestCase):
    """One record, two surfaces, one lock."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.event_path = Path(tmp.name) / "events.jsonl"

    def test_the_disposition_reaches_the_record_the_state_and_the_event(self) -> None:
        payload: dict[str, Any] = {}
        cycle_record: dict[str, Any] = {"run_id": "run-1", "cycle": 1}
        review_cycle._record_review_cycle(
            event_path=self.event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(write_state=False),
            cycle_record=cycle_record,
            disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
        )
        disposition = review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
        self.assertEqual(disposition, cycle_record["disposition"])
        self.assertEqual(disposition, payload["review_fix_disposition"])
        self.assertEqual([cycle_record], payload["review_cycles"])
        appended = [
            entry
            for entry in read_jsonl(self.event_path)
            if entry.get("name") == events.REVIEW_CYCLE_EVENT
        ]
        self.assertEqual(1, len(appended))
        self.assertEqual(disposition, appended[0]["payload"]["disposition"])

    def test_cycles_accumulate_rather_than_replace(self) -> None:
        payload: dict[str, Any] = {}
        for cycle in (1, 2):
            review_cycle._record_review_cycle(
                event_path=self.event_path,
                payload=payload,
                persistence=run_persistence._RunPersistence(write_state=False),
                cycle_record={"cycle": cycle},
                disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            )
        self.assertEqual(
            [1, 2], [record["cycle"] for record in payload["review_cycles"]]
        )
        self.assertTrue(json.dumps(payload["review_cycles"]))
