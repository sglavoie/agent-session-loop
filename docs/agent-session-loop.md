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
Both `run` and `doctor` default to the current directory; pass
`--cwd REPO_ROOT` to run the harness somewhere else.

## Workflow: Split, Expand, Repair, Verify, Run

The canonical loop-ready lifecycle is **split → expand → repair if needed →
run**. In directory mode, `session-expand` validates the split plan and then
runs final preflight with `--mark-loop-ready`; a clean plan is left ready for
`session-run`. `asl run` hard-gates on the `loop-ready` readiness state, but
self-heals derived progress/readiness drift (stale or missing `progress.md`
and recomputable `manifest.md` readiness metadata) before pausing, so a freshly
split-and-expanded plan rarely needs a manual repair step. Each session is
gated on its `## Completion Contract` `### Verify` recipe.

1. Create the split plan with `session-split`. The skill writes
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
when a pause condition requires review. Resume a paused run by re-running the
same command after fixing the blocker.

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
- Each check object has `name` (string), `type` (one of `command`,
  `file_exists`, `regex_in_file`), and type-specific fields.
- `command`: `argv` (non-empty array of strings, no shell), optional
  `timeout_seconds` (defaults to 60, max 3600), optional `cwd`.
- `file_exists`: `path` (string, repo-root-relative).
- `regex_in_file`: `path` (string, repo-root-relative), `pattern` (regex
  string).
- Every check also accepts an optional `baseline_expectation` (one of `skip`,
  `pass`, `fail`; defaults to `skip`). `pass`/`fail` declare how the check must
  behave on the *unmodified* repository; `asl baseline` records the evidence
  and preflight blocks a not-yet-started plan whose declared baselines lack
  current evidence.

`session-expand` validates each contract against the codebase and records any
problems in a per-session `## Contract Issues` section in
`session-<id>.expand.md`. `session-run` refuses to advance when issues are
flagged; `asl run` enforces the same gate per turn.

After writing a `- **Status**: complete` handoff, the runner and the
`session-run` skill both invoke:

```bash
asl verify ~/.local/share/agent-plans/example-plan/ --session 2 --json
```

`asl verify` runs the recipe in order. Exit 0 means every check passed;
non-zero exit is a hard pause. The JSON body carries a stable `reason` field
(`ok`, `verify-failed`, `no-contract`, `malformed-contract`, `cwd-error`, or
`invalid-session`) plus per-check rows with `failure_reason` enums
(`command-failed`, `command-timeout`, `command-not-found`, `command-error`,
`file-missing`, `file-error`, `regex-not-matched`). On verification failure,
the skill reverts the session handoff back to `pending` and the runner pauses
with `reason: verify-failed`.

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

Optional auto-commit modes require the resolved working directory to be a clean
git worktree before the first harness turn starts, and require `spec` on
`PATH`. Use
`--commit-per-session` to commit after each verified completed session, or
`--commit-at-end` to commit once after all pending sessions complete in the
same run. Both modes always invoke `spec commit --yes --no-verify`. A
stop-after-current request before all sessions complete exits successfully
without running the final `--commit-at-end` commit.

## Commands

