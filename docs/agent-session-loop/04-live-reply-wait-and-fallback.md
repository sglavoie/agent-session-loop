### Live reply wait and fallback

When an enabled worker turn exits successfully with a reply-capable
`RUNNER_PAUSE:`, the runner publishes one retry-stable question derived from
plan/run/session/turn identity and enters additive `waiting_for_reply` state.
This is live bounded activity, not a real pause: it emits
`mailbox`/`wait-started`, but does not emit `run_paused`, release the run, or
notify Agent Mux of a pause.

Every published question records the `run_id` that owns it. A question is
**open** only while that run is live — present, readable, unpaused, unfinished,
and holding an active lock. The mailbox is append-only and outlives every run,
so a question whose owner has ended is history: nothing waits on it and no
closure can reach a worker. A fallback pause therefore retires its own escalation
without appending a further record, and questions written before run
attribution carry no `run_id` and are never open.

The runner waits five minutes initially. Lease freshness is checked only at an
active deadline. At that checkpoint, a fresh supervisor lease extends the wait by 60 seconds;
an absent or stale lease ends the live wait unless a post-suspend lease grace is
active. The 30-minute ceiling bounds *unsupervised* time, not wall clock. It is
measured from the last lease that proved a supervisor alive, so it rolls forward
with every fresh probe: a continuously watched wait has no terminal instant and
outlives the ceiling indefinitely, while a wait nobody watches still ends within
30 minutes of the last proof. A live autopilot therefore no longer loses a run
because a human took a long time to answer; the backstops for a genuinely stuck
run are the autopilot's own attach bound and hang escalation, not this counter.
The wait paces itself on the monotonic clock, which freezes across system sleep,
so a suspend never refunds the active-deadline budget. On wake the runner detects
the suspend,
re-anchors the persisted wall-clock deadlines around the surviving monotonic
budget, and grants the supervisor a 90-second awake grace to re-prove
liveness — the autopilot has not polled yet, so the on-disk lease is briefly
wall-clock ancient even with a live supervisor. Inside that grace a non-fresh
lease neither ends the wait nor extends the active deadline, and the grace
never extends the ceiling; once the grace lapses with the lease still
stale, the wait ends exactly as before. Suspended time is not unsupervised time —
nobody could prove liveness while the machine slept — so the ceiling alone
re-anchors at the wake instant and starts its full span again from there. The runner polls the mailbox every two seconds and
tolerates sustained mailbox or checkpoint lease I/O failure for ten seconds
before falling back. The first reply or resolution wins the closure race. A
reply starts an explicit one-shot answer continuation of the same session. A
`continue` resolution starts a separately typed one-shot continuation that
explicitly says no operator answer was supplied; a `pause` resolution clears
the live wait and preserves the ordinary terminal pause. Even a queued
stop-after-current is consumed only after a scheduled continuation completes.
An already-committed closure takes precedence over mailbox exhaustion and is
consumed before exhaustion can end the wait. Every reply
continuation source—mailbox, `--reply`, or `--reply-stdin`—uses the same safe
prompt representation under `### Operator reply`: one ASCII JSON object
between `BEGIN ASL OPERATOR REPLY JSONL` and
`END ASL OPERATOR REPLY JSONL`. The worker consumes only its `text` value;
newlines, non-ASCII characters, prompt headings, and control-like text remain
escaped inside that value and cannot create runner-owned lines. A continue
resolution instead uses one escaped ASCII JSON object carrying only its action
and optional note; the note is context, never an operator answer or control
prose. Both reply and resolution continuations are consumed exactly once. If no closure is
available, timeout, a stale lease at an active deadline that outlasts any
post-suspend grace, a full/unavailable
mailbox, or missing question context clears `waiting_for_reply` and records the
same ordinary
terminal pause the non-mailbox flow would have recorded.

