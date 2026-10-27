### Conversation continuity

A turn cancelled mid-flight has to be *continued*, not restarted, and the only
thing that can continue it is the harness's own conversation. That native
identifier is a durable fact about the plan rather than a detail of the run that
learned it, so it is stored in `conversations.jsonl` beside `state.json` and
never inside it: the next run replaces `state.json` wholesale, which would
destroy the very reference that exists to survive that restart.

Storage is append-only. Opening, confirming, and retiring a conversation are
three records, each appended under an exclusive `flock` on the sidecar
`conversations.lock` and flushed through the platform durability barrier before
the lock is released. A short write is rolled back to the file's previous size,
and an append that finds an unterminated tail from an interrupted write
terminates it rather than cutting it back, so a crash stays visible as a
rejected line instead of being silently healed. Sequences are positional and
one-based, so a malformed or truncated line keeps its place in history. Records
carry identifiers, bindings, and timestamps only; prompts, transcripts, and
feedback text never reach this ledger.

A stored reference names the plan directory, the plan session, the harness, the
workspace root and registry digest, the access profile, the repository scope
aliases, and up to 32 prior attempts (run ID, turn, and outcome — so a resumed
worker can be told which operations were interrupted). Lookup is keyed by
`(session_id, harness)` and nothing else; every other bound field is *checked*
against the stored record rather than used to widen the search. There is
deliberately no "latest conversation" lookup anywhere in the interface.

Selection therefore has exactly one state that permits continuation and five
that do not:

- `resumable`: the newest record for the key is a confirmation and every bound
  field matches.
- `absent`: no conversation was ever recorded for that key — the ordinary shape
  of a plan whose state predates the feature, which reads as an empty ledger
  rather than an error.
- `unconfirmed`: the native ID was persisted before work was submitted (which is
  the required order) and the harness never accepted work on it, so the
  conversation may hold nothing.
- `incompatible`: the record exists but is bound to a different plan directory,
  workspace, registry digest, access profile, or repository scope; the differing
  fields are named.
- `retired`: the conversation was retired and may never be selected again.
  Completing a plan session retires every conversation bound to it, so a later,
  unrelated attempt at the same session ID cannot continue finished work.
- `corrupt`: at least one physical line could not be read. An unreadable line
  cannot be attributed to a key, so it may be the retirement or supersession of
  the conversation being asked about; nothing may be selected while one exists.
  Skipping it and answering from an older record is exactly the silent selection
  of a stale conversation this ledger exists to prevent.

Apart from `absent`, which proceeds by opening a new conversation because a
session's first turn continues nothing, every non-`resumable` state requires
user input. The way out is an explicit,
one-use operator authorization carried on a continuation intent, and that intent
has no serialization at all — the value that carries the fresh-conversation
override is one this code cannot write down, so nothing can rehydrate it into a
later run.

Conversation-continuity capability is reported per harness as `available`,
`staged`, or `unsupported`, and a report always names every harness exactly
once. The state is derived from the harness adapters rather than declared
beside them: a harness is `available` exactly when its adapter both reports the
conversation a turn ran on and accepts one to resume, which is the same pair of
flags a turn checks when it refuses a capture sink or a resume intent. `codex`,
`claude`, and `copilot` are all `available` today. `staged` is the state for a
harness the reference, intent, report, durable ledger, and turn-lifecycle seam
already exist for but whose adapter neither captures a native identifier nor
resumes against one, because an operator action backed by incomplete recovery
must not be offered.

The turn boundary is where a native identifier is settled, so the seam that
carries it is part of a harness turn rather than part of the ledger. A transport
reports the conversation it learned from its handshake, or the one it stated on
argv where the protocol allows that; the sink persists it, and the transport
submits work only if that call returned — a conversation recorded
after submission is one a crash in between could lose while the work went ahead.
The same identifier named twice is recorded once; a different identifier is
refused, because one turn runs on one conversation. A capture that fails to
persist abandons the turn with nothing submitted and reports the persistence
error, rather than running work into a conversation nothing could find again.
The seam is turn-scoped like the live-guidance endpoint: closing it waits out a
capture already in flight and refuses every capture afterwards, so a late
protocol callback cannot record against a turn that is already over.
Immediately after the capture the transport asks the lifecycle whether the turn
may still be submitted, which is what puts a cancellation arriving in that
window in the `handshake` stage below: it costs the spawn, not the turn.

A resume intent travels the other way on the invocation, and a turn refuses one
its adapter cannot honor instead of quietly starting a fresh conversation.
Where an adapter has a protocol transport and a one-shot fallback, the refusal
runs both ways: a resumed turn has no fallback window, and the one-shot
invocation cannot even be built for a resume. A transport whose prompt is on
argv has no handshake to capture in and can only read its identifier out of the
turn's own output afterwards; because that work cannot be un-submitted, a
capture failure there reports `continuity_unavailable` on the result rather than
abandoning a turn that already ran.

