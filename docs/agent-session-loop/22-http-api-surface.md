### HTTP API surface

Read endpoints (loopback-open, no token required):

- `GET /` — bundled Workbench HTML shell (`Cache-Control: no-store`).
- `GET /static/<path>` — bundled JS/CSS assets
  (`Cache-Control: no-cache, must-revalidate` plus strong-validator
  `ETag` from `st_mtime_ns` + `st_size`; the server replies `304 Not
  Modified` to a matching `If-None-Match`).
- `GET /api/health` — liveness probe. The one route that requires no role
  at all, in either mode.
- `GET /api/capabilities` — what this deployment can do. Returns `mode`
  (`local` or `remote`), one entry per operation with its `role`,
  whether it is `available`, and — when it is not — the provider's own
  `reason` in the words to show whoever is looking at the greyed-out
  control, plus `unavailable_operations` and the declared `routes`. The
  application answers this itself rather than asking the provider, so a
  deployment that can do very little can still say so. Attempting an
  unavailable operation returns `503` with
  `reason: "operation-unavailable"` and that same sentence as `error`.
- `GET /api/summary` — counts and health only, readable by a
  summary-scoped caller. The narrow read a status pane needs without
  owner-scoped plan detail; every other read requires the owner.
- `GET /api/queue` — every discovered plan summarized via
  `WorkbenchSummary` (counts, primary state, recommended action,
  warnings, last-message preview, artifact paths, compact Recovery
  Center summary) plus the resolved `state_root` and
  `state_root_exists` fields used for that scan. Each plan summary also
  carries the additive triage fields documented under
  [Triage queue payload](#triage-queue-payload) so the frontend can
  sort, group, and search without re-deriving status from raw counts.
  `GET /api/queue?sort=oldest` sorts by oldest activity first: complete
  plans sort on `completed_at` and every other plan on
  `last_activity_at`, and a missing timestamp is treated as oldest.
  Queue responses also include
  `archive_retention_days` and per-plan `archivable` / `archive_blocker`
  fields used by Cleanup mode. The scan behind the response is the shared
  catalog (see [Plan catalog](21-plan-catalog.md#plan-catalog)), so the payload also carries
  `catalog_generation`, `catalog_complete`, `catalog_total`, and
  `catalog_warnings` — the last naming any plan the scan could not classify,
  as opposed to `discovery_warnings`, which name a source it could not read.
- `GET /api/plan?plan_dir=<path>` — single-plan detail. The payload
  carries the same additive triage fields as `/api/queue` (`urgency_rank`,
  `urgency_group`, `activity_label`, `concise_reason`, `last_activity_at`,
  `last_activity_source`, `searchable_text`) plus the same compact
  `recovery` summary used by queue rows and detail-panel fallbacks.
- `GET /api/recovery?plan_dir=<path>` — full read-only Recovery Center
  diagnosis for one plan. It returns the `plan_dir`, `recovery` payload,
  mirrored top-level `artifacts`, and selected `run_id`, `session_id`,
  `turn_index`, and `commit_index` context. Missing or invalid
  `plan_dir` values return `invalid-request` or `invalid-plan`; split
  plans that cannot be inspected return `malformed-plan`.
- `GET /api/registry` — registry payload plus any loader warnings.
- `GET /api/state?plan_dir=<path>` — latest runner `state.json` under
  `state`, plus three top-level keys derived from the plan's session
  files: `sessions` (ordered list with `id`/`status`/`raw_value`/
  `line_number`/`path`, plus integer `number` for integer-only IDs;
  dotted IDs such as `2.1` are preserved verbatim and sorted in
  canonical dotted order), `counts` (`{pending, complete, empty,
  unknown}`), and `next_pending_id` (string or `null`). The schema
  matches `asl status --json` so frontend renderers can share a single
  shape. Malformed plans (missing `manifest.md`/`context.md`, no
  `session-*.md` files, etc.) return HTTP 500 with
  `reason: "malformed-plan"` and a `details.plan_dir` field — operators
  should branch on `reason`, not the HTTP status alone.
- `GET /api/events?plan_dir=<path>&offset=N&limit=N` — paginated
  `events.jsonl` entries (default 200, cap 2000).
- `GET /api/logs?plan_dir=<path>&stream=stdout|stderr|...` — bounded
  stdout/stderr tail or offset read (default 64 KiB, cap 1 MiB).
  Accepts `stream=stdout|stderr` for the active turn or an explicit
  `path=<absolute>` validated to be inside the plan's state directory.
- `GET /api/last-message?plan_dir=<path>` — bounded last-message text.
- `GET /api/result?plan_dir=<path>` — active turn `result.json` payload.
- `GET /api/verification?plan_dir=<path>` — active verification block.
- `GET /api/commits?plan_dir=<path>` — commit artifact metadata.
- `GET /api/progress?plan_dir=<path>` — parsed `progress.md` entries.
- `GET /api/final-review?plan_dir=<path>` — bounded `final-review.md`
  content (default 256 KiB).
- `GET /api/questions?plan_dir=<path>` — resolver prompt and Q&A
  context for the paused turn (mirrors `asl questions --json`).
- `GET /api/redirect-status?plan_dir=<path>` — durable redirect
  operations for one plan, projected through `redirect_cli` so the stage
  a browser renders and the stage `asl control redirect-status` prints
  are one derivation of the same journal. Returns `journal_path`,
  `owner`, `owner_run_id`, `live_target`, `operations` (each with both
  the durable `status` and the presentation-only `stage`),
  `uncertain_operation_ids`, the `journal_complete` / `torn_tail` /
  `unreadable_lines` honesty fields, `truncated`, `checked_at`,
  `conversations`, and the `action_token`, `permitted_actions`, and
  `disabled_reasons` a composer needs to submit a correction without
  deriving its own freshness proof. A repeatable `operation_id=<id>`
  filters the window, which is capped at
  `workbench_server.MAX_REDIRECT_OPERATIONS` (64) and truncated from the
  oldest end. No field carries the operator's words — only
  `feedback_digest` and `feedback_bytes`.
- `GET /api/conversation-recovery?plan_dir=<path>` — which recovery an
  unresumable conversation is waiting on, and nothing more. Returns
  `recovery_required`, `pause_reason`, `decision`
  (`fresh-conversation` or `predecessor-not-stopped`), `auto_resume`
  (always `false`), `authorized_by`, `inspect_command`,
  `authorization_command`, `one_use`, `note`, `conversations`, and the
  `retained_operations` a fresh start would still carry. The endpoint
  performs no recovery: doing so would persist a one-use operator
  authorization in a request a browser can replay, and
  `predecessor-not-stopped` has no authorization at all.
- `GET /api/artifacts?plan_dir=<path>` — artifact paths and metadata
  for the latest run.
- `GET /api/archive` — archive entries plus the current
  `archive_retention_days`, computed `expires_at` values, each entry's
  `cleanup_state`, and the `archive_cleanup` summary of which entries
  retention has marked as cleanup candidates. The read removes nothing.

Mutating endpoints (bearer-token required):

- `POST /api/registry/pin` — upsert a `RegistryPin`. Payload:
  `{"plan_dir": "...", "label": "...", "profile": {...}}`.
- `POST /api/registry/unpin` — `{"plan_dir": "..."}`.
- `POST /api/plan/archive` — archive one live or discovered plan by
  moving the plan directory, matching ASL state directory, and pin/profile
  metadata into the Workbench archive root. Payload:
  `{"plan_dir": "...", "confirm_plan_dir": "..."}`. The two paths must
  canonicalize to the same full path, matching the UI's typed
  full-path confirmation. The server refuses arbitrary non-plan paths
  and returns `409` with `reason: "active-lock"` when an active runner
  lock is present.
- `POST /api/plans/archive` — bulk archive. Payload:
  `{"plan_dirs": ["..."]}`. Returns per-entry `results` plus
  `archived_count` and `failed_count`; failures are isolated to the
  affected entries.
- `POST /api/plan/delete` — backward-compatible archive alias for
  `POST /api/plan/archive`. It no longer permanently deletes a live
  plan; clients that need permanent removal should purge the resulting
  archive entry through `POST /api/archive/delete`.
- `POST /api/archive/restore` — restore one archive entry to its
  original plan and state paths. Payload: `{"archive_id": "..."}`.
  Returns `409` with `reason: "restore-conflict"` if a destination path
  that would be written already exists.
- `POST /api/archive/delete` — permanently purge one or many archive
  entries. Payload: `{"archive_ids": ["..."], "confirm_count": N}`.
  `confirm_count` must equal the number of selected IDs; otherwise the
  request returns `reason: "confirmation-mismatch"` and removes nothing.
- `POST /api/archive/settings` — update the global archive retention
  window. Payload: `{"archive_retention_days": N}` with a positive
  integer day count.
- `POST /api/launch` — spawn `asl run` for a plan. Payload:
  `{"plan_dir": "...", "profile": {...}}`. Returns `pid`, `argv`
  (with reply prompts redacted), `plan_dir`, `profile`, `reply_provided`,
  `reply_length`, `reply_origin`, and `state_dir`. An optional
  `launch_id` makes a resend idempotent; see the resend notes below.
- `POST /api/reply` — resume a paused run with a verbatim reply.
  Payload: `{"plan_dir": "...", "reply_text": "..."}`. The `reply_text`
  body is streamed to the child via `--reply-stdin`; only `reply_length`
  survives in the response. It accepts the same optional `launch_id`.
- `POST /api/control/stop-after-current` — enqueue a
  `request-stop-after-current` command on the per-plan control queue.
  The runner consumes it at the next checkpoint.
- `POST /api/control/abort` — enqueue a `request-abort-turn` command.
  Payload: `{"plan_dir": "...", "grace_seconds": 120, "reason": "..."}`;
  both are optional and validated through the same `control_queue` path
  `asl control abort` uses, so an out-of-range window returns
  `invalid-grace` and an empty reason returns `invalid-reason`. The
  response reports `run_active`, `turn_active`, the resolved
  `grace_seconds`, `abort_reason`, and a `note` saying whether the abort
  ends a turn or degrades to stop-after-current. Like the other control
  endpoints it reports the best-effort `signal` wakeup it sent, but it has
  no receipt equivalent of `asl control abort --wait`: the HTTP surface
  confirms the append, never the runner's observation.
- `POST /api/control/select-harness` — enqueue a
  `select-future-harness` command. Payload:
  `{"plan_dir": "...", "harness": "codex|claude|copilot"}`. The active
  turn is never interrupted; the request takes effect at the next turn.
- `POST /api/control/redirect` — submit one urgent correction against the
  turn the run is on now, through the same `intervention` /
  `redirect_admission` path `asl control redirect` and the bridge use.
  Payload: `{"plan_dir": "...", "operation_id": "...",
  "action_token": "...", "text": "...", "expected_target": {...},
  "wait_seconds": N, "run_id": "..."}`. `operation_id` is required and is
  the idempotency key; `action_token` comes from
  `GET /api/redirect-status`; `expected_target` is optional and turns "the
  turn ended while the operator was typing" into a `stale-target` refusal
  instead of a correction delivered to whatever the runner moved on to;
  `wait_seconds` is bounded by
  `redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS`. Unlike the other
  control endpoints this one is not a queue append — it writes to the
  redirect journal and rings the urgent channel — so its status codes
  report what became durable: `202` with `accepted` and a `redirect`
  receipt when anything was persisted, `200` with `redirect.replayed:
  true` when the `operation_id` was already submitted *and* carries the
  same correction, `409` when nothing was persisted (a stale action
  token, an action the run does not permit, an admission refusal such as
  `stale-target`, or `reason: "redirect-replay-conflict"` — see below),
  `400` for a malformed or oversized body, and `503` with `reason:
  "redirect-unavailable"` when the journal itself cannot be read. The
  response carries `feedback_digest` and `feedback_bytes`, never the
  correction text.

  Replay is by request identity, not by id alone. Before answering a
  known `operation_id` the handler compares the submitted `text` — and
  the `expected_target`, when one is supplied — against the durable
  record, the same rule `redirect_store_requests.submit_request` applies to a
  first submission. A retry that changed either is not a retry: it is
  refused `409` with `reason: "redirect-replay-conflict"`,
  `retryable: false`, and `details.conflict` of `feedback` or `target`,
  the recorded receipt in `details.redirect`, and the journal untouched.
  Because identity includes the words, `text` is required even on a
  replay; `GET /api/redirect-status` is the body-free way to ask what
  became of an id.
- `POST /api/unlock` — remove an operational lock. Active locks return
  409 unless the payload includes `"force": true`; idempotent missing
  locks return 200 with `had_lock: false`.

All control mutations except `POST /api/control/redirect` enqueue records
onto `<state-dir>/<plan-hash>/control-queue.jsonl` rather than touching
`state.json` directly. The active runner drains the queue between
lifecycle checkpoints and translates each accepted command into the
same `RunControlAction` transitions that the in-process keyboard
listener already emits, so `run_control` events look identical
regardless of source. A redirect is deliberately not a checkpoint
command: it appends to the plan's redirect journal and rings the urgent
channel, because a correction that waits for the next checkpoint is a
correction the turn it was aimed at has already outlived.

Three mutating endpoints are safe for a browser to resend, and each one
says so with a different mechanism because they have different durable
records to appeal to:

- `POST /api/control/redirect` answers a resent request from the redirect
  journal. Replay is checked by `operation_id` *before* the action token
  is validated, because the correction that landed is exactly what made
  the token the client still holds stale; checking the token first would
  turn every lost response into a refused retry. The request itself is
  still checked: a resend must carry the same correction (and the same
  turn, when it names one) the journal recorded under that id, or it is
  refused `409 redirect-replay-conflict` with nothing rewritten.
- `POST /api/launch` and `POST /api/reply` accept an optional `launch_id`
  bounded by `redirect_store_vocab.REDIRECT_MAX_OPERATION_ID_CHARS`, and return
  the original launch result with `replayed: true` for a repeat of an id
  already admitted for the plan. The receipt is durable: both routes admit
  through the plan's operation journal, so a resend is idempotent across
  Workbench processes and across a restart, not only within the process
  that spawned. An id resubmitted for a different action or a different
  plan is refused `409 operation-conflict` rather than quietly reused, and
  a second submission while one is still outstanding is refused `409
  operation-outstanding`.
- Both spawning endpoints also refuse outright, with `409` and
  `reason: "run-already-active"`, while `intervention.inspect_run`
  reports a run holding the plan, and with
  `reason: "launch-already-in-flight"` while a process this server
  started for the plan is still alive. The second check covers the window
  a reconnecting browser actually lands in — spawned, but not yet holding
  the lock — where nothing durable says a launch is under way. A paused
  run is not a conflict: resuming one is what `/api/reply` is for.

### Durable operation admission

Every mutation the Workbench and the connector can both submit passes one
gate first. `agent_session_loop.workbench_operations` keeps a per-plan
journal at `<state-dir>/<plan-hash>/operations.jsonl`, beside the redirect
journal, and holds an exclusive `flock` across read, decide, and append —
so two Workbench processes, or a Workbench and the connector, submitting
at the same moment serialize there and exactly one is admitted. The rules
themselves live in `asl_workbench.operations`, which is stdlib-only and
shares its status vocabulary with the Companion's `protocol.py`.

An operation id names one action against one target. Identity is the
sha256 fingerprint of the method and the target, so `launch_id` "abc" for
one plan and `launch_id` "abc" for another are different operations, and
the same id aimed at a different method is a conflict rather than a
replay.

Dispatch is recorded before the side effect, never after. An admitted
operation is written `pending`; the row moves to `dispatched` immediately
before the process is spawned. That ordering is what makes an interruption
answerable: **a killed process leaves `expired` when the mutation
certainly did not run, and `unconfirmed` when it may have and nobody can
say.** Reconciliation draws its evidence from ASL — the plan lock through
`intervention.inspect_run`, the owning pid through `state.is_pid_running`
— and never infers success from absence: an accepted operation whose run
has vanished becomes `unconfirmed`, not `consumed`. An `unconfirmed`
operation is never retried automatically. A person submitting a new
operation in its place, against a target that is demonstrably idle,
settles the old row `superseded` with detail `replaced-uncertain` — which
records that it was replaced, not that it did not run.

Admission is a gate in front of the plan lock, not a replacement for it.
ASL's lock remains the execution authority: admission stops a second
attempt from being *started*, and the lock is what stops a second run from
*executing*. Reconciliation classifies a lock without reclaiming it.

The journal holds identifiers only. `OPERATION_PERSIST_FIELDS` is an
allowlist, and `FORBIDDEN_PERSISTED_KEYS` is checked against every nested
value before a row is written, so reply text and action tokens cannot
reach the file even under a key the allowlist keeps.

### One path for a launch, a reply, and a paused resume

`agent_session_loop.workbench_actions.perform` is the single application
operation behind `POST /api/launch`, `POST /api/reply`, and the bridge's
`run.resume` and `question.reply`. It runs the same six steps every time:
inspect the run through `intervention.inspect_run`, check freshness,
admit through the journal, consult the caller's guard, record dispatch,
carry the mutation out, and settle the row. Surface-specific code is
reduced to translating a refusal into that surface's error shape, so the
local Workbench and the connector cannot drift into answering the same
request differently. `asl_workbench.actions` holds the portable half —
the action/method table, the launch-refusal classification, and the
reading of the plan's event log — so the Pi interprets an imported log
exactly as the Mac interprets a live one.

**A stale target is refused with or without an action token.** A request
that names a `run_id` or a `question_id` is making a claim about that run
or that question, and the claim is checked against what
`intervention.inspect_run` reports regardless of which surface asked. A
bridge caller must additionally carry a fresh `action_token`; a browser
caller that names nothing beyond the plan is held to the run-ownership
checks alone. Either way, a target that has moved is refused before
admission, so the operation id the request carried is still spendable.

**What settles an operation depends on what it promised.** A launch
promised a process, so the spawn is the whole of it and the row goes
straight to `consumed`. A reply-bearing operation — `question.reply` and
the `run.resume` that streams a reply into a new run's stdin — promised
that an operator's words were *read*, which a `Popen` does not establish.
Those rows settle `accepted`, and reach `consumed` only on a
`reply_provided` record in the plan's own `events.jsonl` whose `source`
carries the matching origin (`mailbox` or `stdin`) and, for a mailbox
reply, the matching `question_id`. A resumed run mints a new run id, so
the evidence *reports* which run read the reply rather than matching it
against the id the operation named — requiring a match would refuse every
successful resume. A witness settles a reply-bearing row even while the
process that submitted it is still alive, because the run's record is not
a claim about that process's progress.

**A launch refused before the spawn is `rejected`; a child killed after
it is `unconfirmed`.** `asl_workbench.actions.launch_closure` draws that
line from the refusal reason plus whether a process was created, which is
the same distinction `expired` and `unconfirmed` draw one step earlier. A
guard refusal closes the row `cancelled` on the way past, so a withdrawn
submission stops holding the plan's run-start scope instead of blocking
every later one.

**A launched run gets its own session.** `workbench_launch.launch_plan` passes
`start_new_session=True`, matching the `AbandonProcessGroup` the launchd
agents already set. A Ctrl-C in the terminal running `asl workbench
serve`, a connector going away, or a closing terminal each signals a
process *group*; without its own session the worker would be killed
mid-turn by an event that has nothing to do with the plan.

All responses use the `responses.success_payload`/`error_payload`
envelopes with `command: "workbench"`. Stable error reasons reused
verbatim from the underlying helpers include `invalid-plan`,
`active-lock`, `malformed-plan`, `invalid-harness`, `bind-failed`, and
the `control_queue`/`workbench` reason codes. Control endpoints surface
the enqueued record under `queued_command` (not `command`) so the
top-level envelope key is not overwritten.

### One path for a stop, an abort, and a correction

`agent_session_loop.workbench_controls` is the single application service
behind the Workbench's `POST /api/control/*`, `GET /api/redirect-status`
and `GET /api/conversation-recovery`, and behind the connector's
`run.stop-after-current`, `run.abort`, `redirect`, `redirect-status`, and
`recover-conversation`. Every control is checked, queued, and followed by
the same best-effort wakeup the CLI producer sends, through
`intervention.dispatch_control_command`; every correction is judged
against the redirect journal in one order. `asl_workbench.controls` holds
the portable half — the control-to-queue-command table, what an abort is
about to mean, the projection window, and the narrowing every
client-chosen field goes through — so the Pi and the Mac cannot be given
two spellings of one rule.

**How hard a surface checks is a parameter, not a second
implementation.** `AUTHORIZATION_OPEN` queues a control for a plan whose
runner has not started yet, which is what a local operator with no
snapshot has always been able to do and what the CLI still does;
`AUTHORIZATION_STRICT`, which the connector uses for every control decided
against a live turn, refuses anything that is not about the run the caller was
shown. Both are correct for their caller.
A correction is the exception on both surfaces: it interrupts a turn, so
it is always held to a fresh `action_token` — a request without one is
refused `stale-action-token` rather than as a malformed body.

**Replay is judged before freshness.** The journal is asked what an
`operation_id` already names before the token is checked, on both
surfaces, because a correction that landed moves the plan, which moves the
token, which would make the retry of a lost response look like a stale
second correction. What is not short-circuited is the request itself: an
edited retry is `redirect-replay-conflict` with the conflicting field
named, because reporting an operator's edited words as a successful replay
of the words the worker actually got is the falsehood this path exists to
avoid.

**The seven interruption boundaries keep their distinctions.** A
correction persisted and unanswered reads `unanswered`; one handed to an
attempt that never receipted it reads `uncertain`, and neither is resolved
by guessing on either surface. Both reads are read-only by construction —
`recover-conversation` reports the relaunch and the corrections it would
inherit, and never performs it, because performing it would persist an
operator's one-use authorization inside a request that can be replayed.

**A refusal keeps its vocabulary per surface.** The service raises what
happened and how retryable it is; the HTTP surface turns that into a
status code and the bridge into one of its versioned `ERROR_CODES`, with
any reason outside that closed enum travelling as `ERROR_INVALID_REQUEST`
carrying the durable reason in `details.reason`.

### Triage queue payload

Each plan summary in `/api/queue` (and `/api/plan`) carries additive
fields the frontend uses to triage at a glance. All fields are
backwards-compatible — no existing key is renamed or removed.

- `urgency_rank` (int) — lower means higher priority; the queue sorts
  ascending by default. Operator-blocking states come first.
- `urgency_group` (string) — one of `needs-action`, `in-progress`,
  `ready`, or `complete`. Mirrors `workbench_summary_model.URGENCY_GROUPS`.
- `activity_label` (string) — short status string for the row
  ("Awaiting reply", "Running", "Complete", …). Mirrors
  `workbench_summary_model.ACTIVITY_LABEL_BY_STATE`.
- `concise_reason` (string) — one-line cause behind the current state
  (pause reason, last failure summary, etc.).
- `last_activity_at` (ISO 8601 string or `null`) — most recent
  meaningful activity timestamp.
- `last_activity_source` (string or `null`) — provenance of the
  timestamp. One of `active-verification`, `active-turn`,
  `run-finished`, `run-started`, `lock-created`, `progress-journal`.
  Mirrors `workbench_summary_model.LAST_ACTIVITY_SOURCES`.
- `completed_at` (ISO 8601 string or `null`) — when the plan reached
  `primary_state === "complete"`, sourced from the latest `progress.md`
  entry timestamp and falling back to the runner `run_finished_at`. It
  is `null` for non-complete plans and for complete plans with no
  recorded completion time. This additive field drives the completion
  timestamp shown in the queue/preview and the `sort=oldest` ordering
  for complete plans.
- `searchable_text` (string) — pre-lowercased, whitespace-collapsed
  concatenation of label, plan_dir, primary_state, recommended_action,
  urgency_group, activity_label, concise_reason, next_action_hint,
  detail, `"session <N>"`, pause_reason, pause_message,
  last_message_preview, run_id, harness, and discovery sources. The
  frontend's search input does a substring match against this field;
  callers should not re-normalize it.
- `recovery` (object) — compact Recovery Center summary from
  `WorkbenchRecovery.compact_payload()`. It includes `kind`, `title`,
  `summary`, `severity`, `recoverable`, `primary_action`,
  `artifact_count`, `command_count`, and `warning_count`. Queue/detail
  clients can render the compact summary immediately, then fetch
  `/api/recovery` for full evidence, commands, and artifacts.
- `review_resume` (object) — the same additive continuation projection exposed
  by `asl status --json`. Queue rows use it to distinguish a truly terminal
  complete plan from ready repair materialization/rereview, an insufficient
  absolute cap, or a malformed artifact that needs external correction. The
  compact and full Recovery Center payloads expose the identical object.

Urgency rank constants (lower = higher priority), mirrored from
`workbench_summary_model.URGENCY_RANK_BY_STATE`. The `activity_label` column shows
the literal UI badge string the queue renders for each state, so
external readers can predict row text without consulting
`workbench_summary_model.ACTIVITY_LABEL_BY_STATE`. A zero-pending, eligible review
continuation keeps the `recent` state but uses the distinct
`workbench_summary_model.REVIEW_CONTINUATION_ACTIVITY_LABEL` badge:

| Primary state     | urgency_rank | urgency_group | activity_label                |
| ----------------- | -----------: | ------------- | ----------------------------- |
| paused-for-reply  |           10 | needs-action  | Reply needed                  |
| failed            |           20 | needs-action  | Last run failed               |
| stale             |           30 | needs-action  | Stale state                   |
| invalid           |           40 | needs-action  | Plan unreadable               |
| verifying         |           50 | in-progress   | Verifying                     |
| running           |           60 | in-progress   | Active turn                   |
| recent (pending sessions) |           70 | ready         | Run finished, sessions remain |
| recent (zero-pending review continuation) | 70 | ready | Review continuation ready |
| ready             |           80 | ready         | Ready to run                  |
| complete          |           90 | complete      | All sessions complete         |

The exported Python constants (`workbench_summary_model.URGENCY_GROUPS`,
`workbench_summary_model.LAST_ACTIVITY_SOURCES`, `workbench_summary_model.URGENCY_RANK_BY_STATE`,
`workbench_summary_model.URGENCY_GROUP_BY_STATE`, `workbench_summary_model.ACTIVITY_LABEL_BY_STATE`, and
`workbench_summary_model.REVIEW_CONTINUATION_ACTIVITY_LABEL`) are the source of truth — keep
any external code that branches on these strings in sync with that module.
