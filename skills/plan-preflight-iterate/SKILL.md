---
name: plan-preflight-iterate
description: Guided Q&A repair of a split plan directory driven by `asl preflight --json` findings and their machine-readable repair hints. Asks targeted questions, applies edits, reruns preflight, and marks the plan loop-ready only after all checks are green.
allowed-tools: AskUserQuestion, Bash, Read, Edit, Write, Glob, Grep
---

# Plan Preflight Iterate

## Purpose

Repair a split plan directory through an interactive Q&A loop driven by `asl preflight --json` output. Each red and yellow finding carries a stable `repair_hints` entry — `check_id`, `severity_hint`, `fix_category`, `message`, and `question_template` — produced by `agent_session_loop.preflight`. This skill turns those hints into targeted user questions, applies the resulting edits to session and manifest files, reruns preflight to confirm progress, and only marks the plan `loop-ready` once every check is green.

Use this skill before `asl run` whenever preflight is not green. The companion skills are `session-split` (creates the plan directory), `session-expand` (annotates session files), and `session-run` (executes one session). This skill sits between split/expand and run, taking the plan from "not loop-ready" to "loop-ready".

## Input

The user provides a plan directory path. Optional flags pass through to `asl preflight`:

- `--cwd REPO_ROOT` — repo root for environment checks
- `--harness codex|claude|copilot` — harness to validate
- `--commit-per-session` or `--commit-at-end` — validate commit-mode prerequisites
- `--state-dir DIR` — override operational state directory
- `--max-pending-sessions N` — override the turn-budget pending-session cap

Determine `<skill-prefix>` once for the active harness and use it for every user-facing skill invocation you print:

- Codex: `$`
- Copilot and Claude: `/`
- Unknown harness: use the prefix from the user's invocation; if no prefix is visible, ask before printing a ready-to-paste skill invocation

Only prefix user-facing skill invocations. Do not prefix YAML names, skill directory paths, filenames, internal prose references to skill names, or shell commands that are not skill invocations.

When printing commands, substitute the resolved prefix; never print the literal `<skill-prefix>` placeholder.

Examples:

```
$plan-preflight-iterate ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/
$plan-preflight-iterate ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ --cwd ~/code/myrepo --harness claude
$plan-preflight-iterate ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ --commit-per-session
$plan-preflight-iterate ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ --max-pending-sessions 40
/plan-preflight-iterate ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/
/plan-preflight-iterate ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ --cwd ~/code/myrepo --harness claude
/plan-preflight-iterate ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ --commit-per-session
/plan-preflight-iterate ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ --max-pending-sessions 40
```

Reject malformed inputs and stop without changes:

- Missing plan directory argument
- Unknown flags
- More than one plan directory
- `--harness` with a value outside `codex`, `claude`, `copilot`
- Both `--commit-per-session` and `--commit-at-end` together
- `--max-pending-sessions` missing a positive integer value

## Instructions

### Step 1: Parse Input and Resolve Plan Directory

Parse the argument list. Verify the plan directory exists and contains `manifest.md`, `context.md`, and at least one canonical `session-<ID>.md` file. If the directory is missing or unreadable, print an error and stop — this skill repairs an existing split plan; it does not create one.

Record the optional `--cwd`, `--harness`, `--commit-mode`, `--state-dir`, and `--max-pending-sessions` values. They will be forwarded verbatim to every `asl preflight` invocation so that the same set of checks runs on every iteration.

### Step 2: Baseline Preflight Run

Run the baseline preflight using the Bash tool:

```text
asl preflight <plan-dir> --json [--cwd <repo>] [--harness <name>] [--commit-per-session|--commit-at-end] [--state-dir <dir>] [--max-pending-sessions <n>]
```

Capture stdout. Parse the JSON body. Expect the stable shape emitted by `agent_session_loop.preflight._preflight_payload_fields`:

- `ok` (bool) — `True` only when every finding is green
- `findings` (array) — each item has `severity`, `category`, `message`, optional `session_id`/`path`/`line`/`detail`
- `findings_by_severity` (counts: `red`, `yellow`, `green`)
- `repair_hints` (array) — one entry per finding in the same order, each with `category`, `check_id`, `severity_hint`, `message`, `question_template`, `fix_category`
- `readiness` (object | null), `expected_readiness_sha256` (string | null)
- `max_pending_sessions` (int | null)
- `mark_loop_ready_requested` (bool), `marked_loop_ready` (bool)

