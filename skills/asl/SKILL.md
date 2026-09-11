---
name: asl
description: Autonomously supervise an `asl run` invocation end to end. Give it the exact `asl run ...` command and it starts or attaches to the run, polls structured status, surfaces pauses that need a human answer, and drives the run to completion. Companion to session-split, session-expand, and session-run.
allowed-tools: Bash, Read
---

# ASL Autopilot

## Quick Reference

- Supervise: `asl outbox supervise PLAN_DIR --supervision-id SUPERVISION_ID`
- Answer: `asl inbox reply PLAN_DIR --to QUESTION_ID --text REPLY_TEXT --wait-consumed --wait-timeout 300 --json`
- Steer: `asl inbox guidance PLAN_DIR --text GUIDANCE_TEXT --wait-delivered --json`
- Recover: `asl status PLAN_DIR --json`
- Every supervision command in one place: `asl supervisor-guide [--json]`

Read `references/supervisor-context.md` in this skill directory before
improvising a supervision command. It is the generated copy of the startup card
the harness already loaded, plus the full guide text; `asl supervisor-guide` is
authoritative whenever the two disagree. The binding workflow below still
governs how the helper is launched and how a paused run is resumed.

## Purpose

Supervise a single `asl run` invocation from start to finish. The user hands
this skill the exact `asl run ...` command they would type; the skill runs the
bundled helper, which starts or attaches to the run, polls `asl status --json`,
streams concise progress, and stops for a human answer only when the run pauses
on an interactive decision it cannot resolve. Unless the command carries
`--no-mailbox`, it establishes a supervisor lease before launch, discovers live
worker questions and notices, and keeps supervising while the runner waits for
a closure.

The helper owns command parsing, safety rejection, run start/attach/poll, pause
detection and the recoverable-pause retry budget (see payload `pause_policy`),
reply resume, and final-review verdict parsing. The runner itself owns automatic
repair/rereview cycles: after a findings review it materializes same-plan repair
sessions, executes them, and rereviews, bounded by
`asl run --max-review-fix-cycles` (default 4), the per-group reopen cap
`--max-repair-group-attempts` (default 2), and the absolute cycle ceiling
`--max-review-fix-cycles-ceiling` (default 8). This skill is supervision and
Q&A only — run the helper; when a paused run needs an answer, infer a
defensible reply or ask the user, then resume; and when it reports remaining
review findings, surface them to the user. It never drafts, splits, or expands
follow-up fix plans, and it does not relaunch a capped review by quietly
raising those limits.

## Input

The user provides the exact `asl run` command as the skill argument, for example:

```text
$asl asl run ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/
/asl asl run ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/
```

The command may include leading environment assignments (such as
`CLAUDE_CONFIG_DIR=~/.claude-personal`), quoted arguments, and normal `asl run`
flags — including `--max-review-fix-cycles N` (deprecated alias
`--max-review-fix-plans N`), `--max-repair-group-attempts N`, and
`--max-review-fix-cycles-ceiling N`, which bound the runner's own
repair/rereview loop. Shell operators, pipes, redirects, `--dry-run`, and
user-supplied `--reply` / `--reply-stdin` are not accepted — the helper owns
resume behavior. `--fresh-conversation` is refused for a different reason: it
is a one-use authorization to discard an agent's saved conversation, so it stays
with the person who gave it. Run
`asl run <plan-dir> --resume --fresh-conversation` yourself, then supervise the
plan. `--no-mailbox` is accepted and disables watcher construction,
lease writes, live guidance, notice observation, and mailbox question closure
for both launch and attach behavior.

`--spark` and `--no-spark` are ordinary forwarded run flags. Spark is an
implementation-only runner policy: eligible prepared Codex sessions may use
`gpt-5.3-codex-spark`, while review/audit turns, generated review repairs,
live model overrides, non-Codex harness switches, and ineligible sessions use
the resolved baseline. Do not reinterpret or remove either flag. The runner
owns classified quota/unavailable-model fallback, preserves the failed Spark
turn, and immediately retries the still-pending session on the baseline; the
Autopilot helper should supervise that continuation normally rather than add a
second retry of its own. Surface the `spark` and `spark_route` status metadata
when it explains a cooldown, baseline route, or fallback.

