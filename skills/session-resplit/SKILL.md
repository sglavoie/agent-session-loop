---
name: session-resplit
description: Split one pending session in an existing split plan into nested serial child sessions while preserving plan metadata, locks, archives, and rerun guidance.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Session Resplit

## Purpose

Replace one pending session in an existing split plan with nested child sessions such as `session-8.1.md`, `session-8.2.md`, and `session-8.3.md`. Use this when one pending session is too large or needs clearer boundaries, but the rest of the plan should remain intact.

Sequential by design: generated children are serial work units with parseable completion contracts. This skill does not read or emit parallel scheduling metadata. Legacy `parallel-validation.md` files and any `## Parallel Metadata` blocks left on disk from older plans are ignored as plan maintenance and execution inputs.

This skill is a companion to `session-split`, `session-expand`, and `session-run`:

- `session-split` creates the original plan directory.
- `session-expand` verifies plan facts and writes `session-<ID>.expand.md` files.
- `session-run` executes one pending `session-<ID>.md` file at a time, serially.
- `session-resplit` rewrites one pending parent session into nested pending children, then instructs the user to rerun `session-expand`.

Targeted parent selection (`--session <ID>`, direct `session-<ID>.md` paths) is intentionally preserved here because this skill is plan maintenance, not session execution. The `session-run` skill is serial-only and accepts only `<plan-dir>/`; the targeting flags on this skill let an operator pick which pending parent to rewrite without changing the serial execution contract.

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
$session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/
$session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ 4
$session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/session-8.md
$session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/session-8.2.md --parts 3
$session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ --session 8
$session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ --session 8.2 --parts 3
/session-resplit
/session-resplit 3
/session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/
/session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ 4
/session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/session-8.md
/session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/session-8.2.md --parts 3
/session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ --session 8
/session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ --session 8.2 --parts 3
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

### Step 1: Discover the Helper

All resplit mutations go through `skills/session-resplit/scripts/resplit-session.py` (also reachable through the installed skill directory). The helper owns parsing, lock acquisition, archival, child generation, contract allocation, ledger updates, and readiness refresh by delegating to `agent_session_loop.authoring` — the same rendering, table-mutation, transaction, and readiness API used by `session-split`. Do not hand-write child session files, hand-edit manifest tables, or reimplement any of its schemas in this skill; if the helper is missing, stop with a clear error instead of performing the contract manually.

This skill's job is the semantic work the helper cannot do: choosing good child boundaries, writing child instructions, and allocating the parent's completion contract across children (Step 5). Everything mechanical belongs to Python.

### Step 2: Inspect the Plan

Verify the resolved plan directory exists and contains `context.md` and `manifest.md`.

Read `context.md` and `manifest.md`. Do not read `parallel-validation.md` even if it exists; that file is a legacy artifact from older plan splits and is intentionally ignored by both this skill and `session-run`. Ignore any `## Parallel Metadata` blocks, dependency-wave tables, or ownership tables left in `manifest.md` or in session files — they are not maintained and do not gate resplit decisions.

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
- The plan directory has an active `agent-session-loop` lock.

Resplitting is plan maintenance, not session execution. A pending parent that depends on earlier incomplete sessions can still be resplit — preserve the dependency by generating serial children that link in the same `> **Previous session**` chain.

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
- All implementation instructions above the final handoff

Strip any `## Parallel Metadata` block from the parent body before partitioning the instructions; do not copy it into generated child files.

Create child sessions that are useful execution units, not arbitrary slices. Prefer boundaries based on files, subsystems, phases, tests, or explicit deliverables. When a compact parent paragraph lists deliverables after a colon or in bullets, split on those deliverables before falling back to generic phases.

The helper renders each child file deterministically (heading, context/previous-session references, `## Lineage`, `## Parent Scope`, `## Child Scope`, `## Completion Contract`, `## Handoff`) via `agent_session_loop.authoring.render_session_file`. Do not author child markdown by hand; supply semantics through `--child-spec`.

**Child spec schema** (`--child-spec <file>` takes a JSON list, or an object with a `children` list; one entry per child, in child-ID order):

```json
{
  "title": "<child title>",
  "instructions": "<child-scope instructions markdown>",
  "done_when": ["<explicit exit criterion>", "..."],
  "inherit_checks": ["<parent check name>", "..."],
  "checks": [{"name": "...", "type": "command|file_exists|regex_in_file", "...": "..."}]
}
```

Contract allocation rules (enforced by the helper; violations fail closed before any file is touched):

- Every child must state explicit `done_when` criteria and allocate at least one verification check via `inherit_checks` (parent check names) and/or `checks` (explicit replacement check objects, same JSON shape as contract checks).
- When any child inherits parent checks, **every** parent check must be inherited by at least one child; leaving a parent check unallocated is rejected as `ambiguous-contract`.
- When no child inherits, every child must carry its own `checks` — that is the explicit replacement of the parent recipe.
- `inherit_checks` naming a check the parent contract does not define, legacy `{title, instructions}`-only specs, and empty allocations are all rejected (`ambiguous-contract`). Unknown spec keys are rejected (`ambiguous-input`).

