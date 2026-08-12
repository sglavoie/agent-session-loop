# Agent Session Loop

`agent-session-loop` repeatedly invokes an agent harness against a split
session plan directory. The default short executable alias is `asl`. Each
harness turn runs one `$session-run` or `/session-run` invocation, then the
runner re-reads the plan files before deciding whether to continue.

```bash
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex --max-sessions 1
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex --commit-per-session
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex --commit-at-end
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex --note "operator reference"
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex --review-harness claude
bin/.local/bin/asl run ~/.local/share/agent-plans/example-plan/ --harness codex --dry-run
bin/.local/bin/asl status ~/.local/share/agent-plans/example-plan/
bin/.local/bin/asl tail ~/.local/share/agent-plans/example-plan/
bin/.local/bin/asl sessions
bin/.local/bin/asl sessions --running
bin/.local/bin/asl sessions --recent --limit 5
bin/.local/bin/asl sessions --json
```

The package is stdlib-only and lives in `agent_session_loop/`. The executable
wrapper at `bin/.local/bin/agent-session-loop` adds this repository to
`sys.path` and dispatches to the package CLI. `bin/.local/bin/asl` is a
relative symlink to that wrapper. The long executable remains available.

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
asl doctor --harness codex --plan-dir ~/.local/share/agent-plans/example-plan/
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
(`reason: all-complete`, `max-sessions`, and `stopped` respectively). Resume a
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
`file-missing`, `file-error`, `regex-not-matched`). On verification failure,
the skill reverts the session handoff back to `pending` and the runner pauses
with `reason: verify-failed`.

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

Optional auto-commit modes require `spec` on `PATH`. Legacy plans require their
resolved Git worktree to be clean before the first harness turn; registered
plans require every declared repository clean. Use `--commit-per-session` to
commit after each verified completed session, or `--commit-at-end` to commit
after all pending sessions complete in the same run. Registered per-session
groups contain only changed write aliases proven by the turn guard; registered
end groups contain every changed declared repository. Repositories are
committed serially in manifest order with `spec commit --yes --no-verify`. A
stop-after-current request before all sessions complete exits successfully
(`reason: stopped`) without running the final `--commit-at-end` commit. A stop
honored once every session is complete still runs that commit first, then stops
before any `--review-harness` work.

## Commands

```text
asl run PLAN_DIR [--cwd ROOT] [--harness codex|claude|copilot] [--harness-model MODEL] [--review-harness codex|claude|copilot] [--review-harness-model MODEL] [--effort EFFORT] [--timeout SEC] [--audit-harness codex|claude|copilot] [--audit-harness-model MODEL] [--audit-effort EFFORT] [--audit-timeout SEC] [--supervisor-audit|--no-supervisor-audit] [--max-sessions N] [--max-pending-sessions N] [--max-review-fix-cycles N] [--review-guard-exclude PATH] [--review-guard-include-ignored] [--commit-per-session|--commit-at-end] [--state-dir DIR] [--unsafe-bypass|--no-unsafe-bypass] [--no-mailbox] [--allow-not-loop-ready] [--reply TEXT|--reply-stdin] [--json] [--dry-run]
asl preflight PLAN_DIR [--cwd ROOT] [--harness codex|claude|copilot] [--commit-per-session|--commit-at-end] [--state-dir DIR] [--max-pending-sessions N] [--mark-loop-ready] [--json]
asl verify PLAN_DIR --session ID [--cwd ROOT] [--artifact-dir DIR] [--json]
asl contract-check PLAN_DIR [--session ID] [--cwd ROOT] [--json]
asl baseline PLAN_DIR [--cwd ROOT] [--per-check-timeout SECONDS] [--total-timeout SECONDS] [--json]
asl split (--plan-file PATH | --inline-stdin) --contracts-file PATH [--cwd ROOT] [--base-dir DIR] [--skill-prefix auto|codex|claude|copilot] [--analysis-file PATH] [--json]
asl prepare PLAN.md [--cwd ROOT] [--workspace-inventory-exclude PATH ...|--clear-workspace-inventory-excludes] [--harness codex|claude|copilot] [--harness-model MODEL] [--validator-harness H] [--review-harness H] [--review-harness-model MODEL] [--base-dir DIR] [--state-dir DIR] [--unsafe-bypass|--no-unsafe-bypass] [--json]
asl prepare --resume PLAN_DIR [--workspace-inventory-exclude PATH ...|--clear-workspace-inventory-excludes] [--rebind-workspace ...]
asl expand-ingest PLAN_DIR --batch-file PATH [--json]
asl register-repositories (PLAN_DIR | --plan-file PATH | --inline-stdin) [--cwd ROOT] [--output PATH] [--state-dir DIR] [--json]
asl status PLAN_DIR_OR_ID [--state-dir DIR] [--json]
asl questions PLAN_DIR [--state-dir DIR] [--json]
asl review-verdict PLAN_DIR [--state-dir DIR] [--json]
asl tail PLAN_DIR [--state-dir DIR] [--lines N] [--no-follow]
asl sessions [--running] [--recent] [--limit N] [--state-dir DIR] [--verbose] [--json]
asl doctor --harness codex|claude|copilot [--harness-model MODEL] [--effort EFFORT] [--cwd ROOT] --plan-dir PLAN_DIR [--unsafe-bypass|--no-unsafe-bypass] [--json]
asl inbox guidance PLAN_DIR_OR_ID (--text TEXT | --text-stdin) [--state-dir DIR] [--json]
asl inbox reply PLAN_DIR_OR_ID --to QUESTION_ID (--text TEXT | --text-stdin) [--wait-consumed] [--wait-timeout SECONDS] [--state-dir DIR] [--json]
asl inbox resolve PLAN_DIR_OR_ID --to QUESTION_ID --action continue|pause [--note TEXT|--note-stdin] [--wait-consumed] [--wait-timeout SECONDS] [--state-dir DIR] [--json]
asl inbox read PLAN_DIR_OR_ID [--peek] [--state-dir DIR] [--json]
asl ask PLAN_DIR_OR_ID (TEXT | --question-id QUESTION_ID) [--wait] [--wait-timeout SECONDS] [--state-dir DIR] [--json]
asl notify PLAN_DIR_OR_ID (TEXT | --text-stdin) [--state-dir DIR] [--json]
asl outbox wait PLAN_DIR_OR_ID [--include questions|notices|all] [--supervision-id UUID] [--since SEQ] [--timeout SECONDS] [--state-dir DIR] [--json]
asl assurance list [--limit ROWS|--all] [--state open|resolved|dismissed|accepted-risk] [--severity low|medium|high|critical] [--state-dir DIR] [--json]
asl assurance show ISSUE [--state-dir DIR] [--json]
asl assurance resolve ISSUE [--note TEXT] [--state-dir DIR] [--json]
asl assurance dismiss ISSUE [--note TEXT] [--state-dir DIR] [--json]
asl assurance accept-risk ISSUE [--note TEXT] [--state-dir DIR] [--json]
asl assurance merge ISSUE TARGET [--note TEXT] [--yes] [--state-dir DIR] [--json]
asl assurance closeout RUN_ID --outcome accepted|reworked|escaped [--rework-minutes N] [--issue ISSUE ...] [--note TEXT] [--plan PLAN_DIR_OR_ID] [--replace] [--state-dir DIR] [--json]
asl assurance process-pending [--limit RUNS] [--run-id RUN_ID] [--plan PLAN_DIR_OR_ID] [--audit-harness NAME] [--audit-harness-model MODEL] [--audit-effort LEVEL] [--audit-timeout SECONDS] [--state-dir DIR] [--json]
asl assurance plan ISSUE --harness NAME --output PATH [--harness-model MODEL] [--effort LEVEL] [--timeout SECONDS] [--state-dir DIR] [--json]
asl control stop-after-current PLAN_DIR_OR_ID [--state-dir DIR] [--json]
asl control select-harness PLAN_DIR_OR_ID --harness codex|claude|copilot [--state-dir DIR] [--json]
asl control set-model PLAN_DIR_OR_ID [--harness codex|claude|copilot] [--harness-model MODEL|--clear-harness-model] [--effort EFFORT|--clear-effort] [--state-dir DIR] [--json]
asl unlock PLAN_DIR [--state-dir DIR] [--force] [--json]
asl compact-state PLAN_DIR [--state-dir DIR] [--apply] [--json]
asl gc [PLAN_DIR] [--state-dir DIR] [--max-age-days DAYS] [--dry-run|--apply] [--json]
asl migrate-state PLAN_DIR --from OLD_PLAN_PATH [--state-dir DIR] [--apply] [--json]
asl metrics [--since-days N] [--state-dir DIR] [--json]
```

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

Both files are created and repaired to mode `0600`. They live under
`<state-root>/<plan-path-sha256>/`; they are not stored in the plan directory
or repository.

A `supervision-end` record is the mailbox's terminal signal for one supervision
lifetime: the Autopilot helper process that was supervising this plan has
exited. It ends the helper's lifetime and does not prove the supervised run is
quiescent. Most terminal reasons do leave the `asl run` child finished, but
`worker_hung` deliberately stops the helper without killing or waiting on that
child, which stays alive holding the plan lock and can publish questions and
notices after the terminal record. A reader that needs to know whether work is
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

### Mailbox commands

The seven mailbox commands share normal plan-reference, `--state-dir`, and
`--json` behavior:

- `asl inbox guidance` appends supervisor guidance from exactly one of `--text`
  or `--text-stdin` and renews the supervisor lease first.
- `asl inbox reply` atomically appends the first reply to `--to QUESTION_ID`.
  Concurrent later replies fail with `question-already-answered`; a missing
  target fails with `question-not-found`. `--wait-consumed` optionally waits
  for a worker/runner receipt, with `--wait-timeout` defaulting to 90 seconds;
  a receipt timeout leaves the successful append `ok: true`,
  `delivery_state: accepted`, and `consumed: false`.
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
  guidance remains pending for later runs.
- `asl ask` appends a worker question, or resumes an existing one with
  `--question-id`. `--wait` polls for the first reply or resolution for
  90 seconds by default;
  `--wait-timeout` accepts 0 through 1800 seconds and requires `--wait`.
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
  `--text-stdin` are supplied, and `stdin-read-error` when standard input
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
- Plan resolution shared by all seven commands reports `ambiguous-plan`,
  `malformed-plan`, `state-error`, or `os-error`.

Supervisor write and wait commands first read the current lease and decide
between a heartbeat and a takeover. A fresh, current-version lease carrying a
`run_id` proves a live supervisor is projecting this plan, so the command
re-emits the same `supervisor_id` and question projection with only
`updated_at` refreshed. A missing, stale, v1, unprojected, or unreadable
lease means no supervisor is alive, so the command takes over with a new
random supervisor identity and no projection — a hand-answered run still
survives past its base deadline. The worker commands do not claim supervisor
ownership.

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

The current net surface is two mailbox state files, seven mailbox commands, one
model-control command, and one `mailbox` event name with
`guidance-observed`, `notice-published`, `wait-preparation-failed`,
`wait-started`, and `wait-cleared` action variants.
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
recovered or skipped. A failure retains earlier successful commits and pauses
with repository-qualified commit artifacts. Legacy single-repository commits
use the same coordinator, which also makes a failed per-session commit
retryable on resume.

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

Autonomous `asl run` turns prompt the harness with a Python-built session task
packet (`runner.build_task_packet`): a harness-neutral prompt that references
the session file, `context.md`, `progress.md`, expansion notes, and the
predecessor handoff by absolute path and embeds no readiness, lock, or
session-selection policy — the runner owns all of that. Packet turns do not
invoke the manual `session-run` skill; that skill remains the manual
one-session-per-conversation path. Setting `ASL_WORKER_MODE=skill` in the
runner's environment is a temporary rollback that restores the legacy
skill-invocation prompt for autonomous turns.

For a registered plan, both packet and rollback prompts include the ordered
credential-free registry, the selected session's exact read and write aliases,
and the `Alias:path/to/file` handoff/reference rule. They reveal
absolute resolved roots only for aliases in that session's read scope. The
harness invocation cwd is the bound workspace root. Before every registered
implementation turn (including reply and resolution resumes), the runner
recursively inventories all registered and unregistered Git worktrees beneath
that root and captures fail-closed content evidence. It repeats the capture
before handling any harness result. Changes pass only for repositories named
by the session's write aliases; an empty write set makes the turn read-only.
Changes in read-only, out-of-read-scope, or unregistered repositories and all
added or removed repositories pause as `workspace-write-scope-violation`.
Unavailable inventory or fingerprint evidence pauses as
`workspace-write-guard-unavailable`. Both post-turn failures retain repository
contents, restore the handoff to pending, record qualified evidence, and run
before verification or commit. Scope violations require a durable scope repair
and fresh expansion/preflight before continuing. The workspace write guard
prunes only subtrees listed in the plan's persisted
`workspace_inventory_excludes` policy before traversal. This policy is for
uninspectable non-repository workspace content; it cannot name, contain, or be
contained by a registered repository. Other inventory and Git errors remain
fail-closed. Guard evidence reports these paths as `inventory_excluded`,
separately from repo-content `excluded` paths supplied by
`--review-guard-exclude`. The guard shares the
`--review-guard-include-ignored` setting with the review mutation
guard. By default it cannot observe untracked paths hidden by Git ignore rules,
including ignored cache/build output; tracked changes remain observable. Pass
`--review-guard-include-ignored` to include ignored untracked paths in both
guards. Generated registered reviews use the same workspace-wide guard with
all declared aliases readable and an empty write set, so any registered or
unregistered repository mutation is retained, qualified, and rejected before
the turn outcome can take precedence.

Registered repository paths may not traverse symlinks, even when the target is
another directory beneath the workspace; resolution rejects such a binding
before preparation can make it runnable. Preflight captures every declared
repository directly and fails closed with its alias when that evidence is
unavailable. Its recursive inventory of undeclared siblings is supplementary:
an uninspectable sibling or an exceeded inventory bound is reported as a
non-blocking diagnostic and does not prevent a valid registered plan from
remaining loop-ready.

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
version=3`) and additionally require the structured artifact
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
autopilot helper both read verdicts through it. Other review execution
failures (nonzero exit, timeout, verification failure on missing or empty
`final-review.md`) or repo mutations outside the plan directory also fail
the run.

Registered plans generate review session version 4 and structured artifact
version 3 while legacy plans continue to generate the exact v3/v2 form above.
The repository-aware session reads every registry alias, writes none, and
includes an ordered combined repository base containing each alias, path,
origin, HEAD, and pre-existing qualified dirty paths. Every v3 finding carries
a primary `repo`, every evidence path uses `Alias:path`, and every verification
check carries `repo`; aliases are validated against the plan registry. Readers
continue to accept historical artifact v2 and generated review marker versions.

Legacy review turns use the single-repository mutation guard described below.
Registered review turns use the generic workspace vector guard across every
registered and unregistered worktree; any content or topology change pauses
with repository-qualified evidence and restores the generated review handoff
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
discovered repository, including unregistered worktrees; this is the escape
hatch for an unsupported nested untracked repository or other node reported by
an undeclared checkout. Values must be
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
independently to each of the at most two agent attempts rather than as one
shared budget. `--audit-harness` must differ from `--harness`, since a harness
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

Opting out is the only way to run no checks at all. A run that stays enabled but
resolves no independent role, loses both agent attempts, draws a harness that
cannot enforce the read-only profile, or catches its auditor mutating the
workspace still publishes whatever the deterministic checks proved, under an
`unavailable` verdict for the semantic half that went missing.

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

Interrupting an audit turn cancels the audit rather than retrying it. The first
Ctrl+C ends the attempt, the verdict is `unavailable` with reason `interrupted`,
terminalization still completes, and the run keeps the exit code it had already
earned.

Ordering inside one run is fixed. Every ending reaches the terminalization
coordinator exactly once, the audit runs there first, and its artifacts and
ledger records are on disk before `run_finished` is appended — so the terminal
event and the state projection carry the same verdict rather than two. The
outcome and the exit code are decided before a run reaches the coordinator and
auditing changes neither: a finding, a failed audit, or a failed publication
never turns a successful run into a failed one, never rewrites a pause reason,
and never touches the artifacts the run recorded.

A terminated run also carries an additive `assurance_report` block beside its
`assurance` object, in `state.json`, in the `run_finished` event, in
`asl run --json`, and in `asl status --json`. It repeats the audit state and
eligibility and adds the copyable issue IDs, the new and recurring counts, the
absolute path of `supervisor-audit.json` in the run directory, the corpus-wide
open issue count, and `list_command` (`asl assurance list`). Findings carried by
an `unavailable` verdict are flagged `partial: true`, because those are the
problems the audit managed to see rather than all of them. Text output renders
the same three lines:

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
compatibility alias — passing both flags is an input error). A source review
counts once when it produced repair children, including legacy
`not-loop-ready` histories and crash-recovered reopen records; ordinary cycle
and reopen evidence never double-count it. The minimum cap needed for another
materialization is therefore `historical_materializations + 1`, not the prior
run's cap plus one.

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

For registered rereviews, the cycle records an ordered `repositories` base
vector instead of collapsing the workspace to one `repo_head`. Delta selection
requires a resolvable ancestor base and complete dirty-path evidence for every
declared repository. The rendered combined scope provides explicit `git -C`
commands per repository and alias-qualified changed paths; failure in any
repository widens the whole rereview to full scope. Reopen records preserve the
same ordered vector, qualified findings, and repair-group scopes. If a crash
loses the cycle event after children were authored, continuation reconstructs
the vector from the generated review session and each group scope from the
generated children, so staged repairs, restored dirty paths, and eventless
rereviews do not collapse repository namespaces.

A pending review session's scope is a static list, but the repository keeps
moving — a crash and resume, or an in-run reuse, can leave a delta scope that
predates files the reviewer must now inspect, and untracked additions never
show up in `git diff <base_sha>` at all. So immediately before a pending
generated review session executes, the runner re-derives its scope from current
Git evidence and rewrites the `## Delta Review Scope` section in place,
widening to full scope whenever a narrower one can no longer be proven. Only
that section changes: the session keeps its id, its generated-review version
marker, its completion contract, its pending handoff, and the prior findings
recorded by the earlier review. A session with no scope section is already
full-scope and is left untouched, so first reviews cost no extra Git probes. A
scope section the runner cannot parse fails closed with a `malformed-plan`
pause rather than executing against stale bounds.

An all-complete plan is not necessarily inert. A later run with a higher
absolute cap automatically reopens `cycles-disabled` or
`cycle-cap-exhausted` findings. A prior `malformed-review-repair` becomes
eligible after `final-review.json` is externally corrected into a safe,
internally consistent v2 artifact; changed findings or grouping are accepted
and audited through new fingerprints and Markdown/JSON digests. ASL never
rewrites that artifact, and manually changing its outcome to `all-clear` does
not complete the plan—the generated rereview session is the only all-clear
authority. Existing generated repair children are authoritative after a crash:
the runner executes pending children or appends a missing rereview, but never
recreates them.

The loop stops on an all-clear rereview, repeated finding fingerprints,
unstructured findings, operator stop requests, or an unchanged insufficient
cap. Repeated and unstructured findings are terminal circuit breakers. Every
review completion appends a `review_cycle` event and a `review_cycles[]` state
entry recording the cycle number, review session id, outcome,
introduced/closed/repeated fingerprints, repair session ids, disposition, and
full artifact digests (digest fields are nullable for legacy records).
Registered records additionally retain the qualified structured findings,
ordered repository bases, and finding-derived repair-group read/write scopes.
A reopen attempt appends `review_repair_reopened`; successful records preserve
source lineage, previous/current fingerprints and digests, exact child IDs,
repository evidence, group attribution, and before/after cap accounting. The
run payload's `review_fix_disposition` keeps
the compatible disposition vocabulary (`all-clear`, `repairs-materialized`,
`cycles-disabled`, `cycle-cap-exhausted`, `findings-repeated`,
`unstructured-findings`, `malformed-review-repair`, or `not-loop-ready`).
Cycle and continuation state are reconstructed from append-only events plus
generated sessions, so caps, idempotency, and rereview scheduling survive
pauses, crashes, and later processes.

A review turn that produces artifacts failing verdict validation is not a
human's problem on the first try. The runner reverts the review session to
pending with the exact validation error as a handoff note, appends a
`review_verdict_retry` event (run id, session, attempt, max attempts,
fingerprint, failure reason, error), and re-runs the same review session in the
same run — the retry packet carries the prior error as repair references, so
the reviewer sees precisely what to fix. The budget is two retries, counted
only from records appended after the latest `review_cycle` event, so every
reopened cycle starts fresh. An error that repeats (same reason and error text,
same fingerprint) is treated as exhausted immediately rather than burning the
remaining attempt. Exhaustion keeps the existing `verify-failed` pause — no new
pause reason — and adds `review_verdict_fingerprint`,
`review_verdict_repair_attempt`, `review_verdict_repair_max_attempts`, and
`review_verdict_repair_exhausted: true` to the pause details; the autopilot
treats that key like `verification_repair_exhausted` and stops relaunching. A
pending stop-after-current request wins over a retry: the run pauses instead,
recording `review_verdict_repair_declined: "stop-after-current"` with
`review_verdict_repair_exhausted: false` so the record does not claim a budget
was spent. The mutation guard runs on every retry turn against a fresh
pre-turn fingerprint, and scope refresh rewrites only the
`## Delta Review Scope` section, so the rejection notes survive.