`--max-sessions N` is a **global** budget: the helper stops the whole supervised
run after `N` implementation sessions have completed, not `N` per relaunch. With
no `--max-sessions`, a single `asl run` process drives every pending session to
completion (there is no per-chunk restarting). See Step 4 for how a budget stop
is reported. A run can also be stopped gracefully **without** a budget — see
*Request a graceful stop mid-run* below.

For a registered multi-repository plan, `--cwd` is the common workspace root,
not one child checkout. Prefer the authoritative `workspace_root`,
`registry_digest`, and ordered `repositories` projection from `asl status
--json`; an omitted root may be filled from that binding, but a conflicting
root is `user_required`. Autopilot never infers a parent from one repository or
grants itself `--rebind-workspace` authority. A deliberate move is a separate
operator action:

```text
asl prepare PLAN_DIR --resume --cwd NEW_WORKSPACE --rebind-workspace
```

## Instructions

### Step 1: Run the bundled helper

The helper ships beside this skill at `<skill-dir>/scripts/asl-autopilot.py`,
where `<skill-dir>` is the directory containing this `SKILL.md`. Harnesses expose
the shared skill through a symlink, so resolve the real path before invoking.
Before launching it, mint a **launch identity** — one fresh UUID, kept for the
whole supervision:

```text
uuidgen
```

That identity is the only thing that separates this helper's terminal record
from one appended by another helper on the same plan. A helper may attach to an
already-active matching run, so two helper lifetimes can overlap and a mailbox
sequence captured at launch cannot tell a still-alive older helper's later exit
from this one's.

Then launch the helper **detached**, with the user's exact command and that
identity:

```text
<skill-dir>/scripts/asl-autopilot.py --command '<exact asl run command>' --supervision-id <launch-identity> --json --detach
```

`--detach` returns immediately. It prints one JSON launch handshake — `ok`,
`supervision_id`, `pid`, `plan_dir`, `result_path`, `stderr_path` — and its exit
code reports the **launch**, never the run: `0` once the fork succeeded, `1`
with `stage: "launch"` and `reason: "detach-failed"` when it did not. The run's
own verdict is only ever in the `result.json` at `result_path`, read in Step 4.
The supervision it forked is reparented to init, so it outlives the tool call,
the agent turn, and the harness session.

Never append `&`. Never rely on the harness backgrounding the command. Never
invent a redirect target or a `mktemp` directory — the helper owns where its
output lands, and `--supervision-log-dir DIR` is the only way to move it. A
foreground launch cannot work in this harness: a run lasts hours and a tool call
is capped at 600 seconds. Backgrounding it yourself is worse than useless — a
helper parented by a harness shell dies whenever that shell is reaped, at turn
end, at session end, or at a harness restart, and it dies leaving stdout empty,
which is indistinguishable from a healthy silent run.

The handshake proves only that the fork returned. **The first
`asl outbox supervise` call is the proof of launch.** If it answers
`LOST reason=helper-never-started`, the launch failed: report that to the user
and do not re-enter the loop.

#### Output contract (binding)

A healthy run is silent. Between launching the helper and the final report you
may emit text in exactly three cases: (1) a `QUESTION` record needs the user's
answer — relay it and ask; (2) supervision is blocked and cannot continue
without the user; (3) Step 4's final report.

Everything else is forbidden output: acknowledging the launch, announcing a
poll, restating a command you are about to run or just ran, summarizing an
`IDLE` line, reporting elapsed time, quoting the payload, describing what the
run "seems" to be doing, or reassuring the user that supervision continues.
When `asl outbox supervise` prints `IDLE`, print nothing and reissue the
identical command immediately. Hours of silence is the correct output.

Poll exactly one thing: `asl outbox supervise`. Never also tail the helper's
background output — that is the fallback for a dead or disabled mailbox
(`--no-mailbox`, SIGKILL, crash), and reading both doubles the noise for no
added signal.

Notices are informational. They never wake supervision, are never relayed, and
stay readable afterwards with
`asl outbox wait <plan-dir> --include notices --since 0 --timeout 0`.

Supervision is a strict turn-lifetime invariant: **never end the agent turn
while the helper is active**, even if the harness backgrounds the helper or
returns a session/cell identifier. Supervise by blocking on
`asl outbox supervise`, which long-polls the mailbox and returns the moment
something actionable lands:

```text
asl outbox supervise <plan-dir> --supervision-id <launch-identity> [--state-dir DIR]
```

