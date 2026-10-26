## Live Mailbox and Future-Turn Model Control

ASL's optional mailbox lets a supervisor steer a running worker without
changing the selected session or converting every question into a terminal
pause. It adds exactly two files below the canonical per-plan state directory:

- `mailbox.jsonl` is a non-destructive JSONL log containing `guidance`,
  `question`, `reply`, `resolution`, `notice`, `supervision-end`, worker `ack`,
  `wait-start`, `wait-end`, and `receipt` records. New appends use schema v3
  while reads remain compatible with legacy v1/v2 and mixed logs. Physical 1-based line
  number is the durable sequence, including
  malformed lines. Appends are locked, the file is never truncated or rotated,
  and an append that would exceed 4 MiB
  fails atomically with `mailbox-full`. The `full` field in `asl status --json`
  reports exhaustion rather than exact cap equality: it becomes true when
  fewer bytes remain than the smallest record produced with normal generated
  metadata. Individual larger appends can fail before that exhaustion
  threshold. The final 512 KiB below the cap is a lifecycle reserve. Ordinary
  records stop at its boundary; closures retain 288 KiB of nested headroom for
  wait termination and receipts, and wait termination retains the final 96 KiB
  for receipts. Thus ordinary exhaustion cannot strand an active question,
  its `wait-end`, or its consumption evidence. The
  `accepts_questions` field in `asl status --json` reports that shared
  non-closure cap with the same exhaustion probe `full` applies to the file
  cap. `accepts_replies` and `accepts_resolutions` separately report whether
  capacity remains for each minimal valid closure. `full` itself keeps its
  file-faithful physical-cap meaning; it does not incorporate either the
  per-kind reserve or the larger minimum closure sizes. A question is closed
  logically by its first accepted reply or resolution; the question and every
  accepted closure record remain append-only history, while losing concurrent
  appends fail without writing. ASL does not rotate, compact, or physically
  delete mailbox records. The question body a
  waiting runner publishes is itself bounded: at
  most the first 16,000 characters of the last-message artifact, with longer
  messages truncated deterministically and given a footer naming the on-disk
  artifact path that holds the full text.
- `mailbox-lease.json` is a separate, locked, fixed-size 512-byte supervisor
  lease. It is fresh for 90 seconds after its UTC `updated_at`. Supervisor
  long-polls and the Autopilot renew it every 30 seconds; stopping supervision
  leaves the file in place to expire naturally.
- `mailbox-supervision-<supervision-id>.json` is one small resumable cursor per
  supervising launch, written only by `asl outbox supervise`. It holds that
  launch's `next_since`, never regresses, restarts at 0 whenever it is
  unreadable or belongs to another launch, and is pruned after seven days
  without an update. Losing it costs a replay, never correctness.

All three files are created and repaired to mode `0600`. They live under
`<state-root>/<plan-path-sha256>/`; they are not stored in the plan directory
or repository.

A `supervision-end` record is the mailbox's terminal signal for one supervision
lifetime: the Autopilot helper process that was supervising this plan has
exited. It ends the helper's lifetime and does not prove the supervised run is
quiescent. Most terminal reasons do leave the `asl run` child finished, but
`worker_hung` deliberately stops the helper without killing or waiting on that
child, which stays alive holding the plan lock and can publish
questions and notices after the terminal record. The runner may already have
made one SIGTERM-only attempt against identity-verified deepest leaves of a
`result_received_process_alive` tree; automatically recovered attempts clear
the hang and continue the run, while `worker_hung` carries the unresolved
attempt under `result.hang.hang_suspected.recovery` and authorizes no stronger
signal. A reader that needs to know whether work is
still running asks `asl status <plan-dir> --json`; the record answers only
whether this supervision is over. Beyond the common `id`, `kind`, `created_at`,
and `version` fields it carries a required non-empty `text` holding the helper's
raw exit reason, a required `outcome` from the closed enum `complete`, `paused`,
`stopped`, `blocked`, `error`, an optional `run_id` validated only when
present, and an optional `supervision_id` — a UUID naming the supervising
launch that ended, rejected as `invalid-supervision-id` when it is present and
malformed. The kind is schema v3 only, so a v1 or v2 line claiming it is rejected
as an unknown kind for its version. Only the Autopilot helper writes the record,
at most once per helper process; the helper attaches no `run_id`, because one
helper supervises several sequential `asl run` children and a single run id
would be ambiguous.

