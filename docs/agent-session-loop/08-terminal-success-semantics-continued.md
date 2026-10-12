<!-- Continuation of the previous part. -->

Each of those splits changed what a line already on disk means, which is why
records are stamped `version: 3` and why the two older versions stay readable.
Version 1 wrote `continued` from a run whose worker was already up, so on a
version-1 journal the carry *is* the delivery, while on a later one the same
line says a prompt was assembled and nothing ran. Version 2 had that split but
no `launching`, so a version-2 carry with no delivery after it cannot say
whether its writer died before the prompt went out or after it. Replay
therefore reads each line at the version its own writer stamped on it, and all
three versions are readable — an upgrade caught a plan mid-run leaves a journal
with some of each. A version-1 `continued` records the delivery it meant, so
`delivered_at` is read off the carry and `delivered_legacy` marks that the
evidence is the old format's single mark rather than an observed launch. A
version-2 `continued` records a writer that had no way to declare the launch
window at all, which `launch_unrecorded` marks, so its silence is evidence of
nothing.

What that buys is the guarantee the splits would otherwise have reversed, and
it is the same guarantee twice. Neither a legacy version-1 delivery nor an
ambiguous version-2 carry is released or recovered automatically: a pre-upgrade
correction whose worker never receipted it stays `uncertain`, out of the release
path and out of automatic recovery, and so does a version-2 carry with no launch
record after it, rather than either being handed to the next attempt as words
nobody saw. Only a version-3 carry with no `launching` line behind it is
provably unsubmitted — that writer would have declared the window — and only it
is released and recovered without an operator. Legacy receipts are untouched:
an old `acknowledged` line still settles its correction.

Identity is the producer's own `operation_id`: resubmitting it with the same
target and the same feedback digest returns the existing request instead of
appending a second one, while resubmitting it with a different target or digest
fails with `invalid-operation-id` rather than silently replacing what was
already promised. The first terminal record for a
request wins and every later one is refused as `already-settled`, which is what
makes a retry that races its own settlement safe. The whole read-decide-append
sequence holds an exclusive lock on the journal and fullsyncs before releasing
it, and a short write is rolled back to the file's previous size, so a
concurrent producer sees a whole record or none of one. Feedback text lives
only in the journal: every projected object names its byte count and its digest
and none of them carries its bytes, so a correction cannot leak into an event,
a status payload, or a log line meant only to say that a redirect exists.

Submission and acceptance are separate facts, and keeping them separate is the
whole of what admission does. A submission that is refused before anything is
persisted — the plan is invalid, no run is active, the run is paused, there is
no active turn, the target names a different turn than the one in flight, the
active turn is a terminal audit, the lock is owned by another host, the lock is
stale, or the lock names no urgent endpoint — leaves the correction with its
author, who still has it to resend. Past those checks the order is: append the
`submitted` record, send the urgent wakeup, wait a bounded `2.0` seconds for a
terminal record, then report. `accepted` means a runner appended the acceptance
itself. `unavailable` means the wakeup did not land, and the request is durably
settled as `rejected` with reason `urgent-channel-unavailable` before the
answer returns, so no later runner can accept a correction bound to a channel
already known to be dead — the operation id comes back regardless, because the
producer needs to know what was written. `uncertain` means the wait elapsed
with the request deliberately left unsettled, since settling it there would
race an acceptance already in flight; the honest answer is to name the
operation id and let the journal be re-read. Run facts are read before the
lock and the two reads are not atomic, so the lock checks fail closed and are
ordered most-specific-first: a correction admitted against a lock that went
stale or foreign in between would have no reachable reader, and its author
would have been told it was on its way.

Acceptance is a promise that the turn stops, and keeping it is the urgent
controller's job. The turn's own wait loop already has a cooperative route —
it notices the armed abort signal, names the cancellation window, kills the
group and sweeps the descendants — and every part of that route can be blocked
by the failure a redirect is usually sent about. It runs only once the
transport is open, so a handshake wedged inside `open_transport` never reaches
it, and it samples the process table before it kills, on the `5.0`-second bound
the post-turn sweep uses, which is longer than the whole escalation window. So
escalation is a separate path with its own thread, its own leaf lock and its
own monotonic clock, and four properties.