- Pass the same `--supervision-id` on every call. It scopes the terminal record
  to this launch and keys the persisted cursor, so the command is reissued
  verbatim and no `--since` is ever threaded by hand. The cursor starts at 0, so
  questions that were already open before the launch stay visible.
- Exactly one of four tokens is printed, always with exit code 0.
- `IDLE` means nothing actionable happened: print nothing and reissue the
  identical command.
- `ACTION questions=<n>` carries `<n>` question records that no closure has
  answered yet, framed below the token. Surface them.
- `DONE outcome=<outcome> seq=<n>` carries this launch's own terminal record:
  stop the loop and do not reissue. Read that `outcome` before trusting stdout.
  Every outcome except `error` means the helper wrote and flushed its complete
  `--json` result before appending the record, so it is already parseable when
  the token returns.
- `outcome: error` means the helper died abnormally — a raised exception or a
  termination signal — so its stdout may be missing, truncated mid-write, or
  complete but unproven. Never require parseable JSON there: take the
  termination detail from the record's `text`, recover run state with
  `asl status <plan-dir> --json`, and report the abnormal exit instead of a
  result.
- The record ends this supervision, not necessarily the run. It proves the
  helper exited, not that the `asl run` child is quiescent: a `worker_hung` stop
  leaves that child alive holding the plan lock (see "reason: worker_hung" in
  Step 2), so worker questions and notices can still land after `DONE`. Check
  `asl status <plan-dir> --json` before treating the plan as idle.
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
  bounded stdout tail below, and the loop itself as the backstop.
- The wait returns on a five-second internal cadence, so a question surfaces
  within seconds rather than at the next poll boundary.
- A returned question that is still open also refreshes the body-free
  supervisor lease projection and makes a best-effort
  `AgentSessionLoopMailboxQuestion` handoff to AgentMux. This applies to the
  explicit supervisor command, not the helper's internal watcher; notification
  failure never changes the result.
- `--timeout` defaults to 550 seconds, which sits below the supervising
  harness's own tool-call ceiling (safe for a 600-second cap). Loop rather than
  spending the 1800-second maximum in a single call.
- Running this against a live helper heartbeats its lease; it does not take over
  supervision.
- An error keeps the ordinary contract: a `reason:` payload and a nonzero exit.

Cadence matters because the runner's first wait is five minutes: any polling
interval near or above 300 seconds can let a question open and expire entirely
between two observations. Tailing the helper's background output is the fallback
for when the mailbox is unavailable, or when no terminal record can ever arrive
(`--no-mailbox`, SIGKILL, machine crash) — not the primary path; when relying on
it, poll at least every 30 seconds until the helper reaches a terminal result.
Under the detached launch that output is the handshake's `stderr_path` file, so
tail that path rather than a backgrounded job.

`outbox supervise` filters its batch to questions no closure has answered, but a
question can still finish between the wake and the reply. Before replying to any
returned question, confirm it is still open: a `WAIT_ENDED` or
`CLOSURE_CONSUMED` record for that id, or an `asl inbox reply` rejected with
`question-not-waiting`, means it is already finished and must not be answered
again.

Surface every `QUESTION` record immediately. After queuing a reply or resolution,
continue supervising until a `CLOSURE_CONSUMED` record, an equivalent consumed
receipt, or a terminal pause/result is observed.

`--json` emits a single machine-readable result on stdout; progress heartbeats
go to stderr. Each heartbeat carries live progress — the running session label,
`(n/total)`, and `idle <n>s` since the child last emitted output — and escalates
to a `WARN possibly stalled` line once idle time crosses the stall threshold.
The WARN is soft: supervision keeps waiting. Above it sit two hard hang bars
that end supervision with `reason: worker_hung` while leaving the run alive —
see "reason: worker_hung" in Step 2. To
override the runner's cycle cap at launch, include `--max-review-fix-cycles N`
inside the `--command` string (`0` still reviews but creates no repair
sessions). The same launch command may also set `--max-repair-group-attempts N`
or `--max-review-fix-cycles-ceiling N` when the operator deliberately wants
different review-loop limits for that run.