Cancelling a turn is two observations rather than one. Requesting the
cancellation records it and returns immediately, taking no lock a guidance
delivery could be holding, so a request never queues behind the very turn it is
cancelling. Whether the worker stopped is observed separately, and is reported
only once the process is reaped and its transport is shut down. A cancellation
is acted on in one of four windows, which is what the turn's `cancel_stage`
names: `unstarted` before the process is spawned, so nothing is started and
nothing is killed; `handshake` at the transport's next checkpoint, so no work is
submitted and only the spawn is torn down; `execution`, the wait loop's kill of
a turn already doing work; and `shutdown`, once the turn is past its wait loop.
A request inside its grace period is not yet due and stops nothing, so a turn
that finishes inside its grace was never aborted. A request that arrives after
the turn is over is completed on the spot and staged `shutdown` rather than
leaving its caller waiting for a kill that will never be issued.

In the `execution` window a transport that has a live protocol channel is asked
to interrupt its turn first, so the conversation records it as interrupted
rather than as one that stopped answering. That request is issued alongside the
kill rather than before it, and never waited on: the deadline is enforced by
terminating the process regardless of what the protocol call is doing.

The invariants later work must preserve: a reference is written before the work
that uses it is submitted and confirmed only once the harness has accepted work
on it; `(session_id, harness)` selects at most one live conversation per plan;
every non-`resumable` state requires user input; retirement is an append and a
retired conversation is never selected again; and the conversation module
imports nothing that imports the state module, taking a plan *state directory*
rather than a plan directory, which is what keeps migration's dependency
one-way.

#### What a turn decides, and the one-use fresh start

The ledger answers what may be continued; the run decides what this turn does
about it, once, before the turn starts. That decision has exactly three
outcomes and no fourth: continue the saved conversation by naming its native
identifier to the adapter; open one, because this session has never had one;
or stop with `conversation-recovery-required`. Whichever it is, the turn
appends a `conversation_continuation` event before the harness starts — recorded
even when the answer is "open a new one", because the interesting question
months later is which turns did *not* continue what came before, and a decision
only recorded when it went wrong leaves no baseline to read that against.

`absent` and `resumable` are the two selection states that proceed; the other
four stop. A session's first turn needs no authorization, since nothing is being
discarded. Everything else does, and the authorization is `--fresh-conversation`:
one fresh harness conversation, for the first turn that needs it, with retained
files and outstanding feedback kept and the agent's accumulated context not. The
turn that spends it retires the conversation it supersedes with reason
`superseded`, so the ledger says why the old one stopped being used.

The flag is one-use in four places, and all four are needed because each covers
a replay the others do not: an in-run counter, so a later turn, the next
session, or a generated repair child reads an exhausted authorization; the
`asl run --resume` overlay, which rehydrates flags from persisted state and
excludes this dest; the supervisor's child-argv builder, which relaunches the
same session on every recoverable pause and skips this dest; and
`run_config`, which persists no key for it at all, so there is nothing on disk
for a fourth surface to find. `conversation-recovery-required` is classified
environment-blocked rather than recoverable for the same reason: a supervisor
retry that "recovered" it would be a retry that started over without anyone
deciding to.

What is bound is what would invalidate the work: the workspace root and registry
digest, the access profile, and the writable repository scope aliases. What is
not bound is what an operator legitimately changes between attempts — model,
effort, timeout, and spark routing — none of which makes an agent's accumulated
context wrong. A plan with no repository registry is bound by a digest
synthesized from its canonical root, which is an identity rather than a
registry: it proves the run's root is the root the conversation was recorded
under, so a run pointed at a different `--cwd` reads as `incompatible` and stops.

After the turn, the conversation is confirmed only if the turn actually
submitted work — a turn abandoned at the capture seam, or cancelled in the
`unstarted` or `handshake` stage, leaves it unconfirmed, because resuming into
nothing is indistinguishable from resuming into a conversation whose work was
lost. A turn that was interrupted, aborted, timed out, cancelled after work
began, or exited non-zero is still confirmed and still resumable; it records an
attempt (run ID, turn, and `interrupted` or `failed`) so the next turn knows
what it is picking up. Those post-turn writes report their failures rather than
raising: the turn has already run, and a bookkeeping error must not be what
loses it. A lost confirmation fails closed — the next attempt reads
`unconfirmed` and asks the operator.

#### A restore the provider refuses