Before accepting a completed handoff, the runner checks every v3 wait-mode
question attributed to that exact worker turn. If one is unanswered, has an
accepted closure without a receipt, or was consumed with a `pause` resolution,
the runner restores the session to `pending` and pauses with body-free mailbox
IDs and sequences as evidence. Question, reply, and note bodies are never
copied into that guard evidence.

When `asl inbox reply` sees a fresh v2 supervisor lease, it captures that
evidence before renewing the lease. The lease supplies the owning `run_id` and
supervisor liveness but need not already know the question: evidence is
captured either when the projection names the target as its latest unanswered
question, or when the lease demonstrably predates the question — its
`projection_seq` is behind the question's mailbox sequence for the same run —
which closes the short window before the supervisor's next poll learns of a
just-asked question. After the first reply append succeeds, a post-append
mailbox snapshot must show no open questions for the projected `run_id` before
ASL emits the text-free `AgentSessionLoopMailboxReply` Agent Mux marker; that
re-read stays the authoritative condition. A
question an earlier run abandoned — or a legacy question with no `run_id` —
therefore never suppresses the marker, while a genuinely open sibling question
for the same run still does. Its public fields are
`run_id`, `state_path`, `question_id`, reply `source_sequence`,
`mailbox_projection_sequence`, `emitted_time`, and a stable
`<run-id>:mailbox-reply:<reply-sequence>` source event ID. When the lease
predates the question, `mailbox_projection_sequence` is the reply's own
mailbox sequence — strictly monotonic within the same file, so still a valid
generation counter — otherwise it is the lease's `projection_seq`. It never
carries the question or reply body. Missing, stale, malformed, or
run-mismatched evidence skips the marker; Agent Mux discovery, delivery,
timeout, or acknowledgement failure never changes the successful mailbox
reply result.
The supervisor lease's later zero-question projection remains the compatibility
fallback.

`asl inbox resolve` applies the same fresh pre-renewal evidence and
authoritative post-append no-open-question gate without changing the reply
marker. A proved answerless closure emits
`AgentSessionLoopMailboxResolution` with `run_id`, `state_path`, `question_id`,
the resolution's physical `source_sequence`, `resolution_action`,
`mailbox_projection_sequence`, `emitted_time`, and stable
`<run-id>:mailbox-resolution:<resolution-sequence>` source event ID. A
resolution is never marked from stale, malformed, run-mismatched, or incomplete
evidence, and an open sibling question for the projected run suppresses it.
The marker contains no question, reply, resolution-note, or notice body.
Discovery, delivery, timeout, or acknowledgement failure remains fail-open:
the accepted resolution still succeeds and the later zero-question lease
projection remains the compatibility fallback.

If the live wait cannot be prepared, the runner first emits an additive
`mailbox`/`wait-preparation-failed` event with the run/session/turn identity
and the preparation `reason`, then records the ordinary terminal pause without
changing its reason, details, notification, or exit behavior. Non-fresh lease
reasons are `missing`, `invalid-size`, `empty`, `invalid-json`, `not-object`,
`unsupported-version`, `invalid-supervisor-id`, `invalid-updated-at`,
`invalid-run-id`, `invalid-projection-seq`, `invalid-unanswered-questions`,
`invalid-latest-question-id`, `invalid-latest-question-created-at`,
`incomplete-question-projection`, `future-updated-at`, and `expired`. I/O,
capacity, and context reasons are
`lease-read-error`, `mailbox-full`, `mailbox-read-error`,
`mailbox-write-error`, `no-paused-run`, `missing-question-context`, and
`reply-context-mismatch`.

