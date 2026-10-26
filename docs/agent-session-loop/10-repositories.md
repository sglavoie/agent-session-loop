<!-- Continuation of the previous part. -->

`gc` reclaims whole `runs/<run-id>/` directories once they age past a cutoff,
which is what finally reaches the per-turn `stdout.log`/`stderr.log` files that
dominate the corpus: `compact-state` rewrites JSON records and never touches
those logs, which is why compaction projects a rounding error against gigabytes
of growth. Unlike the Workbench cleanup and archive views, which retire a whole
plan a human selected, `gc` judges individual runs against an age cutoff and
leaves their plan in place. With `PLAN_DIR` it sweeps one plan; with no
positional argument it judges every plan state directory under the resolved
state root and reports reclaimable bytes per plan directory as well as in
aggregate. A run is reclaimable only when its finish time can be read from its
own `turn-*/result.json` artifacts and is at least `--max-age-days` (default 30)
old, its plan directory carries no lock, it is not reported running by the same
scan `asl sessions` uses, and it is not the newest run of its plan. Every other
run is retained with a stated reason — `plan-locked`, `live-run`, `pin-unknown`,
`open-issue`, `unfinished`, `undated`, `newest-run`, or `within-cutoff`,
evaluated in that order — so a sweep that reclaims little still explains what
held the rest. A stale lock is
refused exactly like an active one: clearing it stays a deliberate `asl unlock`,
never a side effect of reclaiming disk. State directories whose plan directory
cannot be recovered are counted as unresolved and are not judged at all. The
default is a dry-run that deletes nothing (`--dry-run` says so explicitly and is
mutually exclusive with `--apply`); `--apply` removes the projected directories,
reports the bytes actually reclaimed, and exits nonzero if any removal failed.
`asl gc --dry-run` is a pure projection: it deletes nothing and writes nothing,
locks included. It is the one reader that inspects a lock without the read-side
reclamation described under [Stale unfinished `state.json` with no
lock](20-troubleshooting.md#stale-unfinished-statejson-with-no-lock), so a dry run leaves every lock
file in place and one plan is judged identically whether it was reached through
`PLAN_DIR` or through the corpus sweep — the whole-corpus liveness pre-pass can
no longer decide a retain question by removing the lock that answers it.
`--apply` deletes `runs/<run-id>/` directories and nothing else, so lock hygiene
is never an applied action either.
`--json` emits the full per-plan projection, including every run's size and
retain reason.
A corpus-wide sweep also measures the Workbench archive under
`<state_root>/archive/`, which `gc` never judges or deletes: the report (and the
`archive` JSON object) states its entry count and bytes and how many entries are
past the Workbench retention window, and points at the Workbench Archive view,
the only place archive entries are purged.

`maintenance` is the scheduled counterpart to `gc`: the same reclaim, performed
daily by a job ASL installs and owns, plus one additional lane `gc` does not
have. `asl maintenance install` writes
`~/Library/LaunchAgents/<prefix>.asl-maintenance.plist` (the prefix is
`ASL_LAUNCHD_LABEL_PREFIX`, default `com.sglavoie`) and bootstraps it
into the calling user's GUI domain; `asl maintenance uninstall` boots it out and
deletes the definition, removing nothing from the state corpus. The plist is
written by the command rather than shipped as a file for a separate tool to
load on purpose: a recipe that bootstraps every plist in a directory would give
the label two owners and could silently re-load a job the operator had
deliberately uninstalled. The rendered job runs
`asl maintenance run --apply --max-runtime-seconds 900 --json` once a day at
03:30 with `RunAtLoad` false, `ProcessType` `Background`, and `LowPriorityIO`,
and appends its own output to `maintenance.log` under the state root. The
executable is resolved to an absolute path at install time because a launchd job
inherits no shell `PATH`. Nothing in the job depends on Agent Mux, and nothing in
it requires a cockpit to be installed.

`asl maintenance run` measures the corpus against two thresholds and does
nothing at all unless one is crossed: more than 8 GiB of state, or less than 15%
free on the volume holding it. Under both thresholds the outcome is
`under-threshold` and the pass costs one stat walk, which is what makes a daily
job affordable. `--force` plans and applies regardless; it relaxes the trigger,
never the policy. When a threshold is crossed, maintenance reclaims in two lanes.
The first is `gc`'s: whole expired `runs/<run-id>/` directories, selected by the
same retention classifier, so `plan-locked`, `live-run`, `pin-unknown`,
`open-issue`, `unfinished`, `undated`, `newest-run`, and `within-cutoff` all
protect a run here exactly as they do there. The second lane touches only runs
the first is forbidden to delete: a run retained as `newest-run` whose finish
time is more than 90 days old has its reproducible per-turn bodies
(`stdout.log`, `stderr.log`, `last-message.txt`) gzipped in place, and once
compressed detail still exceeds 64 MiB for that run, bodies are removed
largest-first until it fits. `state.json`, `events.jsonl`, and every
`result.json` are never compressed and never removed by either lane, so a
compacted run keeps its authoritative state, event, and result records intact.

Every removed or compressed body leaves a durable `<name>.body.json` sidecar
recording the original path, byte count, SHA-256, compressed location, removal
timestamp, and the `log-body-budget` reason. The sidecar is fsynced *before* the
bytes go, so an interrupted pass can leave metadata describing a body that is
still present, but never a removed body with no metadata. Compression is
deterministic (pinned gzip mtime) and the removal order is a pure function of
on-disk sizes, so the same corpus plans the same actions on every host and a
repeated pass is a no-op.

The whole mutating pass runs under a single state-root-wide `flock`
(`maintenance.lock`), so two passes can never interleave; a pass that cannot
claim it reports `lock-unavailable` and mutates nothing. That claim is the only
thing the corpus's other writers do *not* participate in — runners, lock
writers, and assurance publishers never take it — so every fact that authorizes
a deletion is read again at the moment it is used, rather than once per pass.

Pressure is measured twice. The reading that decides whether to request the
claim at all is taken before the wait, and a pass that then blocks behind
another pass is holding exactly the number the pass ahead of it exists to
falsify, so the corpus is measured a second time once the claim is granted. If
it is back under both thresholds by then, the outcome is `pressure-relieved`,
nothing is planned, and nothing is touched; `--force` is the only thing that
overrides that second reading. Both readings are kept in the result and the
manifest: `usage` is the pre-claim measurement that admitted the pass and
`usage_at_claim` is the one taken under the claim.

Liveness, pins, and plan locks are then re-read under the claim to narrow the
plan — but narrowing is all that reading does, and it never authorizes what
survives it. Every destructive action is fenced by its own reading taken
immediately before it acts: each whole-run removal, each body compression (which
unlinks the original once the gzip and its sidecar are durable), and each
budgeted body removal re-reads the plan lock, the live-run set, and the pin set
first.

That reading is taken at the `unlink` itself, not only at the top of the
operation containing it, because both compaction operations read an entire body
before they destroy anything and a body can be gigabytes: compression streams
the gzip and takes the digest on the same pass, and a removal with no sidecar
yet digests what it is about to delete. A pin, plan lock, or live run appearing
during that preparation still stops the deletion, and what it leaves behind is
true in every case. A gzip that was only staged is discarded, so the corpus is
byte-for-byte what it was. A gzip already published keeps its `<name>.body.json`
sidecar saying `removed: false` beside the original that is still there — the
same recoverable shape an interrupted pass leaves, which the next unprotected
pass converges. A removal tombstone already made durable is withdrawn back to
its pre-removal record, keeping the digest that preparation paid for, so no
sidecar ever claims a removal that did not happen; a withdrawal that cannot
itself be written is reported as a `partial-failure` rather than left standing.

Anything that came alive, acquired a pin, or acquired a lock — whether
since planning or since the last action in the same pass — is dropped and listed
in `revalidation_drops`, where each entry names its `lane` (`reclaim` or
`compaction`) and the `stage` at which it was caught (`revalidation` or
`mutation`). A run fenced out midway through compaction keeps every body it
still has and the pass moves on to the next run. A failed
mutation is reported with the bytes it left allocated, and those bytes are never
counted as reclaimed — a partial `rmtree` re-measures the directory and counts
only the difference. `--max-runtime-seconds` (default 900) bounds the pass; work
stops cleanly at the deadline and the remainder is reported as a non-strict
`deadline-exceeded` finding for the next scheduled run.

When protected evidence alone still exceeds the budget, maintenance deletes
nothing outside policy and raises the strict `protected-over-budget` health
finding instead, exiting nonzero: that is a human decision (close an open
assurance issue, `asl unlock` a stale plan lock, or raise the budget), not one
an unattended sweeper should make. A pass that suffered a partial failure
reports `partial-failure` and suppresses the over-budget verdict, because a
corpus that failed to shrink has not proven that policy cannot shrink it.

Each pass fsyncs a `maintenance.json` manifest under the state root holding the
full last result plus a bounded 10-entry history. `asl maintenance status` reads
it back and reports job presence, whether the installed definition still matches
what this version would render, whether launchd has it loaded, and the last run's
outcome, reclaimed bytes, and failures; it exits nonzero only when that last
recorded pass itself failed. The install verifier (`just verify-asl-install`,
or `python3 -m agent_session_loop.install_verify`) carries the same information as
an advisory `maintenance-job` check, so an install with no job at all still
verifies clean.

`migrate-state` repairs the state orphaning that a plan-directory move causes:
because operational state is keyed by the SHA-256 of the plan's canonical path,
renaming or moving a plan directory strands its state under the old path hash.
`asl status` (and the start of `asl run`) surface these strays as
`orphaned_state_candidates` — matched by comparing each stranded state's
recorded `initial_snapshot.content_sha256` against the current plan content and
confirming the recorded `plan_dir` no longer exists — each carrying the exact
`asl migrate-state PLAN_DIR --from OLD_PLAN_PATH` command to run. The scan is
skipped whenever the plan already has its own `state.json` (the common case).
`migrate-state` renames the state directory to the new path hash and rewrites
the absolute `plan_dir`/`state_dir`/`state_path`/`events_path`/`lock_path`
fields inside `state.json`, then appends a `state_migrated` event. It also
repoints the plan-owned conversation ledger, which lives beside `state.json`
rather than inside it: every readable record naming the old plan directory is
rewritten to the new one through a single atomic durable replace, line order is
preserved, and unreadable lines are copied back byte-for-byte rather than
repaired. The report and the `state_migrated` event carry
`conversation_records`, `conversation_records_rewritten`, and a
`conversation_note`. Historical per-turn `result.json` paths are left untouched
and reported as an `unmigrated_artifacts_note`. It refuses (in both dry-run and
apply, leaving both state directories untouched) when the source plan directory
still exists on disk (`source-plan-exists` — its state is not orphaned, so
migrating would steal a live plan's state), the source holds an active lock, the
destination state directory already exists, or the source conversation ledger
records some other plan directory (`conversation-plan-mismatch` — relocating it
would attach another plan's resumable conversations to the target). Unreadable
ledger *lines* are reported rather than refused, since one truncated append must
not strand the whole state directory; a ledger file that cannot be read at all
is `conversation-ledger-error`. The default is a dry-run; pass `--apply` to perform the
rename and rewrite, and `--json` for the machine payload. A relocation also
brings the record it is already rewriting forward to the current state schema
version in the same write; a record that cannot carry a projection, or one whose
`version` this build does not recognize, still relocates and reports the
shortfall rather than blocking the repair the operator asked for.

Omitting `--from` selects the other mode: upgrade *this* plan's own
`state.json` in place, from an absent `version` (v1) or from v2, to the current
schema version, adding the `agent_mux` projection. Nothing else in the record
changes — paused state, terminal state, turns, and artifact references are
preserved — and the upgrade appends a `state_migrated` event naming the versions
it moved between and the projection generation it allocated. It is idempotent:
a record already at the current version is a reported no-op, not a rewrite.
It refuses (`incompatible-active-run`) when a live lock is held over a record
this build would have to upgrade, because that process holds the payload in
memory and would overwrite the upgrade on its next transition; a live run
already at the current version is a benign no-op instead. It also refuses an
unrecognized `version` (`unknown-schema-version`) and a record with no run
identity to project (`unprojectable-state`), leaving the file untouched in both
cases.

`baseline` executes every contract check that declares a `baseline_expectation`
of `pass` or `fail` (checks with the default `skip` carry no baseline
obligation) and records the observed outcome as evidence in the derived
`PLAN_DIR/baseline-results.json`. Evidence is keyed by normalized recipe,
canonical cwd, resolved executable, and environment signature; the repository
fingerprint is stored inside the record so preflight can tell *stale* evidence
(fingerprint changed) from *missing* evidence. Per-check runtime is clamped to
120 seconds and the whole pass to 600 seconds by default
(`--per-check-timeout` / `--total-timeout` override, never exceeding the caps).
`baseline-results.json` is excluded from the readiness hash by construction, so
recording evidence never invalidates a loop-ready manifest. Preflight requires
current baseline evidence only while no session is complete; once
implementation has started, baseline findings are suppressed.
For registered plans, the identity also includes the ordered registry digest,
each check runs in its selected repository, and the record stores an aggregate
fingerprint over every registered repository. A change in a repository that a
particular baseline check did not execute in still makes that evidence stale.
Legacy plans retain their historical v1 key and single-repository fingerprint.

`split` runs the deterministic session-split authoring chain as one command:
`capture_inline_plan` (for `--inline-stdin`) or an explicit `--plan-file` →
`parse_plan_document` → one `contract_from_payload` per session from the
`--contracts-file` JSON (keyed by canonical session id) → an atomic
`PlanTransaction` staging `context.md`, `progress.md`, each `session-N.md`, and
a serial-only `manifest.md` → `refresh_readiness_metadata(state="draft")`. It
resolves a fresh collision-safe `output_dir` itself and never overwrites an
existing one. `--skill-prefix auto` detects the invoking harness for generated
skill invocations (erroring when detection is ambiguous); `--analysis-file` is
appended verbatim to the manifest execution note. The `--json` payload reports
`output_dir`, `plan_id`, `source_path`, `session_ids`, `files`, `readiness`,
`next_steps` (per-harness `session-expand` invocations), and `skill_prefix`.
For a multi-repository source, the payload also reports ordered `repositories`
rows and `registry_digest`; those keys remain absent for legacy sources.
Stable failure reasons: `single-session` (splitting is not needed),
`malformed-plan` (the parser's line-specific `PlanError` verbatim),
`missing-contract:<id>`, `invalid-contract:<id>` (a present contract entry that
is not a JSON object or is missing `done_when`/`verify`), and `input-error`. The
`session-split` skill is a thin
wrapper that authors the per-session contracts and then calls `asl split`.

`split --check` lints a source plan's session headings and exits without writing
anything: no output directory, and no captured source file even with
`--inline-stdin`. It needs no `--contracts-file`, which is what lets it run
*before* any contract is authored. `parse_plan_document` is fail-fast and raises
on the first malformed heading; `--check` walks the same fence-aware grammar
through `lint_plan_document` and accumulates instead, so one pass reports every
malformed heading in the plan. Each entry in the `findings` array carries `line`,
`category`, the verbatim `message`, `source_line`, and `suggested_line`. Stable
categories: `invalid-session-id`, `zero-id-component`, `malformed-heading`,
`missing-id-whitespace`, `missing-session-id`, `non-numeric-session-id`,
`session-id-suffix`, `dangling-title-separator`, `malformed-title`,
`punctuation-free-title`, `unsupported-title-separator`, and
`duplicate-session-id`. `suggested_line` is non-null exactly when the damage has
a single correct rewrite (`punctuation-free-title` and
`unsupported-title-separator`), and any suggestion emitted is guaranteed to
parse; every other category would invent an id, renumber sessions, or drop a
title, so it is left for a human decision. Repository declarations add
`repositories-placement`, `duplicate-repositories`,
`malformed-repositories`, `invalid-repository`, `invalid-session-scope`, and
`missing-repository-registry`. The last one needs `--cwd` (default `.`): the
check resolves the plan's absolute and `Alias:path` targets against that root
and reports a source that reaches a second Git repository, or escapes the root
into a path a Git repository still owns, while declaring no `## Repositories`
section. Only paths that exist on disk count, anything inside a code fence is
ignored, and a target belonging to no Git worktree is `external` rather than a
finding — so illustrative paths, plan-store artifacts, and other non-Git files
invent no repositories and need no repair. The `--json` payload then carries a
`repository_footprint` object with `suggested_registry_table` and
`suggested_scopes`, and `asl register-repositories --plan-file PATH --cwd ROOT`
writes that migration into the source markdown rather than leaving it to be
pasted; the write path (`asl split` without `--check`) applies the
same test to the plan text *and* to every `--contracts-file` `path`/`cwd`
value, failing with `reason: malformed-plan`. A clean check exits `0` with
`ok: true`, `checked: true`, `session_ids`, and `session_count` — the canonical
ids a caller should key contracts by. Findings exit `1` with
`reason: malformed-plan` and `error` set to the first finding's message; fewer
than two sessions exits `1` with `reason: single-session`. Because repairing one
heading can expose a finding the fail-fast parser had masked (a duplicate
canonical id is only visible once both headings parse), callers re-check after
each repair round; the `session-split` skill drives that loop for at most three
rounds and splits the repaired text through `--inline-stdin`, leaving the user's
original plan file untouched.

Multi-repository sources may declare exactly one top-level `## Repositories`
section before the first session. Its Markdown table has the exact ordered
columns `Alias`, `Path`, and `Origin`, at least one row, and no blank values.
The parser preserves declaration order, validates aliases, workspace-relative
paths, non-overlap, and credential-free origin identities, and ignores
registry-shaped tables inside code fences or session bodies. Both the
fail-fast parser and `split --check` report malformed tables, duplicate
declarations or rows, and unsupported placement with source line numbers.
Authoring removes only that registry block from shared context and writes the
normalized authoritative table to `manifest.md`; it is not duplicated in
`context.md`. The registry therefore participates in both plan-content and
stable-readiness hashes. Sources without the section keep their historical
manifest, context, and JSON response structure.

A four-repository workspace can use a non-Git common parent such as
`/Users/dev/1_dev_projects`:

```markdown
## Repositories

| Alias | Path | Origin |
| --- | --- | --- |
| SVC | tundra_svc | git@github.com:tundra/SVC.git |
| OAK | tundra_OAK | git@github.com:tundra/OAK.git |
| OWL | tundra_OWL_J | git@github.com:tundra/OWL.J.git |
| Kiln | tundra_Kiln | git@github.com:tundra/Kiln.git |
```

The common parent becomes a valid `--cwd` only because the registry makes the
Git boundaries explicit. ASL resolves each path beneath that parent, requires
it to be an exact non-overlapping Git top-level with the expected normalized
origin, and performs Git evidence, verification, mutation-guard, review, and
commit work in the selected child repositories. A legacy plan with no registry
still requires its `--cwd` itself to be the Git worktree root; merely pointing
an old plan at a directory containing several repositories does not opt it into
workspace behavior. A legacy plan that nonetheless reaches a second repository
is refused rather than reinterpreted: `split --check`, `preflight`, `prepare`,
`expand-ingest`, and `run` all fail closed until the registry exists.

Migrating an existing legacy plan that has outgrown one repository is two
commands, not a hand edit:

```text
asl register-repositories PLAN_DIR --cwd BOUND_ROOT --json
asl prepare PLAN_DIR --resume --rebind-workspace --cwd WORKSPACE_ROOT
```

The first writes the declarations; the second rebinds the plan to the derived
workspace root. `--rebind-workspace` accepts a plan whose `manifest.md` now
carries a registry while `plan-metadata.json` is still the legacy single-root
form; the stale binding is discarded rather than compared against the registry.
Preflight emits the same pair as `auto_repair_plan` steps, so a skill driving
that plan performs the migration without an operator, and `asl run` applies the
first step itself whenever the migration admits the plan. The equivalent hand edit —
pasting `repository_footprint.suggested_registry_table` into `manifest.md`
above the first session heading, adding `**Reads repositories**:` /
`**Writes repositories**:` to each `session-<id>.md` from
`repository_footprint.suggested_scopes`, and adding a `repo` alias and a
repository-relative `path`/`cwd` to every contract check — is what the command
derives, and is worth doing by hand only when the command refuses and names the
fact it could not observe.

`register-repositories` writes all three declarations for a split plan
directory, deriving every value from disk rather than asking a human to paste
one. It
resolves the plan's repository intent against `--cwd`, derives the workspace
root, the ordered registry, one read/write scope per session, and a `repo`
alias plus repository-relative `path`/`cwd` for every contract check, then
writes `manifest.md` and every `session-N.md` through one `PlanTransaction` and
refreshes `**Readiness SHA-256**` while preserving the readiness state.
`plan-metadata.json` is deliberately untouched: replacing a legacy v1 binding
is the rebind's job. Prose, handoffs, and every byte outside those regions stay
verbatim, and re-running is a fixed point that reports `already-registered`.
The command fails closed. When a fact cannot be observed it writes zero bytes,
exits `1`, and names the one thing it could not see as
`missing fact: <fact>`; the vocabulary is `workspace-root`,
`repository-origin`, `contract-path-outside-registry`,
`command-check-repo`, and `completed-handoff-paths`. A registry row with a
guessed origin would fail validation later, so nothing is invented. The last
fact is the part-way plan: a registry makes a completed handoff's bare
`Files modified` entries illegal, so a plan whose finished sessions recorded
unqualified paths is refused rather than migrated into a plan nothing can
parse. Qualify those entries as `Alias:path` and re-run. A completed handoff
of `(none)` records no path and migrates normally. A plan whose lock is held by a live
run exits `1` with `reason: lock-contention` instead of rewriting session files
underneath that run.

Applying the migration is not the whole repair, and the order of what remains
is authoritative rather than advisory. The registry changes the plan's registry
digest, which invalidates the prepared binding and the baseline evidence
captured under it, so the command prints — and reports as `next_commands` — the
rebind first and the re-run second:

```text
asl prepare PLAN_DIR --resume --rebind-workspace --cwd WORKSPACE_ROOT
asl run PLAN_DIR --cwd WORKSPACE_ROOT
```

Both name the derived workspace root, not the `--cwd` the plan was bound to.
The rebind re-runs validation and re-captures baselines, which is why it is a
separate deliberate command instead of a side effect of the migration. The
`--json` payload serializes the whole result: `status`, `missing_fact`,
`detail`, `workspace_root`, ordered `repositories`, `sessions_changed`,
`files`, `readiness`, and `next_commands`.

The same gate fires earlier, in `asl split --check`, when no plan directory
exists yet. `asl register-repositories (--plan-file PATH | --inline-stdin)`
runs the identical derivation over a source plan's markdown and emits the
migrated plan instead of writing files: the registry above the first session
heading, both scope lines under every session heading, and a rewritten
contract in each session that has one. The input is never written. Without
`--output` the migrated markdown is the whole of stdout, so it can be piped
straight into `asl split`; with `--output PATH` it is written there and stdout
carries the human summary, and `--output` naming the input is refused. The
registry rows spell each origin as an absolute Git URL, because that is what a
source plan's `## Repositories` table accepts, while a split plan's manifest
records the transport-neutral identity. The refusal taxonomy is the same four
facts, and a refusal emits no markdown at all rather than a partial plan. The
`--json` payload adds `source_path`, `output_path`, and `markdown`;
`markdown` is the input verbatim when the migration is unnecessary and `null`
when it is refused.

Each session in a multi-repository source must contain these exact fields:

```markdown
**Reads repositories**: SVC, OAK
**Writes repositories**: SVC
```

Reads must contain at least one known alias. Writes may be `(none)`, otherwise
every write alias must also appear in reads. Missing or duplicate fields,
unknown aliases, empty lists, and mixed `(none)` values fail closed during
`split --check` and authoring. The canonical fields are rendered near the top
of each session file and participate in readiness hashing. A completed
multi-repository handoff lists modified files as comma-separated qualified
paths such as `SVC:internal/foo.go`; unqualified, unknown, or out-of-write-scope
entries are rejected. Single-repository plans keep their existing syntax.

`prepare` runs the whole preparation pipeline in one deterministic command:
structure gate (at least two session headings, each with a parseable inline
`## Completion Contract`) → deterministic authoring (the `asl split` Step 8
sequence through one atomic plan transaction) → batched validator workers →
expansion rendering and exact factual corrections → baseline execution →
`--mark-loop-ready` preflight. A source plan that fails the structure gate is
rejected with `reason: needs-plan-iteration` before any output directory is
created. Failures after authoring preserve the plan directory as `draft` with
structured `blockers` and print a shell-quoted `asl prepare --resume PLAN_DIR`
command; success prints the exact `asl run` command. `--resume` reruns
validation only for sessions missing from the stored
`prepare-validation.json` aggregate and reuses recorded baseline evidence
whose repository fingerprint still matches; it never creates another split.

Registered plans use validator-output version 2. The packet names the canonical
workspace root and exact ordered resolved registry, then gives each session only
the absolute repository roots in its declared read scope. This is advisory
research context, not filesystem isolation. Every claim, issue, concrete detail,
reuse pattern, dependency, and correction carries a `repo` alias. Ingestion
rejects output before mutation when its workspace, registry, session IDs, or an
attributed alias differs from the authoritative plan or falls outside the
session's read scope. An unbound registered plan is bound to its first validated
workspace in the same atomic transaction as the validation aggregate,
expansions, reference lines, and factual corrections.

Registered expansion notes render repository-qualified locations. Validators
report path prose that could refer to more than one read repository as a
`repository-ambiguous` issue; `prepare` preserves that evidence but refuses to
mark the plan loop-ready until the source claim is qualified. Preparation runs
baseline checks and final preflight through the resolved workspace context, so
each check executes in its declared repository and cached evidence covers the
whole ordered registry.

Validator-output version 1 remains accepted only for legacy single-repository
plans, and only while the plan actually is one. The legacy packet names its
`codebase_root` as the sole bound root and instructs the validator to report any
path resolving outside it as a `repository-ambiguous` issue. Before a version 1
packet is built, and again before its output is ingested, `prepare` recomputes
the plan's repository footprint against that root; a plan reaching a second
repository is refused with the dedicated reason `repository-footprint` rather
than `validator-schema`, and the error payload carries the same
`repository_footprint` object preflight emits. A version 1 payload that itself
reports `repository-ambiguous` is refused for the same reason.

The plan's repository *intent* is recomputed at the same two points, and is
refused with the reason `repository-registry-required` carrying the bounded
`repository_intent` object. Intent counts locations the plan has still to
create, so it catches the multi-repository plan a footprint scan reads as
single-repository. Because the check sits on the validation-authority path,
which prepare runs before the baseline and preflight stages and re-runs against
the stored aggregate on `--resume`, validation ingest, baseline publication,
readiness marking, and resume preparation are all refused before any plan file
is written.

Explicit workspace replacement is available only for an existing registered
plan:

```text
asl prepare PLAN_DIR --resume --cwd NEW_WORKSPACE --rebind-workspace
```