With mailbox supervision enabled, the helper writes a fresh fixed-size lease
before starting the child, polls questions and notices every five seconds, and
renews the lease every 30 seconds using the existing child/attach wait loops—no
watcher thread is added. Each message appears on stderr as one bounded,
JSON-escaped `QUESTION id=... preview=...` or `NOTICE id=... preview=...`
record; its complete body is retained in the corresponding `questions` or
`notices` array of the final structured `outbox`. The watcher keeps one
physical-sequence cursor across attach and self-launch transitions. On attach
it replays notices attributed to the still-live run, suppressing duplicates for
the lifetime of that supervisor process while allowing another supervisor
process to replay them at least once. Notices never enter the open-question
projection or lease payload. The watcher stops renewing and lets the lease
expire when supervision returns.
Lifecycle progress is body-free and bounded: `WAIT_ENDED ...` identifies a
terminated wait and `CLOSURE_CONSUMED ...` confirms that the worker or runner
actually observed an accepted closure.

One-shot supervisor commands do not displace a live helper. When the on-disk
lease is fresh and carries a question projection, `asl inbox guidance`,
`asl inbox reply`, `asl outbox wait`, and `asl outbox supervise` heartbeat it —
the helper's supervisor
identity and projection survive with only the lease timestamp refreshed during
that initial heartbeat. A wait that returns an actionable open set then
refreshes that same identity with its exact latest projection. Only a missing,
stale, or unprojected lease is taken over with a fresh supervisor identity, so a
human stepping into an unsupervised plan still registers as its supervisor.

#### Send live guidance or future-turn controls

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

#### Deny a command or freeze a file mid-run

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

