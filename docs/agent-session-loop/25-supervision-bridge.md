## Supervision Bridge

`asl bridge serve --stdio --protocol 2` streams ASL's active-run
projection to an external supervision client over stdin/stdout and
accepts the intervention set an operator can already reach from the CLI
and the Workbench: reply, resolve, resume, stop-after-current, abort,
and a Workbench deep link. Every one of those goes through the same
`agent_session_loop.intervention` service layer the other two surfaces
use, so a rule can never hold on one surface and not another. Protocol 2
is the only supported version; requesting another — including the retired
protocol 1 — exits 2 with a stderr diagnostic and serves nothing.

### Compatibility is identity, not negotiation

The server-first `hello` carries a `contract_set_id`: the digest over the
canonical corpus that defines these frames, the lifecycle hook payload,
and the projection at once. One identifier pins every component version,
so the components cannot drift apart, and the frame is closed against
unknown properties so a fourth cannot be added without moving the
identifier. The hello also declares `handshake_required`, the ordered
method list, the event list, and the mutating subset of the methods —
protocol 1 published methods and events in one array, which left "can I
call this?" and "will I be sent this?" indistinguishable.

Mutations are gated behind `session.handshake`. It accepts only an exact
contract-set match together with a snapshot digest the bridge still
holds, and answers `{contract_set_id, handshake_id, mutations_enabled,
snapshot_digest}`. A foreign contract set is refused once as
`contract-set-mismatch` with `retryable` false: retrying cannot fix a
build-time disagreement, and there is no protocol-1 parser left to fall
back to. The refusal message is a fixed string rather than an
interpolation of whatever the client sent, because a compatibility reason
reaches an operator's screen verbatim. Reads survive a mismatch, so a
stale cockpit can still render what it has and explain itself. The one
exception is `workbench.ensure`, whose answer is mutation capability
rather than state (see Hello below).

A handshake against a projection that has since moved is answered rather
than refused: `mutations_enabled` is false and the response names the
digest to refresh to. A mutation attempted against a stale handshake
fails as `stale-snapshot`, retryable in the narrow sense that refreshing
and handshaking again is the fix. This complements the action token
rather than duplicating it: the token proves the *plan* has not moved
since the client read it, and the handshake proves the client read it
from a projection this bridge produced and still holds. The gate runs
before the request is parsed, so an unauthorized client is refused rather
than coached on its parameters, and it is enforced from the declared
mutating-method list rather than inside each handler, so a method added
to that list is gated by construction.

### Transport and framing

- NDJSON: one JSON object per line, UTF-8, newline-terminated.
- stdout carries protocol frames only. Diagnostics go to stderr.
- Maximum frame size is 1 MiB (`max_frame_bytes` in the hello).
- The server writes `hello` before reading any input.

### Hello

```json
{"type":"hello","protocol":2,"asl_version":"...",
 "contract_set_id":"asl-amx-1.<16 hex>",
 "capabilities":["session.handshake","runs.snapshot","question.get",
   "question.reply","question.resolve","run.resume",
   "run.stop_after_current","run.abort","workbench.ensure",
   "run.redirect","run.redirect_status","run.recover_conversation"],
 "events":["runs.changed","operation.updated"],
 "mutation_methods":["question.reply","question.resolve","run.resume",
   "run.stop_after_current","run.abort","run.redirect"],
 "handshake_required":true,
 "max_frame_bytes":1048576,"poll_interval_seconds":1.0}
```

Capabilities are ordered and additive. Detect a feature by membership,
never by position or list length. The list is derived from the registered
method handlers and event names rather than written by hand, so a name
cannot be advertised before something stands behind it. `mutation_methods`
is a subset of `capabilities` in the same order; a capability absent from
it is a read, and reads never need a current snapshot. One read is still
gated: `workbench.ensure` writes nothing, but it hands out an owner
Workbench token whose routes can mutate, so it is refused with the same
`contract-set-mismatch` an unhandshaken mutation gets until a handshake
has matched the contract set. It does not need `mutations_enabled`, so a
handshake against a moved projection still opens it.

### Requests and responses

A request is `{"type":"request","protocol":1,"id":"<non-empty>",
"method":"<name>","params":{}}`. `params` must be an object when
present. The response echoes `id` and carries `ok` plus exactly one of
`result` or `error`:

```json
{"type":"response","id":"req-1","ok":true,"result":{...}}
{"type":"response","id":"req-1","ok":false,
 "error":{"code":"unknown-method","message":"...",
          "retryable":false,"details":{"method":"runs.explode"}}}
```

Error codes are a closed set — branch on `code`, not on `message`:

