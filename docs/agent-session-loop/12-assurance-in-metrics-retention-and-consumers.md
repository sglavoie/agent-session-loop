<!-- Continuation of the previous part. -->

`asl assurance process-pending` audits the runs whose own process died before it
could audit itself. When abandonment is proven, the terminal reconstruction
writes `assurance-pending.json` into the dead run's directory and nothing more:
that reconstruction happens inside `asl status` and inside lock acquisition, and
neither may launch a harness or wait on one, so the debt is recorded there and
paid here. The record is self-contained — it copies the plan directory,
workspace root, and the run's own harness and audit settings — because the next
run of the same plan overwrites `state.json` and would otherwise take them with
it. The terminal `run_finished` event and the persisted state carry a bounded
`assurance_pending` object naming the run, when the debt was recorded, and where
the record lives. The abandoned run's own outcome is never rewritten: it died
abandoned and stays abandoned, whatever the later audit says.

Only records this feature wrote are ever candidates, so a state root full of
history that predates auditing enumerates as empty rather than as a backlog
nobody asked for. A record whose run wrote no assurance object at all carries
`skip: predates-assurance`, and one whose run already reached a verdict or opted
out carries `skip: audit-already-settled`; both are reported and neither is
processed in bulk. Naming `--run-id RUN_ID` processes that run anyway, which is
the only way an audit is ever run for old state, and it is one run at a time by
a person who asked for it.

A bulk pass is also bounded to the current code generation. The boundary is the
ledger's own newest `resolve` disposition: resolving an issue is the operator's
statement that a defect is fixed, and it is the only decision that claims the
code moved, since dismissing an issue or accepting its risk both leave the
behavior exactly as the audits found it. A pending record dated before that
moment describes a run of code the fix has since replaced, so grading it can only
re-report the defect the resolution already answered — which is how a backlog
reopens issues an operator closed. Such a record is reported as `skipped` with
`reason: superseded-generation`; it is never deleted, and `--run-id RUN_ID` still
audits it. A ledger that has resolved nothing sets no boundary and leaves every
record current, and a record carrying no timestamp is left current too, because
dropping a run over a missing field would hide it rather than date it.

Processing is serial and single-winner. Each run is claimed with a nonblocking
`flock` on `assurance-catchup.lock` in its own run directory, so two catch-up
passes divide the backlog instead of racing on it and a run another pass holds
is reported as `claimed` rather than waited on. The ledger is consulted inside
that claim, so a run already published is reported as `already-audited` and is
never published twice; repeating the command is therefore idempotent. A run
whose plan has moved on to a later run is left alone as `unusable` with
`reason: evidence-superseded`, because the evidence an audit would read no
longer describes the run it is owed for. `--limit RUNS` bounds the audits one
pass launches, defaulting to 20; it counts audits rather than records, so skips
and already-published runs cannot crowd out real work, and a non-positive value
exits `1` with `reason: input-error`. `--plan PLAN_DIR_OR_ID` narrows the pass to
one plan, and `--audit-harness`, `--audit-harness-model`, `--audit-effort`, and
`--audit-timeout` override what the dead run recorded, for the case where the
harness that would have audited it is the reason nobody can.

`asl assurance plan ISSUE --harness NAME --output PATH` turns an accepted issue
into a proposal a human reads and decides on. It writes exactly one artifact —
a Markdown document at `--output` — and produces no plan, no session, no
readiness, no commit, and no change to any repository. That boundary is
structural: the drafting module imports the ledger, the models, the projection,
the harness layer, and the state layer, and nothing else, so `asl split`,
`asl prepare`, `asl preflight`, `asl expand-ingest`, and `asl run` are not
reachable from it at all.

