# ASL Autopilot: mid-run controls

On-demand reference for the `asl` skill (`../SKILL.md`). Read it before
steering, guarding, stopping, aborting, or redirecting a supervised run, and
when a run pauses for a decision rather than a failure.

## Send live guidance or future-turn controls

The helper owns the foreground, so send live commands from a separate shell (or
start it in the background):

```text
asl inbox guidance <plan-dir> --text '<guidance>' [--wait-delivered] [--wait-timeout SECONDS] [--state-dir DIR] --json
asl control set-model <plan-dir> [--harness codex|claude|copilot] --harness-model MODEL [--effort EFFORT] [--state-dir DIR] --json
```

Guidance may steer implementation decisions inside the selected session, but
cannot override its fixed scope, completion contract, safety requirements, or
runner-owned mutation rules. Model/effort controls are observed on the
five-second control cadence but affect only the next implementation invocation
in the current runner process; they never modify the active subprocess or a
review turn.

Always pass `--wait-delivered`. Without it the command reports only what it
wrote, and prints `Guidance queued but not delivered (no delivery wait
requested)`. With it, ASL waits for the delivery ledger to reach a verdict and
tells you which one it reached:

- `Guidance delivered live to <harness>` — the running turn received it. Codex
  is steered over its app-server, claude over a bidirectional stream-JSON
  conversation. Delivery is never inferred from a successful write to a pipe:
  claude's is confirmed only by a protocol acknowledgement or by an ASL-owned
  `PreToolUse` hook that injects the text as additional context.
- `Guidance queued but not delivered (<reason>)` — nothing received it yet. A
  copilot turn cannot be steered mid-flight (`unsupported-active-steer`); ASL
  never cancels or restarts a turn to deliver guidance. Between turns the
  reason is `no-active-turn`. Either way the guidance stays pending and is
  folded into the next turn's initial prompt (`delivered-next-turn`), or is
  picked up when the worker runs `asl inbox read` (`delivered-by-read`).

`asl status --json` projects the same verdicts under `mailbox.guidance`: state,
harness, turn, reason, and receipt timestamp — never the guidance body.

Guidance is advice, never enforcement. Telling a worker to make no further
changes and exit does **not** stop the session: even guidance the ledger
confirms as `delivered-live` only reaches the worker's context, and a worker
that ignores it keeps working. Only `asl control` stops a run — `stop-after-current` at the next
session boundary, `abort` inside the turn now in flight. Never report a run as
stopped because guidance asking it to stop was sent.

## Deny a command or freeze a file mid-run

When the instruction is enforceable rather than advisory, use a guard instead of
guidance:

```text
asl control guard add <plan-dir> [--deny-command "CMD ARG ..."] [--protect-path ALIAS:PATH] [--reason TEXT] [--wait] [--timeout SECONDS] [--state-dir DIR] --json
asl control guard remove <plan-dir> --id GUARD_ID [--wait] [--timeout SECONDS] [--state-dir DIR] --json
asl control guard list <plan-dir> [--state-dir DIR] --json
```

Pick by what you actually mean:

- **guidance** for the *direction* of the work — what to build, what approach to
  take. It is advice and nothing enforces it.
- **`--deny-command`** for an *action that must not happen*, such as
  `git push origin main`. Matched as an exact argv prefix after POSIX
  tokenization; a denied invocation exits `126` with a stable message.
- **`--protect-path`** for *files that must end the turn exactly as they
  started*, written `Alias:glob` (`.` is the alias for a single-repository
  plan). Compared against the turn-start snapshot at finalization, so a change
  made before you added the guard still counts and a change that was undone
  exactly does not.
- **`abort`** only when the *turn running right now* must be terminated. Neither
  guard kind cancels a running turn.

Guards last for the current run only, and both `add` and `remove` fail with
`no-active-run` when there is none. Use `--wait` when it matters: without it the
command proves only that the request was queued, while `--wait` waits for
`guard-applied`, `guard-removed`, or `guard-rejected`.

