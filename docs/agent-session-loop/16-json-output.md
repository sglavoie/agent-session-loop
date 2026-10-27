<!-- Continuation of the previous part. -->

While a harness turn is active, `state.json` also includes `active_turn` with
the run id, turn index, session ID, harness, prompt, model and effort metadata,
artifact paths, elapsed and idle seconds, last activity metadata, heartbeat
count, and any best-effort action-required signal. While the runner is
executing a post-turn `## Completion Contract` `### Verify` recipe, `state.json`
also includes `active_verification` with the run id, turn index, session ID,
total `check_count`, the declared `check_names` and `check_kinds`,
`current_check_index`, `current_check_name`, `current_check_kind`,
`started_at`, `current_check_started_at`, `elapsed_seconds`,
`check_elapsed_seconds`, and `heartbeat_count`. `active_turn` is cleared before
`active_verification` is populated, so the two phases are mutually exclusive;
both are `null` between turns and after `run_finished`. During the bounded
nonterminal answer window, additive `waiting_for_reply` records only the
question/session/turn identity, start time, base deadline,
`unsupervised_deadline_at` with its `unsupervised_ceiling_seconds` span, and the
last observed lease activity. `unsupervised_deadline_at` is the ceiling as of
the last anchor the runner persisted — the start of the wait, or the instant it
woke from a system suspend — and a reader rolls it forward from
`last_lease_activity_at` plus the ceiling span rather than reading it as a
terminal instant. Waits persisted before the ceiling began rolling carry a
static `hard_deadline` instead of those two keys, and every reader still renders
that key for them. `events.jsonl` records
`run_started`, `preflight_skipped`, `preflight_auto_repaired`,
`conversation_continuation`, `redirect_continuation`,
`turn_started`, `turn_heartbeat`, `action_required`,
`turn_hang_suspected`, `turn_hang_cleared`, `harness_degraded`,
`turn_execution_finished`, `turn_handoff_correction`, `turn_rejected`,
`session_reopened`, `turn_finished`,
`verification_started`, `verification_check_started`, `verification_heartbeat`,
`verification_check_finished`, `verification_check_skipped`,
`verification_finished`, `commit_started`,
`commit_finished`, `commit_group_started`, `commit_group_updated`,
`commit_group_finished`, `paused_work_committed`, `repair_commit_preflight`,
`workspace_write_scope_repair`, `run_control`, `review_scope_selected`,
`review_session_created`, `review_cycle`, `review_verdict_retry`,
`review_repair_reopened`, `mailbox`, `reply_provided`,
`resolution_consumed`, `process_sweep`, `agent_mux_delivery`,
`finalization_started`, `finalization_heartbeat`, `finalization_finished`,
`run_paused`, and
`run_finished` events as they happen. `process_sweep` is conditional: it is
appended only when a post-turn or post-check sweep actually signalled a
surviving process (see **Process sweep** above). Outside a run,
`asl accept-ref-updates --accept` appends `workspace_ref_updates_accepted`,
`asl acknowledge-external-changes --accept` appends
`workspace_external_changes_acknowledged`, and `asl migrate-state --apply`
appends `state_migrated`.
The less self-describing names, one line each:

- `preflight_auto_repaired`: the startup readiness gate rewrote a missing
  progress journal or the manifest readiness line and reran preflight; the
  payload is the repair's action, category, and path.
- `turn_hang_suspected` / `turn_hang_cleared`: the live hang assessment
  crossed into, or retracted, a suspected hang for the active turn.
- `harness_degraded`: the harness lost an auxiliary transport on the way up
  and ran the turn anyway; it carries no pause or failure reason.
- `commit_group_started` / `commit_group_updated` / `commit_group_finished`:
  a commit group's `phase` (`started`, an intermediate phase, `complete`),
  with the `group` record also upserted into run state.
- `repair_commit_preflight`: a generated review-repair child passed its commit
  preflight before launch (a failure pauses `commit-preflight` instead).
- `workspace_write_scope_repair`: a retained out-of-scope change passed its
  repair check (fresh expansion or restored), so the session may run.
