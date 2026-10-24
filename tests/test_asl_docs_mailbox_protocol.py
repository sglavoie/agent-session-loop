"""Mailbox protocol docs regression tests."""

from __future__ import annotations

import unittest

from agent_session_loop import mailbox_constants, run_coordination_constants
from tests.asl_docs_support import ASL_GUIDE, ASL_SKILL_HELPER_INTERNALS, read_repo_text

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class MailboxProtocolDocsTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return read_repo_text(relative)

    def test_command_synopsis_exposes_final_mailbox_surface(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        commands = docs.split("## Commands", 1)[1].split("\n## ", 1)[0]

        self.assertIn("[--no-mailbox]", commands)
        for command in (
            "asl inbox guidance",
            "asl inbox reply",
            "asl inbox resolve",
            "asl inbox read",
            "asl ask",
            "asl notify",
            "asl outbox wait",
        ):
            self.assertIn(command, commands)
        self.assertIn("asl control set-model", commands)
        self.assertIn("--clear-harness-model", commands)
        self.assertIn("--clear-effort", commands)

    def test_docs_expose_the_supervisor_guide_and_leaf_help_routing(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        commands = docs.split("## Commands", 1)[1].split("\n## ", 1)[0]
        autopilot = docs.split("## Autopilot Skill", 1)[1].split("\n## ", 1)[0]
        guide = self._read(ASL_GUIDE)
        supervision = guide.split("### Live ASL supervision", 1)[1].split("\n### ", 1)[
            0
        ]

        self.assertIn(
            "asl supervisor-guide [--harness auto|codex|claude|copilot] [--json]",
            commands,
        )
        for marker in (
            "`selected_harness`",
            "`leaf_help`",
            "carry equivalent commands",
            "--supervision-id ... --json --detach",
            "`result_path`",
            "lazy\n`handler_ref` mechanism",
            "`asl outbox supervise --help`",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, autopilot)
        for group in (
            "`bridge`",
            "`workbench`",
            "`control`",
            "`inbox`,\n`outbox`",
            "`assurance`",
        ):
            with self.subTest(group=group):
                self.assertIn(group, autopilot)

        # The workbench parent shows the serve flags, so neither document may
        # apply the leaf-only sentence to it.
        for marker in (
            "a leaf-only group's",
            "`asl workbench` is the exception",
            "bare `asl workbench` form runs `asl workbench serve`",
        ):
            with self.subTest(workbench_marker=marker):
                self.assertIn(marker, autopilot)

        for marker in (
            "asl supervisor-guide [--harness auto|codex|claude|copilot] [--json]",
            "`asl outbox supervise --help`",
            "every supported candidate when detection is absent or ambiguous",
            "leaf-only group's help lists its leaves and none of their flags",
            "`asl workbench` instead carries the serve flags on the parent",
        ):
            with self.subTest(readme_marker=marker):
                self.assertIn(marker, supervision)
        self.assertNotIn(
            "`asl workbench`, `asl control`",
            supervision,
        )

    def test_docs_describe_activation_precedence_waits_and_fallback(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[
            1
        ].split("\n## ", 1)[0]

        for marker in (
            "`mailbox.jsonl`",
            "`mailbox-lease.json`",
            "fixed-size 512-byte supervisor",
            "fresh for 90 seconds",
            "mode `0600`",
            "existing `mailbox.jsonl` or a fresh supervisor lease",
            "`asl run --no-mailbox` always wins",
            "cannot override the",
            "runner-owned mutation rules",
            "90 seconds by default",
            "polls mailbox\n  metadata every two seconds",
            "default timeout is 300 seconds",
            "polls mailbox metadata every five seconds",
            "`acknowledged: false` plus an `acknowledgement_error`",
            "waits five minutes initially",
            "Lease freshness is checked only at an\nactive deadline",
            "extends the wait by 60 seconds",
            "30-minute ceiling bounds *unsupervised* time, not wall clock",
            "ordinary\nterminal pause",
            "next\nimplementation invocation",
            "never affect generated review turns",
        ):
            self.assertIn(
                marker,
                mailbox_docs,
                msg=f"missing mailbox contract marker: {marker!r}",
            )

    def test_docs_describe_exhausted_mailbox_reply_precedence(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("### Live reply wait and fallback", 1)[1].split(
            "\n### ", 1
        )[0]
        normalized = " ".join(mailbox_docs.split())

        self.assertIn(
            "An already-committed closure takes precedence over mailbox "
            "exhaustion and is consumed before exhaustion can end the wait.",
            normalized,
        )
        self.assertIn(
            "If no closure is available, timeout, a stale lease at an active "
            "deadline that outlasts any post-suspend grace, a full/unavailable "
            "mailbox",
            normalized,
        )
        self.assertIn("such as `mailbox-full`", normalized)
        self.assertIn(
            "an absent, stale, or otherwise non-fresh supervisor lease with "
            "no post-suspend lease grace left to cover it",
            normalized,
        )
        self.assertIn(
            "grants the supervisor a 90-second awake grace to re-prove liveness",
            normalized,
        )
        self.assertIn(
            "a non-fresh lease neither ends the wait nor extends the active "
            "deadline, and the grace never extends the ceiling",
            normalized,
        )
        self.assertIn(
            "a continuously watched wait has no terminal instant and outlives "
            "the ceiling indefinitely",
            normalized,
        )
        self.assertIn(
            "`timeout` only when the unsupervised ceiling was reached",
            normalized,
        )
        self.assertIn(
            "`suspend_lease_grace_seconds`, the awake lease grace that suspend applied",
            normalized,
        )
        self.assertIn("`suspend_skew_seconds`", normalized)
        self.assertIn(
            "`unsupervised_seconds`, how long it ran without a supervisor "
            "lease proving liveness",
            normalized,
        )

    def test_docs_describe_shared_closure_reserve_and_availability(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[1]
        normalized = " ".join(mailbox_docs.split())

        # Pin the documented reserve and minimum-closure sizes to their constants.
        self.assertEqual(512 * 1024, mailbox_constants.MAILBOX_CLOSURE_RESERVE_BYTES)
        self.assertEqual(
            mailbox_constants.MAILBOX_CLOSURE_RESERVE_BYTES,
            mailbox_constants.MAILBOX_REPLY_RESERVE_BYTES,
        )
        self.assertEqual(
            mailbox_constants.MAILBOX_CLOSURE_RESERVE_BYTES,
            mailbox_constants.MAILBOX_QUESTION_RESERVE_BYTES,
        )
        self.assertEqual(176, mailbox_constants.MAILBOX_MIN_REPLY_BYTES)
        self.assertEqual(200, mailbox_constants.MAILBOX_MIN_RESOLUTION_BYTES)
        self.assertEqual(192 * 1024, mailbox_constants.MAILBOX_WAIT_END_RESERVE_BYTES)
        self.assertEqual(96 * 1024, mailbox_constants.MAILBOX_RECEIPT_RESERVE_BYTES)
        self.assertIn(
            "The final 512 KiB below the cap is a lifecycle reserve",
            normalized,
        )
        self.assertIn(
            "closures retain 288 KiB of nested headroom for wait termination "
            "and receipts",
            normalized,
        )
        self.assertIn(
            "ordinary exhaustion cannot strand an active question, its "
            "`wait-end`, or its consumption evidence",
            normalized,
        )
        self.assertIn(
            "`accepts_replies` and `accepts_resolutions` separately report "
            "whether capacity remains for each minimal valid closure",
            normalized,
        )
        self.assertIn(
            "`full` itself keeps its file-faithful physical-cap meaning",
            normalized,
        )

        status_docs = docs.split("## JSON Output", 1)[1].split(
            "\n## Pause Semantics", 1
        )[0]
        status_normalized = " ".join(status_docs.split())
        self.assertIn(
            "Beside `full` it reports `accepts_questions`, `accepts_replies`, "
            "and `accepts_resolutions`",
            status_normalized,
        )
        self.assertIn(
            "the latter two independently report capacity for their "
            "differently sized minimal valid closures",
            status_normalized,
        )
        self.assertIn(
            "all three fields and the resolution/notice totals are `null` "
            "alongside `full` rather than defaulting to writable or empty",
            status_normalized,
        )
        self.assertIn(
            "`replies and resolutions still accepted`, `only replies still "
            "accepted`, or `reply and resolution capacity exhausted`",
            status_normalized,
        )
        self.assertIn(
            "never overclaims that a closure can land",
            status_normalized,
        )

    def test_docs_describe_bounded_question_body(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[1]
        normalized = " ".join(mailbox_docs.split())

        # Pin the documented character bound to the constant it describes.
        self.assertEqual(
            16000, run_coordination_constants.MAILBOX_QUESTION_TEXT_MAX_CHARS
        )
        self.assertIn(
            "The question body a waiting runner publishes is itself bounded: "
            "at most the first 16,000 characters of the last-message artifact",
            normalized,
        )
        self.assertIn(
            "truncated deterministically and given a footer naming the "
            "on-disk artifact path that holds the full text",
            normalized,
        )

    def test_docs_describe_resolution_marker_choices_and_notice_replay(
        self,
    ) -> None:
        docs = self._read("docs/agent-session-loop.md")
        normalized = " ".join(docs.split())

        for marker in (
            "`AgentSessionLoopMailboxQuestion` Agent Mux handoff",
            "`<run-id>:mailbox-question:<question-sequence>` source event ID",
            "Closed/history-only records never emit the marker",
            "`AgentSessionLoopMailboxResolution` with `run_id`, `state_path`, "
            "`question_id`",
            "`<run-id>:mailbox-resolution:<resolution-sequence>` source event ID",
            "The marker contains no question, reply, resolution-note, or notice body",
            "offers three explicit outcomes: submit a factual answer with "
            "`asl inbox reply`",
            "`continue_command`, and `pause_command`",
            "notices from the exact live run are emitted as bounded escaped "
            "`NOTICE` progress and replayed at least once on attach within "
            "each supervisor process",
            "final structured outbox declares `include: all` and preserves "
            "complete separate `questions` and `notices` arrays",
        ):
            self.assertIn(marker, normalized)

        self.assertIn(
            "A question is closed logically by its first accepted reply or resolution",
            normalized,
        )
        self.assertIn(
            "ASL does not rotate, compact, or physically delete mailbox records",
            normalized,
        )

    def test_docs_and_skill_describe_lease_heartbeat_vs_takeover(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        # The asl skill's lease heartbeat rules live in its helper reference.
        skill = self._read(ASL_SKILL_HELPER_INTERNALS)
        normalized_docs = " ".join(docs.split())
        normalized_skill = " ".join(skill.split())

        self.assertNotIn("establish or renew a lease", normalized_docs)
        for marker in (
            "decide between a heartbeat and a takeover",
            "re-emits the same `supervisor_id` and question projection "
            "with only `updated_at` refreshed",
            "takes over with a new random supervisor identity and no projection",
        ):
            self.assertIn(
                marker,
                normalized_docs,
                msg=f"docs omit lease heartbeat marker: {marker!r}",
            )
        for marker in (
            "One-shot supervisor commands do not displace a live helper.",
            "heartbeat it — the helper's supervisor identity and "
            "projection survive with only the lease timestamp refreshed",
            "Only a missing, stale, or unprojected lease is taken over "
            "with a fresh supervisor identity",
        ):
            self.assertIn(
                marker,
                normalized_skill,
                msg=f"skill omits lease heartbeat marker: {marker!r}",
            )


if __name__ == "__main__":
    unittest.main()
