---
name: session-run
description: Execute the next pending session from a split plan directory. Sequential by design — accepts only `<plan-dir>/` plus internal `--runner-managed`; explicit session targeting and parallel worker mode are not supported. Delegates authoritative status, readiness, and verification checks to `asl` scripts; `asl run` invokes this skill with `--runner-managed` so Python owns postflight enforcement. Companion to session-split and session-expand.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Session Run

## Purpose

Execute one session from a split plan directory. The skill is sequential by design: it always picks the first session with status `pending` from `asl status`. There is no explicit-session mode, no direct-session-file mode, and no parallel worker mode. One session per invocation — the user controls pacing by invoking this skill once per conversation.

## Input

The user provides a plan directory path as the host agent command or skill invocation argument.

`asl run` appends `--runner-managed` to the skill invocation it sends to the harness. Treat that flag as an internal execution-mode marker, not as user-facing syntax: in runner-managed mode the Python runner already holds the plan lock, has run the authoritative readiness gate (auto-repairing stale/missing `progress.md` and recomputable `manifest.md` readiness metadata before pausing), and will run verification, handoff revert, derived ledger sync, and readiness refresh after this skill returns. The skill itself must still never write `progress.md` (see Step 6d).

Runner-managed prompts may carry extra context beyond the first invocation line. When `asl run --reply TEXT` resumes a paused session, the runner prepends the `<skill-prefix>session-run <plan-dir>/ --runner-managed` invocation on line 1 and appends an `## ASL reply context` block containing the operator reply, the previous pause metadata, the paused turn's last message, and resume instructions. The first line is the only CLI invocation — everything below the `## ASL reply context` heading is prompt context for this skill, not additional CLI syntax. Do not try to parse the reply block as flags or other tokens; parse only the first line for the plan directory and the optional `--runner-managed` marker.

Determine `<skill-prefix>` once for the active harness and use it for every user-facing skill invocation you print:

- Codex: `$`
- Copilot and Claude: `/`
- Unknown harness: use the prefix from the user's invocation; if no prefix is visible, ask before printing a ready-to-paste skill invocation

Only prefix user-facing skill invocations. Do not prefix YAML names, skill directory paths, filenames, internal prose references to skill names, or shell commands that are not skill invocations.

When printing commands, substitute the resolved prefix; never print the literal `<skill-prefix>` placeholder.

Examples:

```
$session-run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/
/session-run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/
```

Accepted input forms:

- **Directory auto-advance**: `<plan-dir>/` selects the first session with status `pending` in dotted numeric order. This is the only supported invocation form.
- **Runner-managed suffix**: the input may end with `--runner-managed` when invoked by `asl run`; remove this marker before resolving the plan directory path, but keep the execution mode.

Explicit session selectors (`--session <ID>`) and direct session file paths (`<plan-dir>/session-<ID>.md`) are not accepted by this skill. Reject them in Step 1. Plan-maintenance operations against a specific session belong to `session-resplit`, not `session-run`.

## Instructions

### Step 1: Parse Input

Trim trailing whitespace from the invocation argument and determine the execution mode:

- **Runner-managed marker**: if the final token is exactly `--runner-managed`, remove it and set `runner_managed=true` before resolving the plan directory.
- **Directory auto-advance mode**: the remaining input must be a single plan directory path with no other tokens.

Reject malformed inputs and stop without making changes:

- Any `--session` flag or session ID positional argument (no longer supported by this skill)
- A path ending in `session-<ID>.md` (no longer supported by this skill)
- Any other unknown flags or extra positional arguments
- `--runner-managed` anywhere except the final token

Verify the resolved plan directory exists and contains `manifest.md` and `context.md`.

When `runner_managed=true` and the input is rejected, emit `Runner status: paused` followed by `RUNNER_PAUSE: malformed-plan - unsupported session-run input; expected <plan-dir>/ [--runner-managed]` so the Python runner records a structured pause instead of `no-status-change`.

### Step 1a: Honor Runner-Managed Reply Context

If the prompt contains an `## ASL reply context` block (always immediately after the first invocation line in runner-managed reply mode), treat that block as authoritative operator-supplied answers for the paused session. The block's structure is:

- `### Operator reply` — the literal text the operator passed to `asl run --reply TEXT`.
- `### Previous pause` — `Reason`, `Message`, `Session ID`, `Turn index`, `Run ID` bullets describing the prior pause.
- `### Pause details` — JSON object (sorted keys) or `(none)` when empty.
- `### Previous paused last message` — the verbatim last-message text from the paused turn (no code fence).
- `### Instructions` — explicit resume rules.

Behavior:

- Use the `### Operator reply` text as the answer to whatever interactive question paused the previous turn. Do not ask the user again. Do not reopen the question to the operator before consulting the reply.
- The runner injects this prompt only on the **first** resumed turn after `--reply`; turn 2+ on the same run carry the plain session prompt with no reply block. Do not expect the block to repeat.
- The operator reply text is not persisted in `events.jsonl` or `state.json` — only its character count via `REPLY_PROVIDED_EVENT`. Treat the in-prompt block as the single source of truth for the reply text within this turn.
- If the reply answers all blocking questions, proceed through Step 5 (Implement) using those answers and complete the session normally.
- If the reply is genuinely incomplete or contradictory and you cannot resolve the remaining ambiguity from the reply text plus existing plan context, stop before mutating the handoff and emit:

  ```text
  Runner status: paused
  RUNNER_PAUSE: blocked - interactive decision gates require user answers
  ```

  Then exit. The runner records the pause and the operator can rerun `asl run --reply TEXT` with a more complete answer.

- Never escalate to a new question when the reply already supplies the answer — pausing again after a complete reply wastes a runner turn.

### Step 2: Read Context and Manifest

Read `context.md` from the plan directory. Hold this as shared context for the session implementation.

Read `manifest.md` from the plan directory. Treat the manifest's session-status table as derived bookkeeping; final handoff statuses in the session files remain authoritative for completion. Older plans may still contain a `## Parallel Execution` block (with `### Dependency Waves`, `### Session Ownership`, or worker-command subsections) — ignore those sections. This skill no longer schedules parallel work.

Do not read `parallel-validation.md` even if it exists on disk. This skill no longer consults that file.

### Step 2.5: Read the Progress Journal

Check for `<plan-dir>/progress.md`. If it exists, read it as the primary cross-session digest: it summarizes what each completed session changed, decided, discovered, and flagged. Use it instead of opening every prior session file.

Treat the journal as best-effort context:

- Format: a `# Progress Journal` header with the `<!-- agent-session-loop progress-journal v1 -->` schema-version comment, followed by one `## Session <id> — <status> (<completed_at>)` block per completed session. Each block carries `- Files touched:`, `- Decisions:`, `- Discovered facts:` (indented bullets or `none`), `- Open questions:` (indented bullets or `none`), and `- Blockers:` lines. The em-dash in headings is `—` (U+2014), not a hyphen.
- If `progress.md` is absent (older plans, or fresh plans where no session has completed yet), fall back to reading the `## Handoff` block of each completed prior session directly. The journal is always optional context.
- `progress.md` is **runner-managed** and **excluded** from `plan_content_sha256` / `stable_readiness_sha256`. Reading or skipping it cannot change readiness state, and sessions never write to it (see Step 6d).
- For deep detail on any one prior session, still consult its `## Handoff` block; the journal is the distilled digest, the handoff is the source of truth.

### Step 3: Build Session Status Map

Run the status script and use its JSON as the authoritative session list and handoff status map:

```text
asl status <plan-dir> --json
```

If `asl status` exits non-zero or the JSON has `ok: false`, stop without making changes. Use the JSON `reason`/`error` fields in the report and emit a `RUNNER_PAUSE: malformed-plan - <summary>` line when `runner_managed=true`.

Select the next session using `next_pending_id` from the JSON payload; it is already resolved by the same parser used by `asl run`. If no session has status `pending`, every session in `sessions` is `complete`: print `Next command: All sessions implemented - nothing left to run.` and stop.

### Step 4: Load Session

Read `session-<ID>.md` for the selected session.

Also check for `session-<ID>.expand.md`. If it exists, read it — this contains verified file paths, corrected signatures, and codebase patterns discovered by `session-expand`. Treat expansion notes as authoritative corrections to the session plan.