A protected path that differs at finalization does **not** roll anything back.
The run keeps every filesystem change, rejects the turn's result, returns the
session to `pending`, and pauses with `guard-protected-path-changed` (or
`guard-protected-path-unavailable` when it had no comparison to read). Both are
inspect-and-repair pauses: restore the path or remove the guard, then resume.
No mailbox reply resolves them.

`asl control guard list` and `asl status` report each guard's coverage per lane
(`command-gate`, `pretool`, `postflight`) as `active`, `degraded`, or
`unsupported` with a reason — read it rather than assuming, since a lane a
harness cannot offer is reported honestly instead of silently missing. Guards
are an operational safeguard, not a sandbox: they do not stop an equivalent
operation performed by another executable, a library call, or a raw network
request.

## Request a graceful stop mid-run

To stop a supervised run early without waiting for it to finish, request the
stop **out of band**:

```text
asl control stop-after-current <plan-dir> [--state-dir DIR] [--wait] [--timeout SECONDS] --json
asl control abort <plan-dir> [--grace SECONDS] [--reason TEXT] [--state-dir DIR] [--wait] [--timeout SECONDS] --json
```

Pass the same `plan-dir` and `--state-dir` the helper was given. Because the
helper blocks in the foreground, send this from a **separate shell** — or start
the helper in the background — whenever a mid-run stop may be needed. The request
is queued durably, so it is also safe to send when no run is active yet: the next
runner to drain the queue applies it.

`stop-after-current` takes effect **after the current session finishes** and
prevents everything the run would start next — remaining sessions, the generated
`--review-harness` review, and review repair children — surfacing as
`reason: stopped` with `ok: true` plus `completed` and `pending` counts — an
intentional clean early stop, not a pause and not a failure. This holds whether
the helper launched the run itself or **attached** to a run already in flight:
in the attach case it reads the stop back from the finished run's persisted
state, so it never relaunches over an honored request. Do **not** auto-resume
it; re-running the same command later continues from the remaining sessions, or
runs the pending review when every session is already complete. See Step 4
of `../SKILL.md`.

`abort` is the enforcing mechanism when the session running *right now* must not
finish — a worker heading somewhere wrong, or a workspace that has to be
rebound. The runner delivers the abort to the worker as guidance naming
`--reason`, gives it `--grace` seconds (default 120) to wind down, and kills the
harness process group at the deadline if it has not. **An abort has three
outcomes and only one of them stops the run**, so read the run's own terminal
state before reporting any of them:

- **The turn was killed** (the worker did not wind down in time). The session
  goes back to `pending` with a Handoff note saying it holds no completion, and
  the run exits `reason: stopped` with `stop_kind: abort` — a clean early stop,
  never a pause and never a failure. Because that session is reverted rather
  than completed, the progress counts show one fewer completed session than the
  worker appeared to have done.