`preflight` inspects the plan without invoking a harness and reports every
finding needed to clear the loop-ready gate: missing readiness lines, contract
schema problems, manifest/session drift, environment prerequisites, and
harness/commit-mode prerequisites when `--cwd`, `--harness`, or a commit-mode
flag is supplied. The turn-budget check defaults to allowing 24 pending
sessions; 25 or more pending sessions emits yellow `env-turn-budget`. Pass
`--max-pending-sessions N` to `asl preflight` or `asl run` when a larger serial
plan is intentional and should still pass the loop-ready gate. JSON output
carries a stable shape with `findings`, `findings_by_severity`, `repair_hints`,
`readiness`, `expected_readiness_sha256`, `max_pending_sessions`,
`mark_loop_ready_requested`, and `marked_loop_ready` fields. JSON also always
includes `auto_repairs`; it is an empty list unless `--mark-loop-ready`
performed a derived repair.
The stable finding categories are `plan-structure`, `canonical-session-id`,
`manifest-readiness`, `missing-contract`, `malformed-contract`,
`ambiguity-sentinel`, `path-reference`, `verification-scope`,
`verification-duplicate`, `command-not-runnable`,
`progress-journal-missing`, `baseline-missing`, `baseline-stale`,
`baseline-unexpected`, `baseline-timeout`, `baseline-near-timeout`,
`workspace-root`, `repository-registry`, `repository-root`,
`repository-origin`, `repository-overlap`, `repository-scope`,
`repository-contract`, `repository-footprint`,
`repository-registry-required`, `env-repo-root`, `env-git`,
`env-harness`, `env-spec`, `env-lock`, `env-amx`, and `env-turn-budget`.
Consumers should branch on these
category values and the paired repair hint rather than parsing message prose.

`repository-footprint` is red-only and applies to legacy plans: it fires when a
contract or handoff target that some Git repository owns resolves outside the
bound root, or when the plan's targets resolve into more than one Git
repository. An absolute path that stays inside the bound root is accepted, so
plans predating repo-relative authoring keep passing. A target that escapes the
bound root and belongs to *no* Git worktree is classified as `external` instead
and never forces a registry: a registry row can only describe a Git repository,
so refusing such a plan would demand a declaration nobody could write. A plan
naming an ASL plan-store artifact or any other non-Git path therefore needs no
repair and no reworded prose. The finding is derived from the plan's stored `cwd` when
`plan-metadata.json` is present, so `asl preflight PLAN_DIR --json` reports it
without `--cwd`. It is one of two non-bypassable categories: `asl run
--allow-not-loop-ready` still pauses with `pause_reason: not-loop-ready` and
the category listed in `pause_details.blocker`, and `verification` refuses
the escaping check at run time even if the gate is skipped. The JSON payload
carries a `repository_footprint` object (described under JSON Output) whose
`suggested_registry_table` is the exact `## Repositories` block the migration
writes. The fix is `asl register-repositories`, not a widened `--cwd`; a plan
that escapes its bound root while staying inside one repository is the one case
the migration answers `not-required`, and its escaping target has to be
rewritten instead.

`repository-registry-required` is the second non-bypassable category. It is
red-only, applies to legacy plans, and fires when the plan *intends* to reach
more than one Git repository — including contract targets no session has
created yet, which `repository-footprint` structurally cannot see because it
only counts paths that already exist. Candidates are collected from session
prose, `context.md`, `manifest.md`, parsed contracts, and stored plan
metadata, resolved through symlinks to the nearest existing ancestor, and
anchored to the plan's stored `cwd` or an explicit `--cwd`. Its repair hint
(`repository-registry-required.declare`) names `asl register-repositories` and
the three declarations that command derives from disk: a top-level
`## Repositories` table, per-session `**Reads repositories**:` and
`**Writes repositories**:` scopes, and a `repo` alias plus repository-relative
`path`/`cwd` on every contract check. The JSON payload carries a bounded,
credential-redacted `repository_intent` object whose `git_roots` names the
repositories to register. `--mark-loop-ready` cannot silence it —
`_mark_loop_ready` refuses outright while any non-bypassable category is red —
but the category is no longer outside auto-repair. When preflight was given a
`--cwd` and the same read-only derivation `asl register-repositories` runs
returns a migration for the plan, `auto_repair_plan` leads with two steps:

```text
asl register-repositories <plan-dir> --cwd <bound-root> --json
asl prepare <plan-dir> --resume --rebind-workspace --cwd <workspace-root>
```

Both carry `--state-dir` when preflight was given one, so the migration
inspects the lock preflight inspected. `suggested_pause` is then `None`,
because a skill driving the plan clears the blocker mechanically. Both steps
are withheld — and the pause stands — whenever that derivation does not
return a migration, so a single-repository escape (a `repository-footprint`
finding with one Git root) still pauses with no step: `asl register-repositories`
would answer `not-required` there and the rebind would fail with
`--rebind-workspace requires a registered plan`. A derivation that refuses is
withheld the same way and additionally names its `missing_fact` in the pause
detail, because advertising a step the command has already decided to refuse —
for an unobservable `origin`, a contract check under no registered repository,
or a completed handoff a registry would invalidate — would advertise a repair
that cannot land. `asl run` applies the same migration
itself at its intent gate, on that same admissibility rule rather than one of
its own, then still pauses once for the rebind. `asl prepare` refuses the same plan before
publishing any change with exit reason `repository-registry-required`, and
`asl expand-ingest` emits the same reason with the `repository_intent`
payload. Never resolve the condition by selecting one child checkout as the
root. The runner re-derives the same verdict independently, so the invariant
holds even when preflight never runs — see the repository hard gate under
Command Behavior.
For a registered plan, `--cwd` is the non-Git-capable common workspace root.
Preflight resolves the ordered registry once; validates child existence,
directory type, exact Git top-level, origin, disjointness, scope, and contract
locations; inventories all workspace Git roots fail-closed; and reuses the
resolved repository context for command and baseline checks. Commit mode
requires every registered tree to be clean. Registered JSON adds
`workspace_root`, `registry_digest`, and ordered `repositories` summaries;
text output prints the same ordered summaries. Legacy plans retain `Repo root`
text and omit those additive JSON fields.
`--mark-loop-ready` force-backfills stale or missing `progress.md` when
`progress-journal-missing` is the only non-readiness blocker, then flips
`**Readiness**` to `loop-ready` and re-stamps the SHA and timestamp when the
only remaining blocking findings are manifest-readiness findings. It refuses
when any other non-readiness red or yellow finding remains.

The readiness hash includes session files and expansion notes. Legacy
`parallel-validation.md`, `## Parallel Execution` manifest blocks, and
`## Parallel Metadata` session blocks are excluded entirely.

`verify` reads a session's `## Completion Contract` and executes its `### Verify`
recipe against `--cwd` (defaults to the current directory). Exit code is the
gate decision: 0 when every authoritative gate passes, non-zero otherwise.
Conditional diagnostics run only after a named gate failure, so they enrich a
failure without becoming an additional gate. JSON output includes `passed`,
`recipe`, executed `checks`, `skipped_checks`, `diagnostic_failures`, and a
stable `reason` (`ok`,
`verify-failed`, `no-contract`, `malformed-contract`, `cwd-error`, or
`invalid-session`). Per-check rows include stdout/stderr snippets and a stable
`failure_reason` enum.
`--commit-at-end` runs the same grouped coordination after all pending sessions
finish successfully in that runner invocation. In registered mode it selects
all currently changed declared repositories; a clean repository is omitted.
`--commit-at-end` cannot be combined
with `--max-sessions` unless `--max-sessions` is high enough to complete every
session that was pending at the start of the run. If text TTY run controls stop
the run before all pending sessions finish, `--commit-at-end` does not run.

`--note TEXT` stores display-only reference text on the run payload. The note is
not sent to the harness and does not change plan execution. Text runs print it
in the compact `s` status snapshot, final completion or pause output, and
Ctrl+C interrupted exits. JSON/state output preserves it as top-level `note`.

For normal text TTY runs, `run` also listens for keyboard controls:

- `p`: toggle stop-after-current. When active, the current session still
  finishes and any `--commit-per-session` commit for that session runs; the run
  then exits 0 with `ok: true`, `paused: false`, and `reason: stopped`. The stop
  covers every kind of work the loop would start next: remaining sessions, the
  generated `--review-harness` session, and review repair children. Press `p`
  again before the session finishes to cancel the request.
- `h`: print a timestamped help line with available keys and current control
  state.
- `s`: print a compact live status snapshot to stderr. The snapshot includes
  the active or next session, session counts, active and future harness, model
  and effort metadata, control state, and current artifact paths. It does not
  mutate `state.json` or append a `run_control` event.
- `t`: toggle inline live tailing for the active text run. When enabled,
  inline tailing first prints the recent active-session stdout/stderr context
  (up to 40 prefixed log lines, re-replayed on each enable) and then streams
  only new log lines while enabled. It does not emit event or progress lines;
  normal runner progress remains the single source for heartbeats, session
  start/finish, control, status, and commit lines.
- `1`, `2`, `3`: select `codex`, `claude`, or `copilot` as the sticky harness
  for future turns in the same runner invocation. The active subprocess is never
  interrupted.

Keyboard controls are disabled for `run --json`, `run --dry-run`, and runs
where stdin or stderr is not a TTY.

`asl control` is the programmatic equivalent of those keys, so a supervising
agent driving `run --json` can still request them. Each subcommand appends one
command to the same durable per-plan control queue the Workbench HTTP API feeds
(`control-queue.jsonl`); the runner drains it at its lifecycle checkpoints and
applies the identical transition the keyboard listener emits. Nothing else is
mutated — the runner alone owns `state.json`.

- `asl control stop-after-current PLAN_DIR_OR_ID` queues the `p` request. Its
  payload reports `enqueued`, `command_id`, `run_active`,
  `pending_after_current` (sessions still pending once the executing one
  finishes), and `review_continuation_pending` (the run carries a review harness
  and its `review_resume` projection is not `terminal`), plus a `note`: `will
  stop after current session`, `current session is the last; stop also prevents
  the pending review`, `current session is the last; stop is a no-op` when
  nothing would follow, or `no active run; command applies when a runner next
  drains the queue`.
- `asl control select-harness PLAN_DIR_OR_ID --harness H` queues the `1`/`2`/`3`
  request. Its payload reports `enqueued`, `command_id`, `harness`,
  `run_active`, and a `note` naming the harness future sessions will use. An
  unknown harness fails with `reason: invalid-harness` before anything is
  enqueued.
- `asl control set-model PLAN_DIR_OR_ID` queues process-local model and effort
  overrides for a future implementation turn. Its payload reports `enqueued`,
  `command_id`, the optional target `harness`, the model and effort set/clear
  operations, `run_active`, and `applies_to: next-implementation-turn`. At least
  one set/clear operation is required; omitting all four fails with
  `reason: missing-model-control-operation` before anything is enqueued. An
  unknown target harness fails with `reason: invalid-harness`.

Enqueueing succeeds for a readable plan even with no runner attached; the
command simply waits in the queue for the next drain. All three subcommands
accept the same `PLAN_DIR_OR_ID` compact-label resolution as `status` and reuse
its `ambiguous-plan` / `malformed-plan` / `state-error` failure reasons. A queue
the process cannot create or append to — an unwritable or non-directory state
root — fails with `reason: os-error` and a `details.queue_path`, still as exactly
one JSON object under `--json` (a single stderr line otherwise), never a
traceback. Any other command rejected by the durable queue fails with
`reason: invalid-command`. The control-handler reason vocabulary is
`invalid-command`, `invalid-harness`, `missing-model-control-operation`, and
`os-error`.

`status` reads durable plan files and prints session counts, the next pending
session, content hashes, state paths, lock state, and any active harness turn.
Use `--json` for scripts. Its positional argument, `PLAN_DIR_OR_ID`, accepts
either an existing plan directory or the exact compact plan label printed after
`plan` by `asl sessions`. This compact plan label identifies a whole plan; it
is distinct from the dotted session identifier printed after `session` (for
example `session 2.1`), which names one session inside a plan and is not
accepted here. Existing absolute or relative directories always take
precedence, and only a bare, nonexistent argument (no directory component)
triggers a state-root lookup honoring `--state-dir`. Matching is exact and
case-sensitive against existing plan directories: zero matches keep the current
`malformed-plan` error, while multiple matches fail with `reason:
ambiguous-plan`. Bare-label resolution is available to `status`, `asl control`,
and all seven mailbox commands. They are `asl inbox guidance`,
`asl inbox reply`, `asl inbox resolve`, `asl inbox read`, `asl ask`,
`asl notify`, and `asl outbox wait`. `run`,
`tail`, `unlock`, and other plan-taking commands remain path-only. Successful
text and JSON output continue to report the canonical resolved `plan_dir`.

`tail` prints recent activity for the latest run and follows `events.jsonl`
plus live harness and commit stdout/stderr logs until the observed run
finishes. Use `--no-follow` to print recent activity and exit. `tail` remains
the full event-and-log viewer; the `t` keyboard control embeds a logs-only
variant of the same follower inside a text TTY `run` so runner progress is the
single source for heartbeats and event lines.

`sessions` shows ASL-managed activity across every plan state directory under
the resolved state root. It scans each per-plan-hash directory for live
`lock.json` and `state.json` (running rows) and walks every
`runs/<run_id>/turn-*/result.json` artifact (recent rows). Output has two
sections: `Running sessions` lists currently active turns (or `loop`-kind
rows when a runner is alive between turns) with started time, elapsed, idle,
PID, harness, session ID, turn index, and plan; `Recent sessions` lists the
most recently finished turns with finished time, duration, status result,
harness, session ID, run ID, and plan. `--running` and `--recent` are
independent filters; passing neither (or both) shows both sections. `--limit`
must be non-negative, defaults to `20`, and truncates recent rows only. Text
output uses compact plan-directory basenames; `--verbose` opts into full
canonical paths; JSON always includes full paths. The compact plan basename
shown after `plan` can be passed directly to `asl status` as its
`PLAN_DIR_OR_ID` argument; the dotted value after `session` remains a
session identifier within a plan and is not a `status` argument. A missing state root returns
success with empty arrays — only a catastrophic state-root error yields a
nonzero exit with `reason: state-error`. Recent rows are sourced from immutable turn artifacts,
so repeated runs for the same plan are not collapsed by the latest
`state.json`. Stale locks (dead PIDs, mismatched plan dirs, or invalid
shapes) and unreadable artifacts surface in dedicated `Stale locks` and
`Skipped artifacts` footer sections without aborting the scan.

`doctor` performs a non-mutating harness and path check. For registered plans
it resolves `--cwd` as the common workspace root and reports the ordered child
repositories in text and JSON. Use `--json` for scriptable prerequisite checks.

`unlock` removes a stale lock. It refuses to remove an active lock unless
`--force` is supplied. Use `--json` when automation needs the removed lock
state or an active-lock failure payload.

