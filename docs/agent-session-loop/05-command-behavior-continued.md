<!-- Continuation of the previous part. -->

For a registered plan, both packet and rollback prompts include the ordered
credential-free registry, the selected session's exact read and write aliases,
and the `Alias:path/to/file` handoff/reference rule. They reveal
absolute resolved roots only for aliases in that session's read scope. The
harness invocation cwd is the bound workspace root. Before every registered
implementation turn (including reply and resolution resumes), the runner
captures fail-closed content evidence for exactly the repositories the plan
registered, and for no others. It repeats the capture before handling any
harness result. Changes pass only for repositories named
by the session's write aliases; an empty write set makes the turn read-only.
Changes in read-only or out-of-read-scope registered repositories and all
added or removed registered repositories pause as
`workspace-write-scope-violation`. Unavailable repository or fingerprint
evidence pauses as
`workspace-write-guard-unavailable`. Both post-turn failures retain repository
contents, restore the handoff to pending, record qualified evidence, and run
before verification or commit.

`--allow-external-changes ALIAS` (repeatable, registered plans only, persisted
for `--resume`) declares a registered repository someone else may keep
changing while the run works — typically the operator editing a repository
that early sessions only read. A content change there during a turn whose
write scope excludes the alias is recorded under the guard record's
`external_changes` (writer unknown) instead of pausing; adding or removing a
checkout still pauses. The guard can no longer tell the worker's writes there
from anyone else's, so the pre-tool policy refuses the worker's file-edit tools
in that alias for the turn (`external_read_only_aliases`, Claude only; a shell
write is not predicted). A session that writes the alias owns every change in
it as before, and a per-session commit stages those changes, so stop editing
that repository before such a session runs. An unregistered alias, or the flag
on a legacy plan, fails the run. Scope violations require a durable scope repair
and fresh expansion/preflight before continuing. A later run that finds such a
violation still uncleared, and a readiness gate that did not pass, refuses
before the loop starts and names the retained record rather than the gate —
but only while the durable scope still excludes what the violation named. The
retained record froze the aliases the session held when the guard rejected it,
so reading it alone can never observe the widening the refusal itself asked
for, and the retained changes are what keep the gate red: the run refused
forever on evidence about the past. Every violation naming an alias the session
may now write is therefore treated as answered, and the run proceeds to the
turn-level repair check, which re-derives the whole verdict from freshly
captured evidence and still pauses when the retained changes really are out of
scope. Nothing else clears the refusal: an unregistered repository, which no
scope edit can authorize, an incomplete observation, a scope that cannot be
read, and a record with no violations to judge all keep it. When the retained
session is
an ASL-generated repair child, the runner performs part of that repair itself:
before the turn starts — ahead of repair commit preflight, task-packet
rendering, the persisted active-turn prompt and the write guard — it widens the
child's persisted **Writes repositories** in place to the union its own findings
require, re-reads the scope from the rewritten file, and records the change
under the repair evidence's `write_scope_regeneration`. The rewrite widens only
and touches only generated children, so a human-authored session's declared
scope is never rewritten and an alias the current findings no longer require is
never removed. Because the file itself is rewritten before anything reads it,
preflight, expansion, the completion contract, prompt rendering and the guard
all keep reading the same single scope. Commit preflight normally refuses to
start a repair child whose writable repositories are dirty; on this retained
path it sanctions exactly the paths the retained violating turn's own guard
record names as changed *and still holding the content that turn left at them*,
recording them under `retained_scope_repair` as
`sanctioned_dirty_paths` beside the `retained_turn_paths` they were proven
against, because the runner is the author of that dirt and demanding a clean
tree would dead-end the very repair the retention exists to enable. A pathname
is not an origin, so a scope-violation guard record keeps the turn's whole
post-turn content vector (`snapshot_after`) beside the pre-turn one, and
preflight recaptures the writable repositories under the exclusion policy that
record itself names and compares the two. A retained path whose worktree
content or index state has moved since — an operator edit or an operator
`git add` between the pause and the resume — is reported under
`content_drifted_dirty_paths`, loses its sanction, and pauses the run. When
that comparison cannot be made at all — a record written with no post-turn
vector, a writable repository the vector never covered, a capture failure, a
repository that has since disappeared — `content_evidence` carries
`available: false` with the reason and nothing is sanctioned, because an
unproven path is not a proven one. The
sanction stops there. A repair child's commit is repository-wide, so any other
dirty path — work started after the pause, in a repository the child is about
to touch — would be published under the repair's name; preflight instead
pauses as `commit-preflight` before the harness starts, naming those paths as
`unsanctioned_dirty_paths` and the repositories holding them. Preflight
enumerates untracked files individually and reads Git's NUL-framed status, so a
retained path inside a newly created directory, or one whose name would
otherwise be quoted, still matches the recorded evidence; anything the guard
could not observe is not proof of origin and is treated as unrelated. Nothing
escapes review either: the guard still judges every retained change against the
post-regeneration scope before the turn begins. A
repository the plan never
registered is outside the guard's domain: another `asl run` process working in
an unrelated checkout beneath the same root cannot pause this run, and no
exclusion policy is needed to keep it quiet. Git errors inside a registered
repository remain fail-closed.