If the selected session is not the first session in the sorted status map, also read the **Handoff** section from the immediately preceding session file for context. Do not derive the previous session with integer arithmetic; use the sorted session ID list from `asl status`.

Older split plans may still contain a `## Parallel Metadata` block at the top of each session file. Ignore it. This skill does not validate parallel safety, ownership, or required-prior-session metadata from session files — `asl status` resolves the next pending session, and `asl preflight` enforces plan-level readiness.

### Step 4a: Pre-Implementation Readiness Checks

Before executing any implementation work, delegate plan-wide safety checks to the Python scripts. Each pause here happens **before** the handoff is touched, so no revert is needed — emit the pause line, do not run Step 5, do not modify the handoff, and let Step 7 print the report with the `Next command:` line pointing back at the same plan directory.

When `runner_managed=true`:

- Do not run `asl preflight`.
- Do not recompute readiness SHA values in the skill.
- Trust the active `asl run` process to own the lock, readiness lifecycle, verification gate, handoff revert, derived ledger sync, and readiness refresh.
- You may still surface non-blocking context from expansion files in the final report.

When `runner_managed=false`, run:

```text
asl preflight <plan-dir> --json
```

Stop unless the command exits zero, `ok` is true, and the payload's readiness state is `loop-ready`. Emit:

```text
RUNNER_PAUSE: unsafe - plan is not loop-ready; run `asl preflight <plan-dir> --mark-loop-ready` (auto-backfills stale/missing progress.md) or `asl run <plan-dir>` (self-heals derived progress/readiness drift before pausing) before continuing
```

Do not repair readiness from the skill. `asl preflight --mark-loop-ready` is the canonical manual repair: it auto-backfills `progress.md` when the only blockers are `progress-journal-missing` and then marks the plan loop-ready. `asl run` performs the same auto-repair from inside the loop. Non-derived findings (contracts, paths, locks, etc.) still pause; if `preflight` reports contract findings, surface its `repair_hints` rather than interpreting `session-<ID>.expand.md` contract prose as authoritative.

### Step 5: Implement

Execute the session's instructions — everything above the `## Handoff` section in the session file. Use the context from `context.md`, expansion notes (if any), and the immediately preceding handoff (if any) to inform the implementation.

Follow all instructions in the session file faithfully. Make real code changes, run tests if specified, and handle edge cases described in the session.

### Step 6: Update Handoff

Edit the Handoff section in `session-<ID>.md` with actual results. The expected schema is:

```markdown
## Handoff

- **Status**: complete
- **Files modified**: …
- **Key decisions**: …
- **Blockers for next session**: …
- **Notes**: …

### Discovered Facts
- Lockfile X uses Y format (not Z as the plan assumed).
- Retry path is at foo/bar.py:142, not foo/baz.py.

### Open Questions
- Should we cache the parser output? Defer to whoever owns perf.
```

Required fields (always present, in this order):

- **Status**: `complete` on success
- **Files modified**: list each file with a brief description of what changed
- **Key decisions**: any notable choices made during implementation
- **Blockers for next session**: issues the next session should be aware of
- **Notes**: anything else relevant

Optional `###` subsections — include each only when there is content to record; omit the heading entirely otherwise:

- **`### Discovered Facts`** — novel findings, surprises, or corrections (file paths, signatures, API quirks, behavior the plan assumed wrong). One terse, top-level `- ` bullet per fact. Indented sub-bullets are silently dropped by the parser, so keep the list flat with no leading whitespace.
- **`### Open Questions`** — explicit uncertainties handed forward to a later session or to the user. One terse, top-level `- ` bullet per question. Same flat-bullet rule as above.

Heading match is case-sensitive and exact. Use the literal strings `### Discovered Facts` and `### Open Questions`; these mirror the `DISCOVERED_FACTS_HEADING` / `OPEN_QUESTIONS_HEADING` constants exported from `agent_session_loop.plans`, so the parser and skill docs cannot drift. File paths in `Files modified` must not contain literal commas (the runner CSV-parses that field when synthesizing the journal entry).

Use the Edit tool to update the handoff in place — do not rewrite the entire file. The selected session handoff remains the required completion mutation and the source of truth.

### Step 6a: Verify Before Final Commit

Mirror the `asl run` loop policy without duplicating runner-managed postflight.

