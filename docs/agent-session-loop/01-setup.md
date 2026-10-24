## Setup

Run from this repository, or add `bin/.local/bin` to `PATH`:

```bash
export PATH="$PWD/bin/.local/bin:$PATH"
asl status ~/.local/share/agent-plans/example-plan/
```

The runner supports `codex`, `claude`, and `copilot` harnesses. Defaults use the
executable names on `PATH`, and optional overrides live in
`~/.config/agent-session-loop/harnesses.json` or
`$XDG_CONFIG_HOME/agent-session-loop/harnesses.json`.

Session files use canonical session IDs in `session-<id>.md`. IDs may be plain
integers such as `1` or nested dotted IDs such as `2.1`; leading zeroes are
invalid, so a heading like `Session 01` must still produce `session-1.md`.
Status text, tail output, artifact paths, and JSON `id` fields use the same
canonical value. JSON payloads keep integer compatibility aliases for
integer-only plans where older callers already consumed numeric fields.

```json
{"codex": {"executable": "/path/to/codex"}}
```

The config loader also accepts these equivalent shapes:

```json
{"codex": "/path/to/codex"}
```

```json
{"harnesses": {"codex": {"executable": "/path/to/codex"}}}
```

Check local prerequisites before a real run:

```bash
asl doctor ~/.local/share/agent-plans/example-plan/ --harness codex
```

`doctor` verifies the working directory, plan directory, configured harness
executable, next pending session, generated prompt, and argv.
Both `run` and `doctor` default to the current directory. Pass `--cwd ROOT` to
select the repository root for a legacy plan or the common workspace root for
a registered plan.

## Workflow: Split, Expand, Repair, Verify, Run

The canonical loop-ready lifecycle is **split → expand → repair if needed →
run**. In directory mode, `session-expand` validates the split plan and then
runs final preflight with `--mark-loop-ready`; a clean plan is left ready for
`session-run`. `asl run` hard-gates on the `loop-ready` readiness state, but
self-heals derived progress/readiness drift (stale or missing `progress.md`
and recomputable `manifest.md` readiness metadata) before pausing, so a freshly
split-and-expanded plan rarely needs a manual repair step. Each session is
gated on its `## Completion Contract` `### Verify` recipe.

1. Create the split plan with `session-split`. The skill first validates the
   source plan with `asl split --check`, repairs mechanical session-heading
   damage (and asks before any repair that would change what the plan means),
   and only then splits. The skill writes
   `manifest.md` with three readiness lines populated as
   `**Readiness**: draft`, `**Readiness SHA-256**`, and
   `**Readiness checked at**`. Every `session-<id>.md` is emitted with a
   `## Completion Contract` block containing `### Done when` bullets and a
   `### Verify` JSON block. The default contract skeleton is intentionally
   minimal (one `command` check running `python3 -m unittest -q` plus a
   placeholder `Done when` bullet); replace the placeholder with a real
   criterion and refine the check command per session.

2. Run `session-expand` on the plan directory so expansion notes and
   per-session `## Contract Issues` sections are available. Expansion validates
   each session contract against the actual codebase, runs final
   `asl preflight --mark-loop-ready`, and prints `session-run` only when the
   plan is loop-ready.

3. If a pending session is still too large, resplit that session and regenerate
   expansion guidance before running the loop:

```text
$session-resplit ~/.local/share/agent-plans/example-plan/ --session 2 --parts 2
$session-expand ~/.local/share/agent-plans/example-plan/
```

4. Check the plan:

```bash
asl status ~/.local/share/agent-plans/example-plan/
```

5. If expansion reports blocking preflight findings, repair them before running:

   Final preflight reports red and yellow findings with stable machine-readable
   `repair_hints`. Repair findings by hand, or drive the guided Q&A repair
   loop. Re-run `session-expand` after repair so it can refresh expansion notes
   and mark the plan loop-ready:

```text
$plan-preflight-iterate ~/.local/share/agent-plans/example-plan/
/plan-preflight-iterate ~/.local/share/agent-plans/example-plan/
$session-expand ~/.local/share/agent-plans/example-plan/
```

   You can also run the final gate manually. `--mark-loop-ready` repairs
   manifest-readiness-only findings (missing readiness lines, stale SHA, or
   `draft`/`reviewed` state) **and** auto-backfills stale/missing `progress.md`
   (force semantics) when the only non-readiness blockers are
   `progress-journal-missing` findings; both repairs surface in the JSON
   payload's `auto_repairs` array. It still refuses when other non-readiness
   red/yellow findings remain:

```bash
asl preflight ~/.local/share/agent-plans/example-plan/ --cwd $PWD --json --mark-loop-ready
```

6. Preview the first harness turn:

```bash
asl run ~/.local/share/agent-plans/example-plan/ --harness codex --dry-run
```

7. Run the loop:

```bash
asl run ~/.local/share/agent-plans/example-plan/ --harness codex
```

The runner stops when every `session-<id>.md` handoff status is `complete`, when
`--max-sessions` is reached, when an operator requests stop-after-current, or
when a pause condition requires review. The first three are successful exits
(`reason: all-complete`, `max-sessions`, and `stopped` respectively). A plan that
completes every session but whose review cycle ends while findings are still open
exits `reason: review-findings-unresolved` with `ok: false` instead of
`all-complete`, and its `action_required` block carries the terminating
disposition, the review outcome, and the unresolved finding counts. Resume a
paused or stopped run by re-running the same command after fixing the blocker.

`asl run` refuses any plan whose `**Readiness**` is not `loop-ready`, but it
first attempts to self-heal recoverable drift before pausing. The runner's
readiness gate auto-repairs stale or missing `progress.md` (force semantics)
and recomputable `manifest.md` readiness metadata (state, SHA, checked-at), then
reruns preflight; if every blocker was derived, the run proceeds without
operator intervention and the JSON payload's `readiness_gate_repair` field
records what was rewritten. Non-derived blockers (contracts, paths, cwd,
locks, harness availability, commit preflight, ambiguity sentinels, malformed
plans, verification failures) still pause as before.

Override with `--allow-not-loop-ready` for emergency execution; preflight
findings are still reported in the run payload, and the readiness-gate
auto-repair is skipped because the gate itself is bypassed. The override also
suppresses the per-session verification gate's `no-contract` pause, but it
does not suppress failures from contracts that are present — a malformed or
failing contract still pauses the run.

ASL is sequential by design. `session-run` always selects the next pending
session from the manifest and runs one session per harness turn; there is no
explicit `--session` selector and no parallel scheduling. `session-run`
validates the current manifest readiness state and SHA before implementation.

Older split plans may still have a `parallel-validation.md` file, a
`## Parallel Execution` block in `manifest.md`, or `## Parallel Metadata`
blocks in each `session-<id>.md`. These legacy artifacts are ignored: the
runner does not read them, the readiness hash excludes them, and new plans
emitted by `session-split` and `session-resplit` do not contain them.

## Completion Contracts

Each `session-<id>.md` carries a `## Completion Contract` block with two
required subsections:

```markdown
## Completion Contract

### Done when

- Replace this with a verifiable success criterion.

### Verify

```json
{
  "session": "<id>",
  "checks": [
    {
      "name": "unit-suite",
      "type": "command",
      "argv": ["python3", "-m", "unittest", "-q"],
      "timeout_seconds": 180
    }
  ]
}
```
```

Exactly one such block may appear per session file. `parse_completion_contract`
refuses a file carrying two or more `## Completion Contract` headings (headings
inside code fences are quoted text and do not count) rather than silently
selecting one, and `asl split` drops a session's inline source contract when a
`--contracts-file` entry supersedes it, so a generated file never holds both.

The fenced block is JSON-only because the package is stdlib-only. The parser
lives in `agent_session_loop.contracts` and is authoritative for accepted
shapes. Required and optional fields:

- `session` (optional): when present, must match the canonical session ID of
  the host file.
