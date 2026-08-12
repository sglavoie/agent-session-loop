---
name: asl
description: Autonomously supervise an `asl run` invocation end to end. Give it the exact `asl run ...` command and it starts or attaches to the run, polls structured status, surfaces pauses that need a human answer, and drives the run to completion. Companion to session-split, session-expand, and session-run.
allowed-tools: Bash, Read
---

# ASL Autopilot

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
`asl run --max-review-fix-cycles` (default 4). This skill is supervision and Q&A
only — run the helper; when a paused run needs an answer, infer a defensible
reply or ask the user, then resume; and when it reports remaining review
findings, surface them to the user. It never drafts, splits, or expands
follow-up fix plans.

## Input

The user provides the exact `asl run` command as the skill argument, for example:

```text
$asl asl run ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/
/asl asl run ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/
```

The command may include leading environment assignments (such as
`CLAUDE_CONFIG_DIR=~/.claude-personal`), quoted arguments, and normal `asl run`
flags — including `--max-review-fix-cycles N` (deprecated alias
`--max-review-fix-plans N`), which bounds the runner's own repair/rereview
cycles. Shell operators, pipes, redirects, `--dry-run`, and user-supplied
`--reply` / `--reply-stdin` are not accepted — the helper owns resume behavior.
`--no-mailbox` is accepted and disables watcher construction, lease writes,
live guidance, notice observation, and mailbox question closure for both launch
and attach behavior.

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

Then run the helper with the user's exact command, passing the identity:

```text
<skill-dir>/scripts/asl-autopilot.py --command '<exact asl run command>' --supervision-id <launch-identity> --json
```

Supervision is a strict turn-lifetime invariant: **never end the agent turn
while the helper is active**, even if the harness backgrounds the helper or
returns a session/cell identifier. Supervise by blocking on `asl outbox wait`,
which long-polls the mailbox and returns the moment a record lands:

```text
asl outbox wait <plan-dir> --include all --supervision-id <launch-identity> --since <next_since> --timeout 550 [--state-dir DIR] --json
```

- Pass the same `--supervision-id` on every call. It scopes `supervision_ended`
  to this launch and changes nothing else about the wait.
- Start at `--since 0` so questions that were already open before the launch
  stay visible, then feed each response's `next_since` into the next call.
- `supervision_ended: true` means the batch carries this launch's own terminal
  record: stop the wait loop and do not reissue. Read that record's `outcome`
  before trusting stdout. Every outcome except `error` means the helper wrote
  and flushed its complete `--json` result before appending the record, so it
  is already parseable when the wait returns.
- `outcome: error` means the helper died abnormally — a raised exception or a
  termination signal — so its stdout may be missing, truncated mid-write, or
  complete but unproven. Never require parseable JSON there: take the
  termination detail from the record's `text`, recover run state with
  `asl status <plan-dir> --json`, and report the abnormal exit instead of a
  result.
- The record ends this supervision, not necessarily the run. It proves the
  helper exited, not that the `asl run` child is quiescent: a `worker_hung` stop
  leaves that child alive holding the plan lock (see "reason: worker_hung" in
  Step 2), so worker questions and notices can still land after the terminal
  record. Check `asl status <plan-dir> --json` before treating the plan as idle.
- A `supervision-end` record in `supervision_ends` while `supervision_ended` is
  `false` belongs to another helper on this plan — an earlier invocation or one
  whose lifetime overlapped this launch. Handle nothing, let `next_since`
  advance past it, and keep waiting.
- The record is durable log content read through a sequence cursor, not a live
  notification, so a helper that already exited before the first wait was issued
  returns it on that first call instead of after the full timeout.
- Absence of the record is **not** proof that the helper is alive: `--no-mailbox`,
  SIGKILL, and a machine crash leave nothing behind. Keep both fallbacks — the
  bounded stdout tail below, and the `--timeout 550` loop as the backstop.
- Anything else means supervision continues. `timed_out: true` with empty
  `questions`, `notices`, and `supervision_ends` means nothing happened — reissue
  immediately. `timed_out: false` with `supervision_ended: false` means some
  other producer woke the wait; handle those records and reissue as before.
- The wait returns on a five-second internal cadence, so a question surfaces
  within seconds rather than at the next poll boundary.
