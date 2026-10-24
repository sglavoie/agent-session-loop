<!-- Continuation of the previous part. -->

A reopened review is scoped against the tree its *source* review read. The
newest repair evidence is either a `repairs-materialized` cycle or a
materialized `review_repair_reopened` record, and a reopen supersedes the cycle
it descends from — so the reopen carries the same `repo_head`, `worktree_dirty`,
`dirty_paths`, and (for a registered workspace) `repositories` base vector,
copied at write time from that source cycle's own record. Reopen records written
before this copy existed carry none of them; the runner recovers those fields in
memory when it reads such a record, from the source cycle's still-durable event,
and never rewrites the historical record itself. Both the copy and the recovery
are attribution-only and fail closed: the cycle has to record the same `cycle`
number and `review_session_id` the reopen names — an absent field is not
agreement — and any cycle whose own `review_output_sha256` or
`review_artifact_sha256` disagrees with the reopen's `source_*` digests
identifies a different review and abandons the lookup entirely. Identity-
matching duplicate cycles are accepted only while they agree about the baseline.
When no cycle can be attributed — including the crash window where children
became durable and the cycle event never did — the fields stay absent and the
rereview falls back to `scope: full` under `fallback_reason: no-recorded-base`.
Recovery never overwrites a baseline field the reopen already carries, and the
`current_findings` and `current_repositories` a reopen records still supersede
the source review's own.

For registered rereviews, the cycle records an ordered `repositories` base
vector instead of collapsing the workspace to one `repo_head`. Delta selection
requires a resolvable ancestor base and complete dirty-path evidence for every
declared repository. The rendered combined scope provides explicit `git -C`
commands per repository and alias-qualified changed paths; failure in any
repository widens the whole rereview to full scope. Reopen records preserve the
same ordered vector, qualified findings, and repair-group scopes. If a crash
loses the cycle event after children were authored, continuation reconstructs
the vector from the generated review session and each group scope from the
generated children, so staged repairs, restored dirty paths, and eventless
rereviews do not collapse repository namespaces.

A pending review session's scope is a static list, but the repository keeps
moving — a crash and resume, or an in-run reuse, can leave a delta scope that
predates files the reviewer must now inspect, and untracked additions never
show up in `git diff <base_sha>` at all. So immediately before a pending
generated review session executes, the runner refreshes an older marker and
Completion Contract to the current replay recipe, then re-derives its scope
from current Git evidence and rewrites the `## Delta Review Scope` section in
place, widening to full scope whenever a narrower one can no longer be proven.
The refresh preserves the session id, pending handoff, prior findings, and
ordered repository review base. A session with no scope section is already
full-scope and is left untouched, so first reviews cost no extra Git probes. A
scope section the runner cannot parse fails closed with a `malformed-plan`
pause rather than executing against stale bounds.

An all-complete plan is not necessarily inert. A later run with a higher
absolute cap automatically reopens `cycles-disabled` or
`cycle-cap-exhausted` findings. A prior `malformed-review-repair` becomes
eligible after `final-review.json` is externally corrected into a safe,
internally consistent v2 artifact; changed findings or grouping are accepted
and audited through new fingerprints and Markdown/JSON digests. ASL never
rewrites that artifact, and manually changing its outcome to `all-clear` does
not complete the plan—the generated rereview session is the only all-clear
authority. Existing generated repair children are authoritative after a crash:
the runner executes pending children or appends a missing rereview, but never
recreates them.

Generated repair children use a mandatory runner-owned `per-child` commit
policy even when ordinary `commit_mode` is null. Before each child launches,
ASL requires a clean set of that child's writable repositories;
failure pauses as `commit-preflight` with repository-qualified dirty paths and
spends no harness turn. After verification, ASL commits only dirty aliases
attributed by the write guard, in registry order. A no-change child or a child
whose worker already committed is clean and produces no runner commit. Partial
multi-repository failures retain the existing durable commit-group recovery
path. State, run config, and events record `repair_commit_mode: "per-child"` so
the supervisor can distinguish these attempts from unauthorized commits under
a null ordinary mode.