When `runner_managed=true`, **do not run `asl verify` from the skill** and **do not revert handoff status from the skill**. The Python runner will inspect the handoff after this turn, run the same verification engine, revert to `pending` with a failure note on any contract or verification failure, sync derived ledgers after success, and refresh readiness metadata before the next turn.

When `runner_managed=false`, after writing the `complete` handoff in Step 6, invoke `asl verify` to execute the session's `Verify` recipe. The skill is the manual path that runs one session per harness invocation; the runner is the loop path. Both must enforce the same completion contract through the same `asl verify` script.

Invocation:

```text
asl verify <plan-dir> --session <ID> --cwd <repo-root> --json
```

- `--session <ID>` is required and must be the exact selected session id (e.g., `2`, `2.1`). It is an internal argument to `asl verify`, not a user-facing `session-run` mode.
- `--cwd <repo-root>` should point at the repository root where the implementation work happened. When the skill is invoked from inside that repository, pass the current working directory; otherwise omit `--cwd` and let `asl verify` use its own cwd.
- `--json` is mandatory because the skill needs the stable `reason` field for branching.
- Exit code is the gate decision: `0` means every check passed; any non-zero exit is a hard pause. The skill can rely on exit code alone for the gate decision.

Parse the JSON body to read the stable `reason` field. Recognized failure reasons are `verify-failed`, `no-contract`, `malformed-contract`, `cwd-error`, and `invalid-session`. On a pass, the JSON shape is `{"ok": true, "command": "verify", "reason": "ok", "passed": true, ...}`. On failure, it has `ok: false`, `paused: false`, a stable `reason`, an `error` string, and — for `verify-failed` and `no-contract` — the same `passed`, `recipe`, and `checks` keys as the pass payload. Per-check rows include `stdout`/`stderr` snippets and a stable `failure_reason` enum (`command-failed`, `command-timeout`, `command-not-found`, `command-error`, `file-missing`, `file-error`, `regex-not-matched`); mirror these names verbatim if the skill prints check summaries.

On any non-zero exit, fail closed without advancing:

1. Use the Edit tool to revert the handoff `- **Status**: complete` line back to `- **Status**: pending` in `session-<ID>.md`. Do not rewrite the entire handoff — flip just the status line.
2. Append a short bullet under `- **Notes**:` summarizing the failure. Prefer the JSON `error` string and a short per-check summary when available, e.g., `- verification failed: <reason> - <error>` or `- verification failed: 1/3 checks failed (first failure: <name> - <failure_reason>)`.
3. Emit `RUNNER_PAUSE: <reason> - <short summary>` using the JSON `reason` field verbatim so it greps cleanly alongside `asl run` loop output.
4. Skip Step 6b and Step 6c. Proceed to Step 7 and print the report, but **do not** advance the `Next command:` to another session — Step 7 will naturally point back at the same plan directory because the handoff was reverted to `pending` and `asl status` will resolve `next_pending_id` to the same session.

`reason: no-contract` handling: the runner skips verification entirely when the operator passed `--allow-not-loop-ready` and the session lacks a `## Completion Contract` section. The skill has no equivalent flag and therefore must fail closed by default with the same revert + pause sequence above. Users who need to bypass the contract requirement should switch to `asl run --allow-not-loop-ready`, or repair the plan first by adding a completion contract and re-marking the plan loop-ready via `session-split` Step 10a / `asl preflight --mark-loop-ready`.

`reason: cwd-error` and `reason: invalid-session` indicate the skill itself called `asl verify` incorrectly (bad `--cwd` or bad `--session`); revert the handoff and pause exactly the same way, but the corrective action is for the skill caller to fix the invocation, not the plan.

If verification passes (`ok: true`, exit `0`), continue to Step 6b. Do not write a parallel verification artifact — the `## Handoff` mutations plus any real pause line are the complete record. The runner writes a per-turn `result.json` under a `verification` key; the skill does not need to duplicate that.

If text output is captured instead of JSON (e.g., legacy invocation without `--json`), `asl verify` writes the pass/fail summary on **stderr** when verification fails and on **stdout** when it passes. Always capture both streams.

### Step 6b: Inject Notes for Future Sessions