| Code | Meaning | `retryable` |
| --- | --- | --- |
| `malformed-json` | Frame was not parseable JSON. `id` is `null`. | no |
| `invalid-request` | Missing/empty `id`, missing `method`, or non-object `params`. | no |
| `unsupported-protocol` | `protocol` is not 1. `details.supported` lists accepted versions. | no |
| `unknown-method` | Method not in the capability list. | no |
| `frame-too-large` | Inbound line exceeded `max_frame_bytes`. | no |
| `internal-error` | Projection raised. | yes |
| `mutation-failed` | A durable write or spawn failed. Nothing was applied. | yes |
| `workbench-unavailable` | The bridge could not start a Workbench server. | yes |

Mutations add the service layer's own refusal reasons, shared verbatim
with the CLI and the Workbench so a client sees one vocabulary:

| Code | Meaning | `retryable` |
| --- | --- | --- |
| `stale-action-token` | The row the request was built from no longer describes the plan. `details.expected_fingerprint` is the current token. | yes |
| `action-not-permitted` | The action is not in this run's current `actions`. | no |
| `no-active-run` | No live run holds the plan. | no |
| `run-mismatch` | `params.run_id` is not the current run. | no |
| `not-paused` | `run.resume` on a run that is not paused. | no |
| `no-open-question` | No question is waiting for a reply. | no |
| `question-mismatch` | `params.question_id` is not open on this plan. | no |
| `empty-text` | Reply text was blank or whitespace. | no |
| `question-body-too-large` | Body exceeds 256 KiB. `details.fallback` names `workbench.ensure`. | no |
| `missing-question-body` | The selected question carries no readable text. | yes |
| `invalid-plan` | The plan directory could not be canonicalized. | no |

None of these end the server loop. An oversize frame has its remainder
drained so the next line still parses. Only end-of-stream on stdin or a
broken stdout ends the process, and both exit 0.

### `runs.snapshot`

Returns the deterministic projection of every active or actionable plan:

```json
{"protocol":1,"rows":[...],"row_count":1,
 "warnings":[],"truncated":false,"digest":"<hex>",
 "projection":{"interval_seconds":1.0,"observed_interval_seconds":9.8,
   "build_seconds":9.8,"age_seconds":0.4,
   "staleness_bound_seconds":20.0,"failed_projections":0}}
```

Rows cover only these primary states, in Workbench urgency order:
`paused-for-reply`, `failed`, `stale`, `invalid`, `verifying`,
`running`. Healthy `ready`, `recent`, and `complete` plans are never
present. Each row carries `row_id` (a stable hash of `plan_dir` and
`run_id`), the normalized `plan_dir`, `label`, `run_id`, `session_id`,
`primary_state`, `urgency_rank`, `urgency_group`, `recommended_action`,
`activity_label`, `phase`, `harness`, `workspace_root`,
`registry_digest`, `repository_count`, `window`, run and activity
timestamps, `pause_reason`, `question`, `actions`, `disabled_reasons`,
`action_token`, and `warnings`.

`actions` lists exactly what this run permits right now, drawn from
`question.reply`, `question.resolve`, `run.resume`,
`run.stop_after_current`, `run.abort`, `workbench.ensure`, and
`run.redirect`.
`disabled_reasons` explains, per unavailable action, why. `action_token`
fingerprints the plan identity together with the run, question, and
permitted-action state the row was built from; a mutation must return it.

Human-readable strings come only from ASL's fixed activity vocabulary.
No free-form summary prose — reasons, details, message previews, pause
messages — is copied into a row.

`question` is `null` unless the run is waiting on an operator. When
present it carries `context_kind`, `question_id`, `question_ids`,
`unanswered_count`, and body-free timing and status metadata. **Question
bodies are never serialized.** A client that needs the text reads it
through `asl questions` or `asl inbox read`.

Per-field and aggregate bounds cap text, identifiers, paths, warnings,
question IDs, and row counts, and the snapshot is shrunk until it fits
the frame ceiling, so corrupt state cannot produce an unreadable frame.
A shrunk snapshot sets `truncated`.

`projection` describes the walk these rows came from, so a client can
label how stale they are instead of assuming the declared interval:

- `interval_seconds` — the declared floor, the same number `hello`
  advertises.
- `observed_interval_seconds` — the measured start-to-start cadence of
  the last two walks that published, never below the floor or the last
  published build.
- `build_seconds` — how long the walk that produced these rows took, so
  a row read at its start is already that much older than the frame.
- `age_seconds` — how long ago that walk landed, measured when this
  response was written rather than when the walk finished.
