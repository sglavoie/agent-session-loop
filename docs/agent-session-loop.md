# Agent Session Loop

`agent-session-loop` repeatedly invokes an agent harness against a split
session plan directory. Each harness turn runs one `$session-run` or
`/session-run` invocation, then the runner re-reads the plan files before
deciding whether to continue.

```bash
bin/.local/bin/agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex
bin/.local/bin/agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex --max-sessions 1
bin/.local/bin/agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex --dry-run
bin/.local/bin/agent-session-loop status ~/.local/share/agent-plans/example-plan/
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

## Commands

```text
agent-session-loop run PLAN_DIR --cwd REPO_ROOT [--harness codex|claude|copilot] [--timeout SEC] [--max-sessions N] [--state-dir DIR] [--unsafe-bypass] [--json] [--dry-run]
agent-session-loop status PLAN_DIR [--state-dir DIR] [--json]
agent-session-loop doctor --harness codex|claude|copilot --cwd REPO_ROOT --plan-dir PLAN_DIR
agent-session-loop unlock PLAN_DIR [--state-dir DIR] [--force]
```

`run` acquires a per-plan lock, inspects the first pending session, invokes the
selected harness, records turn artifacts, and verifies that exactly the expected
session changed from `pending` to `complete`.

`status` reads durable plan files and prints session counts, the next pending
session, content hashes, state paths, and lock state. Use `--json` for scripts.

`doctor` performs a non-mutating harness and path check.

`unlock` removes a stale lock. It refuses to remove an active lock unless
`--force` is supplied.

## Harness Commands

The built-in adapters construct argv lists directly and never use shell
interpolation.

Codex:

```text
codex exec -C <cwd> --add-dir <plan-dir> -s workspace-write -o <last-message-file> <prompt>
```

Claude:

```text
claude --print --add-dir <plan-dir> --permission-mode dontAsk <prompt>
```

Copilot:

```text
copilot -C <cwd> --add-dir <plan-dir> --allow-all-tools --allow-all-paths --no-ask-user --silent -p <prompt>
```

Harness prompts use the expected skill prefix:

```text
$session-run <plan-dir>/
/session-run <plan-dir>/
```

Codex writes a last-message file with `-o`. Claude and Copilot do not use that
file, so their last-message capture falls back to stdout.

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
- `events.jsonl`: append-only run, pause, and turn events.
- `lock.json`: active per-plan runner lock.
- `stdout-N.log`: stdout from the harness turn for session `N`.
- `stderr-N.log`: stderr from the harness turn for session `N`.
- `last-message-N.txt`: final harness message when available.
- `result-N.json`: diagnostic result for session `N`, including argv, status
  changes, runner signals, and artifact paths.

## JSON Output

Use `--json` for runner integrations:

```bash
agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex --max-sessions 1 --json
agent-session-loop status ~/.local/share/agent-plans/example-plan/ --json
```

Important fields include `ok`, `paused`, `reason`, `pause_reason`,
`completed_sessions`, `turns`, `initial_snapshot`, `final_snapshot`,
`state_path`, and `events_path`.

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

## Troubleshooting

Check what the runner sees:

```bash
agent-session-loop status ~/.local/share/agent-plans/example-plan/ --json
```

Preview the next command without invoking the harness:

```bash
agent-session-loop run ~/.local/share/agent-plans/example-plan/ --cwd "$PWD" --harness codex --dry-run
```

Inspect the most recent artifacts named in `state.json` or the JSON output:

```bash
jq '.turns[-1] | {stdout_path, stderr_path, last_message_path, result_path}' ~/.local/state/agent-session-loop/<plan-path-hash>/state.json
```

Remove a stale lock:

```bash
agent-session-loop unlock ~/.local/share/agent-plans/example-plan/
```

Remove an active lock only after verifying the owning process is not a real
runner you still need:

```bash
agent-session-loop unlock ~/.local/share/agent-plans/example-plan/ --force
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