The `mailbox`/`wait-cleared` event reports why the live wait ended. Its reason
is `reply` when an answer arrived, `resolution` when an answerless closure
arrived, `timeout` only when the unsupervised ceiling was reached — a full
ceiling has passed since the last lease that proved a supervisor alive — and
`supervisor-lost` when the active deadline found an absent, stale, or otherwise
non-fresh supervisor lease with no post-suspend lease grace left to cover it.
`supervisor-lost` is the ordinary end of an unwatched wait, so `timeout` is now
the narrow case: the ceiling lapsed while the wait was still covered by a lease
extension or a post-suspend grace that had not yet let the wait exit.
Resolution exits add `resolution_message_id`, `resolution_seq`, and
`resolution_action`. Other fallback paths
preserve their specific error reason, such as `mailbox-full`,
`mailbox-read-error`, `lease-read-error`, or `missing-question-context`;
an unexpected wait failure reports `error`.

A wait that ended without a closure also names the lease evidence behind that
reason: `lease_supervisor_id` and `lease_updated_at` identify the last lease the
wait actually read, and `lease_reason` carries the non-fresh reason that
rejected it. Each key is omitted when the wait has no value for it, so a
`timeout` whose last probe still found a fresh lease reports that lease
without a rejection reason, and a wait that never probed a lease — including
every `reply` or `resolution` exit — reports none of the three. This distinguishes a supervisor
that was never there from one that was still watching when the wait ended. A wait
that detected a system suspend additionally reports `suspend_skew_seconds`,
the wall-clock time the sleeping machine cost the wait, and
`suspend_lease_grace_seconds`, the awake lease grace that suspend applied —
so a `supervisor-lost` that fired only after a lapsed grace reads differently
from one that never saw a suspend. Both keys are omitted when no suspend was
detected, and every reply or resolution exit still reports none of these keys.

Such a wait also reports `unsupervised_seconds`, how long it ran without a
supervisor lease proving liveness. It is measured from the last fresh lease the
wait probed, or from the start of the wait when no probe ever found one, and it
excludes suspended time because a sleeping machine gives nobody the chance to
prove liveness. It is exactly the quantity the ceiling bounds, so a `timeout`
always reports at least the ceiling, a `supervisor-lost` that was watched until
moments before the end reports a short stretch even after a wait of many hours,
and one that never saw a live supervisor reports nearly the whole wait. Every
reply or resolution exit reports none of it.

### Future implementation model control

`asl control set-model` uses the existing consume-once control queue. At least
one set/clear operation is required. `--harness` targets a specific
implementation harness; when omitted, the runner resolves the target while
draining accepted commands, after any earlier `select-harness` command in the
same queue batch. `--harness-model` and `--effort` install process-local
overrides; `--clear-harness-model` and `--clear-effort` return the target to the
original run flag or native default.

Controls are drained between turns and every five seconds during active
implementation and review subprocesses. They never mutate an active
subprocess: the resolved model and effort are snapshotted when the next
implementation invocation starts. Overrides remain available to later
implementation turns in the current runner process only, disappear when that
process exits, and never affect generated review turns.

### Surface and observability accounting

The current net surface is three mailbox state files, eight mailbox commands, one
model-control command, and one `mailbox` event name with
`guidance-observed`, `notice-published`, `wait-preparation-failed`,
`wait-started`, `wait-cleared`, `guidance-delivered`, and
`guidance-delivery-deferred` action variants.
It adds no runner or
supervisor threads and no terminal statuses. `status --json` exposes paths,
counts, capacity, rejected-record count, lease freshness, and compact
`waiting_for_reply` identity/deadlines without raw message bodies.
`guidance-observed` carries message identity, physical sequence, character
length, and SHA-256 digest. `wait-cleared` carries the wait identity and
deadlines, the exit `reason`, `pending_outbox`, the measured count of
questions open for that run when a final mailbox snapshot is available, and the
`lease_supervisor_id`, `lease_updated_at`, and `lease_reason` evidence for the
last lease a non-closure exit observed, plus `suspend_skew_seconds` and
`suspend_lease_grace_seconds` when the wait detected a system suspend. It also
carries `unsupervised_seconds`, the suspend-excluded stretch that non-closure
exit ran without a lease proving supervision.
`wait-preparation-failed` carries run/session/turn identity and the exact
preparation `reason`, without creating live-wait state.
For a resolution, `wait-cleared` additionally carries resolution message
identity, physical sequence, and action. `notice-published` carries
run/session/turn and notice identity, physical sequence, character length, and
SHA-256 digest. `asl tail` renders those metadata explicitly rather than
falling back to a bare action name; neither event carries a resolution note or
notice body.
`guidance-delivered` and `guidance-delivery-deferred` carry guidance and
delivery identity, physical sequence, delivery state, stable delivery reason,
channel, harness, and run/session/turn identity. They carry nothing derived
from the guidance body — not its text, not its length, not a digest — because
a delivery event is evidence about transport, not about content.
`reply_provided` carries reply length and source identity. None of these events
duplicates raw guidance, question, reply, resolution-note, or notice text, and
closed waits do not increment pause metrics because those metrics count only
`run_paused`.