If exit code is non-zero and the JSON body contains a `reason` of `plan-structure`, print the error and stop — the plan directory itself is malformed and must be repaired or recreated with `session-split` before this skill can help.

If `ok` is `True` and the baseline already has no red/yellow findings, skip directly to Step 5 (Mark Loop-Ready). If the only red/yellow findings are `manifest-readiness` and/or `progress-journal-missing` findings, also skip to Step 5: `asl preflight --mark-loop-ready` is the canonical repair for missing readiness metadata, stale readiness SHA, and `draft`/`reviewed` readiness state, **and** it auto-backfills stale/missing `progress.md` (force semantics) when `progress-journal-missing` is the only remaining non-readiness blocker. Both repairs surface in the JSON payload's `auto_repairs` array.

Print a one-line baseline summary (e.g., `Baseline: 3 red, 2 yellow, 8 green`) so the user can see the starting state.

### Step 3: Triage Findings and Pair With Hints

Build an in-memory list of `(finding, hint)` pairs by zipping `findings` and `repair_hints` index-for-index. Drop green findings — they are informational and never block.

Sort the surviving pairs to keep the user's attention on the most impactful items first:

1. Red severity before yellow.
2. Within each severity, group by `fix_category` in this priority order so related repairs cluster together:
   - `plan-recreate`, `plan-edit`, `manifest-update`, `filename-rename`, `session-recipe-edit`, `env-state`, `env-install`, `invocation-fix`, `informational`
3. Within each `fix_category`, preserve preflight's original finding order.

Aggregate counts per `fix_category` and print a short triage table before opening Q&A so the user knows the shape of the repair work:

```
Repair plan:
- plan-edit: 2 (sessions 3, 5)
- manifest-update: 1
- env-state: 1
```

### Step 4: Iterative Repair Rounds

Run up to **5 rounds** of guided repair. Each round handles a small batch (1–3 findings) and then reruns preflight.

#### 4a. Pick Up to 3 Findings for This Round

Take the next 1–3 unresolved `(finding, hint)` pairs from the sorted triage list. Prefer at most one pair per `fix_category` per round so the user can switch contexts smoothly. If only one finding remains in the batch, still proceed — the round will be short.

#### 4b. Render the Questions

For each picked pair, build an `AskUserQuestion` entry using the hint's `question_template`. Substitute `str.format` keys from the finding's fields (`session_id`, `path`, `line`, `detail`, `message`). If a key is missing on the finding, substitute the empty string rather than failing — the templates are tolerant by design.

Choose the option set based on `fix_category`:

- `plan-edit`
  Question header: short category label (≤12 chars). Options:
  - `Walk me through it` / "Open the file and apply guided edits with my answers"
  - `Suggest a draft` / "Generate a proposed edit I can review and tweak"
  - `Skip for now` / "Leave this finding unresolved this round"

- `manifest-update`
  - `Run mark-loop-ready` / "Re-run `asl preflight --mark-loop-ready` once non-readiness blockers are clear"
  - `Edit manifest.md` / "Edit the readiness block manually"
  - `Skip for now` / "Leave this finding unresolved this round"

- `progress-journal-missing.backfill`
  - `Run backfill --force` / "Run `asl backfill-progress --force <plan-dir>` to rebuild `progress.md`, then rerun preflight and mark-loop-ready"
  - `Defer to mark-loop-ready` / "Skip the direct backfill; `asl preflight --mark-loop-ready` will auto-backfill in Step 5 when this is the only remaining non-readiness blocker"
  - `Skip for now` / "Leave this finding unresolved this round"

- `filename-rename`
  - `Apply the rename` / "Rename the file to the canonical name shown in the finding detail"
  - `Skip for now` / "Leave this finding unresolved this round"

- `session-recipe-edit`
  - `Edit the Verify recipe` / "Update the JSON Verify block in the session file"
  - `Skip for now` / "Leave this finding unresolved this round"

- `env-state`
  - `Run the suggested fix` / "Run `asl unlock`, stash dirty changes, etc."
  - `I'll fix it outside the skill` / "Pause this finding; I'll resolve it elsewhere"
  - `Skip for now` / "Leave this finding unresolved this round"

- `env-install`
  - `Show install instructions` / "Print the suggested install command and pause"
  - `Drop the dependent flag` / "Re-run preflight without `--commit-*` or harness flags"
  - `Skip for now` / "Leave this finding unresolved this round"