*Armed before acceptance is reported.* Arming is cheap and externally
unobservable — a deadline and a background sample, no signal, no protocol call,
no journal record — so it runs *before* the acceptance append rather than after
it, and there is no window in which a producer has been told its correction
landed while nothing is counting down. It is revocable for exactly as long as
that stays true: a store refusal stands the watchdog down and the turn keeps
running. Once the acceptance record exists nothing may take it back. The
deadline is `2.0` seconds from arming, derived from the same constant the
producer's own bounded wait is derived from, because the two are halves of one
promise and two constants could drift into a window where the producer gives up
before the runner escalates. A delay in the append therefore eats into that
window rather than extending it, and an acceptance that becomes durable after
the watchdog has already given up waiting for it escalates immediately instead
of not at all. A second correction against a turn already escalating rides the
deadline already running; it does not get a fresh two seconds.

*Native cancellation initiated immediately.* The moment acceptance is durable
the harness is asked to stop over its own protocol and the abort signal is
armed with the escalation's remaining grace, so the cooperative path aims at
the same deadline. Both are fire-and-forget: the protocol call goes to a daemon
thread nothing joins, because `interrupt` on a wedged transport is precisely
the call that can block forever.

*Force-stopped at the deadline regardless.* The watchdog reads a monotonic
clock, holds no lock but its own, and calls one `killpg` on the process group
the run stated it owns at spawn time. Between the deadline passing and that
signal there is no `ps`, no journal read, no transport call and no lock shared
with guidance delivery or any protocol request — so the escalation cannot queue
behind them. Ownership is stated rather than looked up, since a controller that
resolved a pgid at the deadline would be reading the process table on the one
path that must not read it. Two refusals guard the aim: a pgid of 1 or less,
and a pgid equal to the runner's own group.

*Reported, because killing is not stopping.* A descendant that left the process
group survives a group kill, and a signalled process stays visible until its
parent reaps it. Evidence is therefore collected on a background thread at
arming time, while the worker is still alive — the only moment an escapee is
reachable by a descendant walk — on a `1.0`-second bound rather than the
post-turn sweep's five. Identity-checked cleanup and confirmation both run
*after* the kill: each escapee's pid is re-authenticated against the start time
the evidence recorded before it is signalled, and only a match is, because the
most likely moment for a pid to be handed to someone else is right after the
process wearing it was killed. A reused pid and a pid whose identity cannot be
read at all are both reported rather than signalled; an unverified survivor
blocks continuation, which is strictly better than killing a stranger.

Continuation is gated on that report rather than on the kill having been
issued. A successor may start only when the evidence was actually collected,
the group answers signal 0 with "no such process", no tracked descendant is
still running under its recorded identity, nothing answered with a permission
error, *and* the turn's owner has reported the worker reaped. The last
condition is separate on purpose: a caller that has not waited on its child has
not finished with it, and an unreaped group leader keeps its process group
answering signal 0 — so "killed" and "collected" are different claims and only
both together license a second worker on one conversation. `ASL_PROCESS_SWEEP_DISABLED`
withholds the evidence and therefore the proof, which reports as incomplete and
blocks; it does not stop the run's own worker group from being killed, because
that group is this run's to stop and was never what the opt-out was about.