The loop stops on an all-clear rereview, an exhausted repair group, repeated
finding fingerprints, unstructured findings, operator stop requests, or an
unchanged insufficient cap. Repeated, exhausted-group, and unstructured
findings are terminal circuit breakers. Every
review completion appends a `review_cycle` event and a `review_cycles[]` state
entry recording the cycle number, review session id, outcome,
introduced/closed/repeated fingerprints, repair session ids, disposition, and
full artifact digests (digest fields are nullable for legacy records).
Registered records additionally retain the qualified structured findings,
ordered repository bases, and finding-derived repair-group read/write scopes.
A reopen attempt appends `review_repair_reopened`; successful records preserve
source lineage, previous/current fingerprints and digests, exact child IDs,
repository evidence, group attribution, and before/after cap accounting. The
run payload's `review_fix_disposition` keeps
the compatible disposition vocabulary (`all-clear`, `repairs-materialized`,
`cycles-disabled`, `cycle-cap-exhausted`, `repair-group-exhausted`,
`findings-repeated`,
`unstructured-findings`, `malformed-review-repair`, or `not-loop-ready`).
Cycle and continuation state are reconstructed from append-only events plus
generated sessions, so caps, idempotency, and rereview scheduling survive
pauses, crashes, and later processes.

A review turn that produces artifacts failing verdict validation is not a
human's problem on the first try. The runner reverts the review session to
pending with the exact validation error as a handoff note, appends a
`review_verdict_retry` event (run id, session, attempt, max attempts,
fingerprint, failure reason, error), and re-runs the same review session in the
same run — the retry packet carries the prior error as repair references, so
the reviewer sees precisely what to fix. The budget is two retries, counted
only from records appended after the latest `review_cycle` event, so every
reopened cycle starts fresh. An error that repeats (same reason and error text,
same fingerprint) is treated as exhausted immediately rather than burning the
remaining attempt. Exhaustion keeps the existing `verify-failed` pause — no new
pause reason — and adds `review_verdict_fingerprint`,
`review_verdict_repair_attempt`, `review_verdict_repair_max_attempts`, and
`review_verdict_repair_exhausted: true` to the pause details; the autopilot
treats that key like `verification_repair_exhausted` and stops relaunching. A
pending stop-after-current request wins over a retry: the run pauses instead,
recording `review_verdict_repair_declined: "stop-after-current"` with
`review_verdict_repair_exhausted: false` so the record does not claim a budget
was spent. The mutation guard runs on every retry turn against a fresh
pre-turn fingerprint, and scope refresh rewrites only the
`## Delta Review Scope` section, so the rejection notes survive.

`preflight` inspects the plan without invoking a harness and reports every
finding needed to clear the loop-ready gate: missing readiness lines, contract
schema problems, manifest/session drift, environment prerequisites, and
harness/commit-mode prerequisites when `--cwd`, `--harness`, or a commit-mode
flag is supplied. The turn-budget check defaults to allowing 24 pending
sessions; 25 or more pending sessions emits yellow `env-turn-budget`. Pass
`--max-pending-sessions N` to `asl preflight` or `asl run` when a larger serial
plan is intentional and should still pass the loop-ready gate. Its repair hint
names that flag, `asl run --max-sessions N` to bound the run inside the budget,
and completing or closing pending sessions. It deliberately does not suggest
resplitting: a resplit replaces one pending parent with several pending
children, which raises the count the finding is about. JSON output
carries a stable shape with `findings`, `findings_by_severity`, `repair_hints`,
`readiness`, `expected_readiness_sha256`, `max_pending_sessions`,
`mark_loop_ready_requested`, and `marked_loop_ready` fields. JSON also always
includes `auto_repairs`; it is an empty list unless `--mark-loop-ready`
performed a derived repair.
The stable finding categories are `plan-structure`, `canonical-session-id`,
`manifest-readiness`, `missing-contract`, `malformed-contract`,
`ambiguity-sentinel`, `path-reference`, `verification-scope`,
`verification-duplicate`, `command-not-runnable`,
`progress-journal-missing`, `baseline-missing`, `baseline-stale`,
`baseline-unexpected`, `baseline-timeout`, `baseline-near-timeout`,
`workspace-root`, `repository-registry`, `repository-root`,
`repository-origin`, `repository-overlap`, `repository-scope`,
`repository-contract`, `repository-footprint`,
`repository-registry-required`, `env-repo-root`, `env-git`,
`env-harness`, `env-lock`, `env-amx`, and `env-turn-budget`.
Consumers should branch on these
category values and the paired repair hint rather than parsing message prose.