Workbench intentionally has no mailbox-authoring, mailbox-reply,
mailbox-resolution, mailbox-notice, or live model-control endpoint in v1.
Mailbox rotation/compaction, automatic
interrupt-and-restart, plan/session queue mutation, and live session-budget
mutation also remain out of scope.

## Command Behavior

`run` acquires a per-plan lock, inspects the first pending session, invokes the
selected harness, records turn artifacts, and verifies that exactly the expected
session changed from `pending` to `complete`. `run` also enforces the loop-ready
gate: unless `--allow-not-loop-ready` is passed, it requires `manifest.md`
`**Readiness**` to be `loop-ready` after first attempting to repair derived
drift. The repair path force-rebuilds stale or missing `progress.md`, rewrites
recomputable readiness metadata, reruns preflight, and surfaces the attempt in
the run JSON as `readiness_gate_repair`. Each repair entry uses the same fields
as preflight `auto_repairs`; manifest-readiness rewrites set `target_state` to
`"loop-ready"` or, for the complete-plan review path, `"complete"`.
`--allow-not-loop-ready` skips this readiness-gate repair because the gate is
bypassed. After each completed session, the runner invokes the same
`asl verify` recipe; a non-zero exit pauses the run with
`reason: verify-failed`. `--allow-not-loop-ready` also suppresses the
verification gate's `no-contract` pause for sessions that lack a
`## Completion Contract`, but it never suppresses a failing contract that is
present. `--commit-per-session` then runs its repository commit group before
the next harness turn starts. Registered targets come from the passing turn
guard and are filtered again for a dirty worktree at commit time, so a harness
that already advanced HEAD cleanly is not committed a second time. Each group
is checkpointed in state and events
before its first repository command and after every outcome. Its durable
identity records the scope, session, ordered aliases, successful repository
SHAs, failed alias, and remaining aliases. If a process stops or a later
repository fails, the next run reconstructs the unresolved group and completes
it before launching a worker. Already successful repositories are never
committed twice; unresolved repositories that are now clean are recorded as
recovered or skipped. A failure retains earlier successful commits, records
repository-qualified commit artifacts, and does not stop the run: the group is
left owed, retried at the next commit boundary in the same run, and the loop
continues to the next session. Each group is retried at most twice inside one
run, so a repository that refuses every time does not turn the session loop
into a commit loop; whatever is still owed when the run ends is reported in
`commit_failures`, `unresolved_commit_groups`, the final summary, and the
supervisor audit's `commit-group-resolution` check. Legacy single-repository
commits use the same coordinator, which also makes a failed per-session commit
retryable on resume, and their single target is filtered for a dirty worktree
exactly as registered targets are: `asl commit` exits non-zero when nothing is
staged, so a session that changed nothing inside the bound repository must not
be handed to a commit at all. A group built over a clean target recorded that
refusal as the repository's failure with empty `staging_paths` and
`qualified_paths`, and the resumed group then read the still-clean tree at the
unchanged HEAD as `skipped-clean` and finalized as complete (ASI-19).