- `checks` (required): non-empty array of check objects.
- `parallel` (optional, defaults to `false`): when `true` and the recipe has at
  least two `command` checks in the same execution phase, `file_exists` and
  `regex_in_file` checks in that phase run first sequentially in declared
  order and its command checks then run concurrently (at most four at a time).
  Authoritative gates form the first phase; eligible failure diagnostics form
  the second. Results, `result.json`, and failure
  details stay in declared order, so a parallel run is byte-identical to the
  sequential one; per-check timeouts, output capture, and artifacts are
  unchanged. Commands sharing mutable state (the same output directory, port,
  database, or lock) must not opt in — non-interference is the contract
  author's responsibility. `asl baseline` always runs checks sequentially.
  Interrupting a parallel recipe (Ctrl-C, or any error surfacing from a
  concurrent check) kills every command process still running, cancels the
  checks still queued, and waits for the workers before it re-raises, so an
  abandoned check publishes no result, no failure artifact, and no progress
  event after the interrupt. Every command check runs in a session of its own
  (`start_new_session=True`), and both cancellation and the per-check timeout
  signal the whole process group, so a check that fans out into helper
  processes leaves no descendant *in that group* behind to write output or
  repository side effects — nor to hold the captured pipes open and stall the
  cancellation. Every command check is then swept (see **Process sweep** under
  Safety Modes), which also reaches a descendant that left the group with
  `setsid` on the timeout, cancellation, and interrupt paths — including a
  Ctrl-C under a sequential recipe — because each snapshots the check's
  descendants before the group kill. What stays bounded either way is
  the cost: the pipe readers of a killed check are joined under a five-second
  limit and those of a check that exited on its own under thirty seconds, so an
  escapee holding those pipes open delays the recipe by at most that long
  rather than indefinitely, and an abandoned check still publishes no result,
  artifact, or event.
- Each check object has `name` (string), `type` (one of `command`,
  `file_exists`, `regex_in_file`), an optional repository alias in `repo`, and
  type-specific fields. In a registered plan, every check must resolve to a
  known alias: registries with multiple entries require `repo`, while a
  one-entry registry supplies its sole alias when omitted. Plans without a
  registry may omit `repo` and retain the legacy model.
- Every check accepts optional `on_failure_of`, a non-empty array of check
  names. Without it, the check is an authoritative gate and contributes to the
  contract's pass/fail decision. With it, the check is a diagnostic: it runs
  only when at least one named gate fails and never changes the aggregate
  decision. Targets must be unconditional gates in the same recipe; unknown
  names, self-references, and diagnostic chains are rejected. A recipe must
  contain at least one gate.
- Every check also accepts optional boolean `review_replay` (default `false`).
  Only unconditional gates marked `true` are copied into later generated
  reviews. Use it for durable current-state assertions that should remain true
  after every repair. Leave HEAD identity, dirty-tree state, intermediate-file
  presence, and other point-in-time checks unmarked. A diagnostic carrying
  `on_failure_of` cannot set `review_replay: true`.
- `command`: `argv` (non-empty array of strings, no shell), optional
  `timeout_seconds` (defaults to 60, max 3600), optional `cwd`.