- `conversation_continuation`: once per turn, before the harness starts, which
  conversation the turn runs on and why, including "open a new one".
- `redirect_continuation`: an accepted mid-turn correction became the next
  attempt of the same session, was dropped because selection moved on, or was
  consumed; it names operation ids, never the feedback.
- `review_verdict_retry`: a review verdict failed validation and the same
  review session is re-run in-run instead of pausing.
- `workspace_ref_updates_accepted` / `workspace_external_changes_acknowledged`:
  an operator accepted a previewed remote-ref update digest, or acknowledged
  checkpointed external changes, for the next pending session.
- `state_migrated`: the state directory was relocated, or `state.json` was
  upgraded in place to the current schema version.
`turn_execution_finished` states that the harness process ended and nothing
more: exit code, the timed-out/interrupted/aborted flags, the turn's timings
and artifact paths, and `plan_readable` for whether the plan still parsed
afterwards. It is appended after `result.json` has been written and fsynced,
so the durable record of a turn ending can never name artifacts that are not
on disk, and it stays separate from `turn_finished`, which additionally means
the turn was accepted.
`turn_finished` is emitted from exactly one function,
`turn_execution_commit.commit_turn` — reached through `TurnExecutor.commit` —
and only after every post-execution guard has passed: the workspace and review
mutation guards, the mailbox completion guard, session verification, the
progress journal append, the derived status ledger sync, and any per-session
commit. The committed payload names a snapshot re-read once at that point, so
the digest it publishes is the plan as it stands after the runner's own
bookkeeping — the readiness refresh rewrites `manifest.md` every turn — rather
than the plan as it stood when the guards ran. Session statuses must not have
moved in that window: the guards passed on the accepted snapshot, so drift is
refused as a `ledger-sync` rejection with an `unexpected-status-change` pause
instead of being published. `result.json` and `state.json` are both written
before the event names them, so a reader that has the event always has the
artifacts it points at. Progress output and the AgentMux `turn-finish`
notification follow the durable event rather than preceding it, which is why an AgentMux
stream shows `turn-finish` after `verification-finish` — verification is a
guard that can still refuse the turn. The committed payload is returned as a
read-only mapping and is never rewritten afterwards.
`paused_work_committed` is the settlement's own record, appended once when a
recoverable pause under an active commit mode settles the work its turn
retained. It is later than `run_paused` by construction — the commit must run
after the session has been reverted to `pending`, and events are append-only —
so a reader that wants the outcome beside the pause reads it from state rather
than from the pause event. Its payload is the `run_id`, the `session_id`, and
the `paused_work_commit` record verbatim, which is the same object written to
the turn's `result.json`, to the run payload, and into `pause_details`. Like
`process_sweep` it is conditional: a pause that owes no settlement — a review
session, a non-recoverable reason, or a run with no commit mode that is not a
repair child — appends nothing at all.
`turn_rejected` is the terminal statement about a turn a post-execution guard
refused; such a turn never receives `turn_finished`. Its payload names the
`guard`, the `reason` (the same token the run pauses with), whether the session
was `reopened`, and any guard-specific `detail`. The `guard` names which
post-execution gate refused: `workspace-write-guard`,
`legacy-implementation-write-guard`, `protected-path-guard`,
`review-mutation-guard`, `mailbox-completion-guard`, `plan-artifact-guard`,
`review-verdict`, `session-verification`, or `plan-inspection`. When the guard also returned a
session the worker had marked complete back to `pending`, a `session_reopened`
event follows it with `from_status` / `to_status`, so the reverse transition is
replayable from the event stream instead of being visible only by comparing the
final plan against the last turn record. That event also carries the
`content_sha256` and `counts` of the plan as reinspected after the rewrite, so a
consumer replaying the stream lands on the run's own authoritative projection
rather than on a state it has to re-read the plan to confirm.
`turn_handoff_correction` is appended when a completed handoff was
mechanically malformed — no `Files modified` line, the `(none yet)`
placeholder left in place, or an entry that is not `Alias:path` — and the
runner spent one bounded correction turn on it instead of pausing. The
session is reverted to `pending` with the defect named in its Handoff
`- **Notes**`, then re-run once. A repeat of the same failure, a defect the
worker cannot fix in its own handoff (an entry naming a repository outside the
session's `Writes`), or a plan that still will not parse after the revert all
fall through to the `malformed-plan` pause, whose details then carry the same
`handoff_correction` evidence under an `exhausted` flag. Review artifacts are
independent of the handoff, so `final-review.md` and `final-review.json` are
digested and classified into that evidence before the correction turn starts
and are never rewritten by it. The `mailbox` name uses
`guidance-observed`, `notice-published`, `wait-preparation-failed`,
`wait-started`, `wait-cleared`, `guidance-delivered`, and
`guidance-delivery-deferred` action variants; mailbox activity does not
create a separate event name per record kind. Verification heartbeats
fire every 30 seconds while a single check is in flight, so long-running
checks remain observable in `tail` and the live status snapshot. Verification
check argv is captured in the `verification_check_started` payload and not
streamed to a log file; command stdout and stderr remain captured in the
underlying `asl verify` invocation and in the turn `result.json` under the
`verification` key. Every `verification_check_finished` payload carries a
`role` of `gate` or `diagnostic`, so a reader grading executed checks one at a
time knows which of them could not have gated the result; the recipe-level
counts alone cannot say which check that was. A conditional diagnostic whose
gates all pass emits `verification_check_skipped` with its name, kind,
dependencies, and stable `condition-not-met` reason. `verification_finished`
reports configured, executed, gate, diagnostic, and skipped counts, where the
diagnostic count is configured rather than executed — subtract the skipped
count to bound how many failures the recipe could have absorbed.
A `"parallel": true` recipe adds that same additive key to
`verification_started`; its `verification_check_started` /
`verification_check_finished` records interleave by real completion order while
`result.json` stays in declared order, and heartbeats track whichever check
currently holds the display slot. Every check that has started and not yet
finished is tracked, so when the displayed check finishes the slot moves to
another still-active check and heartbeats continue until the last command
retires. An interrupt kills every abandoned check before `run_verification`
re-raises, so no callback outlives it; `finish` independently drops any check
callback from the moment it begins — before it waits for the heartbeat thread —
so `verification_finished` stays the last verification lifecycle event,
`active_verification` stays `null`, and no check progress line can appear after
the terminal verification message.
`review_session_created`
fires once per run, when the runner appends a fresh generated review session;
resuming an existing pending review session does not re-emit it. Its payload
carries `run_id`, the review `session_id`, `review_harness`,
`review_session_path`, `review_output_path`, and the additive `review_scope` /
`review_base_sha` keys. `review_scope_selected` fires immediately before it with
`run_id`, `session_id`, `scope`, `base_sha`, `fallback_reason`, and the additive
`refreshed` key — the auditable record of why a rereview was or was not
narrowed. It fires a second time, with `refreshed: true`, whenever a pending
review session's scope is re-derived and rewritten before that session executes,
so a resumed review's effective bounds are recorded rather than inferred from
the creation-time event. `review_cycle` fires
once per
completed review session and carries the repair/rereview cycle record (cycle
number, review session id, outcome, finding fingerprints with
introduced/closed/repeated classification, repair session ids, the reviewed
`repo_head` / `worktree_dirty` / `dirty_paths`, and
disposition). `review_repair_reopened` records each later attempt to consume
an existing findings artifact. Successful records include the source
cycle/session/disposition, previous and current artifact evidence, repair child
IDs, and before/after materialization-cap accounting; failed attempts preserve
the stable result and error without consuming budget. Every attempt, refused or
not, also carries the reviewed baseline — `repo_head` / `worktree_dirty` /
`dirty_paths`, or the ordered `repositories` vector — copied from the source
cycle it names, and omits those keys entirely when no cycle can be attributed
to it. `tail` renders review
cycle events inline alongside other events. Commit events include the commit index,
scope, session ID for per-session commits, argv, cwd, artifact paths, status
result, duration, and commit SHA when one was created. `run_control` events
record `stop-after-current-requested`, `stop-after-current-canceled`,
`stop-after-current-consumed`, `abort-turn-requested`, `abort-turn-honored`,
`abort-turn-satisfied-by-worker`, `abort-turn-degraded`,
`finalization-abort-honored`,
`control-command-observed`, `tail-enabled`,
`tail-disabled`, and
`harness-selected`, `guard-applied`, `guard-removed`, `guard-rejected`,
`guards-expired`, and `implementation-model-updated` actions with
`stop_after_current`, `tail_enabled`,
`next_harness`, and, when available, session and turn context. Harness-selection
events also include `current_harness` and `selected_harness`. Abort events also
include `abort_requested`, `abort_grace_seconds`, `abort_reason`, and — on
`abort-turn-requested` alone — the `abort_deadline` the terminal actions clear.
`control-command-observed` is the receipt `asl control <command> --wait` polls
for and carries the `command_id` and `queue_command` it acknowledges.
The four `guard-*` actions carry the `command_id` they answer, the `guard_ids`
they concern, and the active `guards` summary after the change; a rejection
also carries its `reason` and `detail`. `guards-expired` is recorded once at
run termination and names the guards the run was still enforcing.
`abort-turn-degraded` is an abort observed between turns: nothing was killed,
and the request became `stop-after-current-requested`.
`finalization-abort-honored` is the abort that arrived after the last turn had
already ended: it cancels the terminal supervisor audit instead of a turn, arms
with no grace window, and leaves the run's own outcome untouched. `tail` renders
these as `run control:` lines.

`finalization_started`, `finalization_heartbeat`, and `finalization_finished`
bracket the post-turn work the run does while it still holds the plan lock —
today the terminal supervisor audit. The started event names the `run_id`,
`phase`, `started_at`, and `cancellable`; the heartbeat repeats every `30`
seconds with the whole `active_finalization` projection — adding
`elapsed_seconds`, `heartbeat_count`, and `last_heartbeat_at`; and the finished
event reports `heartbeat_count`, `duration_seconds`, `finished_at`, and
`cancelled` for a phase an abort cut short. Their purpose is to keep a
legitimately long finalization distinguishable from a hang.

For normal text runs, live progress is printed to stderr with local compact
timestamps, for example `[2026-06-04 14:32:10] session 2/3 started`, so stdout
remains the final human summary. Generated final-review turns are labeled
explicitly, for example `[2026-06-04 14:32:10] review session started (7/7)`.
The live progress lines cover run start, session starts and finishes, periodic
30-second heartbeats, action-required signals, commits, run-control help or
toggle messages, compact status snapshots, pauses, and run finish. Inline-tail
output, when `t` is enabled, streams only harness/commit log lines and does not
duplicate heartbeats or any other event/progress lines. Detailed artifact paths
remain in the final summary, `status`, `state.json`, and `events.jsonl`, and
are included in the `s` status snapshot on demand.

For `run --json`, stdout remains the final JSON payload and stderr stays quiet;
live monitoring should read `state.json`, `events.jsonl`, or use `tail`.

## JSON Output

Use `--json` for runner integrations:

```bash
asl run ~/.local/share/agent-plans/example-plan/ --harness codex --max-sessions 1 --json
asl status ~/.local/share/agent-plans/example-plan/ --json
asl questions ~/.local/share/agent-plans/example-plan/ --json
asl run ~/.local/share/agent-plans/example-plan/ --harness codex --reply "1a, 2b, 3a" --json
asl sessions --json
asl sessions --running --json
asl sessions --recent --limit 5 --json
asl doctor ~/.local/share/agent-plans/example-plan/ --harness codex --json
asl unlock ~/.local/share/agent-plans/example-plan/ --json
```

All parsed subcommands that support `--json` print JSON errors to stdout. Common
error fields are `ok`, `command`, `reason`, `error`, `paused`, and `details`.
A flag value a handler refuses itself, such as `tail --lines 0`,
`plans --stale-days -1`, `plans --readiness bogus`, `metrics --since-days 0`,
`gc --max-age-days -1`, or `commit --timeout -1`, is `reason: input-error`,
with an `error` worded like argparse's own refusal (`--lines must be a
positive integer; got '0'`).
`asl tail --json` is a stream rather than one object: it prints the latest
run's `events.jsonl` records one per line — the last `--lines` of them, then
each new one until `run_finished` (or only the first part with `--no-follow`)
— and leaves harness log lines to the text form; a failure is still one error
object. `asl backfill-progress --json` answers `reason: dry-run` with the
parsed `entries` and the rendered `journal` under `--dry-run`, `reason:
written` with `entries_written`, `completed_session_ids`, and `progress_path`
otherwise, and refuses an existing journal without `--force` as `reason:
progress-exists`. `asl workbench pin` and `asl workbench unpin` accept `--json`
like `asl workbench registry`, answering `plan_dir` and `registry_path`.
`run --json` preserves its existing fields and adds the common `command` and
`details` fields; important run fields include `paused`, `reason`,
`pause_reason`, `pause_details`, `harness`, `harness_config`, `model`,
`model_source`, `observed_model`, `observed_model_source`, `model_projection`,
`effort`, `effort_source`, `commit_mode`, `note`, `commits`,
`control`, `no_mailbox`, `mailbox_enabled`, `mailbox_activation`,
`completed_sessions`, `completed_session_ids`, `turns`, `initial_snapshot`,
`final_snapshot`, `state_path`, and `events_path`. Runs without commit flags
report `commit_mode: null` and `commits: []`.

`status --json` adds a read-only `mailbox` projection. It reports the canonical
mailbox and lease paths, presence/activation, byte size and 4 MiB limit,
physical `last_seq`, pending-guidance and unanswered-question counts, rejected
record count, full state, resolution and notice totals, and best-effort errors.
Beside `full` it reports `accepts_questions`, `accepts_replies`, and
`accepts_resolutions`: the first says whether a record still fits under the
reduced cap shared by every non-closure kind, while the latter two independently
report capacity for their differently sized minimal valid closures. When the
mailbox cannot be read, all three fields and the resolution/notice totals are
`null` alongside `full` rather than defaulting to writable or empty. The text
`Mailbox:` line says `replies and resolutions still accepted`, `only replies
still accepted`, or `reply and resolution capacity exhausted` from those two
closure flags and never overclaims that a closure can land. It also renders the
resolution and notice totals without rendering their bodies. Alongside the file-faithful
`unanswered_questions` total it reports `open_questions`, the subset owned by a
run that is still live; it is `0` when no live run holds the plan and `null`
when the mailbox cannot be read. The text `Mailbox:` line leads with the open
count and appends the unanswered historical difference only when it is
nonzero, so an
abandoned record reads as history rather than a live block. Its lease fields include
`lease_present`, `lease_fresh`, `lease_reason`, and `lease_error`. The stable
lease-reason vocabulary is `missing`, `invalid-size`, `empty`, `invalid-json`,
`not-object`, `unsupported-version`, `invalid-supervisor-id`,
`invalid-updated-at`, `invalid-run-id`, `invalid-projection-seq`,
`invalid-unanswered-questions`, `invalid-latest-question-id`,
`invalid-latest-question-created-at`, `incomplete-question-projection`,
`future-updated-at`, and `expired`. An absent lease is
projected as `lease_present: false` with `lease_reason: null`; a fresh lease
has `lease_fresh: true` with `lease_reason: null`. Stale or malformed leases
carry their classification in `lease_reason`, while `lease_error` carries a
best-effort malformed/unreadable detail and is otherwise `null`. The projection
never returns raw message bodies.

Supervisor lease schema v2 remains a fixed 512-byte record and adds an optional,
text-free question projection: `run_id`, `projection_seq`,
`unanswered_questions`, `latest_unanswered_question_id`, and
`latest_unanswered_question_created_at`. The latest-question fields are absent
when the count is zero. The autopilot binds the projection only after status
attributes `state.json` to the current lock holder and republishes it after each
successful five-second mailbox poll. The projection sequence advances only when
the observed unanswered-question set changes, so unrelated mailbox records
cannot reassert an acknowledged question. Readers continue to accept v1 leases,
which carry no question projection.

The mailbox record schema version is **not** bumped for question ownership:
`run_id` is an optional question-only field that older readers ignore and newer
readers treat as absent when malformed, blank, or over 128 characters. A bad
value degrades the question to unattributed rather than rejecting the line, so
the reject vocabulary is unchanged and history written before run attribution
keeps parsing.

Before every self-launch or relaunch, the autopilot removes the previous
`run_id`, observed question IDs, and pending projection while preserving the
physical mailbox cursor, then publishes an unbound lease. Status is re-probed at
mailbox deadlines until `state.json` is proven newer than the current child
lock; only then can that run's v2 projection return. This prevents a watcher
that survives multiple chunks from refreshing an earlier run's question under
its successor.

Both `status --json` and a live run's state
expose compact `waiting_for_reply` metadata while a bounded answer wait is
active. The adjacent top-level `waiting_for_reply_status` field is `live` when
the wait's unsupervised ceiling is still in the future and the plan has an
active runner lock, `expired` when that ceiling has passed or the owning runner
lock is absent or stale, and `invalid` when the persisted ceiling is missing or
malformed; it is `null` when no wait is present. Because the ceiling bounds
unsupervised time, the classification consults lease freshness rather than a
bare timestamp: the projected ceiling is the later of `unsupervised_deadline_at`
and `last_lease_activity_at` plus `unsupervised_ceiling_seconds`, so a wait whose
supervisor keeps renewing stays `live` past the instant persisted at its start.
A record written before the ceiling began rolling carries only `hard_deadline`
and is classified from that timestamp exactly as it was before. A terminal
paused projection still takes precedence for legacy pause consumers.

Paused `run --json` payloads also carry an `interactive_help` object whenever
the paused turn left a non-empty `last-message.txt` body (it is `null`
otherwise). The block surfaces the data a wrapping coordinator needs to route
the operator conversation without rebuilding state: `questions_command` and
`reply_command` are ready-to-paste `asl questions` and
`asl run --resume --reply TEXT` invocations preserving the current `--state-dir`; `session_id` is the paused
turn's session, `next_pending_id` is the canonical next pending ID,
`turn_index` is the paused turn index, `artifact_paths` repeats the durable
`last_message_path`/`stdout_path`/`stderr_path`/`result_path`/`artifact_dir`,
and `rendered_prompt` is the same resolver prompt `asl questions` would emit
(the operator reply text is never embedded). `asl questions --json` mirrors
the same fields under `ok: true` (`session_id`, `next_pending_id`,
`last_message`, `rendered_prompt`, `state_path`); failures use stable
`reason` strings (`no-paused-run`, `missing-question-context`,
`plan-not-found`, `malformed-plan`). The reply turn itself is identifiable from
`turns[*].prompt_kind` (`"session"` for plain session prompts,
`"reply"` for the one-shot resume); reply turns also carry a `reply_source`
summary (`run_id`, `session_id`, `turn_index`, `last_message_path`,
`reply_length`) while the verbatim reply text is never persisted.

Review-harness fields are present on every run payload. `review_requested` is
`true` when `--review-harness` was supplied, `false` otherwise. `review_harness`
is the requested review harness name or `null`. `review_session_id` is the
canonical ID of the generated review session once it has been appended or
resumed, otherwise `null`. `review_output_path` is the absolute path to
`PLAN_DIR/final-review.md` whenever review was requested, otherwise `null`.
`review_completed` is `true` only after the review turn finished successfully.
`review_session_executed` is the canonical ID of the review session whose turn
actually ran, stamped when that turn returns rather than when it commits, and
`null` when no review turn ran at all. `review_verdict` is `null` until a review
completes successfully; then it contains `status_line`, `outcome`
(`"all-clear"` or `"findings"`), `total_findings`, and `findings_by_severity`
(`high`, `med`, `low`). A review that ran without reaching its commit — because
its handoff failed plan inspection, say — is attested from the artifact it left
on disk instead: `review_verdict` is read back from `final-review.md`, and
`review_output_sha256` / `review_artifact_sha256` record the digests of the two
review artifacts. That read-back applies the same artifact requirement the run
itself enforced, so a generated v2 review missing its `final-review.json` keeps
`review_verdict: null` — the digests still describe what landed on disk, but a
verdict the run refused as invalid is never projected back as if it stood.
`review_skipped_reason` is `null` for clean review
completions, for implementation-only runs, and for any run whose review turn
executed; for review-requested runs whose review never ran it records the
terminal reason (`max-sessions`, `stopped`, a pause reason, etc.).
`completed_sessions` and `completed_session_ids` continue to include every
completed turn for backward compatibility; consumers that need to distinguish
review from implementation turns read `turns[*].session_kind`, which is either
`"implementation"` or `"review"`. The same `session_kind` field is also
recorded on `active_turn` while a review turn is in flight and on the recent
session overview rows surfaced by `asl sessions`. Dry-run `planned` payloads
include `review_requested`, `review_harness`, and `review_output_path` so
planners can preview the review path before any harness runs.

Both `status --json` and `run --json` expose the same additive
`review_resume` projection. Existing fields remain unchanged, so older clients
may ignore this block. `action` is one of `initial-review`,
`materialize-repairs`, `run-repairs`, `rereview`, or `terminal`; `eligible`
describes the persisted/effective-cap evaluation, and `blocker` is `null`,
`review-harness-required`, `higher-cap-required`, `artifact-invalid`,
`not-loop-ready`, `repair-scope-stale`, a standing environment-blocked pause
reason (`commit-preflight`, `commit-failed`, `repository-registry-required`, or
`workspace-repository-contention`), a terminal disposition (`all-clear`,
`repair-group-exhausted`, `findings-repeated`, or `unstructured-findings`), or
a non-disposition terminal blocker (`repair-sessions-missing`,
`repair-sessions-not-runnable`, or `unknown-disposition`). Repair children the
executability gate refuses are withdrawn by the publication rollback described
above, so an ordinary refusal now leaves no pending sessions at all. That cycle
stays a materialization candidate rather than becoming terminal: a
`not-loop-ready` cycle with no surviving children and no historical
materialization never materialized a child set for anything to have deleted, so
it projects `materialize-repairs` with `eligible: false` and blocker
`not-loop-ready` until the refusal that withdrew the children is cleared, at
which point the same findings re-materialize. `repair-sessions-missing` remains
what it always meant — a child set deleted out from under a cycle that really
did materialize one. A repair child that `session-resplit` archived out of the
plan is not that loss: the parent is gone by design and nested children took its
place, authored fresh and so carrying no repair marker of their own. Event
lineage still names the parent, so the projection reconciles it — a lineage
repair id the plan no longer holds hands its place to the sessions nested
strictly beneath it, which then have to satisfy the same completion accounting
the parent would have, and appear in `pending_repair_session_ids` and
`completed_repair_session_ids` in its stead. `repair_session_ids` still reports
the lineage as recorded. The reconciliation is fail-closed on both sides: a
parent still present in the plan is never handed to its descendants, whatever it
looks like inside, and a parent with no descendants at all is the deletion the
blocker exists for. Without it a plan whose every repair had completed was
reported terminal instead of being sent to the rereview its findings still
required (ASI-146). Children
retained under `--allow-not-loop-ready`, and children authored by an older ASL
that stranded them, still look runnable while the plan is not loop-ready: a
`run-repairs` continuation for such a plan reports `eligible: false` with
blocker `not-loop-ready` until the plan is marked loop-ready again, rather than
claiming those children are ready to run. A `run-repairs` continuation
also reports `eligible: false` with blocker `repair-scope-stale` when a pending
repair child's persisted **Writes repositories** is narrower than the scope its
own findings require, which would hand that child a work list naming files it
is not authorized to write. The comparison is record to record — the persisted
child scope against the scope recomputed from the validated findings — because
the retained guard is derived from that same persisted scope and agrees with it
even when both are too narrow. An additive `repair_scope_gaps` block names the
missing aliases and their provenance per child; rewriting the child with the
required scope clears the blocker. The blocker is a gate, not an advisory:
`asl run` projects this continuation before it dispatches any pending generated
repair child, widens every child the `repair_scope_gaps` block names — not only
the one about to run, since one stale sibling blocks the plan-wide projection —
and reprojects. A child the second projection still refuses is never launched;
the run pauses as `malformed-review-repair` carrying the blocker and the
missing-alias evidence. A `run-repairs` continuation also reports
`eligible: false` with the standing pause reason as its blocker when the newest
run in the event log paused on one of the environment preconditions
`pause_policy` classifies as `inspect` — the pauses a workspace fix clears
rather than a mailbox answer. Those conditions outlive the run that hit them: a
dirty repository, an unregistered one, or a plan left at `draft` is still there
when the next run opens, so calling the pending children eligible would
contradict the paused state recorded in the same `state.json`. The signal is
the run's own terminal record, not a live `git status`, so the projection stays
reproducible from the log alone. A later `run_started` clears it, which means
rerunning the plan after fixing the workspace is enough and nothing has to be
rewritten; a `run_finished` does not, because the runner appends one for paused
runs too. An unrecognized recorded disposition is retained in
`source_disposition` for diagnosis and maps to the stable
`unknown-disposition` blocker. The projection includes source lineage,
historical/required/selected cap values, pending and completed repair IDs,
artifact validity/error, previous/current finding fingerprints, and nullable
source/current Markdown and JSON SHA-256 digests. For example, an all-complete
plan stopped at its absolute cap can report:

```json
{
  "review_resume": {
    "source_cycle": 2,
    "review_session_id": "7",
    "source_disposition": "cycle-cap-exhausted",
    "action": "materialize-repairs",
    "eligible": false,
    "blocker": "higher-cap-required",
    "historical_materializations": 2,
    "minimum_required_cap": 3,
    "selected_cap": 2,
    "repair_session_ids": [],
    "pending_repair_session_ids": [],
    "completed_repair_session_ids": [],
    "source_fingerprints": ["finding:abc"],
    "current_fingerprints": ["finding:abc"],
    "source_review_output_sha256": "<sha256-or-null>",
    "source_review_artifact_sha256": "<sha256-or-null>",
    "current_review_output_sha256": "<sha256>",
    "current_review_artifact_sha256": "<sha256>",
    "artifact_valid": true,
    "artifact_error": null
  }
}
```

A new run's explicit `--max-review-fix-cycles 3` prospectively satisfies that
budget blocker even when read-only status was computed under the older cap.
Stable insufficient-budget runs make no session or artifact changes and return
the same projection. When a reopen is attempted, `run --json` additionally
exposes the latest `review_reopen` record; the durable append-only copy is the
`review_repair_reopened` event. `review_cycles`, `review_fix_disposition`, and
the existing review-verdict fields remain available for compatibility.

Commit payloads include `commit_index`, `scope`, `session`, `session_id`,
`artifact_dir`, `stdout_path`, `stderr_path`, `result_path`, `argv`, `cwd`,
`returncode`, `success`, `status_result`, `head_before`, `head_after`,
`commit_sha`, `started_at`, `finished_at`, `duration_seconds`, `stdout_chars`,
and `stderr_chars`. Integer-only session commits also include
`session_number`. `status_result` is `success` only when the command exits zero
and changes `HEAD`; nonzero exits, missing `HEAD`, and unchanged `HEAD` are
recorded as failed commit attempts. A failed commit is also copied into the
`commit_failures` entry it produced and into that turn's `session_commit`
record; when a pause carries a commit at all, it is copied into
`pause_details.commit` as well.
