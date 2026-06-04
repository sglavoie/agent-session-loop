# Agent Session Loop

`agent-session-loop` repeatedly invokes an agent harness against a split
session plan directory. Each harness turn runs one `$session-run` or
`/session-run` invocation, then the runner re-reads the plan files before
deciding whether to continue.

```bash
bin/.local/bin/agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex
bin/.local/bin/agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex --max-sessions 1
bin/.local/bin/agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex --commit-per-session
bin/.local/bin/agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex --commit-at-end
bin/.local/bin/agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex --dry-run
bin/.local/bin/agent-session-loop status ~/.local/share/agent-plans/example-plan/
bin/.local/bin/agent-session-loop tail ~/.local/share/agent-plans/example-plan/
```

The package is stdlib-only and lives in `agent_session_loop/`. The executable
wrapper at `bin/.local/bin/agent-session-loop` adds this repository to
`sys.path` and dispatches to the package CLI.

## Setup

Run from this repository, or add `bin/.local/bin` to `PATH`:

```bash
export PATH="$PWD/bin/.local/bin:$PATH"
agent-session-loop status ~/.local/share/agent-plans/example-plan/
```

The runner supports `codex`, `claude`, and `copilot` harnesses. Defaults use the
executable names on `PATH`, and optional overrides live in
`~/.config/agent-session-loop/harnesses.json` or
`$XDG_CONFIG_HOME/agent-session-loop/harnesses.json`.

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
agent-session-loop doctor --harness codex --cwd "$PWD" --plan-dir ~/.local/share/agent-plans/example-plan/
```

`doctor` verifies the working directory, plan directory, configured harness
executable, next pending session, generated prompt, and argv.

## Workflow After Session Expand

1. Create the split plan with `session-split`.
2. Run `session-expand` on the plan directory so expansion notes and parallel
   validation are available.
3. Check the plan:

```bash
agent-session-loop status ~/.local/share/agent-plans/example-plan/
```

4. Preview the first harness turn:

```bash
agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex --dry-run
```

5. Run the loop:

```bash
agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex
```

The runner stops when every `session-N.md` handoff status is `complete`, when
`--max-sessions` is reached, or when a pause condition requires review. Resume
a paused run by re-running the same command after fixing the blocker.

Optional auto-commit modes require `--cwd` to be a clean git worktree before
the first harness turn starts, and require `spec` on `PATH`. Use
`--commit-per-session` to commit after each verified completed session, or
`--commit-at-end` to commit once after all pending sessions complete in the
same run. Both modes always invoke `spec commit --yes --no-verify`.

## Commands

```text
agent-session-loop run PLAN_DIR --cwd REPO_ROOT [--harness codex|claude|copilot] [--model MODEL] [--effort EFFORT] [--timeout SEC] [--max-sessions N] [--commit-per-session|--commit-at-end] [--state-dir DIR] [--unsafe-bypass] [--json] [--dry-run]
agent-session-loop status PLAN_DIR [--state-dir DIR] [--json]
agent-session-loop tail PLAN_DIR [--state-dir DIR] [--lines N] [--no-follow]
agent-session-loop doctor --harness codex|claude|copilot [--model MODEL] [--effort EFFORT] --cwd REPO_ROOT --plan-dir PLAN_DIR [--json]
agent-session-loop unlock PLAN_DIR [--state-dir DIR] [--force] [--json]
```

`run` acquires a per-plan lock, inspects the first pending session, invokes the
selected harness, records turn artifacts, and verifies that exactly the expected
session changed from `pending` to `complete`. `--commit-per-session` then runs
`spec commit --yes --no-verify` before the next harness turn starts.
`--commit-at-end` runs the same command once after all pending sessions finish
successfully in that runner invocation. `--commit-at-end` cannot be combined
with `--max-sessions` unless `--max-sessions` is high enough to complete every
session that was pending at the start of the run.

`status` reads durable plan files and prints session counts, the next pending
session, content hashes, state paths, lock state, and any active harness turn.
Use `--json` for scripts.

`tail` prints recent activity for the latest run and follows `events.jsonl`
plus live harness and commit stdout/stderr logs until the observed run
finishes. Use `--no-follow` to print recent activity and exit.

`doctor` performs a non-mutating harness and path check. Use `--json` for
scriptable prerequisite checks.

`unlock` removes a stale lock. It refuses to remove an active lock unless
`--force` is supplied. Use `--json` when automation needs the removed lock
state or an active-lock failure payload.

## Harness Commands

The built-in adapters construct argv lists directly and never use shell
interpolation.

Codex:

```text
codex exec -C <cwd> --add-dir <plan-dir> -s workspace-write [--model <model>] [-c model_reasoning_effort="<effort>"] -o <last-message-file> <prompt>
```

Claude:

```text
claude --print --add-dir <plan-dir> --permission-mode dontAsk [--model <model>] [--effort <effort>] <prompt>
```

Copilot:

```text
copilot -C <cwd> --add-dir <plan-dir> --allow-all-tools --allow-all-paths --no-ask-user --silent [--model <model>] [--effort <effort>] -p <prompt>
```

Harness prompts use the expected skill prefix:

```text
$session-run <plan-dir>/
/session-run <plan-dir>/
```

Codex writes a last-message file with `-o`. Claude and Copilot do not use that
file, so their last-message capture falls back to stdout. `--model` and
`--effort` are passed to the harness only when supplied. Omitting `--model`
preserves the native CLI default; reporting uses best-effort metadata from
Codex `~/.codex/config.toml`, Copilot `~/.copilot/settings.json`, or
`native CLI default` with source `unresolved` when the default cannot be read.

## Safety Modes

The default Codex mode uses workspace-write sandboxing:

```text
codex exec -C <cwd> --add-dir <plan-dir> -s workspace-write -o <last-message-file> <prompt>
```

`--unsafe-bypass` changes Codex and Claude to their harness-specific bypass
modes:

```text
codex exec -C <cwd> --add-dir <plan-dir> --dangerously-bypass-approvals-and-sandbox -o <last-message-file> <prompt>
claude --print --add-dir <plan-dir> --permission-mode bypassPermissions <prompt>
```

Copilot always uses its explicit allow flags. Use bypass mode only when the plan
and repository are trusted and the harness must write outside the normal
workspace sandbox.

The runner itself never marks sessions complete. Durable completion is only the
`- **Status**: complete` line inside each `session-N.md` handoff. Operational
state can be deleted and rebuilt from the plan files.

## State Files

By default, runtime metadata is stored under:

```text
~/.local/state/agent-session-loop/<plan-path-hash>/
```

`--state-dir DIR` changes the state root; the plan hash subdirectory is still
created below that root. The hash is based on the canonical plan directory path,
so logs and locks remain stable as session files change.

Runtime files include:

- `state.json`: latest run payload.
- `events.jsonl`: append-only run, pause, turn, and commit events.
- `lock.json`: active per-plan runner lock.
- `runs/<run_id>/`: immutable artifacts for one runner invocation.
- `runs/<run_id>/turn-0001-session-N/stdout.log`: stdout from the harness turn.
- `runs/<run_id>/turn-0001-session-N/stderr.log`: stderr from the harness turn.
- `runs/<run_id>/turn-0001-session-N/last-message.txt`: final harness message
  when available.
- `runs/<run_id>/turn-0001-session-N/result.json`: diagnostic result for the
  turn, including argv, status changes, runner signals, snapshots, and artifact
  paths.
- `runs/<run_id>/commit-0001-session-N/stdout.log`: stdout from an automatic
  per-session `spec commit --yes --no-verify` invocation.
- `runs/<run_id>/commit-0001-session-N/stderr.log`: stderr from that commit.
- `runs/<run_id>/commit-0001-session-N/result.json`: diagnostic commit result,
  including argv, cwd, return code, HEAD before/after, status result, commit
  SHA when one was created, and artifact paths.
- `runs/<run_id>/commit-0001-end/...`: equivalent artifacts for
  `--commit-at-end`.

`state.json` is updated to the latest run and includes `run_id`, `run_dir`, and
`turns[*]` entries with `artifact_dir`, `stdout_path`, `stderr_path`,
`last_message_path`, and `result_path`. It also includes `commit_mode`,
`commits`, and, after successful auto-commit preflight, `commit_preflight`.
Failed-run artifacts remain in their original `runs/<run_id>/` directory when a
later retry succeeds.

While a harness turn is active, `state.json` also includes `active_turn` with
the run id, turn index, session number, artifact paths, elapsed and idle
seconds, last activity metadata, heartbeat count, and any best-effort
action-required signal. `events.jsonl` records `turn_started`,
`turn_heartbeat`, `action_required`, `turn_finished`, `commit_started`,
`commit_finished`, `run_paused`, and `run_finished` events as they happen.
Commit events include the commit index, scope, session number for per-session
commits, argv, cwd, artifact paths, status result, duration, and commit SHA when
one was created.

For normal text runs, live progress is printed to stderr with local compact
timestamps, for example `[2026-06-04 14:32:10] agent-session-loop: session 2
started (turn 2)`, so stdout remains the final human summary. The live lines
cover run start, session starts and finishes, periodic 30-second heartbeats,
action-required signals, commits, pauses, and run finish. Detailed artifact
paths remain in the final summary, `status`, `state.json`, and `events.jsonl`
instead of being repeated on every turn line.

For `run --json`, stdout remains the final JSON payload and stderr stays quiet;
live monitoring should read `state.json`, `events.jsonl`, or use `tail`.

## JSON Output

Use `--json` for runner integrations:

```bash
agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex --max-sessions 1 --json
agent-session-loop status ~/.local/share/agent-plans/example-plan/ --json
agent-session-loop doctor --harness codex --cwd "$PWD" --plan-dir ~/.local/share/agent-plans/example-plan/ --json
agent-session-loop unlock ~/.local/share/agent-plans/example-plan/ --json
```

All parsed subcommands that support `--json` print JSON errors to stdout. Common
error fields are `ok`, `command`, `reason`, `error`, `paused`, and `details`.
`run --json` preserves its existing fields and adds the common `command` and
`details` fields; important run fields include `paused`, `reason`,
`pause_reason`, `pause_details`, `model`, `model_source`, `effort`,
`effort_source`, `commit_mode`, `commits`, `completed_sessions`, `turns`,
`initial_snapshot`, `final_snapshot`, `state_path`, and `events_path`. Runs
without commit flags report `commit_mode: null` and `commits: []`.

Commit payloads include `commit_index`, `scope`, `session`, `artifact_dir`,
`stdout_path`, `stderr_path`, `result_path`, `argv`, `cwd`, `returncode`,
`success`, `status_result`, `head_before`, `head_after`, `commit_sha`,
`started_at`, `finished_at`, `duration_seconds`, `stdout_chars`, and
`stderr_chars`. `status_result` is `success` only when the command exits zero
and changes `HEAD`; nonzero exits, missing `HEAD`, and unchanged `HEAD` are
recorded as failed commit attempts. A failed commit is also copied into
`pause_details.commit`.

Dry-run `planned` payloads, harness invocation payloads, active turn state,
events, and turn `result.json` artifacts include the same model and effort
metadata. Source values are `flag`, `codex_config`, `copilot_settings`, and
`unresolved`; omitted effort is reported as `null`.

`status --json` reports parsed session state, hashes, state paths, and lock
state. `doctor --json` reports the resolved harness executable, generated
prompt, argv, and model/effort metadata without invoking the harness. Text
`run` and `doctor` output print the selected model after `Harness:` and print
effort only when an explicit effort was supplied. `unlock --json` reports
whether a lock existed, whether it was removed, whether it was active or stale,
and the lock path.

## Pause Semantics

A paused run exits nonzero and leaves the plan lock released. Inspect the JSON
payload, `state.json`, `events.jsonl`, and the last turn artifacts, then rerun
the same command after resolving the issue.

Common pause reasons:

- `nonzero-exit`: harness command exited nonzero.
- `timeout`: harness exceeded `--timeout`.
- `no-status-change`: the pending session did not become complete.
- `multiple-status-changes`: more than one session changed during one turn.
- `unexpected-status-change`: a different session changed.
- `malformed-plan`: the plan could not be inspected or a status is missing.
- `unknown-status`: a handoff status is not `pending` or `complete`.
- `lock-contention`: another active runner holds the plan lock.
- `runner-pause`: the harness output contained a `RUNNER_PAUSE:` or paused
  runner status signal.
- `no-pending-but-incomplete`: no pending session exists, but not all sessions
  are complete.
- `commit-preflight`: auto-commit setup failed before the first harness turn,
  such as a dirty initial worktree, missing `spec`, non-git `--cwd`, or an
  incompatible `--commit-at-end` and `--max-sessions` combination.
- `commit-failed`: `spec commit --yes --no-verify` ran but did not create a new
  commit, including nonzero exits and zero-exit no-op commits that leave `HEAD`
  unchanged.

## Troubleshooting

Check what the runner sees:

```bash
agent-session-loop status ~/.local/share/agent-plans/example-plan/ --json
```

Preview the next command without invoking the harness:

```bash
agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex --dry-run --model gpt-5 --effort high
```

Inspect the most recent artifacts named in `state.json` or the JSON output:

```bash
jq '.turns[-1] | {stdout_path, stderr_path, last_message_path, result_path}' ~/.local/state/agent-session-loop/<plan-path-hash>/state.json
jq -r '.turns[-1].artifact_dir' ~/.local/state/agent-session-loop/<plan-path-hash>/state.json
jq -r '.turns[-1].result_path' ~/.local/state/agent-session-loop/<plan-path-hash>/state.json | xargs jq '{session, returncode, status_changes, runner_pause, plan_error}'
```

Follow the active or latest run:

```bash
agent-session-loop tail ~/.local/share/agent-plans/example-plan/
agent-session-loop tail ~/.local/share/agent-plans/example-plan/ --lines 80 --no-follow
```

Remove a stale lock:

```bash
agent-session-loop unlock ~/.local/share/agent-plans/example-plan/ --json
```

Remove an active lock only after verifying the owning process is not a real
runner you still need:

```bash
agent-session-loop unlock ~/.local/share/agent-plans/example-plan/ --force --json
```

If a plan has no pending sessions but is not complete, inspect every
`session-N.md` handoff and normalize statuses to exactly `pending` or
`complete`.

## Verification

Run the regression suite and compile check from this repository:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile agent_session_loop/*.py bin/.local/bin/agent-session-loop
```

Use a synthetic plan directory for manual smoke tests so generated state, locks,
and logs do not touch an active project plan.

Manual smoke checklist:

- Create a synthetic split plan, run with a fake harness that fails one turn,
  confirm the run pauses, then rerun successfully and confirm the failed and
  successful turns are preserved under different `runs/<run_id>/` directories.
- Make a required plan file unreadable or remove `context.md`, then confirm
  `run --json`, `status --json`, and `doctor --json` emit JSON failures without
  Python tracebacks.
- Run `doctor --json` against a valid synthetic plan and confirm it reports the
  resolved harness executable, prompt, and invocation argv.
- Create a synthetic active lock, confirm `unlock --json` reports
  `reason: active-lock`, then run `unlock --force --json` and confirm the lock
  is removed.