#### Request a graceful stop mid-run

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
runs the pending review when every session is already complete. See Step 4.

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
  stopped`. Handle it as an ordinary pause (Step 3) and never report it as a
  stop.
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

#### Redirect the worker mid-turn (explicit authorization only)

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

#### Two pauses that are decisions, not failures

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

### Step 2: Handle notices, live questions, and pauses

First distinguish an informational notice, a live mailbox question, and a
terminal pause. A `NOTICE id=...` record is nonblocking at-least-once progress:
observe or relay it, but never reply to it and never turn it into a user-owned
decision. A `QUESTION id=...` progress record and `waiting_for_reply` status are
nonterminal, healthy bounded activity. Close a live question from a separate
shell with exactly one of the commands below:

```text
asl inbox reply <plan-dir> --to <question-id> --text '<answer>' [--state-dir DIR] --json
asl inbox resolve <plan-dir> --to <question-id> --action continue [--note '<note>'] [--state-dir DIR] --json
asl inbox resolve <plan-dir> --to <question-id> --action pause [--note '<note>'] [--state-dir DIR] --json
```

Add `--wait-consumed --wait-timeout 300` when closing a live question so the
command can confirm the receipt. A successful append is only **accepted** until
`consumed: true` and a receipt are returned; a consumption timeout remains a
successful append with `consumed: false`. Never call an unconsumed closure
“delivered,” and keep polling the helper after either outcome.

Use `--text-stdin` or `--note-stdin` when a body should not appear in argv. A
reply supplies a factual answer. A `continue` resolution supplies no answer and
resumes the same session; its optional note is context, never an answer. A
`pause` resolution supplies no answer and deliberately converts the live wait
into the ordinary terminal paused-run recovery path. The first concurrent reply
or resolution wins, and the runner consumes that closure once. Do not stop the
helper, invoke `--reply-stdin`, or report a terminal pause while an unresolved
live wait is active. The runner waits five minutes initially.
Lease freshness is checked only at an active deadline: a fresh lease extends
the wait by 60 seconds, while an absent or stale lease ends it — unless the
runner detected a system suspend, which grants the supervisor a 90-second
awake grace to re-prove liveness before a stale lease ends the wait. Inside
that grace a stale lease neither ends the wait nor extends it, and the grace
never stretches the ceiling. The 30-minute ceiling bounds unsupervised time,
not wall clock: it rolls forward with every fresh lease, so keeping the
supervisor alive keeps the wait alive, and a wait nobody watches still ends
within 30 minutes of the last proof.
An already-committed reply or resolution takes
precedence over mailbox exhaustion and is consumed before exhaustion can end
the wait. If no closure is available, timeout, mailbox
fullness/unavailability, or a stale lease at an active deadline that outlasts
any post-suspend grace falls
back to the existing terminal pause flow below; there is no mailbox-only
terminal status.

Retry mechanics are helper-owned — see the result payload's `pause_policy`.
The helper auto-retries recoverable same-session pauses (`verify-failed`, `transport-failed`, `timeout`, `no-status-change`) up to twice per session and reason. Only `transport-failed` backs off before relaunching (5 seconds, then 30 seconds); the other recoverable pauses retry immediately. An unclassified `nonzero-exit` pause is never retried. Escalation follows the block's `user_action`: `reply` means an operator answer can clear the pause, so it goes through the mailbox wait; `inspect` means no answer can and the environment has to change instead, so it escalates immediately; `null` is a recoverable pause that needs no human. Three kinds of pause are `inspect` — the non-retryable harness failures that carry no question context (`quota-exhausted`, `auth-failed`, `invalid-model`); the pauses blocked on a workspace or environment precondition (`commit-preflight`, `commit-failed`, `repository-registry-required`, `workspace-repository-contention`, `not-loop-ready`, `required-checks-incomplete`, `review-not-clear`, `guard-protected-path-changed`, `guard-protected-path-unavailable`, `conversation-recovery-required`); and the verdicts a post-turn guard reached along with the turn outcomes nobody asked a question about (`workspace-write-scope-violation`, `workspace-write-guard-unavailable`, `review-guard-unavailable`, `review-mutated-worktree`, `plan-artifact-tampered`, `unexpected-status-change`, `multiple-status-changes`, `nonzero-exit`, `malformed-plan`, `malformed-review-repair`, `interrupted`, `redirect-successor-blocked`). The dividing principle is whether a mailbox answer can make the run proceed: a dirty worktree stays dirty however the operator answers. For that third group the same principle reads from the other side — the mailbox wait is entered only from the runner-pause disposition, so a guard verdict advertising `reply` was advertising an answer it had no channel to receive. An `inspect` pause carries its own `remediation` entry naming the workspace change that clears it, so read `action_required.remediation` before escalating. `requires_user` covers every pause a machine cannot retry, and `policy_version` distinguishes a schema change from a real contradiction when a persisted block is compared against a fresh classification; `policy_version: 3` is the revision that moved the blocked-precondition reasons off `reply`, and `policy_version: 4` the one that moved the guard verdicts and unclassifiable turn outcomes off it too. When a pause genuinely needs an answer, the result carries `reason: needs_agent_reply` with a `questions` block holding the resolver prompt, the paused agent's last message, the pause metadata, and artifact paths.

Choose the closure conservatively, on two axes rather than one:

- **Epistemic — is the answer defensible?** Is it clearly derivable from the
  plan, the repository, and the run artifacts referenced in the `questions`
  block? This is circle of competence: inside it the answer is a fact to read
  from those artifacts; outside it the answer is a guess, so relay it.
- **Consequence — is what the answer causes reversible?** A **type-2 door** is
  cheap to undo (an implementation detail, a naming choice, a retry, a wording
  fix) and may be answered autonomously once defensible. A **type-1 door** is
  hard or impossible to undo (a schema decision, a data migration, a deletion, a
  published artifact, anything the completion contract bakes in) and relays to
  the user **even when the answer is defensible** — the cost of being wrong,
  not the confidence of being right, decides who answers.

The resulting rule: defensible and reversible → reply; defensible but
irreversible → relay; not defensible → relay; plainly nonblocking → resolve
`continue`; should become ordinary paused recovery → resolve `pause`. The four
closures in full:

- Resolve `continue` autonomously only when the worker explicitly labels the
  message nonblocking or its informational nature is plain. Do not use a fake
  reply for an answerless continuation.
- Send a factual reply only when it is clearly defensible from the plan, the
  repository, and the run artifacts referenced in the `questions` block, **and**
  what it sets in motion is a type-2 door. Read those artifacts before deciding.
- Relay ambiguous decisions to the user and wait for their explicit answer when
  intent, a trade-off, or information outside the plan/repository/artifacts is
  required — and relay a type-1 door on the same terms even when the artifacts
  answer it.
- Resolve `pause` only when the live wait should deliberately become ordinary
  paused-run recovery instead of continuing in place.

After a terminal `needs_agent_reply` stop, resume by streaming the chosen reply
on stdin. The helper launches `asl run --reply-stdin`; the reply never appears
in argv:

```text
printf '%s' '<reply text>' | <skill-dir>/scripts/asl-autopilot.py --command '<same exact command>' --supervision-id <new-launch-identity> --json --reply-stdin --detach
```

The detached parent reads that reply to the end before forking, so it reaches
the run even though the forked supervision's own stdin is `/dev/null`.

Every helper launch is its own supervision: mint a fresh launch identity for it
and use that identity in the wait loop that follows, so the previous launch's
terminal record cannot end this one.

If the helper reports `reason: user_required` (a foreign active lock) or a
`blocked` / `status-error` stop, surface that to the user rather than resuming. A
paused run may also cite a `command-not-runnable` readiness finding (an
unresolved `argv[0]`, or a non-importable `python -m <module>`); surface it
rather than resuming.

Registered workspace guard and commit-group failures are also operator-visible,
not generic retry candidates:

- `workspace-write-scope-violation` retains the worker's changes, restores the
  handoff to pending, and reports qualified `Alias:path` evidence for writes in
  read-only or out-of-scope registered repositories and for added/removed
  registered repositories. A repository the plan never registered cannot raise
  it. Do not revert or adopt that delta; the operator must restore it or
  durably repair scope and refresh preparation evidence.
- `workspace-write-guard-unavailable` means complete Git evidence could not be
  captured for a registered repository. Treat it fail-closed and surface the
  affected aliases/topology. The failing node always sits inside the registry,
  so the repair is that repository's own evidence — there is no exclusion that
  waives it.
- `commit-failed` may represent a partial multi-repository commit group. Read
  the unresolved group, successful per-repository SHAs, failed alias, remaining
  aliases, and qualified artifacts from status/recovery output. Never undo or
  recommit recorded successes; after the failing repository is repaired,
  rerunning the same command resumes the durable group before another worker.

#### Optional: sleep through a quota reset

A `quota-exhausted` pause is fail-closed by default — the helper stops
`user_required` and waits for a human. Pass `--wait-quota-reset` to the
**helper** (not inside the `--command` string) to let it sleep to the
provider's reset instead:

```text
<skill-dir>/scripts/asl-autopilot.py --command '<exact asl run command>' --supervision-id <launch-identity> --json --detach --wait-quota-reset
```

It waits only when every guard passes: the pause is `quota-exhausted`, the
runner parsed a reset hint (`retry_at` — a `Retry-After` second count, an ISO
timestamp, `N hours` / `N minutes`, or a time of day with an optional
`(Area/City)` zone; one naming an instant off the end of the calendar counts as
unparsed), the resulting wait fits an eight-day ceiling, and fewer than eight
resets have already been slept through in this supervision. It then
sleeps to that reset plus a five-minute buffer — in short slices measured
against the wall clock, so a machine suspend is noticed rather than slept out —
and relaunches the same plan through the ordinary chunk machinery. A reset
already in the past waits just the buffer, and each attempt spends one of the
eight resumes, so a provider that keeps rejecting converges to `user_required`
rather than looping. `auth-failed` and `invalid-model` never wait. Any declined
guard keeps today's `user_required` stop and names the guard under
`quota_wait.skipped`, so a run that could have waited always says why it did
not.

#### reason: worker_hung

Two hard hang bars sit above the soft `WARN possibly stalled` line: the
runner's `result_received_process_alive` suspicion observed on consecutive
probes, or a probe whose idle time reaches the idle ceiling (classed
`zero_cpu_idle` when the runner's suspicion says so, else
`idle_ceiling_exceeded`). Both rest on the runner's process-tree CPU sampling,
accumulated per process identity so a descendant exiting between two samples is
not read as the tree going idle:
it raises neither suspicion class while the tree is burning CPU, and the idle
ceiling stands down whenever the live `hang_cpu_progress` sample reports
`progressing: true` — a worker piping through a buffering consumer is silent,
not stalled. An unknown sample (no fresh `ps`) still escalates.
When either bar fires, the helper stops with `reason: worker_hung`
without killing or waiting on the `asl run` child. Before the helper can reach
that unresolved stop, the runner gives only `result_received_process_alive` one
automatic recovery attempt: it re-samples the tree, requires the harness root
and each deepest leaf to retain the same pid, birth time, command, and ancestry,
then sends SIGTERM once to the verified leaves. It never escalates to SIGKILL,
never targets parent shells, and never does this for generic `zero_cpu_idle`, a
progressing or failed sample, a root-only tree, or when process sweeping is
disabled. Resumed output clears the episode and the same runner continues.
`worker_hung` means that recovery was inapplicable or did not resume the turn;
the supervised child stays alive and keeps the plan lock. Its terminal
`supervision-end` record therefore marks the end of supervision only: that live
child can still publish mailbox questions and notices after the record lands.

Read `result.hang` first. It carries the `class`, the runner's `hang_suspected`
block verbatim (including its sampled `process_tree` and additive `recovery`
report), `idle_seconds`, `probes`, the plan/state/events paths, and the active
turn's artifact paths. The recovery report names its policy, attempt time,
signal, candidate/signaled/already-gone pids, identity mismatches, and signal
failures. Automatically recovered episodes have a later `turn_hang_cleared`
event and never reach this stop; a report inside `worker_hung` is an unresolved
attempt, not permission for a stronger signal. `supervised_child_pid` and
`detached` appear only when the helper launched the chunk itself. Then:

1. **Re-verify before trusting the snapshot** — pids get reused, so match on
   pid, birth time, command, and ancestry: compare
   `ps -axo pid,ppid,state,time,lstart,command` against
   `hang.hang_suspected.process_tree`.
2. **Ask the user before any further signal.** The automatic boundary was
   SIGTERM-only; unresolved cases require explicit approval.
3. If approved, kill **only the identified leaf** — the deepest zero-CPU
   descendant (e.g. a `perl .../diff-so-fancy` pager wedged under the worker's
   `/bin/zsh -c`) — never the runner or the `asl run` process.
4. Re-run the same helper command to re-attach and continue supervision.

### Step 3: Handle review findings

When the supervised command used `--review-harness`, a clean completion still
carries the review verdict. An all-clear review returns `reason: complete` with a
`review` block whose `outcome` is `all-clear`; nothing more is needed. When the
review reports findings, the helper returns `reason: review_findings` with a
`review` block holding `review_output_path` (the `final-review.md` to read in
full), `status_line`, `total_findings`, `findings_by_severity`, and a bounded
`summary`.

A run whose review cycle ends with findings still open exits `ok: false` with
`reason: review-findings-unresolved` rather than `all-complete`; the helper
re-tags it `review_findings` instead of treating it as a blocked run, so the
severity counts survive.

`review_findings` means no automatic continuation remains for the command's
current cap and artifact state. Do **not** draft, split, or expand a
follow-up fix plan. Instead:

1. Read the full `final-review.md` at `review_output_path`, then run
   `asl review-verdict <plan-dir> --json` for the authoritative verdict and relay
   its `next_steps` remediation commands.
2. Hand the remaining findings to the user with the status line and the run's
   recorded cycle disposition (`review_fix_disposition`). When the payload's
   `review_resume` block reports `higher-cap-required` or `artifact-invalid`,
   include its minimum cap or artifact-remediation detail so the user can resume
   the same plan. When it reports `repair-group-exhausted` or
   `cycle-cap-exhausted`, do **not** relaunch with a raised cap; report the
   recurring `repair_group` (when present) and hand control back to the
   operator for a manual fix or resplit decision.

Registered review is one combined read-only review across the ordered registry.
Every finding, evidence path, and completion check is repository-attributed;
repair children read the full registry but write only finding-derived aliases,
and every review or repair turn passes the same workspace mutation guard. Keep
aliases on findings and report qualified paths—never collapse the review to one
repository or treat a mutation from the read-only review harness as a finding.

### Step 4: Report

The detached helper's structured result is the `result.json` at the handshake's
`result_path`, not this turn's stdout; `stderr_path` holds the progress
heartbeats. Read `result_path` once `DONE` ends the loop, and fall back to
`asl status <plan-dir> --json` when the outcome was `error` and that file may be
truncated or absent.

Summarize the outcome: which run was supervised, whether it completed or paused,
and any blocker that needs the user's attention.

When the supervised command set `--max-sessions N` and the budget is spent with
sessions still pending, the helper returns `reason: max-sessions` with `ok: true`,
plus `completed` and `pending` counts. This is an **intentional, clean early
stop** — not a pause and not a failure. Report the progress (e.g. "completed N of
M sessions; K pending") and do **not** auto-resume, reply, or run `asl questions`;
re-running the same command later continues from the remaining sessions.

The same holds for `reason: stopped`, which the helper returns when the runner
honored an out-of-band `asl control` request (see *Request a graceful stop
mid-run* under Step 1). It too carries `ok: true` with `completed` and `pending`
counts and is an **intentional, clean early stop** — never report it as a pause,
a failure, or `blocked`, and never auto-resume it. Report the progress and say
that re-running the same command continues from the remaining sessions, or runs
the pending review when the stop landed after the last session.

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

#### Report the supervisor audit

When ASL audited its own supervision of the run, the helper attaches a
display-only `assurance` block to its result: `state` (`all-clear`, `findings`,
`unavailable`, `skipped`, or `pending`), `issue_ids` such as `ASI-42`,
`issue_count`, `new_issue_count`, `recurring_issue_count`, `partial`,
`status_line`, and `list_command`. Relay it and stop there:

- If `issue_ids` is present, say what the audit found and hand the user the
  `list_command` (`asl assurance list`) so they can read the inbox themselves.
- `partial: true` means an `unavailable` audit still reported what it managed
  to see, so report those findings as incomplete rather than as the whole
  picture.
- `all-clear`, `skipped`, and `pending` need no line of their own; announcing a
  clean audit every run is what teaches a user to skip the one line that
  matters.
- The audit is advisory. It never changed the run's outcome, so never restate a
  successful run as failed, or a pause reason as an audit finding, because an
  issue was opened.

This skill reports assurance and never drives it. Do not pass, add, or remove
`--supervisor-audit`, `--no-supervisor-audit`, `--audit-harness`,
`--audit-harness-model`, `--audit-effort`, or `--audit-timeout` — they belong to
the exact `asl run` command the user gave. Never run `asl assurance
process-pending`, never resolve, dismiss, accept-risk, merge, or close out an
issue, and never run `asl assurance plan`: dispositions and improvement drafts
are the user's decisions, and the helper has no field through which an audit
could be configured, launched, retried, or disposed of.

## Notes

- One `asl run` command per invocation.
- The helper — not the skill prose — owns command parsing, safety rejection,
  run start/attach, polling, the retry budget (`pause_policy`), reply handling,
  and final-review verdict parsing. Keep the skill workflow thin.
- Automatic repair/rereview cycles are runner-owned, bounded by
  `asl run --max-review-fix-cycles` (default 4; `--max-review-fix-plans` is a
  deprecated alias), `--max-repair-group-attempts` (default 2), and
  `--max-review-fix-cycles-ceiling` (default 8). Corrected artifacts can resume
  the same plan; this skill never creates follow-up fix plans, and on
  `repair-group-exhausted` or `cycle-cap-exhausted` it reports the recurring
  `repair_group` instead of relaunching with higher caps.
- Preserve registered workspace semantics across launch, attach, replies,
  combined review, and recovery: use the bound workspace root, retain ordered
  repository aliases and qualified paths, never rebind automatically, and
  preserve partial commit SHAs.
- The helper is always launched with `--detach`, so its lifetime never depends
  on the harness shell that started it, and its result is a file rather than
  this turn's stdout.
- `asl outbox supervise` is the supervision loop and the only thing polled; it
  prints one of four tokens, and `LOST` ends the loop the way `DONE` does —
  reported to the user with `asl status <plan-dir> --json` evidence, never
  auto-relaunched. Tailing the helper's background output is the fallback for
  when the mailbox is unavailable. `asl outbox wait` remains the
  general-purpose reader, including for notices after the fact.
- Keep the mailbox discipline on both axes of Step 2's closure rule, which is
  the single statement of it: continue only plainly nonblocking messages, reply
  only from clearly defensible plan/repository/artifact facts *and* only when
  the consequence is reversible, relay user-owned ambiguity and every
  irreversible type-1 decision even when the artifacts answer it, and use pause
  only to enter ordinary paused recovery.
- Mental models: read the sibling reference `../references/mental-models.md` on
  demand rather than inlining it, and cite at most 3 by name. This skill's
  assigned models are **reversibility (type-1 / type-2 doors)** and **circle of
  competence** — the two axes of the closure rule above — plus **absence of
  evidence**, the rule that a missing `DONE` record is not proof the helper is
  alive. Its operating rule: any claim about run health — including answering
  "is it still working?" — must cite a fresh `asl outbox supervise` token or a
  fresh `asl status <plan-dir> --json`, never the absence of output. Silence is
  what a healthy run and a run that never started look like alike.
- Mailbox failure is fail-open for supervision: continue owning the child and
  let the runner use the ordinary pause/resume flow. Never invent a mailbox
  reply, truncate the append-only log, delete a lease, or add Workbench
  mailbox/model-control endpoints.