Each registered repository is captured together with its linked worktrees. The
runner enumerates `git worktree list` for the registered root and fingerprints
every other checkout sharing its object store — HEAD, tracked content, and
index — under a `<registered-path>::worktree::<absolute-path>` key, so a
mutation reached through a second checkout is as visible as one made in the
root. A linked worktree answers to its owning repository's alias: a write alias
authorizes its changes, a read-only alias makes them a `read-only-repository`
violation, and its `changed` entry names the owner under `worktree_of`.
Creating or removing a linked worktree of a writable repository is authorized
work rather than a topology breach. A worktree that is itself a registered root
keeps its own key instead of being captured twice. If `git worktree list`
fails, the repository gains a `linked-worktrees-unlistable` entry and the guard
fails closed: not knowing whether a repository has other checkouts is not
evidence that it has none. A concurrent `asl run` in a linked worktree of a
registered repository is therefore inside the guard's domain and must work
under a write alias. Repo-content `excluded` paths supplied by
`--review-guard-exclude` apply to every registered repository. The guard
shares the `--review-guard-include-ignored` setting with the review mutation
guard. By default it cannot observe untracked paths hidden by Git ignore rules,
including ignored cache/build output; tracked changes remain observable. Pass
`--review-guard-include-ignored` to include ignored untracked paths in both
guards. Generated registered reviews use the same registry-scoped guard with
all declared aliases readable and an empty write set, so any mutation of a
registered repository is retained, qualified, and rejected before the turn
outcome can take precedence.

Legacy implementation turns use the same before/after evidence lifecycle even
without an auto-commit mode. The bound root must resolve to one Git repository;
otherwise `workspace-write-guard-unavailable` pauses before the harness starts.
The guard fingerprints that whole repository, including every durable ref, and
records exact HEAD tips plus added, removed, or changed refs. A moved HEAD is
classified by ancestry as a creation, fast-forward, rewind, deletion, or
rewrite with bounded added/removed commit counts. Thus a worker-created commit
or history rewrite is visible in `workspace_write_guard` when `commit_mode` is
null instead of disappearing between ASL's own commit-attempt records. If the
post-turn repository or ancestry evidence cannot be established, the runner
retains content, restores the handoff to pending, and fails closed before any
verification or commit step.

Registered repository paths may not traverse symlinks, even when the target is
another directory beneath the workspace; resolution rejects such a binding
before preparation can make it runnable. Preflight captures every declared
repository directly and fails closed with its alias when that evidence is
unavailable. It does not look at undeclared siblings at all: an uninspectable
checkout the plan never registered produces no finding, blocking or otherwise,
and cannot stop a valid registered plan from remaining loop-ready.

