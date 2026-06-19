---
name: session-run
description: Execute the next pending or explicitly selected session from a split plan directory. Supports parallel-safe explicit session scheduling with dependency validation while preserving serial auto-advance. Runs `asl verify` after each session and pauses on contract issues, readiness divergence, or verification failure. Companion to session-split and session-expand.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Session Run

## Purpose

Execute one session from a split plan directory. Directory-only input preserves the serial auto-advance behavior by picking the first session with status `pending`. Explicit input runs exactly the selected session after dependency and validation checks, which lets a coordinator assign parallel-safe workers without racing on auto-pick. One session per invocation — the user controls pacing by invoking this skill once per conversation.

## Input

The user provides a plan directory path, a plan directory plus an explicit session selector, or a direct session file path as the host agent command or skill invocation argument.

Determine `<skill-prefix>` once for the active harness and use it for every user-facing skill invocation you print:

- Codex: `$`
- Copilot and Claude: `/`
- Unknown harness: use the prefix from the user's invocation; if no prefix is visible, ask before printing a ready-to-paste skill invocation

Only prefix user-facing skill invocations. Do not prefix YAML names, skill directory paths, filenames, internal prose references to skill names, or shell commands that are not skill invocations.

When printing commands, substitute the resolved prefix; never print the literal `<skill-prefix>` placeholder.

Examples:

```
$session-run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/
$session-run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/ --session 2
$session-run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/ --session 2.1
$session-run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/session-2.md
$session-run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/session-2.1.md
/session-run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/
/session-run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/ --session 2
/session-run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/ --session 2.1
/session-run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/session-2.md
/session-run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/session-2.1.md
```

Accepted input forms:

- **Directory auto-advance**: `<plan-dir>/` keeps the serial workflow and selects the first pending session.
- **Explicit flag**: `<plan-dir>/ --session <ID>` runs exactly `session-<ID>.md`, where `<ID>` is a session ID such as `2` or `2.1`.
- **Direct session file**: `<plan-dir>/session-<ID>.md` runs exactly that session.

Concurrent auto-advance invocations are unsafe because multiple workers can select the same pending session. Parallel workers must use an explicit session target assigned by a coordinator.

## Instructions

### Step 1: Parse Input

Trim trailing whitespace from the invocation argument and determine the execution mode:

- **Directory auto-advance mode**: the input is a directory path with no extra selector.
- **Explicit flag mode**: the input is a directory path followed by exactly `--session <ID>`, where `<ID>` matches `[1-9][0-9]*(\.[1-9][0-9]*)*`.
- **Direct file mode**: the input is a path ending in `session-<ID>.md`; derive the plan directory from the file's parent directory and the selected session ID from the filename.

Reject malformed inputs and stop without making changes:

- Unknown flags or extra positional arguments
- `--session` without a valid session ID
- A direct file path whose basename is not exactly `session-<ID>.md` with a valid session ID
- A selected session file that does not exist

Verify the resolved plan directory exists and contains `manifest.md` and `context.md`.

### Step 2: Read Context and Manifest

Read `context.md` from the plan directory. Hold this as shared context for the session implementation.

Read `manifest.md` from the plan directory. If it contains `## Parallel Execution`, `### Dependency Waves`, `### Session Ownership`, or parallel worker commands, hold that metadata as scheduling guidance. The manifest is guidance, not the completion source of truth; handoff statuses in the session files remain authoritative for completion.

If `<plan-dir>/parallel-validation.md` exists, read it before selecting or implementing the session. Treat it as authoritative over manifest and session-file parallel metadata for dependency, ownership, and safety claims.

### Step 3: Build Session Status Map

Build the exact session file list first (`session-<ID>.md`, excluding `session-<ID>.expand.md`). Session IDs must match `[1-9][0-9]*(\.[1-9][0-9]*)*`.

Sort session files by dotted numeric order: compare dot-separated components as integers, and when all compared components are equal, the shorter ID sorts first. For example, `2` sorts before `2.1`, and `2.10` sorts after `2.2`.