- A returned question that is still open also refreshes the body-free
  supervisor lease projection and makes a best-effort
  `AgentSessionLoopMailboxQuestion` handoff to AgentMux. This applies to the
  explicit `asl outbox wait` command, not the helper's internal watcher;
  notification failure never changes the wait result or its JSON schema.
- Cap `--timeout` below the supervising harness's own tool-call ceiling (550 is
  safe for a 600-second cap) and loop, rather than spending the 1800-second
  maximum in a single call.
- Running this against a live helper heartbeats its lease; it does not take over
  supervision.

Cadence matters because the runner's first wait is five minutes: any polling
interval near or above 300 seconds can let a question open and expire entirely
between two observations. Tailing the helper's background output is the fallback
for when the mailbox is unavailable, or when no terminal record can ever arrive
(`--no-mailbox`, SIGKILL, machine crash) — not the primary path; when relying on
it, poll at least every 30 seconds until the helper reaches a terminal result.

`outbox wait` selects on sequence and kind only, with no answer-state filter, so
it re-returns a question whose seq is past the cursor even when that question was
already replied to, resolved, or expired. Before replying to any returned
question, confirm it is still open: a `WAIT_ENDED` or `CLOSURE_CONSUMED` record
for that id, or an `asl inbox reply` rejected with `question-not-waiting`, means
it is already finished and must not be answered again.

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
inside the `--command` string (`0` still reviews but creates no repair sessions).

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
`asl inbox reply`, and `asl outbox wait` heartbeat it — the helper's supervisor
identity and projection survive with only the lease timestamp refreshed during
that initial heartbeat. A wait that returns an actionable open set then
refreshes that same identity with its exact latest projection. Only a missing,
stale, or unprojected lease is taken over with a fresh supervisor identity, so a
human stepping into an unsupervised plan still registers as its supervisor.

#### Send live guidance or future-turn controls

The helper owns the foreground, so send live commands from a separate shell (or
start it in the background):

```text
asl inbox guidance <plan-dir> --text '<guidance>' [--state-dir DIR] --json
asl control set-model <plan-dir> [--harness codex|claude|copilot] --harness-model MODEL [--effort EFFORT] [--state-dir DIR] --json
```

Guidance may steer implementation decisions inside the selected session, but
cannot override its fixed scope, completion contract, safety requirements, or
runner-owned mutation rules. It remains pending until the worker runs
`asl inbox read`. Model/effort controls are observed on the five-second control
cadence but affect only the next implementation invocation in the current
runner process; they never modify the active subprocess or a review turn.

#### Request a graceful stop mid-run

To stop a supervised run early without waiting for it to finish, request the
graceful stop **out of band**:

```text
asl control stop-after-current <plan-dir> [--state-dir DIR] --json
```

Pass the same `plan-dir` and `--state-dir` the helper was given. Because the
helper blocks in the foreground, send this from a **separate shell** — or start
the helper in the background — whenever a mid-run stop may be needed. The request
is queued durably, so it is also safe to send when no run is active yet: the next
runner to drain the queue applies it.

The stop takes effect **after the current session finishes** and prevents
everything the run would start next — remaining sessions, the generated
`--review-harness` review, and review repair children — surfacing as
`reason: stopped` with `ok: true` plus `completed` and `pending` counts — an
intentional clean early stop, not a pause and not a failure. This holds whether
the helper launched the run itself or **attached** to a run already in flight:
in the attach case it reads the stop back from the finished run's persisted
state, so it never relaunches over an honored request. Do **not** auto-resume
it; re-running the same command later continues from the remaining sessions, or
runs the pending review when every session is already complete. See Step 4.

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
The helper auto-retries recoverable same-session pauses (`verify-failed`, `transport-failed`, `timeout`, `no-status-change`) up to twice per session and reason. Only `transport-failed` backs off before relaunching (5 seconds, then 30 seconds); the other recoverable pauses retry immediately. An unclassified `nonzero-exit` pause is never retried. When a pause genuinely needs an answer, the result carries `reason: needs_agent_reply` with a `questions` block holding the resolver prompt, the paused agent's last message, the pause metadata, and artifact paths.

Choose the closure conservatively:

- Resolve `continue` autonomously only when the worker explicitly labels the
  message nonblocking or its informational nature is plain. Do not use a fake
  reply for an answerless continuation.