The drafter is given the canonical merged issue, its whole observation and
disposition history, any proposal already drafted for it, and the run
directories holding the evidence its findings cite. It runs a single turn under
the same restricted read-only access profile the supervisor audit uses, from a
throwaway working directory, with no `plan_dir`, no bypass, and read access to
the evidence run directories plus the read-only ASL package and skill trees. A
harness that cannot enforce that profile exits `1` with
`reason: harness-unavailable` rather than running under a weaker one. Those read
roots are fingerprinted by content on both sides of the turn: any change to them,
or an output file the agent created itself, exits `1` with
`reason: mutation-detected` and discards the reply, ahead of the exit code and
the timeout, because a read-only turn that wrote something has already disproved
the boundary the whole draft rests on.

The reply must be a non-empty Markdown document carrying the ATX headings
`Summary`, `Problem and Evidence`, `Proposed Changes`, `Test Plan`, and
`Assumptions`, once each. Heading detection ignores anything inside a code
fence, so a section named only in an example does not count; one outer fence
wrapping the whole answer is unwrapped rather than rejected. Anything else exits
`1` with `reason: malformed-output` and writes nothing.

ASL writes the validated document itself, never the agent. The file is created
exclusively and fullsynced along with its parent directory, so a path that
already exists is refused with `reason: output-exists` before a harness is
launched at all and drafting twice never overwrites the proposal a human is
reading. Only once the file is durable is `improvement_plan_drafted` appended to
the ledger, naming the issue, the harness and model, the output path, and the
document's SHA-256; `asl assurance show` then lists it. If that append fails the
document is kept rather than deleted and the error names where it landed, since
a validated proposal a human can read outranks a history that never mentions it.
`--timeout SECONDS` bounds the single turn and defaults to 600; drafting is
never retried, because a rejected reply is a refusal an operator sees rather
than a budget to spend twice.

### Assurance in metrics, retention, and consumers

`asl metrics` adds an `assurance` section, aggregated once from the shared
ledger rather than per plan, since the inbox spans every plan. Coverage and
correctness are reported separately: `coverage` counts eligible runs, ineligible
runs, runs that reached a terminal verdict, and the resulting rate, while
`audit_states` counts all five states so a window with no audits and a window
with no findings never render the same. `issues` reports sightings, new versus
recurring, findings by severity and by category, the open backlog now, and
recurrence after resolution — replayed over the window, because a reopen from a
year ago says nothing about the last seven days, and reported as corpus-wide
totals beside a `recurrence_after_resolution_by_category` map, because a
planner seeding a premortem needs which class of defect came back rather than
only that one did. It also reports how much of the backlog a machine can name:
`bound_total` counts the canonical issues carrying an identity key and
`unbound_total` those still reachable only by number, taken over issues rather
than over allocated ids, because a key an issue absorbed through a merge names
that issue now. `integrity` counts the anomalies and rejected lines replay
found, with `identity_key_conflicts` — a stored key claiming a second issue —
counted apart from `duplicate_identity_bindings`, since only the first is a
contradiction. `dispositions` reports the
counts by kind, the dismissal and accepted-risk rates, and the time from an
issue's first sighting to its disposition. `closeouts` reports the outcomes,
escaped issues, and rework minutes, counted per run through the projection so a
superseding verdict replaces the one it corrects instead of being counted twice.
Every count is windowed by `--since-days` except the open backlog, which is a
fact about now. An unreadable ledger yields `available: false` with the reason
and zeroed counters rather than a failed command.

`asl gc` never collects the ledger: `assurance-inbox.jsonl` sits directly under
the state root and the sweep only ever removes `runs/<run-id>/` directories, and
the report states that as `ledger_excluded: true` beside the ledger path. Run
directories referenced by an open issue are retained with reason `open-issue`,
ahead of the age cutoff and the newest-run rule but behind the plan lock and the
live-run check. Pinning is by run ID alone, because that is all an occurrence
records. Once every issue naming a run is resolved, dismissed, or accepted, the
pin is released and the run becomes reclaimable on the ordinary schedule while
the ledger keeps its whole history. References that no longer resolve to a
directory anywhere under the state root are reported as
`missing_run_references` rather than hidden, so an operator following an issue's
evidence trail learns up front that the artifacts are gone.

