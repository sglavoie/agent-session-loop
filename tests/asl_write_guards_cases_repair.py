# mypy: ignore-errors
from .asl_write_guards_support import *


class RegenerateStaleRepairChildScopesTests(unittest.TestCase):
    """Which pending children a scope-stale projection gets to widen."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        plan_dir = Path(self._tmp.name).resolve() / "plan"
        write_plan(plan_dir, ("complete", "pending", "pending"))
        self.snapshot = plans_snapshot.inspect_plan(plan_dir)
        self.workspace = _workspace("SVC")

    def _regenerate(
        self,
        gaps: tuple[dict[str, Any], ...],
        *,
        regenerated: Any = None,
        scope_error: bool = False,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        widened: list[str] = []

        def widen(*, session: Any, plan_dir: Any, workspace: Any, scope: Any) -> Any:
            widened.append(session.id)
            return regenerated

        scope = plans_session_scope.SessionScope(reads=("SVC",), writes=("SVC",))
        with (
            mock.patch.object(
                run_prompts,
                "_session_scope",
                side_effect=plans_errors.PlanError("unparseable")
                if scope_error
                else None,
                return_value=scope,
            ),
            mock.patch.object(
                workspace_evidence,
                "_regenerated_repair_child_write_scope",
                side_effect=widen,
            ),
        ):
            records = write_guards_repair._regenerate_stale_repair_child_scopes(
                snapshot=self.snapshot,
                workspace=self.workspace,
                gaps=gaps,
            )
        return records, widened

    def test_every_pending_child_the_projection_named_is_widened(self) -> None:
        records, widened = self._regenerate(
            ({"repair_session_id": "2"}, {"repair_session_id": "3"}),
            regenerated={"widened": True},
        )
        self.assertEqual(["2", "3"], widened)
        self.assertEqual([{"widened": True}, {"widened": True}], records)

    def test_a_child_that_is_not_pending_is_left_alone(self) -> None:
        # Session 1 is complete: rewriting a finished session's persisted scope
        # would rewrite a record the run already acted on.
        _, widened = self._regenerate(({"repair_session_id": "1"},))
        self.assertEqual([], widened)

    def test_a_gap_naming_no_session_this_plan_holds_is_skipped(self) -> None:
        _, widened = self._regenerate(
            ({"repair_session_id": "99"}, {"repair_session_id": None}, {})
        )
        self.assertEqual([], widened)

    def test_a_scope_that_cannot_be_parsed_keeps_the_blocker(self) -> None:
        # Fail open: a child whose scope nobody can read is not rewritten from
        # a scope nobody could derive.
        records, widened = self._regenerate(
            ({"repair_session_id": "2"},), scope_error=True
        )
        self.assertEqual([], widened)
        self.assertEqual([], records)

    def test_a_child_the_widener_declined_contributes_no_record(self) -> None:
        records, widened = self._regenerate(
            ({"repair_session_id": "2"},), regenerated=None
        )
        self.assertEqual(["2"], widened)
        self.assertEqual([], records)


class PendingRepairChildContinuationTests(unittest.TestCase):
    """When the gate clears a stale scope, and what it returns afterwards."""

    def setUp(self) -> None:
        self.workspace = _workspace("SVC")
        self.refreshed = 0

    def _project(
        self,
        *projections: review_continuation.ReviewResume,
        regenerated: list[dict[str, Any]] | None = None,
        legacy: bool = False,
    ) -> tuple[review_continuation.ReviewResume, int, int]:
        widened = 0

        def regenerate(**_: Any) -> list[dict[str, Any]]:
            nonlocal widened
            widened += 1
            return list(regenerated or [])

        def refresh(*_: Any) -> None:
            self.refreshed += 1

        with (
            mock.patch.object(
                review_cycle, "_set_review_resume", side_effect=list(projections)
            ),
            mock.patch.object(
                write_guards_repair,
                "_regenerate_stale_repair_child_scopes",
                side_effect=regenerate,
            ),
            mock.patch.object(
                write_guards_repair.readiness_gate,
                "_refresh_active_readiness_state",
                side_effect=refresh,
            ),
        ):
            resume = write_guards_repair._pending_repair_child_continuation(
                snapshot=cast(Any, SimpleNamespace(plan_dir=Path("/tmp/plan"))),
                event_path=Path("/tmp/events.jsonl"),
                payload={},
                persistence=cast(Any, SimpleNamespace()),
                workspace=None if legacy else self.workspace,
                max_repair_group_attempts=3,
                max_review_fix_cycles=3,
                review_requested=True,
            )
        return resume, widened, self.refreshed

    def test_a_stale_scope_is_cleared_once_and_the_gate_reprojects(self) -> None:
        cleared = _resume(eligible=True, blocker=None)
        resume, widened, refreshed = self._project(
            _resume(), cleared, regenerated=[{"session_id": "2"}]
        )
        self.assertIs(cleared, resume)
        self.assertEqual(1, widened)
        self.assertEqual(1, refreshed)

    def test_the_second_projections_verdict_is_returned_even_when_it_still_blocks(
        self,
    ) -> None:
        # Revalidate rather than assume: the rewrite is widening-only and
        # fail-open, so the reprojection is the only proof the gap closed.
        still_blocked = _resume()
        resume, widened, _ = self._project(
            _resume(), still_blocked, regenerated=[{"session_id": "2"}]
        )
        self.assertIs(still_blocked, resume)
        self.assertEqual(1, widened)

    def test_a_regeneration_that_widened_nothing_does_not_reproject(self) -> None:
        first = _resume()
        resume, widened, refreshed = self._project(first, regenerated=[])
        self.assertIs(first, resume)
        self.assertEqual(1, widened)
        self.assertEqual(0, refreshed)

    def test_a_legacy_run_with_no_workspace_never_rewrites_a_scope(self) -> None:
        first = _resume()
        resume, widened, _ = self._project(first, legacy=True)
        self.assertIs(first, resume)
        self.assertEqual(0, widened)

    def test_a_continuation_that_is_not_running_repairs_is_left_alone(self) -> None:
        first = _resume(action="review")
        resume, widened, _ = self._project(first)
        self.assertIs(first, resume)
        self.assertEqual(0, widened)

    def test_an_eligible_continuation_is_left_alone(self) -> None:
        first = _resume(eligible=True)
        resume, widened, _ = self._project(first)
        self.assertIs(first, resume)
        self.assertEqual(0, widened)

    def test_another_blocker_is_not_this_gates_business(self) -> None:
        first = _resume(blocker="repair-group-exhausted")
        resume, widened, _ = self._project(first)
        self.assertIs(first, resume)
        self.assertEqual(0, widened)

    def test_a_stale_blocker_naming_no_gaps_rewrites_nothing(self) -> None:
        first = _resume(gaps=())
        resume, widened, _ = self._project(first)
        self.assertIs(first, resume)
        self.assertEqual(0, widened)
