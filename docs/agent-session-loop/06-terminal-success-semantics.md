<!-- Continuation of the previous part. -->

Grading by session needs a session, and the runner records the same guard both
in the turn result and in the pause it raises, so a violation the guard itself
left unattributed is first correlated to the session named by a scope-violation
pause that recorded the very same violation — which rejoins the two halves of
one write into one claim instead of reporting it twice. Sameness is decided on
the guard's own violation list, the repositories it names and the paths inside
them, because that is the only durable evidence two records describe one write:
an unattributed record naming a repository or a path no pause named is a
second, distinct violation and keeps its own unattributed reading, and a
violation two pauses attribute to two sessions stays unattributed because
choosing between them would be a guess. Where nothing durable names a session,
the violation can still be caught, but only in a bundle that recorded no commit
at all: with no session to match a commit against, every commit in the bundle
is a candidate for having carried the write, and reporting a medium catch on
the strength of "nothing was committed" would be asserting evidence that was
never checked. An unattributed violation alongside any commit is therefore an
escape.

The one commit a run makes that no accepted turn produced is graded by
`guards.paused-work-settlement`. It reads the `paused_work_committed` record on
its own terms: the outcome has to be one the settlement actually writes, a
`failed` one has to say where its reason was recorded — the caught error or the
underlying commit's own classification — and has to leave the group among the
ones the run ended owing, and a success has to agree with itself about residue,
since `committed` and `committed-with-residue` differ by nothing else. The
record also has to be mirrored into state under `paused_work_commit`, because
the append and the write happen under one lock and every other reader reads the
mirror. Grading it is what earns the record its exemption from the
unknown-anomaly catch-all, which had reported every successful settlement as a
failure nothing classified: the record carries the pause's reason, and a reason
is what that catch-all reads as failure.

The same settlement is also the one epilogue a run is allowed after its terminal
record. It commits deliberately downstream of `run_paused` — the revert, the
plan re-read and the pause must all have happened before it can know what it is
committing — so `observability.terminal-artifacts` exempts the commits of a
`session-paused` group opened after that record, and only when the block closed
on a `paused_work_committed` record. A settlement that died mid-commit is work
after the terminal record in exactly the sense that check means, and so is any
other post-terminal commit. The correlation is on the group id rather than on
the commit payload's scope, because a paused group's commits are stamped
`scope: "session"` like every other session commit.

A run that paused on a harness failure is graded on whether its own records
agree with each other, not on whether it retried. The lifecycle lane reads
`pause_details.failure_classification` against the persisted `pause_policy`:
the `retryable` flag has to match what the reason itself classifies as
retryable, and a failure marked retryable has to sit under a policy that is
`recoverable`, sets `requires_user` false, and grants at least one relaunch —
with any of those missing, nothing will ever pick the session back up. The
retry is deliberately not looked for inside the run, because the supervisor
performs it by launching a fresh run, and a bundle is a single run that can
never contain it.

Naming a reason also stops that session's `turn_finished` and
`turn_execution_finished` records from reaching the unknown-anomaly catch-all,
which otherwise reported every classified harness failure a second time as a
failure no deterministic check classifies. The exemption is deliberately narrow:
it covers those two record names for the one session `pause_details` names,
while the run is actually paused. Another session's failed turn, another record
type from the same session, a stale classification on a run that has resumed
past it, and a pause that named no reason at all are all still anomalies
nothing explains.

A turn an operator killed is accounted for the same way. Every
`abort-turn-honored` run-control record has to trace back to an
`abort-turn-requested` naming the same session and turn index, and has to carry
the `abort_reason` the request gave; a turn the run killed with neither behind
it is a finding, because nothing then says who asked. When the trace holds, that
one turn's records are exempt from the unknown-anomaly catch-all — keyed on
session and turn index together, so a later turn of the same session that failed
on its own is still reported. Without this the deliberate kill read as a failure
nothing could explain, since the run never paused and so recorded no pause
classification to grade.