The `outcome` also states what the helper's stdout is worth. On every outcome
except `error`, the record is appended only after the complete `--json` result
has been written and flushed, so an agent woken by one of those records can
parse that result immediately. `error` is the abnormal-exit outcome and carries
no stdout guarantee: a raised exception or a termination signal can end the
helper before the write, mid-write, or after the flush but before the record,
so the result may be absent, truncated, or complete but unproven. A supervisor
woken by its own `error` record must therefore decide from the record alone —
the termination detail is in `text` — and recover run state from
`asl status <plan-dir> --json` rather than parsing helper stdout. `error` never
comes from an ordinary terminal reason, so the outcome alone separates the two
cases.
The helper maps its raw reason onto the enum — `complete`/`review_findings` to
`complete`, `paused`/`needs_agent_reply` to `paused`, `max-sessions`/`stopped`
to `stopped`, every other terminal reason to `blocked`, and a raised exception
or termination signal to `error` — while the raw reason stays readable in `text`.
The runner deliberately does not emit one: a single supervised session launches
several sequential `asl run` children, so a per-run signal would wake the
supervisor at every chunk boundary and cost an agent turn each time; only the
helper's own exit means supervision is over. The record appends at the full
file-cap tier alongside `receipt`, so an otherwise full mailbox still accepts
the terminal signal rather than stranding a supervisor in the dead wait the
record exists to end. `--no-mailbox`, a SIGKILLed helper, and a machine crash
leave no record at all; those cases keep the existing stdout-tail fallback.

Every helper launch carries a `supervision_id`: the launching agent passes
`--supervision-id UUID` to the helper, and a launch that omits it gets a freshly
minted one reported back in the helper's `--json` result. Because helpers may
attach to an already-active matching run, two helper lifetimes can overlap on
one plan, and a physical sequence baseline captured at launch cannot tell a
still-alive older helper's later record from this launch's own exit. The
identity can: a reader treats only the record carrying its own launch identity
as terminal. Records left by pre-correlation helpers carry no `supervision_id`
and are therefore never another launch's terminal signal.

### Activation and precedence

A direct `asl run` resolves mailbox activation once, before its first turn. An
existing `mailbox.jsonl` or a fresh supervisor lease enables the protocol.
`asl run --no-mailbox` always wins and disables prompt guidance, mailbox
polling, and live question/reply continuation even when either file exists.
That flag is part of resumable run configuration, so `asl run --resume`
preserves the opt-out. The Autopilot establishes its lease before launching or
attaching unless its command includes `--no-mailbox`.

Mailbox presence alone continues to activate prompt guidance, but a
reply-capable pause starts a live question/reply wait only while a fresh
supervisor lease exists. Without one, the runner records the ordinary terminal
pause immediately instead of waiting for a reply that no supervisor can send.

Mailbox or lease read/write failure never makes an implementation run fail.
Activation and turn-start guidance reads degrade to the ordinary non-mailbox
path; a question that cannot be appended or observed falls back to the existing
terminal pause/resume workflow. A stale lease alone does not activate a direct
run, but an existing mailbox log does, so unacknowledged guidance intentionally
survives sessions and later runs at the same canonical plan path.

Supervisor guidance has deliberately narrow precedence. It may change
implementation choices inside the selected session, but it cannot override the
fixed session, its scope, completion contract, safety requirements,
runner-owned files, or runner-owned mutation rules. At each turn start the
runner snapshots all pending guidance but renders at most the newest 20 pending
guidance records. The complete pending-guidance subsection, including its
notices and delimiters, has a 65,536-byte UTF-8 rendering budget. If either
bound omits records, the packet reports the omission, states that the records
remain pending and unacknowledged, and repeats the shell-safe `asl inbox read`
command. Records omitted by the byte budget are dropped from the prompt in
full rather than emitted as clipped JSON, and the notice identifies each
omitted record by physical sequence and message ID. A record that cannot fit
within the complete subsection on its own is omitted first. If the remaining
records exceed the budget together, the runner then evicts the oldest remaining
record until the subsection fits, predictably preserving the newest records
that fit. Every rendered record remains one JSON object between fixed
delimiters; newlines and prompt-like bodies remain JSON escaped. If no record
fits, the packet labels the delimited block as intentionally empty instead of
promising records below it. The worker must then run `asl inbox read` to merge
newer guidance and acknowledge the snapshot.