Everything above assumes the ledger's answer is the binding one. It is not: a
confirmed record can outlive the history it names, and the only party that
knows is the provider, in the handshake of a turn that asked to continue it.
That answer arrives as a nonzero exit with a message in it, which is unroutable
— classified as an ordinary harness failure the turn pauses under whatever
`harness_failures` makes of the provider's complaint, and, worse, the ledger
entry stays resumable, so every later launch asks for the same dead
conversation and fails the same way. The operator's documented recovery
controls cannot reach it, because those controls act on the ledger and nothing
told the ledger anything.

So the refusal is a structural field. `HarnessResult.conversation_restore_refused`
carries the provider's own words, and it is set only when the provider itself
rejected the restore *request*: the codex app-server answering `thread/resume`
with an error response, or an ACP agent answering `session/load` with one. Both
transports distinguish that from a transport failure by raising `RestoreRefused`
only from the narrow `_RequestRejected` a rejected request produces — a dead
process, a closed pipe, or a timeout is still an ordinary failure, because none
of them is the provider saying the history is gone. The separation is kept in
the pending-request record rather than reconstructed afterwards: a `_Pending`
carries an *answered* rejection and a transport reason in two different fields,
so the path that fails every in-flight request when the process dies cannot
produce the exception the restore path reads as a disowned history. The near
misses are excluded for the same reason, and each would cost a live history if
it were not: settings drift on a restore, a thread or session the provider
substituted for the one asked for, an agent that never advertised
`loadSession`, and a replay that could not be read all leave the conversation
intact.

A rejected request is necessary and not sufficient. The same call that answers
"there is no such thread" also answers "internal error", "try again later", and
"your token expired", and retiring a conversation is permanent — the operator's
only way past it is authorizing a fresh start, which discards the accumulated
history a working provider would have handed back an hour later. So the
rejection has to *diagnose a missing history*, and that judgement lives in
`restore_diagnosis.py`: one vocabulary all three adapters ask, because the
alternative is an outage reading as a refusal on codex and an error on copilot.
It sits below the harnesses and imports nothing from the package, which is what
lets the adapters use it at all.

It judges the error the provider actually sent, not a rendered string. Both
transports carry the JSON-RPC code, message, and `data` through as a
`ProviderRejection` instead of flattening them at the raise, and the order of
the checks is the argument: a protocol-fixed code — `-32700`, `-32600`,
`-32601`, `-32603` — decides nothing about history, because those are verdicts
on the *call*, not on a thread; a message or `data` blob explaining itself as
temporary, overloaded, rate-limited, unauthenticated, forbidden, or malformed
vetoes the reading outright; and only what survives both may be carried by
missing-history wording. That order is what makes "internal error: the
conversation store is unavailable" a failed turn rather than a retired
conversation, even though it names the conversation. `-32602` is deliberately
not on the fixed-code list, because it is what both providers answer a
genuinely missing thread with. `data` is read for the veto and never as
positive evidence, since it is where providers put retry hints rather than
history verdicts.

The veto is the second half of the rule and never the first. Every phrasing on
the positive side has to name a conversation, session, or thread as the subject
of the absence, because a sentence that merely says something is missing is
almost never about the history: "no such host: api.example" is a name lookup,
"no such model" is a configuration mistake, and "no such file or directory" is
whatever the provider was reading at the time. None of the three is temporary,
so no vocabulary of transient words could ever have excused them — a
subject-free positive match cannot be made safe afterwards, it can only be
refused up front. The verdict has to attach to that subject too, within the
identifier a provider may name it by and no further: "session `s-1` does not
exist" is a disowned history, and "session/load failed: the settings file does
not exist" is a missing file in a sentence that happens to mention a session.

Naming the subject is itself two requirements, because English builds compound
nouns by stacking them and the history words are ordinary ones. "Session file
not found", "thread pool not found", "no such session/load method" and "unknown
session storage backend" each name a history noun and each is about something
else — a configuration file, a worker pool, an RPC method, a storage backend —
so the noun has to hold the *subject* position rather than merely appear: either
the verb attaches to it directly, or the clause ends where the noun and its
identifier end.

The classifier requires a leading word boundary and a complete history subject.
Punctuation inside a method name (`session::load`, `session()`, `session[load]`)
does not end that subject. Quoting, separators, and digits do not establish a
native identifier: `"config.json"`, `config-v2.json`, and `v2-backend` remain
infrastructure. Accepted identifier forms are UUIDs and the supported numeric
history names (`s-1`, `c-3`, `thread-77`, `acp-session-1`, `v2.thread-9`),
optionally quoted or introduced as an ID. Unknown identifier forms leave the
saved conversation intact on a failed turn. `no such session.` still diagnoses
missing history; `pthread not found` does not.