For each session file, read the status from the final exact `## Handoff` block:

- Find the last line whose trimmed text is exactly `## Handoff`.
- Within that block, read the first line that starts with `- **Status**:`.
- Ignore any `- **Status**:` lines above the final handoff, including preserved original-plan templates such as `**Handoff (fill on completion)**`.
- Treat a missing final handoff or missing populated status line as a malformed plan.

Build `(session-id, status)` pairs from those final handoff statuses. Valid runnable statuses are exactly `pending` and `complete`.

- If **no session files exist**: print an error and stop.

In **directory auto-advance mode**, find the first session with status `pending` in dotted numeric order.

- If **no sessions are pending** (all are `complete`): print `Next command: All sessions implemented - nothing left to run.` and stop.

In **explicit mode**, select only the requested session ID:

- If the selected session is missing from the status map, print an error and stop.
- If the selected session is already `complete`, print an error and stop.
- If the selected session status is anything other than `pending`, print an error and stop.
- Never fall back to another pending session.

### Step 4: Load Session

Read `session-<ID>.md` for the selected session.

Also check for `session-<ID>.expand.md`. If it exists, read it — this contains verified file paths, corrected signatures, and codebase patterns discovered by `session-expand`. Treat expansion notes as authoritative corrections to the session plan.

Read the selected session's `## Parallel Metadata` block if present. In explicit mode, use it together with `manifest.md` and optional `parallel-validation.md` to validate that this exact session may run now.

Explicit-session validation fails closed. Stop without implementing or updating any handoff if:

- `parallel-validation.md` marks the selected session as blocked, unsafe to run now, ownership-conflicted, or dependent on unresolved work.
- `parallel-validation.md` adds required prior session IDs that are not complete.
- The session's `Required prior sessions` metadata lists prior session IDs whose handoff status is not `complete`.
- The selected session has a prerequisite implied by concrete implementation instructions and that prerequisite is not complete.

Manifest/session safety classifications guide parallel scheduling but do not by themselves forbid serial explicit execution after all required prior sessions are complete. For example, `serial-required` means "do not run concurrently"; it does not mean "never run explicitly." Validation notes can still block execution when they identify unresolved dependencies or ownership conflicts.

In directory auto-advance mode, if the selected session is not the first session in the sorted status map, also read the **Handoff** section from the immediately preceding session file for context. Do not derive the previous session with integer arithmetic; use the sorted session ID list.

In explicit mode, read the **Handoff** section for every required prior session ID. Also read the immediately preceding session file in dotted numeric order if it is complete or if metadata/validation/instructions list it as required. A pending previous session is not automatically a blocker unless required by metadata, validation notes, or concrete implementation dependencies.

### Step 4a: Pre-Implementation Contract and Readiness Checks

Before executing any implementation work, run these fail-closed checks against the artifacts emitted by `session-expand`. Each pause here happens **before** the handoff is touched, so no revert is needed — emit the pause line, do not run Step 5, do not modify the handoff, and let Step 7 print the report with the `Next command:` line pointing back at the same selected session.

- **Contract issues** (per-session, from `session-<ID>.expand.md`): if a `## Contract Issues` section exists for the selected session and is **not** the literal `_no contract issues found_`, stop. Emit:

  ```text
  RUNNER_PAUSE: malformed-contract - expansion flagged contract issues for session <ID>; run `asl verify <plan-dir> --session <ID>` for the canonical finding set
  ```

  Do not try to fix contract bodies during session-run. The user repairs them through `session-split` Step 10a or by hand, then re-runs `session-expand` and this skill.

  If the `## Contract Issues` section is missing entirely (e.g., the expansion file pre-dates session 11), treat the contract as "not checked" — proceed but mention in the report that expansion did not validate the contract.