The text form of `asl inbox read` applies the same newest-20-record and
65,536-byte complete-section limits. It reports record-cap and byte-budget
omissions in-band using message sequence/ID references; an extreme identity
list is compacted with its exact omitted count and boundary identities rather
than making the output unbounded. The command still reads one complete,
immutable pending snapshot. Unless `--peek` is supplied, its acknowledgement
covers that full snapshot through the highest pending sequence, including
records omitted only from text rendering. With `--peek`, or if the
acknowledgement append fails, omitted records remain pending. Guidance appended
concurrently after the immutable read also remains pending. The `--json`
payload is not rendering-truncated and continues to return the complete
snapshot.

### Guidance delivery

Appending guidance and delivering it are different events, and the mailbox
records them separately. A `guidance` record that no delivery record speaks
about is `queued`: durable, and nothing has reported acting on it. Every
delivery attempt appends a `guidance-delivery` record naming the guidance, the
channel, the turn, the harness, and a stable reason — never the body.

The three harnesses reach different channels, and the ledger says which:

- **Codex** is steered in-turn over the app-server's `turn/steer`, which is
  accepted or rejected by the protocol itself. A success records
  `delivered-live`.
- **Claude** is steered over two routes at once. The guidance is written to the
  turn's stdin message stream, where the CLI queues it as a follow-up turn, and
  it is placed in a turn-scoped spool that an ASL-owned `PreToolUse` hook
  injects as `additionalContext` before the model's next tool call. A
  successful stdin write is *not* a delivery and is never recorded as one: the
  attempt records `deferred` with reason `awaiting-confirmation` and stays
  open. It becomes `delivered-live` only on evidence — the CLI echoing the
  message back on its output stream as a protocol acknowledgement, or the hook
  leaving a receipt saying it injected the guidance. The echo has to be *that*
  message: echoes are matched against the messages ASL wrote to this turn's
  stdin, by whitespace-normalized body equality and in write order, and each
  echo is spent on at most one of them. So the turn's own prompt claims its own
  echo however late it arrives, and neither a prompt nor any other message that
  merely happens to contain a short guidance phrase can acknowledge guidance
  the model has not been given. The expectation is published before the bytes
  move and withdrawn if the write or flush fails, because the output-processing
  thread can observe the echo while the write is still returning: recorded
  afterwards, that echo would match nothing and the guidance would stay
  unacknowledged for the rest of the turn. If the turn ends first,
  the process exits, or stdin is refused, the guidance stays deliverable and
  the next turn carries it.
- **Copilot** runs one prompt per process and its protocol has no same-turn
  steer. The only in-turn control it offers is cancellation, which is never
  used for steering: cancelling a turn would destroy the work the guidance was
  meant to redirect. An attempt during an active turn records `deferred` with
  reason `unsupported-active-steer`. The guidance then rides the next turn's
  prompt, and `delivered-next-turn` is recorded only once that prompt has been
  accepted — that is, once the process carrying it has started.

A worker's own `asl inbox read` records `delivered-by-read` on the `read`
channel for each guidance in the snapshot it was shown.

`awaiting-confirmation` is the one deferral reason that is not a verdict: it
means the attempt is still running, so the delivery spine retries the same
attempt for the rest of the turn and the ledger records at most one entry per
(guidance, state, reason) per turn. Every other reason ends that turn's
attempt. Guidance is advisory throughout: a deferral never stops a turn, and
enforcement remains the job of guards, not guidance.

`asl status --json` projects the newest 20 guidance records under
`mailbox.guidance`, each with its `state` and its `latest_attempt`
(delivery id, state, reason, channel, harness, run/session/turn identity, and
the receipt timestamp). Bodies never appear there.

### Mailbox commands

The eight mailbox commands share normal plan-reference, `--state-dir`, and
`--json` behavior:

- `asl inbox guidance` appends supervisor guidance from exactly one of `--text`
  or `--text-stdin` and renews the supervisor lease first. The append and the
  delivery are separate facts: the exit code follows the append alone, and
  `delivery_state` reports what became of the guidance. Without
  `--wait-delivered` nothing is observed and the text form says "Guidance
  queued (delivery not observed)". With it, the command waits up to
  `--wait-timeout` seconds (30 by default; `--wait-timeout` requires
  `--wait-delivered`) for a delivery record naming the turn that was live when
  the wait started, and reports either "Guidance delivered live …" or
  "Guidance queued but not delivered …" with the reason. An
  `awaiting-confirmation` record never ends the wait: it says the attempt is
  still running, not that it failed.