`--review-harness codex|claude|copilot` appends a final pure-critique session
after the implementation sessions finish. The flag must differ from the startup
`--harness`; otherwise the run exits with `reason: input-error` before any
harness invocation. Implementation sessions run first. If `--commit-at-end` is
set, the final commit runs after the implementation sessions complete and before
the review session is created. The runner then appends or resumes a generated
`session-<id>.md` review file in the plan directory, switches readiness back to
`running` with a fresh SHA, and runs that session with the review harness
forced (runtime harness switches via `1`/`2`/`3` do not override the review
turn). The review session instructs the harness to write findings to
`PLAN_DIR/final-review.md`, make no repo code changes, and mark only itself
complete; `final-review.md` is overwritten by the latest successful review.
The final non-empty line must be exactly `Status: All clear, no findings` or
`Status: N Findings (H high, M med, L low)` with severity counts adding up to
`N`; missing or malformed status lines fail review verification. Generated
review sessions carry a versioned marker (`asl:generated-review-session
version=4`) and additionally require the structured artifact
`PLAN_DIR/final-review.json`: a JSON object with `version: 2`, an `outcome`
(`all-clear` or `findings`), and a `findings` list where every finding carries
a unique `id`, `severity` (`high`/`med`/`low`), `title`, `evidence` (objects
with a `path` and optional `symbol`), `explanation`, `repair_group`,
`done_criteria`, and `verification_checks` (completion-contract check
objects). The artifact must agree with the `final-review.md` status line on
outcome and per-severity totals; a malformed, contradictory, or incomplete
artifact fails review verification. Artifact enforcement is pinned to a fixed
minimum marker version (`review.STRUCTURED_REVIEW_MIN_VERSION`, currently 2)
rather than to the generated version, so raising the generated version never
exempts review sessions already pending on disk at an older version.
Pre-existing v1 review sessions (base
marker only) remain Markdown-only compatible — no `final-review.json` is
required for them. The grammar, artifact schema, and shared verdict payload
live in `agent_session_loop.review`; the runner and the `asl` skill's
autopilot helper both read verdicts through it. The two generated review gates
(`final-review-output-evidence-based` and `final-review-artifact-consistent`)
report every rejection as one JSON line on stderr carrying the `check`, a
stable `reason` such as `status-line-not-final` or `finding-missing-keys`, and
the values that failed the predicate, so a failed gate names the defect instead
of only its exit status. Other review execution
failures (nonzero exit, timeout, verification failure on missing or empty
`final-review.md`) or repo mutations outside the plan directory also fail
the run.

Those two gates read the review's own artifacts, so on their own they say
nothing about the workspace the run produced — a repair session verifies only
the findings it was created for, which would leave an all-clear review after
the last repair resting on no broad check at all. The generated review
contract therefore also replays every gate the plan's own sessions explicitly
mark with `review_replay: true`. Unmarked checks are session-local, including
legacy checks authored before replay metadata existed. This fail-safe opt-in
keeps contradictory historical assertions about HEAD, dirty trees, and
intermediate files out of later reviews.
Each check is copied from its source session's completion contract, renamed
`broad:<session>:<name>` so two sessions cannot collide on a bare name,
deduplicated across sessions by repository, kind, argv, cwd, path, and
pattern, and stripped of its `baseline_expectation` and `review_replay`
authoring metadata. Diagnostic
checks — those naming an `on_failure_of` gate — are left out because they never
decide whether verification passes. Prior generated review sessions contribute
nothing, so the validators are not replayed onto themselves. The replayed gates
run after the two validators, since rejecting a malformed review is cheap and
replaying the plan's whole recipe is not. The two artifact validators and every
non-`broad:*` gate must pass. A valid structured `findings` verdict may defer
failures only when every failure is from a `broad:*` replay: the turn keeps
`verification.passed: false`, records the raw failures under
`deferred_failures`, and adds an acceptance reason of
`findings-defer-broad-failures` before entering the ordinary repair/rereview
cycle. An `all-clear` verdict can never defer a broad failure. Neither can a
malformed artifact, a failed artifact validator, or a mixture containing any
other failed gate; those paths reopen the review and retain the normal bounded
retry/pause behavior. The supervisor considers a deferral accounted for only
after repairs are materialized and a later review reaches all-clear.