- `invocation-fix`
  - `Pick a different --cwd` / "Provide a new repo root and rerun preflight"
  - `Skip for now` / "Leave this finding unresolved this round"

- `plan-recreate`
  - `Stop and recreate the plan` / "Exit so I can rerun `session-split` from scratch"
  - `Skip for now` / "Leave this finding unresolved this round"

- `informational`
  - Do not ask a question; surface the message inline.

Open one `AskUserQuestion` call with all picked questions in the same batch.

#### 4c. Apply the User's Answers

Apply edits one finding at a time:

- **plan-edit**: Read the session file from `finding.path`. For missing/malformed `## Completion Contract` errors, build the contract scaffold using the parser shape (a `### Done when` bullet list and a fenced ```json``` Verify block whose `session` matches `session_id`). For ambiguity sentinels, jump to `finding.line` and use Edit to replace the sentinel with the user's wording. For path-reference findings, remove `..` traversal from the relative contract `path` field or replace it with an explicit absolute path. After every edit, re-read the file to confirm the change landed.

- **manifest-update**: When the user picked `Run mark-loop-ready`, defer it until Step 5. When they picked `Edit manifest.md`, open `manifest.md` and use Edit to fix the `**Readiness**`, `**Readiness SHA-256**`, or `**Readiness checked at**` line called out in the finding `message`/`detail`.

- **progress-journal-missing.backfill**: When the user picked `Run backfill --force`, run `asl backfill-progress --force <plan-dir>` via Bash; the command rewrites `progress.md` atomically from completed handoffs. Confirm the file exists after the call. When the user picked `Defer to mark-loop-ready`, take no action this round — Step 5 will auto-backfill via `asl preflight --mark-loop-ready` when no non-readiness/non-progress blockers remain.

- **filename-rename**: Use Bash to `git mv` (preferred when the plan directory is tracked) or `mv` the source to the canonical target named in the finding `detail` (which is always formatted as `rename to <target>`). Never rename outside the plan directory.

- **session-recipe-edit**: Open the session file. The Verify recipe is a fenced ```json``` block inside `## Completion Contract`. Edit only that block: bump timeouts, fix `path` values, relax regex patterns — never silently delete a check. If the user picked `Skip for now`, leave the recipe alone.

- **env-state**: For stale or active locks, run the suggested `asl unlock <plan-dir>` command. For dirty worktrees, print the `git status --short` summary and pause for the user to stash/commit outside the skill — do not stash on their behalf. Mark the finding as deferred if the user chose `I'll fix it outside the skill`.

- **env-install**: Print the suggested install hint from the hint `message` and pause. If the user picked `Drop the dependent flag`, drop the matching CLI flag from the in-memory invocation state so subsequent preflight runs do not re-trigger the finding.

- **invocation-fix**: Ask the user for a new `--cwd` value via a follow-up question, validate the path with Bash `test -d`, and update the in-memory invocation state. Do not edit any plan files.

- **plan-recreate**: Print a short message recommending `<skill-prefix>session-split <source-plan>` and stop the skill with `PREFLIGHT_ITERATE_PAUSE: plan-recreate` so the user can pick up outside this loop.

- **informational** or `Skip for now`: leave the finding alone but record it as "deferred" so it does not re-enter this round's batch.

When an edit is applied, print a one-line confirmation naming the file and the change (`Edited session-3.md: added Completion Contract scaffold`). Use the same phrasing the final report will repeat.

#### 4d. Rerun Preflight and Re-Triage

After processing the round's batch, rerun `asl preflight <plan-dir> --json` with the same forwarded flags. Parse the fresh JSON.

Compare `findings_by_severity` against the previous report:

- If red+yellow strictly decreased: continue to the next round with the remaining unresolved findings, unless only `manifest-readiness` and/or `progress-journal-missing` findings remain; in that case proceed to Step 5 (both are auto-repaired by `asl preflight --mark-loop-ready`).
- If red+yellow stayed the same or increased: surface the diff to the user. Ask whether to retry the same batch with different answers, skip to the next finding, or stop. Do not silently loop on findings that are not improving.
- If red+yellow reached zero: proceed to Step 5.

Always rebuild the `(finding, hint)` pairs from the fresh report. Hint identity may change (for example, `manifest-readiness.sha-mismatch` may become `manifest-readiness.state-not-loop-ready` after an edit) and stale pairs must not drive a new question.