A ledger that cannot be read at all fails closed instead of reading as an empty
inbox. The report carries `available: false` with the reason and
`collection_blocked: true`, and every run in scope — plan scope and corpus scope
alike — is retained under `pin-unknown`, ahead of every rule but the plan lock
and the live-run check. The dry-run text says so in words, and `--apply` removes
nothing until the ledger is readable again: an empty pin set read off a ledger
nobody could parse states an absence of knowledge rather than an absence of
pins, and the deletion it would otherwise license is unrecoverable. A ledger
file that does not exist yet is not this case — that is a readable empty inbox,
and it pins nothing.

The Workbench summary and the Autopilot supervision result carry the same
verdict in bounded form: state, eligibility, reason, up to eight issue IDs with
`issue_ids_truncated` when there are more, the new and recurring counts, the
partial flag, the one-line status text, and the inbox command. Neither carries a
field with which to configure, launch, retry, or dispose of an audit, and the
Autopilot's human summary names what the audit found rather than deciding
anything about it. All existing state, event, status, metrics, retention,
Workbench, and Autopilot fields keep their meaning; every field described here
is additive.

### Execution timing accounting

`agent_session_loop/execution_timing.py` reconciles per-run timing and is a pure
collector: it reads no files, holds no clock, and prints nothing. Callers supply
the evidence and own the window; `asl metrics` is its only operator-facing
consumer today. Three rules explain most of what its numbers mean, and a reader
comparing two of them needs all three.

**`unattributed` is a residual, not a measurement.** A run's attributed time is
the sum of its turn, verification, commit, and assurance phases; whatever the
wall clock has left over is the runner's own orchestration between phases,
including the idle gap before a turn. It is reported unclamped when it is
substantially negative — phases claiming more time than the run lasted is a
contradiction, and rounding it up to zero would make an impossible total look
plausible. For the same reason a finished verification, commit, or assurance
phase whose length nothing states — no usable `duration_seconds` and not enough
timestamps to span one — contributes no seconds and leaves a
`phase-timing-unknown` gap. Two matching event names are evidence that the phase
*ran*, not that it took zero time; without the gap its seconds would silently
join `unattributed` while the run still counted as `complete`, putting a
fabricated zero-length phase into the share basis. The run stays included and
every duration it does know stays attributed; only its eligibility to back a
share is withdrawn. An explicitly recorded `0.0` is a measurement and is
unaffected.

**Check work is not phase time.** A verification phase contributes its elapsed
span. The per-check durations are summed separately as cumulative check *work*
and never added to a phase, because a parallel recipe runs several checks at
once and their durations would otherwise claim the same seconds twice. On a
corpus of serial recipes the two figures nearly coincide; on parallel ones the
work figure is legitimately larger. An individual check is identified by
`(run id, turn index, check index)`, keyed identically by the timing collector
and by the metrics scope ranking, so `verification_checks.checks` and
`verification_checks.scoped_checks` count the same executions. A repeated
`verification_check_finished` restates one execution rather than reporting a
second: the first usable duration stands, and the repeat becomes a
`duplicate-evidence` anomaly when the two agree within the tolerance and a
`conflicting-evidence` one when they do not, which takes the run out of the
share basis. A record missing either identity field cannot be distinguished
from another such record, so it reconciles into the shared slot both collectors
use and leaves a `missing-identity` gap: the check count is then a floor.