```text
asl run PLAN_DIR [--cwd REPO_ROOT] [--harness codex|claude|copilot] [--harness-model MODEL] [--review-harness codex|claude|copilot] [--review-harness-model MODEL] [--effort EFFORT] [--timeout SEC] [--max-sessions N] [--max-pending-sessions N] [--max-review-fix-cycles N] [--commit-per-session|--commit-at-end] [--state-dir DIR] [--unsafe-bypass|--no-unsafe-bypass] [--allow-not-loop-ready] [--reply TEXT|--reply-stdin] [--json] [--dry-run]
asl preflight PLAN_DIR [--cwd REPO_ROOT] [--harness codex|claude|copilot] [--commit-per-session|--commit-at-end] [--state-dir DIR] [--max-pending-sessions N] [--mark-loop-ready] [--json]
asl verify PLAN_DIR --session ID [--cwd REPO_ROOT] [--artifact-dir DIR] [--json]
asl baseline PLAN_DIR [--cwd REPO_ROOT] [--per-check-timeout SECONDS] [--total-timeout SECONDS] [--json]
asl prepare PLAN.md [--cwd REPO_ROOT] [--harness codex|claude|copilot] [--harness-model MODEL] [--validator-harness H] [--review-harness H] [--review-harness-model MODEL] [--base-dir DIR] [--state-dir DIR] [--unsafe-bypass|--no-unsafe-bypass] [--json]
asl prepare --resume PLAN_DIR [...]
asl status PLAN_DIR_OR_ID [--state-dir DIR] [--json]
asl questions PLAN_DIR [--state-dir DIR] [--json]
asl tail PLAN_DIR [--state-dir DIR] [--lines N] [--no-follow]
asl sessions [--running] [--recent] [--limit N] [--state-dir DIR] [--verbose] [--json]
asl doctor --harness codex|claude|copilot [--harness-model MODEL] [--effort EFFORT] [--cwd REPO_ROOT] --plan-dir PLAN_DIR [--unsafe-bypass|--no-unsafe-bypass] [--json]
asl unlock PLAN_DIR [--state-dir DIR] [--force] [--json]
asl compact-state PLAN_DIR [--state-dir DIR] [--apply] [--json]
asl metrics [--since-days N] [--state-dir DIR] [--json]
```

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
present. `--commit-per-session` then runs
`spec commit --yes --no-verify` before the next harness turn starts.

Autonomous `asl run` turns prompt the harness with a Python-built session task
packet (`runner.build_task_packet`): a harness-neutral prompt that references
the session file, `context.md`, `progress.md`, expansion notes, and the
predecessor handoff by absolute path and embeds no readiness, lock, or
session-selection policy — the runner owns all of that. Packet turns do not
invoke the manual `session-run` skill; that skill remains the manual
one-session-per-conversation path. Setting `ASL_WORKER_MODE=skill` in the
runner's environment is a temporary rollback that restores the legacy
skill-invocation prompt for autonomous turns.

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
version=2`) and additionally require the structured artifact
`PLAN_DIR/final-review.json`: a JSON object with `version: 2`, an `outcome`
(`all-clear` or `findings`), and a `findings` list where every finding carries
a unique `id`, `severity` (`high`/`med`/`low`), `title`, `evidence` (objects
with a `path` and optional `symbol`), `explanation`, `repair_group`,
`done_criteria`, and `verification_checks` (completion-contract check
objects). The artifact must agree with the `final-review.md` status line on
outcome and per-severity totals; a malformed, contradictory, or incomplete
artifact fails review verification. Pre-existing v1 review sessions (base
marker only) remain Markdown-only compatible — no `final-review.json` is
required for them. The grammar, artifact schema, and shared verdict payload
live in `agent_session_loop.review`; the runner and the `asl` skill's
autopilot helper both read verdicts through it. Other review execution
failures (nonzero exit, timeout, verification failure on missing or empty
`final-review.md`) or repo mutations outside the plan directory also fail
the run. A git mutation guard fingerprints dirty, staged, and untracked content
outside `PLAN_DIR` before and after the review turn and pauses the run with
`reason: review-mutated-worktree` if the fingerprint changes. Review turns are
never auto-committed even under `--commit-per-session`. Review is skipped (with
the reason recorded in `review_skipped_reason`) on `--max-sessions` reached,
stop-after-current, verification failure, harness failure, commit failure, or
any pause during the implementation phase. If the plan is already fully
complete at run start, `--review-harness` still creates and runs the review
session. If a pending generated review session already exists in the plan and
the run is started without `--review-harness`, the run pauses with
`reason: review-harness-required`. After a successful review, text runs print
`Review: PLAN_DIR/final-review.md` followed by the `Status:` line as the final
summary line.

After a review session completes with structured findings, the runner drives
bounded same-plan repair/rereview cycles: it materializes one pending repair
child per `repair_group` beneath the review session (`N.1`, `N.2`, ... via
`agent_session_loop.review_repair`), executes the children serially with the
implementation harness, then appends the next top-level review session and
rereviews. `--max-review-fix-cycles N` bounds how many repair materializations
a plan may accumulate (default 2; `0` still runs the review but never creates
repair sessions; `--max-review-fix-plans` is a deprecated compatibility
alias — passing both flags is an input error). The loop stops early on an
all-clear rereview, on repeated findings (each finding is fingerprinted from
its normalized severity, title, evidence paths, and symbols —
`review.finding_fingerprint` — so line-number drift between reviews does not
defeat the comparison; a fingerprint that repeats the previous review stops
the loop without another cycle), on operator stop requests, or on cap
exhaustion, and pauses with `reason: malformed-review-repair` when safe repair
sessions cannot be materialized. Every review completion appends a
`review_cycle` event and a `review_cycles[]` state entry recording the cycle
number, review session id, outcome, introduced/closed/repeated finding
fingerprints, repair session ids, and disposition; the run payload's
`review_fix_disposition` carries the final disposition (`all-clear`,
`repairs-materialized`, `cycles-disabled`, `cycle-cap-exhausted`,
`findings-repeated`, `unstructured-findings`, `malformed-review-repair`, or
`not-loop-ready`). Cycle state is reconstructed from the persistent
`events.jsonl`, so caps and repeated-finding detection survive pauses and
relaunches.

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
gate decision: 0 when every check passes, non-zero otherwise. JSON output
includes `passed`, `recipe`, `checks`, and a stable `reason` (`ok`,
`verify-failed`, `no-contract`, `malformed-contract`, `cwd-error`, or
`invalid-session`). Per-check rows include stdout/stderr snippets and a stable
`failure_reason` enum.
`--commit-at-end` runs the same command once after all pending sessions finish
successfully in that runner invocation. `--commit-at-end` cannot be combined
with `--max-sessions` unless `--max-sessions` is high enough to complete every
session that was pending at the start of the run. If text TTY run controls stop
the run before all pending sessions finish, `--commit-at-end` does not run.

`--note TEXT` stores display-only reference text on the run payload. The note is
not sent to the harness and does not change plan execution. Text runs print it
in the compact `s` status snapshot, final completion or pause output, and
Ctrl+C interrupted exits. JSON/state output preserves it as top-level `note`.

For normal text TTY runs, `run` also listens for keyboard controls:

- `p`: toggle stop-after-current. When active, the current session still
  finishes and any `--commit-per-session` commit for that session runs; if more
  sessions remain, the run exits 0 with `ok: true`, `paused: false`, and
  `reason: stopped`. Press `p` again before the session finishes to cancel the
  request.
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
ambiguous-plan`. Resolution is `status`-only; `run`, `tail`, `unlock`, and other
plan-taking commands remain path-only. Successful text and JSON output continue
to report the canonical resolved `plan_dir`.

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