- `staleness_bound_seconds` — the sum of the three above: the oldest any
  row in this frame can be before the next walk replaces it.
- `failed_projections` — how many walks have raised this session. A
  nonzero count with a healthy `age_seconds` means rows are being served
  from the last walk that succeeded.

Every measured field except `failed_projections` describes the walk whose
rows are being served. A failed walk publishes nothing, so it bumps only
`failed_projections`: it never rewrites the cost or the cadence of the
snapshot that stays published, and `age_seconds` keeps growing from the
last successful walk.

The block is attached when a snapshot is served, not when it is built,
and it is excluded from `digest` — measured durations jitter, and a
corpus that did not change did not change.

### `runs.changed`

The server polls the projection at least once per second and emits an
event only when the snapshot digest actually changed:

```json
{"type":"event","seq":1,"event":"runs.changed","data":{"digest":"<hex>"}}
```

`seq` is monotonic across the session. An idle corpus produces no
events, and a digest already delivered through `runs.snapshot`
suppresses the next event for that same digest.

The one-second interval is a floor, not a promise. A projection walks
every discovered plan, so its cost scales with the corpus: several
hundred plans take seconds, not milliseconds. The walk therefore runs on
its own thread, and requests are answered from the last projection it
published while the next one runs beside them. Nothing on the protocol
loop waits for a walk: polling a ten-second projection on the read loop
left the server permanently mid-build, so it never returned to `select`
and no client ever got an answer.

The next walk is owed one interval after the last one *started*, not
after it finished. Counting from the finish added a second full build to
every cycle, so a ten-second walk observed change every twenty seconds
while `hello` advertised one; a corpus that cannot be walked within the
interval now slips by its overrun instead. `poll_interval_seconds` in
`hello` reports only that floor, so a client that needs the real bound
reads the measured one off the snapshot rather than inferring it.

Before the first walk lands there is nothing truthful to serve — an
empty projection would claim an empty corpus and re-enable actions
against rows nothing has read — so a `runs.snapshot` arriving on a cold
bridge is refused with `internal-error` and `retryable` set. It is not
held until the walk lands: the protocol loop is single-threaded, so
waiting would block that response and every frame behind it for the
whole corpus walk. A client re-asks and gets rows as soon as the first
walk publishes, which is what the Agent Mux daemon already does inside
its snapshot budget.

A walk that fails owes a full second before the next attempt, whether it
failed on the worker thread or on the loop's own poll. A failure
publishes nothing, so the deadline of the last walk that succeeded is
already in the past: without a deadline of its own the loop selected
with timeout zero and retried the same failing walk immediately. During
that second the bridge keeps answering, and a `runs.snapshot` is served
from the last projection that succeeded with an honest `age_seconds`
rather than refused.

### `question.get`

Fetches the one question body a snapshot deliberately withholds.
`params` are `plan_dir` and an optional `question_id`; omitting it
selects the run's current question. A live wait resolves to the mailbox
record; a paused run has no mailbox question, so the synthetic one is the
harness's own last message, exactly what `asl questions` resolves. The
result carries `question_id`, `context_kind` (`waiting` or `pause`),
`body`, `body_bytes`, and a fresh `action_token`.

Bodies are bounded at 256 KiB. A larger one is refused with
`question-body-too-large` and `details.fallback` naming
`workbench.ensure`, because the Workbench already streams artifacts of
any size and a truncated question is worse than an explicit redirect.

### Mutating methods

`question.reply`, `question.resolve`, `run.resume`,
`run.stop_after_current`, `run.abort`, and `run.redirect` all take
`plan_dir`, `operation_id` (client-chosen, non-empty), and
`action_token`, plus optional `run_id` and `question_id` for exact-match
validation. `question.reply`, `run.resume`, and `run.redirect` take
`text`; `question.resolve` takes `action` (`continue` or `pause`) and an
optional `note`. `run.redirect` has its own section below: it is the only
mutation aimed at a turn that is still running rather than at one that
has already stopped.

On a run holding several open questions, `question.reply` and
`question.resolve` close whichever `question_id` the caller names, as long
as it is still in the run's unanswered set; omitting `question_id` closes
the run's primary question. Because `action_token` fingerprints that
unanswered set, every accepted closure invalidates the token, so a client
closing questions in sequence must re-read the token — from a fresh
`runs.snapshot` row or from the `question.get` result for the next
question — before submitting again.

The bridge re-reads and re-validates the plan immediately before it
writes anything, so a request built from a row that has since moved is
refused rather than misapplied. Bridge callers are additionally held to
strict active-run matching even where the control CLI deliberately
accepts a command for a runner that has not started yet: a supervision
client acted on a row it was shown, so a mismatch is a bug to surface.