Registered plans generate review session version 5 and structured artifact
version 3 while legacy plans generate the v4/v2 form above.
The repository-aware session reads every registry alias, writes none, and
includes an ordered combined repository base containing each alias, path,
origin, HEAD, and pre-existing qualified dirty paths. Every v3 finding carries
a primary `repo`, every evidence path uses `Alias:path`, and every verification
check carries `repo`; aliases are validated against the plan registry. Readers
continue to accept historical artifact v2 and generated review marker versions.

Legacy review turns use the single-repository mutation guard described below.
Registered review turns use the generic vector guard across every repository
the plan registered; any content or topology change pauses with
repository-qualified evidence and restores the generated review handoff
to pending while retaining the mutation. The post-turn
comparison runs ahead of every other result branch, so a review turn that also
timed out, exited nonzero, was interrupted, emitted `RUNNER_PAUSE`, or left the
plan unreadable still pauses on the guard rather than on its own outcome — those
pauses are retryable or reply-driven, and a relaunch would adopt the mutated
worktree as its new baseline. The suppressed outcome stays visible on the turn
record (`timed_out`, `returncode`, `interrupted`, `runner_pause`). Before the
first commit `HEAD` does not resolve, so the guard diffs against the
repository's empty tree and staged content is still fingerprinted. Every probe
is anchored at the repository root and reports repo-root-relative paths, so a
nested `--cwd` still fingerprints the whole repository rather than that one
subtree. Both diffs are taken with text conversion and external diff drivers
disabled, so the evidence is the file's raw content rather than whatever a
`.gitattributes` rule or git config renders it as: a driver whose output is
identical for two different contents makes git report no diff at all for a path
whose bytes moved, and a review turn can write both the attributes file and the
config. Gitignored files are left out of the untracked evidence by default: the
repository's ignore rules are the operator's existing declaration of what is not
the code under review, and the paths they name churn on their own during a
review turn — byte-compiled output, test and lint caches, and the harness's own
stowed settings, which commit `bf421f9` gitignored for exactly that reason.
`--review-guard-include-ignored` below restores the strict scan for a repository
whose ignored tree is expected to stay still. Deferring to those rules would
otherwise let a turn write the rule that blinds the guard, so every untracked
`.gitignore` git can apply is fingerprinted under either mode, including one
hidden by its own rules — git reads a new ignore file before answering the scan,
so a turn could otherwise create `generated/.gitignore` holding `*` beside the
file it wrote and leave both invisible. The ordinary ignored content beside such
a file stays out of the default evidence, and so does a `.gitignore` inside an
ignored directory, whose rules git never applies because it never descends
there, and a `.gitignore` that is a symlink, which git refuses to follow when it
loads ignore rules — retargeting one changes no rule git applied. That last
omission is from the ignore-source evidence only: the link is still an ordinary
untracked node, so the default scan above fingerprints an unignored one from
its link text, and `--review-guard-include-ignored` fingerprints an ignored one
there the same way — retargeting either still moves the fingerprint and pauses
the review. An ignored symlinked `.gitignore` under the default mode is the one
retarget the guard does not report. The exclusion sources that live
outside the worktree are fingerprinted under either mode — `.git/info/exclude`,
the effective `core.excludesFile` setting, and the file that setting selects —
because none of the scans above can see them: without them a review turn could
hide a mutation by ignoring the path it wrote, either by editing an exclusion
file directly or by repointing `core.excludesFile` at rules it writes itself.
Git follows a symlinked exclusion source, so a linked one is fingerprinted
twice: the link node itself, which makes retargeting or replacing the link
visible, and the fully resolved file git reads through it, named under a
`target:` prefix, without which rewriting only the target's rules would move
nothing. A symlink cycle, an unreadable target, or a target that is neither a
regular file nor a symlink makes the guard unusable rather than blind; a
dangling link records its target as absent, so creating that file later still
moves the fingerprint.
Untracked
paths are fingerprinted as repository nodes and symlinks are never followed: a
regular file contributes its bytes and a symlink contributes its node kind and
link text, so a retargeted or dangling link is visible and a link swapped for a
file holding the target's bytes cannot compare equal. Tracked entries marked
`assume-unchanged` or `skip-worktree` are fingerprinted the same way, from their
raw worktree content: git honours those flags by never comparing the entry
against the working tree, so no diff — including the guard's — can see a
mutation to one. A flagged entry with no worktree file is recorded as absent
(the ordinary sparse-checkout state) rather than failing the guard closed, so
deleting or restoring one still moves the fingerprint. Any other
failing git probe — or any untracked path git reported that cannot be
`lstat`ed, read, or resolved as a link — makes the guard unusable rather than
empty: the run pauses
with `reason: review-guard-unavailable` (before the turn starts when the
pre-turn capture fails, otherwise after it, reverting the review session to
pending).
`--review-guard-exclude PATH` (repeatable, merged with the
`os.pathsep`-separated `ASL_REVIEW_GUARD_EXCLUDE` environment variable) adds
repo-relative files or directories the guard ignores — use it for a *tracked*
path a harness rewrites on its own, or an untracked one git does not already
ignore. In a registered workspace each value applies independently to every
registered repository; a checkout the plan never registered needs no exclusion
because no guard captures it. Values must be
repo-relative and free of `..` segments; anything else is an `input-error`
before the first turn. Each value is matched literally against the repository
root — `*`, `?`, and `[]` are ordinary filename characters, not wildcards — and
drops exactly that path and its subtree from the tracked diff, the untracked
evidence, the worktree ignore sources, and the diff-suppressed index entries
alike — excluding a subtree therefore also stops a `.gitignore` inside it from
being evidence, which is the same declaration that the subtree is not the code
under review. It is how a path git
does not ignore leaves the evidence; for ignored untracked paths the default
already defers to git's own rules. It does not apply to the exclusion sources
outside the worktree — `.git/info/exclude`, the `core.excludesFile` setting, and
the file it selects — which these repo-relative values cannot describe. The
resolved allowlist is persisted in
`run_config`, so `--resume` keeps it, and it is reported as `excluded` on every
guard record.
`--review-guard-include-ignored` puts gitignored untracked files back into the
guard's evidence, restoring the strict scan for a repository whose ignored tree
is expected to stay still. It is off by default because the ignored tree here
churns on its own every review turn — the full suite rewrites `__pycache__`
trees and the test and lint tooling caches beside them, and the Claude harness
rewrites its own
stowed `settings.json` — so a strict default discards a 5–17 minute review turn
on evidence that names no code. The flag takes no value and is persisted in
`run_config`, so `--resume` keeps it, and it is reported as `include_ignored` on
every guard record, so a `review-mutated-worktree` pause says which mode
produced its evidence. It governs the guard's evidence only: the delta rereview
scope — which code a rereview must reread — defers to git's ignore rules under
either mode, because a genuine code repair never lands in a gitignored path.
Review turns are
never auto-committed even under `--commit-per-session`. Review is skipped (with
the reason recorded in `review_skipped_reason`) on `--max-sessions` reached,
stop-after-current (`reason: stopped`), verification failure, harness failure,
commit failure, or any pause during the implementation phase. If the plan is already fully
complete at run start, `--review-harness` still creates and runs the review
session. If a pending generated review session already exists in the plan and
the run is started without `--review-harness`, the run pauses with
`reason: review-harness-required`. After a successful review, text runs print
`Review: PLAN_DIR/final-review.md` followed by the `Status:` line as the final
summary line.

