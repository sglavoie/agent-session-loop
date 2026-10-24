"""Supervision wait-loop and terminal-record docs regression tests."""

from __future__ import annotations

import unittest

from agent_session_loop import mailbox_cli_constants
from tests.asl_docs_support import (
    ASL_SKILL_QUOTA_AND_HANGS,
    read_asl_skill_bundle,
    read_repo_text,
)

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class SupervisionDocsTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return read_repo_text(relative)

    def test_asl_skill_ends_wait_loop_on_terminal_supervision_record(
        self,
    ) -> None:
        # Token details live in `references/supervise-tokens.md`; the skill as a
        # whole (SKILL.md plus its references) must still carry them.
        skill = read_asl_skill_bundle()
        normalized = " ".join(skill.split())

        for marker in (
            "mint a **launch identity** — one fresh UUID",
            "--supervision-id <launch-identity>",
            "two helper lifetimes can overlap and a mailbox sequence captured "
            "at launch cannot tell a still-alive older helper's later exit "
            "from this one's",
            "Pass the same `--supervision-id` on every call",
            "keys the persisted cursor, so the command is reissued verbatim "
            "and no `--since` is ever threaded by hand",
            "The cursor starts at 0, so questions that were already open "
            "before the launch stay visible",
            "Exactly one of four tokens is printed, always with exit code 0",
            "`IDLE` means nothing actionable happened: print nothing and "
            "reissue the identical command",
            "`ACTION questions=<n>` carries `<n>` question records that no "
            "closure has answered yet",
            "`DONE outcome=<outcome> seq=<n>` carries this launch's own "
            "terminal record: stop the loop and do not reissue. Read that "
            "`outcome` before trusting stdout",
            "Every outcome except `error` means the helper wrote and flushed "
            "its complete `--json` result before appending the record",
            "`outcome: error` means the helper died abnormally",
            "its stdout may be missing, truncated mid-write, or complete but unproven",
            "Never require parseable JSON there: take the termination detail "
            "from the record's `text`, recover run state with "
            "`asl status <plan-dir> --json`",
            "Another helper's terminal record — an earlier invocation, or one "
            "whose lifetime overlapped this launch — never ends this loop",
            "It is absorbed internally: the cursor advances past it and the "
            "wait continues on the remaining budget",
            "So is a batch whose questions have all already closed. Neither "
            "ever reaches stdout",
            "Absence of `DONE` is **not** proof that the helper is alive",
            "Keep both fallbacks — the bounded stdout tail below, and the "
            "loop itself as the backstop",
            "when no terminal record can ever arrive (`--no-mailbox`, "
            "SIGKILL, machine crash)",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"asl skill omits supervision-end marker: {marker!r}",
            )

    def test_asl_skill_launches_the_helper_detached(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        for marker in (
            "--supervision-id <launch-identity> --json --detach",
            "`--detach` returns immediately.",
            "It prints one JSON launch handshake — `ok`, `supervision_id`, "
            "`pid`, `plan_dir`, `result_path`, `stderr_path` — and its exit "
            "code reports the **launch**, never the run",
            "The run's own verdict is only ever in the `result.json` at `result_path`",
            "reparented to init, so it outlives the tool call, the agent "
            "turn, and the harness session",
            "Never append `&`. Never rely on the harness backgrounding the "
            "command. Never invent a redirect target or a `mktemp` directory",
            "A foreground launch cannot work in this harness: a run lasts "
            "hours and a tool call is capped at 600 seconds",
            "a helper parented by a harness shell dies whenever that shell "
            "is reaped, at turn end, at session end, or at a harness restart",
            "**The first `asl outbox supervise` call is the proof of launch.**",
            "If it answers `LOST reason=helper-never-started`, the launch "
            "failed: report that to the user and do not re-enter the loop",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"asl skill omits detached-launch marker: {marker!r}",
            )
        self.assertNotIn(
            "--supervision-id <launch-identity> --json\n```",
            skill,
            msg="asl skill still prescribes a foreground helper launch",
        )

    def test_asl_skill_documents_the_lost_token(self) -> None:
        # Token details live in `references/supervise-tokens.md`; the skill as a
        # whole (SKILL.md plus its references) must still carry them.
        skill = read_asl_skill_bundle()
        normalized = " ".join(skill.split())

        for marker in (
            "Exactly one of four tokens is printed, always with exit code 0",
            "`LOST reason=<reason>` means this launch's supervising helper "
            "is gone without a terminal record. Stop the loop, run `asl "
            "status <plan-dir> --json`, and surface to the user what died "
            "and whether the run itself is still alive. **Never "
            "auto-relaunch**",
            "`helper-exited-without-record` means the helper recorded a "
            "process identity and that process is now gone",
            "`helper-never-started` is reported only when no run is holding "
            "the plan at all, so nothing is running and the launch itself "
            "failed",
            "It waits out a 120-second grace window first, so a slow start "
            "is never called dead",
            "`LOST` never preempts `DONE` or `ACTION`",
            "A run started as a plain `asl run` with no helper writes no "
            "process identity and stays `IDLE`",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"asl skill omits LOST marker: {marker!r}",
            )
        for token in (
            mailbox_cli_constants.SUPERVISE_TOKEN_LOST,
            mailbox_cli_constants.SUPERVISE_LOST_HELPER_EXITED,
            mailbox_cli_constants.SUPERVISE_LOST_HELPER_NEVER_STARTED,
        ):
            self.assertIn(
                token,
                normalized,
                msg=f"asl skill omits supervise constant: {token!r}",
            )
        self.assertIn(
            f"{int(mailbox_cli_constants.SUPERVISE_LIVENESS_GRACE_SECONDS)}"
            "-second grace",
            normalized,
        )

    def test_asl_skill_requires_evidence_for_every_health_claim(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        self.assertIn(
            "**absence of evidence**, the rule that a missing `DONE` record "
            "is not proof the helper is alive. Its operating rule: any claim "
            'about run health — including answering "is it still working?" '
            "— must cite a fresh `asl outbox supervise` token or a fresh "
            "`asl status <plan-dir> --json`, never the absence of output",
            normalized,
            msg="asl skill omits the health-claim rule under absence of evidence",
        )

    def test_readme_documents_the_lost_supervision_contract(self) -> None:
        readme = self._read("agent_session_loop/README.md")
        normalized = " ".join(readme.split())

        for marker in (
            "`DONE outcome=<outcome> seq=<n>`, or `LOST reason=<reason>` at "
            "exit code 0",
            "the helper records its `(pid, process birth time)` identity "
            "before starting the child",
            "`helper-exited-without-record` reports a helper whose process is gone",
            "`helper-never-started` reports one that never wrote an identity "
            "while no run holds the plan",
            "neither ever preempting `DONE` or `ACTION`",
            "exposed under `--json` as `lost_reason` and `run_evidence`",
            "Supervision is launched detached (`asl-autopilot.py --detach`)",
            "prints one JSON launch handshake carrying `pid`, `result_path`, "
            "and `stderr_path`",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"asl README omits supervision-liveness marker: {marker!r}",
            )

    def test_asl_skill_binds_a_silent_supervision_output_contract(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        for marker in (
            "#### Output contract (binding)",
            "A healthy run is silent.",
            "you may emit text in exactly three cases: (1) a `QUESTION` record "
            "needs the user's answer — relay it and ask; (2) supervision is "
            "blocked and cannot continue without the user; (3) Step 4's final "
            "report",
            "Everything else is forbidden output: acknowledging the launch, "
            "announcing a poll, restating a command you are about to run or "
            "just ran, summarizing an `IDLE` line, reporting elapsed time, "
            'quoting the payload, describing what the run "seems" to be '
            "doing, or reassuring the user that supervision continues",
            "When `asl outbox supervise` prints `IDLE`, print nothing and "
            "reissue the identical command immediately. Hours of silence is "
            "the correct output",
            "Poll exactly one thing: `asl outbox supervise`. Never also tail "
            "the helper's background output",
            "reading both doubles the noise for no added signal",
            "Notices are informational. They never wake supervision, are "
            "never relayed, and stay readable afterwards with `asl outbox "
            "wait <plan-dir> --include notices --since 0 --timeout 0`",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"asl skill omits output-contract marker: {marker!r}",
            )

    def test_docs_describe_terminal_supervision_record(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        readme = self._read("agent_session_loop/README.md")
        normalized_docs = " ".join(docs.split())
        normalized_readme = " ".join(readme.split())

        for marker in (
            "`notice`, `supervision-end`, worker `ack`",
            "A `supervision-end` record is the mailbox's terminal signal",
            "a required `outcome` from the closed enum `complete`, `paused`, "
            "`stopped`, `blocked`, `error`, an optional `run_id` validated "
            "only when present, and an optional `supervision_id`",
            "Every helper launch carries a `supervision_id`",
            "a reader treats only the record carrying its own launch identity "
            "as terminal",
            "`--supervision-id UUID` scopes that scalar to one supervising launch",
            "A record from another launch is still returned and still "
            "advances `next_since`",
            "Only the Autopilot helper writes the record, at most once per "
            "helper process",
            "On every outcome except `error`, the record is appended only "
            "after the complete `--json` result has been written and flushed",
            "`error` is the abnormal-exit outcome and carries no stdout guarantee",
            "the result may be absent, truncated, or complete but unproven",
            "must therefore decide from the record alone — the termination "
            "detail is in `text` — and recover run state from "
            "`asl status <plan-dir> --json` rather than parsing helper stdout",
            "`error` never comes from an ordinary terminal reason, so the "
            "outcome alone separates the two cases",
            "The runner deliberately does not emit one",
            "appends at the full file-cap tier alongside `receipt`",
            "`--include all` wakes on questions, notices, or the terminal "
            "`supervision-end` record",
            "returns separate `questions`, `notices`, and `supervision_ends` "
            "arrays plus the `supervision_ended` scalar",
            "A wake that carries only `supervision-end` records emits no "
            "Agent Mux handoff and writes no lease question projection",
        ):
            self.assertIn(
                marker,
                normalized_docs,
                msg=f"docs omit supervision-end marker: {marker!r}",
            )

        for marker in (
            "the Autopilot helper appends one terminal `supervision-end` message",
            "Every outcome except `error` is appended after the helper "
            "flushed its complete result",
            "`error` marks an abnormal exit whose stdout may be missing, "
            "truncated, or unproven and must be decided from the record alone",
            "`asl outbox wait --include all` wakes on it and reports it "
            "through `supervision_ends` and `supervision_ended`",
            "The runner emits no such record",
            "every launch carries a `supervision_id`",
            "reports `supervision_ended` only for that launch's own record "
            "while another launch's record still advances the cursor",
        ):
            self.assertIn(
                marker,
                normalized_readme,
                msg=f"README omits supervision-end marker: {marker!r}",
            )

    def test_docs_separate_helper_termination_from_run_termination(
        self,
    ) -> None:
        docs = self._read("docs/agent-session-loop.md")
        readme = self._read("agent_session_loop/README.md")
        # Token details live in `references/supervise-tokens.md`; the skill as a
        # whole (SKILL.md plus its references) must still carry them.
        skill = read_asl_skill_bundle()
        normalized_docs = " ".join(docs.split())
        normalized_readme = " ".join(readme.split())
        normalized_skill = " ".join(skill.split())

        self.assertNotIn(
            "has exited, so no further worker questions or notices are coming",
            normalized_docs,
        )
        for marker in (
            "the mailbox's terminal signal for one supervision lifetime",
            "It ends the helper's lifetime and does not prove the supervised "
            "run is quiescent",
            "`worker_hung` deliberately stops the helper without killing or "
            "waiting on that child, which stays alive holding the plan lock "
            "and can publish questions and notices after the terminal record",
            "asks `asl status <plan-dir> --json`; the record answers only "
            "whether this supervision is over",
        ):
            self.assertIn(
                marker,
                normalized_docs,
                msg=f"docs omit supervision-lifetime marker: {marker!r}",
            )

        self.assertIn(
            "That record ends the helper's supervision lifetime and does not "
            "prove the supervised run is quiescent: a `worker_hung` stop "
            "leaves the `asl run` child alive holding the plan lock, so later "
            "worker questions and notices can still land after the terminal "
            "record",
            normalized_readme,
        )

        for marker in (
            "The record ends this supervision, not necessarily the run. It "
            "proves the helper exited, not that the `asl run` child is "
            "quiescent",
            "a `worker_hung` stop leaves that child alive holding the plan lock",
            "Check `asl status <plan-dir> --json` before treating the plan as idle",
        ):
            self.assertIn(
                marker,
                normalized_skill,
                msg=f"asl skill omits supervision-lifetime marker: {marker!r}",
            )
        # The `worker_hung` procedure lives in the on-demand hang reference.
        hangs = " ".join(self._read(ASL_SKILL_QUOTA_AND_HANGS).split())
        self.assertIn(
            "Its terminal `supervision-end` record therefore marks the end of "
            "supervision only: that live child can still publish mailbox "
            "questions and notices after the record lands",
            hangs,
        )


if __name__ == "__main__":
    unittest.main()