Control commands use `operation_id` as the durable control-queue command
ID, and `run.abort` uses ASL's default 120-second abort grace. `run.resume`
spawns `asl run --resume --reply-stdin`, which rehydrates the harness,
model, effort, state directory, and workspace binding the original run
persisted, and streams the reply through stdin so it never appears in
argv. Replaying the same `operation_id` returns the stored receipt
without spawning again.

### Operations and `operation.updated`

A mutation returns as soon as ASL's own record is durable, which is
earlier than the moment the runner acts on it:

```json
{"operation_id":"op-1","method":"question.reply","status":"accepted",
 "plan_dir":"/plans/alpha","row_id":"<hex>","run_id":"...",
 "question_id":"...","closure_id":"...","command_id":null,"detail":null}
```

The bridge then watches ASL's records and emits a body-free
`operation.updated` event carrying the same payload with a terminal
`status`:

| Status | Settled by |
| --- | --- |
| `consumed` | A mailbox receipt for the closure, a `run_control` command-observed event, or a `reply_provided` record naming the reply a resume carried. |
| `rejected` | A control command left the queue with no delivery receipt and the run is gone. |
| `expired` | No outcome was observed within the 15-minute receipt window. |
| `superseded` | A resume's plan is still paused under a different run. |

A resume is settled by a run's own record of reading the reply, never by
the plan leaving the paused state. The pause lifting is a fact about the
plan and not about the operator's words: a run that resumes, reads
nothing, and pauses again on its next question would look identical.

The window holds at most 64 operations. Count-based eviction only ever
drops already-settled operations, so a burst of requests cannot evict an
in-flight resume and let it spawn twice; anything unsettled leaves only
through the TTL. It is a receipt window, not a journal: a long-lived
bridge never accumulates operator history.

### `workbench.ensure`

Takes `plan_dir` and an optional `focus` (`reply`, `verification`,
`active`, or `events`) and returns a deep link. It requires a handshake
that matched the contract set — the link's token is the Workbench owner
token, which passes its unlock and launch routes — and is refused
`contract-set-mismatch` before one, exactly as a mutation would be:

```json
{"plan_dir":"/plans/alpha","row_id":"<hex>","focus":"reply",
 "url":"http://127.0.0.1:53311/?token=...#plan?dir=%2Fplans%2Falpha&focus=reply"}
```

The first call lazily starts one Workbench server bound to `127.0.0.1`
on an ephemeral port with a freshly generated token; later calls reuse
it, and it is shut down with the bridge. The token rides in the query
string because a fragment is never sent to the server, and the plan
selection rides in the fragment because the Workbench front end routes on
it.

### `run.redirect`

Submits one urgent correction against the turn the run is on *now*,
rather than waiting for it to finish and pause. It is a mutating method,
so it takes `plan_dir`, `operation_id`, and `action_token` like the
others, plus `text`, an optional `expected_target`, and an optional
`wait_seconds`:

```json
{"plan_dir":"/plans/alpha","operation_id":"amx-op-9c31aa",
 "action_token":"<hex>","text":"...",
 "expected_target":{"run_id":"...","session_id":"9","turn_index":1,
   "harness":"claude"},
 "wait_seconds":5.0}
```

`expected_target` is optional and load-bearing when supplied. A client
that rendered a row and then waited for a human is exactly the client
whose turn may have ended in between; naming the turn turns that race
into a `stale-target` refusal instead of a correction delivered to
whatever the runner moved on to. `wait_seconds` defaults to 2 and is
capped at 30, because the bridge answers one frame at a time and a wait
one client chooses is a wait every other client on the session pays.

The receipt is body-free — the correction's words appear in no frame the
bridge can emit, and `feedback_digest` with `feedback_bytes` is what lets
a client prove *which* correction a record is:

```json
{"operation_id":"amx-op-9c31aa","plan_dir":"/plans/alpha",
 "row_id":"<hex>","run_id":"...","outcome":"accepted","reason":null,
 "target":{"run_id":"...","session_id":"9","turn_index":1,
   "harness":"claude"},
 "waited_seconds":0.053,"replayed":false,"journal_complete":true,
 "operation":{"operation_id":"amx-op-9c31aa","status":"accepted",
   "stage":"interrupting","stage_description":"...",
   "journal_state":"accepted","reason":null,"target":{...},
   "continuation":null,"continuation_recovered":false,
   "feedback_digest":"<32 hex>","feedback_bytes":37,
   "created_at":"...","expires_at":"..."}}
```

