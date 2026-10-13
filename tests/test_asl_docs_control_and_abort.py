"""Run control, abort, and workbench redirect docs regression tests."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    control_cli,
    control_queue,
    control_signal,
    events,
    run_coordination_control,
    turn_execution_constants,
)
from tests.asl_docs_support import read_repo_text

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class ControlAndAbortDocsTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return read_repo_text(relative)

    def _control_docs(self) -> str:
        docs = self._read("docs/agent-session-loop.md")
        return docs.split("`asl control` is the programmatic equivalent", 1)[1].split(
            "\n`status` reads durable plan files", 1
        )[0]

    def test_docs_pin_control_abort_and_receipt_constants(self) -> None:
        normalized = " ".join(self._control_docs().split())

        # Pin the documented windows to the constants that produce them.
        self.assertEqual(120, control_queue.DEFAULT_ABORT_GRACE_SECONDS)
        self.assertEqual(1, control_queue.MIN_ABORT_GRACE_SECONDS)
        self.assertEqual(86_400, control_queue.MAX_ABORT_GRACE_SECONDS)
        self.assertEqual(0.5, control_cli.CONTROL_WAIT_POLL_INTERVAL_SECONDS)
        self.assertEqual(60.0, control_cli.DEFAULT_CONTROL_WAIT_TIMEOUT_SECONDS)
        self.assertEqual(1800.0, control_cli.MAX_CONTROL_WAIT_TIMEOUT_SECONDS)
        self.assertEqual(
            5.0, turn_execution_constants.LIVE_CONTROL_POLL_INTERVAL_SECONDS
        )

        self.assertIn(
            "end the in-flight turn after a bounded grace window (default "
            "`120` seconds)",
            normalized,
        )
        self.assertIn(
            "A grace outside 1–86400 seconds fails with `reason: invalid-grace`",
            normalized,
        )
        self.assertIn(
            "the handler tails `events.jsonl` every `0.5` seconds",
            normalized,
        )
        self.assertIn(
            "`--timeout` requires `--wait` and must be between 0 and 1800 seconds",
            normalized,
        )
        self.assertIn("The default wait is `60` seconds", normalized)
        self.assertIn(
            "instead of waiting out its `5.0`-second control poll",
            normalized,
        )

    def test_docs_describe_the_control_wakeup_and_its_host_limit(self) -> None:
        normalized = " ".join(self._control_docs().split())

        self.assertIn(
            "The wakeup is a single byte written to the FIFO recorded as "
            "`wakeup_endpoint` in the plan's `lock.json`",
            normalized,
        )
        # The runner opens the read end before publishing the lock that names
        # it, which is what makes an authenticated owner unnecessary.
        self.assertIn(
            "opens it for reading **before** it publishes the `lock.json` "
            "that names it",
            normalized,
        )
        for reason in sorted(
            value
            for name, value in vars(control_signal).items()
            if name.startswith("REASON_") and isinstance(value, str)
        ):
            self.assertIn(
                f"`{reason}`",
                normalized,
                msg=f"wakeup reason {reason!r} is undocumented",
            )
        self.assertIn(
            "never a delivery guarantee: a command whose wakeup did not land "
            "is still applied at the next poll",
            normalized,
        )
        self.assertIn("the wakeup is **same-host only**", normalized)
        self.assertIn(
            "a runner whose lock records another hostname reports "
            "`remote-host` and picks the command up on its ordinary poll "
            "cadence instead",
            normalized,
        )

    def test_docs_enumerate_run_control_actions(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        state_files = docs.split("## State Files", 1)[1].split("\n## ", 1)[0]
        event_reference_docs = state_files.split("`run_control` events", 1)[1].split(
            "`tail` renders", 1
        )[0]

        for action in events.RUN_CONTROL_ACTIONS:
            self.assertIn(
                f"`{action}`",
                event_reference_docs,
                msg=f"events.jsonl reference omits run-control action {action!r}",
            )

        # `control-command-observed` is a receipt, not a state transition, so it
        # is the one action that never reaches `control.last_action`.
        last_action_values = tuple(
            action
            for action in events.RUN_CONTROL_ACTIONS
            if action != events.RUN_CONTROL_ACTION_COMMAND_OBSERVED
        )
        self.assertLessEqual(
            set(run_coordination_control.RUN_CONTROL_EVENT_ACTIONS.values()),
            set(last_action_values),
        )
        self.assertNotIn(
            events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
            set(run_coordination_control.RUN_CONTROL_EVENT_ACTIONS.values()),
        )
        last_action_docs = state_files.split("`last_action` is one of", 1)[1]
        for action in last_action_values:
            self.assertIn(
                f"`{action}`",
                last_action_docs,
                msg=f"state.json `last_action` omits run-control action {action!r}",
            )
        normalized_state_files = " ".join(state_files.split())
        self.assertIn(
            "`control-command-observed` is a receipt for a drained command "
            "rather than a state transition, so it is recorded in "
            "`events.jsonl` and never becomes `last_action`",
            normalized_state_files,
        )

    def test_asl_skill_registers_abort_beside_stop_after_current(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        self.assertIn(
            "asl control stop-after-current <plan-dir> [--state-dir DIR] "
            "[--wait] [--timeout SECONDS] --json",
            normalized,
        )
        self.assertIn(
            "asl control abort <plan-dir> [--grace SECONDS] [--reason TEXT] "
            "[--state-dir DIR] [--wait] [--timeout SECONDS] --json",
            normalized,
        )
        for marker in (
            "Guidance is advice, never enforcement.",
            "does **not** stop the session",
            "Only `asl control` stops a run",
            "`abort` is the enforcing mechanism when the session running "
            "*right now* must not finish",
            "Never report a run as stopped because guidance asking it to stop "
            "was sent.",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"skill omits abort-enforcement marker: {marker!r}",
            )

    def test_asl_skill_requires_wait_and_reports_its_timeout(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        for marker in (
            "Use `--wait` whenever the stop actually matters.",
            "it exits 0 whether or not any runner ever saw it, which is "
            "exactly how a stop can look sent and still be ignored",
            "A `--wait` that times out is a **failure to report, never an "
            "assumed success**",
            "do not claim the run stopped",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"skill omits control-wait marker: {marker!r}",
            )
        for reason in ("no-runner", "receipt-timeout", "abort-timeout"):
            self.assertIn(
                f"`{reason}` —",
                normalized,
                msg=f"skill omits wait failure reason {reason!r}",
            )

    def test_asl_skill_reports_the_abort_early_stop(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        self.assertNotIn(
            "honored an out-of-band `asl control stop-after-current` request",
            normalized,
        )
        for marker in (
            "`stop_kind` names which request produced that stop.",
            "`abort` means `asl control abort` ended the turn that was in flight",
            "reverted to `pending` with a Handoff note saying it holds no "
            "completion, so it is *not* counted in `completed`",
            "It is still a clean early stop: same `ok: true`, still never a "
            "pause or a failure, still never auto-resumed.",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"skill omits abort early-stop marker: {marker!r}",
            )

    def test_docs_and_skill_pin_every_abort_outcome(self) -> None:
        # The runner leaves a turn that ends inside the grace window to the
        # ordinary pause/completion path, so only the killed outcome stops the
        # run; docs and skill have to say all three or a supervisor will report
        # a stop that never happened.
        control_docs = " ".join(self._control_docs().split())
        skill = " ".join(self._read("skills/asl/SKILL.md").split())

        for marker in (
            "**Killed at the deadline.** The turn was still running when the "
            "window expired",
            "`abort-turn-honored` is recorded, and the run exits with "
            "`reason: stopped`",
            "**Self-paused inside the window.**",
            "the run exits `paused` with the worker's pause reason and "
            "`ok: false`, never `reason: stopped`",
            "**Completed inside the window.**",
            "the completion stands, and the run continues to the next session",
            "An abort observed between turns has nothing to kill and degrades "
            "to stop-after-current instead.",
            "Only `abort-turn-honored` means the run is stopping",
        ):
            self.assertIn(
                marker,
                control_docs,
                msg=f"control docs omit abort-outcome marker: {marker!r}",
            )

        for marker in (
            "**An abort has three outcomes and only one of them stops the "
            "run**, so read the run's own terminal state before reporting any "
            "of them",
            "**The turn was killed**",
            "the run exits `reason: stopped` with `stop_kind: abort`",
            "**The worker wound down and paused itself**",
            "the run comes back `paused` with the worker's pause reason and "
            "`ok: false`, *not* `reason: stopped`",
            "**The worker completed its session**",
            "the session stays `complete` and **the run continues to the next "
            "session**",
            "never report the run as stopped on the strength of the abort alone",
            "Only `abort-turn-honored` proves the run is stopping",
            "An abort the worker satisfied inside its grace window never "
            "reaches this verdict",
        ):
            self.assertIn(
                marker,
                skill,
                msg=f"skill omits abort-outcome marker: {marker!r}",
            )

    def test_docs_describe_the_autopilot_stop_kind(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        autopilot_docs = docs.split("## Autopilot Skill", 1)[1].split("\n## ", 1)[0]
        normalized = " ".join(autopilot_docs.split())

        for marker in (
            "`stop_kind` names which request it was: `stop-after-current` for "
            "the `p` key or `asl control stop-after-current`, honored at the "
            "session boundary",
            "`abort` for `asl control abort`, which ended the turn in flight "
            "and returned its session to `pending`, so that session is *not* "
            "in `completed`",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"autopilot docs omit stop_kind marker: {marker!r}",
            )

    def test_docs_describe_the_workbench_abort_endpoint(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        normalized = " ".join(docs.split())

        self.assertIn(
            "`POST /api/control/abort` — enqueue a `request-abort-turn` command.",
            normalized,
        )
        self.assertIn(
            "so an out-of-range window returns `invalid-grace` and an empty "
            "reason returns `invalid-reason`",
            normalized,
        )
        self.assertIn(
            "it has no receipt equivalent of `asl control abort --wait`: the "
            "HTTP surface confirms the append, never the runner's observation",
            normalized,
        )

    def test_docs_describe_the_workbench_redirect_endpoint(self) -> None:
        """The HTTP correction path, including what its status codes promise."""
        docs = self._read("docs/agent-session-loop.md")
        normalized = " ".join(docs.split())

        for marker in (
            "`POST /api/control/redirect` — submit one urgent correction "
            "against the turn the run is on now",
            "`202` with `accepted` and a `redirect` receipt when anything was "
            "persisted",
            "`409` when nothing was persisted",
            "The response carries `feedback_digest` and `feedback_bytes`, "
            "never the correction text.",
            "A redirect is deliberately not a checkpoint command",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"docs omit workbench redirect marker: {marker!r}",
            )

    def test_docs_describe_the_workbench_redirect_reads(self) -> None:
        """The two reads, and that the recovery one performs no recovery."""
        docs = self._read("docs/agent-session-loop.md")
        normalized = " ".join(docs.split())

        for marker in (
            "`GET /api/redirect-status?plan_dir=<path>` — durable redirect "
            "operations for one plan",
            "`GET /api/conversation-recovery?plan_dir=<path>` — which "
            "recovery an unresumable conversation is waiting on",
            "The endpoint performs no recovery: doing so would persist a "
            "one-use operator authorization in a request a browser can "
            "replay",
            "`/api/redirect-status`, `/api/conversation-recovery`) are "
            "**not** authenticated",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"docs omit workbench redirect-read marker: {marker!r}",
            )

    def test_docs_describe_the_workbench_launch_idempotency_key(self) -> None:
        """``launch_id``, and the durable journal the receipt now comes from."""
        docs = self._read("docs/agent-session-loop.md")
        normalized = " ".join(docs.split())

        for marker in (
            "accept an optional `launch_id` bounded by "
            "`redirect_store.REDIRECT_MAX_OPERATION_ID_CHARS`",
            "both routes admit through the plan's operation journal, so a "
            "resend is idempotent across Workbench processes and across a "
            "restart, not only within the process that spawned",
            '`reason: "launch-already-in-flight"` while a process this '
            "server started for the plan is still alive",
            "A paused run is not a conflict: resuming one is what `/api/reply` is for.",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"docs omit launch idempotency marker: {marker!r}",
            )


if __name__ == "__main__":
    unittest.main()