For the two phrasings whose verb comes first, "no such …" and "unknown …", what
may follow the noun is listed as predicates rather than excluded as nouns, for
the same fail-closed reason the subject is required at all: there is no end to
the infrastructure a provider can put a history word in front of, and a
misconfigured install that reads as a disowned history costs an operator the
accumulated context a repaired install would have handed straight back.

The whole thing is fail-closed in one direction: an unrecognized sentence is
not a refusal. The turn fails, the ledger entry survives, and the next launch
continues the conversation — so a provider that rephrases its refusals costs a
turn, and never an agent's history. Nothing about the recovery path changes for
a history that is genuinely gone; what changes is that a provider having a bad
minute now costs nothing at all, and an ordinary relaunch afterwards resumes
the exact saved conversation without anyone authorizing a fresh one.

Claude has no restore handshake to refuse, since `--resume` goes on argv and the
CLI either continues the conversation or complains and exits, so the refusal is
recognized after the fact and gated four ways rather than one: the turn asked
to resume something; the CLI never announced a session of its own, which every
turn it actually started does before running anything; the complaint names the
very conversation that was asked for; and the complaint reads as a missing
history under the same shared vocabulary, so "conversation <id>: service
temporarily unavailable" is a failed turn here too. A stopped turn — timed out,
aborted, interrupted — is excluded outright, because none of those says whether
the history exists and all of them can leave the CLI having printed nothing.

`handoff_guards._terminating_result_disposition` names the branch
`restore-refused`, placed after the three stopped-turn dispositions and ahead of
the unreadable-plan one: an operator who stopped the turn owns it, but a plan
that could not be re-read is a condition that predates the turn and the ledger
still has to be told. `turn_dispositions.settle_restore_refused` does the one
durable thing the refusal implies — retires the record with reason `unusable`,
the ledger's word for a conversation that is no longer a history, rather than
`superseded`, since nothing replaced it. That single write buys both remaining
behaviours out of machinery that already exists: an ordinary launch afterwards
selects `retired`, which is already outside the states that may proceed, and
stops for recovery; and a launch carrying `--fresh-conversation` spends its
one-use authorization, supersedes nothing because nothing live is left to
supersede, and opens a new conversation with no resume identifier. The pause is
`conversation-recovery-required` and its message names the fresh-start
authorization as the way forward, appending "(the block could not be made
durable)" when the ledger write itself failed, since in that case the next
launch would try the same dead conversation. The turn record's status is that
same pause reason rather than a harness-failure classification, so the turn and
the pause tell one story. Nothing ran, so there is no completion claim to
reconcile: the refusal happens in the handshake, before any work is submitted.

Durable mailbox, event, and state-ledger writes issue `fcntl(F_FULLFSYNC)` on
Darwin so completed records cross the device cache. Filesystems that reject
that device barrier as unsupported fall back to `fsync`; other platforms use
`fsync` directly. Atomic state replacement also syncs the containing directory
after the rename. Supervisor lease heartbeats and immutable diagnostic
artifacts do not make this power-loss durability guarantee.

`state.json` is updated to the latest run and includes `run_id`, `run_dir`, and
`turns[*]` entries with `artifact_dir`, `stdout_path`, `stderr_path`,
`last_message_path`, and `result_path`. It also includes `commit_mode`,
`commits`, `control`, `mailbox_enabled`, `mailbox_activation`, and, after
successful auto-commit preflight,
`commit_preflight`. The `control` object reports `stop_after_current`,
`tail_enabled`, the pending-abort projection `abort_turn_requested` with its
`abort_grace_seconds`, `abort_reason`, and `abort_deadline`, `next_harness`,
process-local `implementation_overrides`, `consumed`, and `last_action`;
`last_action` is one of
`stop-after-current-requested`, `stop-after-current-canceled`,
`stop-after-current-consumed`, `abort-turn-requested`, `abort-turn-honored`,
`abort-turn-satisfied-by-worker`, `tail-enabled`, `tail-disabled`,
`harness-selected`, `implementation-model-updated`, or `null`. The three
abort-pending fields are cleared the moment the abort resolves, so a terminal
abort `last_action` sits beside `abort_turn_requested: false`.
`control-command-observed` is a receipt for a drained command rather than a
state transition, so it is recorded in `events.jsonl` and never becomes
`last_action`.
`mailbox_activation` records the once-per-run decision and its reason
(`mailbox-present`, `fresh-lease`, `inactive`, or `disabled-by-flag`) plus the
resolved paths and best-effort lease evidence. Failed-run artifacts remain in
their original `runs/<run_id>/` directory when a later retry succeeds.
