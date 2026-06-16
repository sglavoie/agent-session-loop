---
name: session-resplit
description: Split one pending session in an existing split plan into nested child sessions while preserving plan metadata, locks, archives, and rerun guidance.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Session Resplit

## Purpose

Replace one pending session in an existing split plan with nested child sessions such as `session-8.1.md`, `session-8.2.md`, and `session-8.3.md`. Use this when one pending session is too large or needs clearer boundaries, but the rest of the plan should remain intact.

This skill is a companion to `session-split`, `session-expand`, and `session-run`:

- `session-split` creates the original plan directory.
- `session-expand` verifies plan facts and writes `session-<ID>.expand.md` files.
- `session-run` executes one pending `session-<ID>.md` file at a time.
- `session-resplit` rewrites one pending parent session into nested pending children, then instructs the user to rerun `session-expand`.

## Input

The user may provide no argument, a child-count integer, a plan directory, a direct session file, or a plan directory with an explicit `--session` selector.

Determine `<skill-prefix>` once for the active harness and use it for every user-facing skill invocation you print or write into generated files:

- Codex: `$`
- Copilot and Claude: `/`
- Unknown harness: use the prefix from the user's invocation; if no prefix is visible, ask before printing a ready-to-paste skill invocation

Only prefix user-facing skill invocations. Do not prefix YAML names, skill directory paths, filenames, internal prose references to skill names, or shell commands that are not skill invocations.

When printing commands or writing generated files, substitute the resolved prefix; never print or write the literal `<skill-prefix>` placeholder.

Examples:

```
$session-resplit
$session-resplit 3
$session-resplit ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/
$session-resplit ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/ 4
$session-resplit ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/session-8.md
$session-resplit ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/session-8.2.md --parts 3
$session-resplit ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/ --session 8
$session-resplit ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/ --session 8.2 --parts 3
/session-resplit
/session-resplit 3
/session-resplit ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/
/session-resplit ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/ 4
/session-resplit ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/session-8.md
/session-resplit ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/session-8.2.md --parts 3
/session-resplit ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/ --session 8
/session-resplit ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/ --session 8.2 --parts 3
```

Accepted input forms:

- **No argument**: infer the plan directory from the current working directory or visible plan context, then split the first pending session in dotted numeric order using an inferred child count.
- **Integer child count only**: infer the plan directory, split the first pending session, and create exactly that many children.
- **Plan directory**: split the first pending session in that plan directory.
- **Plan directory plus child count**: split the first pending session in that plan directory into exactly that many children.
- **Direct session file**: split exactly that pending `session-<ID>.md` file.
- **Direct session file plus child count**: split exactly that pending file into exactly that many children.
- **Explicit flag**: `<plan-dir>/ --session <ID>` splits exactly `session-<ID>.md`.
- **Explicit flag plus child count**: `<plan-dir>/ --session <ID> --parts <N>` or `<plan-dir>/ --session <ID> <N>`.

Session IDs must match `[1-9][0-9]*(\.[1-9][0-9]*)*`. Child counts must be integers from 2 through 20.

Reject malformed inputs and stop without modifying files:

- Unknown flags or extra positional arguments
- More than one plan directory or session file
- Both a direct session file and `--session`
- `--session` without a valid session ID
- `--parts` without a valid child count
- Multiple child counts
- Child count lower than 2 or higher than 20

## Inference Rules

Resolve missing values conservatively:

1. **Plan directory**:
   - If a direct session file is provided, use its parent directory.
   - If a plan directory is provided, use it directly.
   - If no path is provided, walk upward from the current working directory until a directory contains `context.md`, `manifest.md`, and at least one exact `session-<ID>.md` file.
   - If the current conversation or visible command context contains exactly one readable plan directory, that may be used.
   - If no unique plan directory can be inferred, ask the user for a plan directory and stop.
2. **Target session**:
   - A direct session file or `--session <ID>` selects the exact parent session.
   - Otherwise select the first pending session in dotted numeric order, using final handoff statuses as the source of truth.
3. **Child count**:
   - An integer argument or `--parts <N>` is authoritative.
   - Without an explicit count, choose a useful count from 2 through 5 based on the parent instructions: 2 for a short session, 3 for medium multi-step work, and 4 or 5 only when the parent clearly contains separable subsystems or phases.
   - If no meaningful split is possible, stop with `RESPLIT_PAUSE: cannot-split - parent instructions are too small or indivisible`.

Never infer by modifying or reading the original source plan outside the split plan directory. The split plan directory is the unit of work.

## Instructions

### Step 1: Discover Helpers

If `skills/session-resplit/scripts/resplit-session.py` exists, prefer using that deterministic helper for parsing, lock acquisition, archival, child generation, and ledger updates. Read the script interface before running it.

If no helper exists, perform the contract manually. Keep edits scoped to the selected plan directory and `skills/session-resplit/` only when improving this skill.

### Step 2: Inspect the Plan