- `file_exists`: `path` (string).
- `regex_in_file`: `path` (string), `pattern` (regex string).
  In a registered plan, every `path` and `cwd` is relative to its selected
  repository and cannot be absolute, use Windows path syntax, or contain an
  upward-traversing `..` segment. Without a registry, file and regex paths and
  command working directories retain the legacy absolute-path behavior;
  preflight still rejects unsafe relative file paths as before.
  For `verify`, `contract-check`, and `baseline`, `--cwd` denotes the workspace
  root when the plan has a registry and the legacy repository root otherwise.
  Commands and Python-module probes use the selected repository (plus the
  check's repository-relative `cwd`), while file and regex checks resolve under
  that same alias. JSON check rows and observer events include the alias,
  qualified cwd/path, absolute resolved root, and resolved cwd/path.
- A check accepts only the keys documented for its type. Any other key is
  rejected before the check is parsed, because a key no executor reads is
  dropped on the way to execution and would let a check claim semantics it
  does not have. `file_exists` asserts the path EXISTS and `regex_in_file`
  asserts the pattern is PRESENT; neither has absence semantics. Assert that
  something is gone with a `command` check that exits non-zero while it is
  still there.
- Every check also accepts an optional `baseline_expectation` (one of `skip`,
  `pass`, `fail`; defaults to `skip`). `pass`/`fail` declare how the check must
  behave on the *unmodified* repository; `asl baseline` records the evidence
  and preflight blocks a not-yet-started plan whose declared baselines lack
  current evidence. Baseline recording evaluates each declared obligation
  independently; `on_failure_of` controls runtime verification scheduling,
  not whether an explicit baseline expectation is sampled.

Keep one authoritative gate per logical verification surface. In particular,
do not make a focused test command and a broader command that already contains
it unconditional peers. If the focused command adds useful failure evidence,
make it conditional on the aggregate gate. This rule is language-neutral:
ASL compares and executes opaque `argv` arrays rather than interpreting a test
runner's language or selection syntax. Preflight emits yellow
`verification-duplicate` findings for exact duplicate `argv`/`cwd` pairs.

`session-expand` validates each contract against the codebase and records any
problems in a per-session `## Contract Issues` section in
`session-<id>.expand.md`. `session-run` refuses to advance when issues are
flagged; `asl run` enforces the same gate per turn.

After writing a `- **Status**: complete` handoff, the runner and the
`session-run` skill both invoke:

```bash
asl verify ~/.local/share/agent-plans/example-plan/ --session 2 --json
```

`asl verify` runs all gates first, then diagnostics whose named gate failed
(with concurrent command checks within each phase under `"parallel": true`,
reported in declared order). Exit 0 means every gate passed; non-zero exit is a
hard pause. The JSON body carries a stable `reason` field
(`ok`, `verify-failed`, `no-contract`, `malformed-contract`, `cwd-error`, or
`invalid-session`), executed `checks`, `skipped_checks`, and separate
`diagnostic_failures`, plus per-check rows with `failure_reason` enums
(`command-failed`, `command-timeout`, `command-not-found`, `command-error`,
`file-missing`, `file-error`, `regex-not-matched`). A failed command check's
`detail` appends the last non-empty diagnostic line the check wrote — stderr
preferred over stdout — so a validator that names the predicate it rejected
travels into the pause record instead of leaving only a rendered argv. On
verification failure,
the skill reverts the session handoff back to `pending` and the runner pauses
with `reason: verify-failed`.

A failed command check also carries `failure_diagnostic`, and it is `null` for
every check ASL did not generate itself. Promoting a command's last stderr line
into ASL's own schema is a claim about provenance, and only `generated_gate` —
stamped in process by the runner on the recipe it rendered, unsettable from a
session file — can establish it. A `ci` command, or any other command authored
in a `## Completion Contract`, is therefore never a generated gate, so its
`failure_diagnostic` is `null` no matter how much output it retained. That is
the content boundary working, not a lost diagnostic: without it, arbitrary
application output would travel as control-plane text. The rejection itself is
not lost — the check's `stderr_artifact_path` retains the full text, and the
supervisor bundle indexes that artifact for every failed check, so an auditor
reads the diagnostic from the evidence index instead.

For a registered plan, pass its workspace root to `--cwd`; ASL resolves and
validates the declared registry locally before dispatching each check to its
alias. A legacy plan continues to interpret `--cwd` as one repository root.

`asl contract-check` statically diagnoses the same `Verify` recipes without
executing any command — the single-file mode plan-wide `asl preflight` cannot
express (`--session ID` narrows to one session; omit it to sweep the whole
plan). It wraps `contracts.read_completion_contract`, the fail-open
`diagnose_command`/`diagnose_command_scope` runnability probes, and file/regex
resolution against `--cwd`. Every check becomes a row whose `status` is `ok`,
`schema-error` (the contract failed to parse), `missing-contract` (no
`## Completion Contract`), or `factual-issue`. A `factual-issue` row carries a
`reason` enum: `command-not-runnable` (executable off PATH, module not
importable, or an overly broad operator-health command), `file-missing`,
`regex-not-matched`, or `bare-python` (an unversioned `python` interpreter,
frequently unavailable where `python3` exists — use `python3`; versioned
`python3 -c`/`python3 -m`/script invocations are fine). Exit 0 only when every
row is `ok`; otherwise exit 1 with
`reason: contract-issues` and the full `checks` list plus an `issue_count`.
Static check rows include `repo` (`null` for legacy checks), and verification
observer start, finish, and skip events carry the same alias. This schema
is also used by repository-aware execution: ASL resolves the alias through the
validated workspace registry before diagnosing or running the check.

### The AgentMux Compatibility Contract

`asl contract-check PLAN_DIR --agent-mux [--state-dir DIR] [--json]` adds a
second, plan-independent block reporting the compatibility contract ASL owns for
the AgentMux integration: per-component schema version and digest, the
contract-set ID, the corpus and state roots, the supported protocol
capabilities, and the fixture verdict. Exit stays 0 only when the contract set
is consistent as well as every contract row; an inconsistent corpus with clean
rows exits 1 with `reason: contract-set-issues`, and a corpus that cannot be
read at all exits 1 with `reason: contract-set-error`.

ASL is the sole run authority, so ASL — not AgentMux — owns the schemas the two
components exchange. `tests/fixtures/agent_mux_contract/` holds canonical
schemas for **projection v1** (the `agent_mux` block in `state.json`),
**lifecycle hook v2** (the payload piped to `amx hook-indicator`), and **bridge
protocol v2** (the NDJSON frames `asl bridge serve` writes), each beside
positive fixtures the schema must accept and negative fixtures it must reject
for a named reason at a named JSON pointer. Publishing the contract was not
adopting it, and the three components adopted it separately: the projection and
the lifecycle hook emitted from the corpus first, and the bridge followed by
cutting to protocol 2 and retiring its own parallel fixture set into this one.
All three are now pinned by the single contract-set ID, which is why the bridge
hello carries that ID rather than a version per component — listing them
separately is what would let them drift.

Four properties are structural rather than conventional. Every object declares
`additionalProperties: false`, every string carries an enum, const, format, or
`maxLength`, and every array a `maxItems`, so a producer cannot widen the wire
without being noticed. Absence is spelled `null` rather than omission, so a
consumer never distinguishes "missing" from "not applicable". Timestamps have
one UTC spelling — RFC 3339 with an explicit `+00:00`, rejecting `Z`, naive
values, and other offsets — so the same instant compares byte for byte across
all three components, and paths are absolute, traversal-free POSIX. Question and
reply prose is refused by name at every depth before any schema-guided walk
runs, so a leak is diagnosed as `body-not-allowed` where it actually is rather
than as an unknown field; the one body-bearing frame declares its exemption as
the JSON pointer `/result/body`, and the linter reconciles declarations against
exemptions in both directions, so a stale exemption is as much a schema defect
as an undeclared one. Bounded ASL-authored diagnostics — the 160-character
`question_preview` AgentMux already renders, `pause_message`, and the bridge
`message` and `detail` — remain permitted: the rule is not "no text" but "no
undeclared text, and every declared text field carries a ceiling".

Identity is derived, never declared. The contract-set ID is a SHA-256 over the
canonical bytes of every corpus `.json` file in sorted path order, truncated to
64 bits and prefixed `asl-amx-1.`; editing any schema or fixture byte moves it
and nothing else can. `manifest.json` maps that ID to component versions,
digests, roots, document ceilings, fixture counts, and the capability lists a
consumer feature-detects against, and is excluded from the digest it carries.
Every file is stored in one canonical rendering — sorted keys, two-space indent,
no ASCII escaping, trailing newline — so `python3 -m
agent_session_loop.agent_mux_contract --write` is a no-op or a diff, never a
whitespace churn. Fixture documents carry the placeholder
`asl-amx-1.0000000000000000`, because a fixture cannot carry an ID computed from
its own bytes; the authoritative value lives in the manifest.

## Migration From Older Split Plans

Older split plans that pre-date this lifecycle lack the three `**Readiness**`
lines in `manifest.md` and the `## Completion Contract` block in every
`session-<id>.md`. `asl run` will refuse such plans because they cannot be
marked `loop-ready`.

To migrate an existing plan in place:

1. Run `asl preflight ~/.local/share/agent-plans/old-plan/ --json` to enumerate
   missing artifacts. Findings carry `repair_hints` that name the exact
   `fix_category` (`plan-edit`, `manifest-update`, `session-recipe-edit`, etc.)
   needed for each gap.
2. Either drive the guided repair skill —
   `$plan-preflight-iterate ~/.local/share/agent-plans/old-plan/` — which asks
   targeted questions and applies the edits, or repair by hand:
   - Add `**Readiness**: draft`, `**Readiness SHA-256**`, and
     `**Readiness checked at**` lines to `manifest.md`.
   - Add a `## Completion Contract` block (above the final `## Handoff`) to
     every `session-<id>.md`, with at least one `### Done when` bullet and a
     `### Verify` JSON block holding a non-empty `checks` array.
3. Run `session-expand` to refresh per-session expansion notes and final
   preflight readiness against the new contract shape. When final preflight
   succeeds, the plan is marked `loop-ready`.
4. Resume the loop with `asl run`.

For one-off emergency execution of an unmigrated plan, pass
`--allow-not-loop-ready` to `asl run`. Sessions without a `## Completion
Contract` will still skip verification under the override; sessions that do
carry a contract are still gated by it.

`session-resplit` is plan maintenance, not a runner subcommand. It archives one
pending parent `session-<id>.md`, writes nested pending children such as
`session-2.1.md` and `session-2.2.md`, archives stale expansion artifacts, and
tells you to rerun `session-expand`. A legacy `parallel-validation.md` is left
on disk untouched. `session-resplit` keeps the explicit `--session <ID>`
selector because it is plan maintenance; `session-run` does not. After
expansion is regenerated, `asl status` and `asl run --dry-run` should show the
first child as the next pending session.

For a registered multi-repository plan, every generated child inherits the
parent's read and write scope exactly. Resplitting never broadens authority;
an explicit later plan edit may narrow a child's scope before fresh readiness
validation.

Legacy plans require their
resolved Git worktree to be clean before the first harness turn; registered
per-session plans require the next session's write repositories clean (its
read-only repositories need only be inspectable, since their dirt can never
enter its commit), and registered end commits require every declared
repository clean. Use `--commit-per-session` to
commit after each verified completed session, or `--commit-at-end` to commit
after all pending sessions complete in the same run. Registered per-session
groups contain only changed write aliases proven by the turn guard; registered
end groups contain every changed declared repository. Repositories are
committed serially in manifest order with ASL's own `asl commit`, invoked as
`python -m agent_session_loop commit --yes --no-verify` inside this checkout and
scoped to the paths the turn guard recorded. Every such invocation carries an
explicit `--timeout`, and the run bounds the subprocess itself as a backstop, so
a stalled message harness soft-fails instead of holding the session loop; the
run's own `--timeout` is not what supplies that bound, since it is routinely
unset. That backstop kills the subprocess's process group and then sweeps the
descendants it snapshotted beforehand, because the nested command's own harness
runs in a session of its own and would otherwise survive the group signal (see
[Process sweep](14-safety-modes.md#process-sweep)); the expiry reports exit 124 either way. A commit that fails is recorded, retried, and reported instead of pausing
the run. A
stop-after-current request before all sessions complete exits successfully
(`reason: stopped`) without running the final `--commit-at-end` commit. A stop
honored once every session is complete still runs that commit first, then stops
before any `--review-harness` work.