A commit group's own scope is one of three values, and they are not the same
vocabulary as a commit payload's. `session` is the accepted per-session commit,
`end` the end-of-run sweep, and `session-paused` the retained-work commit a
recoverable pause makes for a session that is back to `pending`. The third value
exists because a group's durable identity is hashed from its scope, session, and
ordered aliases: without it the paused-work group and the accepted group for the
same session would share a `group_id`, and the later upsert would overwrite the
completed row rather than record a second commit. The commit *payloads* a
paused-work group produces are still stamped `scope: "session"`, deliberately —
that field feeds the per-commit progress labels, the `tail` renderer, the run
summary, and the workspace projection, all of which mean "per-session or
end-of-run" by it. So a reader telling a session's two commits apart reads the
group's scope, never the commit's.

The sentences that name a *group* read that scope for the same reason. A
`session-paused` group is labelled `session <id> retained work`, so the
`commit failed` line, the recovered-group line, and the final
`commit group(s) were never completed` warning distinguish a lost accepted
commit from a lost settlement of work no verification passed — which is what an
operator holding the relaunch is deciding on. `session` and `end` groups are
named as they always were, and a group summary persisted before this scope
existed carries no scope and falls back to naming its session.

That settlement is what makes a supervised commit run recover on its own. Under
`--commit-per-session` or `--commit-at-end`, a `verify-failed`,
`transport-failed`, `timeout`, or `no-status-change` pause commits the work its
turn retained before the process exits, using `asl commit --message-file` with a
deterministic message rather than a generated one: the pause may *be* the
harness failing, so the recovery must not spend another bounded harness turn,
and a marking that says the work is unverified must not be rewordable by a
model. The commit runs strictly after the session has been reverted to
`pending`, since a legacy workspace stages repository-wide and committing any
earlier would commit the plan file still saying `complete`. The selection is
exactly the one the accepted path would have used at the same boundary — the
write guard's evidence for a session commit, the end-of-run sweep under
`--commit-at-end` — so a pause never stages more than an accepted turn would
have under a message describing one turn. Review sessions are excluded, matching
the accepted path; a repair child is included even with no commit mode, because
it owns the commit its parent's mode never reached it with.

That evidence is cumulative across the attempts of one session, because a
session that pauses recoverably is relaunched inside the same run and resumes
the same conversation, so its attempts are one piece of work rather than
several. Each turn's write guard captures a fresh before-image, so attempt two's
own record names only what attempt two touched; before the first consumer reads
it, the turn's `workspace_write_guard` record is merged with the previous
attempt's for the same session and rewritten in place, in the same turn
`result.json` the guard already wrote. Path deltas accumulate — the union, each
repository's `head_before` from the earliest attempt that observed it and its
`head_after` from the latest, ref transitions composed rather than concatenated
— while every state field is the current attempt's alone: violations,
unavailable repositories, the scope aliases, and the registered repository list
are what this attempt found, never what an earlier one did. A merge can only
carry work forward, never upgrade a verdict: it happens only when both records
passed with no violations and no unavailable repository, so a failing attempt
neither inherits an earlier attempt's clean record nor lends its own paths to
one. The same merged record is what the protected-path guard adjudicates and
what a per-session commit stages, so all three consumers see the same session,
not the same turn. Nothing verification-related crosses an attempt: a check that
passed against attempt one's tree is not proof for files attempt two changed,
and the baseline fingerprint already refuses a dirty tree.

The ledger behind that merge is scoped to the run and is never rehydrated from
`state.json`. A new run re-reads the tree it finds — a previous run's retained
bytes are dirt the startup commit gate and the dirty-target sweep already see —
and a path list restored from a prior run would name files nothing in this run
has observed. It is mirrored into the run payload as `session_attempts` for
audit, and retired when the session completes. Interruption gains nothing from
any of it: `interrupted` is not a recoverable pause reason, so an interrupted
session's retained work is never settled by a commit, and the repair and review
budgets are derived from the events the run actually recorded, so an attempt
that never reached verification consumes neither.