The semantic auditor answers in a versioned JSON contract, and its second
version replaced implicit identity with explicit selection. Every finding
carries exactly one `identity` object with no default: `{"decision":
"existing", "issue_id": "ASI-N"}` says the defect is the issue the supplied
catalog already names, and `{"decision": "new"}` names no id and asks ASL to
allocate one. An answer at the older version carries no identity object and is
rejected rather than read as a request to mint an issue for every finding. A
missing or malformed decision, an unknown key beside it, an unparseable id, or
an id on a `new` request fails the attempt as `malformed-output`; an id the
supplied catalog does not list fails it as `invalid-reference`. Fingerprints
stay ASL's to compute from the finding itself, so a wrong selection misfiles one
finding instead of corrupting deduplication elsewhere.

The catalog handed to the auditor is the projection's identity catalog rendered
as JSON inside the prompt, covering every lifecycle state so a defect that
recurs after being resolved, dismissed, or accepted still selects the issue that
owns it. It is bounded to 64 KiB: when the inbox is larger, the listing keeps
the most recently seen issues, lists them in issue order, and reports how many
the bound left out. An omitted id is refused as unselectable rather than as
nonexistent, because telling an auditor that an issue does not exist when it
merely did not fit is what would teach it to invent ids. The digest names the
whole inbox rather than the listed sample. An unreadable or missing ledger
supplies the empty catalog instead of failing the audit, which honestly leaves a
new allocation as the only available answer. The single retry repeats that same
catalog, restates the identity rule, and still withholds the rejected answer.

Selecting from that catalog leaves one answer ASL cannot check against a
listing, so a request for a new allocation is matched against the inbox before
it is accepted. Matching queries the catalog's indexes rather than scanning
every pair, so it costs the number of issues that share an invariant, a file, or
a word with the finding. An exact fingerprint and an exact ASL-owned identity
key are matches and are reused outright, because a fingerprint is ASL's own
derivation from the finding's category, component, and invariant key and a
reworded summary does not make the defect new. Everything weaker is a candidate:
an identical invariant key under a different category or component, the same
source file plus an overlapping invariant-key segment, or normalized-summary
Jaccard overlap of at least 0.60. A shared file alone is not a candidate, and
neither the component path nor the category is split into segments for that
test, because every sibling module shares its directories and a category is a
bucket name.

A candidate is never applied. It goes back in the one retry as JSON naming what
was matched on, with the listing widened so a candidate the 64 KiB bound had
dropped is selectable and the digest unchanged, since it names the whole inbox
rather than the sample. If the corrected answer still declares the finding new,
the audit is `unavailable` with reason `identity-ambiguous`, no issue is minted,
and the deterministic findings are published as usual. The attempt is recorded
as `invalid-reference` rather than under a new token, because that vocabulary is
a closed enum persisted inside audit reports. The candidate retry comes out of
the two-answer budget rather than adding to it, so an answer that is malformed
once and ambiguous next never sees the list.

A caught mutation opens its critical issue while the attempt is still running,
so the terminal publication names that issue ID in the run summary rather than
recording the same mutation a second time. It counts as new when this run opened
it and as recurring when an earlier run did, which is the rule an allocated
finding already follows, and it makes the run's most severe issue copyable from
`asl run`, `asl status`, the Workbench, and the Autopilot instead of only from
the inbox. A named ID must be one the ledger actually allocated: publication
resolves it through the merge chain under the lock and refuses the whole append
when it owns no issue, so a summary never claims an issue the inbox cannot
show.