- `asl inbox reply` atomically appends the first reply to `--to QUESTION_ID`.
  Concurrent later replies fail with `question-already-answered`; a missing
  target fails with `question-not-found`. `--wait-consumed` optionally waits
  for a worker/runner receipt, with `--wait-timeout` defaulting to 90 seconds;
  a receipt timeout leaves the successful append `ok: true`,
  `delivery_state: accepted`, `consumed: false`, and `timed_out: true`;
  `waited` says whether a consumption wait ran at all. Unlike a resolution, a
  reply needs no active run: an async answer stays in the mailbox for a later
  turn to collect. Only the run that owns the question writes a receipt, so
  when no active run exists, or the question belongs to another run,
  `--wait-consumed` returns at once with `waited: false` and
  `unconsumable_reason` (`no-active-run` or `question-not-open`), which is
  `null` otherwise.
- `asl inbox resolve` atomically closes a live-run question without fabricating
  reply text. `--action continue` directs a worker to proceed without treating
  an optional `--note`/`--note-stdin` value as an operator answer;
  `--action pause` directs a raw worker wait to emit `RUNNER_PAUSE: blocked`
  and makes a runner-owned wait preserve its original ordinary terminal pause.
  Resolution is accepted only when authoritative status identifies an active
  run and the question belongs to that run. It supports the same
  `--wait-consumed` and `--wait-timeout` consumption contract as replies.
- `asl inbox read` returns pending guidance and, unless `--peek` is supplied,
  appends an acknowledgement through the last guidance sequence in that
  complete snapshot, even if text-mode rendering omits records under its shared
  count or byte budget. If that acknowledgement cannot be appended because the
  mailbox is full or unwritable, the read still succeeds and returns the
  guidance with `acknowledged: false` plus an `acknowledgement_error`; the
  guidance remains pending for later runs. A non-`--peek` read also appends one
  `delivered-by-read` delivery record per guidance in the snapshot, on the
  `read` channel and attributed to the active worker turn, and returns them as
  `delivery_receipts`. That is what distinguishes an explicit read from the
  legacy cumulative acknowledgement, which remains honoured as read evidence
  but names no turn. With no authoritative active turn no receipts are written
  and `delivery_receipts` is empty; the acknowledgement still stands.
- `asl inbox acknowledge` is the worker's receipt for the operator corrections
  its own attempt carries. `--attempt TOKEN` is the token that attempt's prompt
  rendered; `--redirect-id` may be repeated, and omitting it receipts exactly
  what the journal records this attempt as carrying. Each correction is refused
  or receipted on its own — an unknown id costs only itself — but a token that
  does not name the live attempt refuses every one of them, because the token is
  the claim being checked. The refusals are `redirect-attempt-mismatch` for a
  token that names another attempt (a stale worker from the interrupted turn, or
  a transcript replaying an older prompt), `redirect-no-live-attempt` when no
  authoritative active turn exists to check the token against,
  `redirect-not-carried` and `redirect-continuation-mismatch` when the journal
  did not record this attempt as the correction's carrier, and the journal's own
  `unknown-redirect-request`, `redirect-already-settled`,
  `redirect-request-expired` and `redirect-not-accepted`. The command exits
  non-zero when anything was refused. Repeating a receipt appends nothing and
  reports `repeated: true`. No payload ever carries the correction's text, and
  nothing waits on the result.
- `asl ask` appends a worker question, given as positional `TEXT` or read
  from standard input with `--text-stdin`, or resumes an existing one with
  `--question-id`. `--wait` polls for the first reply or resolution for
  90 seconds by default;
  `--wait-timeout` accepts 0 through 1800 seconds and requires `--wait`.
  This bounds unsupervised time: a fresh supervisor lease extends the
  deadline while a supervisor collects a decision. A missing, stale, or
  unreadable lease stops extending it, as does an unreadable mailbox.
  Zero performs one poll without a lease extension.
  Supplying `--wait-timeout` alone fails with `reason: invalid-timeout`. An
  unclosed bounded wait returns success with `timed_out: true`, allowing the
  worker to reuse the same question ID or emit `RUNNER_PAUSE:` as a fallback.
  JSON keeps `answered` and `reply` reply-only while adding `resolved`,
  `resolution`, and `pause_required`; either closure keeps `timed_out: false`.
  The wait polls mailbox
  metadata every two seconds and re-parses the append-only log only when its
  metadata changes.
  New waits use mailbox schema v3: the question and first `wait-start` are
  appended atomically, resumed waits append another `wait-start`, and timeout
  appends `wait-end` under the same mailbox lock used by closures. A reply or
  resolution to a v3 `delivery_mode: wait` question is accepted only during an
  active wait; a late closure fails with `question-not-waiting`. When `asl ask`
  observes a closure it atomically appends a `receipt` and closure `wait-end`.
  Historical v1/v2 and v3 `delivery_mode: async` questions retain their
  asynchronous closure behavior.