The supervisor audit that grades ASL's own control plane is configured by
`--supervisor-audit`/`--no-supervisor-audit`, `--audit-harness`,
`--audit-harness-model`, `--audit-effort`, and `--audit-timeout`. Audits are
enabled by default and `--audit-timeout` defaults to `600` seconds, applied
independently to each agent attempt rather than as one shared budget. `--audit-harness` must differ from `--harness`, since a harness
cannot grade its own turns; the run exits with `reason: input-error` when it
does not. It may equal `--review-harness`, which was already independent of the
implementation. Without an explicit `--audit-harness`, the audit role is
inherited from the effective review harness together with its model;
`--audit-harness-model` overrides that inherited model as well as the audit
harness's own default, so it deliberately does not require `--audit-harness`.
`--audit-effort` never inherits from `--effort`. When no independent role can be
resolved — no explicit audit harness and no review harness — the configuration
stays valid and enabled and the run's assurance ends `unavailable` with reason
`no-independent-role`; ASL never silently audits itself. All six settings are
persisted in `run_config`, restored by `--resume` unless passed explicitly, and
re-emitted in the reconstructed resume command. A run persists an initial
assurance state of `pending` only once it is eligible (it has a run ID and a run
directory); `--no-supervisor-audit` persists `skipped` with reason `opted-out`
immediately, and `--dry-run` persists `skipped` with reason `dry-run` while
still reporting the role, timeout, and attempt budget a real run would have used
under `planned.audit`. Text runs print a `Supervisor audit: STATE` summary line.
The persisted and audited role includes `source: explicit` or `source: review`
so operators can distinguish those two provenance paths. Those validated enum
values survive evidence redaction; arbitrary fields named `source` remain
content-redacted.

