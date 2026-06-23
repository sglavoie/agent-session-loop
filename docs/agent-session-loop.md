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
`session-run`. The runner hard-gates on the `loop-ready` readiness state, and
each session is gated on its `## Completion Contract` `### Verify` recipe.

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
   `draft`/`reviewed` state), but it still refuses when non-readiness red/yellow
   findings remain:

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

`asl run` hard-refuses any plan whose `**Readiness**` is not `loop-ready`.
Override with `--allow-not-loop-ready` for emergency execution; preflight
findings are still reported in the run payload. The override also suppresses
the per-session verification gate's `no-contract` pause, but it does not
suppress failures from contracts that are present — a malformed or failing
contract still pauses the run.

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
asl run PLAN_DIR [--cwd REPO_ROOT] [--harness codex|claude|copilot] [--review-harness codex|claude|copilot] [--model MODEL] [--effort EFFORT] [--timeout SEC] [--max-sessions N] [--commit-per-session|--commit-at-end] [--state-dir DIR] [--unsafe-bypass|--no-unsafe-bypass] [--allow-not-loop-ready] [--reply TEXT] [--json] [--dry-run]
asl preflight PLAN_DIR [--cwd REPO_ROOT] [--harness codex|claude|copilot] [--commit-per-session|--commit-at-end] [--state-dir DIR] [--mark-loop-ready] [--json]
asl verify PLAN_DIR --session ID [--cwd REPO_ROOT] [--json]
asl status PLAN_DIR [--state-dir DIR] [--json]
asl questions PLAN_DIR [--state-dir DIR] [--json]
asl tail PLAN_DIR [--state-dir DIR] [--lines N] [--no-follow]
asl sessions [--running] [--recent] [--limit N] [--state-dir DIR] [--verbose] [--json]
asl doctor --harness codex|claude|copilot [--model MODEL] [--effort EFFORT] [--cwd REPO_ROOT] --plan-dir PLAN_DIR [--unsafe-bypass|--no-unsafe-bypass] [--json]
asl unlock PLAN_DIR [--state-dir DIR] [--force] [--json]
```

`run` acquires a per-plan lock, inspects the first pending session, invokes the
selected harness, records turn artifacts, and verifies that exactly the expected
session changed from `pending` to `complete`. `run` also enforces the loop-ready
gate: it refuses any plan whose `manifest.md` `**Readiness**` line is not
`loop-ready`, unless `--allow-not-loop-ready` is passed. After each completed
session, the runner invokes the same `asl verify` recipe; a non-zero exit pauses
the run with `reason: verify-failed`. `--allow-not-loop-ready` also suppresses
the verification gate's `no-contract` pause for sessions that lack a
`## Completion Contract`, but it never suppresses a failing contract that is
present. `--commit-per-session` then runs
`spec commit --yes --no-verify` before the next harness turn starts.

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
Markdown findings inside `final-review.md` never fail the run. Only review
execution failures (nonzero exit, timeout, verification failure on missing or
empty `final-review.md`) or repo mutations outside the plan directory fail the
run. A git mutation guard fingerprints dirty, staged, and untracked content
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
`Review: PLAN_DIR/final-review.md` in the final summary.

`preflight` inspects the plan without invoking a harness and reports every
finding needed to clear the loop-ready gate: missing readiness lines, contract
schema problems, manifest/session drift, environment prerequisites, and
harness/commit-mode prerequisites when `--cwd`, `--harness`, or a commit-mode
flag is supplied. JSON output carries a stable shape with `findings`,
`findings_by_severity`, `repair_hints`, `readiness`,
`expected_readiness_sha256`, `mark_loop_ready_requested`, and
`marked_loop_ready` fields. `--mark-loop-ready` flips `**Readiness**` to
`loop-ready` and re-stamps the SHA and timestamp when the only blocking
findings are manifest-readiness findings. It refuses when any non-readiness red
or yellow finding remains.

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
Use `--json` for scripts.

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
canonical paths; JSON always includes full paths. A missing state root returns
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
failures such as `not-loop-ready` preserve the previous paused `state.json` and
`events.jsonl` so the same reply can be retried after repair.
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
copilot -C <cwd> --add-dir <plan-dir> --allow-all-tools --allow-all-paths --no-ask-user --silent [--model <model>] [--effort <effort>] -p <prompt>
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
`--model` and `--effort` are passed to every selected harness only when
supplied. Omitting `--model` preserves each harness CLI default; reporting uses
per-turn best-effort metadata from Codex `~/.codex/config.toml`, Copilot
`~/.copilot/settings.json`, or `native CLI default` with source `unresolved`
when the default cannot be read. If a text TTY run switches harnesses with `1`,
`2`, or `3`, later turns recompute native model and effort metadata for the
selected harness. Explicit `--model` and `--effort` values carry over to later
selected harnesses.

## Safety Modes

By default, `run` and `doctor` use harness-specific bypass modes for every
harness. For Codex and Claude this means:

```text
codex exec -C <cwd> --add-dir <plan-dir> --dangerously-bypass-approvals-and-sandbox -o <last-message-file> <prompt>
claude --print --output-format stream-json --include-partial-messages --add-dir <plan-dir> --permission-mode bypassPermissions <prompt>
```

`--unsafe-bypass` is accepted as an explicit form of the default. Pass
`--no-unsafe-bypass` to use the harness default sandbox and approval behavior:

```text
codex exec -C <cwd> --add-dir <plan-dir> -s workspace-write -o <last-message-file> <prompt>
claude --print --output-format stream-json --include-partial-messages --add-dir <plan-dir> --permission-mode dontAsk <prompt>
```

Copilot always uses its explicit allow flags. Because bypass mode is the
default, run only trusted plans against trusted repositories, or pass
`--no-unsafe-bypass` when the harness should stay in its normal workspace
sandbox.

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
`commit_finished`, `run_control`, `review_session_created`, `run_paused`, and
`run_finished` events as they happen. Verification heartbeats fire every 30
seconds while a single check is in flight, so long-running checks remain
observable in `tail` and the live status snapshot. Verification check argv is
captured in the `verification_check_started` payload and not streamed to a
log file; command stdout and stderr remain captured in the underlying
`asl verify` invocation and in the turn `result.json` under the `verification`
key. `review_session_created`
fires once per run, when the runner appends a fresh generated review session;
resuming an existing pending review session does not re-emit it. Its payload
carries `run_id`, the review `session_id`, `review_harness`,
`review_session_path`, and `review_output_path`. `tail` renders it inline
alongside other events. Commit events include the commit index,
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
`Run state:` line whenever the runner state is orphaned. `doctor
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
- `not-loop-ready`: the plan's `manifest.md` `**Readiness**` is not `loop-ready`
  and the run did not pass `--allow-not-loop-ready`. The pause payload includes
  the readiness state and the preflight finding summary.
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

## Troubleshooting

Check what the runner sees:

```bash
asl status ~/.local/share/agent-plans/example-plan/ --json
```

Preview the next command without invoking the harness:

```bash
asl run ~/.local/share/agent-plans/example-plan/ --harness codex --dry-run --model gpt-5 --effort high
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

## Verification

Run the regression suite and compile check from this repository:

```bash
python3 -m pytest tests/test_agent_session_loop.py
python3 -m pytest skills/session-split/scripts/test_*.py skills/session-resplit/scripts/test_*.py
python3 /Users/dev/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/session-resplit
just sync-skills
python3 -m unittest discover -s tests
python3 -m py_compile agent_session_loop/*.py bin/.local/bin/agent-session-loop bin/.local/bin/asl
just build-ccl
just all
just verify-asl-install
```

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
