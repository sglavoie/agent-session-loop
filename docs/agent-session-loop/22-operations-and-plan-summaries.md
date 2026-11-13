### Durable operation admission

Every mutation the supervision bridge submits passes one gate first.
`agent_session_loop.operation_store` keeps a per-plan journal at
`<state-dir>/<plan-hash>/operations.jsonl`, beside the redirect journal, and
holds an exclusive `flock` across read, decide, and append — so two bridge
sessions submitting at the same moment serialize there and exactly one is
admitted. The rules themselves live in `agent_session_loop.operation_rules`,
which is stdlib-only and imports nothing else from the package.

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

`agent_session_loop.run_actions.perform` is the single application
operation behind the bridge's `run.resume` and `question.reply`. It runs the same seven steps every time:
inspect the run through `intervention.inspect_run`, check freshness,
admit through the journal, consult the caller's guard, record dispatch,
carry the mutation out, and settle the row. Surface-specific code is
reduced to translating a refusal into that surface's error shape.
`agent_session_loop.action_rules` holds the stdlib-only half — the
action/method table, the launch-refusal classification, and the reading of
the plan's event log.

**A stale target is refused with or without an action token.** A request
that names a `run_id` or a `question_id` is making a claim about that run
or that question, and the claim is checked against what
`intervention.inspect_run` reports. A bridge caller must additionally carry a
fresh `action_token`. Either way, a target that has moved is refused before
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
it is `unconfirmed`.** `agent_session_loop.action_rules.launch_closure` draws that
line from the refusal reason plus whether a process was created, which is
the same distinction `expired` and `unconfirmed` draw one step earlier. A
guard refusal closes the row `cancelled` on the way past, so a withdrawn
submission stops holding the plan's run-start scope instead of blocking
every later one.

**A launched run gets its own session.** `plan_launch.launch_plan` passes
`start_new_session=True`, matching the `AbandonProcessGroup` the launchd
agents already set. A bridge client going away or a closing terminal each
signals a process *group*; without its own session the worker would be
killed mid-turn by an event that has nothing to do with the plan. The child
carries the invocation origin `plan-launch`.

### One path for a stop, an abort, and a correction

`agent_session_loop.control_service` is the single application service
behind the bridge's `run.stop_after_current`, `run.abort`, `run.redirect`,
`run.redirect_status`, and `run.recover_conversation`. Every control is checked, queued, and followed by
the same best-effort wakeup the CLI producer sends, through
`intervention.dispatch_control_command`; every correction is judged
against the redirect journal in one order. `agent_session_loop.control_rules` holds
the stdlib-only half — the control-to-queue-command table, what an abort is
about to mean, the projection window, and the narrowing every
client-chosen field goes through.

**How hard a caller is checked is a parameter, not a second
implementation.** `AUTHORIZATION_OPEN` queues a control for a plan whose
runner has not started yet, which is what the CLI does;
`AUTHORIZATION_STRICT`, which the bridge uses for every control, refuses
anything that is not about the run the caller was shown.
A correction always interrupts a turn, so it is always held to a fresh
`action_token` — a request without one is
refused `stale-action-token` rather than as a malformed body.

**Replay is judged before freshness.** The journal is asked what an
`operation_id` already names before the token is checked, because a correction that landed moves the plan, which moves the
token, which would make the retry of a lost response look like a stale
second correction. What is not short-circuited is the request itself: an
edited retry is `redirect-replay-conflict` with the conflicting field
named, because reporting an operator's edited words as a successful replay
of the words the worker actually got is the falsehood this path exists to
avoid.

**The seven interruption boundaries keep their distinctions.** A
correction persisted and unanswered reads `unanswered`; one handed to an
attempt that never receipted it reads `uncertain`, and neither is resolved
by guessing. Both reads are read-only by construction —
`recover-conversation` reports the relaunch and the corrections it would
inherit, and never performs it, because performing it would persist an
operator's one-use authorization inside a request that can be replayed.