`doctor` performs a non-mutating harness and path check. Use `--json` for
scriptable prerequisite checks.

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

`prepare` runs the whole preparation pipeline in one deterministic command:
structure gate (at least two session headings, each with a parseable inline
`## Completion Contract`) → deterministic authoring (the session-split Step 8
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
finding counts, artifact corpus size and in-window growth, and projected
compaction savings (a dry-run over the state directories active inside the
window, net of retained failure-artifact bytes; use `asl compact-state` for a
full per-plan projection). The command
is read-only: it never mutates state, events, or plan directories.

`asl questions` reads the durable paused turn for a plan and renders an external
resolver prompt that another agent (or a human) can use to gather operator
answers. It never invokes the harness, never mutates plan state, and never
writes the operator's reply anywhere. Text output prints the rendered prompt
preceded by a one-line hint that points back at `asl run --reply`; `--json`
emits `ok`, `command: "questions"`, `session_id`, `next_pending_id`, the
verbatim `last_message`, the assembled `rendered_prompt`, and `state_path`. If
no paused turn exists, `--json` produces `ok: false` with a stable `reason`
(`no-paused-run`, `missing-question-context`, or `malformed-plan`) and `asl run`
exits cleanly without a pause stub. The `asl questions` resolver prompt is an
external operator-facing prompt headed
`# Paused Agent Session - Resolver Prompt`; it is intentionally distinct from
the in-harness resume block headed `## ASL reply context`, though both surfaces
include the paused question text and metadata needed to answer it.

`asl run --reply TEXT` resumes a paused plan by injecting the operator's
verbatim reply as a one-shot prefix on the next harness turn only. The reply is
expanded into a `## ASL reply context` block above the regular session prompt;
`build_reply_prompt` always reuses `build_session_prompt` byte-for-byte for the
first line. The runner persists the resume metadata as a `reply_provided`
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

## Harness Commands

The built-in adapters construct argv lists directly and never use shell
interpolation.

Codex:

```text
codex exec -C <cwd> --add-dir <plan-dir> --dangerously-bypass-approvals-and-sandbox [--model <model>] [-c model_reasoning_effort="<effort>"] -o <last-message-file> <prompt>
```

Claude:

```text
claude --print --output-format stream-json --include-partial-messages --add-dir <plan-dir> --permission-mode bypassPermissions [--model <model>] [--effort <effort>] <prompt>
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
claude --print --output-format stream-json --include-partial-messages --add-dir <plan-dir> --permission-mode bypassPermissions <prompt>
copilot -C <cwd> --add-dir <plan-dir> --allow-all-tools --allow-all-paths --no-ask-user --silent -p <prompt>
```

`--unsafe-bypass` is accepted as an explicit form of the default. Pass
`--no-unsafe-bypass` to use the harness default sandbox and permission mode
where supported by that harness:

```text
codex exec -C <cwd> --add-dir <plan-dir> -s workspace-write -o <last-message-file> <prompt>
claude --print --output-format stream-json --include-partial-messages --add-dir <plan-dir> --permission-mode dontAsk <prompt>
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
- `runs/<run_id>/commit-0001-end/...`: equivalent artifacts for
  `--commit-at-end`.

`state.json` is updated to the latest run and includes `run_id`, `run_dir`, and
`turns[*]` entries with `artifact_dir`, `stdout_path`, `stderr_path`,
`last_message_path`, and `result_path`. It also includes `commit_mode`,
`commits`, `control`, and, after successful auto-commit preflight,
`commit_preflight`. The `control` object reports `stop_after_current`,
`tail_enabled`, `next_harness`, `consumed`, and `last_action`; `last_action` is
one of `stop-after-current-requested`, `stop-after-current-canceled`,
`stop-after-current-consumed`, `tail-enabled`, `tail-disabled`,
`harness-selected`, or `null`. Failed-run artifacts remain in their original
`runs/<run_id>/` directory when a later retry succeeds.

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
both are `null` between turns and after `run_finished`. `events.jsonl` records
`turn_started`, `turn_heartbeat`, `action_required`, `turn_finished`,
`verification_started`, `verification_check_started`, `verification_heartbeat`,
`verification_check_finished`, `verification_finished`, `commit_started`,
`commit_finished`, `run_control`, `review_session_created`, `review_cycle`,
`run_paused`, and `run_finished` events as they happen. Verification heartbeats fire every 30
seconds while a single check is in flight, so long-running checks remain
observable in `tail` and the live status snapshot. Verification check argv is
captured in the `verification_check_started` payload and not streamed to a
log file; command stdout and stderr remain captured in the underlying
`asl verify` invocation and in the turn `result.json` under the `verification`
key. `review_session_created`
fires once per run, when the runner appends a fresh generated review session;
resuming an existing pending review session does not re-emit it. Its payload
carries `run_id`, the review `session_id`, `review_harness`,
`review_session_path`, and `review_output_path`. `review_cycle` fires once per
completed review session and carries the repair/rereview cycle record (cycle
number, review session id, outcome, finding fingerprints with
introduced/closed/repeated classification, repair session ids, and
disposition). `tail` renders them inline alongside other events. Commit events include the commit index,
scope, session ID for per-session commits, argv, cwd, artifact paths, status
result, duration, and commit SHA when one was created. `run_control` events
record `stop-after-current-requested`, `stop-after-current-canceled`,
`stop-after-current-consumed`, `tail-enabled`, `tail-disabled`, and
`harness-selected` actions with `stop_after_current`, `tail_enabled`,
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
`control`, `completed_sessions`, `completed_session_ids`, `turns`,
`initial_snapshot`, `final_snapshot`, `state_path`, and `events_path`. Runs
without commit flags report `commit_mode: null` and `commits: []`.

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
`Run state:` line whenever the runner state is orphaned. `status --json` error
payloads echo the original argument in `plan_dir`: an uninspectable plan (or an
unresolved label) reports `reason: malformed-plan`, an unreadable state root
reports `reason: state-error`, and a bare label matching more than one existing
plan directory reports `reason: ambiguous-plan` with `details.state_root` and a
sorted `details.candidates` list of canonical paths. In text mode an ambiguous
label prints a concise stderr error instructing the operator to pass a full
plan directory path. `doctor
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

## Pause Semantics

A stop-after-current request is not a pause. When `p` is active at a lifecycle
checkpoint and another session remains pending, the run exits successfully with
`ok: true`, `paused: false`, and `reason: stopped`, then releases the plan
lock. If the just-completed session was the final pending session, the run ends
with `reason: all-complete`. Stop-after-current does not add a pause reason.

A paused run exits nonzero and leaves the plan lock released. Inspect the JSON
payload, `state.json`, `events.jsonl`, and the last turn artifacts, then rerun
the same command after resolving the issue.

Common pause reasons:

- `nonzero-exit`: harness command exited nonzero.
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
- `commit-failed`: `spec commit --yes --no-verify` ran but did not create a new
  commit, including nonzero exits and zero-exit no-op commits that leave `HEAD`
  unchanged.
- `harness-unavailable`: a runtime-selected future harness could not be resolved
  before the next turn started. The previous completed turn remains recorded,
  the lock is released, and `pause_details` identifies the session, requested
  harness, configured executable, resolved executable when available, config
  path, and error.
- `not-loop-ready`: the plan's `manifest.md` `**Readiness**` is not
  `loop-ready` after the runner attempted any eligible derived repair, and the
  run did not pass `--allow-not-loop-ready`. The pause payload includes the
  readiness state, preflight finding summary, and `readiness_gate_repair` when
  a repair was attempted.
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
- `review-mutated-worktree`: the review harness changed repo content outside
  the plan directory. The pre/post fingerprint covers the working tree's
  `HEAD`, tracked diff (with the plan directory excluded), and untracked files
  outside the plan directory; any change trips the guard. The implementation
  turns that ran before the review are preserved.
- `interrupted`: the run was interrupted by Ctrl+C.

## Autopilot Skill (`$asl`)

The shared `asl` skill (`skills/asl/`, invoked as `$asl` under Codex or `/asl`
under Claude/Copilot) supervises a single `asl run` invocation from start to
finish without a human babysitting each turn. The operator hands the skill the
exact `asl run ...` command they would otherwise type; the bundled helper
(`skills/asl/scripts/asl-autopilot.py`) starts or attaches to the run, polls
`asl status --json`, streams heartbeats to stderr, auto-retries recoverable
pauses, and stops only when a run needs a human answer or has finished.

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
- Never persists reply text: resumes flow through `asl run --reply-stdin`, so
  the reply appears only on the child's stdin, never in argv or helper output.

### Attach behavior

Each supervision cycle reads `asl status --json` first. A stale lock is treated
as safe to start over (ASL clears stale locks before acquiring). An active lock
for the same plan and state means **attach**: the helper polls the foreign run
to completion instead of starting a second one. An active lock for a different
plan directory, or an observably different harness than the operator requested,
stops with `user_required` for a human to resolve. Self-relaunches (for
`--max-sessions` capped chunks) and attach polls share one bounded budget so a
wedged plan can never loop forever.

### Reply behavior

The helper auto-retries recoverable same-session pauses (`verify-failed`,
`nonzero-exit`, `timeout`, `no-status-change`) up to twice per session and
reason before escalating. When a pause genuinely needs an answer, the result
carries `needs_agent_reply` with a `questions` block (resolver prompt, the
paused agent's last message, pause metadata, artifact paths). The driving agent
either infers a clearly defensible reply from the plan/repo/artifacts or relays
the question to the operator, then resumes by streaming the reply on stdin:

```text
printf '%s' '<reply text>' | skills/asl/scripts/asl-autopilot.py --command '<same exact command>' --json --reply-stdin
```

### Review-fix loop behavior

When the supervised command used `--review-harness`, a clean completion still
carries the final-review verdict. An all-clear review returns `complete` with a
`review` block whose `outcome` is `all-clear`. A review that reports findings
returns `review_findings` with the `final-review.md` path, status line,
severity counts, and a bounded summary excerpt. On findings the driving agent
opens a bounded follow-up fix cycle — draft a fix plan, `session-split` and
`session-expand` it, then rerun the helper against the generated plan directory.
Automatic review-fix loops are capped at 2 generated follow-up plans; after
that, remaining findings go to the operator instead of another auto-loop. This
loop cap lives in skill prose because each helper invocation supervises exactly
one command and there is no cross-invocation loop counter.

### Final helper statuses

The helper emits a single JSON result on stdout whose `reason` field is the
terminal verdict (`ok` is `true` only for a clean completion):

- `complete` (`ok: true`): every session finished. With `--review-harness`, an
  all-clear review is attached under `review.outcome: all-clear`.
- `needs_agent_reply` (`ok: false`): the run paused on a decision the helper
  could not auto-resolve; a `questions` block carries the resolver context and
  the driving agent must supply a reply.
- `review_findings` (`ok: false`): the run completed but the final review
  reported findings; a `review` block carries the verdict for a follow-up fix
  cycle.
- `user_required` (`ok: false`): an active `asl run` lock holds this plan/state
  for a different run; a human must resolve the conflict before supervising.
- `blocked` (`ok: false`): a chunk stopped without completing and without a
  recoverable pause (the child's non-paused failure reason is attached).
- `capped` (`ok: false`): the supervision budget (self-relaunch chunks or
  attach polls) was exhausted with sessions still pending.
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

If a plan has no pending sessions but is not complete, inspect every final
`session-<id>.md` handoff and normalize statuses to exactly `pending` or
`complete`. Ignore older status templates preserved above the final handoff.

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
sets the optional repository root and `--label` sets the optional
human-readable name. Other launch-profile fields stay `null` (runner
default) unless edited through the UI. `unpin` removes the matching
pin. `registry` prints the loaded registry (pins, `plan_roots`, and
any loader warnings); pass `--json` for the
`responses.success_payload`-shaped envelope used elsewhere in the CLI.

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

Urgency rank constants (lower = higher priority), mirrored from
`workbench.URGENCY_RANK_BY_STATE`. The `activity_label` column shows
the literal UI badge string the queue renders for each state, so
external readers can predict row text without consulting
`workbench.ACTIVITY_LABEL_BY_STATE`:

| Primary state     | urgency_rank | urgency_group | activity_label                |
| ----------------- | -----------: | ------------- | ----------------------------- |
| paused-for-reply  |           10 | needs-action  | Reply needed                  |
| failed            |           20 | needs-action  | Last run failed               |
| stale             |           30 | needs-action  | Stale state                   |
| invalid           |           40 | needs-action  | Plan unreadable               |
| verifying         |           50 | in-progress   | Verifying                     |
| running           |           60 | in-progress   | Active turn                   |
| recent            |           70 | ready         | Run finished, sessions remain |
| ready             |           80 | ready         | Ready to run                  |
| complete          |           90 | complete      | All sessions complete         |

The exported Python constants (`workbench.URGENCY_GROUPS`,
`workbench.LAST_ACTIVITY_SOURCES`, `workbench.URGENCY_RANK_BY_STATE`,
`workbench.URGENCY_GROUP_BY_STATE`, `workbench.ACTIVITY_LABEL_BY_STATE`)
are the source of truth — keep any external code that branches on these
strings in sync with that module.

### Recovery Center payload

The Recovery Center is deterministic. It recomputes a diagnosis from
plan files, runner state, lock state, commit artifacts, verification
payloads, and preflight repair hints on every refresh; no harness/model
is called to summarize failures, and no recovery state is persisted.

`workbench.RECOVERY_KINDS` is the source-of-truth vocabulary:
`none`, `reply-needed`, `not-loop-ready`, `verify-failed`,
`no-contract`, `malformed-contract`, `stale-lock`, `stale-state`,
`commit-preflight`, `commit-failed`, and `generic`. V1 maps common
structured blockers to concrete cards and falls back to `generic` for
unknown failures.

The full `/api/recovery` payload's `recovery` object contains:

- `plan_dir`, `kind`, `title`, `summary`, `severity`, and `recoverable`.
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
  `result.json`, failed commit files, and related config files.
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