- **Plan readiness snapshot** (plan-wide, from `parallel-validation.md`): treat any `## Plan Readiness` section as a stale-prone snapshot, not as a hard gate. The Python runner already holds the active plan lock and has run the authoritative preflight gate before invoking this skill, so do not run full `asl preflight` here. Instead, read the current manifest readiness metadata and recompute the current structural SHA with `agent_session_loop.plans.stable_readiness_sha256(plan_dir)`.

  Stop only when the current manifest metadata is missing or malformed, the current readiness SHA differs from the recomputed SHA, or the current readiness state is neither `loop-ready` nor `running`. Emit:

  ```text
  RUNNER_PAUSE: unsafe - plan readiness is not current; run `asl preflight --mark-loop-ready` before continuing
  ```

  A `running` state is valid when this skill is invoked by `asl run`; `loop-ready` is valid for direct manual `/session-run` or `$session-run` invocations. If the snapshot says `Divergence: recorded SHA differs...` but the current manifest SHA matches the recomputed SHA, continue and mention in the report that `parallel-validation.md` has stale readiness text.

- **Preflight snapshot** (plan-wide, from `parallel-validation.md`): treat any `## Preflight Snapshot` section as **informational only**. Red counts > 0 do not block explicit-mode session execution because `asl run`'s hard gate already enforces `loop-ready`. Surface the snapshot in the report so the user understands the pre-execution state, but do not duplicate the gate decision.

### Step 5: Implement

Execute the session's instructions — everything above the `## Handoff` section in the session file. Use the context from `context.md`, manifest scheduling guidance, validation notes (if any), expansion notes (if any), and required prior handoffs (if any) to inform the implementation.

Follow all instructions in the session file faithfully. Make real code changes, run tests if specified, and handle edge cases described in the session.

Explicit mode runs exactly the selected session. Do not auto-advance to another session within the same invocation, even if the selected session finishes quickly.

### Step 6: Update Handoff

Edit the Handoff section in `session-<ID>.md` with actual results:

- **Status**: complete
- **Files modified**: list each file with a brief description of what changed
- **Key decisions**: any notable choices made during implementation
- **Blockers for next session**: issues the next session should be aware of
- **Notes**: anything else relevant

Use the Edit tool to update the handoff in place — do not rewrite the entire file. The selected session handoff remains the required completion mutation and the source of truth.

### Step 6a: Verify Before Final Commit

Mirror the `asl run` loop policy: after writing the `complete` handoff in Step 6, invoke `asl verify` to execute the session's `Verify` recipe. The skill is the manual path that runs one session per harness invocation; the runner is the loop path. Both must enforce the same completion contract so manual and loop behavior stay consistent.

Invocation:

```text
asl verify <plan-dir> --session <ID> --cwd <repo-root> --json
```

- `--session <ID>` is required and must be the exact selected session id (e.g., `2`, `2.1`).
- `--cwd <repo-root>` should point at the repository root where the implementation work happened. When the skill is invoked from inside that repository, pass the current working directory; otherwise omit `--cwd` and let `asl verify` use its own cwd.
- `--json` is mandatory because the skill needs the stable `reason` field for branching.
- Exit code is the gate decision: `0` means every check passed; any non-zero exit is a hard pause. The skill can rely on exit code alone for the gate decision.

Parse the JSON body to read the stable `reason` field. Recognized failure reasons are `verify-failed`, `no-contract`, `malformed-contract`, `cwd-error`, and `invalid-session`. On a pass, the JSON shape is `{"ok": true, "command": "verify", "reason": "ok", "passed": true, ...}`. On failure, it has `ok: false`, `paused: false`, a stable `reason`, an `error` string, and — for `verify-failed` and `no-contract` — the same `passed`, `recipe`, and `checks` keys as the pass payload. Per-check rows include `stdout`/`stderr` snippets and a stable `failure_reason` enum (`command-failed`, `command-timeout`, `command-not-found`, `command-error`, `file-missing`, `file-error`, `regex-not-matched`); mirror these names verbatim if the skill prints check summaries.

On any non-zero exit, fail closed without advancing:

1. Use the Edit tool to revert the handoff `- **Status**: complete` line back to `- **Status**: pending` in `session-<ID>.md`. Do not rewrite the entire handoff — flip just the status line.
2. Append a short bullet under `- **Notes**:` summarizing the failure. Prefer the JSON `error` string and a short per-check summary when available, e.g., `- verification failed: <reason> - <error>` or `- verification failed: 1/3 checks failed (first failure: <name> - <failure_reason>)`.
3. Emit `RUNNER_PAUSE: <reason> - <short summary>` using the JSON `reason` field verbatim so it greps cleanly alongside `asl run` loop output.
4. Skip Step 6b and Step 6c. Proceed to Step 7 and print the report, but **do not** advance the `Next command:` to another session — Step 7 will naturally point back at the same selected session because the handoff was reverted to `pending`.

`reason: no-contract` handling: the runner skips verification entirely when the operator passed `--allow-not-loop-ready` and the session lacks a `## Completion Contract` section. The skill has no equivalent flag and therefore must fail closed by default with the same revert + pause sequence above. Users who need to bypass the contract requirement should switch to `asl run --allow-not-loop-ready`, or repair the plan first by adding a completion contract and re-marking the plan loop-ready via `session-split` Step 10a / `asl preflight --mark-loop-ready`.

`reason: cwd-error` and `reason: invalid-session` indicate the skill itself called `asl verify` incorrectly (bad `--cwd` or bad `--session`); revert the handoff and pause exactly the same way, but the corrective action is for the skill caller to fix the invocation, not the plan.

If verification passes (`ok: true`, exit `0`), continue to Step 6b. Do not write a parallel verification artifact — the `## Handoff` mutations plus any `RUNNER_PAUSE:` line are the complete record. The runner writes a per-turn `result.json` under a `verification` key; the skill does not need to duplicate that.

If text output is captured instead of JSON (e.g., legacy invocation without `--json`), `asl verify` writes the pass/fail summary on **stderr** when verification fails and on **stdout** when it passes. Always capture both streams.

### Step 6b: Inject Notes for Future Sessions

If during implementation you produced guidance, corrections, or discoveries that a **future** session needs (e.g., "Notes for S3", "Heads-up for S4", "S5 should use the new helper at X"), do **not** leave them inline in your chat response. They must be written into the target session file so the future `<skill-prefix>session-run` invocation actually sees them.

For each note targeted at session `<TARGET_ID>` where `<TARGET_ID>` sorts after the current selected `<ID>` in dotted numeric order:

- If `session-<TARGET_ID>.expand.md` exists, append the note to it under a clear heading like `## Notes from session-<ID>`. Expansion files are the right home for verified facts, corrected paths, and codebase discoveries.
- If `session-<TARGET_ID>.expand.md` does not exist, append the note to `session-<TARGET_ID>.md` under a `## Notes from session-<ID>` section placed **above** the `## Handoff` section (so it is part of the instructions that get executed, not the handoff results).

Use the Edit tool to make these injections. Never just print "Notes for S3: ..." in the report — that information will be lost the moment the conversation ends.

The current session's own handoff (Step 6) is for _results of this session_. Step 6b is for _instructions/context for later sessions_. Keep these separate.

### Step 6c: Sync Derived Status Ledgers

After the selected handoff is updated and any future-session notes are injected, rebuild the status map from final handoff blocks and sync derived status tables where it is safe:

- In directory auto-advance mode, sync recognized status tables after every completed session.
- In explicit mode, if any session remains `pending`, skip shared `manifest.md`/`context.md` status-table sync to avoid concurrent workers racing on shared files.
- In explicit mode, if no sessions remain `pending`, sync recognized status tables as final bookkeeping.
- Always treat session handoffs as authoritative. `manifest.md` and `context.md` are derived bookkeeping only.

For `manifest.md`, update markdown tables that have a session ID column (`#`, `Session`, `Session #`, `Session number`, `Session ID`, or `ID`) and a `Status` column. Keep row order and non-status cells intact.

For `context.md`, update only clear markdown session-status tables with both a session ID column and a `Status` column. Do not rewrite prose, checklists, ambiguous tables, or unrelated uses of words like `pending`.