**A refusal keeps the bridge's vocabulary.** The service raises what
happened and how retryable it is; the bridge turns that into one of its
versioned `ERROR_CODES`, with
any reason outside that closed enum travelling as `ERROR_INVALID_REQUEST`
carrying the durable reason in `details.reason`.

### Plan summary payload

Each plan summary in `asl plans --json` carries additive triage fields, and
the bridge's `runs.snapshot` projects its rows from the same summaries. All
fields are backwards-compatible — no existing key is renamed or removed.

- `urgency_rank` (int) — lower means higher priority; the bridge snapshot
  orders rows ascending by it. Operator-blocking states come first.
- `urgency_group` (string) — one of `needs-action`, `in-progress`,
  `ready`, or `complete`. Mirrors `plan_summary_model.URGENCY_GROUPS`.
- `activity_label` (string) — short status string for the row
  ("Awaiting reply", "Running", "Complete", …). Mirrors
  `plan_summary_model.ACTIVITY_LABEL_BY_STATE`.
- `concise_reason` (string) — one-line cause behind the current state
  (pause reason, last failure summary, etc.).
- `last_activity_at` (ISO 8601 string or `null`) — most recent
  meaningful activity timestamp.
- `last_activity_source` (string or `null`) — provenance of the
  timestamp. One of `active-verification`, `active-turn`, `turn-record`,
  `run-finished`, `run-started`, `lock-created`, `progress-journal`.
  Mirrors `plan_summary_model.LAST_ACTIVITY_SOURCES`.
- `completed_at` (ISO 8601 string or `null`) — when the plan reached
  `primary_state === "complete"`, sourced from the latest `progress.md`
  entry timestamp and falling back to the runner `run_finished_at`. It
  is `null` for non-complete plans and for complete plans with no
  recorded completion time. It is the completion time a client can show for a
  finished plan.
- `searchable_text` (string) — pre-lowercased, whitespace-collapsed
  concatenation of label, plan_dir, primary_state, recommended_action,
  urgency_group, activity_label, concise_reason, next_action_hint,
  detail, `"session <N>"`, pause_reason, pause_message,
  last_message_preview, run_id, harness, and discovery sources. A client
  that searches plans does a substring match against this field; callers
  should not re-normalize it.
- `recovery` (object) — compact recovery card summary from
  `RecoveryCard.compact_payload()`. It includes `kind`, `title`,
  `summary`, `severity`, `recoverable`, `primary_action`,
  `artifact_count`, `command_count`, and `warning_count`. The full card is
  `RecoveryCard.to_payload()`, described under the recovery card payload.
- `review_resume` (object) — the same additive continuation projection exposed
  by `asl status --json`. Clients use it to distinguish a truly terminal
  complete plan from ready repair materialization/rereview, an insufficient
  absolute cap, or a malformed artifact that needs external correction. The
  compact and full recovery card payloads expose the identical object.

Urgency rank constants (lower = higher priority), mirrored from
`plan_summary_model.URGENCY_RANK_BY_STATE`. The `activity_label` column shows
the literal label string each state carries, so
external readers can predict row text without consulting
`plan_summary_model.ACTIVITY_LABEL_BY_STATE`. A zero-pending, eligible review
continuation keeps the `recent` state but uses the distinct
`plan_summary_model.REVIEW_CONTINUATION_ACTIVITY_LABEL` label:

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

The exported Python constants (`plan_summary_model.URGENCY_GROUPS`,
`plan_summary_model.LAST_ACTIVITY_SOURCES`, `plan_summary_model.URGENCY_RANK_BY_STATE`,
`plan_summary_model.URGENCY_GROUP_BY_STATE`, `plan_summary_model.ACTIVITY_LABEL_BY_STATE`, and
`plan_summary_model.REVIEW_CONTINUATION_ACTIVITY_LABEL`) are the source of truth — keep
any external code that branches on these strings in sync with that module.