`repository-footprint` is red-only and applies to legacy plans: it fires when a
contract or handoff target that some Git repository owns resolves outside the
bound root, or when the plan's targets resolve into more than one Git
repository. An absolute path that stays inside the bound root is accepted, so
plans predating repo-relative authoring keep passing. A target that escapes the
bound root and belongs to *no* Git worktree is classified as `external` instead
and never forces a registry: a registry row can only describe a Git repository,
so refusing such a plan would demand a declaration nobody could write. A plan
naming an ASL plan-store artifact or any other non-Git path therefore needs no
repair and no reworded prose. The finding is derived from the plan's stored `cwd` when
`plan-metadata.json` is present, so `asl preflight PLAN_DIR --json` reports it
without `--cwd`. It is one of two non-bypassable categories: `asl run
--allow-not-loop-ready` still pauses with `pause_reason: not-loop-ready` and
the category listed in `pause_details.blocker`, and `verification` refuses
the escaping check at run time even if the gate is skipped. The JSON payload
carries a `repository_footprint` object (described under JSON Output) whose
`suggested_registry_table` is the exact `## Repositories` block the migration
writes. The fix is `asl register-repositories`, not a widened `--cwd`; a plan
that escapes its bound root while staying inside one repository is the one case
the migration answers `not-required`, and its escaping target has to be
rewritten instead.

`repository-registry-required` is the second non-bypassable category. It is
red-only, applies to legacy plans, and fires when the plan *intends* to reach
more than one Git repository — including contract targets no session has
created yet, which `repository-footprint` structurally cannot see because it
only counts paths that already exist. Candidates are collected from session
prose, `context.md`, `manifest.md`, parsed contracts, and stored plan
metadata, resolved through symlinks to the nearest existing ancestor, and
anchored to the plan's stored `cwd` or an explicit `--cwd`. The document a plan
was split from is excluded, and so is the directory holding it. `manifest.md`
records that document in a `**Split from**:` line; the gate resolves that
pointer to a path and exempts it, rather than filtering the line it appears on,
so the source document stops counting *wherever it is named* — the metadata
field, session prose, an expansion note, a handoff, or an artifact
`asl expand-ingest` regenerates. Exempting the parent directory as well means
sibling plans in the same store, routine in multi-plan groups, do not trip the
gate either. The split and iterate skills refuse to modify the source, which is
what makes the whole exemption safe.

Two guards bound it. A pointer that names a directory exempts nothing: its
parent is meaningless and the subtree would be unbounded. The parent directory
is exempted only when it sits strictly inside a Git repository — never when it
is that repository's root, `$HOME`, the filesystem root, or a directory in no
repository at all — so a source plan at a repository root cannot disable the
gate for that repository, and one kept in a plain directory cannot exempt every
repository beneath it. A source path that no longer exists is still exempt,
which is deliberate: resolution would otherwise walk it up to its nearest
existing ancestor and name that ancestor's repository. Everything else in the
source repository still requires a registry, and the report labels these
candidates `plan-provenance` rather than `plan-artifact` so a wrongly exempted
path stays distinguishable from runner bookkeeping.

What this deliberately hides is a plan whose source document lives in another
repository *and* which intends to rewrite that document, or a sibling beside
it, in place. Such a plan reads as single-repository here. The skills' refusal
to modify the source is the reason that trade is acceptable; a plan that really
must edit its own source needs a registry declaring that repository. Its repair hint
(`repository-registry-required.declare`) names `asl register-repositories` and
the three declarations that command derives from disk: a top-level
`## Repositories` table, per-session `**Reads repositories**:` and
`**Writes repositories**:` scopes, and a `repo` alias plus repository-relative
`path`/`cwd` on every contract check. The JSON payload carries a bounded,
credential-redacted `repository_intent` object whose `git_roots` names the
repositories to register. `--mark-loop-ready` cannot silence it —
`_mark_loop_ready` refuses outright while any non-bypassable category is red —
but the category is no longer outside auto-repair. When preflight was given a
`--cwd` and the same read-only derivation `asl register-repositories` runs
returns a migration for the plan, `auto_repair_plan` leads with two steps:

```text
asl register-repositories <plan-dir> --cwd <bound-root> --json
asl prepare <plan-dir> --resume --rebind-workspace --cwd <workspace-root>
```

Both carry `--state-dir` when preflight was given one, so the migration
inspects the lock preflight inspected. `suggested_pause` is then `None`,
because a skill driving the plan clears the blocker mechanically. Both steps
are withheld — and the pause stands — whenever that derivation does not
return a migration, so a single-repository escape (a `repository-footprint`
finding with one Git root) still pauses with no step: `asl register-repositories`
would answer `not-required` there and the rebind would fail with
`--rebind-workspace requires a registered plan`. A derivation that refuses is
withheld the same way and additionally names its `missing_fact` in the pause
detail, because advertising a step the command has already decided to refuse —
for an unobservable `origin`, a contract check under no registered repository,
or a completed handoff a registry would invalidate — would advertise a repair
that cannot land. `asl run` applies the same migration
itself at its intent gate, on that same admissibility rule rather than one of
its own, then still pauses once for the rebind. `asl prepare` refuses the same plan before
publishing any change with exit reason `repository-registry-required`, and
`asl expand-ingest` emits the same reason with the `repository_intent`
payload. Never resolve the condition by selecting one child checkout as the
root. The runner re-derives the same verdict independently, so the invariant
holds even when preflight never runs — see the repository hard gate under
Command Behavior.
For a registered plan, `--cwd` is the non-Git-capable common workspace root.
Preflight resolves the ordered registry once; validates child existence,
directory type, exact Git top-level, origin, disjointness, scope, and contract
locations; captures every registered repository fail-closed; and reuses the
resolved repository context for command and baseline checks. Per-session commit
mode inspects the current session’s read and write trees but requires only
its write trees to be clean — a dirty read-only tree is reported green, because
the session's commit stages write aliases only and the write guard judges the
turn's own before/after delta; at-end commits require every registered tree to
be clean. Registered JSON adds
`workspace_root`, `registry_digest`, and ordered `repositories` summaries;
text output prints the same ordered summaries. Legacy plans retain `Repo root`
text and omit those additive JSON fields.
`--mark-loop-ready` force-backfills stale or missing `progress.md` when
`progress-journal-missing` is the only non-readiness blocker, then flips
`**Readiness**` to `loop-ready` and re-stamps the SHA and timestamp when the
only remaining blocking findings are manifest-readiness findings. It refuses
when any other non-readiness red or yellow finding remains.

The readiness hash includes session files and expansion notes. Legacy
`parallel-validation.md`, `## Parallel Execution` manifest blocks, and
`## Parallel Metadata` session blocks are excluded entirely.

`verify` reads a session's `## Completion Contract` and executes its `### Verify`
recipe against `--cwd` (defaults to the current directory). Exit code is the
gate decision: 0 when every authoritative gate passes, non-zero otherwise.
Conditional diagnostics run only after a named gate failure, so they enrich a
failure without becoming an additional gate. JSON output includes `passed`,
`recipe`, executed `checks`, `skipped_checks`, `diagnostic_failures`, and a
stable `reason` (`ok`,
`verify-failed`, `no-contract`, `malformed-contract`, `cwd-error`, or
`invalid-session`). Per-check rows include stdout/stderr snippets and a stable
`failure_reason` enum.
`--commit-at-end` runs the same grouped coordination after all pending sessions
finish successfully in that runner invocation. In registered mode it selects
all currently changed declared repositories; a clean repository is omitted.
`--commit-at-end` cannot be combined
with `--max-sessions` unless `--max-sessions` is high enough to complete every
session that was pending at the start of the run. If text TTY run controls stop
the run before all pending sessions finish, `--commit-at-end` does not run.