`compact-state` reclaims duplicated operational JSON by converting a plan's
older v1 `state.json`, `events.jsonl`, and per-turn `result.json` records to the
compact schema-v2 projection (the same shape new runs already persist). It
strips per-turn plan snapshots and raw verification output from state and
events, and for result records it extracts complete failed-check output into
recoverable gzip artifacts (readable through the bounded log API) while
discarding successful-check output after keeping a bounded preview. The default
is a dry-run that reports projected bytes reclaimed, files rewritten, and lossy
pass-output removal without writing; pass `--apply` to rewrite the records.
Reclaim figures are net of the gzip bytes those retained failure artifacts add
(reported separately as `artifact_bytes_added`), so a compaction that keeps a
poorly compressible failure artifact never reports more saving than is actually
reclaimed on disk.
Every rewrite is atomic (temp-file + rename), so an interrupted compaction never
leaves a partially written JSON file, and re-running is idempotent. `--json`
emits the full `CompactionReport` payload.

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
lock](#stale-unfinished-statejson-with-no-lock), so a dry run leaves every lock
file in place and one plan is judged identically whether it was reached through
`PLAN_DIR` or through the corpus sweep — the whole-corpus liveness pre-pass can
no longer decide a retain question by removing the lock that answers it.
`--apply` deletes `runs/<run-id>/` directories and nothing else, so lock hygiene
is never an applied action either.
`--json` emits the full per-plan projection, including every run's size and
retain reason.

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
fields inside `state.json`, then appends a `state_migrated` event. Historical
per-turn `result.json` paths are left untouched and reported as an
`unmigrated_artifacts_note`. It refuses (in both dry-run and apply, leaving both
state directories untouched) when the source plan directory still exists on disk
(`source-plan-exists` — its state is not orphaned, so migrating would steal a
live plan's state), the source holds an active lock, or the destination state
directory already exists. The default is a dry-run; pass `--apply` to perform the
rename and rewrite, and `--json` for the machine payload.

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

The replacement root must differ from the current registered binding, every
declared repository must resolve beneath it as an exact Git top-level with the
expected credential-free origin, and the plan must have no active runner lock.
New preparation, legacy plans, unchanged bindings, and ordinary `asl run`
invocations reject the flag. After validating the replacement workspace,
prepare holds the plan lock and copies the plan into an isolated system-temporary
staging view while preserving symlinks.
It discards the staged copy's old aggregate and baseline cache, revalidates all
sessions, regenerates every expansion and correction, runs fresh baselines,
generates replacement metadata, and completes final loop-ready preflight there.
The original plan is untouched until one `PlanTransaction` publishes the new
metadata, validator aggregate, expansion files, corrected session text,
baseline cache, and readiness manifest. The transaction records immutable
payloads in a durable journal before replacing any destination: ordinary I/O
failures roll back immediately, while the next authoritative plan read rolls a
process interruption forward to the complete new generation. Publication and
recovery share a marker-scoped mutex; acquisition has a bounded wait, and a
timeout reports the lock path plus its recorded process and thread holder rather
than leaving plan readers blocked indefinitely. A failure before publication
discards the staged view, releases the lock, and preserves all old plan bytes;
staged blocker and step paths are rebased to the durable plan directory. Prepare JSON
adds `workspace_rebind` with credential-free `old` and `new` workspace and
repository projections, a `failure_stage` and `error` on failure, and the
relative `published_files` list on success. Failed rebind guidance retains
`--rebind-workspace` in its resumable command. Lock and concurrency races use
the distinct `workspace-rebind-failed` reason and include `workspace_rebind`
context rather than being classified as input errors.

A registered plan may persist explicit workspace inventory exclusions during
preparation:

```text
asl prepare PLAN_DIR --resume --cwd WORKSPACE_ROOT \
  --workspace-inventory-exclude 'Library/Application Support/Protected.dv'
```

The option is repeatable. Supplied values replace the existing policy after
being normalized as literal workspace-relative POSIX paths, sorted, and
deduplicated. Absolute paths, `..` traversal, the workspace root, and any path
that equals, contains, or sits beneath a registered repository are rejected.
ASL never discovers or adds these exclusions automatically. An ordinary
same-root resume with neither policy option preserves the stored list;
`--clear-workspace-inventory-excludes` empties it. A deliberate workspace
rebind clears the old list unless replacement exclusions are supplied, because
the paths belong to the old root. The two policy options are mutually
exclusive.

`asl expand-ingest` is the standalone ingestion step `prepare` runs internally,
exposed for the manual `session-expand` path: it reads one validator-output
batch file (`--batch-file`), parses it fail-closed, merges it into the stored
`prepare-validation.json` aggregate (`ingest_validator_output`), (re)renders the
batch's `session-<id>.expand.md` files (`write_expansions`), and applies every
proposed factual correction for the batch (`apply_corrections`). The success
payload reports the ingested `sessions`, the written `files`, and per-correction
`corrections` outcomes — `applied`/`already-applied`/`blocked`, where a blocked
correction carries a stable `blocker` (`not-found`, `ambiguous`,
`contract-body`, `handoff-body`, or `structural`) — plus a `correction_summary`
count. A malformed payload exits 1 with `reason: validator-schema` and the
`PlanError` message verbatim before any correction reaches disk; an unreadable
batch file exits 1 with `reason: input-error`.

`prepare` also writes `PLAN_DIR/plan-metadata.json` as soon as the authored or
resumed plan directory is bound, including plans retained after a later
preparation failure. Legacy plans retain the deliberately small version 1
schema:

```json
{
  "version": 1,
  "cwd": "/canonical/absolute/repository/path"
}
```

Registered plans write version 3 with `workspace_root`, the ordered registry
digest, and `workspace_inventory_excludes`. Version 2 remains readable and is
interpreted as an empty exclusion list; the next prepare metadata write uses
version 3. On ordinary resume, `prepare` refuses to rebind when the stored root,
digest, or validation aggregate differs from the resolved workspace; the
explicit staged replacement flow above is the only exception. `asl run` also
refuses a root or registry mismatch and never rewrites an existing binding;
only an unbound legacy plan is backfilled on a non-dry run. `--dry-run` does not
write metadata. Registered runner state and JSON surfaces add `workspace_root`,
`registry_digest`, ordered resolved `repositories`, and
`workspace_inventory_excludes`, while retaining `cwd` as the workspace-root
compatibility alias. Resume validates those persisted workspace fields against
the current durable binding before continuing. The
metadata file is not plan prose and is excluded from both the plan content and
stable readiness hashes.

Source-plan session headings use a tolerant, fence-aware grammar shared by
`session-split`, `plan-iterate`, and `asl prepare`. Outside matching backtick or
tilde fences, the parser accepts case-insensitive H2-H6 ATX headings with zero
to three leading spaces, horizontal whitespace after the opening hashes, and a
positive dotted-decimal ID after the `Session` token. Source components may be
zero-padded. The ID may stand alone or introduce a non-empty title with `:`,
`-`, `–`, or `—`; horizontal whitespace around that separator is optional, and
whitespace-delimited ATX closing hashes are allowed. For example, all of these
are explicit boundaries:

```markdown
## Session 01
 ### session 02 - Add validation ###
###### SESSION 03.004 — Ship docs
```

Accepted source headings remain verbatim: preparation does not require
normalization approval or rewrite the source. Artifact identities are strict
and canonical, however, so `Session 03.004` produces ID `3.4`,
`session-3.4.md`, and unpadded manifest and completion-contract IDs. The
general-purpose `SessionId` API remains strict; zero padding is tolerated only
while parsing the source heading. An H2-H6 line outside a fence that begins
with the `Session` token but has a zero component, unsupported or dangling
separator, or another malformed suffix raises a line-specific `PlanError`.
The grammar-neutral structure gate surfaces that diagnostic as
`needs-plan-iteration` before authoring any output rather than treating the
candidate as body text or as a plan with no boundaries.

`--harness-model` and `--review-harness-model` are preserved in JSON payloads
and forwarded into both the emitted `asl run` command and any emitted
`asl prepare --resume` command. `--review-harness-model` requires
`--review-harness`. Preparation deliberately has no validator-model flag:
`--validator-harness` always uses that harness's native or configured default.
Stable JSON reasons: `ok`, `needs-plan-iteration`, `validation-failed`,
`baseline-unsatisfied`, `not-loop-ready`, `input-error`, `cwd-error`, and
`plan-structure`.

`metrics` aggregates workflow reliability and storage numbers from the
persisted state corpus, defaulting to the last 7 days (`--since-days N`
widens or narrows the window). It reports turn totals and unsuccessful
runtime, first-pass session completion, pause counts by reason (including the
never-retry quota/auth/invalid-model classifications), verification repair
convergence (verify-failed pauses, exhaustion, and sessions repaired after a
failure), review repair-cycle dispositions with introduced/closed/repeated
finding counts, reopen attempts and successful/failed reopen materializations,
reopen attempts by source disposition, and reopen failures by result. Those
cycle and reopen activity counters are timestamp-windowed; recovered reopen
backfills supply lineage but are not fresh attempts. Review materializations
are timestamp-windowed too: they are deduplicated by source review across the
full `review_cycle` and `review_repair_reopened` history plus generated repair
children, then counted only when the event that carries one falls inside the
window. Reconciling against the whole history is what keeps one source review
from being counted twice for legacy lineage-less cycles, while a
materialization recovered from durable children alone has no timestamp and is
excluded, exactly as an undated turn or event is. `review_resume` remains the
whole-history cap authority after materialize/event-append crashes, so its
`historical_materializations` deliberately exceeds a narrow window's count.
JSON exposes the explicit
`review_cycles.unique_materializations`, `reopen_attempts`,
`successful_reopen_materializations`, `reopen_failures`,
`reopen_attempts_by_source_disposition`, and `reopen_failures_by_result`
fields while retaining
`review_cycles.repairs_materialized` as a compatible alias with the same
idempotent value. Metrics also report artifact corpus size and in-window
growth, and projected compaction savings (a dry-run over the state directories
active inside the window, net of retained failure-artifact bytes; use
`asl compact-state` for a full per-plan projection). Every state directory is
sized, since corpus size is not a windowed question, but one whose files were
all last written before the window opened is skipped before its event log is
read, so a narrow window costs a stat walk instead of a full-corpus read. Those compaction savings
cover operational JSON only; `asl gc` projects the age-based reclaim over
`runs/<run-id>/` directories, which is where corpus size actually accumulates,
and renders its byte totals the same way so the two commands compare directly.
The command is read-only:
it never mutates state, events, or plan directories.

`asl questions` reads either an active live mailbox wait or the durable paused
turn for a plan and renders an external resolver prompt that another agent (or
a human) can use. It never invokes the harness and never writes an operator
reply. A live resolver is headed `# Live Agent Session - Resolver Prompt` and
offers three explicit outcomes: submit a factual answer with `asl inbox reply`,
close a plainly nonblocking question without an answer using `asl inbox resolve
--action continue`, or end the live wait for ordinary paused-run recovery using
`--action pause`. Its JSON adds `context_kind: "waiting"`, `question_id`,
`waiting_for_reply`, `reply_command`, `reply_stdin_command`,
`continue_command`, and `pause_command`. A durable paused resolver is headed
`# Paused Agent Session - Resolver Prompt` and retains its `asl run --reply`
resume guidance. Both JSON shapes include `ok`, `command: "questions"`,
`session_id`, `next_pending_id`, verbatim `last_message`, assembled
`rendered_prompt`, and `state_path`. If neither context exists, `--json`
produces `ok: false` with a stable `reason` (`no-paused-run`,
`missing-question-context`, or `malformed-plan`) and exits cleanly. These
external operator-facing prompts remain distinct from the in-harness
`## ASL reply context` and `## ASL resolution context` blocks.

`asl review-verdict` is the authoritative, read-only surface for a plan's
final-review verdict. It merges the shared `load_review_verdict` payload (its
`ok`/`reason` and, when the structured `final-review.json` is present, the full
untruncated `findings` list) with the read-only continuation evaluation under
`review_resume`, then attaches the complete `summary` of `final-review.md` with
`summary_truncated: false` and `summary_total_chars`, plus `next_steps`
remediation commands. Unlike the autopilot helper's bounded excerpt — which
flags `summary_truncated` and points here via a `full_review` hint — this
command never truncates, so it is the escape hatch when a long review would
otherwise be clipped. It never mutates state, events, or plan directories.

`asl run --reply TEXT` resumes a paused plan by injecting the operator's reply
as a one-shot prefix on the next harness turn only. The reply is expanded into
a `## ASL reply context` block above the regular session prompt;
`build_reply_prompt` always reuses `build_session_prompt` byte-for-byte for the
first line. Under `### Operator reply`, the stripped body is the `text` value
of one ASCII JSON object between `BEGIN ASL OPERATOR REPLY JSONL` and
`END ASL OPERATOR REPLY JSONL`; the argv, stdin, and mailbox origins share this
representation. The runner persists the resume metadata as a `reply_provided`
event (no reply text) and tags the resumed turn with `prompt_kind: "reply"`
plus a `reply_source` summary; the literal reply text is never stored in
`state.json`, `events.jsonl`, `argv`, or turn artifacts (the persisted argv
substitutes a placeholder like `<asl reply prompt redacted: N chars>` for the
full in-harness reply prompt).
The reply is consumed only after the resumed harness turn starts; pre-harness
failures such as an uncleared `not-loop-ready` gate preserve the previous
paused `state.json` and `events.jsonl` so the same reply can be retried after
repair.
Subsequent turns in the same run drop back to the plain session prompt. The
flag is allowed only when the most recent turn paused with a non-empty
`last-message.txt`; otherwise `run --reply` exits non-zero with `reason:
no-paused-run`, `reason: missing-question-context`, `reason:
reply-context-mismatch` (paused turn references a different session than the
one currently pending), or `reason: empty-reply` (CLI-only validation; the
flag's argument was empty or whitespace). `run --dry-run --reply TEXT` prints
the additional `Resolver prompt:` block above the regular `Prompt:` and
`Argv:` blocks so operators can preview both surfaces without touching the
harness, consuming the paused state, or writing the preview payload to
`state.json`/`events.jsonl`.

`asl run --reply-stdin` is the same resume as `--reply TEXT`, except the reply
text is read from stdin instead of an argv value, so it never appears in the
process listing. `--reply` and `--reply-stdin` are mutually exclusive. This is
the channel the `$asl` autopilot skill drives when it resumes a paused run (see
[Autopilot skill](#autopilot-skill-asl)); operators typically use `--reply TEXT`
directly.

`asl assurance list` and `asl assurance show` are the read-only operator surface
over the assurance inbox. Both read the append-only `assurance-inbox.jsonl`
ledger through the same projection every other consumer uses, under a shared
lock; neither appends a record, mints an issue id, rebuilds the index cache, or
repairs a malformed line, so browsing a damaged inbox reports the damage instead
of quietly rewriting it. A missing ledger is an empty inbox, not an error.

The assurance ledger and the live per-plan mailbox are separate systems, and
nothing connects them. `assurance-inbox.jsonl` sits directly under the state
root, is shared by every plan in the corpus, records what audits found after
runs ended, is permanent, and is written under its own `assurance-inbox.lock`.
`mailbox.jsonl` sits inside one plan's state directory, carries guidance,
questions, replies, and notices for a run that is still going, is enabled per
run, and is written under its own mailbox locking with a separate
`mailbox-lease.json` supervisor lease. They share no file, no lock, no id
space, and no lifetime; `asl inbox ...` never reads or writes the assurance
ledger, and `asl assurance ...` never reads or writes a mailbox.

`list` answers the default triage question — what is still open — by printing
the 50 most recently observed open issues, newest sighting first, with the
issue id, severity, state, occurrence count, last-seen timestamp, component, and
a one-line summary preview per row. `--limit ROWS` widens or narrows the cap and
`--state`/`--severity` re-aim the filters; `--all` is the negation of the whole
default, meaning every state with no row cap, and is refused in combination with
`--limit` or `--state` rather than silently picking a winner. When the cap hides
matching issues the text form says how many and how to see them. Unusable filter
values exit `1` with `reason: input-error` before the ledger is opened.

`show ISSUE` prints everything the ledger records about one issue: current
state, the fingerprint metadata it deduplicates by (category, component,
invariant key, lane, schema version), the full summary and recommendation,
observation history with the run and audit artifact behind each sighting,
recorded dispositions, absorbed ids, reopen count, improvement plan drafts, and
the affected runs with their artifact references and closeouts. Occurrence
history is capped the same way rows are, with the newest kept and the remainder
counted. Because a merge redirects an id rather than deleting it, showing a
merged-away id succeeds and reports both the id asked for and the id it landed
on. An id that names nothing exits `1` with `reason: unknown-issue`.

Both commands accept the canonical `ASI-42` and the documented bare `42`
shorthand, and both always emit the canonical form, so any id printed by `run`,
`status`, or `list` pastes straight into `show`. One payload builder feeds the
text and JSON renderers, so the two surfaces cannot disagree about what an issue
is. Ledger lines the projection could not read are reported as warnings rather
than failures — on stderr in the text form, and as `warnings`, `warning_count`,
`warnings_omitted`, `rejected_count`, and `anomaly_count` in the JSON payload —
because the rest of the file is still history a human needs and hiding the
rejected lines would hide the only evidence that the ledger is damaged. `show`
prints those warnings before an `unknown-issue` error, since a rejected line is
often the reason a legitimately copied id no longer resolves.

`asl assurance resolve`, `dismiss`, and `accept-risk` are how an issue is
managed. Each appends one record carrying the decision and an optional
`--note TEXT`; nothing is edited and nothing is deleted, so revising a decision
leaves both records and the order they were reached in on file. None of them
removes the issue, and the printed confirmation says what the new state claims
about the future rather than that the issue is gone: a recurrence after
`resolve` reopens it, while a recurrence after `dismiss` or `accept-risk` is
recorded without reopening it. A disposition aimed at a merged-away id lands on
the issue that owns its history today and reports both ids. An id that names
nothing exits `1` with `reason: unknown-issue`.

`asl assurance merge ISSUE TARGET` moves one issue's whole history into
another. It cannot be undone, so a person at a terminal is asked to confirm
first; `--yes` skips that prompt, and a piped or `--json` invocation is treated
as a deliberate one and is never prompted. Declining exits `1` with
`reason: cancelled` and writes nothing. Every refusal reports the projection's
own reason — `merge-self`, `merge-unknown-source`, `merge-unknown-target`,
`merge-source-already-merged`, `merge-target-already-merged`, or `merge-cycle`.

`asl assurance closeout RUN_ID --outcome accepted|reworked|escaped` records what
one audited run's findings were worth, with optional `--rework-minutes N`,
repeatable `--issue ISSUE`, `--note TEXT`, `--plan PLAN_DIR_OR_ID`, and
`--replace`. The run id resolves through the state corpus: plan attribution is
recovered from whichever plan state directory holds the run, and `--plan` is
required only when several plans hold the same id, which exits `1` with
`reason: ambiguous-run` and lists the candidates. A `--plan` that never produced
the run exits `1` with `reason: unknown-run`, and a run directory that has been
pruned is not an obstacle, because the ledger rather than the filesystem decides
what can be closed out. An outcome outside the vocabulary and a negative or
non-finite `--rework-minutes` exit `1` with `reason: input-error` before the
ledger is opened; a run with no audit record exits `1` with
`reason: unknown-run`, an `--issue` the inbox never opened exits `1` with
`reason: unknown-issue`, and a run that already has a closeout exits `1` with
`reason: duplicate-closeout` unless `--replace` is passed. Replacing is an
append like everything else: the new record names the ledger line holding the
closeout it supersedes as `seq-N`, both verdicts stay readable, and the later
one is the one in force.

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
year ago says nothing about the last seven days. `dispositions` reports the
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

## Harness Commands

The built-in adapters construct argv lists directly and never use shell
interpolation.

Codex:

```text
codex exec -C <cwd> --add-dir <plan-dir> --dangerously-bypass-approvals-and-sandbox [--model <model>] [-c model_reasoning_effort="<effort>"] -o <last-message-file> <prompt>
```

Claude:

```text
claude --print --verbose --output-format stream-json --include-partial-messages --add-dir <plan-dir> --permission-mode bypassPermissions [--model <model>] [--effort <effort>] <prompt>
```

Copilot:

```text
copilot -C <cwd> --add-dir <plan-dir> [--allow-all-tools --allow-all-paths] --no-ask-user --silent [--model <model>] [--effort <effort>] -p <prompt>
```

Harness prompts use the expected skill prefix:

```text
$session-run <plan-dir>/ --runner-managed
/session-run <plan-dir>/ --runner-managed
```

Codex writes a last-message file with `-o`. Claude streams JSONL in print mode;
the adapter renders that stream as concise `claude ...` stdout log lines for
live tailing and keeps the final `result` text as the last message. Copilot does
not use a last-message file, so its last-message capture falls back to stdout.
ASL's `--harness-model` is forwarded only to turns using the implementation
harness declared by `--harness`. `--review-harness-model` is forwarded only to
generated review turns and requires `--review-harness`; it never affects
implementation turns. Omitting either model flag preserves the corresponding
harness CLI default. Reporting still uses per-turn best-effort metadata from
Codex `~/.codex/config.toml`, Copilot `~/.copilot/settings.json`, or
`native CLI default` with source `unresolved` when the default cannot be read.
If a text TTY run switches implementation harnesses with `1`, `2`, or `3`, the
switched harness uses its native or configured default rather than inheriting
`--harness-model`. `--effort` remains shared and carries across implementation
and review turns, including later harness switches. `doctor --harness-model`
checks the selected harness/model pairing without adding a review role.

## Safety Modes

By default, `run` and `doctor` use harness-specific bypass modes for every
harness. For Codex, Claude, and Copilot this means:

```text
codex exec -C <cwd> --add-dir <plan-dir> --dangerously-bypass-approvals-and-sandbox -o <last-message-file> <prompt>
claude --print --verbose --output-format stream-json --include-partial-messages --add-dir <plan-dir> --permission-mode bypassPermissions <prompt>
copilot -C <cwd> --add-dir <plan-dir> --allow-all-tools --allow-all-paths --no-ask-user --silent -p <prompt>
```

`--unsafe-bypass` is accepted as an explicit form of the default. Pass
`--no-unsafe-bypass` to use the harness default sandbox and permission mode
where supported by that harness:

```text
codex exec -C <cwd> --add-dir <plan-dir> -s workspace-write -o <last-message-file> <prompt>
claude --print --verbose --output-format stream-json --include-partial-messages --add-dir <plan-dir> --permission-mode dontAsk <prompt>
copilot -C <cwd> --add-dir <plan-dir> --no-ask-user --silent -p <prompt>
```

For Copilot, `--no-unsafe-bypass` does not re-enable interactive approval prompts; it keeps `--no-ask-user --silent` and only removes `--allow-all-tools --allow-all-paths`.
Because bypass mode is the default, run only trusted plans against trusted
repositories, or pass `--no-unsafe-bypass` when the harness should stay in its
normal workspace sandbox.

The runner itself never marks sessions complete. Durable completion is only the
`- **Status**: complete` line inside the final `## Handoff` block of each
`session-<id>.md` file. Operational state can be deleted and rebuilt from the
plan files. After a successful turn, the runner may update recognized status
tables in `manifest.md` and `context.md`, but those tables are derived
bookkeeping; handoff statuses remain authoritative.

### Process sweep

Every harness turn and every verification command check runs in a session of
its own (`start_new_session=True`), so each owns a process group. However that
turn or check ends, the runner then sweeps it: one read of the process table,
then `SIGKILL` for every process still in the group and for every process
recorded in a snapshot of the root's descendants taken while the root was still
alive, provided it is still running under an identical command string *and* an
identical start time. That re-verification is what makes a snapshot safe to act
on after the fact: a pid reused by an unrelated process no longer matches on
command, and one reused by a process running the same command is rejected on
its start time (reported as `snapshot_pid_reused`). The start time comes from
`ps -o lstart=`; where a platform's `ps` does not report one, entries carry no
start time and command re-verification stands alone. Pid reuse also decides the
other route: the caller reaps the root before sweeping, so a root pid that is
still in the table now belongs to something else and the group under that number
is not the turn's — enumeration is skipped (`pgid_skipped: root_pid_reused`)
rather than aimed at a stranger's children. The root pid, the
runner itself, and pids <= 1 are never signalled. The sweep is best-effort
cleanup of something already finished, so an unreadable process table or a kill
that raced a reap degrades to a report rather than an error and can never fail
a turn or a check. It exists for two observed failures: a leftover dev server
from an earlier check failing a later one, and a descendant that called `setsid`
while holding the captured pipes open, which stalled a run for over two hours.

The sweep is **on by default**. Set `ASL_PROCESS_SWEEP_DISABLED=1` (or `true`,
`yes`, `on`) to turn it off; any other value, including a typo, leaves it
enabled, so a mistyped opt-out never silently disables cleanup. With the sweep
off, no process table is read and no report is produced.

The two identification routes cover different escapes, and the paths differ in
which routes they have:

- **Process group members.** Positive identification, since a group is only
  entered by inheritance from the root. This is the only route available after
  a turn or check that exited on its own, because there is no kill to snapshot
  before.
- **A pre-kill descendant snapshot.** A group kill reparents a `setsid`
  descendant to pid 1, after which no descendant walk can reach it, so the
  snapshot only works if it was taken while the root was still alive. The
  verification timeout, cancellation, and interrupt paths (the last covering a
  Ctrl-C that lands on the thread running a sequential recipe) and the harness
  turn timeout each take one; for a silent harness turn, the process tree the
  hang sentinel samples while diagnosing it serves as the same evidence, and
  that turn's sweep consumes it so a later turn handed the same root pid cannot
  reuse a sample of a tree that is gone.

The honest limit: a descendant that left the process group **and** whose parent
chain died before any snapshot was taken is identified by neither route and
survives the sweep. In practice that is a `setsid` descendant of a turn or check
that exited normally while nothing had sampled its tree. It can no longer stall
the run — the pipes it holds are no longer waited on, under the reader-join
bounds above — but it is not killed. Work that must not outlive the turn or
check that started it therefore must not be detached into a session of its own.

A sweep that killed something, or failed to, appends one `process_sweep` event
per turn and per swept check. Its payload carries `phase` (`turn` or
`verification`), the run/turn/session identity, the `check` name for a
verification sweep, and the report: `killed`, `kill_failed`, `survivors_after`,
and an `enumeration` block with `expected_pgid`, `table_size`, `pgid_members`,
`snapshot_size`, `snapshot_pid_reused`, `snapshot_verified`, `already_gone`, and
`resampled`. A sweep
that found nothing is the normal case and is not recorded, so a `process_sweep`
event always means real cleanup happened.

## State Files

By default, runtime metadata is stored under:

```text
~/.local/state/agent-session-loop/<plan-path-hash>/
```

`--state-dir DIR` changes the state root; the plan hash subdirectory is still
created below that root. The hash is based on the canonical plan directory path,
so logs and locks remain stable as session files change. `asl sessions` scans
every per-plan-hash subdirectory under the resolved state root, classifying
`lock.json` and `state.json` for running rows and walking every
`runs/<run_id>/turn-*/result.json` artifact for recent rows. Recent rows come
from the immutable artifact tree, so the same plan's repeated runs are visible
rather than collapsed by whichever `state.json` was overwritten last; running
rows come from the live lock plus the latest `state.json` `active_turn`.

State, event, and result records carry schema version 2: `state.json` and each
turn `result.json` have a top-level `version: 2`, and every `events.jsonl`
record carries `version: 2` on its envelope. Readers treat a missing `version`
key as v1 and keep accepting those records. The persisted v2 `state.json` is a
compact projection — per-turn `before`/`after` session lists and raw
verification check stdout/stderr are dropped (bounded previews, byte counts,
sha256 digests, and gzip artifact paths remain) — while each turn's
`result.json` keeps full per-turn detail. No v2 state or event record embeds
raw verification output; full failed-check output lives in the referenced
`verification/*.gz` artifacts.

Runtime files include:

- `state.json`: latest run payload.
- `events.jsonl`: append-only run, pause, turn, and commit events.
- `lock.json`: active per-plan runner lock.
- `mailbox.jsonl`: optional 4 MiB-capped mailbox log; physical lines are durable
  sequences and the file is never truncated in v1.
- `mailbox-lease.json`: optional fixed-size supervisor activity lease, fresh for
  90 seconds.
- `runs/<run_id>/`: immutable artifacts for one runner invocation.
- `runs/<run_id>/turn-0001-session-<id>/stdout.log`: stdout from the harness
  turn.
- `runs/<run_id>/turn-0001-session-<id>/stderr.log`: stderr from the harness
  turn.
- `runs/<run_id>/turn-0001-session-<id>/last-message.txt`: final harness
  message when available.
- `runs/<run_id>/turn-0001-session-<id>/result.json`: diagnostic result for the
  turn, including argv, status changes, runner signals, snapshots, and artifact
  paths.
- `runs/<run_id>/commit-0001-session-<id>/stdout.log`: stdout from an automatic
  per-session `spec commit --yes --no-verify` invocation.
- `runs/<run_id>/commit-0001-session-<id>/stderr.log`: stderr from that commit.
- `runs/<run_id>/commit-0001-session-<id>/result.json`: diagnostic commit
  result, including argv, cwd, return code, HEAD before/after, status result,
  commit SHA when one was created, and artifact paths.
- `runs/<run_id>/supervisor-audit-input.json`: the bounded operational evidence
  bundle the audit read.
- `runs/<run_id>/supervisor-audit.json`: the validated audit record.
- `runs/<run_id>/supervisor-audit.md`: the operator-readable audit report.
- `runs/<run_id>/supervisor-audit-attempt-N/`: per-attempt prompt, returned
  message, and outcome.
- `runs/<run_id>/assurance-pending.json`: the audit debt an abandoned run left
  for `asl assurance process-pending`.
- `runs/<run_id>/commit-0001-end/...`: equivalent artifacts for
  `--commit-at-end`.

Three assurance files sit one level up, directly under the state root rather
than inside any plan's directory, because the inbox spans the whole corpus:

- `assurance-inbox.jsonl`: the append-only assurance ledger.
- `assurance-inbox.lock`: the exclusive-append lock; writers never lock the
  data file itself.
- `assurance-index.json`: a disposable cache, rebuilt whenever it is missing,
  stale, malformed, or of an unknown version.

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
`tail_enabled`, `next_harness`, process-local `implementation_overrides`,
`consumed`, and `last_action`; `last_action` is one of
`stop-after-current-requested`, `stop-after-current-canceled`,
`stop-after-current-consumed`, `tail-enabled`, `tail-disabled`,
`harness-selected`, `implementation-model-updated`, or `null`.
`mailbox_activation` records the once-per-run decision and its reason
(`mailbox-present`, `fresh-lease`, `inactive`, or `disabled-by-flag`) plus the
resolved paths and best-effort lease evidence. Failed-run artifacts remain in
their original `runs/<run_id>/` directory when a later retry succeeds.

While a harness turn is active, `state.json` also includes `active_turn` with
the run id, turn index, session ID, harness, prompt, model and effort metadata,
artifact paths, elapsed and idle seconds, last activity metadata, heartbeat
count, and any best-effort action-required signal. While the runner is
executing a post-turn `## Completion Contract` `### Verify` recipe, `state.json`
also includes `active_verification` with the run id, turn index, session ID,
total `check_count`, the declared `check_names` and `check_kinds`,
`current_check_index`, `current_check_name`, `current_check_kind`,
`started_at`, `current_check_started_at`, `elapsed_seconds`,
`check_elapsed_seconds`, and `heartbeat_count`. `active_turn` is cleared before
`active_verification` is populated, so the two phases are mutually exclusive;
both are `null` between turns and after `run_finished`. During the bounded
nonterminal answer window, additive `waiting_for_reply` records only the
question/session/turn identity, start time, base deadline,
`unsupervised_deadline_at` with its `unsupervised_ceiling_seconds` span, and the
last observed lease activity. `unsupervised_deadline_at` is the ceiling as of
the last anchor the runner persisted — the start of the wait, or the instant it
woke from a system suspend — and a reader rolls it forward from
`last_lease_activity_at` plus the ceiling span rather than reading it as a
terminal instant. Waits persisted before the ceiling began rolling carry a
static `hard_deadline` instead of those two keys, and every reader still renders
that key for them. `events.jsonl` records
`turn_started`, `turn_heartbeat`, `action_required`, `turn_finished`,
`verification_started`, `verification_check_started`, `verification_heartbeat`,
`verification_check_finished`, `verification_check_skipped`,
`verification_finished`, `commit_started`,
`commit_finished`, `run_control`, `review_scope_selected`,
`review_session_created`, `review_cycle`,
`review_repair_reopened`, `mailbox`, `reply_provided`,
`resolution_consumed`, `process_sweep`,
`run_paused`, and
`run_finished` events as they happen. `process_sweep` is conditional: it is
appended only when a post-turn or post-check sweep actually signalled a
surviving process (see **Process sweep** above). The `mailbox` name uses
`guidance-observed`, `notice-published`, `wait-preparation-failed`,
`wait-started`, and `wait-cleared` action variants; mailbox activity does not
create a separate event name per record kind. Verification heartbeats
fire every 30 seconds while a single check is in flight, so long-running
checks remain observable in `tail` and the live status snapshot. Verification
check argv is captured in the `verification_check_started` payload and not
streamed to a log file; command stdout and stderr remain captured in the
underlying `asl verify` invocation and in the turn `result.json` under the
`verification` key. A conditional diagnostic whose gates all pass emits
`verification_check_skipped` with its name, kind, dependencies, and stable
`condition-not-met` reason. `verification_finished` reports configured,
executed, gate, diagnostic, and skipped counts.
A `"parallel": true` recipe adds that same additive key to
`verification_started`; its `verification_check_started` /
`verification_check_finished` records interleave by real completion order while
`result.json` stays in declared order, and heartbeats track whichever check
currently holds the display slot. Every check that has started and not yet
finished is tracked, so when the displayed check finishes the slot moves to
another still-active check and heartbeats continue until the last command
retires. An interrupt kills every abandoned check before `run_verification`
re-raises, so no callback outlives it; `finish` independently drops any check
callback from the moment it begins — before it waits for the heartbeat thread —
so `verification_finished` stays the last verification lifecycle event,
`active_verification` stays `null`, and no check progress line can appear after
the terminal verification message.
`review_session_created`
fires once per run, when the runner appends a fresh generated review session;
resuming an existing pending review session does not re-emit it. Its payload
carries `run_id`, the review `session_id`, `review_harness`,
`review_session_path`, `review_output_path`, and the additive `review_scope` /
`review_base_sha` keys. `review_scope_selected` fires immediately before it with
`run_id`, `session_id`, `scope`, `base_sha`, `fallback_reason`, and the additive
`refreshed` key — the auditable record of why a rereview was or was not
narrowed. It fires a second time, with `refreshed: true`, whenever a pending
review session's scope is re-derived and rewritten before that session executes,
so a resumed review's effective bounds are recorded rather than inferred from
the creation-time event. `review_cycle` fires
once per
completed review session and carries the repair/rereview cycle record (cycle
number, review session id, outcome, finding fingerprints with
introduced/closed/repeated classification, repair session ids, the reviewed
`repo_head` / `worktree_dirty` / `dirty_paths`, and
disposition). `review_repair_reopened` records each later attempt to consume
an existing findings artifact. Successful records include the source
cycle/session/disposition, previous and current artifact evidence, repair child
IDs, and before/after materialization-cap accounting; failed attempts preserve
the stable result and error without consuming budget. `tail` renders review
cycle events inline alongside other events. Commit events include the commit index,
scope, session ID for per-session commits, argv, cwd, artifact paths, status
result, duration, and commit SHA when one was created. `run_control` events
record `stop-after-current-requested`, `stop-after-current-canceled`,
`stop-after-current-consumed`, `tail-enabled`, `tail-disabled`, and
`harness-selected`, and `implementation-model-updated` actions with
`stop_after_current`, `tail_enabled`,
`next_harness`, and, when available, session and turn context. Harness-selection
events also include `current_harness` and `selected_harness`. `tail` renders
these as `run control:` lines.

For normal text runs, live progress is printed to stderr with local compact
timestamps, for example `[2026-06-04 14:32:10] session 2/3 started`, so stdout
remains the final human summary. Generated final-review turns are labeled
explicitly, for example `[2026-06-04 14:32:10] review session started (7/7)`.
The live progress lines cover run start, session starts and finishes, periodic
30-second heartbeats, action-required signals, commits, run-control help or
toggle messages, compact status snapshots, pauses, and run finish. Inline-tail
output, when `t` is enabled, streams only harness/commit log lines and does not
duplicate heartbeats or any other event/progress lines. Detailed artifact paths
remain in the final summary, `status`, `state.json`, and `events.jsonl`, and
are included in the `s` status snapshot on demand.

For `run --json`, stdout remains the final JSON payload and stderr stays quiet;
live monitoring should read `state.json`, `events.jsonl`, or use `tail`.

## JSON Output

Use `--json` for runner integrations:

```bash
asl run ~/.local/share/agent-plans/example-plan/ --harness codex --max-sessions 1 --json
asl status ~/.local/share/agent-plans/example-plan/ --json
asl questions ~/.local/share/agent-plans/example-plan/ --json
asl run ~/.local/share/agent-plans/example-plan/ --harness codex --reply "1a, 2b, 3a" --json
asl sessions --json
asl sessions --running --json
asl sessions --recent --limit 5 --json
asl doctor --harness codex --plan-dir ~/.local/share/agent-plans/example-plan/ --json
asl unlock ~/.local/share/agent-plans/example-plan/ --json
```

All parsed subcommands that support `--json` print JSON errors to stdout. Common
error fields are `ok`, `command`, `reason`, `error`, `paused`, and `details`.
`run --json` preserves its existing fields and adds the common `command` and
`details` fields; important run fields include `paused`, `reason`,
`pause_reason`, `pause_details`, `harness`, `harness_config`, `model`,
`model_source`, `effort`, `effort_source`, `commit_mode`, `note`, `commits`,
`control`, `no_mailbox`, `mailbox_enabled`, `mailbox_activation`,
`completed_sessions`, `completed_session_ids`, `turns`, `initial_snapshot`,
`final_snapshot`, `state_path`, and `events_path`. Runs without commit flags
report `commit_mode: null` and `commits: []`.

`status --json` adds a read-only `mailbox` projection. It reports the canonical
mailbox and lease paths, presence/activation, byte size and 4 MiB limit,
physical `last_seq`, pending-guidance and unanswered-question counts, rejected
record count, full state, resolution and notice totals, and best-effort errors.
Beside `full` it reports `accepts_questions`, `accepts_replies`, and
`accepts_resolutions`: the first says whether a record still fits under the
reduced cap shared by every non-closure kind, while the latter two independently
report capacity for their differently sized minimal valid closures. When the
mailbox cannot be read, all three fields and the resolution/notice totals are
`null` alongside `full` rather than defaulting to writable or empty. The text
`Mailbox:` line says `replies and resolutions still accepted`, `only replies
still accepted`, or `reply and resolution capacity exhausted` from those two
closure flags and never overclaims that a closure can land. It also renders the
resolution and notice totals without rendering their bodies. Alongside the file-faithful
`unanswered_questions` total it reports `open_questions`, the subset owned by a
run that is still live; it is `0` when no live run holds the plan and `null`
when the mailbox cannot be read. The text `Mailbox:` line leads with the open
count and appends the unanswered historical difference only when it is
nonzero, so an
abandoned record reads as history rather than a live block. Its lease fields include
`lease_present`, `lease_fresh`, `lease_reason`, and `lease_error`. The stable
lease-reason vocabulary is `missing`, `invalid-size`, `empty`, `invalid-json`,
`not-object`, `unsupported-version`, `invalid-supervisor-id`,
`invalid-updated-at`, `invalid-run-id`, `invalid-projection-seq`,
`invalid-unanswered-questions`, `invalid-latest-question-id`,
`invalid-latest-question-created-at`, `incomplete-question-projection`,
`future-updated-at`, and `expired`. An absent lease is
projected as `lease_present: false` with `lease_reason: null`; a fresh lease
has `lease_fresh: true` with `lease_reason: null`. Stale or malformed leases
carry their classification in `lease_reason`, while `lease_error` carries a
best-effort malformed/unreadable detail and is otherwise `null`. The projection
never returns raw message bodies.

Supervisor lease schema v2 remains a fixed 512-byte record and adds an optional,
text-free question projection: `run_id`, `projection_seq`,
`unanswered_questions`, `latest_unanswered_question_id`, and
`latest_unanswered_question_created_at`. The latest-question fields are absent
when the count is zero. The autopilot binds the projection only after status
attributes `state.json` to the current lock holder and republishes it after each
successful five-second mailbox poll. The projection sequence advances only when
the observed unanswered-question set changes, so unrelated mailbox records
cannot reassert an acknowledged question. Readers continue to accept v1 leases,
which carry no question projection.

The mailbox record schema version is **not** bumped for question ownership:
`run_id` is an optional question-only field that older readers ignore and newer
readers treat as absent when malformed, blank, or over 128 characters. A bad
value degrades the question to unattributed rather than rejecting the line, so
the reject vocabulary is unchanged and history written before run attribution
keeps parsing.

Before every self-launch or relaunch, the autopilot removes the previous
`run_id`, observed question IDs, and pending projection while preserving the
physical mailbox cursor, then publishes an unbound lease. Status is re-probed at
mailbox deadlines until `state.json` is proven newer than the current child
lock; only then can that run's v2 projection return. This prevents a watcher
that survives multiple chunks from refreshing an earlier run's question under
its successor.

Both `status --json` and a live run's state
expose compact `waiting_for_reply` metadata while a bounded answer wait is
active. The adjacent top-level `waiting_for_reply_status` field is `live` when
the wait's unsupervised ceiling is still in the future and the plan has an
active runner lock, `expired` when that ceiling has passed or the owning runner
lock is absent or stale, and `invalid` when the persisted ceiling is missing or
malformed; it is `null` when no wait is present. Because the ceiling bounds
unsupervised time, the classification consults lease freshness rather than a
bare timestamp: the projected ceiling is the later of `unsupervised_deadline_at`
and `last_lease_activity_at` plus `unsupervised_ceiling_seconds`, so a wait whose
supervisor keeps renewing stays `live` past the instant persisted at its start.
A record written before the ceiling began rolling carries only `hard_deadline`
and is classified from that timestamp exactly as it was before. A terminal
paused projection still takes precedence for legacy pause consumers.

Paused `run --json` payloads also carry an `interactive_help` object whenever
the paused turn left a non-empty `last-message.txt` body (it is `null`
otherwise). The block surfaces the data a wrapping coordinator needs to route
the operator conversation without rebuilding state: `questions_command` and
`reply_command` are ready-to-paste `asl questions` and `asl run --reply TEXT`
invocations preserving the current `--state-dir`; `session_id` is the paused
turn's session, `next_pending_id` is the canonical next pending ID,
`turn_index` is the paused turn index, `artifact_paths` repeats the durable
`last_message_path`/`stdout_path`/`stderr_path`/`result_path`/`artifact_dir`,
and `rendered_prompt` is the same resolver prompt `asl questions` would emit
(the operator reply text is never embedded). `asl questions --json` mirrors
the same fields under `ok: true` (`session_id`, `next_pending_id`,
`last_message`, `rendered_prompt`, `state_path`); failures use stable
`reason` strings (`no-paused-run`, `missing-question-context`,
`malformed-plan`). The reply turn itself is identifiable from
`turns[*].prompt_kind` (`"session"` for plain session prompts,
`"reply"` for the one-shot resume); reply turns also carry a `reply_source`
summary (`run_id`, `session_id`, `turn_index`, `last_message_path`,
`reply_length`) while the verbatim reply text is never persisted.

Review-harness fields are present on every run payload. `review_requested` is
`true` when `--review-harness` was supplied, `false` otherwise. `review_harness`
is the requested review harness name or `null`. `review_session_id` is the
canonical ID of the generated review session once it has been appended or
resumed, otherwise `null`. `review_output_path` is the absolute path to
`PLAN_DIR/final-review.md` whenever review was requested, otherwise `null`.
`review_completed` is `true` only after the review turn finished successfully.
`review_verdict` is `null` until a review completes successfully; then it
contains `status_line`, `outcome` (`"all-clear"` or `"findings"`),
`total_findings`, and `findings_by_severity` (`high`, `med`, `low`).
`review_skipped_reason` is `null` for clean review completions and for
implementation-only runs; for review-requested runs that did not complete it
records the terminal reason (`max-sessions`, `stopped`, a pause reason, etc.).
`completed_sessions` and `completed_session_ids` continue to include every
completed turn for backward compatibility; consumers that need to distinguish
review from implementation turns read `turns[*].session_kind`, which is either
`"implementation"` or `"review"`. The same `session_kind` field is also
recorded on `active_turn` while a review turn is in flight and on the recent
session overview rows surfaced by `asl sessions`. Dry-run `planned` payloads
include `review_requested`, `review_harness`, and `review_output_path` so
planners can preview the review path before any harness runs.

Both `status --json` and `run --json` expose the same additive
`review_resume` projection. Existing fields remain unchanged, so older clients
may ignore this block. `action` is one of `initial-review`,
`materialize-repairs`, `run-repairs`, `rereview`, or `terminal`; `eligible`
describes the persisted/effective-cap evaluation, and `blocker` is `null`,
`review-harness-required`, `higher-cap-required`, `artifact-invalid`, a
terminal disposition (`all-clear`, `findings-repeated`, or
`unstructured-findings`), or a non-disposition terminal blocker
(`repair-sessions-missing`, `repair-sessions-not-runnable`, or
`unknown-disposition`). An unrecognized recorded disposition is retained in
`source_disposition` for diagnosis and maps to the stable
`unknown-disposition` blocker. The projection includes source lineage,
historical/required/selected cap values, pending and completed repair IDs,
artifact validity/error, previous/current finding fingerprints, and nullable
source/current Markdown and JSON SHA-256 digests. For example, an all-complete
plan stopped at its absolute cap can report:

```json
{
  "review_resume": {
    "source_cycle": 2,
    "review_session_id": "7",
    "source_disposition": "cycle-cap-exhausted",
    "action": "materialize-repairs",
    "eligible": false,
    "blocker": "higher-cap-required",
    "historical_materializations": 2,
    "minimum_required_cap": 3,
    "selected_cap": 2,
    "repair_session_ids": [],
    "pending_repair_session_ids": [],
    "completed_repair_session_ids": [],
    "source_fingerprints": ["finding:abc"],
    "current_fingerprints": ["finding:abc"],
    "source_review_output_sha256": "<sha256-or-null>",
    "source_review_artifact_sha256": "<sha256-or-null>",
    "current_review_output_sha256": "<sha256>",
    "current_review_artifact_sha256": "<sha256>",
    "artifact_valid": true,
    "artifact_error": null
  }
}
```

A new run's explicit `--max-review-fix-cycles 3` prospectively satisfies that
budget blocker even when read-only status was computed under the older cap.
Stable insufficient-budget runs make no session or artifact changes and return
the same projection. When a reopen is attempted, `run --json` additionally
exposes the latest `review_reopen` record; the durable append-only copy is the
`review_repair_reopened` event. `review_cycles`, `review_fix_disposition`, and
the existing review-verdict fields remain available for compatibility.

Commit payloads include `commit_index`, `scope`, `session`, `session_id`,
`artifact_dir`, `stdout_path`, `stderr_path`, `result_path`, `argv`, `cwd`,
`returncode`, `success`, `status_result`, `head_before`, `head_after`,
`commit_sha`, `started_at`, `finished_at`, `duration_seconds`, `stdout_chars`,
and `stderr_chars`. Integer-only session commits also include
`session_number`. `status_result` is `success` only when the command exits zero
and changes `HEAD`; nonzero exits, missing `HEAD`, and unchanged `HEAD` are
recorded as failed commit attempts. A failed commit is also copied into
`pause_details.commit`.

Dry-run `planned` payloads, harness invocation payloads, active turn state,
events, and turn `result.json` artifacts include the same model and effort
metadata. Source values are `flag`, `codex_config`, `copilot_settings`, and
`unresolved`; omitted effort is reported as `null`. The top-level `harness`
field is the startup harness. `turns[*].harness` and `active_turn.harness` are
authoritative for runtime-selected per-turn harnesses after `1`, `2`, or `3`.

`status --json` reports parsed session state, hashes, state paths, and lock
state. Each parsed session includes canonical string `id`; integer-only
sessions also include numeric `number` for compatibility. The top-level
`next_pending_id` is the canonical string ID, while `next_pending` remains a
numeric value for integer-only plans and a string for dotted plans. The
top-level payload also surfaces `active_turn` and `active_verification` (each
`null` when the corresponding phase is not in flight) and a `runner_state`
block with `stale_unfinished` (boolean) and `stale_reason` (string or `null`).
`stale_unfinished` is `true` whenever `state.json` is present without a
`run_finished_at` and no active lock guards it (no lock file, or a stale lock);
`stale_reason` carries a short human-readable explanation. Text output prints
`Run state: stale/orphaned (<reason>)` immediately under the regular
`Run state:` line whenever the runner state is orphaned. When that orphan is
*provably* dead the same call also marks it abandoned (see [Stale unfinished
`state.json` with no lock](#stale-unfinished-statejson-with-no-lock)), adding an
`abandoned` block to `runner_state` and printing
`Run state: abandoned (<detected_at> by <trigger>: <lock reason>)`.
`status --json` error
payloads echo the original argument in `plan_reference`: an uninspectable plan
(or an unresolved label) reports `reason: malformed-plan`, an unreadable state
root reports `reason: state-error`, and a bare label matching more than one
existing plan directory reports `reason: ambiguous-plan` with
`details.state_root` and a sorted `details.candidates` list of canonical paths.
Error payloads omit `plan_dir`, reserving that key for canonical paths in
successful payloads. In text mode an ambiguous label prints a concise stderr
error instructing the operator to pass a full plan directory path. `doctor
--json` reports the resolved harness executable, generated prompt, argv, and
model/effort metadata without invoking the harness. Text `run` and `doctor`
output print the selected model after `Harness:` and print effort only when an
explicit effort was supplied. `unlock --json` reports whether a lock existed,
whether it was removed, whether it was active or stale, and the lock path.

`sessions --json` prints a success payload with `command: "sessions"`,
`state_root` (canonical path), `filters` (an object with boolean `running`
and `recent`), `limit` (a non-negative integer), `running` (array of running
rows), `recent` (array of recent rows, truncated to `limit`), `stale_locks`,
`skipped`, and `counts`. `counts` mirrors the row arrays with integer
`running`, `recent`, `stale_locks`, and `skipped` fields. Running rows include
`kind` (`"turn"` for an active turn, `"verification"` while the runner is
executing a post-turn `### Verify` recipe, `"loop"` when the lock is alive but
no `active_turn` or `active_verification` exists), `plan_dir`, `state_dir`,
`run_id`, `harness`, `session_id`, `turn_index`, `pid`, `started_at`,
`elapsed_seconds`, `idle_seconds`, `last_activity_at`, `lock_active`, and
`lock_stale`. Verification rows also include `check_index`, `check_count`,
`check_name`, and `check_kind`; the text renderer appends a
`check k/n NAME` suffix and uses `null` for `idle_seconds` because verification
has no idle concept. Plain turn rows leave the verification fields `null`. Recent
rows include `plan_dir`, `state_dir`, `run_id`, `harness`, `session_id`,
`turn_index`, `status`, `finished_at`, `duration_seconds`, and
`artifact_dir`. Plan paths in JSON are always full canonical paths; the
text rendering uses basenames by default and switches to full paths when
`--verbose` is passed. A missing state root returns success with empty
arrays. Invalid parsed inputs such as `--limit -1` produce
`reason: input-error`; catastrophic state-root errors produce
`reason: state-error`.

`preflight --json`, `split --check --json`, and a refused `expand-ingest --json`
share one additive `repository_footprint` object. It is present only when the
plan implicates more than one repository or escapes its bound root, and is
omitted entirely otherwise:

```json
{
  "workspace_root": "/Users/dev/1_dev_projects",
  "bound_root": "/Users/dev/1_dev_projects/tundra_svc",
  "registry_present": false,
  "registry_required": true,
  "detected": [
    {
      "alias": "Kiln",
      "path": "tundra_Kiln",
      "origin": "github.com/tundra/Kiln.git",
      "root": "/Users/dev/1_dev_projects/tundra_Kiln",
      "evidence": [
        {"line": 42, "text": "…", "kind": "contract-path", "confidence": "hard"}
      ]
    }
  ],
  "out_of_root": [
    {"line": 42, "text": "…", "kind": "contract-path", "confidence": "hard"}
  ],
  "external": [
    {"line": 51, "text": "…", "kind": "prose", "confidence": "hard"}
  ],
  "unattributed": [{"line": 88, "text": "…", "candidates": ["Kiln", "SVC"]}],
  "suggested_registry_table": ["| Alias | Path | Origin |", "| --- | --- | --- |"],
  "suggested_scopes": {"1": {"reads": ["Kiln"], "writes": ["Kiln"]}}
}
```

`bound_root` is the root the plan was measured against; `workspace_root` is the
common parent the suggested registry is expressed relative to. `detected` rows
are discovered from disk — exact Git worktree roots, `git remote get-url
origin` normalized to a credential-free identity, and directory basenames
validated as aliases with deterministic numeric dedup — so nothing in
`suggested_registry_table` is invented. `kind` is one of `contract-path`,
`contract-cwd`, `handoff`, or `prose`, and `confidence` is `hard` or `soft`;
`soft` targets are advisory and never on their own force `registry_required`.
`out_of_root` lists the targets that escaped `bound_root` while still belonging
to a Git repository — the ones a registry row can describe, and the only ones
that force `registry_required`. `external` lists the escaping targets that
belong to no Git worktree at all; they are reported as evidence and never force
a registry, so a payload exists only when some other target already required
one. `unattributed` lists
path prose that resolved to no single repository and is the only part requiring
a human decision. `suggested_scopes` is keyed by session ID and derived from the
aliases that session's own checks and handoff entries resolve into, with writes
only where the session declares a handoff target.

`metrics --json` prints `command: "metrics"`, the resolved `state_root`, the
window as `since_days`, `window_start`, and `generated_at`, then
`state_dirs_scanned` and `state_dirs_active` beside the `turns`, `first_pass`,
`pauses`, `verification_repair`, `review_cycles`, `agent_mux_delivery`,
`artifacts`, and `compaction` objects. Every field in those objects answers for
the window alone, including `review_cycles.unique_materializations` and its
`review_cycles.repairs_materialized` alias, so narrowing `--since-days` never
raises a count. `state_dirs_scanned` counts the whole corpus and
`artifacts.total_bytes` measures it, because size is not a windowed question;
`state_dirs_active` counts only the directories with in-window turn or event
activity, and a directory whose files were all last written before the window
opened is skipped before its event log is read.

## Pause Semantics

A stop-after-current request is not a pause. When `p` is active at a lifecycle
checkpoint and any further work remains — another pending session, an
unappended `--review-harness` review, or repair children a findings review would
materialize — the run exits successfully with `ok: true`, `paused: false`, and
`reason: stopped`, then releases the plan lock. The request is consumed only at
that moment, so a run that keeps working never records it as honored. If nothing
would follow the just-completed session, the run ends with `reason:
all-complete`. Stop-after-current does not add a pause reason.

An active `waiting_for_reply` mailbox window is also not a pause. The runner
retains its lock, `paused` remains false, status/tail report healthy bounded
activity, and no `run_paused` event or pause metric is created. A received
mailbox reply continues the same session inside the same runner process. While
that runner remains alive, a bounded-wait timeout or unavailable mailbox makes
it clear the additive field before entering the ordinary pause path below. If
the runner exits before it can perform that cleanup, the persisted wait remains
nonterminal; once its plan lock is absent or stale, recovery surfaces classify
it as `expired` even before its unsupervised ceiling and synthesize the
`wait-expired` pause reason without rewriting `state.json`. That lock check is
what makes an abandoned wait recoverable promptly: the ceiling itself rolls
forward with lease activity, so a record left behind by a supervised wait can
carry a projected ceiling well in the future.

A paused run exits nonzero and leaves the plan lock released. Inspect the JSON
payload, `state.json`, `events.jsonl`, and the last turn artifacts, then rerun
the same command after resolving the issue.

Common pause reasons:

- `nonzero-exit`: harness command exited nonzero.
- `quota-exhausted`: the provider rejected the turn for quota and supplied a
  recognized or unrecognized reset hint; ordinary runs stop for operator
  action, while Autopilot may wait only under its explicit bounded quota policy.
- `auth-failed`: harness authentication failed; retrying without repairing
  credentials is not automatic.
- `invalid-model`: the requested harness model was rejected; choose a supported
  model or clear the override before resuming.
- `timeout`: harness exceeded `--timeout`.
- `no-status-change`: the pending session did not become complete.
- `multiple-status-changes`: more than one session changed during one turn.
- `unexpected-status-change`: a different session changed.
- `malformed-plan`: the plan could not be inspected, a final handoff status is
  missing, or derived status ledgers could not be synced.
- `unknown-status`: a handoff status is not `pending` or `complete`.
- `lock-contention`: another active runner holds the plan lock.
- `runner-pause`: the harness output contained a `RUNNER_PAUSE:` or paused
  runner status signal.
- `no-pending-but-incomplete`: no pending session exists, but not all sessions
  are complete.
- `commit-preflight`: auto-commit setup failed before the first harness turn,
  such as a dirty initial worktree, missing `spec`, a non-git working
  directory, or an incompatible `--commit-at-end` and `--max-sessions`
  combination.
- `commit-failed`: a durable commit group could not be reconstructed or a
  repository's `spec commit --yes --no-verify` did not create a clean new
  commit. This includes nonzero exits, zero-exit no-op commits that leave
  `HEAD` unchanged, and a command that changes `HEAD` but leaves tracked or
  untracked Git status behind. The pause details identify the failed alias,
  successful repository SHAs, remaining aliases, and repo-qualified artifacts.
- `harness-unavailable`: a runtime-selected future harness could not be resolved
  before the next turn started. The previous completed turn remains recorded,
  the lock is released, and `pause_details` identifies the session, requested
  harness, configured executable, resolved executable when available, config
  path, and error.
- `not-loop-ready`: the plan's `manifest.md` `**Readiness**` is not
  `loop-ready` after the runner attempted any eligible derived repair, and the
  run did not pass `--allow-not-loop-ready`. The pause payload includes the
  readiness state, preflight finding summary, and `readiness_gate_repair` when
  a repair was attempted. The same reason also covers the non-bypassable case:
  when any red finding is in a non-bypassable category the run pauses even with
  `--allow-not-loop-ready`, and `pause_details.blocker` lists those categories
  (currently `repository-footprint` and `repository-registry-required`).
- `unsafe`, `blocked`, `dependent`, `unresolved-prerequisite`: a
  `RUNNER_PAUSE: <reason> - ...` line from the invoked `session-run` skill
  carried a structured pause reason. The runner preserves that reason instead
  of collapsing the pause to `runner-pause`. `blocked` is the operator-answer
  case: the paused JSON payload also carries an `interactive_help` block whose
  `questions_command`, `reply_command`, and `rendered_prompt` mirror what `asl
  questions` would print, so a wrapping coordinator can route the operator
  conversation without re-reading turn artifacts.
- `empty-reply`, `no-paused-run`, `missing-question-context`,
  `reply-context-mismatch`: `asl run --reply TEXT` rejected the resume. The
  first is a CLI-only validation (empty or whitespace argument) raised before
  the plan lock is acquired. The other three come from
  `qna.load_qna_context` before the runner sends the first turn:
  `no-paused-run` (the last turn was not paused or the plan has never run),
  `missing-question-context` (the paused turn produced no `last-message.txt`
  body to feed the resolver prompt), and `reply-context-mismatch` (the paused
  turn references a session whose current pending status no longer matches the
  plan, typically because another runner already advanced past it).
- `wait-expired`: `asl questions` synthesized reply-oriented recovery for an
  orphaned nonterminal wait whose unsupervised ceiling passed or whose owning
  runner lock is absent or stale. This recovery reason is never written into `state.json`;
  the persisted wait remains available for read-only status and recovery
  projection.
- `verify-failed`: `asl verify` returned non-zero after a completed session.
  The pause payload includes the verification `reason` and per-check results.
- `no-contract`: the completed session lacks a `## Completion Contract` and the
  run was not started with `--allow-not-loop-ready`.
- `malformed-contract`: the completed session's contract failed to parse or
  validate (see `agent_session_loop.contracts` errors).
- `review-harness-required`: the plan contains a pending generated review
  session (a `session-<id>.md` carrying the `<!-- asl:generated-review-session -->`
  marker) but the run was started without `--review-harness`. Rerun with
  `--review-harness codex|claude|copilot`.
- `workspace-write-scope-violation`: a registered implementation turn changed
  a repository outside its exact write aliases, or added/removed a worktree.
  The guard compares against the fresh pre-turn vector, so unchanged dirty
  baseline content is not adopted as a turn mutation. The pause retains all
  content, returns the selected handoff to pending, precedes the turn's own
  timeout/failure/pause and all verification/commit work, and carries a
  `workspace_write_guard` block with alias-qualified paths, topology changes,
  read/write aliases, and the required scope/expansion/preflight repair flags.
- `repository-registry-required`: a legacy run resolved declarations reaching
  more than one Git top-level. Raised by the runner's own repository hard gate,
  either at run entry (after root and workspace resolution, before the lock and
  before any subprocess) or per session task packet (before its turn launches),
  so it fires even when a `loop-ready` plan short-circuits preflight. It is not
  waived by `--allow-not-loop-ready`. `pause_details.repository_intent` is the
  bounded, credential-redacted intent report; its `git_roots` names the
  conflicting repositories. The fix is to declare the registry and per-session
  scopes, never to widen `--cwd` to their common parent.
- `workspace-write-guard-unavailable`: the runner could not inventory the
  workspace or capture complete Git evidence before or after a registered
  implementation turn. A before-turn failure spends no harness turn. An
  after-turn failure retains content, returns the handoff to pending, and
  outranks every harness-result branch because the runner cannot prove scope.
  An operator may prepare an explicit persisted inventory exclusion only when
  the failing subtree is outside every registered repository; all other
  evidence failures stay fail-closed.
- `review-mutated-worktree`: the review harness changed repo content outside
  the plan directory. The pre/post fingerprint covers the working tree's
  `HEAD`, tracked diff, the separate `--cached` (index) diff — so staging a
  mutation and then restoring the working file to its `HEAD` bytes still trips
  the guard — (with the plan directory and any
  `--review-guard-exclude` paths excluded from both diffs), and untracked nodes
  outside those
  paths (file bytes, or a symlink's kind and link text — never the link
  target's bytes), gitignored ones excluded unless
  `--review-guard-include-ignored` is passed — except for the untracked
  `.gitignore` files git can apply, which are fingerprinted under either mode
  even when their own rules hide them. `.git/info/exclude`, the
  `core.excludesFile` setting, and the file it selects are fingerprinted the
  same way under either mode, and neither the plan directory nor
  `--review-guard-exclude` removes them. Any change trips the guard. Every
  probe is rooted at the repository root, so a nested `--cwd` narrows nothing. The pause message names up to three
  changed paths, and the pause details carry `changed_paths` (`tracked`,
  `untracked_added`, `untracked_removed`, `untracked_changed`,
  `worktree_ignore`, `ignore_config`, `head_changed`)
  plus the `excluded` allowlist and the `include_ignored` mode beside the raw
  fingerprints. The implementation
  turns that ran before the review are preserved. This pause outranks the
  review turn's own failure: a mutating review that also timed out, exited
  nonzero, was interrupted, emitted `RUNNER_PAUSE`, or corrupted the plan
  reports `review-mutated-worktree` (never retryable) instead of the reason it
  would otherwise have paused with.
- `review-guard-unavailable`: a git probe the mutation guard depends on failed
  (git itself unrunnable, or a nonzero `git diff`, `git diff --name-only`,
  either of their `--cached` counterparts, `git ls-files --others`, or
  `git ls-files -v`), or a path git reported could not be
  fingerprinted (`untracked-path-unreadable`, `untracked-link-unreadable`,
  `untracked-node-unsupported`, and the `index-flagged-` counterparts of all
  three for an `assume-unchanged` or `skip-worktree` entry), or a path git named
  as changed but rendered no patch for (`tracked-patch-missing`,
  `index-patch-missing`), so the guard cannot
  prove the review turn left the worktree alone. Failing probes and unreadable paths are never turned into
  empty evidence — dropping a path would hide every later change to it. The
  pause details carry a `review_mutation_guard` block with `phase` (`before` or
  `after` the turn) and `unavailable` (`reason`, `argv`, the offending `path`
  for a per-path failure, and the git `returncode`/`stderr` when there is one). A failure on the post-turn capture
  reverts the review session to pending so a resume re-runs it under a working
  guard.
- `interrupted`: the run was interrupted by Ctrl+C.

## Autopilot Skill (`$asl`)

The shared `asl` skill (`skills/asl/`, invoked as `$asl` under Codex or `/asl`
under Claude/Copilot) supervises a single `asl run` invocation from start to
finish without a human babysitting each turn. The operator hands the skill the
exact `asl run ...` command they would otherwise type; the bundled helper
(`skills/asl/scripts/asl-autopilot.py`) starts or attaches to the run, polls
`asl status --json`, streams heartbeats to stderr, auto-retries recoverable
pauses, and stops only when a run needs a human answer or has finished. Unless
the command carries `--no-mailbox`, it also establishes a supervisor lease
before starting a child, discovers live worker questions, and keeps supervising
while the runner waits for a reply.

### Install and sync

The skill ships in the repository's top-level `skills/asl/` and is stowed into
every harness skill directory the same way as the other shared skills. Run
`just sync-skills` (or `just all`) to refresh the harness package links, then
`just verify-asl-install` to confirm the active `asl` wrapper and the installed
skill links resolve to this checkout. See [Shared AI Skills](../README.md#shared-ai-skills).

### Safety model

The helper is deterministic supervision only; all inferential reasoning stays
with the harness agent driving the skill. It:

- Parses the command itself and refuses shell operators, pipes, redirects,
  `--dry-run`, and user-supplied `--reply` / `--reply-stdin`. It never runs the
  command through a shell — argv is built directly, tilde paths are expanded,
  and `--json` is guaranteed.
- Leaves ASL authoritative for implementation, verification, session status,
  progress journals, and review session execution. The skill does not patch
  repository code directly.
- Prefers the complete ordered workspace projection from `asl status --json`.
  When a registered command omits `--cwd`, the child receives the bound
  workspace root. A conflicting `--cwd` stops as `user_required` with atomic
  rebind guidance; Autopilot never grants itself rebind authority.
- Never persists reply text: resumes flow through `asl run --reply-stdin`, so
  the reply appears only on the child's stdin, never in argv or helper output.
- Uses the existing child/attach wait loops for mailbox polling and lease
  renewal; it does not create a mailbox watcher thread. Full question bodies
  are retained only in the structured result's `outbox`, while stderr receives
  a bounded, single-line, JSON-escaped `QUESTION id=... preview=...` record.

### Attach behavior

Each supervision cycle reads `asl status --json` first. A stale lock is treated
as safe to start over (ASL clears stale locks before acquiring). An active lock
for the same plan and state means **attach**: the helper polls the foreign run
to completion instead of starting a second one. An active lock for a different
plan directory, or an observably different harness than the operator requested,
stops with `user_required` for a human to resolve. `--max-sessions N` is a
global session budget: the supervisor stops cleanly with `max-sessions` once `N`
implementation sessions have completed across the run (there is no per-chunk
restarting — an uncapped run drives every pending session in one process).
Self-relaunches (pause retries and runner-owned review continuations) and attach
polls share one bounded budget so a wedged plan can never loop forever.

Mailbox supervision uses one status-derived watcher across attach polls and
later self-launches. It validates that the advertised mailbox and lease files
belong to the advertised per-plan state directory, starts from the physical
`last_seq`, polls questions and notices every five seconds, and renews its lease
every 30 seconds. Advancing through every physical line prevents cursor replay
after guidance, acknowledgements, filtered records, or malformed records.
Questions and notices use independent accumulators and identity deduplication:
notices from the exact live run are emitted as bounded escaped `NOTICE`
progress and replayed at least once on attach within each supervisor process,
even when the status-derived cursor is already past them. The final structured
outbox declares `include: all` and preserves complete separate `questions` and
`notices` arrays; notices never enter unanswered-question projections or lease
counts. When supervision returns, it stops future watcher activity and lets the
last fixed-size lease expire.
`--no-mailbox` skips watcher construction and lease writes in both launch and
attach modes.

### Reply and resolution behavior

While the helper is running, a `QUESTION id=...` progress record is
nonterminal. Answer it from a separate shell with:

```text
asl inbox reply <plan-dir> --to <question-id> --text '<answer>' [--state-dir DIR] --json
```

Use `--text-stdin` when the answer should not appear in argv. The first
concurrent reply or resolution wins. Use `asl inbox resolve ... --action
continue` only for a plainly nonblocking message that needs no factual answer;
use `--action pause` when the live wait should become ordinary paused-run
recovery. A reply and continue resolution are each consumed once and continue
the same session through their separately typed prompts; a pause resolution
ends the live wait without fabricating an answer. The helper keeps complete
question and notice records in the separate arrays of its eventual `outbox`
payload and treats `waiting_for_reply` as healthy bounded activity, including
the pending-outbox count, rather than a stalled or paused run.

If nobody answers before the runner's five-minute, lease-extendable,
30-minute-absolute wait expires—or mailbox I/O/fullness forces fallback—the
runner records its ordinary terminal pause. The helper then uses the existing
pause behavior below; no separate mailbox-only terminal status is introduced.

The helper auto-retries recoverable same-session pauses (`verify-failed`,
`transport-failed`, `timeout`, `no-status-change`) up to twice per session and
reason before escalating. Only `transport-failed` backs off before relaunching:
5 seconds, then 30 seconds; the other recoverable pauses retry immediately.
An unclassified `nonzero-exit` pause is never retried. When a pause genuinely
needs an answer, the result carries `needs_agent_reply` with a `questions`
block (resolver prompt, the paused agent's last message, pause metadata,
artifact paths). The driving agent either infers a clearly defensible reply
from the plan/repo/artifacts or relays the question to the operator, then
resumes by streaming the reply on stdin:

```text
printf '%s' '<reply text>' | skills/asl/scripts/asl-autopilot.py --command '<same exact command>' --json --reply-stdin
```

For live steering that should affect a future turn, use a separate shell while
the helper owns the foreground:

```text
asl inbox guidance <plan-dir> --text '<scoped guidance>' [--state-dir DIR] --json
asl control set-model <plan-dir> [--harness codex|claude|copilot] --harness-model MODEL [--effort EFFORT] [--state-dir DIR] --json
```

Guidance is delivered at the next turn start and remains pending until the
worker acknowledges it. Model/effort controls are polled during active turns
but affect only the next implementation invocation in the current runner
process; they do not alter the active subprocess or generated review turns.

### Review-fix loop behavior

When the supervised command used `--review-harness`, a clean completion still
carries the final-review verdict. An all-clear review returns `complete` with a
`review` block whose `outcome` is `all-clear`. A review that reports findings
returns `review_findings` with the `final-review.md` path, status line,
severity counts, a bounded summary excerpt, and the freshest `review_resume`
guidance. Repair creation, child execution, and rereview are runner-owned on
the same plan. The supervisor attaches to an active matching lock before
considering zero-pending completion, and when unlocked it relaunches only for
`initial-review`, `materialize-repairs`, or `rereview` actions permitted by the
command's review harness. Only `materialize-repairs` is gated by the effective
absolute cap — 4 unique source-review materializations unless the supervised
command passed `--max-review-fix-cycles N`; eligible `rereview` launches are
cap-free because they consume no materialization budget. A corrected malformed artifact or a higher cap can
therefore resume the same command later without a second plan. Insufficient
caps and terminal all-clear/repeated/unstructured findings do not relaunch, so
every supervision loop has a deterministic stop. The skill never authors,
splits, or expands a separate repair plan.

### Final helper statuses

The helper emits a single JSON result on stdout whose `reason` field is the
terminal verdict (`ok` is `true` only for a clean completion):

- `complete` (`ok: true`): every session finished. With `--review-harness`, an
  all-clear review is attached under `review.outcome: all-clear`.
- `max-sessions` (`ok: true`): the `--max-sessions N` global budget was reached
  with sessions still pending — an intentional clean early stop, not a pause or a
  failure. `completed` and `pending` counts are attached; re-running the same
  command continues from the remaining sessions.
- `stopped` (`ok: true`): a stop-after-current request (the `p` key or
  `asl control stop-after-current`) was honored with work still to do — the
  structural twin of `max-sessions`, not a pause or a failure. `completed` and
  `pending` counts are attached and the helper does not relaunch; re-running the
  same command continues from the remaining sessions, or runs the pending
  review when every session is already complete (`review_skipped_reason:
  stopped`).
- `needs_agent_reply` (`ok: false`): the run paused on a decision the helper
  could not auto-resolve; a `questions` block carries the resolver context and
  the driving agent must supply a reply.
- `review_findings` (`ok: false`): the run completed but the final review
  reported findings and no automatic continuation supported by the current
  command remains; `review` carries the verdict and `review_resume` explains a
  higher-cap, artifact-correction, or terminal circuit-breaker result.
- `user_required` (`ok: false`): an active `asl run` lock holds this plan/state
  for a different run; a human must resolve the conflict before supervising.
- `blocked` (`ok: false`): a chunk stopped without completing and without a
  recoverable pause (the child's non-paused failure reason is attached).
- `capped` (`ok: false`): a safety budget was exhausted before the plan settled
  — `max_chunks` self-launched pause-retry/review relaunches, or
  `attach_max_seconds` (default 12h) of wall clock within a single attach
  episode. Both are kwargs-only, with no CLI flag.
- `status-error` / `run-error` (`ok: false`): `asl status --json` or `asl run`
  itself failed to produce a usable payload.
- `empty-reply` (`ok: false`): `--reply-stdin` was requested but stdin carried
  no non-whitespace reply text.
- A parse-stage failure (`ok: false`, `stage: parse`) with a specific reject
  reason when the requested command is unsupported or malformed.

## Troubleshooting

Check what the runner sees:

```bash
asl status ~/.local/share/agent-plans/example-plan/ --json
```

Preview the next command without invoking the harness:

```bash
asl run ~/.local/share/agent-plans/example-plan/ --harness codex --harness-model gpt-5 --review-harness claude --review-harness-model claude-opus-4 --dry-run --effort high
```

Inspect the most recent artifacts named in `state.json` or the JSON output:

```bash
jq '.turns[-1] | {stdout_path, stderr_path, last_message_path, result_path}' ~/.local/state/agent-session-loop/<plan-path-hash>/state.json
jq -r '.turns[-1].artifact_dir' ~/.local/state/agent-session-loop/<plan-path-hash>/state.json
jq -r '.turns[-1].result_path' ~/.local/state/agent-session-loop/<plan-path-hash>/state.json | xargs jq '{session, session_id, returncode, status_changes, runner_pause, plan_error}'
```

Follow the active or latest run:

```bash
asl tail ~/.local/share/agent-plans/example-plan/
asl tail ~/.local/share/agent-plans/example-plan/ --lines 80 --no-follow
```

For text runs started from a real terminal, press `h` for the current keyboard
controls, press `s` for a compact status snapshot, press `t` to toggle inline
tailing, press `1`/`2`/`3` to select the future harness, and press `p` to
request or cancel stop-after-current. Use `tail` from another terminal to
inspect durable `run_control` events.

Remove a stale lock:

```bash
asl unlock ~/.local/share/agent-plans/example-plan/ --json
```

Remove an active lock only after verifying the owning process is not a real
runner you still need:

```bash
asl unlock ~/.local/share/agent-plans/example-plan/ --force --json
```

A lock whose recorded `hostname` is not this host reports
`lock was recorded on host <name>` and is the one stale lock `asl run` will not
take over: the local process table holds no entry for a remote pid, so liveness
there is unknowable rather than dead. Acquisition fails with a lock-contention
error naming that host; clear it with `asl unlock` (no `--force` — the lock is
stale) once you have confirmed the other runner is gone. A dead pid recorded on
*this* host needs no command at all: the next read of that lock reclaims it (see
[Stale unfinished `state.json` with no
lock](#stale-unfinished-statejson-with-no-lock)).

If a plan has no pending sessions but is not complete, inspect every final
`session-<id>.md` handoff and normalize statuses to exactly `pending` or
`complete`. Ignore older status templates preserved above the final handoff.

### Five-second plan transaction lock timeout

Atomic plan publication and recovery are serialized by a marker-scoped plan
transaction lock. Plan reads wait up to five seconds for the publication to
finish and then fail instead of hanging indefinitely. This timeout can surface
from plan reads behind `asl status`, `asl tail`, `asl prepare`, and the
Workbench API.

The diagnostic names the lock path and the pid, hostname, and thread of the
process that held the lock when it was acquired. Those holder fields identify
the publishing process and its host and thread for investigation; the kernel
lock remains authoritative. Retry the operation once if plan publication was
still in progress. If the timeout repeats, inspect the named holder before
retrying or restarting that process. Do not delete the transaction marker or
confuse this publication lock with the separately managed runner lock.

### Long verification after `session N/N finished`

The runner emits `session N/N finished` as soon as a harness turn writes a
`- **Status**: complete` handoff and the per-turn turn artifacts are flushed.
The independent post-turn verification gate then runs the session's
`## Completion Contract` `### Verify` recipe, which can take longer than the
harness turn itself (full unit suites, integration commands, large
`regex_in_file` scans, etc.). During that window the runner is **not** idle:
`state.json.active_verification` is populated with the in-flight check, and
`events.jsonl` carries `verification_started`, `verification_check_started`,
`verification_heartbeat` (every 30 seconds), `verification_check_finished`, and
finally `verification_finished` records. `asl status`, `asl tail`, and
`asl sessions --running` all surface this phase — `asl sessions --running` reports
the row with `kind="verification"` and a `check k/n NAME` suffix, and `asl tail`
prints the same heartbeats inline. Independent verification remains mandatory:
the harness handoff is only the implementation claim, the verify recipe is the
durable gate. Per-check stdout, stderr, and `failure_reason` enums remain
captured in the turn `result.json` under the `verification` key and in any
`asl verify --json` invocation.

### Stale unfinished `state.json` with no lock

When a runner process is killed (SIGKILL, host reboot, container OOM) without
running its cleanup path, `state.json` can be left without
`run_finished_at` while no `lock.json` is held. `asl status` flags this in two
places:

```bash
asl status ~/.local/share/agent-plans/example-plan/ --json \
  | jq '.runner_state | {stale_unfinished, stale_reason, run_id, run_started_at}'
```

Text output prints a second `Run state: stale/orphaned (<reason>)` line under
the regular `Run state:` line. The orphan is informational: durable session
completion still lives in the `## Handoff` blocks, and the next `asl run` will
acquire a fresh lock, recompute readiness, and resume normally. If the orphan
appears alongside a `Lock: stale (...)` entry, clear the lock with
`asl unlock ... --force --json` only after confirming no other runner is
genuinely active.

Left alone, such a run never gets a terminal record, so `asl tail --follow` and
anything else waiting on `run_finished` waits forever. Both `asl status` and a
stale-lock takeover inside `asl run` therefore reconstruct that ending once
death is *proven*: the lock's pid is gone and its recorded `hostname` matches
this host (or predates the field), or no lock has existed for the 120s
`ABANDONED_NO_LOCK_GRACE_SECONDS` window past `run_started_at`. Marking rewrites
`state.json` with `ok: false`, `reason: abandoned`, a `run_finished_at`, null
`active_turn`/`active_verification`, and an `abandoned` block (`detected_at`,
`trigger`, `lock_reason`, `detected_by_pid`), then appends one synthetic
`run_finished` event carrying `synthetic: true` and `outcome: abandoned`. The
recheck and both writes run under an exclusive `flock` on an `abandon.lock`
sidecar in the plan's state directory, so concurrent `asl status` probes and a
takeover racing them leave exactly one terminal record: the caller that cannot
take that claim declines rather than waiting. It is idempotent, and never fires
on an active lock, a lock recorded on another host,
a fresh no-lock start, or a stale lock with no usable pid. Taking a stale lock
over is not itself proof: the takeover path proves the *previous* owner's death
from the lock it removed, so reclaiming an unparseable or pid-less lock clears
the lock and leaves `state.json` and `events.jsonl` untouched. Any
`waiting_for_reply` record survives as evidence of what the run died holding.

Reading such a lock also removes it, so a dead runner stops blocking the plan
without an operator. The one case that qualifies is a pid missing from the local
process table on a lock whose recorded `hostname` is this host; the removal is
reported once on stderr as `reclaimed stale lock at <path>`. The call that
reclaims still reports the lock it found, so that `asl status` prints
`Lock: stale (pid N is not running)` and the next one finds no lock at all.
Nothing else is reclaimed by a read: a lock recorded on another host, a legacy
lock with no `hostname` key (its pid names no process table), an unparseable
lock, and any live pid all survive. `asl gc` is the one command exempt from all
of this: it has promised to write nothing in dry-run and to touch only run
directories under `--apply`, so it inspects locks without reclaiming them in
either mode. `asl unlock` remains the only route out for
a cross-host lock; the rest are taken over by the next `asl run`. Every
mutation of `lock.json` — a read-side reclaim, a stale takeover, publishing a
new lock, `asl unlock`, and the release at the end of a run — runs under an
exclusive `flock` on a `lock.mutex` sidecar in the plan's state directory. The
lock file cannot order its own replacement because it is created and unlinked,
so two callers holding that path can be looking at different files; the sidecar
is never removed and is therefore a stable identity to queue behind. The proof
that a lock is dead is re-established under that claim and the file is unlinked
before it is released, so a lock a new runner published between the proof and
the removal is left to its new owner rather than deleted. No mutator ever
unlinks without holding the claim, and each degrades in its own way when the
wait expires: a reclaim skips, leaving the lock for the next reader; acquiring
and `asl unlock` fail loudly; and the release at the end of a run leaves its
lock in place and reports that on stderr, because the file sitting there after
a contended wait may already be a replacement owner's live lock. That abandoned
lock names a pid on this host, so it is reclaimed by the next read once the
process exits.

### Missing progress journal entry for the final session after a killed runner

The runner appends to `progress.md` (the runner-managed cross-session digest)
**after** the post-turn verification gate succeeds. If a runner is killed
between writing the `- **Status**: complete` handoff and finishing
verification, the session's handoff is durable but the journal entry will be
missing. The fix is to rerun the loop — verification reruns, and the
post-verification append is idempotent against `(session_id, completed_at)`.
If the operator already finished the run by hand and only wants to backfill
the journal, regenerate it from the handoffs:

```bash
asl backfill-progress ~/.local/share/agent-plans/example-plan/
asl backfill-progress --force ~/.local/share/agent-plans/example-plan/
```

`backfill-progress` reconstructs the journal from completed handoffs; `--force`
replaces an existing journal atomically. Never edit `progress.md` by hand from
a session — the schema-versioned `<!-- agent-session-loop progress-journal v1 -->`
header is what the runner uses to refuse stale or malformed journals.

To inspect the journal without opening the file, use
`asl progress PLAN_DIR [--tail] [--json]`: it prints the runner-managed
journal, follows it live with `--tail`, and emits parsed entries with
`--json`.

### Interactive Q&A resume

When a harness pauses with `RUNNER_PAUSE: blocked - ...` because it is waiting
on operator answers, the runner records the structured pause and surfaces an
`interactive_help` block. The recommended workflow is:

```bash
# 1. Run pauses with reason: blocked and prints interactive_help.
asl run ~/.local/share/agent-plans/example-plan/ --harness codex --json

# 2. Render the resolver prompt for a human or a wrapping agent.
asl questions ~/.local/share/agent-plans/example-plan/

# 3. After collecting answers, resume the same paused session with the
#    operator's verbatim reply. The reply text is never persisted.
asl run ~/.local/share/agent-plans/example-plan/ --harness codex --reply "1a, 2b, 3a"
```

The same `interactive_help.questions_command` and `interactive_help.reply_command`
strings are the canonical paste-ready invocations a coordinator should use:
they preserve the active `--state-dir` so the resume targets exactly the
paused turn. Use `asl run --dry-run --reply TEXT` to inspect the resolver
prompt and the in-harness reply prompt side-by-side without invoking the
harness, consuming the paused state, or writing the preview payload to
`state.json`/`events.jsonl`.

## Workbench

`asl workbench [serve]` runs a stdlib-only HTTP control plane that
surfaces every plan ASL can see in one local browser UI. It is
intentionally loopback, single-user software: there is no remote-access
mode, no multi-tenant auth, and no scheduling logic beyond a thin
renderer over the runner, registry, cleanup/archive storage, control
queue, launch, and unlock helpers.

```bash
asl workbench
asl workbench serve
asl workbench serve --port 0 --open
asl workbench --plan-root ~/work/plans --plan-root ~/.local/share/agent-plans
asl workbench serve --state-dir /tmp/asl-state --json
```

The bare `asl workbench` invocation defaults to `serve`; the same
flags (`--host`, `--port`, `--state-dir`, `--plan-root`, `--open`,
`--json`) are accepted on both the parent and the subcommand so either
position works.

The server binds `127.0.0.1:8765` by default. `--port 0` selects an
ephemeral port (useful for tests or for running multiple workbenches side
by side). `--state-dir DIR` mirrors the same flag on `asl run`; `--plan-root
DIR` may be passed multiple times to add extra directories to plan
discovery. `--open` best-effort launches the default browser at the
tokenized URL; the URL and token are also printed regardless, so the
operator can paste them by hand if the browser cannot be opened. `--json`
prints a startup payload (`reason: serving`, `host`, `port`, `url`,
`token`) instead of human text and the server still blocks until exit.

CLI registry maintenance commands let operators manage pins without
opening the UI:

```bash
asl workbench pin /path/to/plan-dir
asl workbench pin /path/to/plan-dir --cwd /path/to/repo --label "Refactor"
asl workbench unpin /path/to/plan-dir
asl workbench registry --json
```

`pin` upserts a registry entry for the canonical `plan_dir`; `--cwd`
sets the optional legacy repository root and `--label` sets the optional
human-readable name. Registered pins additionally project their prepared
`workspace_root`, registry digest, and complete ordered repositories. Their
launches always use that binding; a conflicting saved root is rejected with
`asl prepare PLAN_DIR --resume --cwd NEW_WORKSPACE --rebind-workspace`
guidance. Other launch-profile fields stay `null` (runner
default) unless edited through the UI. `unpin` removes the matching
pin. `registry` prints the loaded registry (pins, `plan_roots`, and
any loader warnings); pass `--json` for the
`responses.success_payload`-shaped envelope used elsewhere in the CLI.

Queue/detail summaries, launch results, state, Recovery Center, and final-review
responses retain `cwd` for compatibility and add `workspace_root`,
`registry_digest`, and ordered `repositories`. Recovery evidence identifies
repository-qualified failed checks, retained scope violations, unavailable Git
evidence, unresolved commit groups, and partial per-repository commit SHAs.
`GET /api/final-review` also returns the bounded Markdown view plus parsed
`final-review.json` when available, preserving finding repository attribution
and repair-group scopes. Browser and terminal summaries show a bounded prefix
for very large registries; machine-readable vectors remain complete. The
browser's `formatRepositoryAliases` formatter is the authoritative source for
its 12-repository preview limit.

The console output for a normal start is:

```text
Workbench serving on http://127.0.0.1:8765
Authorization token: <opaque secrets.token_urlsafe(32) value>
```

Open `http://127.0.0.1:8765/?token=<token>` (or use `--open`). The
frontend strips `?token=...` from the URL after first load and persists
it in `sessionStorage`; refreshing the tab keeps the token. The token is
regenerated on every server start, so existing tabs need a fresh URL
after a restart.

### Security model

- The default bind is `127.0.0.1`; the server never reaches outside the
  local host without an SSH tunnel or other operator-initiated forward.
- Every mutating endpoint (`POST /api/registry/pin`,
  `POST /api/registry/unpin`, `POST /api/plan/archive`,
  `POST /api/plans/archive`, `POST /api/plan/delete`,
  `POST /api/archive/restore`, `POST /api/archive/delete`,
  `POST /api/archive/settings`, `POST /api/launch`,
  `POST /api/reply`, `POST /api/control/...`, `POST /api/unlock`)
  requires
  an `Authorization: Bearer <token>` header. The token is generated with
  `secrets.token_urlsafe(32)` at startup and compared with
  `secrets.compare_digest`.
- Read endpoints (`GET /api/queue`, `/api/plan`, `/api/recovery`,
  `/api/state`,
  `/api/events`, `/api/logs`, `/api/last-message`, `/api/result`,
  `/api/verification`, `/api/commits`, `/api/progress`,
  `/api/final-review`, `/api/questions`, `/api/registry`,
  `/api/archive`, `/api/artifacts`) are **not** authenticated. They are
  loopback-only by construction. If
  the Workbench is ever exposed beyond the local machine (e.g., SSH port
  forwarding, a reverse proxy), plan inventory and resolver prompts —
  including paused-Q&A `last-message` text — will leak. Treat the URL the
  same way as any other unauthenticated local devtool.
- Reply text only ever travels through the child process stdin via
  `asl run --reply-stdin`. `POST /api/reply` accepts the operator answer
  in the JSON body and forwards it to `workbench.resume_plan_with_reply`,
  which writes it to the child stdin and discards the byte buffer. The
  reply text is never persisted in `state.json`, `events.jsonl`, argv,
  or turn artifacts; only the character count surfaces (as
  `reply_length`) on the launch result and `REPLY_PROVIDED_EVENT`.
- Stop controls are checkpoint-only and never terminate an active
  harness subprocess. They are equivalent to typing `p`, `1`/`2`/`3`,
  etc. at a text TTY runner.
- The Recovery Center is a read-only diagnosis layer. It may display
  copyable repair commands, but the Workbench never executes those
  commands. Action cards only route to existing affordances and
  endpoints (`/api/reply`, `/api/launch`, `/api/unlock`, or local
  detail-panel focus targets).
- `--unsafe-bypass` is still the runner default. The Workbench launch
  form surfaces the choice and the UI confirms the launch before
  spawning a child with bypass mode set, but flipping the toggle does
  not change global runner defaults.

### Registry, pins, launch profiles

The Workbench registry is a JSON file stored at
`$XDG_CONFIG_HOME/agent-session-loop/workbench.json` (default
`~/.config/agent-session-loop/workbench.json`). Schema version `2`. The
file is hand-editable; malformed pin or root entries become recoverable
warnings on the loaded registry rather than failing the server.

Pins record:

- `plan_dir` — canonical absolute path (deduplicated).
- `label` — optional human-readable name shown in the UI.
- `profile` — optional `LaunchProfile` defaulting all fields to `null`.
  Recognized fields mirror `asl run` flags: `cwd`, `harness`,
  `harness_model`, `effort`, `timeout`, `commit_mode` (`per-session` or
  `end`), `review_harness`, `review_harness_model`, `unsafe_bypass`,
  `allow_not_loop_ready`, and `note`. `null` means "no preference"; the
  workbench falls back to the runner default. `review_harness` must differ
  from `harness`, and `review_harness_model` requires `review_harness`.

Schema-v1 registries remain readable, but a legacy `profile.model` is ignored
and surfaced as a recoverable warning; it is never forwarded to `asl run`.
The next pin/profile save writes schema v2 with `harness_model` and
`review_harness_model` only, permanently dropping the legacy shared field.

  The nullable booleans (`unsafe_bypass`, `allow_not_loop_ready`) are
  surfaced in the UI as tri-state `<select>` controls with literal
  `(default)`, `enabled`, and `disabled` options that round-trip
  `null`/`true`/`false` through the registry without coercion. The
  blank/default state emits **no** `--unsafe-bypass` and **no**
  `--no-unsafe-bypass` argv — it leaves the runner default
  (`--unsafe-bypass` today) in place. `enabled` adds
  `--unsafe-bypass` / `--allow-not-loop-ready`; `disabled` adds
  `--no-unsafe-bypass` (`allow_not_loop_ready=false` intentionally has
  no negative flag because the runner has no opposite of
  `--allow-not-loop-ready`).

Concurrent pin/unpin writes (e.g., two browser tabs) are serialized
through an advisory `fcntl.flock` on a sidecar file
`workbench.json.lock` next to the registry. The lock file is created
lazily on first write and inherits the same per-user permissions as the
registry. The atomic write itself uses `tempfile.mkstemp` plus
`os.replace`.

`plan_roots` is a list of extra discovery directories the operator
wants surfaced. They are merged with any `--plan-root` CLI overrides
and the well-known defaults. Registry roots are scanned alongside
CLI-supplied roots and both flow through `/api/queue` under the
canonical `plan-root` discovery source label, so the queue surfaces
plans from registry `plan_roots` even when the operator did not pass
`--plan-root` on the command line.

The registry also stores `archive_retention_days`, default 30 days.
Invalid or missing values load as the default with a recoverable warning,
and `POST /api/archive/settings` is the supported way for the UI to
update the retention window.

### Plan discovery

Discovery sources, in stable canonical order, are:

- `state` — every per-plan directory under the resolved ASL state root
  (e.g., `~/.local/state/agent-session-loop/<plan-hash>/`).
- `registry` — `plan_dir` for every pin in `workbench.json`.
- `plan-root` — the union of registry `plan_roots`, CLI
  `--plan-root DIR` values, and `$AGENT_PLANS_DIR` when set.
- `agent-plans-dir` — `$AGENT_PLANS_DIR` itself when present.
- `tmp-default` — `$TMPDIR/agent-plans` if it exists.
- `share-default` — `$XDG_DATA_HOME/agent-plans` (or
  `~/.local/share/agent-plans`) if it exists.

A plan-root directory is scanned for immediate children that contain
both `manifest.md` and `context.md`; nothing deeper is walked. Each
discovered plan dedup-merges across sources and preserves the full list
of sources that surfaced it.

Archived entries live under `<state-root>/archive/<archive_id>/`.
Archive storage is outside normal plan-root discovery, so archived plans
do not reappear in the queue until they are restored.

### Cleanup and archive workflow

Cleanup mode is a queue review mode for old, inactive work. It switches
the queue to `sort=oldest`, which ages complete plans off their
`completed_at` timestamp and every other plan off `last_activity_at`,
treating a missing timestamp as oldest. Cleanup mode reveals completed
plans on its own gate, so the operator can pick the `complete` filter
chip and review finished work in place without flipping "show
completed". The UI threshold control drives two bulk actions:
`select candidates` adds only the visible rows older than the chosen
number of days, while `select visible` adds every visible archivable row
regardless of age. Both skip rows with an active lock. Queue payloads
include `archivable` and `archive_blocker`; active runner locks are not
archivable and surface `archive_blocker: "active-lock"`.

Archiving moves the plan directory and the matching ASL state directory
together into the Workbench state root, along with available pin/profile
metadata and a summary snapshot. The detail action uses
`POST /api/plan/archive`; `POST /api/plan/delete` remains as a
backward-compatible archive alias for older clients. Both single-plan
routes require the same typed
full-path confirmation through `"confirm_plan_dir"`, refuse arbitrary
non-plan paths, and return `409` with `reason: "active-lock"` when an
active lock is present.

Bulk cleanup uses `POST /api/plans/archive` with `plan_dirs`. The server
archives each entry independently and returns per-entry results plus
`archived_count` and `failed_count`, so one blocked or invalid plan does
not prevent the rest from being archived.

The Archive view reads `GET /api/archive`, which returns archived
entries, `archive_retention_days`, each entry's `expires_at`, and recent
auto-empty results under `archive_maintenance`. `POST
/api/archive/restore` restores the archived plan and state back to their
original paths and restores pin/profile metadata when present. Restore
fails with `409` and `reason: "restore-conflict"` if either destination
that would be written already exists.

The Archive view exposes a bulk-selection bar (`#wb-archive-bulk`) with
`select all`, `restore selected`, `purge selected`, and `clear selection`
controls. `select all` adds every loaded archive entry to the selection; the
count reads `N of M entries selected` and `select all` disables once every
loaded entry is already selected. `restore selected` runs a best-effort bulk
restore: it copies the selected IDs, asks a plain `window.confirm` (not the
typed-count prompt reserved for permanent purge), calls the single-entry `POST
/api/archive/restore` endpoint once per selected ID, continues past per-entry
failures, drops successfully restored IDs from the selection, and reports
`restored X` or `restored X, Y failed` on partial failure. `purge selected`
reuses the typed `confirm_count` purge flow below, and `clear selection` empties
the current selection without restoring or purging any entry.

Permanent purge only applies to archive entries, not live plans. `POST
/api/archive/delete` accepts one or more `archive_ids` and requires a
typed `confirm_count` equal to the selected count; a mismatch returns
`reason: "confirmation-mismatch"` and leaves every entry in place. Archive
maintenance auto-empties entries whose age is at least
`archive_retention_days`; it runs during queue and archive polls, so no
separate daemon is required.

### HTTP API surface

Read endpoints (loopback-open, no token required):

- `GET /` — bundled Workbench HTML shell (`Cache-Control: no-store`).
- `GET /static/<path>` — bundled JS/CSS assets
  (`Cache-Control: no-cache, must-revalidate` plus strong-validator
  `ETag` from `st_mtime_ns` + `st_size`; the server replies `304 Not
  Modified` to a matching `If-None-Match`).
- `GET /api/health` — liveness probe.
- `GET /api/queue` — every discovered plan summarized via
  `WorkbenchSummary` (counts, primary state, recommended action,
  warnings, last-message preview, artifact paths, compact Recovery
  Center summary) plus the resolved `state_root` and
  `state_root_exists` fields used for that scan. Each plan summary also
  carries the additive triage fields documented under
  [Triage queue payload](#triage-queue-payload) so the frontend can
  sort, group, and search without re-deriving status from raw counts.
  `GET /api/queue?sort=oldest` sorts by oldest activity first: complete
  plans sort on `completed_at` and every other plan on
  `last_activity_at`, and a missing timestamp is treated as oldest.
  Queue responses also include
  `archive_retention_days`, `archive_maintenance`, and per-plan
  `archivable` / `archive_blocker` fields used by Cleanup mode.
- `GET /api/plan?plan_dir=<path>` — single-plan detail. The payload
  carries the same additive triage fields as `/api/queue` (`urgency_rank`,
  `urgency_group`, `activity_label`, `concise_reason`, `last_activity_at`,
  `last_activity_source`, `searchable_text`) plus the same compact
  `recovery` summary used by queue rows and detail-panel fallbacks.
- `GET /api/recovery?plan_dir=<path>` — full read-only Recovery Center
  diagnosis for one plan. It returns the `plan_dir`, `recovery` payload,
  mirrored top-level `artifacts`, and selected `run_id`, `session_id`,
  `turn_index`, and `commit_index` context. Missing or invalid
  `plan_dir` values return `invalid-request` or `invalid-plan`; split
  plans that cannot be inspected return `malformed-plan`.
- `GET /api/registry` — registry payload plus any loader warnings.
- `GET /api/state?plan_dir=<path>` — latest runner `state.json` under
  `state`, plus three top-level keys derived from the plan's session
  files: `sessions` (ordered list with `id`/`status`/`raw_value`/
  `line_number`/`path`, plus integer `number` for integer-only IDs;
  dotted IDs such as `2.1` are preserved verbatim and sorted in
  canonical dotted order), `counts` (`{pending, complete, empty,
  unknown}`), and `next_pending_id` (string or `null`). The schema
  matches `asl status --json` so frontend renderers can share a single
  shape. Malformed plans (missing `manifest.md`/`context.md`, no
  `session-*.md` files, etc.) return HTTP 500 with
  `reason: "malformed-plan"` and a `details.plan_dir` field — operators
  should branch on `reason`, not the HTTP status alone.
- `GET /api/events?plan_dir=<path>&offset=N&limit=N` — paginated
  `events.jsonl` entries (default 200, cap 2000).
- `GET /api/logs?plan_dir=<path>&stream=stdout|stderr|...` — bounded
  stdout/stderr tail or offset read (default 64 KiB, cap 1 MiB).
  Accepts `stream=stdout|stderr` for the active turn or an explicit
  `path=<absolute>` validated to be inside the plan's state directory.
- `GET /api/last-message?plan_dir=<path>` — bounded last-message text.
- `GET /api/result?plan_dir=<path>` — active turn `result.json` payload.
- `GET /api/verification?plan_dir=<path>` — active verification block.
- `GET /api/commits?plan_dir=<path>` — commit artifact metadata.
- `GET /api/progress?plan_dir=<path>` — parsed `progress.md` entries.
- `GET /api/final-review?plan_dir=<path>` — bounded `final-review.md`
  content (default 256 KiB).
- `GET /api/questions?plan_dir=<path>` — resolver prompt and Q&A
  context for the paused turn (mirrors `asl questions --json`).
- `GET /api/artifacts?plan_dir=<path>` — artifact paths and metadata
  for the latest run.
- `GET /api/archive` — archive entries plus the current
  `archive_retention_days`, computed `expires_at` values, and the latest
  archive-maintenance auto-empty result.

Mutating endpoints (bearer-token required):

- `POST /api/registry/pin` — upsert a `RegistryPin`. Payload:
  `{"plan_dir": "...", "label": "...", "profile": {...}}`.
- `POST /api/registry/unpin` — `{"plan_dir": "..."}`.
- `POST /api/plan/archive` — archive one live or discovered plan by
  moving the plan directory, matching ASL state directory, and pin/profile
  metadata into the Workbench archive root. Payload:
  `{"plan_dir": "...", "confirm_plan_dir": "..."}`. The two paths must
  canonicalize to the same full path, matching the UI's typed
  full-path confirmation. The server refuses arbitrary non-plan paths
  and returns `409` with `reason: "active-lock"` when an active runner
  lock is present.
- `POST /api/plans/archive` — bulk archive. Payload:
  `{"plan_dirs": ["..."]}`. Returns per-entry `results` plus
  `archived_count` and `failed_count`; failures are isolated to the
  affected entries.
- `POST /api/plan/delete` — backward-compatible archive alias for
  `POST /api/plan/archive`. It no longer permanently deletes a live
  plan; clients that need permanent removal should purge the resulting
  archive entry through `POST /api/archive/delete`.
- `POST /api/archive/restore` — restore one archive entry to its
  original plan and state paths. Payload: `{"archive_id": "..."}`.
  Returns `409` with `reason: "restore-conflict"` if a destination path
  that would be written already exists.
- `POST /api/archive/delete` — permanently purge one or many archive
  entries. Payload: `{"archive_ids": ["..."], "confirm_count": N}`.
  `confirm_count` must equal the number of selected IDs; otherwise the
  request returns `reason: "confirmation-mismatch"` and removes nothing.
- `POST /api/archive/settings` — update the global archive retention
  window. Payload: `{"archive_retention_days": N}` with a positive
  integer day count.
- `POST /api/launch` — spawn `asl run` for a plan. Payload:
  `{"plan_dir": "...", "profile": {...}}`. Returns `pid`, `argv`
  (with reply prompts redacted), `plan_dir`, `profile`, `reply_provided`,
  `reply_length`, `reply_origin`, and `state_dir`.
- `POST /api/reply` — resume a paused run with a verbatim reply.
  Payload: `{"plan_dir": "...", "reply_text": "..."}`. The `reply_text`
  body is streamed to the child via `--reply-stdin`; only `reply_length`
  survives in the response.
- `POST /api/control/stop-after-current` — enqueue a
  `request-stop-after-current` command on the per-plan control queue.
  The runner consumes it at the next checkpoint.
- `POST /api/control/select-harness` — enqueue a
  `select-future-harness` command. Payload:
  `{"plan_dir": "...", "harness": "codex|claude|copilot"}`. The active
  turn is never interrupted; the request takes effect at the next turn.
- `POST /api/unlock` — remove an operational lock. Active locks return
  409 unless the payload includes `"force": true`; idempotent missing
  locks return 200 with `had_lock: false`.

All control mutations enqueue records onto
`<state-dir>/<plan-hash>/control-queue.jsonl` rather than touching
`state.json` directly. The active runner drains the queue between
lifecycle checkpoints and translates each accepted command into the
same `RunControlAction` transitions that the in-process keyboard
listener already emits, so `run_control` events look identical
regardless of source.

All responses use the `responses.success_payload`/`error_payload`
envelopes with `command: "workbench"`. Stable error reasons reused
verbatim from the underlying helpers include `invalid-plan`,
`active-lock`, `malformed-plan`, `invalid-harness`, `bind-failed`, and
the `control_queue`/`workbench` reason codes. Control endpoints surface
the enqueued record under `queued_command` (not `command`) so the
top-level envelope key is not overwritten.

### Triage queue payload

Each plan summary in `/api/queue` (and `/api/plan`) carries additive
fields the frontend uses to triage at a glance. All fields are
backwards-compatible — no existing key is renamed or removed.

- `urgency_rank` (int) — lower means higher priority; the queue sorts
  ascending by default. Operator-blocking states come first.
- `urgency_group` (string) — one of `needs-action`, `in-progress`,
  `ready`, or `complete`. Mirrors `workbench.URGENCY_GROUPS`.
- `activity_label` (string) — short status string for the row
  ("Awaiting reply", "Running", "Complete", …). Mirrors
  `workbench.ACTIVITY_LABEL_BY_STATE`.
- `concise_reason` (string) — one-line cause behind the current state
  (pause reason, last failure summary, etc.).
- `last_activity_at` (ISO 8601 string or `null`) — most recent
  meaningful activity timestamp.
- `last_activity_source` (string or `null`) — provenance of the
  timestamp. One of `active-verification`, `active-turn`,
  `run-finished`, `run-started`, `lock-created`, `progress-journal`.
  Mirrors `workbench.LAST_ACTIVITY_SOURCES`.
- `completed_at` (ISO 8601 string or `null`) — when the plan reached
  `primary_state === "complete"`, sourced from the latest `progress.md`
  entry timestamp and falling back to the runner `run_finished_at`. It
  is `null` for non-complete plans and for complete plans with no
  recorded completion time. This additive field drives the completion
  timestamp shown in the queue/preview and the `sort=oldest` ordering
  for complete plans.
- `searchable_text` (string) — pre-lowercased, whitespace-collapsed
  concatenation of label, plan_dir, primary_state, recommended_action,
  urgency_group, activity_label, concise_reason, next_action_hint,
  detail, `"session <N>"`, pause_reason, pause_message,
  last_message_preview, run_id, harness, and discovery sources. The
  frontend's search input does a substring match against this field;
  callers should not re-normalize it.
- `recovery` (object) — compact Recovery Center summary from
  `WorkbenchRecovery.compact_payload()`. It includes `kind`, `title`,
  `summary`, `severity`, `recoverable`, `primary_action`,
  `artifact_count`, `command_count`, and `warning_count`. Queue/detail
  clients can render the compact summary immediately, then fetch
  `/api/recovery` for full evidence, commands, and artifacts.
- `review_resume` (object) — the same additive continuation projection exposed
  by `asl status --json`. Queue rows use it to distinguish a truly terminal
  complete plan from ready repair materialization/rereview, an insufficient
  absolute cap, or a malformed artifact that needs external correction. The
  compact and full Recovery Center payloads expose the identical object.

Urgency rank constants (lower = higher priority), mirrored from
`workbench.URGENCY_RANK_BY_STATE`. The `activity_label` column shows
the literal UI badge string the queue renders for each state, so
external readers can predict row text without consulting
`workbench.ACTIVITY_LABEL_BY_STATE`. A zero-pending, eligible review
continuation keeps the `recent` state but uses the distinct
`workbench.REVIEW_CONTINUATION_ACTIVITY_LABEL` badge:

| Primary state     | urgency_rank | urgency_group | activity_label                |
| ----------------- | -----------: | ------------- | ----------------------------- |
| paused-for-reply  |           10 | needs-action  | Reply needed                  |
| failed            |           20 | needs-action  | Last run failed               |
| stale             |           30 | needs-action  | Stale state                   |
| invalid           |           40 | needs-action  | Plan unreadable               |
| verifying         |           50 | in-progress   | Verifying                     |
| running           |           60 | in-progress   | Active turn                   |
| recent (pending sessions) |           70 | ready         | Run finished, sessions remain |
| recent (zero-pending review continuation) | 70 | ready | Review continuation ready |
| ready             |           80 | ready         | Ready to run                  |
| complete          |           90 | complete      | All sessions complete         |

The exported Python constants (`workbench.URGENCY_GROUPS`,
`workbench.LAST_ACTIVITY_SOURCES`, `workbench.URGENCY_RANK_BY_STATE`,
`workbench.URGENCY_GROUP_BY_STATE`, `workbench.ACTIVITY_LABEL_BY_STATE`, and
`workbench.REVIEW_CONTINUATION_ACTIVITY_LABEL`) are the source of truth — keep
any external code that branches on these strings in sync with that module.

### Recovery Center payload

The Recovery Center is deterministic. It recomputes a diagnosis from
plan files, runner state, lock state, commit artifacts, verification
payloads, and preflight repair hints on every refresh; no harness/model
is called to summarize failures, and no recovery state is persisted.

`workbench.RECOVERY_KINDS` is the source-of-truth vocabulary:
`none`, `review-continuation`, `reply-needed`, `not-loop-ready`, `verify-failed`,
`no-contract`, `malformed-contract`, `stale-lock`, `stale-state`,
`commit-preflight`, `commit-failed`, and `generic`. V1 maps common
structured blockers to concrete cards and falls back to `generic` for
unknown failures.

The full `/api/recovery` payload's `recovery` object contains:

- `plan_dir`, `kind`, `title`, `summary`, `severity`, and `recoverable`.
- `review_resume`, identical to status/run JSON. A `review-continuation` card
  uses it to show historical materializations, the minimum required cap,
  artifact validation errors, or the ready `materialize-repairs`,
  `run-repairs`, and `rereview` action.
- `run_id`, `session_id`, `turn_index`, and `commit_index` when a
  selected turn or failed commit can be identified.
- `evidence` rows (`label`, `value`, `detail`, `severity`) for pause
  reasons, readiness gates, failed checks, lock PIDs, stale-state
  reasons, commit statuses, harness config paths, and warnings.
- `actions` cards (`id`, `label`, `description`, `kind`, `target`,
  `endpoint`, `method`, `payload`, `disabled`). Action cards only
  target existing UI controls or existing endpoints; valid targets are
  `reply`, `events`, `active`, `verification`, `progress`, `artifacts`,
  `stdout`, `stderr`, `profile`, `launch`, and `unlock`.
- `commands` (`label`, `command`, `description`) for copy-only shell or
  harness repairs such as `asl preflight <plan-dir> --mark-loop-ready`,
  `$plan-preflight-iterate <plan-dir>/`, `asl verify <plan-dir>
  --session <id> --json`, and `asl preflight <plan-dir> --json`.
- `artifacts` (`label`, `path`, `kind`, `source`) for existing
  `state.json`, `events.jsonl`, `progress.md`, stdout/stderr,
  `result.json`, failed commit files, related config files, and—when review
  continuation is relevant—`final-review.md` plus `final-review.json`.
- `warnings` legacy strings plus `warning_details` structured entries
  (`code`, `message`, `severity`). State-read warning codes are
  `state-missing-race`, `state-unreadable`, `state-malformed-json`, and
  `state-invalid-json-shape`.
- A nested `compact` object matching the additive `recovery` summary
  exposed on `/api/queue` and `/api/plan`. Compact payloads intentionally
  expose only `warning_count`, not the full `warning_details` list.

`evidence`, `commands`, and `artifacts` are uncapped, expected-small,
best-effort arrays. Workbench does not impose a runtime item limit on
them; clients should render whatever the diagnosis returns and tolerate
empty lists. Artifact paths are best-effort pointers into state or
artifact directories and can be missing by the time an operator inspects
the card.

Commands in `commands` are copy-only repair guidance. Workbench never
executes those commands; mutating actions are limited to the existing
`/api/reply`, `/api/launch`, and `/api/unlock` endpoints with their
normal confirmation and validation flows.

Action `target` values are UI focus keys from `RECOVERY_ACTION_TARGETS`.
Supported targets scroll or focus the existing detail section named by
the target. If `target` is absent, the frontend falls back to
`RECOVERY_ACTION_TARGETS_BY_ID` and then legacy text heuristics for older
payloads. If a payload supplies an explicit but unsupported target,
Workbench does not run the action or infer a different target.

### Recovery payload examples

These minimized examples show the stable shape custom clients can branch
on. Real `/api/recovery` responses include the full field set described
above, plus nullable IDs such as `run_id`, `session_id`, `turn_index`, or
`commit_index` when they are available.

`review-continuation` with an insufficient absolute cap:

```json
{
  "kind": "review-continuation",
  "severity": "warning",
  "recoverable": true,
  "review_resume": {
    "action": "materialize-repairs",
    "blocker": "higher-cap-required",
    "historical_materializations": 2,
    "minimum_required_cap": 3
  },
  "artifacts": [
    { "label": "Final review", "path": ".../final-review.md" },
    { "label": "Structured review", "path": ".../final-review.json" }
  ]
}
```

`none`:

```json
{
  "kind": "none",
  "severity": "none",
  "recoverable": false,
  "evidence": [],
  "actions": [],
  "commands": [],
  "artifacts": [],
  "warnings": [],
  "warning_details": []
}
```

`reply-needed`:

```json
{
  "kind": "reply-needed",
  "severity": "warning",
  "recoverable": true,
  "session_id": "7",
  "actions": [
    {
      "id": "reply",
      "kind": "endpoint",
      "target": "reply",
      "endpoint": "/api/reply",
      "method": "POST"
    }
  ],
  "commands": []
}
```

`not-loop-ready`:

```json
{
  "kind": "not-loop-ready",
  "severity": "warning",
  "recoverable": true,
  "evidence": [
    { "label": "Readiness", "value": "not-loop-ready" }
  ],
  "actions": [
    { "id": "retry-run", "kind": "navigate", "target": "launch" }
  ],
  "commands": [
    {
      "label": "Repair readiness",
      "command": "asl preflight <plan-dir> --mark-loop-ready"
    }
  ]
}
```

`verify-failed`:

```json
{
  "kind": "verify-failed",
  "severity": "error",
  "recoverable": true,
  "session_id": "7",
  "evidence": [
    { "label": "Failed check", "value": "docs-regression" }
  ],
  "actions": [
    { "id": "inspect-verification", "kind": "inspect", "target": "verification" }
  ],
  "commands": [
    {
      "label": "Rerun verification",
      "command": "asl verify <plan-dir> --session 7 --json"
    }
  ],
  "artifacts": [
    { "label": "Result JSON", "path": ".../result.json", "kind": "json" }
  ]
}
```

`no-contract`:

```json
{
  "kind": "no-contract",
  "severity": "error",
  "recoverable": true,
  "session_id": "3",
  "actions": [
    { "id": "inspect-progress", "kind": "inspect", "target": "progress" }
  ],
  "commands": [
    {
      "label": "Check preflight",
      "command": "asl preflight <plan-dir> --json"
    }
  ]
}
```

`malformed-contract`:

```json
{
  "kind": "malformed-contract",
  "severity": "error",
  "recoverable": true,
  "session_id": "3",
  "actions": [
    { "id": "inspect-artifacts", "kind": "inspect", "target": "artifacts" }
  ],
  "artifacts": [
    { "label": "Session file", "path": ".../session-3.md", "kind": "markdown" }
  ]
}
```

`stale-lock`:

```json
{
  "kind": "stale-lock",
  "severity": "warning",
  "recoverable": true,
  "evidence": [
    { "label": "Lock PID", "value": "12345" }
  ],
  "actions": [
    {
      "id": "unlock",
      "kind": "endpoint",
      "target": "unlock",
      "endpoint": "/api/unlock",
      "method": "POST"
    }
  ]
}
```

`stale-state`:

```json
{
  "kind": "stale-state",
  "severity": "warning",
  "recoverable": true,
  "evidence": [
    { "label": "State", "value": "stale unfinished run" }
  ],
  "actions": [
    { "id": "inspect-active", "kind": "inspect", "target": "active" }
  ],
  "warnings": ["state.json disappeared during recovery diagnosis"],
  "warning_details": [
    {
      "code": "state-missing-race",
      "message": "state.json disappeared during recovery diagnosis",
      "severity": "warning"
    }
  ]
}
```

`commit-preflight`:

```json
{
  "kind": "commit-preflight",
  "severity": "warning",
  "recoverable": true,
  "actions": [
    { "id": "review-profile", "kind": "inspect", "target": "profile" }
  ],
  "commands": [
    { "label": "Inspect readiness", "command": "asl preflight <plan-dir> --json" }
  ]
}
```

`commit-failed`:

```json
{
  "kind": "commit-failed",
  "severity": "error",
  "recoverable": true,
  "commit_index": 1,
  "actions": [
    { "id": "inspect-stdout", "kind": "inspect", "target": "stdout" },
    { "id": "inspect-stderr", "kind": "inspect", "target": "stderr" }
  ],
  "artifacts": [
    { "label": "Commit stderr", "path": ".../commit-stderr.log", "kind": "log" }
  ]
}
```

`generic`:

```json
{
  "kind": "generic",
  "severity": "warning",
  "recoverable": true,
  "evidence": [
    { "label": "Reason", "value": "nonzero-exit" }
  ],
  "actions": [
    { "id": "inspect-events", "kind": "inspect", "target": "events" }
  ]
}
```

Representative mappings:

- `reply-needed` embeds the existing reply workflow and posts only to
  `/api/reply`.
- `not-loop-ready` shows readiness evidence, offers a retry through the
  existing launch flow, and displays copy-only preflight repair
  commands.
- `verify-failed`, `no-contract`, and `malformed-contract` identify the
  failed session when available, link failed-turn artifacts, and display
  copy-only `asl verify`/`asl preflight` commands.
- `stale-lock` and `stale-state` point at lock/state evidence and route
  unlock actions through the existing `/api/unlock` confirmation path.
- `commit-preflight` and `commit-failed` surface commit evidence,
  failed commit artifacts when present, and profile/launch follow-up
  actions.

### Frontend behavior

The bundled UI lives at `agent_session_loop/static/` and ships with
the Python package (no build tooling, no third-party JS). Pages:

- **Queue (`#queue`)** — the operator home, framed as a triage inbox.
  Filter chips collapse `verifying` under `running` and `invalid` under
  `failed`; the other primary states map 1:1. Rows show plan label/path,
  primary state, counts, last-activity preview, next-action hint, and an
  artifact link block. Queue metadata shows the resolved state root and
  last update; if polling fails, the visible rows are explicitly marked
  stale. The list polls every 5 s, pauses while the tab is hidden, and
  diffs rows by `plan_dir` so scroll position and filter selection
  survive each refresh.

  The queue defaults to triage mode: rows sort ascending by
  `urgency_rank` (paused-for-reply first, complete last), and any plan
  whose `urgency_group === "complete"` (or `primary_state === "complete"`)
  is hidden unless the operator flips the "show completed" toggle or
  selects the `complete` chip. A control bar between the filter chips
  and the inbox shell hosts a search input (matches against
  `searchable_text`), a sort `<select>` with the five supported modes
  (`urgency`, `recent`, `label`, `path`, `oldest`), and the "show
  completed" toggle. The search input debounces at 150 ms so a typing burst
  collapses into one `/api/queue` fetch; the clear button bypasses the
  debounce. When zero rows match, the empty-state element writes a
  stable kind onto `data-state` (`loading`, `no-plans`,
  `no-search-results`, `no-filter-matches`, `completed-hidden`, or
  `no-actionable`) and surfaces context-aware recovery actions (clear
  search, show completed, reset filter) for the kinds that have one.

  Row action buttons are navigation-only: clicking a row's action
  routes to the detail view with a focus hint (`#plan?dir=…&focus=<key>`)
  instead of issuing mutating POSTs from the queue. The button carries
  a `data-action-kind="<RecommendedAction>"` attribute (`watch`,
  `reply`, `inspect-failure`, `unlock`, `view`, `run`, `repair-plan`)
  so CSS can style row intent without re-parsing labels. Clicking the
  row body (outside the label `<a>` and the action `<button>`) selects
  the plan and updates the preview without leaving the queue route.

  On viewports ≥ 960 px the queue and detail panels render as a
  two-pane shell: the queue stays sticky on the left while the right
  pane shows either a placeholder ("select a plan to inspect") or the
  selected plan's preview panel. The preview reads from the cached
  queue payload (no extra fetch) and re-renders on selection clicks so
  the operator can scan triage state, pause reason, and last-message
  preview without leaving the queue. Below 960 px the layout collapses
  to a single-pane route toggle (queue ↔ detail) so phones get a clean
  stacked layout with no overlapping or clipped controls.

  The shell exposes three data attributes (`data-route="queue|detail"`,
  `data-viewport="mobile|desktop"`, `data-preview="empty|active"`) so
  CSS can target the placeholder/preview swap without re-querying
  `matchMedia` on every render.
- **Detail (`#plan?dir=<encoded>[&focus=<key>]`)** — per-plan view with
  the Recovery Center, active turn, active verification, session table,
  recent events, progress journal, artifacts, and operator controls
  (pin/unpin, edit profile, launch, reply, stop-after-current, select
  harness, unlock). The Recovery Center appears near the top of the
  detail page so a paused/failed/stale plan shows the diagnosis,
  evidence, copyable commands, related artifacts, and confirmed next
  actions before the raw logs. Dangerous actions (unlock with an active
  lock, launch under
  unsafe-bypass) prompt for in-page confirmation before the API call.

  The optional `focus=<key>` hash query parameter accepts the keys
  registered in `DETAIL_FOCUS_TARGETS` (`recovery`, `reply`, `events`,
  `active`, `launch`, `unlock`). The router validates the key against
  the map and silently drops unknown values. `applyDetailFocus` is
  one-shot — it
  scrolls and focuses the target element on the first render after
  navigation, then clears `state.route.focus` so subsequent 5 s polls
  do not keep snapping the viewport (the param itself stays in the URL,
  so a manual reload will re-focus once).

  The Recovery Center renders compact queue/detail `plan.recovery`
  immediately and then replaces it with full `/api/recovery` evidence
  when that request succeeds. If the detail fetch fails, the compact
  fallback remains visible when available; otherwise the panel renders a
  deterministic "Recovery unavailable" card. Reply-needed recoveries
  load the embedded reply form through `/api/questions` and submit only
  through `/api/reply`. Launch and unlock action cards call the same
  `/api/launch` and `/api/unlock` handlers as the ordinary controls;
  inspect and navigate actions scroll/focus existing detail sections.

  The session table iterates `payload.sessions` from `/api/state` and
  renders the dotted `id` verbatim (e.g., `2.1`, `2.2`), the basename
  of `path` (e.g., `session-2.1.md`), and the row's own `status`
  string (`pending`, `complete`, `empty`, `unknown`). The row whose
  `id` matches `state.active_turn.session_id` by exact string equality
  is overridden to `active` — there is no integer-loop or
  `parseInt(next_pending_session_id)` inference. The empty-state hint
  still reads `state.detailPlan.next_pending_session_id` (sourced from
  `/api/plan`, not `/api/state`); that fallback only surfaces before
  the first successful `/api/state` poll.

Persistence and privacy:

- The frontend persists operator preferences under the `wb.*`
  sessionStorage namespace: `wb.token` (bearer token), `wb.filter`
  (active chip), `wb.search` (search text), `wb.sort` (sort mode),
  `wb.completed` (show-completed toggle), and `wb.selected` (selected
  plan_dir for the preview pane). Each refresh poll preserves these
  preferences and the queue/detail scroll position. Cleanup mode reveals
  completed plans on its own gate and never writes `wb.completed`, so the
  persisted show-completed preference is left untouched when the operator
  exits cleanup mode.
- The reply textarea is intentionally **not** persisted: no
  sessionStorage or localStorage key under the `wb.reply` namespace is
  written, and no refresh path mutates `#wb-reply-text.value` outside
  the explicit clear-on-success flow. An in-progress reply draft
  survives any number of 5 s polls without being clobbered.

Static asset caching:

- The HTML shell uses `Cache-Control: no-store` so a server upgrade is
  never masked by a stale cached index.
- `/static/<path>` uses `Cache-Control: no-cache, must-revalidate` with
  a strong `ETag` derived from `st_mtime_ns + st_size`. The browser
  validates each request; identical bundles short-circuit to `304 Not
  Modified` without re-transferring bytes. If a build pipeline ever
  normalizes mtimes, switch the ETag generator to a content hash before
  the cache policy starts handing out stale 304s.

### Shutdown and lifecycle

`workbench_serve_from_args` installs signal handlers for SIGINT and
SIGTERM via `_install_shutdown_handlers`. Both trigger
`server.shutdown()` from a daemon thread, then the previous handlers
are restored. Ctrl+C remains the documented exit path; `kill <pid>` or
a systemd-style stop also exits cleanly and releases the listening
socket immediately (re-binding to the same port should succeed without
`EADDRINUSE`). Signal installation is silently skipped when the server
is constructed off the main thread; test harnesses call
`server.shutdown()` directly in that case.

Race-safe reads: `/api/logs`, `/api/last-message`, `/api/state`, and
`/api/verification` treat `FileNotFoundError` raised mid-read as "file
absent" — they return `404` (or `present: false`) rather than a half-
filled body with `error: ...`. Operators chasing a transient 500 during
a runner cleanup window should retry once before opening a bug.

### Workbench troubleshooting

- **Token forgotten / browser refused** — restart the server. Tokens
  are regenerated on every start, so reopening
  `http://127.0.0.1:8765/?token=<new>` (or running with `--open`)
  recovers access.
- **Port in use** — bind fails with `EADDRINUSE`. Pass `--port 0` to
  bind ephemerally, or `--port <free>` to choose another fixed port.
- **Plan does not appear in the queue** — the discovery sources are
  read in canonical order (`state`, `registry`, `plan-root`,
  `agent-plans-dir`, `tmp-default`, `share-default`). If the plan
  directory does not have both `manifest.md` and `context.md`, no
  source will surface it; add a `--plan-root` parent or pin it through
  the UI to force it in.
- **Running loops look ready** — confirm the queue metadata state root
  matches the root used by `asl run`/`asl sessions --running --json`.
  Workbench and runner state only line up when they use the same
  `--state-dir` value.
- **Pinned plans missing after a manual edit** — `load_registry`
  reports malformed entries as warnings on the returned payload. Check
  `GET /api/registry` (or the UI badge) for the warning list before
  re-pinning by hand.
- **Stale lock CTA** — `/api/unlock` returns 409 for an active lock
  unless `force: true` is set. Confirm the owning process is dead with
  `asl status --json` (look for `runner_state.stale_unfinished`) before
  unlocking with force.
- **Reply submitted but the run did not resume** — `/api/reply`
  forwards the body to `asl run --reply-stdin`. The runner refuses the
  resume with `reason: no-paused-run`, `missing-question-context`, or
  `reply-context-mismatch` if the paused turn is stale; the response
  body carries the verbatim `reason`.
- **Recovery Center reports `not-loop-ready`** — inspect the readiness
  evidence first. Copy `asl preflight <plan-dir> --mark-loop-ready` only
  after fixing non-derived preflight findings, or use the displayed
  `$plan-preflight-iterate <plan-dir>/` command for an interactive
  repair pass. The Workbench displays both commands but does not run
  either one.
- **Recovery Center reports `verify-failed`, `no-contract`, or
  `malformed-contract`** — open the failed-turn artifacts from the card,
  repair the session or its Completion Contract, then copy the displayed
  `asl verify <plan-dir> --session <id> --json` command before retrying
  the run through the existing launch action.
- **Recovery Center reports `commit-failed` or `commit-preflight`** —
  inspect failed commit artifacts and the repository worktree. Review
  the launch profile's commit mode before retrying; recovery cards never
  mutate git state directly.
- **Recovery Center reports `stale-lock` or `stale-state`** — confirm
  no runner is still active, then use the existing unlock confirmation
  flow. Stale lock cards show lock evidence such as PID/reason when the
  lock file can be read.

## Verification

Run the regression suite and compile check from this repository:

```bash
just test-fast
just test-all
just lint
python3 /Users/dev/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/session-resplit
just sync-skills
python3 -m py_compile agent_session_loop/*.py bin/.local/bin/agent-session-loop bin/.local/bin/asl
just build-ccl
just all
just verify-asl-install
```

The test suite is organized as subsystem modules (`tests/test_asl_*.py`,
stdlib `unittest` only) sharing fixtures through `tests/support.py`.
`just test-fast` runs the unit/schema/parser modules; `just test-all` runs
everything — including the helper-script suites under `skills/` — as isolated
parallel shards via `tests/run_suite.py`, which also fails if a test module is
missing from its shard lists. `tests/test_agent_session_loop.py` remains as a
compatibility shim so historical dotted names
(`tests.test_agent_session_loop.SomeTests`) keep resolving; it contributes no
tests under discovery, so `python3 -m unittest discover -s tests` runs each
test exactly once. Individual modules still run directly, for example
`python3 -m unittest tests.test_asl_verification -q`.

Use a synthetic plan directory for manual smoke tests so generated state, locks,
and logs do not touch an active project plan.

Manual smoke checklist:

- Create a synthetic split plan, run with a fake harness that fails one turn,
  confirm the run pauses, then rerun successfully and confirm the failed and
  successful turns are preserved under different `runs/<run_id>/` directories.
- Run a multi-session synthetic plan from a real terminal, press `h`, press `p`
  while a session is active, and confirm the run exits successfully after the
  current session with `reason: stopped`, a consumed `control` payload, and
  `run_control` events visible through `tail`.
- Repeat the text TTY run with `s`, `t`, and `1`/`2`/`3`; confirm `s` only
  prints stderr status lines, `t` first shows recent active stdout/stderr
  context and then streams new log lines while enabled (without re-emitting
  heartbeats, session start/finish, control, status, or commit lines), and
  later turns use the selected harness.
- Repeat the text TTY run with `--commit-per-session` and confirm the
  per-session commit runs before the stop-after-current request is consumed.
- Queue `asl control stop-after-current` while the last session of a
  `--review-harness` run executes, and confirm the run exits `reason: stopped`
  with `review_skipped_reason: stopped`, no generated review session file, and
  `asl status` reporting `review_resume.action: initial-review`.
- Run a text TTY plan with `--commit-at-end`, stop before all sessions are
  complete, and confirm no final `commit-0001-end` artifact is created.
- Run `run --json` and confirm stdout is the final JSON payload, stderr is
  quiet, and keyboard controls are not enabled.
- Make a required plan file unreadable or remove `context.md`, then confirm
  `run --json`, `status --json`, and `doctor --json` emit JSON failures without
  Python tracebacks.
- Run `doctor --json` against a valid synthetic plan and confirm it reports the
  resolved harness executable, prompt, and invocation argv.
- Create a synthetic plan with sessions `1`, `2`, and `3`; run
  `$session-resplit <plan-dir>/ --session 2 --parts 2`, rerun
  `$session-expand <plan-dir>/`, then confirm `asl status` and
  `asl run --dry-run --json` report `2.1` as the next pending session.
- Create a synthetic active lock, confirm `unlock --json` reports
  `reason: active-lock`, then run `unlock --force --json` and confirm the lock
  is removed.
- In terminal A, start an Autopilot-supervised synthetic two-session plan. In
  terminal B, send multiline `asl inbox guidance`, then confirm the next worker
  prompt receives JSON-escaped guidance and `asl inbox read` acknowledges only
  that snapshot.
- While the first turn is active, queue `asl control set-model`; within the
  five-second control-poll cadence confirm the runner records
  `implementation-model-updated`, the active subprocess is unchanged, and the
  second implementation invocation receives the override. Start a new runner
  process and confirm it returns to the original flag/native default.
- Have the worker emit a question and confirm the supervisor discovers one
  `QUESTION` record. Race two `asl inbox reply` commands, confirm exactly one
  succeeds, and verify the answer continues the same session without a
  `run_paused` event or pause-metric increment.
- Repeat without answering: confirm the bounded wait becomes one ordinary
  terminal pause after timeout. Repeat with `--no-mailbox`: confirm no lease is
  written, no live protocol is added to the prompt, and the historical
  pause/`asl run --reply` path remains unchanged.

Workbench browser workflow:

- Start `asl workbench serve --open` and confirm the queue lists
  state-backed plans, registry pins, and any `--plan-root` children.
  Pin one extra plan from the UI and confirm `workbench.json` gains the
  entry.
- Edit a pin's launch profile (implementation harness/model, review
  harness/model, commit mode), launch the
  plan from the UI under a fake harness, watch the row transition from
  `ready` → `running` → `complete`, and confirm the spawned child is a
  detached `asl run` process (not a child of the server). Confirm the captured
  argv uses `--harness-model` and `--review-harness-model` and contains no
  legacy shared ASL model flag.
- Load a schema-v1 registry pin containing `profile.model`, confirm the global
  warnings area reports that the legacy value was ignored, then save the
  profile and confirm `workbench.json` is schema v2 with only `harness_model`
  and `review_harness_model` model-routing fields.
- Start `asl run <plan-dir>` from a terminal while the Workbench is
  running. Confirm the Workbench attaches to that lock and surfaces the
  same active turn — both terminal and Workbench should observe the
  same `state.json` without lock contention.
- Pause a run with a blocked question (`RUNNER_PAUSE: blocked - ...`),
  open the detail view, submit a reply through the reply panel, and
  confirm `state.json`, `events.jsonl`, and the captured `argv` never
  contain the reply text (only `reply_length` and the redacted argv
  placeholder).
- Click "stop after current" from the Workbench while a fake-harness
  turn is in flight; confirm the turn is not killed and the next
  lifecycle checkpoint records `run_control: stop-after-current-...`
  events sourced from the control queue rather than the keyboard
  listener.
- Create a stale lock (e.g., kill the runner with SIGKILL), open the
  detail view, confirm the "stale lock" CTA is offered, and unlock with
  confirmation. Attempting to unlock an active lock should require the
  force toggle.
- From the detail view, open logs (`stdout` and `stderr`), the last
  message, the active turn's `result.json`, the progress journal, and
  `final-review.md` (when present); confirm every viewer respects the
  bounded read window without 500-ing if the artifact file is missing
  or rotated mid-poll.
- Hard-refresh the page and confirm in browser devtools that
  `/static/app.js` and `/static/styles.css` hit `304 Not Modified` on
  the second load while `GET /` keeps returning `Cache-Control:
  no-store`.
- Open the Workbench in two tabs and pin/unpin different plans
  concurrently; confirm `workbench.json` retains both edits (the
  advisory `workbench.json.lock` sidecar serializes the writers).
- Send SIGTERM to the Workbench process and confirm the listening
  socket is released immediately (re-binding to the same port should
  succeed without `EADDRINUSE`).
- Open the queue against a discovery root that contains a mix of
  paused, failed, running, ready, and complete plans. Confirm the
  default view sorts by `urgency_rank` (paused-for-reply rows surface
  first, complete rows are hidden) and that flipping the "show
  completed" toggle reveals the completed rows in every chip.
- Type a substring that appears only in one plan's label, plan_dir, or
  pause message; confirm the queue narrows to that plan after the
  150 ms search debounce and that the empty-state element renders with
  `data-state="no-search-results"` (and a "clear search" recovery
  button) when the query matches nothing. Switch the sort `<select>`
  through `urgency`, `recent`, `label`, `path`, and `oldest` and confirm
  the order changes accordingly. Reload the page and confirm the search
  text, sort mode, completed-visibility toggle, active filter chip,
  and selected plan all survive via sessionStorage (`wb.search`,
  `wb.sort`, `wb.completed`, `wb.filter`, `wb.selected`).
- Seed at least two complete plans and confirm the queue/preview show
  each plan's completion time (or "not recorded" when no `completed_at`
  was captured) and that non-complete rows show `-`. Enter cleanup mode,
  filter to the `complete` chip, and confirm completed plans appear
  without flipping "show completed". Sort `oldest` and confirm the
  stalest completed plans (by `completed_at`) lead. Press
  `select visible` and confirm every visible archivable completed row is
  selected for bulk archive while active-lock rows stay unselected; press
  `select candidates` and confirm only rows past the staleness threshold
  are selected. Exit cleanup mode and confirm `wb.completed` was not
  changed.
- Open the Archive view with several archived plans. Press `select all` and
  confirm every loaded entry is checked, the count reads `N of M entries
  selected`, and `select all` disables once all loaded entries are selected.
  Press `restore selected`, accept the plain confirm prompt, and confirm the
  entries restore best-effort through `POST /api/archive/restore` (feedback
  reads `restored X`, or `restored X, Y failed` on partial failure) with
  successfully restored entries leaving the selection. Re-select entries, press
  `purge selected`, type the exact count to confirm the typed-count purge, and
  confirm a mismatched count is rejected without deleting anything. Press `clear
  selection` and confirm the selection empties without restoring or purging any
  entry.
- On a viewport ≥ 960 px, click a row body (outside the label and
  action button) and confirm the right pane shows the selected plan
  preview (state badge, triage reason, last-message preview) without
  leaving the queue route. Resize below 960 px and confirm the layout
  collapses to a single pane with no overlapping controls.
- Click a row's "reply" action button on a paused-for-reply plan and
  confirm the URL becomes `#plan?dir=…&focus=reply`, the detail view
  scrolls the reply panel into view, and the reply textarea is focused.
  Repeat with an "unlock" action and confirm the controls panel
  scrolls into view and the Force-unlock button is focused (bail
  without confirming to verify the confirmation gate still works).
- Start typing a reply draft in the paused-Q&A panel, wait through two
  or three 5 s refresh polls, and confirm the draft is still present
  in the textarea (the reply text must never be persisted to
  sessionStorage/localStorage and must never be overwritten by
  polling).
- Seed representative Recovery Center plans in the Workbench state
  root: ready (`kind: none`), running (`kind: none` with active lock),
  reply-needed, not-loop-ready, verify-failed, stale-lock or
  stale-state, commit-failed, and complete (`kind: none`). Open each
  detail view and confirm the card kind, severity, evidence rows,
  action-card targets, copy-only commands, and artifact links match the
  seeded state without executing arbitrary repair commands.