The bundle carries the run's slice of `mailbox.jsonl` beside its slice of the
event log, under `mailbox` with `records` and a `summary` reporting
`available`, `unparsable_line_count`, `unscoped_count`, and a count per kind.
That count is what schema version 2 renamed: under its old name,
`rejected_count`, it sat beside `counts.turn_rejected` in the events summary and
the auditing agent read the two as one number, filing "zero rejections next to a
`turn_rejected` record" as a contradiction 46 times over runs that had all
behaved correctly. Bundles written under version 1 keep the old spelling, so the
reader falls back to it rather than re-judging a damaged log as an undamaged
one. Only a
question, a notice, a resolution and a supervision end name a run outright; a
reply, a receipt and the wait records join their exchange through `to`,
`wait_id` or `closure_id`, so ownership is seeded from the named records and
walked forward, and a record that joins nothing is another run's or the plan's
and is counted rather than reported as withheld. Message prose never reaches
the bundle: `text` is application content, replaced by its digest and byte
length like any other, so the audit can see that an operator ruled and when,
not what was said. Without this an authorized mid-run narrowing — an operator
answering a question by striking two checks a session could never pass — reads
as a gate the run evaded, because the bundle held the contract the session was
materialized with and the recipe it actually ran and nothing between them.
`state/mailbox.jsonl` is registered in the evidence index so a finding can cite
it. Under the size cap the exchange gives up its lifecycle records
(`wait-start`, `wait-end`, `receipt`, `ack`) before ordinary events; the
question, the ruling on it, and the notice are never shed.

Opting out is the only way to run no checks at all. A run that stays enabled but
resolves no independent role, spends either attempt budget, draws a harness that
cannot enforce the read-only profile, or observes workspace activity during
the audit still publishes whatever the deterministic checks proved, under an
`unavailable` verdict for the semantic half that went missing.

The snapshot comparison is a workspace-consistency check, not writer attribution.
Every audit attempt fingerprints the live registered repositories and their linked
worktrees before and after the turn, even when the run recorded commits. A
changed, added, or removed repository makes the audit `unavailable` with outcome
`workspace-changed` and reason `workspace-changed-during-audit`; the auditor's
answer is discarded and the attempt is not automatically retried. Missing or
unprobeable registered evidence also fails closed. Changes confined to
`refs/remotes/` remain excluded because they update a cache without changing
workspace files, local branches, or commits. Native read-only harness profiles
remain unchanged.

Snapshots prove activity, never who caused it: an escaped auditor write and a
concurrent operator edit look the same. ASL records the comparison in
`supervisor-audit-workspace-change.json`, explicitly marked `unattributed`, and
in the attempt's `outcome.json`. Artifact recording is best effort and cannot
make an invalidated audit usable. The ordinary medium-severity availability
issue records this degradation; snapshot activity never appends or reopens the
critical `audit-read-only-mutation` identity. Critical auditor-breach findings
require independent writer-specific evidence, which this comparison does not
collect. Historical mutation outcomes, artifacts, and ledger records remain
readable and are not rewritten; replaying a snapshot artifact alone does not
establish a critical guard failure.