**Known durations and shares answer different questions.** Every run lands in
exactly one trust classification — `active` (no terminal record), `inconsistent`
(an anomaly: an invalid number, a duration contradicting its own timestamps, two
sources contradicting each other, overlapping phases, a finish with no start, or
phases exceeding the wall clock), `partial` (a gap: a missing start, an unknown
wall duration, a reconstructed terminal record, a turn with no timing, a turn of
unknown kind, a finished phase of unknown length, or a start with no finish), or
`complete`. Measured seconds
aggregate over every included run so a partial run's real seconds are not thrown
away, while shares aggregate over `complete` runs alone, because a share is a
claim about a whole run and a run missing a phase would understate that phase for
everyone. The two are published side by side and are deliberately not
interchangeable.

A run enters the window by its finish time and then contributes its whole
duration. Dry runs are excluded on the evidence of `run_started.dry_run`; a run
whose start record is absent cannot be shown not to be one, so it is kept and
marked rather than assumed. Timing for one turn is taken from
`turn_execution_finished`, then `turn_finished`, then the turn's `result.json`,
because the execution event is written by the code that watched the process
exit while the other two are copies made later; a lower-precedence source
disagreeing by more than the tolerance neither overwrites the chosen value nor
is discarded, but becomes a `conflicting-evidence` anomaly. Records the
collector cannot attribute to a run are counted in `unattributable_records` and
`unattributable_turn_results` rather than dropped, so a caller can see that
evidence went unread; plan-scoped events such as `preflight_skipped` carry no
`run_id` and land there legitimately.

### Comparing timing across later natural runs

Timing is observational: it re-reads the evidence the loop already writes and
adds no traversal, no clock, and no run of its own. That makes a comparison
between two reports cheap, and also makes it easy to compare two things that
were never the same measurement. The procedure below is the one to repeat when
asking whether a later batch of natural runs actually moved.

Take the baseline and each later reading the same way: `asl metrics
--since-days <n> --json`, keeping `execution_timing.window_start`,
`execution_timing.coverage`, `execution_timing.phases`, and
`execution_timing.review_scope`. The window always ends at the moment of the
call — `window_end` is `null` for a rolling window — so two readings never
cover the same interval, and a run enters a window by its *finish* time and
then contributes its whole duration, including any part that elapsed before
`window_start`. Neither figure is wrong; they answer "what finished in this
window", not "what elapsed in this window".

Read coverage before reading any number it qualifies. `runs_included`,
`runs_outside_share_basis`, `exclusions`, and
`included_runs_by_classification` say which runs the seconds and the shares
were computed over. A share that moved because the share basis gained or lost
runs is not the workflow moving, and `seconds` totals across windows of
different sizes are not comparable at all — compare `phases.shares` and
per-plan rows in `execution_timing.plans`, not corpus totals.

Then compare like with like. The split between `implementation` and
`generated_repair` is what a change to the repair lifecycle is expected to
move; `review_scope.by_scope` and `review_scope.fallback_reasons` say whether
rereviews are narrowing to a recorded base or falling back to full scope, which
is the mechanism behind that split. Cumulative verification check work lives in
`verification_checks.cumulative_seconds` and is reported beside, never inside,
the `verification` phase, so compare it against the earlier check work figure
rather than against a phase.

An independent recomputation is worth doing once per release rather than per
reading: sum `turn_execution_finished`, `verification_finished`,
`commit_finished`, and `verification_check_finished` durations straight out of
`events.jsonl` for a fixed set of run IDs, and reconcile against the same runs
in the report. Differences that are expected and not defects: runs whose
evidence predates a field, run directories that hold only a supervisor audit
and never emitted `run_started`, and the pre-window portion of a run that
straddles `window_start`.

Rollback is a plain revert. The code can be reverted without rewriting any
historical state, because every change to persisted data is additive: reopen
records gained baseline fields that older readers ignore, and the
`execution_timing` section is a new key that a reverted `asl metrics` simply
stops emitting. No migration, backfill, or state rewrite is involved in either
direction, and the reader-side recovery that reconstructs a missing reopen
baseline works in memory and never edits an event. If a revert also takes the
skill text with it, resync the installed copies with `just sync-skills` and
confirm with `just verify-asl-install`.