Publishing that audit is one transaction, and the auditor's selections are
requests it revalidates rather than identities it accepts. Under the append
lock, each finding is resolved in one order: an exact fingerprint, then the
ASL-owned identity key already bound to that fingerprint's issue, then the
selection the audit made, and only then a new ID. Anything earlier overrules
what follows it, so a defect the ledger already names is not made new by an
answer that could not see it. A selection is checked against the issues the
ledger actually allocated; one naming an issue another publisher has since
merged away follows the merge, since the merge says the two were always one
defect, while one naming no issue refuses the whole publication. A fingerprint
first seen under a selected issue is attached to it by being observed there, so
the next exact sighting recurs without anyone being asked again. The summary,
every observation, and the `issue.asi-N` binding of every ID the transaction
minted are a single write, with each binding after the observation that opened
its issue, so an ID never exists without the key that names it. Because every
route reads state taken inside the lock, a decision made against a catalog that
has since moved cannot mint from what it saw; the digest it was reviewed against
is compared there and a publication that outran it is reported as refreshed
rather than refused. The two canonical reports are staged under
`supervisor-audit-staging/` in the run directory and hard-linked to their
canonical names only once that resolution succeeds — both or neither, with the
filesystem refusing a name already taken. A failure afterwards gives the names
back, so a run that could not publish leaves no report, no phantom summary, no
orphan ID, and no duplicate observation, and stays retryable.

Interrupting an audit turn cancels the audit rather than retrying it. The first
Ctrl+C ends the attempt, the verdict is `unavailable` with reason `interrupted`,
terminalization still completes, and the run keeps the exit code it had already
earned.

Ordering inside one run is fixed. Every ending reaches the terminalization
coordinator exactly once, the terminal adjudicator runs there first, the audit
second, and the audit's artifacts and ledger records are on disk
before `run_finished` is appended — so the terminal event and the state
projection carry the same verdict rather than two. The outcome and the exit
code a caller carries into the coordinator are a proposal that only the
adjudicator may overturn; auditing changes neither: a finding, a failed audit,
or a failed publication never turns a successful run into a failed one, never
rewrites a pause reason, and never touches the artifacts the run recorded.

### Terminal success semantics

Success has one definition, applied by one adjudicator to every terminal path —
normal completion, review exhaustion, a resumed run, an operator's
stop-after-current, the failure paths, and the refusals raised above the loop.
A run may report
`ok: true` only when every required verification reached `passed`, and a plan
may call itself finished only when a final review came back all-clear. Anything
else becomes a pause with a stable reason:

- `required-checks-incomplete` — a required verification never reached
  `passed`. That covers an unpassed turn verification and, more often, a broad
  plan gate that a findings review deferred under
  `findings-defer-broad-failures` and that no later all-clear review cycle ever
  redeemed. `pause_details` carries `deferred_unresolved` (the deferring run ID,
  review session, turn index, and outstanding check names) and
  `unpassed_verifications`.
- `review-not-clear` — a run reporting `all-complete` whose final review is
  missing or did not come back all-clear. `pause_details` carries
  `review_completed`, `review_fix_disposition`, `review_outcome`,
  `unresolved_findings`, `findings_by_severity`, and `review_skipped_reason`.

Only `all-complete` is asked for a review: `stopped` and `max-sessions` report
the early exit an operator asked for and never claim the plan was reviewed to
the end. A dry run and a run already paused keep the verdict they arrived with,
because the specific pause is the evidence an operator needs. A run that is
failing but not paused is still adjudicated, and only the required-checks clause
can fire on it: ASL's one such ending is `review-findings-unresolved`, whose
repair loop by definition never converged, so an unredeemed deferral it still
owes becomes a `required-checks-incomplete` pause a resume re-evaluates rather
than a stranded failure. The narrower reason survives on
`terminal_adjudication.claimed_ok`/`claimed_reason` and on the `action_required`
record the review path already wrote. Every ending records the claim it was
tested against under
`terminal_adjudication` (`claimed_ok`, `claimed_reason`, `blocked`, `reason`,
`details`), in `state.json`, the `run_finished` event, and `asl run --json`.