A report that never arrived is a refusal too, and for an accepted correction it
is the same refusal a negative one is. Cleanup and confirmation run per pid
against a real process table after the kill, while the turn's teardown waits for
the report on a fixed bound, so "the report is not here" is the shape an
ordinary slow cleanup takes as readily as a wedged one — and a watchdog thread
that died on the way leaves exactly the same nothing behind. None of the three
is evidence that anything stopped, so once an acceptance is durable the gate
blocks on `termination-unreported`, or on `termination-failed` when the
escalation is known to have raised. Only a turn nothing was accepted against
keeps the no-report pass, which is every ordinary turn and also one that armed
for a correction the store then refused: that turn interrupted nothing and owes
its successor no proof. The gate does not wait for a late report — the teardown
already spent its bound, and a second wait would push an unbounded cleanup into
the disposition that has to decide — but it re-reads the machine on every call,
so a report that lands afterwards clears the block on the next attempt rather
than being stale. A teardown whose wait ran out records `report_wait_exhausted`
on the turn's redirect payload, so a blocked continuation can be told from one
that was never given time.

What the run does next is the `redirected` turn disposition, dispatched after
`interrupted` and before `timeout`, `aborted` and every other terminating
branch. It is selected from the acceptance record rather than from the harness
result, because a redirect arms the same abort signal an `abort-turn` does — so
`aborted` and a 137 exit describe both — while a worker that finished inside
the grace window sets no flag at all and still owes the operator the attempt
their correction was promised. Only the operator's own Ctrl-C outranks it.
Completion and acceptance contend for one latch and exactly one wins: a
correction accepted before the turn settled denies that attempt its completion,
and the claim is taken back the way the nonzero-exit branch takes one back,
recorded as `unsettled-completion-after-redirect` with the reconciled plan image
published rather than the claimed one; a turn that settled first refuses every
later correction with `turn-already-settled` and — deliberately — does not
settle it in the journal, so the request expires on the deadline its producer
was already told about instead of being answered by a turn that is over.

The correction then buys another attempt at the *same* session, on the same
plan lock. The next turn resumes the conversation the correction was written
against and names the operations that were cut short by identifier only; the
feedback itself is never carried, because the worker reads its words out of the
journal. Nothing else advances: the session status is what the corrected turn
left, no failure budget is charged (a redirect is not a failed turn, so it earns
no handoff correction, no reopen, no Spark retry), and no commit is made for the
attempt that was stopped. A second correction against the same session extends
one lineage rather than starting a new one — the attempt number rises, the
operation ids accumulate — and a correction naming a different session replaces
it rather than merging into it. A harness selection queued mid-run is a
statement about the next session, not about finishing the one an operator just
corrected, so the redirected attempt stays pinned to its predecessor's harness
and the switch is deferred and recorded rather than silently taken. A stop or an
abort queued after the correction still wins: the attempt is not started, and
the withdrawal of the completion claim happens either way. Every one of these
outcomes is published as a `redirect_continuation` event — `continued`,
`blocked`, `stopped`, `harness-deferred`, `resumed`, `carried`, `carry-failed` —
carrying the digests and byte counts but never the bytes.

An operator who interrupts a turn usually keeps typing, so what the resumed
prompt carries is decided at one moment rather than filtered by a rule: the
instant that prompt is finalized, every unsettled correction aimed at an earlier
attempt of this session is swept in beside the ones the gate accepted mid-turn,
ordered by submission sequence, and marked `continued` against the attempt about
to start. A correction still `submitted` at that point is accepted as it is
carried, because nothing else will ever accept one aimed at a turn that is
already gone. A correction that arrives *after* the boundary names the attempt
now going live and takes the ordinary mid-turn redirect path — it interrupts that
attempt in turn and appears in the next prompt — so every message is delivered
once, in the order it was sent, with no prompt rewritten after the fact. Carrying
is idempotent: re-preparing the same attempt re-reads what it already carries
rather than sweeping up whatever arrived since, a second attempt may not take
delivery of a correction another one carried, and a correction an earlier
continuation already delivered is not offered again to a later one — it is in the
conversation both attempts share. A per-correction refusal (an expired one, one
aimed at another session) costs only that correction and is reported in the
event; a journal that cannot be read at all degrades to naming the interrupted
operation ids, which is what the previous behaviour did, and records
`carry-failed`.