The deterministic message survives the process that rendered it, because a
settlement can fail like any other commit and be recovered by a later run. The
group records the exact text it commits in `verbatim_message`, outside the
identity hash so the group is still found by the `group_id` its failure
recorded, and the run that recovers a `session-paused` group rewrites that text
under its own run directory and commits it with `--message-file` again. Both
reasons for not generating the message outlive the first attempt: the harness
may still be the thing that failed, and the marking must read the same however
many processes it took to land. A recovered `session` or `end` group is
unaffected and still asks the run's message harness for a message. A
`session-paused` group that carries no message is treated as malformed durable
evidence — the recovery fails and is recorded, rather than falling back to the
harness the scope exists to avoid.

A recovery of that group can itself be refused, and its retry is spent where it
is armed rather than at the loop boundary every other scope uses. The boundary
that consumes a re-armed retry is the top of the run loop, and the startup
commit gate stands between it and the startup recovery: a paused-work group that
failed there would leave the run facing its own unresolved dirt at a gate whose
`commit-preflight` pause is not recoverable, so two transient refusals in a row
would need a human where one did not. So a `session-paused` group is retried in
place, ahead of that gate, until it lands or spends the same two retries every
group gets. Only that scope repeats there, because only that scope recovers
without a harness — a `session` or `end` recovery asks the message harness for a
message per attempt, and its retry waits for the loop boundary as before. A
repository that refuses every commit still reaches the gate and still stops the
run for inspection; it just no longer arrives with a retry it never got to
spend.

Two limitations are deliberate rather than pending. **Registered residue**: the
staging a registered session commit uses is derived entirely from write-guard
evidence, and that evidence excludes the run's own plan directory, so a
registered run whose plan directory is tracked inside a declared repository
still stops at the startup commit gate — now with `paused_work_commit.residue`
naming the paths instead of stopping silently. Committing the plan directory
would mean the run committing its own control plane, which is why the exclusion
is not the thing to relax; the same limitation already applies to the accepted
per-session commit and is not new here. **No commit mode, authored session**: a
recoverable pause in a run with no commit mode leaves its retained work
uncommitted, and a later generated repair child's own commit preflight refuses
that dirt. Settling it would be auto-committing on behalf of an operator who
asked for no auto-commit, which is a worse failure than the one it would fix.

The runner is the last authority on which repositories a run may reach, and it
enforces that independently of preflight. A plan whose `**Readiness**` is
already `loop-ready` short-circuits core preflight, and a legacy plan can skip
`asl prepare` altogether, so a gate that rides on either stage is no gate at
all. Immediately after root and workspace resolution, and before the run lock
is taken or any harness, review, verification, or commit subprocess starts,
`run` re-derives the plan's repository intent against the bound root. Reaching
more than one Git top-level pauses the run with
`pause_reason: repository-registry-required` and a bounded,
credential-redacted `pause_details.repository_intent` object naming the
conflicting roots. The gate is deliberately independent of
`--allow-not-loop-ready`: that flag waives readiness, and no readiness marking
substitutes for the registry that would have bound the second repository.

Each session task packet is rechecked the same way just before its turn
launches. The run-level gate judges the plan as split, but a turn carries more:
expansion notes, `context.md`, the predecessor handoff, live mailbox guidance,
an operator reply, and repair references all reach the worker through the
rendered prompt or the files it points at. The packet recheck reads the
rendered prompt and the contents of every artifact the packet tells the worker
to open, so a redirect that appears only in `session-N.expand.md` — a surface
the plan-level scan deliberately excludes — still pauses the run before the
harness starts. Both gates exempt registered workspaces, whose registry
already names every repository, and both exempt the runner's own trees: the
plan directory, the state root, and the run directory, which an operator may
legitimately keep inside an unrelated checkout. Repository identity is the
resolved Git top-level and never the origin URL, so two clones of one remote
sitting side by side are two repositories.