If during implementation you produced guidance, corrections, or discoveries that a **future** session needs (e.g., "Notes for S3", "Heads-up for S4", "S5 should use the new helper at X"), do **not** leave them inline in your chat response. They must be written into the target session file so the future `<skill-prefix>session-run` invocation actually sees them.

For each note targeted at session `<TARGET_ID>` where `<TARGET_ID>` sorts after the current selected `<ID>` in dotted numeric order:

- If `session-<TARGET_ID>.expand.md` exists, append the note to it under a clear heading like `## Notes from session-<ID>`. Expansion files are the right home for verified facts, corrected paths, and codebase discoveries.
- If `session-<TARGET_ID>.expand.md` does not exist, append the note to `session-<TARGET_ID>.md` under a `## Notes from session-<ID>` section placed **above** the `## Handoff` section (so it is part of the instructions that get executed, not the handoff results).

Use the Edit tool to make these injections. Never just print "Notes for S3: ..." in the report — that information will be lost the moment the conversation ends.

The current session's own handoff (Step 6) is for _results of this session_. Step 6b is for _instructions/context for later sessions_. Keep these separate.

### Step 6c: Sync Derived Status Ledgers

After the selected handoff is updated and any future-session notes are injected, sync derived ledgers only when the Python runner is not already doing it.

When `runner_managed=true`, skip this step. `asl run` rebuilds the status map from final handoff blocks and syncs recognized derived status tables after verification succeeds. Skipping here avoids racing the runner and keeps postflight behavior centralized.

When `runner_managed=false`, rebuild the status map from final handoff blocks and sync recognized derived status tables. `session-run` is always directory auto-advance, so it is safe to sync `manifest.md` and `context.md` after every completed session. Always treat session handoffs as authoritative; `manifest.md` and `context.md` are derived bookkeeping only.

For `manifest.md`, update markdown tables that have a session ID column (`#`, `Session`, `Session #`, `Session number`, `Session ID`, or `ID`) and a `Status` column. Keep row order and non-status cells intact.

For `context.md`, update only clear markdown session-status tables with both a session ID column and a `Status` column. Do not rewrite prose, checklists, ambiguous tables, or unrelated uses of words like `pending`.

If a derived ledger cannot be updated safely, leave it unchanged and mention that in the report. Do not let stale derived tables change the next-session decision.

### Step 6d: Do Not Write the Progress Journal

Never edit `progress.md` from a session. The runner appends an entry automatically after parsing the just-updated handoff (`agent_session_loop.plans.append_progress_entry`) once verification has passed. The append is atomic, version-checked against the `<!-- agent-session-loop progress-journal v1 -->` schema comment, and idempotent against `(session_id, completed_at)`, so retries and runner reruns never double-write.

If you produced facts or questions that future sessions should see, record them in the handoff's `### Discovered Facts` / `### Open Questions` subsections (Step 6); if you have session-specific guidance, inject expansion notes (Step 6b). Do not bypass either of these by writing to `progress.md` directly — the journal is read-only from a session's perspective.

If a plan is missing `progress.md` entirely (older plans authored before the runner shipped journal support), use `asl backfill-progress <plan-dir>` from a separate manual step to reconstruct it from completed handoffs. If the journal exists but is stale or malformed, use `asl backfill-progress --force <plan-dir>` so the CLI replaces it atomically. Never recover it by hand from inside a session.

### Step 7: Report

Print a brief summary:

1. Which session was completed (ID and title)
2. Files modified (short list)
3. Derived status ledgers updated, if any (`manifest.md` and/or `context.md`)
4. What session comes next, or "All sessions implemented" if this was the last one
5. Any blockers or notes for the next session — and, for each, name the file you injected it into (e.g., "Injected note for S3 into session-3.expand.md"). Do not put the note content itself in the report; the report only confirms where it was written.

For runner-friendly output, you may print one or more machine-readable lines immediately before the final `Next command:` line:

- `Runner status: complete` after a successful session handoff update.
- `Runner status: paused` when stopping without completing the selected session because user input, validation, plan repair, or another recoverable intervention is required.
- `RUNNER_PAUSE: <reason> - <short detail>` to describe a pause in a stable, grep-friendly form. Prefer existing fail-closed vocabulary such as `malformed-plan`, `unknown-status`, `lock-contention`, `blocked`, `unsafe`, `verify-failed`, `no-contract`, or `malformed-contract` when it applies. Do not invent new reason strings; reuse the runner-side vocabulary so manual and loop output grep together.