If five rounds complete without reaching green, stop the loop. Step 5 will refuse to mark loop-ready and Step 6 will report what is left.

### Step 5: Mark Loop-Ready

When the most recent preflight returns `ok: true`, or when its only remaining red/yellow findings are `manifest-readiness` and/or `progress-journal-missing` findings, run:

```text
asl preflight <plan-dir> --json --mark-loop-ready [--cwd <repo>] [--harness <name>] [--commit-per-session|--commit-at-end] [--state-dir <dir>] [--max-pending-sessions <n>]
```

Forward the same flags that the iteration used so the readiness check sees the same environment. `--mark-loop-ready` auto-backfills `progress.md` (force semantics) when the only non-readiness blockers are `progress-journal-missing` findings; any such repair appears in the JSON payload's `auto_repairs` array with `action: "rewrite"` and `category: "progress-journal-missing"`. Parse the response:

- Success path: the JSON `reason` is `loop-ready-marked`, `marked_loop_ready` is `true`, `ok` is `true`, and `readiness.state` is `loop-ready`. Report success. If `auto_repairs` is non-empty, mention which categories were auto-repaired in the per-edit confirmation list.
- Refusal path: `reason` is `loop-ready-refused` and `mark_error` describes why. This is rare here because Step 4 should have driven the report to green; if it happens, treat it as a regression — rerun a single Step 4 round on the new findings, then retry the mark.
- Write-failure path: `reason` is `loop-ready-write-failed`. Print the `mark_error` and stop without retrying — the manifest write itself failed.

Skip Step 5 entirely when the loop ended without reaching green. The final report will name the unresolved findings instead.

### Step 6: Final Report

Print a concise summary:

```
Plan preflight iteration complete.

Plan: <plan-dir>
Rounds: <N> of 5
Baseline: <R> red, <Y> yellow, <G> green
Final:    <R> red, <Y> yellow, <G> green
Edits applied:
- <file>: <one-line change>
- <file>: <one-line change>

Loop-ready: marked | not marked (<reason>)

Next step:
- If marked: `<skill-prefix>session-run <plan-dir>/`
- If unresolved findings remain: list them with the relevant `check_id` and `fix_category`, then suggest `<skill-prefix>plan-preflight-iterate <plan-dir>` to resume in a fresh conversation
```

Do not re-print every preflight finding; the per-round confirmations already named the changes. Keep the report to one screen.

## Important Notes

- This skill only repairs an existing split plan. It never creates a plan, runs sessions, or invokes the harness directly. The runner contract (`asl run`) hard-gates on `loop-ready`, but it now self-heals stale/missing `progress.md` and recomputable `manifest.md` readiness metadata before pausing (surfaced via `readiness_gate_repair` in the run JSON); this skill is still the canonical interactive path for plan-edit, contract, env, and other non-derived blockers.
- The `repair_hints` array is the source of truth for what each finding means and how to fix it. Do not invent question wording from `finding.message` alone — use the hint's `question_template` so the UX is consistent with the preflight package's stable surface.
- Never modify a session file outside its `<plan-dir>`. Never modify the source plan referenced by `manifest.md`'s `**Split from**` line.
- `manifest.md` and `context.md` readiness rows and status tables are derived bookkeeping. The session-file handoffs remain the source of truth for completion. This skill must not change handoff statuses; it only repairs structure and metadata so the runner can start.
- When a finding's `fix_category` is `plan-recreate`, do not attempt repairs — stop the skill and recommend rerunning `session-split`.
- When a finding's `fix_category` is `env-install` or `invocation-fix`, the right action is to update the invocation, not the plan. Do not edit plan files to paper over an environment problem.
- Always rerun `asl preflight --json` after a batch of edits. Trust the fresh JSON over remembered findings — overrides like `manifest-readiness.sha-mismatch` may resolve into different `check_id`s after an edit.
- Cap the loop at 5 rounds. If the plan is not green by then, stop and surface what is left; the user can re-invoke the skill after addressing structural issues by hand.
- Only mark loop-ready through `asl preflight --mark-loop-ready`. Do not write `**Readiness**` lines directly — the package is the only place that computes the canonical `Readiness SHA-256`.
- If `asl preflight --json` is unavailable (missing CLI, wrong version), stop with a clear error pointing at the install of `agent-session-loop`. Do not fall back to parsing text output.