- Send a factual reply only when it is clearly defensible from the plan, the
  repository, and the run artifacts referenced in the `questions` block. Read
  those artifacts before deciding.
- Relay ambiguous decisions to the user and wait for their explicit answer when
  intent, a trade-off, or information outside the plan/repository/artifacts is
  required.
- Resolve `pause` only when the live wait should deliberately become ordinary
  paused-run recovery instead of continuing in place.

After a terminal `needs_agent_reply` stop, resume by streaming the chosen reply
on stdin. The helper launches `asl run --reply-stdin`; the reply never appears
in argv:

```text
printf '%s' '<reply text>' | <skill-dir>/scripts/asl-autopilot.py --command '<same exact command>' --supervision-id <new-launch-identity> --json --reply-stdin
```

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
<skill-dir>/scripts/asl-autopilot.py --command '<exact asl run command>' --supervision-id <launch-identity> --json --wait-quota-reset
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
`idle_ceiling_exceeded`). Both rest on the runner's process-tree CPU sampling:
it raises neither suspicion class while the tree is burning CPU, and the idle
ceiling stands down whenever the live `hang_cpu_progress` sample reports
`progressing: true` — a worker piping through a buffering consumer is silent,
not stalled. An unknown sample (no fresh `ps`) still escalates.
When either bar fires, the helper stops with `reason: worker_hung`
**without killing or waiting on anything** — the supervised `asl run` child
stays alive and keeps the plan lock. Its terminal `supervision-end` record
therefore marks the end of supervision only: that live child can still publish
mailbox questions and notices after the record lands.

Read `result.hang` first. It carries the `class`, the runner's `hang_suspected`
block verbatim (including its sampled `process_tree`), `idle_seconds`,
`probes`, the plan/state/events paths, and the active turn's artifact paths;
`supervised_child_pid` and `detached` appear only when the helper launched the
chunk itself. Then:

1. **Re-verify before trusting the snapshot** — pids get reused, so match on
   command strings, not pids: compare
   `ps -axo pid,ppid,state,time,command` against
   `hang.hang_suspected.process_tree`.
2. **Ask the user before killing anything.**
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
   the same plan.

Registered review is one combined read-only review across the ordered registry.
Every finding, evidence path, and completion check is repository-attributed;
repair children read the full registry but write only finding-derived aliases,
and every review or repair turn passes the same workspace mutation guard. Keep
aliases on findings and report qualified paths—never collapse the review to one
repository or treat a mutation from the read-only review harness as a finding.

### Step 4: Report

Summarize the outcome: which run was supervised, whether it completed or paused,
and any blocker that needs the user's attention.

When the supervised command set `--max-sessions N` and the budget is spent with
sessions still pending, the helper returns `reason: max-sessions` with `ok: true`,
plus `completed` and `pending` counts. This is an **intentional, clean early
stop** — not a pause and not a failure. Report the progress (e.g. "completed N of
M sessions; K pending") and do **not** auto-resume, reply, or run `asl questions`;
re-running the same command later continues from the remaining sessions.

The same holds for `reason: stopped`, which the helper returns when the runner
honored an out-of-band `asl control stop-after-current` request (see *Request a
graceful stop mid-run* under Step 1). It too carries `ok: true` with `completed`
and `pending` counts and is an **intentional, clean early stop** — never report
it as a pause,
a failure, or `blocked`, and never auto-resume it. Report the progress and say
that re-running the same command continues from the remaining sessions, or runs
the pending review when the stop landed after the last session.

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
  deprecated alias). Corrected artifacts and higher caps resume the same plan;
  this skill never creates follow-up fix plans.
- Preserve registered workspace semantics across launch, attach, replies,
  combined review, and recovery: use the bound workspace root, retain ordered
  repository aliases and qualified paths, never rebind automatically, and
  preserve partial commit SHAs.
- `asl outbox wait` is the supervision loop; tailing the helper's background
  output is the fallback for when the mailbox is unavailable.
- Keep the mailbox discipline: continue only plainly nonblocking messages,
  reply only from clearly defensible plan/repository/artifact facts, relay
  user-owned ambiguity, and use pause only to enter ordinary paused recovery.
- Mailbox failure is fail-open for supervision: continue owning the child and
  let the runner use the ordinary pause/resume flow. Never invent a mailbox
  reply, truncate the append-only log, delete a lease, or add Workbench
  mailbox/model-control endpoints.