Deferrals are read from the plan's append-only `events.jsonl` rather than from
one run's turns, so a resumed run re-derives the same blocker instead of
forgetting it, and the blocker clears only when the evidence clears — a later
all-clear review cycle over the repaired tree. A deferral whose own cycle
materialized no repairs (the cap disabled or exhausted it) never bought a delay
and is never discharged. Both reasons classify as `inspect` rather than `reply`,
since no mailbox answer makes a failing gate pass, and both carry a preflight
repair hint in `action_required.remediation` naming the pause details to read.

That policy and the lanes feeding it are declared rather than coded as
branches. `agent_session_loop/verification_transitions.py` holds the
per-session verification machine (`scheduled` into `contract-ready`,
`running`, and the settled `passed`, `deferred`, `failed`, `unenforced`, and
`refused`) together with the single terminal adjudicator above, so the
required-check and final-review clauses live beside the states whose evidence
they read and no path can grow a second definition of success.
`agent_session_loop/review_transitions.py` holds the review-verdict machine
(`submitted`, `retrying`, and the settled `accepted`, `exhausted`, `declined`)
and the review-cycle machine (`reviewed`, `materializing`, and the settled
`settled`, `reopened`, `refused`), which makes review exhaustion, deferral,
repeated findings, the per-group repair cap, and rereview named states rather
than conditions. Both build on the shared `transitions.py`, whose machines
validate their own tables and raise `IllegalTransitionError` on any undeclared
step, so an out-of-order verification or review step fails loudly instead of
falling through. The modules return frozen decisions only; pausing, reverting,
and every durable write stay in the runner, and the durable event and result
schemas are unchanged.

Everything that races the run loop from outside it is declared the same way.
the `agent_session_loop/run_coordination_*.py` modules own the three durable
slots a non-loop actor writes: `waiting_for_reply` belongs to a `WaitStateStore` whose
every begin, lease stamp, deadline rebase, and clear is a compare-and-set
against the question that opened the wait, so a lease probe for a wait the loop
has already finished is dropped rather than resurrecting it; `control` is
written only through `record_run_control_action`, so an event and the control
payload can never disagree about what was requested; and `assurance` is
published by a `TerminalAssuranceCoordinator` that closes its finalization
phase whether the audit rendered a verdict, was cancelled, or raised. A
`MailboxWaitCoordinator` brackets one reply-capable pause end to end — the wait
is announced before it can block and cleared after it stops, including when the
poll raises — and a `SupervisionCursor` carries the lease-and-clock ratchet a
bounded wait advances, where a fresh lease is the only thing that rolls the
unsupervised ceiling forward and a suspend restarts it at the wake instant. The
exactly-once reply guarantee is unchanged and now stated in one place: the poll
loop reads the mailbox without its lock, so `mailbox_waits.finish_wait` re-checks
under it and whatever closure it finds there overrides the timeout the loop
arrived with. Like the lane modules, the `run_coordination_*` modules never import the
runner — serialized persistence, the AgentMux notification seam, and the
compacted state projection the audit reads enter through the
`CoordinationPersistence`, `WaitNotifier`, and `state_snapshot` seams — and the
runner keeps a module-level alias for each moved symbol so existing
`mock.patch.object` targets still resolve. Coordination no longer depends on
incidental mutation of runner-owned dictionaries.

The typed boundaries carry a ratchet rather than a convention.
`tests/test_asl_typing_ratchet.py` fails if a code joins the repository-wide
`disable_error_code` baseline, if any per-module override adds a suppression of
its own (mypy unions those with the global list, so a narrow disable is a
repository-wide one wearing a shorter name), if a module leaves one of the
two `enable_error_code` lists, or if a module in the package is on neither of
them. That last check derives the package tree rather than reading a list, so a
module added tomorrow has to pick a table instead of inheriting the global
suppressions in silence.

