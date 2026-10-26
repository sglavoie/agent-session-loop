"""Live-run contract, recovery, and unsupervised-ceiling docs regression tests."""

from __future__ import annotations

import re
import unittest

from agent_session_loop import (
    bridge_protocol,
    events,
    mailbox,
    qna,
    run_coordination_constants,
    state_payloads,
)
from tests.asl_docs_support import ASL_GUIDE, read_asl_skill_bundle, read_repo_text

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class LiveContractDocsTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return read_repo_text(relative)

    def test_docs_describe_durable_operation_admission(self) -> None:
        """The two answers an interruption can leave, and who stays authority."""
        docs = self._read("docs/agent-session-loop.md")
        normalized = " ".join(docs.split())

        for marker in (
            "keeps a per-plan journal at `<state-dir>/<plan-hash>/operations.jsonl`",
            "holds an exclusive `flock` across read, decide, and append",
            "Dispatch is recorded before the side effect, never after.",
            "a killed process leaves `expired` when the mutation certainly "
            "did not run, and `unconfirmed` when it may have and nobody can "
            "say",
            "never infers success from absence",
            "An `unconfirmed` operation is never retried automatically.",
            "settles the old row `superseded` with detail `replaced-uncertain`",
            "Admission is a gate in front of the plan lock, not a replacement for it.",
            "The journal holds identifiers only.",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"docs omit operation-admission marker: {marker!r}",
            )

    def test_docs_describe_the_shared_launch_and_reply_path(self) -> None:
        """One application operation behind both surfaces, and its settlement."""
        docs = self._read("docs/agent-session-loop.md")
        normalized = " ".join(docs.split())

        for marker in (
            "`agent_session_loop.workbench_actions.perform` is the single "
            "application operation behind `POST /api/launch`, `POST /api/reply`, "
            "and the bridge's `run.resume` and `question.reply`",
            "A stale target is refused with or without an action token.",
            "a target that has moved is refused before admission, so the "
            "operation id the request carried is still spendable",
            "What settles an operation depends on what it promised.",
            "promised that an operator's words were *read*, which a `Popen` "
            "does not establish",
            "reach `consumed` only on a `reply_provided` record in the plan's "
            "own `events.jsonl`",
            "the evidence *reports* which run read the reply rather than "
            "matching it against the id the operation named",
            "A guard refusal closes the row `cancelled` on the way past",
            "A launched run gets its own session.",
            "`workbench_launch.launch_plan` passes `start_new_session=True`, matching "
            "the `AbandonProcessGroup` the launchd agents already set",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"docs omit shared launch/reply marker: {marker!r}",
            )

    def test_docs_refuse_the_pause_lifting_as_resume_evidence(self) -> None:
        """The interpretation session 6 replaced, named so it cannot come back."""
        docs = self._read("docs/agent-session-loop.md")
        normalized = " ".join(docs.split())

        self.assertIn(
            "A resume is settled by a run's own record of reading the reply, "
            "never by the plan leaving the paused state.",
            normalized,
        )
        self.assertIn(
            "a `reply_provided` record naming the reply a resume carried",
            normalized,
        )
        self.assertNotIn("a resume's run leaving the paused state", normalized)

    def test_docs_describe_the_conversation_recovery_card(self) -> None:
        """The new recovery kind, and why it offers no endpoint action."""
        docs = self._read("docs/agent-session-loop.md")
        normalized = " ".join(docs.split())

        self.assertIn(
            "`commit-preflight`, `commit-failed`, `conversation-recovery`, "
            "and `generic`",
            normalized,
        )
        for marker in (
            "`conversation-recovery` covers both pauses that hand a decision "
            "to a person rather than to a retry",
            "It is a read-only card by construction",
            "The two reasons previously fell back to `generic`, whose advice "
            "— inspect, then relaunch — is actively wrong here",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"docs omit conversation-recovery marker: {marker!r}",
            )

    def test_docs_enumerate_waiting_for_reply_statuses(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        status_docs = docs.split("## JSON Output", 1)[1].split(
            "\n## Pause Semantics", 1
        )[0]
        pause_docs = docs.split("## Pause Semantics", 1)[1].split(
            "\n## Autopilot Skill", 1
        )[0]

        self.assertIn("`waiting_for_reply_status`", status_docs)
        for waiting_status in state_payloads.WAITING_FOR_REPLY_STATUSES:
            self.assertIn(
                f"`{waiting_status}`",
                status_docs,
                msg=(
                    f"waiting_for_reply_status value {waiting_status!r} is undocumented"
                ),
            )
        self.assertIn(f"`{qna.EXPIRED_WAIT_PAUSE_REASON}`", pause_docs)
        self.assertRegex(
            pause_docs,
            r"`wait-expired` pause reason without rewriting\s+`state\.json`",
        )
        self.assertRegex(
            pause_docs,
            r"This recovery reason is\s+never written into `state\.json`",
        )

    def test_docs_describe_the_rolling_unsupervised_ceiling(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        state_files = docs.split("## State Files", 1)[1].split("\n## ", 1)[0]
        status_docs = docs.split("## JSON Output", 1)[1].split(
            "\n## Pause Semantics", 1
        )[0]
        pause_docs = docs.split("## Pause Semantics", 1)[1].split(
            "\n## Autopilot Skill", 1
        )[0]

        for section_name, section, markers in (
            (
                "state files",
                state_files,
                (
                    "`unsupervised_deadline_at` with its "
                    "`unsupervised_ceiling_seconds` span",
                    "`unsupervised_deadline_at` is the ceiling as of the last "
                    "anchor the runner persisted",
                    "a reader rolls it forward from `last_lease_activity_at` "
                    "plus the ceiling span rather than reading it as a "
                    "terminal instant",
                    "Waits persisted before the ceiling began rolling carry a "
                    "static `hard_deadline` instead of those two keys, and "
                    "every reader still renders that key for them",
                ),
            ),
            (
                "wait validity",
                status_docs,
                (
                    "the classification consults lease freshness rather than "
                    "a bare timestamp",
                    "the projected ceiling is the later of "
                    "`unsupervised_deadline_at` and `last_lease_activity_at` "
                    "plus `unsupervised_ceiling_seconds`",
                    "a wait whose supervisor keeps renewing stays `live` past "
                    "the instant persisted at its start",
                    "A record written before the ceiling began rolling "
                    "carries only `hard_deadline` and is classified from that "
                    "timestamp exactly as it was before",
                ),
            ),
            (
                "pause semantics",
                pause_docs,
                (
                    "classify it as `expired` even before its unsupervised ceiling",
                    "That lock check is what makes an abandoned wait "
                    "recoverable promptly",
                    "the ceiling itself rolls forward with lease activity, so "
                    "a record left behind by a supervised wait can carry a "
                    "projected ceiling well in the future",
                    "orphaned nonterminal wait whose unsupervised ceiling passed",
                ),
            ),
        ):
            normalized = " ".join(section.split())
            for marker in markers:
                self.assertIn(
                    marker,
                    normalized,
                    msg=(
                        f"{section_name} docs omit rolling-ceiling marker: {marker!r}"
                    ),
                )

    def test_docs_enumerate_mailbox_lease_reasons(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        status_docs = docs.split("## JSON Output", 1)[1].split(
            "\n## Pause Semantics", 1
        )[0]
        expected_reasons = {
            value
            for name, value in vars(mailbox).items()
            if name.startswith("LEASE_") and isinstance(value, str)
        }

        for key in (
            "lease_reason",
            "lease_error",
            "lease_present",
            "lease_fresh",
        ):
            self.assertIn(
                f"`{key}`",
                status_docs,
                msg=f"status mailbox projection key {key!r} is undocumented",
            )
        for reason in sorted(expected_reasons):
            self.assertIn(
                f"`{reason}`",
                status_docs,
                msg=f"mailbox lease reason {reason!r} is undocumented",
            )

    def test_docs_lock_net_surface_privacy_and_exclusions(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[
            1
        ].split("\n## ", 1)[0]
        surface_docs = mailbox_docs.split(
            "### Surface and observability accounting", 1
        )[1].split("\n\nWorkbench", 1)[0]
        event_reference_docs = docs.split("`events.jsonl` records", 1)[1].split(
            "Verification heartbeats", 1
        )[0]

        for marker in (
            "three mailbox state files",
            "eight mailbox commands",
            "one\nmodel-control command",
            "one `mailbox` event name",
            "adds no runner or\nsupervisor threads",
            "no terminal statuses",
            "`guidance-observed` carries",
            "`notice-published` carries",
            "`reply_provided` carries",
            "do not increment pause metrics",
            "no mailbox-authoring, mailbox-reply,\nmailbox-resolution, "
            "mailbox-notice, or live model-control endpoint",
        ):
            self.assertIn(
                marker,
                mailbox_docs,
                msg=f"missing surface/privacy marker: {marker!r}",
            )
        for section_name, section in (
            ("surface accounting", surface_docs),
            ("events.jsonl reference", event_reference_docs),
        ):
            for action in events.MAILBOX_ACTIONS:
                self.assertIn(
                    f"`{action}`",
                    section,
                    msg=f"{section_name} omits mailbox action {action!r}",
                )
        normalized_surface_docs = " ".join(surface_docs.split())
        self.assertIn(
            "`wait-cleared` carries the wait identity and deadlines, "
            "the exit `reason`, `pending_outbox`, the measured count "
            "of questions open for that run when a final mailbox snapshot "
            "is available, and the `lease_supervisor_id`, `lease_updated_at`, "
            "and `lease_reason` evidence for the last lease a non-closure exit "
            "observed, plus `suspend_skew_seconds` and "
            "`suspend_lease_grace_seconds` when the wait detected a system "
            "suspend",
            normalized_surface_docs,
        )
        live_wait_docs = mailbox_docs.split("### Live reply wait and fallback", 1)[
            1
        ].split("\n### ", 1)[0]
        for reason in run_coordination_constants.MAILBOX_REPLY_WAIT_PREPARATION_REASONS:
            self.assertIn(
                f"`{reason}`",
                live_wait_docs,
                msg=f"live-wait docs omit preparation reason {reason!r}",
            )
        for stale_draft in (
            "set-max-sessions",
            "--mailbox-interactive",
            "append-only keepalive",
            "transient real pause",
            "model changes affect the active turn",
        ):
            self.assertNotIn(stale_draft, docs)

    def test_guide_and_autopilot_skill_match_live_contract(self) -> None:
        guide = self._read(ASL_GUIDE)
        # The live-mailbox contract spans the skill and its on-demand
        # references (question closure, helper watcher and lease, controls).
        skill = read_asl_skill_bundle()

        for marker in (
            "### Live ASL supervision",
            "`asl run --no-mailbox`",
            "The eight mailbox commands",
            "three private (`0600`) state files, eight commands",
            "Lease freshness is checked only at an active\ndeadline",
            "30-minute ceiling bounds unsupervised time rather\nthan wall clock",
            "`asl control set-model",
            "no runner/supervisor thread",
            "no Workbench",
        ):
            self.assertIn(marker, guide)
        for marker in (
            "`--no-mailbox` is accepted",
            "polls questions and notices every five seconds",
            "renews the lease every 30 seconds",
            "`QUESTION id=... preview=...`",
            "asl inbox reply <plan-dir> --to <question-id>",
            "resumes the same session",
            "Lease freshness is checked only at an active deadline",
            "awake grace to re-prove liveness before a stale lease ends the",
            "30-minute ceiling bounds unsupervised time,\nnot wall clock",
            "already-committed reply or resolution takes\nprecedence over "
            "mailbox exhaustion",
            "affect only the next implementation invocation",
        ):
            self.assertIn(marker, skill)


class SupervisionBridgeDocsTests(unittest.TestCase):
    def test_every_frame_example_speaks_the_served_protocol(self) -> None:
        """The request, snapshot, and error-table examples still said 1."""
        docs = read_repo_text("docs/agent-session-loop/25-supervision-bridge.md")
        normalized = " ".join(docs.split())
        version = bridge_protocol.PROTOCOL_VERSION

        stated = re.findall(r'"protocol":\s*(\d+)', docs)
        self.assertTrue(stated)
        self.assertEqual({str(version)}, set(stated))
        self.assertIn(f"`protocol` is not {version}.", normalized)

    def test_the_bridge_doc_states_the_round_four_guarantees(self) -> None:
        normalized = " ".join(
            read_repo_text("docs/agent-session-loop/25-supervision-bridge.md").split()
        )

        for marker in (
            # workbench.ensure hands out an owner token, so it is gated.
            "refused with the same `contract-set-mismatch` an unhandshaken "
            "mutation gets until a handshake has matched the contract set",
            # A reused operation id is a conflict, not a replay.
            '`details.reason: "redirect-replay-conflict"`, the same refusal '
            "`run.redirect` gives",
            # The echoed id is bounded.
            f"`id` is at most {bridge_protocol.MAX_REQUEST_ID_CHARS} characters",
            # A closed stdout exits 0.
            "points stdout at the null device before returning",
        ):
            self.assertIn(marker, normalized)


if __name__ == "__main__":
    unittest.main()