The corrections themselves are rendered first among the things the resumed
worker reads — ahead of the session it is being asked to continue, in both the
task packet and the legacy skill prompt — one escaped ASCII JSON object per
correction between `BEGIN ASL OPERATOR CORRECTION JSONL` and `END ASL OPERATOR
CORRECTION JSONL`, oldest first, with no record cap. Unlike pending guidance,
which a worker can re-read with one command, a correction omitted from that block
has no other surface. The prompt states what the correction may override — the
earlier text of this prompt — and what it may not: the fixed session and its
scope, the completion contract, safety requirements, and runner-owned files and
mutation rules.

Delivery is not receipt. The worker acknowledges explicitly, with
`asl inbox acknowledge PLAN_DIR --redirect-id ID --attempt TOKEN`, whose exact
form the prompt renders for it; the receipt happens while the worker continues
and nothing waits on it. The claim is doubly identified and both halves are
checked: the token must equal the token the *live* attempt mints — the run id,
session id and turn index, compared whole and never parsed apart, with the
harness deliberately excluded so a re-selected harness does not invalidate a
receipt — and the journal must already record that attempt as the one which
carried the correction. That is what makes the two cheap impostors fail. A stale
worker still alive from the interrupted turn holds a token for the turn that was
corrected, and a transcript replaying an older prompt holds one for an attempt
that has moved on; both are refused with `redirect-attempt-mismatch`, and a
worker naming an attempt that carried nothing is refused with
`redirect-not-carried` or `redirect-continuation-mismatch`. A receipt that cannot
be verified at all — no live attempt is recorded — is refused rather than
trusted. Repeating a receipt appends nothing and reports `repeated`, so a worker
retrying an interrupted command cannot make the journal claim it read the
correction twice, and replay re-checks every one of these conditions, so a
hand-added `acknowledged` line credits no one. Nothing cheaper counts: a
successful pipe write, the prompt appearing in a transcript, a replay of that
transcript, and a harness accepting a prompt all happen without a model having
read anything.

Waiting for a receipt is observational. The wait opens the journal read-only,
consults and signals no cancellation, and returns a report that carries no
failure — a timeout neither retracts the redirect nor triggers a second
interruption. A worker can be busy applying a correction for far longer than any
interesting wait, and treating silence as refusal would attack exactly the work
the correction asked for. The correction survives a timeout either way: it is
durable in the journal and already in the prompt.

When the successor gate refuses, the run pauses with
`redirect-successor-blocked` rather than starting a second worker beside a
predecessor that could not be shown gone. It is an `inspect` pause and a
non-recoverable one: the repair is to the machine — a surviving process, an
unreaped child — and no text an operator sends makes a successor safe to start,
so the pause also makes no automatic commit of the retained work.

A journal that will not accept the launch declaration pauses the same way, with
`redirect-delivery-unrecordable`, and the turn is not started at all. This is
the one piece of redirect bookkeeping the loop may not shrug off: its siblings
report a decision already made, while this one is a precondition of making it. A
journal that cannot record the undertaking cannot record its outcome either, so
launching anyway would put the operator's words in a worker's hands with nothing
on disk to say so — precisely the shape that produces a duplicate delivery
later. The correction is handed back on the way out, as a best effort, and a
carry the same failing journal will not release is reported as uncertain rather
than offered again. It is an `inspect` pause because the repair is to the disk
the journal lives on — a full filesystem, a read-only mount — and no reply
changes that: a turn not run costs a pause an operator can act on, where a turn
run undeclared costs a correction delivered twice with nobody able to tell.

A crash is not a fifth kind of failure here; it is the same four states seen
from the next process. A correction can stop existing mid-flight at six
instants — before anyone accepted it, after acceptance and before it reached a
prompt, after it reached a prompt and before that prompt was declared outbound,
inside the window between that declaration and the worker saying so, after the
receipt, and mid-append on the journal's own last line — and each leaves a
different, readable file behind. Recovery is therefore reconstruction from
durable evidence rather than replay: the next run reads what the dead one wrote
and works out what is still owed, and no part of that answer comes from memory,
a heartbeat, or a retry the operator did not ask for.