`pyproject.toml` carries two such tables, under two contracts. **Boundary**
gets the three codes *and* a linter on the shape mypy would otherwise have to
trust: every public signature fully annotated, no durable payload crossing as a
bare `dict`, `Mapping`, or `list` — anywhere in the annotation, including
inside a union or a generic argument, so `dict | None` and `list[dict]` are
rejected while `list[dict[str, object]]` is not — no `type: ignore`, and no
import of the runner. **Checked** gets the same three codes and nothing else — it is where a
module lands when it is clean under mypy but cannot meet the extraction rules,
starting with `compact_state` and `workbench`, which drive the runner and so
could never satisfy the one-way dependency Boundary enforces. `runner` itself
is on Boundary: the rule bans importing the runner, not being it. An overrides
table cannot name itself (mypy rejects an extra key), so the ratchet resolves
each by an anchor module only that table lists.

A new package module is required to be on one of the two tables, so this is a
step the ratchet fails without rather than a convention. A future extraction
adds its module to the Boundary table in `pyproject.toml` and to
`REQUIRED_RATCHETED_MODULES`; a module that is mypy-clean but not linter-clean
goes to the Checked table and `REQUIRED_CHECKED_MODULES` instead. Those two
registrations are the whole ratchet step: the test lanes in
`tests/run_suite.py` are derived from *test* modules, not package modules, so a
package module never belongs in one. A new `tests/test_*.py` module the
extraction brings joins the fast lane by default, or declares
`ASL_TEST_LANE = "integration"` when it spawns external processes or is
otherwise too slow for it.

Optional post-run assurance stays advisory and never blocks. A run whose audit
landed `unavailable` or `partial` keeps the success it earned and carries an
additive `assurance_degraded` block naming `assurance_state`, `reason`,
`blocking: false`, `run_ok`, the audit's `issue_ids`, and the `evidence`
locators it did leave behind, so a reader can tell a run that was graded clean
from one that was never graded. A settled `skipped` audit is a decision not to
grade rather than a failure to, and records no degradation.

A terminated run also carries an additive `assurance_report` block beside its
`assurance` object, in `state.json`, in the `run_finished` event, in
`asl run --json`, and in `asl status --json`. It repeats the audit state and
eligibility and adds the copyable issue IDs, the new and recurring counts, the
absolute path of `supervisor-audit.json` in the run directory, the corpus-wide
open issue count, and `list_command` (`asl assurance list`). Findings carried by
an `unavailable` verdict are flagged `partial: true`, because those are the
problems the audit managed to see rather than all of them. When the stored
report shows a lane it could not judge, the block also carries `lanes_judged`,
`lanes_total`, and `lanes_without_evidence`, derived by reading that report
rather than stored in the run's assurance object, and the audit line names them:

```text
Supervisor audit: partial (6/8 lanes judged; no evidence: resources-stalls, unknown-anomalies)
```

Text output renders the same three lines:

```text
Assurance issues: 1 new, 1 recurring; ASI-1, ASI-2
Assurance inbox: 4 open; `asl assurance list`
Assurance report: /path/to/runs/<run-id>/supervisor-audit.json
```

When the ledger cannot be read, the open count is reported as unavailable with
the reason rather than guessed, and the command still succeeds. The block is a
sibling key rather than a field inside `assurance`, because the persisted
assurance object is parsed with unknown keys refused. `asl status` renders all
of this without being able to produce any of it: the projection module imports
only the assurance ledger, models, and projection, so nothing reachable from a
status read can launch a harness. Status keeps its existing abandonment
reconstruction, which records the audit debt and still launches nothing.