Verify the resolved plan directory exists and contains `context.md` and `manifest.md`.

Read `context.md` and `manifest.md`. If `parallel-validation.md` exists, read it too; it will become stale after a successful resplit and must be archived.

Build the exact session file list from the plan directory:

- Include files whose basename is exactly `session-<ID>.md`.
- Exclude `session-<ID>.expand.md`.
- Reject files with invalid session IDs.
- Sort session files by dotted numeric order: compare dot-separated components as integers, and when all compared components are equal, the shorter ID sorts first.

For each session file, read status from the final exact `## Handoff` block:

- Find the last line whose trimmed text is exactly `## Handoff`.
- Within that block, read the first line that starts with `- **Status**:`.
- Ignore status lines above the final handoff.
- Treat a missing final handoff or missing populated status line as a malformed plan.

Valid statuses for resplitting are `pending` and `complete`. Stop on unknown or empty statuses unless the selected parent can be repaired without changing implementation intent. Do not resplit a malformed plan silently.

### Step 3: Validate the Target

The selected parent session must exist and must have final handoff status `pending`.

Fail closed without modifying files when:

- The selected parent session is already `complete`.
- The selected parent status is unknown, empty, or malformed.
- Any file already exists for a child ID that would be generated, such as `session-8.1.md` when splitting `session-8.md`.
- Any descendant session already exists under the selected parent prefix, such as `session-8.1.md` or `session-8.1.1.md` when splitting `session-8.md`.
- A matching child expansion file already exists, such as `session-8.1.expand.md`.
- `parallel-validation.md` marks the selected parent as `blocked`, `unsafe to run now`, or `ownership-conflicted`.
- The plan directory has an active `agent-session-loop` lock.

`parallel-validation.md` language saying the selected session is `dependent on unresolved work` is not by itself a resplit blocker. Resplitting is plan maintenance, not session execution. Preserve the dependency by making the generated children serial-required and by carrying required prior sessions into the first child.

### Step 4: Acquire the Plan Lock

Use the same per-plan lock boundary as `agent-session-loop` before modifying any plan files.

Preferred behavior:

- Use `agent_session_loop.state.acquire_lock(plan_dir, state_dir=None, owner="session-resplit")` when available.
- Let the existing helper remove stale locks according to its normal rules.
- Refuse active locks. Do not force-remove an active lock.
- Release the lock with `agent_session_loop.state.release_lock(plan_dir, state_dir=None)` after all edits or after any failure once the lock has been acquired.

If the helper is not available, inspect the lock path through the existing `agent-session-loop status` or `asl status` behavior if present. Stop rather than guessing when lock state cannot be determined.

Use runner-friendly pause lines for lock contention:

```
RESPLIT_STATUS: paused
RESPLIT_PAUSE: lock-contention - active plan lock detected
```

### Step 5: Plan the Child Sessions

Read the selected parent session content above its final `## Handoff` block. Preserve these pieces:

- Title and implementation intent
- `> **Before starting**` context requirement
- Expansion-note reference, if present
- Previous-session reference, if present
- `## Parallel Metadata`, if present
- All implementation instructions above the final handoff

Create child sessions that are useful execution units, not arbitrary slices. Prefer boundaries based on files, subsystems, phases, tests, or explicit deliverables. When a compact parent paragraph lists deliverables after a colon or in bullets, split on those deliverables before falling back to generic phases.

Default child metadata:

- Child IDs are `<PARENT_ID>.1`, `<PARENT_ID>.2`, ..., `<PARENT_ID>.<N>`.
- Child files are `session-<CHILD_ID>.md`.
- The first child inherits the parent's required prior sessions and previous-session reference.
- Each later child requires the immediately preceding child.
- Safety classification defaults to `serial-required`.
- Allowed concurrent sessions defaults to `none`.
- Owned write paths default to a narrowed subset of the parent's owned paths when clear; otherwise preserve the parent owned paths and mark the child serial-required.
- Forbidden/shared paths should preserve the parent's caution text when present.
- Serial blockers should explain that the child sequence was generated from a resplit parent and should run serially until `session-expand` validates it.

Each child session must include:

```
# Session <CHILD_ID>: <child title>

> **Before starting**: Read `context.md` in this directory for shared project context.
> **Previous session**: Read the **Handoff** section at the bottom of `<previous-session-file>`.

## Lineage

- **Split from**: `session-<PARENT_ID>.md`
- **Parent title**: <parent title>
- **Child session**: <CHILD_ID>
- **Parent archive**: `<archive-path>`

## Parallel Metadata

- **Safety classification**: `serial-required`
- **Required prior sessions**: <prior IDs or none>
- **Allowed concurrent sessions**: none
- **Owned write paths**: <paths or unknown - inherited from parent>
- **Forbidden/shared paths**: <paths or parent text>
- **Serial blockers**: Generated child of `session-<PARENT_ID>.md`; run serially until expansion validates narrower ownership.
- **Optional internal tracks**: none

---

## Parent Scope

- Full parent instructions are archived at `<archive-path>/session-<PARENT_ID>.md`.
- Preserve the archived parent implementation intent while completing only this child scope.
- Archived parent expansion notes, if present, are historical context only; rerun `session-expand` for current facts.

## Child Scope

<child-specific instructions>

## Handoff

<!-- Fill this section when the session is complete -->

- **Status**: pending
- **Files modified**: (none yet)
- **Key decisions**: (none yet)
- **Blockers for next session**: (none)
- **Notes**:
```