Reading that state never starts anything. `asl` inspects a plan's redirects by
replaying the journal shared, reading the conversation ledger, and classifying
the lock *without* reclaiming it — a status question does not delete the stale
`lock.json` that explains what happened, does not open the urgent FIFO, and does
not consult the process table. An operator checking twice gets the same answer
twice and changes nothing by asking, which is what keeps "check before acting"
the safe option rather than the expensive one. The same evidence gives a request
an honest status: an unanswered correction reads as `submitted` while some run
could still answer it and as `unanswered` once none can, and the difference is a
fact about locks rather than about the file.

Reconciliation — the half that writes — runs once at run start, under the new
run's own lock, and refuses before it acts. A plan a live process owns, or one
whose lock was written on a host this machine cannot evaluate, is left exactly
as it was: a second reconciler settling requests the first is still answering is
how one correction acquires two contradictory outcomes. Past that gate, anything
past its deadline is expired in the journal; an *unanswered* correction whose
session this plan will not run again is settled `rejected` with reason
`run-ended`, which says the process that owed an answer ended rather than that a
deadline passed; and one the dead run had already *accepted* is reported rather
than rewritten, because first terminal wins and recording a rejection over an
acceptance would reverse a decision about a turn nobody living saw. Everything
still unfinished whose session *is* pending is reported as recoverable and
carried later, when a continuation attempt actually exists to carry it into.

Before any of that, reconciliation hands back the carries the dead run never
launched with. A correction bound into a prompt whose worker never started is
not in any conversation's history and was never in front of anybody, so
releasing it costs nothing and leaves it takeable; left carried it would be
unreachable forever, because a second carry over an existing one is refused.
What makes "never started" a fact rather than a guess is that the run writes the
launch down before it happens. A carry with no launch record behind it is one
the dead run can still be proved not to have sent; a carry whose launch was
declared and whose outcome never arrived proves nothing either way, and is left
exactly where it is. The absence that licenses a release is therefore the
absence of a specific line, never the absence of news. The release is restricted
to *other* runs: a carry belonging to the starting run is
impossible, since its id was minted for this process, and a rule that did not
say so would become a live run releasing its own in-flight prompt the first time
reconciliation ran from somewhere else. A refusal is swallowed rather than
raised — the request stays carried, which is the state it was already in — since
settling a crash's leftovers should not fail over one line that could not be
appended.

Carrying a correction across a crash needs something the ordinary continuation
rule cannot give it. Every run mints a fresh `run_id` and numbers its turns from
one, so the successor attempt differs from the correction's target in both
fields — a different run, and a *lower* turn index — which is precisely what the
ordinary rule refuses. The recovery case is admitted explicitly and marked: the
`continued` line carries `recovered`, replay grants the cross-run latitude only
to a line bearing that marker, and the opt-in is made per operation id rather
than per batch, so naming one genuine recovery cannot widen the rule for the
corrections beside it. A hand-added annotation therefore forges nothing, and an
acknowledgment whose marker disagrees with the delivery it claims is rejected on
replay like any other mismatch. An operator's ordering survives the crash the
same way it survives an interruption, by journal sequence.

The one outcome recovery refuses to decide is a delivery that was submitted, or
may have been, without a receipt. The journal records that the words went out to
a worker; whether a model read them is the question the acknowledgment protocol
exists because nothing cheaper answers. That is the whole reason the submission
is written down apart from the carry: without it this uncertainty would swallow
every crash in the window before the prompt was handed over too — including the
worker a provider refuses the moment it asks to restore its conversation, which
is spawned and then told nothing — and those corrections are recoverable. The
launch record draws the other edge of that window, and it is what keeps the
first edge honest: a run that died between submitting a prompt and recording the
submission left the same file behind as one that died before submitting at all,
so without it the uncertainty would be resolved in the *unsafe* direction and
the correction handed to a second worker. The same goes for a delivery line the
journal simply refused to take, and for a transport whose reporting thread died
before it could say the prompt went out: neither is a refusal, both are
silences, and a silence inside the launch window is a question. A refusal a live
attempt actually observed is different — a cancellation caught before the
handshake finished, a conversation that could not be recorded, a restore the
provider declined — and that attempt says so by releasing the carry, which is
evidence and keeps those corrections recoverable. Re-handing an ambiguous
correction would tell a worker its first correction is new; dropping it would
discard one that may never have been read. So it is reported
as `uncertain` and nothing acts on it automatically — and there is no flag that
does, because the coalescing boundary refuses a second delivery of an already
carried request in any case. The operator who reads the report and concludes it
went unread says so by submitting the correction again: a new request, its own
acceptance, and words chosen knowing what happened. That is the ambiguity
reaching a person, which is the only place it can be resolved.