After a review session completes with structured findings, the runner drives
bounded same-plan repair/rereview cycles: it materializes one pending repair
child per `repair_group` beneath the review session (`N.1`, `N.2`, ... via
`agent_session_loop.review_repair`), executes the children serially with the
implementation harness, then appends the next top-level review session and
rereviews. In a registered plan, every finding must name its repository. Each
generated child reads the full ordered registry and writes only the aliases
named by findings in that repair group; mixed-repository groups retain every
such alias. The child completion contract preserves each check's `repo`, and
same-name checks that disagree across aliases fail closed before any plan file
is written. The ordinary workspace write guard enforces these generated scopes
with the same pause and retained-change behavior as authored sessions.
`--max-review-fix-cycles N` is an **absolute cross-run cap** on
unique source-review materializations (default 4; `0` still runs the review but
never creates repair sessions; `--max-review-fix-plans` is a deprecated
compatibility alias — passing both flags is an input error). `--max-repair-group-attempts N`
sets the default per-group reopen budget (default 2), and
`--max-review-fix-cycles-ceiling N` sets the absolute ceiling a resumed run may
raise `--max-review-fix-cycles` to without an explicit new override (default 8).
A source review counts once when it produced repair children, including legacy
`not-loop-ready` histories and crash-recovered reopen records; ordinary cycle
and reopen evidence never double-count it. The minimum cap needed for another
materialization is therefore `historical_materializations + 1`, not the prior
run's cap plus one.

Materialization is all-or-nothing across the whole publication, not only across
the plan transaction. Two gates run before any file is written, both
non-bypassable: the repository-footprint gate described above, and a contract
gate that resolves every proposed check against the same
`VerificationContext` preflight builds. A repair check naming a `repo`/`cwd`
the plan's registry cannot resolve is refused there, with the plan untouched,
rather than authored and then rejected by preflight — which is what used to
strand a pending child no run could select or clear. Planning itself refuses a
third case, before either gate: a group whose evidence paths or verification
checks name a `session-<id>.md` or `session-<id>.expand.md` **inside this plan
directory** other than the child's own is `malformed-review-repair`. A task
packet makes exactly one session file writable and the plan artifact guard
pauses any turn that changes another, so such a child cannot win either way —
the check either already passes and proves nothing, or it demands the very edit
the guard exists to refuse. Only plan-directory-anchored references count: a
repository file that happens to be named `session-<id>.md` is an ordinary work
target, and nothing but its location distinguishes it. Everything past that point
runs inside a rollback window: `<plan>/.asl-review-repair-publication/` holds
the fsynced pre-publication image of every path the publication may change (the
staged files, `manifest.md`, and `baseline-results.json`) plus a
`journal.json` naming the review session, the child filenames, the phase, and
the publishing pid. The image is durable before the first destination byte
moves. If the post-authoring executability gate blocks, the children are
withdrawn: the window is restored, `ReviewRepairResult.children` is empty,
`rolled_back` is true, and `rollback` carries the restored and removed paths.
The `not-loop-ready` pause reports both as `repair_children_rolled_back` and
`repair_rollback` in `pause_details`. The cycle record keeps what the cycle
planned as well as what it kept: an empty materialized list leaves
`repair_groups` and `repair_session_ids` empty and records the planned groups
under `withdrawn_repair_groups`, `withdrawn_repair_session_ids`, and
`repair_children_rolled_back`, so a cycle that specified repairs and kept none
is distinguishable in the durable record from one that specified none. The
withdrawn groups stay out of `repair_groups` on purpose, because
`review_continuation.repair_group_attempts()` counts one attempt per entry and
a group nobody ran must not spend a group's budget. `--allow-not-loop-ready` retains children
blocked only by bypassable findings; non-bypassable blockers withdraw them
regardless. A publication interrupted by a crash, a signal, or a failed atomic
write leaves the window behind, and recovery is always a rollback rather than a
roll-forward, because nothing on disk proves an interrupted publication would
have reached loop-ready; the review findings are unchanged, so the next cycle
re-plans. `review_repair.recover_review_repair_publication(plan_dir)` performs
it under the caller's plan lock, running
`plans_transaction.recover_pending_plan_transaction` first so a half-installed generation
is settled before the image is restored. `asl run` calls it before its first
plan snapshot, so selection never sees a non-loop-ready generated child, and
`materialize_review_repair_sessions` calls it before planning. A window whose
journal is missing never opened and is discarded; a window whose
`version` is not `REPAIR_PUBLICATION_SCHEMA_VERSION` is left in place and both
callers fail closed on it; a restore that errors makes `asl run` pause with
`reason: malformed-plan` carrying
`pause_details.review_repair_publication_recovery`.