These runner-readable lines are optional for manual use and must not replace the final `Next command:` line when that line is part of the report. Never print a `RUNNER_PAUSE:` line after a successful session, and never print placeholder lines such as `RUNNER_PAUSE: none`; the Python runner treats any `RUNNER_PAUSE:` line as a real pause.

In `runner_managed=true` mode, print `Runner status: complete` only after the selected handoff has been changed to `complete`. If you stop before making that handoff change, print `Runner status: paused` and a `RUNNER_PAUSE: <reason> - <detail>` line so the Python runner records a structured pause instead of `no-status-change`.

End every successful report with exactly one `Next command:` line:

- If any session still has status `pending` after the current handoff update, print `Next command: <skill-prefix>session-run <plan-dir>/`
- If no pending sessions remain, print `Next command: All sessions implemented - nothing left to run.`

Before printing this line, re-check the session statuses after the current handoff update so the next action reflects the actual updated plan directory state.

Use the original plan directory argument provided by the user for `<plan-dir>`. Always display `<plan-dir>` with a trailing slash for directory readability.

## Important Notes

- One session per invocation — never implement more than one session
- Honor `--runner-managed` as an internal mode from `asl run`: implement the next pending session and update its handoff, but leave readiness, verification, handoff revert, and derived ledger sync to the Python runner.
- When the prompt carries an `## ASL reply context` block (only on the first turn after `asl run --reply TEXT`), use the `### Operator reply` text as the answers to the paused question and resume the session without re-asking the operator. If the reply is genuinely incomplete, emit `Runner status: paused` and `RUNNER_PAUSE: blocked - interactive decision gates require user answers` before mutating the handoff.
- Use `asl status <plan-dir> --json` as the authoritative parser for session lists, final handoff statuses, and `next_pending_id`; do not reimplement that parser in prose when the script can answer it.
- In manual mode, use `asl preflight <plan-dir> --json` before implementation and `asl verify <plan-dir> --session <ID> --json` after marking the handoff complete. `--session <ID>` here is an internal `asl verify` argument; it is never a `session-run` invocation form.
- Always read `context.md` before starting implementation
- Always read `manifest.md`; treat its session-status table as derived bookkeeping, not the completion source of truth. Ignore any legacy `## Parallel Execution` section.
- Do not read `parallel-validation.md` even if it exists on disk. The skill no longer consults that file.
- Ignore any legacy `## Parallel Metadata` block at the top of a session file. The skill no longer validates parallel safety, ownership, or required-prior-session metadata from session files.
- Expansion notes (`session-<ID>.expand.md`) take precedence over the session file for factual claims (file paths, line numbers, function signatures)
- The session file is the source of truth for what to implement; expansion notes only correct factual details
- Treat expansion-file `## Contract Issues` as context only; canonical contract findings come from `asl preflight` before manual implementation and from runner/`asl verify` after completion.
- In manual mode, always run `asl verify <plan-dir> --session <ID> --json` (with `--cwd <repo-root>` when the repo root is known) after writing the `complete` handoff. Non-zero exit reverts the handoff to `pending`, appends a failure note, and emits `RUNNER_PAUSE: <reason> - <summary>` using the stable JSON `reason` field.
- The skill has no `--allow-not-loop-ready` equivalent. `reason: no-contract` fails closed; users who must bypass should use `asl run --allow-not-loop-ready` or repair the plan and re-mark it loop-ready.
- Never modify the original plan file
- Modify `context.md` only for clear derived session-status table sync described in Step 6c
- Read `progress.md` in Step 2.5 if it exists — it is the runner-maintained cross-session digest. When absent, fall back to reading prior `## Handoff` blocks directly.
- Never write to `progress.md`; the runner appends entries after verification (Step 6d). Record forward-looking facts in `### Discovered Facts` and forward-looking uncertainties in `### Open Questions` inside the handoff.
- `progress.md` is excluded from `plan_content_sha256` / `stable_readiness_sha256`, so reading or skipping it cannot change readiness state.
- If a session's status is already `complete`, skip it and continue to the next pending one. There is no way to re-run a completed session through this skill.