Damage is reported as incompleteness rather than worked around. A journal whose
last line was cut mid-append is flagged as a torn tail and the next append
terminates it instead of merging into it; a line that cannot be attributed
consumes its sequence and is counted, leaving the requests around it untouched;
and a conversation ledger that cannot be trusted withholds every recovery,
because the turn a correction would be delivered into cannot be selected until
the ledger is repaired — handing it over then would mark it delivered to a
prompt that is never built, while withholding it leaves it recoverable for the
run that starts afterwards. A journal that cannot be read at all stops
reconciliation outright, since every answer it would give is a claim about what
the journal says.

Relocating a plan's state carries the journal across unedited. A redirect
request names a run, a session and a turn, none of which is a path, so
`asl migrate-state` has nothing to rewrite inside it and reports counts instead:
how many requests move with the directory, how many of those are still
unfinished and will be reconciled by the next run, and whether any line was
unreadable or torn. Those counts reach the report, the printed summary, and the
`state_migrated` event; none of them is a word of the correction itself. A
journal that cannot be read refuses the migration with `redirect-journal-error`
before anything is renamed, the same way an unreadable conversation ledger
already does.

```
asl control redirect PLAN_DIR_OR_ID (--text TEXT | --text-stdin) [--session N] [--turn N] [--run-id RUN_ID] [--operation-id ID] [--ttl SECONDS] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control redirect-status PLAN_DIR_OR_ID [--redirect-id ID ...] [--wait-acknowledged] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control recover-conversation PLAN_DIR_OR_ID [--state-dir DIR] [--json]
```

`asl control redirect PLAN_DIR_OR_ID --text TEXT` is the operator surface for
all of that, and it is the only one of the three redirect commands that writes.
It takes one correction — `--text` or `--text-stdin`, exactly one, strict UTF-8,
never blank — and submits it against the turn running right now, then waits out
the acceptance window (`--timeout`, default `2`, maximum `30` seconds) for the
runner's own acceptance record. The payload reports `outcome`, `reason`,
`operation_id`, `target`, `delivery`, `waited_seconds`, the `stage` described
below, and the `expected_target` the submission was checked against; it never
carries the correction's text, only its `feedback_digest` and byte count.
`--session`, `--turn`, and `--run-id` state which turn the correction is meant
for, and every field left out is filled from the live turn, so `--turn 7` alone
still refuses a run that has moved to turn 8 with `stale-target`. `--ttl`
bounds how long the correction stays claimable (default `120`, range
`1`–`3600` seconds). `--operation-id` reuses the identity of an earlier
submission: the journal treats the same id with the same words as the same
request and appends nothing, and refuses the same id carrying different words or
naming a different turn as `invalid-redirect-operation-id`, so a retry
reconciles instead of correcting the worker twice. The command's own input reasons are `missing-text`,
`invalid-text-source`, `stdin-read-error`, `invalid-timeout`, and
`invalid-target`; every other refusal is reported under the word the journal or
the admission layer already uses for it — `invalid-plan`, `no-active-run`,
`run-paused`, `no-active-turn`, `stale-target`, `terminal-audit`,
`remote-owner`, `stale-lock`, `urgent-channel-unavailable`,
`invalid-redirect-feedback`, `invalid-redirect-ttl`,
`invalid-redirect-operation-id`, and `os-error`.