- `asl notify` publishes a nonblocking worker notice from exactly one of
  positional `TEXT` or `--text-stdin`. Publication requires authoritative
  status to identify a live, unpaused, unfinished run with an active lock and
  an exact current session/turn. Missing, ended, or incomplete active-turn
  state fails with `no-active-run`. Notices never establish a supervisor lease,
  open a reply wait, or change question and lease projections; append failures
  remain ordinary command errors. A successful append also makes best-effort
  body-free `mailbox`/`notice-published` activity available to `asl tail`; event
  I/O failure never changes the successful notice result.
- `asl outbox wait` lets a supervisor long-poll after the stateless physical
  `--since` cursor. `--include questions` is the compatibility-preserving
  default; `--include notices` wakes only for notices, and `--include all`
  wakes on questions, notices, or the terminal `supervision-end` record. JSON
  always returns separate `questions`, `notices`, and `supervision_ends` arrays
  plus the `supervision_ended` scalar and the selected `include` mode. The third
  array and the scalar are emitted in every include mode, staying empty and
  `false` under `--include questions` and `--include notices`, so a supervisor
  can branch on one field without first inspecting `include`.
  `--supervision-id UUID` scopes that scalar to one supervising launch:
  `supervision_ended` is then true only for a returned record whose
  `supervision_id` matches, and the echoed `supervision_id` key reports the
  filter in effect (`null` when none was supplied, which keeps the original
  behavior of treating any terminal record as terminal). A record from another
  launch is still returned and still advances `next_since`, so an overlapping
  helper's exit moves the cursor without ending this launch's wait loop. A
  malformed value fails with `invalid-supervision-id`.
  Its default timeout is 300 seconds,
  its maximum is 1800, and `next_since` advances through every physical mailbox
  line on a return or timeout, so filtered records, guidance,
  acknowledgements, and malformed records cannot replay.
  It polls mailbox metadata every five seconds and likewise re-parses only
  after the log changes.
  When a returned question is still open, the command persists the exact
  run-scoped open set in the v2 supervisor lease and emits one best-effort,
  body-free `AgentSessionLoopMailboxQuestion` Agent Mux handoff for that
  actionable set. Its public fields are `run_id`, `state_path`, latest
  `question_id`, `question_created_at`, `unanswered_questions`,
  `delivery_state: open`, the latest question's physical `source_sequence`,
  `mailbox_projection_sequence`, `emitted_time`, and the stable
  `<run-id>:mailbox-question:<question-sequence>` source event ID. It carries
  no question, reply, resolution-note, or notice body. Closed/history-only
  records never emit the marker. Projection-write, Agent Mux discovery,
  delivery, timeout, or acknowledgement failure is fail-open: the normal text
  and JSON results are unchanged, including the JSON schema.
  A wake that carries only `supervision-end` records emits no Agent Mux handoff
  and writes no lease question projection, because both are driven by the
  returned open questions alone: the lease keeps its `run_id`,
  `projection_seq`, and `unanswered_questions` unset. A batch holding both a
  question and the terminal record still projects and emits for the question.
  Text-mode notice output renders at most the newest 20 selected notices under
  a 65,536-byte complete-section budget, and selected `supervision-end` records
  render under those same two bounds; omitted records are identified by
  sequence and message ID, while JSON remains complete. Under `--include all` an
  empty timeout reports `No questions, notices, or supervision ends after seq
  N; timed out.`