If a derived ledger cannot be updated safely, leave it unchanged and mention that in the report. Do not let stale derived tables change the next-session decision.

### Step 7: Report

Print a brief summary:

1. Which session was completed (ID and title)
2. Files modified (short list)
3. Derived status ledgers updated, if any (`manifest.md` and/or `context.md`)
4. What session comes next, or "All sessions implemented" if this was the last one
5. Any blockers or notes for the next session — and, for each, name the file you injected it into (e.g., "Injected note for S3 into session-3.expand.md"). Do not put the note content itself in the report; the report only confirms where it was written.

For runner-friendly output, you may print one or more machine-readable lines immediately before the final `Next command:` line:

- `Runner status: complete` after a successful session handoff update.
- `Runner status: paused` when stopping without completing the selected session because user input, validation, plan repair, dependency resolution, or another recoverable intervention is required.
- `RUNNER_PAUSE: <reason> - <short detail>` to describe a pause in a stable, grep-friendly form. Prefer existing fail-closed vocabulary such as `malformed-plan`, `unknown-status`, `lock-contention`, `blocked`, `unsafe`, `dependent`, `unresolved-prerequisite`, `verify-failed`, `no-contract`, or `malformed-contract` when it applies. Do not invent new reason strings; reuse the runner-side vocabulary so manual and loop output grep together.

These runner-readable lines are optional for manual use and must not replace the final `Next command:` line when that line is part of the report.

End every successful report with exactly one `Next command:` line:

- In directory auto-advance mode, if another pending session remains, print `Next command: <skill-prefix>session-run <plan-dir>/`
- In explicit mode, if another pending session remains and the coordinator/manifest/validation clearly identifies the next assigned explicit target, print `Next command: <skill-prefix>session-run <plan-dir>/ --session <ID>`
- In explicit mode, if another pending session remains but there is no clear next explicit target, print `Next command: <skill-prefix>session-run <plan-dir>/` and state above it that this is the serial fallback after active parallel workers have finished
- If no pending sessions remain, print `Next command: All sessions implemented - nothing left to run.`

Before printing this line, re-check the session statuses after the current handoff update so the next action reflects the actual updated plan directory state.

Use the original plan directory argument provided by the user for `<plan-dir>` when the input was a directory. Use the resolved parent directory when the input was a direct session file. Always display `<plan-dir>` with a trailing slash for directory readability.

## Important Notes

- One session per invocation — never implement more than one session
- Always read `context.md` before starting implementation
- Always read `manifest.md`; use its parallel metadata as guidance, not as the completion source of truth
- If `parallel-validation.md` exists, read it and treat it as authoritative over manifest/session parallel metadata
- Expansion notes (`session-<ID>.expand.md`) take precedence over the session file for factual claims (file paths, line numbers, function signatures)
- The session file is the source of truth for what to implement; expansion notes only correct factual details
- A non-clean `## Contract Issues` section in the expansion file is a pre-implementation pause with reason `malformed-contract`. Do not attempt to repair contract bodies during session-run.
- `## Plan Readiness` in `parallel-validation.md` is informational snapshot text. Before implementation, validate only the current manifest readiness state/SHA; do not advance the readiness lifecycle from this skill.
- `## Preflight Snapshot` in `parallel-validation.md` is informational; do not gate on it.
- Always run `asl verify <plan-dir> --session <ID> --json` (with `--cwd <repo-root>` when the repo root is known) after writing the `complete` handoff. Non-zero exit reverts the handoff to `pending`, appends a failure note, and emits `RUNNER_PAUSE: <reason> - <summary>` using the stable JSON `reason` field.
- The skill has no `--allow-not-loop-ready` equivalent. `reason: no-contract` fails closed; users who must bypass should use `asl run --allow-not-loop-ready` or repair the plan and re-mark it loop-ready.
- Never modify the original plan file
- Modify `context.md` only for clear derived session-status table sync described in Step 6c
- In directory auto-advance mode, if a session's status is already `complete`, skip it and move to the next pending one
- In explicit mode, never skip to another session; fail closed when the selected session cannot run
