# ASL Autopilot: the supervise tokens in full

On-demand reference for the `asl` skill (`../SKILL.md`): what each
`asl outbox supervise` token carries and requires, how `LOST` is diagnosed, and
the polling cadence of the stdout fallback. The loop rule itself is in
`SKILL.md`; read this when a token is not `IDLE`, or before relying on the
fallback.

## Why each launch has its own identity

The launch identity is the only thing that separates this helper's terminal
record from one appended by another helper on the same plan. A helper may attach
to an already-active matching run, so two helper lifetimes can overlap and a
mailbox sequence captured at launch cannot tell a still-alive older helper's
later exit from this one's.

## Tokens

- Pass the same `--supervision-id` on every call. It scopes the terminal record
  to this launch and keys the persisted cursor, so the command is reissued
  verbatim and no `--since` is ever threaded by hand. The cursor starts at 0, so
  questions that were already open before the launch stay visible.
- Exactly one of four tokens is printed, always with exit code 0. A command
  error is not a token: it exits nonzero with a `reason:` payload instead (see
  the last item below).
- `IDLE` means nothing actionable happened: print nothing and reissue the
  identical command.
- `ACTION questions=<n>` carries `<n>` question records that no closure has
  answered yet, framed below the token. Surface them. A question an earlier
  call already reported that is still open — the reply failed, or the call
  was cut off before it landed — is reported again after a 30-second wait
  instead of `IDLE`, so a lost reply cannot leave the run waiting unseen.
- `DONE outcome=<outcome> seq=<n>` carries this launch's own terminal record:
  stop the loop and do not reissue. Read that `outcome` before trusting the
  helper's result. Every outcome except `error` means the helper wrote and
  flushed its complete `--json` result before appending the record — the
  `result.json` at the handshake's `result_path` under `--detach`, stdout
  otherwise — so it is already parseable when the token returns.
- `outcome: error` means the helper died abnormally — a raised exception or a
  termination signal — so its result may be missing, truncated mid-write, or
  complete but unproven. Never require parseable JSON there: take the
  termination detail from the record's `text`, recover run state with
  `asl status <plan-dir> --json`, and report the abnormal exit instead of a
  result.
- The record ends this supervision, not necessarily the run. It proves the
  helper exited, not that the `asl run` child is quiescent: a `worker_hung` stop
  leaves that child alive holding the plan lock (see
  `references/quota-and-hangs.md`), so worker questions and notices can still
  land after `DONE`. Check `asl status <plan-dir> --json` before treating the
  plan as idle.
- Another helper's terminal record — an earlier invocation, or one whose
  lifetime overlapped this launch — never ends this loop. It is absorbed
  internally: the cursor advances past it and the wait continues on the
  remaining budget. So is a batch whose questions have all already closed.
  Neither ever reaches stdout.
- The terminal record is durable log content read through a sequence cursor, not
  a live notification, so a helper that already exited before the first call
  returns `DONE` on that call instead of after the full timeout.
- `LOST reason=<reason>` means this launch's supervising helper is gone without
  a terminal record. Stop the loop, run `asl status <plan-dir> --json`, and
  surface to the user what died and whether the run itself is still alive.
  **Never auto-relaunch** — neither the helper nor the run. Below the token sit
  a one-sentence cause, a `Run evidence: lock ..., run_id ..., runner state
  ..., sessions <complete>/<total> complete.` line, an `Active turn: ...` line,
  and a `Next step:` line naming `asl status <plan-dir> --json`. Under `--json`
  the same arrives as `state: "LOST"` plus `lost_reason` and a `run_evidence`
  object (`lock_active`, `lock_pid`, `run_id`, `runner_state_fresh`,
  `active_turn`, `sessions_complete`, `sessions_total`); both keys are `null`
  under every other token.
- The two reasons call for different things. `helper-exited-without-record`
  means the helper recorded a process identity and that process is now gone; if
  the evidence shows an active lock, the `asl run` child is still working and
  only supervision died. `helper-never-started` is reported only when no run is
  holding the plan at all, so nothing is running and the launch itself failed —
  it is what `&`, a reaped harness shell, or an unwritable log directory looks
  like from here. It waits out a 120-second grace window first, so a slow start
  is never called dead.
- `LOST` never preempts `DONE` or `ACTION`: a wake carrying either outranks it,
  so a helper that finished is always reported as finished. A run started as a
  plain `asl run` with no helper writes no process identity and stays `IDLE`,
  because its lock proves it is alive.
- Absence of `DONE` is **not** proof that the helper is alive: `--no-mailbox`,
  SIGKILL, and a machine crash leave nothing behind. Keep both fallbacks — the
  bounded tail of the helper's output below, and the loop itself as the
  backstop.
- A command error keeps the ordinary contract: a `reason:` payload and a
  nonzero exit. It is the only way this command exits nonzero.

Cadence matters because the runner's first wait is five minutes: any polling
interval near or above 300 seconds can let a question open and expire entirely
between two observations. Tailing the helper's background output is the fallback
for when the mailbox is unavailable, or when no terminal record can ever arrive
(`--no-mailbox`, SIGKILL, machine crash) — not the primary path; when relying on
it, poll at least every 30 seconds until the helper reaches a terminal result.
Under the detached launch that output is the handshake's `stderr_path` file, so
tail that path rather than a backgrounded job.