Without `--child-spec`, the helper derives every child contract from the parent's own `## Completion Contract`: each child inherits the parent's full verify recipe, and the last child additionally inherits the parent's `Done when` criteria. A parent without a parseable contract is rejected with `no-contract` — the helper never inserts a `python3 -m unittest` placeholder. Prefer explicit specs whenever the parent's checks can be split meaningfully.

Child IDs are `<PARENT_ID>.1` through `<PARENT_ID>.<N>`; the first child inherits the parent's previous-session reference (or the session immediately preceding the parent), and each later child points at the prior child. The helper handles all of this.

If the parent had expansion notes, do not copy stale line numbers into child instructions as authoritative facts. The helper's lineage section already marks the archived parent expansion as historical context pending a `session-expand` rerun.

### Step 6: Archive the Parent

Create an archive directory inside the plan directory:

```
resplit-archive/session-<PARENT_ID>-<UTC_TIMESTAMP>/
```

Use a timestamp format that is filesystem-safe, such as `YYYYMMDDTHHMMSSZ`. If that archive path already exists, add a short numeric suffix rather than overwriting.

Archive these files when present:

- The parent `session-<PARENT_ID>.md`
- The parent `session-<PARENT_ID>.expand.md`

The parent session file must no longer exist at the plan root after a successful resplit. Leaving it in place would cause `session-run` to execute the unsplit parent before the new children.

Do not touch `parallel-validation.md` if it exists. It is a legacy artifact from older plans and is intentionally left on disk so that hand-edited historical context is not lost. Neither this skill nor `session-run` reads it.

### Step 7: Write Children and Update Ledgers

The helper performs all writes in one atomic transaction (`authoring.PlanTransaction`): child files, downstream previous-session reference repair, and derived ledger updates land together or not at all, and a failed commit rolls the parent archive back too. It then refreshes the manifest readiness metadata to `draft` with a recomputed stable SHA (`authoring.refresh_readiness_metadata`), since the plan content changed.

Ledger behavior (owned by `authoring.replace_session_status_rows` / `replace_manifest_sessions_count`):

- `manifest.md`: the `**Sessions**:` count is updated, the parent row in every recognized session-status table is replaced with child rows (`pending`, child titles/files), and a `## Resplit Notes` section records the split and archive path.
- `context.md`: only clear session-status tables (session ID column plus `Status` column) are rewritten; prose, checklists, and ambiguous tables are untouched.
- Legacy dependency-wave and ownership tables are never edited, even when they reference the resplit parent.

If the selected parent was the first pending session, the first generated child becomes the next session that `session-run` should execute.

### Step 8: Regenerate Expansion Guidance

After a successful resplit, any parent expansion file is stale by definition and the plan readiness state has been reset to `draft`. The report must instruct the user to rerun `session-expand` against the plan directory before continuing execution so child expansion notes are written, the derived child contracts can be refined against the actual codebase, and the plan can be re-marked loop-ready.

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
4. Files archived (parent session and parent expansion when present)
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

Prefer stable reasons such as `malformed-plan`, `unknown-status`, `lock-contention`, `blocked`, `collision`, `cannot-split`, `ambiguous-input`, `no-contract`, or `ambiguous-contract`. The last two come from the helper's contract-allocation gate: `no-contract` means the parent has no completion contract to derive children from (supply `--child-spec` with explicit contracts), and `ambiguous-contract` means the child specs left the parent's checks partially allocated or a child without explicit criteria/checks.

End every successful report with exactly one `Next command:` line. Because resplitting invalidates the parent expansion file and resets readiness to `draft`, the next command should normally be:

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
- Do not emit `## Parallel Metadata` blocks in generated children. Generated children are serial work units; each must include a parseable `## Completion Contract` block instead.
- Do not read or rewrite `parallel-validation.md`. It is a legacy artifact that both this skill and `session-run` ignore.
- Do not touch legacy dependency-wave or ownership tables in `manifest.md`. Leave their rows intact even when they reference the resplit parent.
- Never author child files, manifest tables, or contracts by hand — the helper and `agent_session_loop.authoring` own those schemas and mutation rules. This skill supplies boundaries, instructions, and contract allocation only.
- Child contracts are never placeholders: they are derived from the parent contract or supplied explicitly per child. Ambiguous allocation fails closed (`no-contract` / `ambiguous-contract`) instead of inserting `python3 -m unittest`.
- After resplitting, rerun `session-expand` before `session-run` so child expansion notes are written, derived child contracts are refined, and the `draft` readiness state can be advanced back to loop-ready.