- **The worker wound down and paused itself** inside the grace window. Nothing
  was killed, so the worker's own pause owns the outcome: the run comes back
  `paused` with the worker's pause reason and `ok: false`, *not* `reason:
  stopped`. Handle it as an ordinary pause (Step 2 of `../SKILL.md`) and never
  report it as a stop.
- **The worker completed its session** inside the grace window. Nothing was
  killed and nothing was reverted: the session stays `complete` and **the run
  continues to the next session**. The abort is spent, so re-issue
  `asl control abort` or `stop-after-current` if the run still must stop, and
  never report the run as stopped on the strength of the abort alone.

An abort that arrives between turns has nothing to kill and degrades to
`stop-after-current`.

Use `--wait` whenever the stop actually matters. Without it the command only
proves the request was appended to the durable queue — it exits 0 whether or not
any runner ever saw it, which is exactly how a stop can look sent and still be
ignored. With `--wait` the command exits 0 only after the runner records its
receipt, and `asl control abort --wait` waits further, for the
`abort-turn-honored` or `abort-turn-satisfied-by-worker` outcome. Give
`--timeout` a value above the `--grace` you requested, since the default wait is
60 seconds and the default grace is 120. Only `abort-turn-honored` proves the
run is stopping; `wait.terminal_action: abort-turn-satisfied-by-worker` says
just that the turn ended by itself, which is the paused or still-running outcome
above — check the run's terminal state before reporting a stop.

A `--wait` that times out is a **failure to report, never an assumed success**.
Report the reason verbatim and what it means, and do not claim the run stopped:

- `no-runner` — nothing is listening. The command is still queued and applies
  whenever a runner next drains it.
- `receipt-timeout` — a runner holds the lock but did not acknowledge in time.
  The command is still queued; the runner may be mid-turn or wedged.
- `abort-timeout` — the runner acknowledged the abort but its outcome has not
  been recorded yet. The abort is armed and the grace window may simply be
  longer than the wait.

In every case the request remains durably queued — the timeout is about the
receipt, not the append — so re-sending is rarely the answer. Report the state
and hand the decision to the user.

## Redirect the worker mid-turn (explicit authorization only)

```text
asl control redirect <plan-dir> --text '<correction>' [--session ID] [--turn N] [--operation-id ID] [--ttl SECONDS] [--timeout SECONDS] [--state-dir DIR] --json
asl control redirect-status <plan-dir> [--redirect-id ID] [--wait-acknowledged] [--timeout SECONDS] [--state-dir DIR] --json
```

**Use this only when the user has explicitly authorized this correction.**
Never self-authorize a redirect from a supervision signal, a failing check, or
your own suspicion that the worker is heading somewhere wrong — relay the
suspicion and let the user decide. Guidance is the advisory lever and a guard
is the enforcing one; a redirect is neither. It **interrupts the turn in
flight**: an accepted correction stops the current worker and the runner
continues the same session on the predecessor's conversation, carrying the
correction into the successor's prompt. That is a per-correction authorization,
not standing authority — it is deliberately not one of the fleet overseer's
operations (`reply`, `guidance`, `verify`, `commit`, `resume`).

Submission is **at-most-once**. A `--timeout` that expires reports the
submission *uncertain* rather than retracting it: the correction may already be
accepted. Do not resubmit blindly. Read `asl control redirect-status` — the only
surface that settles it — and, if you do resubmit, reuse the same
`--operation-id` with the same text, which the journal treats as the same
request rather than a second one. Watching is observational, so a
`--wait-acknowledged` timeout retracts nothing.

## Two pauses that are decisions, not failures

Neither is retryable, so neither spends any retry budget, and neither is ever
auto-resumed. Both surface as `reason: user_required` with a `recovery` block
naming `decision`, `auto_resume: false`, an `inspect_command`, and an
`authorization_command`. Report that block verbatim; do not act on it.

- **`conversation-recovery-required`** (`decision: fresh-conversation`). The
  recorded conversation cannot be resumed — incompatible, unconfirmed, retired,
  or corrupt. Run the `inspect_command`
  (`asl control recover-conversation <plan-dir> --json`) to read why. The
  `authorization_command` is the exact one-use relaunch that discards the saved
  conversation. **Print it; do not run it.** Discarding an agent's saved
  reasoning is the user's decision, and the helper refuses a supervised command
  carrying `--fresh-conversation` precisely so that decision cannot be replayed
  on every retry.
- **`redirect-successor-blocked`** (`decision: predecessor-not-stopped`). A
  redirected turn's predecessor could not be shown stopped, so a successor
  would run beside a worker that may still be alive. The `inspect_command` is
  `asl control redirect-status <plan-dir> --json`, and `authorization_command`
  is `null` — there is no authorization for this one. No text anyone sends makes
  a second worker safe to start beside a live one; the machine has to show the
  predecessor gone. Report the `blocked_reason` and hand it to the user.

## Reporting a stop

`stop_kind` names which request produced that stop. `stop-after-current` means
the run finished its session and stopped at the boundary. `abort` means
`asl control abort` ended the turn that was in flight: the aborted session was
reverted to `pending` with a Handoff note saying it holds no completion, so it
is *not* counted in `completed` and its work is not on disk as a finished
session. Say so when reporting an abort — an operator who reads only the counts
will otherwise assume the aborted session landed. It is still a clean early
stop: same `ok: true`, still never a pause or a failure, still never
auto-resumed. An abort the worker satisfied inside its grace window never
reaches this verdict — it comes back as that worker's pause, or as a run that
kept going — so never infer `stopped` or a reverted session from having sent an
abort.