`asl control redirect-status PLAN_DIR_OR_ID` reports what became of corrections
already submitted, and changes nothing by asking: it replays the journal shared,
classifies the lock without reclaiming it, and opens no FIFO. `--redirect-id` is
repeatable and narrows the report; an id the journal has no record of fails with
`unknown-redirect-request` rather than being reported as an empty result, and an
incomplete journal says so in the same breath. `--wait-acknowledged` adds the
observational receipt wait against the live attempt (`--timeout`, default `30`,
maximum `900` seconds; without the flag, supplying `--timeout` fails with
`invalid-timeout`), refuses with `redirect-no-live-attempt` when no attempt is
running to receipt anything, and re-reads the journal afterwards so the reported
result is the one the receipts produced. A wait that runs out is not a failure:
the correction is still carried, still in the worker's prompt, and still
unsettled, which the payload states as `observational: true`.

Both commands report every operation twice over: `status` is the durable
projection (`submitted`, `unanswered`, `accepted`, `acknowledged`, `rejected`,
`expired`, `uncertain`), copied verbatim so the CLI never becomes a second
source of truth, and `stage` answers "where is my correction?" by adding the one
fact the journal cannot hold — whether the live run is still on the turn the
correction named:

| Stage | Meaning |
| --- | --- |
| `submitted` | persisted and unanswered; a live run may still take it |
| `unaccepted` | persisted and unanswered; no run is left to take it |
| `interrupting` | accepted; the turn it names is still being stopped |
| `stopped` | accepted; the turn it named is gone and a successor owes it |
| `resuming` | carried into the live attempt; no worker receipt yet |
| `acknowledged` | a worker receipted it on the attempt that carried it |
| `rejected` | settled without being acted on |
| `blocked` | the urgent channel could not carry it; durably settled |
| `uncertain` | a worker was launched with it and never receipted it |

`resuming` is read off the carry rather than off the status, so it covers the
live attempt that is holding a correction it has not launched with yet as well
as the one that has. A carry belonging to an attempt that is *not* live reads
`stopped` — a successor still owes it — which is what a prompt that was
assembled and then abandoned leaves behind.

Exit codes mean the same thing in both commands, because the caller that reads
nothing else reads them. `0` is the command doing what was asked. `1` is a
refusal: a usage error, an unresolvable plan, an unreadable journal, or a
redirect definitely refused — nothing to retry unchanged. `2` is
`urgent-channel-unavailable`: the request is durably settled so no later run
will act on it, and resubmitting once a run holds the plan again is safe. `3` is
unresolved, where the answer is an operation id rather than a verdict — an
uncertain submission, or an acknowledgment wait that ended with receipts
outstanding — and the correct next move is to reconcile that id with
`redirect-status`, or to resubmit carrying the same `--operation-id`, never to
send the correction again as new work.

`asl control recover-conversation PLAN_DIR_OR_ID` covers the one pause that has
no question in it. When a run stops on `conversation-recovery-required`, the
harness conversation it would resume can no longer be selected, so there is
nothing for a user to answer and no `--reply` that restarts it. The command
reports `recovery_required`, the pause reason, the conversation ledger's health,
the corrections a fresh start would still carry, and the exact relaunch —
`asl run PLAN_DIR --resume [--state-dir DIR] --fresh-conversation` — which it
prints rather than runs. That is deliberate: the authorization is one-use, is
consumed by the first turn that needs it, and is never persisted, rehydrated by
a later `--resume`, or replayed by a retry, so a command that spent it on the
operator's behalf would be storing the decision it exists to keep with a person.
`asl questions` and the paused task packet follow the same rule for the same
pause: the rendered prompt becomes a recovery prompt that never asks the
resolver to invent a question, `recovery_required` and `recovery_command` carry
the route, and `resume_command` / `reply_stdin_command` / `reply_command` are
`null` so a consumer that prints them without reading `recovery_required` prints
nothing rather than a reply for a question nobody asked.