`operation` is re-read from the redirect journal after the submission
rather than built from the submission in hand, so the status this client
is told and the status the next reader of that journal computes are the
same projection of the same bytes.

Replay is served from the journal **before** the freshness proof is
checked, and that ordering is the whole reason the method can be
retried. A correction that landed moves the plan, which moves the action
token, which would make the retry of a request whose answer was lost in
transit look like a stale second correction. Asking the journal first
makes the `operation_id` the client already holds the answer:
`replayed` is true, `outcome` is null — a replay submitted nothing, so
it has no outcome of its own — and `operation` carries the durable
truth.

What is not short-circuited is the request itself. A replay must present
the same request identity the journal recorded — the same `text`, and a
matching turn if it supplies `expected_target` — which is the rule
`submit_request` applies to a first submission, applied again here.
`text` is therefore required even on a retry; `run.redirect_status` is
the body-free way to ask after an id. A resubmission that changed either
half is refused `invalid-request` with `details.reason:
"redirect-replay-conflict"` and `details.conflict` of `feedback` or
`target` (the closed `ErrorCode` enum is versioned, so the durable
reason travels in `details`), and the recorded request is left exactly
as it was. The same id can never carry different text, and now cannot
claim to.

### `run.redirect_status`

Projects this plan's durable redirect state without writing anything.
`params` are `plan_dir` and an optional `operation_ids` array to select a
subset. It is a **read**: it needs no handshake, because the client most
in need of it is the one that just lost its own record of what it
submitted — including a client whose contract set no longer matches and
which therefore cannot mutate at all.

The result carries `journal_path`, `owner` (`live`, `gone`, or
`unknown`, read from the lock alone), `owner_run_id`, `live_target`, the
`operations` window, `uncertain_operation_ids`, `journal_complete`,
`torn_tail`, `unreadable_lines`, `truncated`, `checked_at`,
`conversations`, and a fresh `action_token`.

Each operation reports both a `status` and a `stage`. `status` is the
correction's durable standing recomputed from the journal — `submitted`,
`unanswered`, `accepted`, `delivered`, `acknowledged`, `rejected`,
`expired`, `uncertain`. `stage` is the same correction as an operator
reads it, derived from that status *plus* the turn the run is on now —
`submitted`, `unaccepted`, `interrupting`, `stopped`, `resuming`,
`acknowledged`, `rejected`, `blocked`, `uncertain`. A client renders the
stage and reconciles on the status. Neither is `OperationStatus`: that
describes a bridge-session receipt, and a redirect outlives the session
that submitted it, so its status has to survive a restart the receipt
does not.

`journal_complete` is the honesty flag and `truncated` is its projection
counterpart: the first says a record may be missing from the source, the
second that one is missing from this frame. The window holds at most 64
operations and truncates from the front, so the most recent corrections
are the ones a client is guaranteed to see. `uncertain_operation_ids`
names the corrections that reached an attempt which never receipted them
— the one state a client must never act on automatically.

Because status is reconstructed from the journal rather than from the
bridge session's in-memory receipt window, a second bridge process
started after a restart answers identically about a correction the first
one submitted.

### `run.recover_conversation`

Reports which manual recovery a pause is waiting on and how to authorize
it. `params` is `plan_dir`. Also a read, and deliberately only a read:
performing the recovery here would persist the operator's one-use
authorization in the one place it must never live — a request that can be
replayed.

```json
{"plan_dir":"/plans/alpha","row_id":"<hex>","run_id":"...",
 "recovery_required":true,
 "pause_reason":"conversation-recovery-required",
 "decision":"fresh-conversation","auto_resume":false,
 "authorized_by":"operator",
 "inspect_command":"asl control recover-conversation ... --json",
 "authorization_command":"asl run ... --resume --fresh-conversation",
 "one_use":true,"note":"...","conversations":{...},
 "retained_operations":[...]}
```

`decision` is `fresh-conversation`, `predecessor-not-stopped`, or null
when the run is not paused or is paused on something an operator does not
decide. `predecessor-not-stopped` has no authorization command at all, so
`authorization_command` is null, `one_use` is false, and `note` is null —
telling an operator that an absent authorization is one-use would read as
an invitation to find it. `pause_reason` is reported whether or not it
maps to a decision, because "paused, and no manual recovery is what it is
waiting on" is an answer while an unexplained null is a second question.
`retained_operations` are the corrections a fresh start would still
carry, listed body-free like everywhere else.

Both commands and the pause-to-decision mapping are derived from
`intervention` and `redirect_cli` rather than re-assembled here, so this
projection and `asl control recover-conversation` cannot hand an operator
two different answers about the same pause.
