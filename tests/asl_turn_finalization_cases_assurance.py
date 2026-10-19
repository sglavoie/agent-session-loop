from .asl_turn_finalization_support import *


class DegradedAssuranceTests(unittest.TestCase):
    """Optional assurance may fail to speak, but it may not fail silently."""

    def setUp(self) -> None:
        self.persistence = run_persistence._RunPersistence(
            write_state=False, append_events=False
        )

    def assurance(
        self,
        state: str,
        reason: str | None,
        *,
        issue_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return assurance_models_summary.AssuranceSummary(
            run_id="20260619T150000000000Z-4242",
            state=state,
            eligible=True,
            reason=reason,
            issue_ids=issue_ids,
            new_issue_count=len(issue_ids),
            artifacts=(
                assurance_models_findings.EvidenceRef(
                    kind="state", locator="state/state.json", detail="run state"
                ),
            ),
        ).to_dict()

    def record(self, payload: dict[str, Any]) -> None:
        run_coordination_terminal.record_degraded_assurance(
            payload, persistence=self.persistence
        )

    def test_an_audit_that_could_not_speak_degrades_a_success(self) -> None:
        payload = {
            "ok": True,
            "paused": False,
            "reason": "all-complete",
            "assurance": self.assurance(
                "unavailable", supervisor_audit.REASON_NO_INDEPENDENT_ROLE
            ),
        }

        self.record(payload)

        degraded = payload["assurance_degraded"]
        self.assertEqual("unavailable", degraded["assurance_state"])
        self.assertEqual(
            supervisor_audit.REASON_NO_INDEPENDENT_ROLE, degraded["reason"]
        )
        self.assertFalse(degraded["blocking"])
        self.assertTrue(degraded["run_ok"])
        self.assertEqual(["state/state.json"], degraded["evidence"])
        # Advisory means advisory: the run keeps the success it earned.
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])

    def test_a_partial_audit_is_degraded_too(self) -> None:
        payload = {
            "ok": True,
            "reason": "all-complete",
            "assurance": self.assurance("partial", "attempts-exhausted"),
        }

        self.record(payload)

        self.assertEqual("partial", payload["assurance_degraded"]["assurance_state"])

    def test_a_settled_skip_is_not_a_degradation(self) -> None:
        payload = {
            "ok": True,
            "reason": "all-complete",
            "assurance": self.assurance("skipped", supervisor_audit.REASON_OPTED_OUT),
        }

        self.record(payload)

        self.assertNotIn("assurance_degraded", payload)

    def test_a_graded_run_carries_no_degradation_note(self) -> None:
        payload = {
            "ok": True,
            "reason": "all-complete",
            "assurance": self.assurance("all-clear", None),
        }

        self.record(payload)

        self.assertNotIn("assurance_degraded", payload)

    def test_a_stale_note_is_dropped_once_the_audit_speaks(self) -> None:
        payload = {
            "ok": True,
            "reason": "all-complete",
            "assurance_degraded": {"assurance_state": "unavailable"},
            "assurance": self.assurance("findings", None, issue_ids=("ASI-999",)),
        }

        self.record(payload)

        self.assertNotIn("assurance_degraded", payload)


if __name__ == "__main__":
    unittest.main()
