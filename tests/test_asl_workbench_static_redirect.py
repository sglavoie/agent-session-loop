"""Cover the urgent-correction composer and conversation recovery front end.

Split from tests/test_asl_workbench_static.py."""

import unittest

from tests import support_asl_workbench_static as support_static
from tests.support import (
    _http_request_raw,
)

ASL_TEST_LANE = "integration"


class WorkbenchStaticRedirectTests(support_static.WorkbenchStaticCase):
    """Cover the urgent-correction composer and conversation recovery front end."""

    def test_root_html_ships_urgent_correction_composer(self) -> None:
        # The composer's promises are made in the markup, so they are pinned
        # there: an explicitly labeled interrupt-and-resume action, a required
        # textarea, and prose that says the draft never leaves this tab.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        markup = body.decode("utf-8")
        for fragment in (
            'id="wb-redirect-panel"',
            'id="wb-redirect-form"',
            'id="wb-redirect-text"',
            'id="wb-redirect-operation"',
            'id="wb-redirect-submit"',
            'id="wb-redirect-clear"',
            'id="wb-redirect-availability"',
            'id="wb-redirect-target"',
            'id="wb-redirect-unresolved-section"',
            'id="wb-redirect-progress-section"',
            'id="wb-redirect-phases"',
            'id="wb-redirect-operations"',
            'id="wb-redirect-conflict"',
            'id="wb-redirect-conflict-message"',
            'id="wb-redirect-new-identity"',
            'data-action="redirect-submit"',
            'data-action="redirect-new-identity"',
        ):
            self.assertIn(fragment, markup, msg=f"missing composer markup: {fragment}")
        # The way out of a refused replay is a labeled control, not a hidden
        # retry: it says it keeps the draft and that the result is a new
        # correction, because the alternative reading -- resending the old one
        # -- is the mistake the refusal exists to prevent.
        self.assertIn("keep this draft and send it as a new correction", markup)
        # The action names both halves of what it does. A button labeled only
        # "submit" would hide the interruption behind a confirm dialog.
        self.assertIn("interrupt turn &amp; resume with this correction", markup)
        self.assertIn("interrupts the in-flight turn now", markup)
        self.assertIn("resumes the saved conversation", markup)
        # Memory-only is a promise the page makes in prose as well as in code.
        # Normalized because the prose is wrapped across source lines.
        prose = " ".join(markup.split())
        self.assertIn("The draft lives in this tab only", prose)
        self.assertIn("never written to disk or to browser storage", prose)
        self.assertIn("cleared only once the correction is durably recorded", prose)
        self.assertIn("it is kept when a submission fails", prose)
        self.assertIn("required", markup)

    def test_root_html_ships_conversation_recovery_group(self) -> None:
        # Recovery that discards an agent's context is reported, never offered
        # as a button: the markup carries the facts, the commands, and the
        # corrections a fresh start still inherits.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/")
        markup = body.decode("utf-8")
        for fragment in (
            'id="wb-conversation-recovery"',
            'id="wb-conversation-facts"',
            'id="wb-conversation-note"',
            'id="wb-conversation-commands"',
            'id="wb-conversation-retained"',
        ):
            self.assertIn(fragment, markup, msg=f"missing recovery markup: {fragment}")
        self.assertIn("reports it", markup)
        self.assertIn("never performs it", markup)
        # No control in that group may perform the recovery.
        self.assertNotIn('data-action="conversation-recover"', markup)
        self.assertNotIn('data-action="fresh-conversation"', markup)

    def test_app_js_redirect_vocabulary_mirrors_python_constants(self) -> None:
        # Every stage redirect_cli can emit must map to a phase chip. A stage
        # with no mapping would render as "unresolved" forever, which is the
        # one thing an operator watching a correction must not be told wrongly.
        import re as _re

        from agent_session_loop import intervention, redirect_cli, redirect_store_vocab

        server = self._start_server()
        decoded = self._shell_source(server)

        stage_match = _re.search(
            r"const REDIRECT_STAGE_PHASES = \{(.*?)\n\};",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(stage_match)
        assert stage_match is not None
        js_stages = tuple(_re.findall(r"(\w+):", stage_match.group(1)))
        self.assertEqual(redirect_cli.STAGES, js_stages)

        phases_match = _re.search(
            r"const REDIRECT_PHASES = \[(.*?)\];", decoded, flags=_re.DOTALL
        )
        self.assertIsNotNone(phases_match)
        assert phases_match is not None
        js_phases = tuple(_re.findall(r'"([^"]+)"', phases_match.group(1)))
        # Acknowledgment is its own phase, separate from cancelling the turn
        # and from resuming with the correction.
        self.assertIn("acknowledged", js_phases)
        self.assertIn("cancelling", js_phases)
        self.assertIn("resuming", js_phases)

        labels_match = _re.search(
            r"const REDIRECT_PHASE_LABELS = \{(.*?)\n\};",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(labels_match)
        assert labels_match is not None
        js_labels = tuple(_re.findall(r"(\w+):", labels_match.group(1)))
        self.assertEqual(js_phases, js_labels)

        phase_targets = set(_re.findall(r'"([^"]+)",', stage_match.group(1)))
        self.assertTrue(phase_targets.issubset(set(js_phases)))

        self.assertIn(
            f'const REDIRECT_ACTION = "{intervention.ACTION_REDIRECT}";', decoded
        )
        self.assertIn(
            "const REDIRECT_MAX_FEEDBACK_BYTES = "
            f"{redirect_store_vocab.REDIRECT_MAX_FEEDBACK_BYTES};",
            decoded,
        )
        self.assertIn(
            "const REDIRECT_MAX_OPERATION_ID_CHARS = "
            f"{redirect_store_vocab.REDIRECT_MAX_OPERATION_ID_CHARS};",
            decoded,
        )
        # A refused replay is recognized by its reason, so the string has to be
        # the server's: a drifted copy would render the refusal as a generic
        # error and clear nothing up about the draft still on screen.
        self.assertIn(
            "const REDIRECT_CONFLICT_REASON = "
            f'"{redirect_store_vocab.ERROR_REPLAY_CONFLICT}";',
            decoded,
        )
        self.assertIn(f'=== "{redirect_store_vocab.CONFLICT_TARGET}"', decoded)

    def test_app_js_redirect_reconcile_hint_mirrors_cli_text(self) -> None:
        # A browser that lost its answer and the operator reading
        # `asl control redirect-status` must be told the same thing about what
        # a resubmission with the same id means.
        import re as _re

        from agent_session_loop import redirect_cli

        server = self._start_server()
        decoded = self._shell_source(server)
        hint_match = _re.search(
            r"const REDIRECT_RECONCILE_HINT =(.*?);\n", decoded, flags=_re.DOTALL
        )
        self.assertIsNotNone(hint_match)
        assert hint_match is not None
        js_hint = "".join(_re.findall(r'"([^"]*)"', hint_match.group(1)))
        self.assertEqual(redirect_cli.RECONCILE_HINT, js_hint)

    def test_app_js_redirect_keeps_operation_identity_and_draft(self) -> None:
        # The three rules the composer exists to honor: one id per draft kept
        # across retries, a clear only on a durable answer, and a guard that
        # makes a second click not a second correction.
        import re as _re

        server = self._start_server()
        decoded = self._shell_source(server)

        self.assertIn("function currentRedirectOperationId()", decoded)
        self.assertIn("function mintRedirectOperationId()", decoded)
        self.assertIn("async function handleRedirectSubmit(event)", decoded)

        handler = _re.search(
            r"async function handleRedirectSubmit\(event\) \{(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(handler)
        assert handler is not None
        source = handler.group(1)
        # Double-click guard: refused before the confirm dialog, and the button
        # is disabled for the duration of the request.
        self.assertIn("if (state.redirect.inFlight)", source)
        self.assertIn("state.redirect.inFlight = true;", source)
        self.assertIn("setRedirectSubmitDisabled(true);", source)
        # The id is minted once and reused; the draft is cleared only inside
        # the ok branch, and every failure path leaves the textarea alone.
        self.assertIn("const operationId = currentRedirectOperationId();", source)
        self.assertIn("if (body && body.ok === true) {", source)
        ok_branch, _, failure = source.partition("if (body && body.ok === true) {")
        cleared, _, rest = failure.partition("} else {")
        self.assertIn("resetRedirectComposer();", cleared)
        self.assertNotIn("resetRedirectComposer();", rest)
        self.assertNotIn("resetRedirectComposer();", ok_branch)
        # A replay is durable too: the journal already holds this id.
        self.assertIn("body.redirect.replayed", source)
        # The freshness proof is the server's, never derived locally.
        self.assertIn("const token = state.redirect.actionToken;", source)

    def test_app_js_redirect_conflict_keeps_the_draft_and_sends_nothing(self) -> None:
        # The lost-response case with an edited draft: the server refuses the
        # replay because the id already names different words, and the tab is
        # then holding a correction no one has accepted. Clearing it would lose
        # the operator's text; resending it under a fresh id behind their back
        # would be a second correction they never asked for. So the branch
        # explains, keeps, and waits.
        import re as _re

        server = self._start_server()
        decoded = self._shell_source(server)

        handler = _re.search(
            r"async function handleRedirectSubmit\(event\) \{(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(handler)
        assert handler is not None
        _, _, failure = handler.group(1).partition("if (body && body.ok === true) {")
        _, _, refused = failure.partition("} else {")
        self.assertIn("if (isRedirectConflict(body))", refused)
        self.assertIn("showRedirectConflict(body, operationId);", refused)
        # Whatever else the branch does, it does not clear the composer and it
        # does not mint a replacement id on the operator's behalf.
        self.assertNotIn("resetRedirectComposer();", refused)
        self.assertNotIn("mintRedirectOperationId()", refused)

        conflict = _re.search(
            r"function showRedirectConflict\(body, operationId\) \{(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(conflict)
        assert conflict is not None
        shown = conflict.group(1)
        # Which half of the identity disagreed, that nothing landed, and how to
        # reconcile -- the same hint the CLI gives.
        self.assertIn("names a different turn than", shown)
        self.assertIn("carries different words from", shown)
        self.assertIn("Nothing was resubmitted", shown)
        self.assertIn("REDIRECT_RECONCILE_HINT", shown)
        self.assertIn("Your draft is kept below.", shown)
        # Body-free like every other redirect frame: a digest identifies the
        # recorded correction, never its words.
        self.assertIn("recorded.feedback_digest", shown)
        self.assertEqual({"feedback_digest"}, set(_re.findall(r"feedback\w*", shown)))

        adopt = _re.search(
            r"function adoptNewRedirectIdentity\(\) \{(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(adopt)
        assert adopt is not None
        adopted = adopt.group(1)
        # The explicit way out: a new id for the draft already on screen, and
        # not one byte sent until the operator submits again.
        self.assertIn(
            "state.redirect.operationId = mintRedirectOperationId();", adopted
        )
        self.assertNotIn("apiRequest(", adopted)
        self.assertNotIn("textarea.value = ", adopted)
        self.assertIn("second correction", adopted)
        self.assertIn(
            'newIdBtn.addEventListener("click", adoptNewRedirectIdentity);', decoded
        )

    def test_app_js_redirect_availability_comes_from_the_server(self) -> None:
        # Whether a correction is possible is a property of the run, not of the
        # browser. The composer reads permitted_actions/disabled_reasons rather
        # than inferring availability from the queue row it was opened from.
        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertIn('planUrl("/api/redirect-status")', decoded)
        self.assertIn("body.permitted_actions", decoded)
        self.assertIn("body.disabled_reasons", decoded)
        self.assertIn(
            "state.redirect.actionToken = body.action_token || null;", decoded
        )
        self.assertIn("function setRedirectSubmitDisabled(disabled)", decoded)
        self.assertIn("/api/control/redirect", decoded)
        self.assertIn('planUrl("/api/conversation-recovery")', decoded)
        # And the reason is read back to the operator in the line that
        # otherwise says what a submission would do. Leaving it in the badge's
        # tooltip would hide the one fact a disabled button raises.
        self.assertIn('el.textContent = "Cannot submit: " + disabledReason;', decoded)

    def test_app_js_redirect_recovers_unresolved_operations_after_reload(self) -> None:
        # A reload wipes the composer, so the journal must be what repopulates
        # the panel. Anything still unanswered is surfaced by id with the
        # reconciliation hint instead of inviting a blind resubmission.
        import re as _re

        from agent_session_loop import redirect_cli

        server = self._start_server()
        decoded = self._shell_source(server)
        unresolved_match = _re.search(
            r"const REDIRECT_UNRESOLVED_STAGES = \[(.*?)\];", decoded, flags=_re.DOTALL
        )
        self.assertIsNotNone(unresolved_match)
        assert unresolved_match is not None
        js_unresolved = tuple(_re.findall(r'"([^"]+)"', unresolved_match.group(1)))
        self.assertIn(redirect_cli.STAGE_UNCERTAIN, js_unresolved)
        self.assertIn(redirect_cli.STAGE_SUBMITTED, js_unresolved)
        for stage in js_unresolved:
            self.assertIn(stage, redirect_cli.STAGES)
        self.assertIn("function renderRedirectUnresolved(operations)", decoded)
        self.assertIn("hint.textContent = REDIRECT_RECONCILE_HINT;", decoded)
        # A failed poll must not erase a correction from the panel: it leaves
        # the operation lists alone and only closes the action.
        unavailable = _re.search(
            r"function renderRedirectUnavailable\(message\) \{(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(unavailable)
        assert unavailable is not None
        self.assertNotIn("renderRedirectOperations(", unavailable.group(1))
        self.assertNotIn("renderRedirectUnresolved(", unavailable.group(1))
        self.assertIn("setRedirectSubmitDisabled(true);", unavailable.group(1))
        # Switching plans discards the draft and this tab's attribution rather
        # than carrying an id across into a journal that never saw it.
        self.assertIn("function resetRedirectComposer()", decoded)
        self.assertIn("function resetRedirectForPlan()", decoded)
        route = _re.search(
            r"function applyRoute\(\) \{(.*?)\n\}\n", decoded, flags=_re.DOTALL
        )
        self.assertIsNotNone(route)
        assert route is not None
        self.assertIn("resetRedirectForPlan();", route.group(1))

    def test_app_js_conversation_recovery_is_reported_not_performed(self) -> None:
        # Both pauses this covers need a person. The panel may render commands
        # to copy; it may not POST anything that performs the recovery.
        import re as _re

        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertIn("async function loadAndRenderConversationRecovery()", decoded)
        self.assertIn("function renderConversationRecovery(payload)", decoded)
        self.assertIn("function renderConversationRetained(operations)", decoded)
        self.assertIn("payload.authorization_command", decoded)
        self.assertIn("payload.inspect_command", decoded)
        self.assertIn("payload.one_use === true", decoded)
        self.assertIn("payload.retained_operations", decoded)
        loader = _re.search(
            r"async function loadAndRenderConversationRecovery\(\) \{"
            r"(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(loader)
        assert loader is not None
        self.assertNotIn('method: "POST"', loader.group(1))
        renderer = _re.search(
            r"function renderConversationRecovery\(payload\) \{(.*?)\n\}\n",
            decoded,
            flags=_re.DOTALL,
        )
        self.assertIsNotNone(renderer)
        assert renderer is not None
        self.assertNotIn("apiRequest", renderer.group(1))

    def test_app_js_redirect_text_is_never_persisted(self) -> None:
        # Privacy regression, same shape as the reply draft: a correction is
        # the most sensitive text this UI holds and must never reach storage,
        # the hash, or a rendered operation row.
        import re as _re

        server = self._start_server()
        decoded = self._shell_source(server)
        self.assertNotIn('"wb.redirect"', decoded)
        leak_patterns = (
            r"sessionStorage\.setItem\([^)]*redirect",
            r"localStorage\.setItem\([^)]*redirect",
            r"setItem\([^)]*wb\.redirect",
            r"setItem\([^)]*wb-redirect-text",
        )
        for pattern in leak_patterns:
            self.assertIsNone(
                _re.search(pattern, decoded),
                msg=f"redirect text persisted via {pattern!r}",
            )
        # Rows show the digest the journal recorded, never the body.
        self.assertIn("operation.feedback_digest", decoded)
        self.assertNotIn("operation.feedback_text", decoded)

    def test_static_css_separates_acknowledged_from_cancelling(self) -> None:
        # Acknowledgment must read differently from the phases around it: an
        # operator glancing at the panel decides whether to resubmit from this.
        server = self._start_server()
        _, body, _ = _http_request_raw(self._base_url(server) + "/static/styles.css")
        decoded = body.decode("utf-8")
        self.assertIn('.wb-redirect__phase[data-phase="acknowledged"]', decoded)
        self.assertIn('.wb-redirect__operation-row[data-phase="acknowledged"]', decoded)
        self.assertIn('.wb-redirect__operation-row[data-phase="unresolved"]', decoded)
        self.assertIn(".wb-redirect__availability", decoded)
        # The disabled submit is the availability gate, so it has to look
        # disabled; the shared .wb-button rules alone leave it looking live.
        self.assertIn(".wb-redirect__actions .wb-button:disabled", decoded)
        # Long ids and stage prose must wrap rather than widen the panel.
        self.assertIn("word-break: break-word;", decoded)
        self.assertIn("@media (max-width: 720px)", decoded)


if __name__ == "__main__":
    unittest.main()