`--note TEXT` stores display-only reference text on the run payload. The note is
not sent to the harness and does not change plan execution. Text runs print it
in the compact `s` status snapshot, final completion or pause output, and
Ctrl+C interrupted exits. JSON/state output preserves it as top-level `note`.

For normal text TTY runs, `run` also listens for keyboard controls:

- `p`: toggle stop-after-current. When active, the current session still
  finishes and any `--commit-per-session` commit for that session runs; the run
  then exits 0 with `ok: true`, `paused: false`, and `reason: stopped`. The stop
  covers every kind of work the loop would start next: remaining sessions, the
  generated `--review-harness` session, and review repair children. Press `p`
  again before the session finishes to cancel the request.
- `h`: print a timestamped help line with available keys and current control
  state.
- `s`: print a compact live status snapshot to stderr. The snapshot includes
  the active or next session, session counts, active and future harness, model
  and effort metadata, control state, and current artifact paths. It does not
  mutate `state.json` or append a `run_control` event.
- `t`: toggle inline live tailing for the active text run. When enabled,
  inline tailing first prints the recent active-session stdout/stderr context
  (up to 40 prefixed log lines, re-replayed on each enable) and then streams
  only new log lines while enabled. It does not emit event or progress lines;
  normal runner progress remains the single source for heartbeats, session
  start/finish, control, status, and commit lines.
- `1`, `2`, `3`: select `codex`, `claude`, or `copilot` as the sticky harness
  for future turns in the same runner invocation. The active subprocess is never
  interrupted.

Keyboard controls are disabled for `run --json`, `run --dry-run`, and runs
where stdin or stderr is not a TTY.

`asl control` is the programmatic equivalent of those keys, so a supervising
agent driving `run --json` can still request them. Each subcommand appends one
command to the same durable per-plan control queue the Workbench HTTP API feeds
(`control-queue.jsonl`); the runner drains it at its lifecycle checkpoints and
applies the identical transition the keyboard listener emits. Nothing else is
mutated — the runner alone owns `state.json`.

- `asl control stop-after-current PLAN_DIR_OR_ID` queues the `p` request. Its
  payload reports `enqueued`, `command_id`, `run_active`,
  `pending_after_current` (sessions still pending once the executing one
  finishes), and `review_continuation_pending` (the run carries a review harness
  and its `review_resume` projection is not `terminal`), plus a `note`: `will
  stop after current session`, `current session is the last; stop also prevents
  the pending review`, `current session is the last; stop is a no-op` when
  nothing would follow, or `no active run; command applies when a runner next
  drains the queue`.
- `asl control abort PLAN_DIR_OR_ID [--grace SECONDS] [--reason TEXT]` queues a
  request to end the in-flight turn after a bounded grace window (default
  `120` seconds). Its payload reports `enqueued`, `command_id`, `run_active`,
  `turn_active`, `grace_seconds`, `reason`, and a `note`: `a turn is active;
  abort will end it`, `no turn active; the run is finalizing, so abort cancels
  its terminal audit`, `no turn active; abort degrades to stop-after-current`,
  or `no active run; command applies when a runner next drains the queue`. The
  grace window is ignored by a finalizing run, which arms at once. A grace
  outside 1–86400 seconds fails with `reason: invalid-grace` and an empty
  `--reason` fails with `reason: invalid-reason`, both before anything is
  enqueued — an out-of-range window is rejected, never clamped.
- `asl control select-harness PLAN_DIR_OR_ID --harness H` queues the `1`/`2`/`3`
  request. Its payload reports `enqueued`, `command_id`, `harness`,
  `run_active`, and a `note` naming the harness future sessions will use. An
  unknown harness fails with `reason: invalid-harness` before anything is
  enqueued.
- `asl control set-model PLAN_DIR_OR_ID` queues process-local model and effort
  overrides for a future implementation turn. Its payload reports `enqueued`,
  `command_id`, the optional target `harness`, the model and effort set/clear
  operations, `run_active`, and `applies_to: next-implementation-turn`. At least
  one set/clear operation is required; omitting all four fails with
  `reason: missing-model-control-operation` before anything is enqueued. An
  unknown target harness fails with `reason: invalid-harness`.