Rereviews are delta-scoped when the runner can prove a narrower scope is safe.
Each review turn records the reviewed tree on its `review_cycle` record
(`repo_head`, `worktree_dirty`, and `dirty_paths` — every path whose reviewed
content differed from that commit, plus every `assume-unchanged` or
`skip-worktree` entry, whose relationship to that commit no diff can establish
either way), derived from the mutation guard's existing fingerprint at no extra
cost. Before appending the next review session the
runner picks a scope and appends a `review_scope_selected` event carrying
`scope`, `base_sha`, and `fallback_reason`; the same `review_scope` and
`review_base_sha` also ride on `review_session_created`. `scope: delta` (a
`## Delta Review Scope` section listing the prior findings to verify plus the
patch commands and changed files described below) requires *all* of: a prior
`repairs-materialized` cycle carrying a `repo_head`, loadable prior findings,
a base that resolves, is still an ancestor of `HEAD` (so a rebase or reset
cannot silently narrow the scope), and diffs cleanly, and either a clean
recorded worktree or the exact `dirty_paths` the reviewer saw. The changed-file
list is Git-derived only — `git diff --name-only --no-renames --no-relative
--no-textconv --no-ext-diff <base_sha>` unioned with its `--cached` counterpart
(`--no-relative` pins both
probes repository-wide and root-relative, which a `diff.relative=true` config
plus a nested `cwd` would otherwise narrow to that subtree), the untracked files
(scanned from the repository root and filtered by git's ignore rules, which are
the operator's declaration of what is not code under review — a genuine repair
never lands in a gitignored path; this deference is independent of
`--review-guard-include-ignored`, which governs the guard's evidence rather than
this scope), every tracked entry marked `assume-unchanged`
or `skip-worktree` (added wholesale, because the same blindness that hides a
repair to one makes it impossible to tell which of them moved), and those
recorded `dirty_paths` — so it
covers committed and uncommitted repairs alike, cannot omit a path the repair
sessions forgot to declare, and stays a superset even when a repair restored a
dirty file to its committed content, deleted an untracked one, staged an
edit and then restored the working file, or edited a flagged entry, none of
which the base-to-worktree diff still shows. The rendered section names two
commands rather than one — `git diff --no-renames --no-relative --no-textconv
--no-ext-diff <base_sha>` and
its `--cached` counterpart — so the reviewer sees the base-to-index patch too,
under the same scope and rename behavior the file list was derived with; a
staged-only repair is otherwise a path in the list whose bytes no command in
the section would print. Both rendered commands carry `--no-textconv` and
`--no-ext-diff` so they print git's own diff of the raw bytes rather than
whatever a configured textconv or external diff driver renders: a driver with
constant output (configured in `.gitattributes` and git config, both of which a
repair turn could itself write) leaves the name probes selecting the repaired
path while the patch commands print nothing at all. Nothing else can narrow a
rereview: a
missing, unresolvable, non-ancestor, or undiffable base (including an
undiffable index, `fallback_reason: index-diff-failed`, or an unlistable one,
`fallback_reason: index-flags-scan-failed`), or a dirty recorded
baseline with no path evidence (`fallback_reason:
dirty-base-evidence-missing`, which is what a pre-`dirty_paths` cycle record
yields), falls back to `scope: full` with the prior findings carried as extra
checks on top of a from-scratch reading, and a missing prior repair cycle or
unloadable findings yields a plain `scope: full`. The first review of a run is
therefore always full-scope. Delta scoping narrows only what the reviewer must
read: the output grammar, required artifacts, and verdict enforcement are
scope-agnostic.