- `asl outbox supervise` is the supervision-loop reader. It wakes only on
  questions and `supervision-end` records, requires `--supervision-id UUID`,
  and keeps no `--since` or `--include` option: the cursor is persisted per
  launch in `mailbox-supervision-<supervision-id>.json` beside the mailbox, so
  the identical command is reissued every iteration. A missing, corrupt,
  mismatched, or out-of-range cursor file is repaired by restarting at 0, and
  a never-regressing write keeps a concurrent reader from rewinding it.
  Cursor files untouched for seven days are pruned on each call. `--timeout`
  defaults to 550 seconds with the same 1800-second maximum. Non-actionable
  wakes — another launch's terminal record, or a batch whose questions have
  all been closed — are absorbed internally against a single deadline and
  never reach stdout, so the cursor still advances past them. Text output is
  exactly one token, always with exit code 0: `IDLE` when the deadline passed
  with nothing actionable, `ACTION questions=<n>` with the framed open
  questions, `DONE outcome=<outcome> seq=<n>` with this launch's own
  terminal record framed below it, or `LOST reason=<reason>` when this launch's
  supervising helper is gone. `LOST` is decided by probing the `(pid, process
  birth time)` identity the Autopilot helper records before starting its child:
  a recorded identity whose process has died is
  `helper-exited-without-record`, while a launch that recorded no identity is
  `helper-never-started` only after a 120-second grace window and only when no
  lock or fresh runner state shows a run holding the plan, so a plain
  `asl run` supervised without the helper stays `IDLE`. A question or terminal
  record on the same pass always outranks it. The framed text names the cause,
  the run evidence (lock, `run_id`, runner state freshness, session counts),
  the active turn, and `asl status` as the next step. `--json` returns the
  ordinary wait payload plus `state`, `cursor_path`, and `since`, plus
  `lost_reason` and a `run_evidence` object (`lock_active`, `lock_pid`,
  `run_id`, `runner_state` (`fresh`, `absent`, `unreadable`, `finished` or
  `stale`), `runner_state_fresh`, `active_turn`, `sessions_complete`,
  `sessions_total`) that are `null` under the other three tokens.
  `asl outbox wait` is unchanged and remains the general-purpose reader.

In text mode, `asl inbox read`, `asl ask`, and `asl outbox wait` retain their
human-readable record headers and command trailers but render each message as
one ASCII JSON object between `BEGIN ASL MAILBOX MESSAGE JSONL` and
`END ASL MAILBOX MESSAGE JSONL`. Newlines, non-ASCII characters, and
prompt-like text therefore remain escaped inside the record. Reply fields stay
backward compatible in `--json`; inbox-read JSON remains complete even when
its text form would apply the guidance rendering budget.

With `--json`, failures use the common error envelope and the following stable
`reason` values:

- Message-input errors are `missing-text` when a required message body is
  absent or blank, `invalid-text-source` when both `--text` and
  `--text-stdin` are supplied (for `asl ask`, both `TEXT` and
  `--text-stdin`), and `stdin-read-error` when standard input
  cannot be read as strict UTF-8. Resolution input uses `invalid-action` for an
  unsupported or missing action, `invalid-note-source` when both optional note
  sources are selected, and `invalid-note` when a supplied note is not text.
- Question and wait-input errors are `invalid-question-source` when `asl ask`
  receives neither or both question text and `--question-id`,
  `invalid-target` when a closure target is not a valid question UUID,
  `invalid-since` when the outbox cursor is not a non-negative integer,
  `invalid-include` when the outbox include mode is unsupported,
  `invalid-supervision-id` when the outbox launch identity is not a UUID, and
  `invalid-timeout` when a timeout is outside 0 through 1800 seconds or
  `--wait-timeout` is supplied without its corresponding `--wait` or
  `--wait-consumed`. Lifecycle end reasons include `timeout`.
- Mailbox-state errors are `no-active-run`, `question-not-found`,
  `question-not-open`, `question-already-answered`,
  `question-already-resolved`, `question-not-waiting`, `mailbox-full`,
  `mailbox-read-error`, and `mailbox-write-error`. Lease I/O errors are
  `lease-read-error` and `lease-write-error`.
- Plan resolution shared by all eight commands reports `ambiguous-plan`,
  `plan-not-found`, `malformed-plan`, `state-error`, or `os-error`.

Supervisor write and wait commands first read the current lease and decide
between a heartbeat and a takeover. A fresh, current-version lease carrying a
`run_id` proves a live supervisor is projecting this plan, so the command
re-emits the same `supervisor_id` and question projection with only
`updated_at` refreshed. A missing, stale, v1, unprojected, or unreadable
lease means no supervisor is alive, so the command takes over with a new
random supervisor identity and no projection — a hand-answered run still
survives past its base deadline. The worker commands do not claim supervisor
ownership.