- `asl control guard add PLAN_DIR_OR_ID --deny-command "git push origin main"`
  queues a run-scoped command guard, and `--protect-path api:config.toml`
  queues a path that must end each turn unchanged;
  `asl control guard remove --id GUARD_ID`
  ends one; `asl control guard list` prints the active set without touching the
  queue. See [Run-scoped guards](09-run-scoped-guards.md#run-scoped-guards).

Enqueueing succeeds for a readable plan even with no runner attached; the
command simply waits in the queue for the next drain. All four subcommands
accept the same `PLAN_DIR_OR_ID` compact-label resolution as `status` and reuse
its `ambiguous-plan` / `malformed-plan` / `state-error` failure reasons. A queue
the process cannot create or append to — an unwritable or non-directory state
root — fails with `reason: os-error` and a `details.queue_path`, still as exactly
one JSON object under `--json` (a single stderr line otherwise), never a
traceback. Any other command rejected by the durable queue fails with
`reason: invalid-command`. The control-handler reason vocabulary is
`abort-timeout`, `finalization-timeout`, `invalid-command`, `invalid-grace`,
`invalid-harness`, `invalid-reason`, `invalid-timeout`,
`missing-model-control-operation`, `no-runner`, `os-error`, and
`receipt-timeout`, plus the guard reasons listed under
[Run-scoped guards](09-run-scoped-guards.md#run-scoped-guards).

Every producer that appends to the queue — the `asl control` subcommands and
the Workbench control endpoints alike — then sends a best-effort wakeup so the
runner drains the queue immediately instead of waiting out its `5.0`-second
control poll. The wakeup is a single byte written to the FIFO recorded as
`wakeup_endpoint` in the plan's `lock.json`, and its outcome is reported as a
`signal` object carrying `delivered` and a `reason`: `delivered` when the byte
was written, `no-runner` when no readable lock exists, `remote-host` when the
lock records a different hostname, `stale-lock` when the recorded PID is no
longer running, `no-endpoint` when the lock names no FIFO at all,
`endpoint-closed` when the FIFO has no reader behind it, plus `endpoint-failed`
when the path is not a FIFO or the write was refused for any other reason.
The field is named `signal` for the older transport it replaced and is kept for
compatibility; nothing is signalled. The wakeup is a latency optimization
layered on the durable queue, never a delivery guarantee: a command whose wakeup
did not land is still applied at the next poll. Because a FIFO is a local
filesystem object, the wakeup is **same-host only** — a runner whose lock
records another hostname reports `remote-host` and picks the command up on its
ordinary poll cadence instead. Nothing about the queue is host-bound; only the
hurry-up is.

The endpoint is *owned* rather than addressed, and that is the whole of its
safety. The runner creates the FIFO and opens it for reading **before** it
publishes the `lock.json` that names it, so which process receives a wakeup is
decided by who holds the read end, not by a PID a producer looks up afterwards.
There is no window in which the recorded address comes to mean a different
process, because a PID is never the address. A producer therefore authenticates
nothing: it reads the lock for the endpoint path and writes to it.

Writing to a FIFO nobody reads cannot act on any process. The producer opens it
`O_WRONLY | O_NONBLOCK`, which fails with `ENXIO` when there is no reader, so an
endpoint left behind by a runner that died reports `endpoint-closed` and the
command waits for the poll. The worst case is the opposite of a misdirected
signal: a spurious drain by a process that legitimately holds the read end,
which costs nothing because the byte carries no payload — the durable queue
carries the command, the wakeup carries only the fact that one is waiting. A
pipe already full of unread bytes is reported as `delivered` for the same
reason: a wakeup is already queued there.

On the receiving side, the runner installs no signal disposition at all, so
there is none to shield and none to restore. A dedicated reader thread does
nothing but set the event the poll thread is blocked on; the main thread is
never interrupted. The endpoint is unlinked as the run exits — after the lock
is released — so a producer that resolved the path while the lock existed and
writes once it is gone finds nothing to open rather than a process to disturb.
A lock written before this field existed, or by a runner whose filesystem
cannot host a FIFO, simply carries no `wakeup_endpoint`: that run reports
`no-endpoint` to producers and applies control commands at its ordinary poll.

A redirect — a correction aimed at the turn that is running *right now* — uses
a second, dedicated endpoint rather than this one. `redirect-wakeup.fifo` is
created beside `control-wakeup.fifo`, opened for reading before the same
`lock.json` is published, and recorded there as `redirect_endpoint`; the lock
payload moved to `version: 4` to carry it, and like `wakeup_endpoint` the field
is informational — no reader validates it. The separation is deliberate and it
inverts the relationship described above. For a stop or an abort the wakeup is
a latency optimization over a durable queue that would apply the command a poll
later anyway; for a redirect the `5.0`-second poll is already too late, so the
urgent endpoint **is** the correctness path and ordinary queue draining is not
a fallback for it. Delivery is reported with the same seven reasons the control
wakeup reports, because the transport is byte-for-byte the same and a parallel
vocabulary for the same seven facts would only be a second thing to keep in
sync. A lock naming no `redirect_endpoint` — written before the field existed,
or by a runner whose filesystem cannot host a FIFO — makes the plan refuse
redirects outright with `urgent-channel-unavailable`, rather than deferring
them to a poll that would deliver them after the turn they were meant to steer.

What a redirect producer writes before it signals is `redirect-journal.jsonl`,
an append-only 4 MiB-capped ledger in the same plan hash directory, with a
512 KiB tail reserved so a journal too full to accept a new submission is never
too full to record the settlement that closes an existing one. Each physical
line is a versioned record of one of nine kinds, and they divide into two
groups. Four are settlements — `submitted`, `accepted`, `rejected`, `expired` —
and a request's state is a reduction over those records rather than a field
anything rewrites, so no line is edited in place and the file is never
truncated. Five are annotations — `continued`, `launching`, `delivered`,
`released`, `acknowledged` — which say which continuation attempt bound an
accepted correction into its prompt, that the attempt was past the last instant
at which it could still promise the prompt had not gone out, whether it actually
submitted that prompt to a worker, whether a carry whose prompt never went out
was given back, and that a worker receipted it. An annotation changes no state,
so `submitted` remains the only non-terminal one and a carried, receipted
correction is still settled exactly once. All five are charged against the
settlement reserve rather than the submission tier: refusing one would leave an
accepted correction with no durable record of which attempt carried it, which is
the only thing a receipt can be checked against.

Four of those annotations exist because planned delivery and actual delivery
are different facts, and a correction can be lost in the gap between them.
Everything from finalizing a prompt to putting it in front of a worker can
still refuse — the prompt build, the conversation selection, an authorized
pause, and then the transport's own handshake, where a provider can decline to
restore the conversation a live process has just asked for — and a journal that
recorded only the carry would say the words had been given to an attempt that
never spoke them. `continued` is therefore the undertaking and `delivered` the
submission: the prompt on the argv of a one-shot turn, the answered
`turn/start`, the accepted `session/prompt`, the stream-JSON message that was
written — never the spawn, which proves only that a process exists. `launching`
cuts that gap in two, because its two halves are not equally answerable: it is
appended at the last instant before the prompt can leave the process, so a carry
without it cannot have been submitted, while a carry with it and no `delivered`
after it is a question the file alone cannot answer — the writer either never
got the prompt out or got it out and died before saying so. `released` is the
undo an attempt owes the operator when it gives up in between: it clears the
carry, and the launch declaration with it, so the request is accepted and
uncarried again and the next attempt that names it may take it. The asymmetry is
what makes releasing safe to call from any unwind path — a correction that
`reached_a_worker`, meaning delivered or acknowledged, may never be released,
because re-offering words a worker was already shown would present one worker's
correction to a second as if it were new. A release inside a declared launch
window is still permitted, and is the only way that window closes without a
delivery: it is a live attempt reporting a refusal it observed, which is
evidence, where a dead attempt's silence is not. At most one `launching`, one
`delivered` and one `acknowledged` exist per carry; `continued` and `released`
may alternate, and each pairing is one attempt's whole story.