For the first child, the previous-session reference should point to the session that immediately precedes the parent in dotted numeric order when one exists. For later children, it should point to the prior child file.

If the parent had expansion notes, do not copy stale line numbers into child instructions as authoritative facts. Instead, place a short lineage note saying the archived parent expansion may contain historical context and that `session-expand` must be rerun.

### Step 6: Archive the Parent and Stale Artifacts

Create an archive directory inside the plan directory:

```
resplit-archive/session-<PARENT_ID>-<UTC_TIMESTAMP>/
```

Use a timestamp format that is filesystem-safe, such as `YYYYMMDDTHHMMSSZ`. If that archive path already exists, add a short numeric suffix rather than overwriting.

Archive these files when present:

- The parent `session-<PARENT_ID>.md`
- The parent `session-<PARENT_ID>.expand.md`
- `parallel-validation.md`

The parent session file must no longer exist at the plan root after a successful resplit. Leaving it in place would cause `session-run` to execute the unsplit parent before the new children.

Do not silently delete stale validation artifacts. Move them into the archive directory and mention the archive in the report.

### Step 7: Write Children and Update Ledgers

Write all child `session-<CHILD_ID>.md` files in the plan root.

Update `manifest.md` as derived bookkeeping:

- Update the top-level `**Sessions**:` count when present.
- In the primary `## Sessions` table, replace the parent row with child rows in child-ID order.
- Keep non-status cells intact where practical, but set each child status to `pending` and file to `session-<CHILD_ID>.md`.
- In dependency-wave and ownership tables, replace the parent row with serial child rows when the table shape is clear.
- If a table cannot be updated safely, leave it unchanged and append a `## Resplit Notes` section explaining which parent was split, which child files were created, and which tables may need manual cleanup.

Update `context.md` only when it contains a clear markdown session-status table with both a session ID column and a `Status` column. Do not rewrite prose, checklists, ambiguous tables, or unrelated uses of `pending`.

If the selected parent was the first pending session, the first generated child becomes the next session that `session-run` should execute.

### Step 8: Regenerate Expansion Guidance

After a successful resplit, `parallel-validation.md` and any parent expansion file are stale by definition. The report must instruct the user to rerun `session-expand` against the plan directory before continuing execution.

Ready-to-paste command:

```
$session-expand <plan-dir>/
```

Use the resolved skill prefix in the actual report.

Do not create child `.expand.md` files by hand unless this skill has just performed the same verification that `session-expand` would perform. Prefer rerunning `session-expand`.

### Step 9: Report

Print a concise summary:

1. Parent session that was resplit
2. Child sessions created
3. Archive directory used
4. Stale validation artifacts archived
5. Derived ledgers updated or intentionally left unchanged
6. Any manual cleanup required
7. Next commands

Use runner-friendly lines immediately before the final next command:

```
RESPLIT_STATUS: complete
```

On a recoverable stop, use:

```
RESPLIT_STATUS: paused
RESPLIT_PAUSE: <reason> - <short detail>
```

Prefer stable reasons such as `malformed-plan`, `unknown-status`, `lock-contention`, `blocked`, `unsafe`, `collision`, `cannot-split`, or `ambiguous-input`.

End every successful report with exactly one `Next command:` line. Because resplitting invalidates expansion and parallel-validation artifacts, the next command should normally be:

```
Next command: $session-expand <plan-dir>/
```

If expansion was already regenerated during the same invocation, end with:

```
Next command: $session-run <plan-dir>/
```

Use a trailing slash for displayed plan directories.

## Important Notes

- Resplit exactly one parent session per invocation.
- Only pending sessions can be resplit.
- Never mark the parent session `complete` to get it out of the way; archive it instead.
- Never leave both the parent session and its generated children as root-level `session-<ID>.md` files.
- Never overwrite existing child sessions or expansion files.
- Never modify the original source plan file referenced by `manifest.md`.
- Treat final handoff status lines as authoritative over derived manifest tables.
- Preserve dotted session IDs everywhere: filenames, headings, dependencies, lineage, examples, and reports.
- Default generated children to serial-required dependencies until `session-expand` proves narrower ownership.
- Archive `parallel-validation.md` after any successful resplit because its cross-session conclusions were computed against the old session graph.
- After resplitting, rerun `session-expand` before `session-run` so child annotations and validation are regenerated with dotted IDs.
