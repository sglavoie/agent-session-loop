"""Cover the HTML shell, detail view, recovery center and safe confirmations.

Split from tests/test_asl_workbench_static.py."""

import unittest

from tests import support_asl_workbench_static as support_static
from tests.support import (
    _http_request_raw,
)


class WorkbenchStaticShellTests(support_static.WorkbenchStaticCase):
    """Cover the HTML shell, detail view, recovery center and safe confirmations."""

    def test_root_html_references_template_id(self) -> None:
        # Smoke check that the template the JS app depends on is bundled.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        decoded = body.decode("utf-8")
        self.assertIn('id="wb-row-template"', decoded)
        self.assertIn('id="wb-filters"', decoded)
        self.assertIn('id="wb-queue-meta"', decoded)
        self.assertIn('id="wb-queue"', decoded)

    def test_root_html_bundles_detail_view_shell(self) -> None:
        # The detail view markup, controls, profile form, embedded reply
        # flow, and row templates must ship in index.html so the SPA can
        # render the per-plan route without additional fetches.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        decoded = body.decode("utf-8")
        required_ids = (
            'id="wb-detail"',
            'id="wb-detail-back"',
            'id="wb-recovery-panel"',
            'id="wb-controls"',
            'id="wb-control-launch"',
            'id="wb-control-stop"',
            'id="wb-control-abort"',
            'id="wb-control-abort-grace"',
            'id="wb-control-unlock"',
            'id="wb-control-archive"',
            'id="wb-profile-form"',
            'id="wb-profile-form-errors"',
            'id="wb-profile-unpin"',
            'id="wb-reply-panel"',
            'id="wb-reply-form"',
            "novalidate",
            'id="wb-reply-prompt"',
            'id="wb-reply-last-message"',
            'id="wb-reply-text"',
            'id="wb-reply-submit-status"',
            'id="wb-detail-sessions"',
            'id="wb-detail-events"',
            'id="wb-detail-progress"',
            'id="wb-detail-artifacts"',
            'id="wb-session-row-template"',
            'id="wb-event-row-template"',
            'id="wb-progress-row-template"',
            'id="wb-artifact-row-template"',
        )
        for marker in required_ids:
            self.assertIn(marker, decoded, msg=f"missing marker: {marker}")
        recovery_idx = decoded.find('id="wb-recovery-panel"')
        reply_idx = decoded.find('id="wb-reply-panel"')
        controls_idx = decoded.find('id="wb-controls"')
        self.assertLess(recovery_idx, reply_idx)
        self.assertLess(reply_idx, controls_idx)
        self.assertNotIn('<section class="wb-panel" id="wb-reply-panel"', decoded)

    def test_root_html_bundles_recovery_center_sections(self) -> None:
        # The Recovery Center is the first actionable detail panel and must
        # ship all hosts the JS renderer updates on each detail refresh.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        decoded = body.decode("utf-8")
        required_markers = (
            'class="wb-panel wb-recovery"',
            'id="wb-recovery-panel"',
            'data-severity="none"',
            'id="wb-recovery-title"',
            'id="wb-recovery-kind"',
            'id="wb-recovery-summary"',
            'id="wb-recovery-primary-action"',
            'id="wb-recovery-evidence-section"',
            'id="wb-recovery-evidence"',
            'id="wb-recovery-commands-section"',
            'id="wb-recovery-commands"',
            'id="wb-recovery-artifacts-section"',
            'id="wb-recovery-artifacts"',
        )
        for marker in required_markers:
            self.assertIn(marker, decoded, msg=f"missing marker: {marker}")
        for text in (
            "Recovery Center",
            "loading recovery diagnosis",
            "Evidence",
            "Commands",
            "Related artifacts",
            "asl run --reply",
            "never persisted to disk or browser storage",
        ):
            self.assertIn(text, decoded, msg=f"missing recovery text: {text}")
        recovery_idx = decoded.find('id="wb-recovery-panel"')
        controls_idx = decoded.find('id="wb-controls"')
        self.assertNotEqual(-1, recovery_idx)
        self.assertLess(recovery_idx, controls_idx)

    def test_app_js_enforces_safe_control_confirmations(self) -> None:
        # The detail view must confirm destructive or sensitive actions
        # before issuing the POST. These strings are load-bearing — the
        # reply textarea must also never be persisted to browser storage.
        server = self._start_server()
        decoded = self._shell_source(server)
        # Hash routing wiring + detail-view scaffolding.
        self.assertIn("parseHashRoute", decoded)
        self.assertIn("refreshDetail", decoded)
        self.assertIn("validateProfilePayload", decoded)
        self.assertIn("validateLaunchPayload", decoded)
        self.assertIn("harness_model", decoded)
        self.assertIn("review_harness_model", decoded)
        self.assertIn("max_review_fix_cycles", decoded)
        self.assertIn("review model requires a review harness", decoded)
        self.assertIn("clearReplyTextarea", decoded)
        self.assertIn('reply: { scroll: "wb-recovery-panel"', decoded)
        self.assertIn('renderedRecovery.kind === "reply-needed"', decoded)
        self.assertIn("renderRecoveryActions", decoded)
        self.assertIn("RECOVERY_ACTION_TARGETS_BY_ID", decoded)
        self.assertIn("handleRecoveryAction", decoded)
        self.assertIn('endpoint === "/api/unlock"', decoded)
        self.assertIn('endpoint === "/api/launch"', decoded)
        # Session 17 moved every mutation onto the shared `runMutation` path,
        # so the archive names its action and its endpoint rather than calling
        # `apiRequest` and reading success off `body.ok`.
        self.assertIn('action: "archive-plan"', decoded)
        self.assertIn('path: "/api/plan/archive"', decoded)
        self.assertIn("await handleUnlockClick()", decoded)
        self.assertIn("await handleLaunchClick()", decoded)
        self.assertIn("handleArchiveClick", decoded)
        self.assertIn("confirm_plan_dir", decoded)
        self.assertIn("clearSelectedPlan();", decoded)
        self.assertIn("navigateToQueue();", decoded)
        self.assertIn("renderReplyContextError", decoded)
        self.assertNotIn("tasks.push(loadAndRenderReply", decoded)
        for reason in (
            "no-paused-run",
            "missing-question-context",
            "reply-context-mismatch",
        ):
            self.assertIn(reason, decoded)
        # Confirmation prompts for dangerous actions.
        self.assertIn("--unsafe-bypass", decoded)
        self.assertIn("Force-unlock this plan?", decoded)
        self.assertIn("Abort the in-flight turn?", decoded)
        self.assertIn('path: "/api/control/abort"', decoded)
        # The confirmation must gate the POST, not follow it.
        self.assertLess(
            decoded.find("Abort the in-flight turn?"),
            decoded.find('path: "/api/control/abort"'),
        )
        self.assertIn("Archive this plan?", decoded)
        self.assertIn("Type the full plan path to confirm", decoded)
        self.assertIn("Submit this reply", decoded)
        # Privacy guard: the reply text must never be persisted.
        # Search for any sessionStorage/localStorage write touching the
        # reply field; a positive match would mean the draft leaks.
        import re as _re

        leak_patterns = (
            r"sessionStorage\.setItem\([^)]*reply",
            r"localStorage\.setItem\([^)]*reply",
            r"setItem\([^)]*wb\.reply",
        )
        for pattern in leak_patterns:
            self.assertIsNone(
                _re.search(pattern, decoded),
                msg=f"reply text persisted via {pattern!r}",
            )

    def test_app_js_recovery_center_refresh_focus_and_privacy_contract(
        self,
    ) -> None:
        # Recovery Center rendering must update from compact queue/plan data
        # first, then replace it with /api/recovery details during every
        # detail refresh. Actions may focus existing panels or delegate to
        # launch/unlock handlers, but reply text still never reaches storage.
        server = self._start_server()
        decoded = self._shell_source(server)
        for section in (
            "Recovery Center loading",
            "Recovery Center panel rendering",
            "Recovery Center actions",
            "Recovery Center evidence",
            "Recovery Center commands",
            "Recovery Center artifacts",
            "Recovery Center target resolution",
            "Recovery Center dispatch",
            "Recovery Center copy UX",
        ):
            self.assertIn(section, decoded, msg=f"missing section: {section}")
        for helper in (
            "loadAndRenderRecovery",
            "renderRecoveryPanel",
            "renderRecoveryActions",
            "createRecoveryActionCard",
            "renderRecoveryEvidence",
            "renderRecoveryCommands",
            "renderRecoveryArtifacts",
            "copyRecoveryCommand",
            "focusDetailKey",
            "normalizeRecoveryActionTarget",
            "hasExplicitRecoveryActionTarget",
            "legacyRecoveryActionTarget",
        ):
            self.assertIn(helper, decoded, msg=f"missing helper: {helper}")
        self.assertIn("loadAndRenderRecovery(", decoded)
        self.assertIn("Array.isArray(detailBody.runs) ? detailBody.runs : []", decoded)
        self.assertIn("state.route.runKey", decoded)
        self.assertIn("state.detailPlan.run_key", decoded)
        self.assertIn('planUrl("/api/recovery")', decoded)
        self.assertIn("renderRecoveryPanel(compact)", decoded)
        self.assertIn("renderRecoveryPanel(body.recovery)", decoded)
        self.assertIn('renderedRecovery.kind === "reply-needed"', decoded)
        self.assertIn("hasLiveQuestion(runs, linkedRunKey, linkedQuestionId)", decoded)
        self.assertIn("run.run_key !== linkedRunKey", decoded)
        self.assertIn("run.question_id !== linkedQuestionId", decoded)
        self.assertIn('typeof run.question_id === "string"', decoded)
        self.assertIn("Number(run.unanswered_count) > 0", decoded)
        self.assertIn('context.context_kind === "waiting"', decoded)
        self.assertIn("context.question_id", decoded)
        self.assertIn('"Reply recorded" +', decoded)
        self.assertIn('" as operation " +', decoded)
        self.assertIn("Waiting for the running session to consume it", decoded)
        self.assertIn("Reply delivered. The question is closed", decoded)
        self.assertIn("if (submit.textContent !== label)", decoded)
        self.assertIn("if (node.disabled !== disabled)", decoded)
        self.assertIn("await loadAndRenderReply()", decoded)
        self.assertIn("hideReplyPanel()", decoded)
        self.assertIn("RECOVERY_ACTION_TARGETS", decoded)
        self.assertIn("RECOVERY_ACTION_TARGETS_BY_ID", decoded)
        self.assertIn("isRecoveryActionTarget(value)", decoded)
        self.assertIn("return legacyRecoveryActionTarget(action);", decoded)
        self.assertIn(
            "if (hasExplicitRecoveryActionTarget(action)) return null;",
            decoded,
        )
        for action_id in (
            "reply",
            "unlock",
            "retry-run",
            "inspect-artifacts",
            "review-profile",
        ):
            self.assertIn(f'"{action_id}"', decoded)
        self.assertIn('endpoint === "/api/unlock"', decoded)
        self.assertIn('endpoint === "/api/launch"', decoded)
        self.assertIn("await handleUnlockClick()", decoded)
        self.assertIn("await handleLaunchClick()", decoded)
        self.assertIn("focusDetailKey(target)", decoded)
        self.assertIn('button.textContent = "copy"', decoded)
        self.assertIn('copyRecoveryCommand(button, command.command || "")', decoded)
        for text in (
            "Recovery unavailable",
            "recovery diagnosis unavailable",
            "No recovery diagnosis is available.",
            "Recovery details failed to load:",
        ):
            self.assertIn(text, decoded, msg=f"missing recovery text: {text}")
        import re as _re

        leak_patterns = (
            r"sessionStorage\.setItem\([^)]*reply",
            r"localStorage\.setItem\([^)]*reply",
            r"setItem\([^)]*wb\.reply",
        )
        for pattern in leak_patterns:
            self.assertIsNone(
                _re.search(pattern, decoded),
                msg=f"reply text persisted via {pattern!r}",
            )


if __name__ == "__main__":
    unittest.main()