Private pin-based grading is removed. The auditor cannot reach a pinned tree,
so checking it misses writes escaping into the live workspace. Auditing no
longer creates temporary Git worktrees or their registrations; historical
`supervisor-audit-cleanup.json` reports remain evidence of earlier cleanup.

Registry identity in the audit is anchored to the same resolved objects the
runner used. A registry row's `path` remains the workspace-relative declaration
for compatibility and display, but containment checks use only its canonical
`resolved_root`. A missing root, a root outside the workspace, or a root the
collector had to redact is non-contained evidence and fails closed; ASL never
interprets the relative `path` against the auditor process's working directory.

Two budgets are spent, not one, because a bad answer and an undelivered request
are different failures. At most two *answers* may be unusable: each retry is a
fresh invocation told only which validation rule the previous one broke. A
launch that never reached the agent — a timeout, a transport failure, or a
provider at capacity — is charged instead to a separate budget of four
*deliveries* and retried with the identical prompt after 15, 60, and 120
seconds, since there is no answer to correct and the outage is not the auditor's
doing. A delivery retry consumes no answer attempt and never advances a
candidate offer. A failed launch archives its classification and the redacted
tails of the harness's own streams in `harness-stdout.txt` and
`harness-stderr.txt` beside that attempt's `outcome.json`, and a launch that
failed because the model was at capacity records the outage in the model
availability ledger under the marker `model-capacity`. The audit never falls
back to a different model: an audit by a model nobody chose is not the audit
that was configured.

A verdict says how much of the rubric was judged. `all-clear` requires all eight
lanes to have passed on their own evidence and `findings` carries at least one
defect; an audit that ran, found nothing wrong, and could not judge every lane
is `partial` with reason `lane-evidence-absent`. It is a completed audit: it
opens no issue, is filed in the ledger as a completed record, and counts as an
audited run, because calling it `all-clear` would overstate it and calling it
`unavailable` would throw away the lanes it did judge. A deterministic check
that answered `unavailable` is a statement about the check rather than about the
run, so it contributes no signal to its lane: where every check on a lane could
not read its evidence, the lane takes the auditor's judgement, and where any
check did answer, the worse of the two verdicts wins. That is what lets a legacy
plan with no registry binding still earn a clean audit.

An out-of-scope write is graded on whether the guard held it, because a run that
paused on a scope violation is a working guard rather than a leak. Two checks
share one reading of the same evidence and one lane.
`packet.write-scope-containment` is critical and owns only the writes that got
past the guard; `packet.write-scope-guard-catch` is medium and owns the ones it
stopped, recommending that the child's durable write scope be widened to the
aliases its own findings name before expansion and preflight are rerun and the
run resumed. A write counts as caught only when the guard recorded the
violation, reverted the session to pending, paused the run on that guard record,
and no commit that could have carried the write followed; anything weaker is an
escape, including a `workspace-write-scope-repair` whose own guard evidence still
lists violations. A guard that could not inventory a repository is neither, and
stays on the critical check, because blindness is not a catch.

Each of those three conditions is read for what it can actually prove. The pause
counts under `workspace-write-guard-unavailable` as well as
`workspace-write-scope-violation` whenever the paused guard record names the
violations, because a guard that both found violations and failed to inventory
something stops the run under the worse of the two reasons and the run still
paused on that record. A commit only defeats a catch when it could have carried
the write: a commit writes one repository's history, so one recorded in a
repository the violation does not name cannot be what carried it, while one whose
record does not say where it landed still counts. A commit group names no
repository of its own — its aliases live in the entries beneath it — so it is
read through those entries, and a group with none committed nothing. And because
the pause is the one condition that lives only in the event log, a bundle whose
event records the size cap dropped cannot report its absence as proof: where the
pause is the only thing standing between the guard and a catch, containment is
`unavailable` rather than failed.

Severity lives on the check rather than on the verdict, so one
invariant key can never report critical on one occurrence and medium on the
next and split its own issue history in the inbox, and since both checks answer
`pass` when their case is absent, the lane rollup keeping the worse of them is
what stops either from masking the other.