The implementation worker and runner have deliberately separate validation
ownership. The worker runs focused checks while iterating, but does not rerun
the full completion recipe solely to make the final gate decision. After the
worker marks the session complete, the runner executes that recipe once as the
authoritative post-turn verification. On failure it restores the session to
pending and supplies the captured evidence to the next repair turn.

That rule bounds the *final gate*, not checking itself, so every worker-facing
surface names the same three cases that justify running a check again or
running a broader one: an edit since that result invalidated it, a failure that
needs diagnosis, or a concrete correctness concern none of the checks already
run covered. None of those surfaces caps how many checks a turn runs or how
long they take — the focused checks covering changed behavior stay available —
and each asks for the brief reason in the handoff `Notes`, so a repeat is an
argued decision rather than a habit. The wording is shared by the task packet
(both the implementation and review roles), the `session-run` skill's shared
execution path, and the `## Child scope` of every generated repair child. The
skill also says outright that "leave the contract's checks passing" describes
the state a session hands over rather than instructing the worker to rerun each
check; its manual `asl verify` step remains the one authoritative run in that
mode. A generated repair child carries one extra rule that only applies to a
split repair cycle: when the evidence supplied to the child already attributes
a failure to another repair group's findings, that attribution is the
explanation and the failure belongs to that group, so the child investigates
further only when what it observes contradicts the supplied evidence, and
records the contradiction in its Handoff when it does.

Whether a live worker *behaves* that way is a different question from whether
the surfaces say so, and prompt tests cannot answer it. The evidence lives in
`tests/fixtures/asl_worker_verification/`, opt in and outside ordinary test
execution. Its `evaluate.py run` drives the candidate checkout's own
`agent-session-loop` wrapper against a disposable repository, plan directory
and state directory across three scenarios — a small repair with focused
validation available, a defect a focused check has to reveal, and a repair
beside a documented failure another group owns — twice each, in fresh
conversations on the harness the sampled history is dominated by. Its
`evaluate.py check` is read-only, needs no credentials, and re-derives every
verdict from what the runs left behind: the invocations the check scripts
recorded as they ran, the run's own `verification_check_finished` events, the
handoff, and the repository's final state. A receipt that says "passed" is
metadata, never a verdict. Worker and runner are told apart by stripping the
surrounding harness's environment markers before the run, then reconciling that
derivation against the run's own gate events, so a mis-derivation reports as
contradictory evidence rather than as a quiet pass. Trial session files state
the task and nothing about verification ownership — a fixture that coached the
worker would be measuring itself — and `check` refuses evidence that is
missing, incomplete, contradictory, stale against the current policy or fixture
digest, driven by an injected harness, or reused across conversations. The
policy digest covers every line that assigns the final gate as well as the
lines bounding a repeated check — both packet roles' ownership paragraphs, the
"do not rerun the full Completion Contract solely as a final gate" sentence
wherever a runner-managed worker reads it, and the skill's ownership note — so
rewriting or removing an ownership instruction ages the cached trials out
rather than leaving them looking current; manual mode's own verify step stays
outside it, because trials run runner-managed.
`tests/test_asl_worker_verification.py` covers all of that with fake harnesses
and synthetic receipts, and launches no model.

Autonomous `asl run` turns prompt the harness with a Python-built session task
packet (`task_packet.build_task_packet`): a harness-neutral prompt that references
the session file, `context.md`, `progress.md`, expansion notes, and the
predecessor handoff by absolute path and embeds no readiness, lock, or
session-selection policy — the runner owns all of that. Packet turns do not
invoke the manual `session-run` skill; that skill remains the manual
one-session-per-conversation path. Setting `ASL_WORKER_MODE=skill` in the
runner's environment is a temporary rollback that restores the legacy
skill-invocation prompt for autonomous turns.
