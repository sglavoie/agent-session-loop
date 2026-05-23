---
name: session-split
description: Split a multi-session plan into per-session files for token-efficient execution. Takes a plan file path. Also proposes session boundaries if the plan lacks session structure.
allowed-tools: Bash, Read, Write
---

# Session Split

## Purpose

Parse a multi-session plan file and split it into a directory of smaller, self-contained files: shared context, individual session files with handoff templates, and a manifest with ready-to-paste execution commands.

## Input

The user provides a plan file path. In Claude slash-command usage, this value is `$ARGUMENTS`. Example:

```
/session-split ~/.claude-personal/plans/tranquil-singing-lantern.md
```

## Instructions

### Step 1: Read and Parse the Plan

Read the entire plan file at the provided path.

Scan for session headings by checking each line against this pattern (case-insensitive):

```
^#{2,3}\s+Session\s+(\d+)\b
```

This matches `## Session 1: Title`, `### Session 2: Title`, `## Session 3`, etc.

**Code fence awareness**: Track whether you are inside a fenced code block (lines between matching ``` or ~~~ delimiters). Skip any heading matches found inside code blocks — they are content, not structure.

Record:

- **context**: all lines from the start of the file up to (but not including) the first session heading
- **sessions**: for each session, capture the session number, title (text after the colon if any), heading level, and all lines from that heading until the next session heading or end of file

### Step 2: Surface Scan

Extracts file paths from the plan text, checks existence and line counts, and builds a per-session complexity profile.

**Proportionality rule** (keeps small plans fast):

- **< 5 file references total**: Skip this entire step
- **5–15 references**: Run sub-steps 2a–2b only (defer 2c until sessions are defined)
- **> 15 references**: Run all sub-steps

**Sub-steps:**

**2a. Extract paths**: Scan the plan for backtick-quoted paths (e.g. `` `src/foo/bar.ts` ``) and bare paths in `**File:**` lines or list items. Deduplicate the results.

**2b. Probe files**: For each extracted path, run:

```bash
wc -l <path> 2>/dev/null || echo "NEW"
```

Record a tuple for each: `(path, lines | NEW, session_number)` — where `session_number` is the session the reference appeared in (or `0` for context).

**2c. Per-session profile**: For each session compute:

- Files touched (count)
- Existing LoC sum (sum of line counts for existing files)
- New files count (count of `NEW` entries)
- Cross-cut score (number of distinct top-level directories among the paths)

**Explicit boundary**: Do NOT verify signatures, function names, or code patterns — that is `session-expand`'s job. This step only measures size and spread.

### Step 3: Handle Edge Cases

**No session markers found**: Propose session boundaries using concrete data:

1. Group work items that share files or directories into one session (use surface scan data when available)
2. Estimate session weight: plan text lines + files touched + existing LoC + subsystem spread
3. Balance sessions by weight — a session touching 15 files across 4 subsystems is far heavier than one editing 3 files in the same package; split heavy sessions, merge lightweight ones
4. Present the proposed session structure with per-session summaries: `<Title> -- <N> files, ~<L> existing lines, <subsystem(s)>`
5. If surface scan was skipped (< 5 file references), fall back to heading/affinity grouping
6. Ask the user for approval before proceeding
7. Once approved, restructure the plan content accordingly and continue to Step 4

**Single session found**: Tell the user: "Only 1 session found — splitting is not needed." Stop.

**Two or more sessions found**: Continue to Step 4.

### Step 4: Create Output Directory

Derive the plan name from the filename (strip the `.md` extension). Create the output directory in the existing shared plan store:

```bash
mkdir -p ~/.claude-personal/plans/<plan-name>/
```

If the directory already exists and contains files, warn the user and ask before overwriting.

### Step 5: Write context.md

Write the context lines to `~/.claude-personal/plans/<plan-name>/context.md`.

If context is empty (the plan starts directly with Session 1), write:

```markdown
# Context

_No shared context section found in the original plan._

**Original plan**: `<original-path>`
```

### Step 6: Write session-N.md Files

For each session, write `~/.claude-personal/plans/<plan-name>/session-N.md` where N is the actual session number from the heading.

**File structure**:

```markdown
# Session N: <title>

> **Before starting**: Read `context.md` in this directory for shared project context.
> **Previous session**: Read the **Handoff** section at the bottom of `session-<N-1>.md`.

---

<original session content here>

---

## Handoff

<!-- Fill this section when the session is complete -->

- **Status**: pending
- **Files modified**: (none yet)
- **Key decisions**: (none yet)
- **Blockers for next session**: (none)
- **Notes**:
```

Rules:

- For Session 1, omit the "Previous session" line
- Preserve all original content exactly — do not summarize, rewrite, or reformat
- **Heading normalization**: if the original session heading was `###` level, promote all headings within the session by one level so the file reads naturally as a standalone document (session title becomes `#`, its `####` sub-sections become `##`, etc.)
- Preserve fenced code blocks verbatim

### Step 7: Write manifest.md

Write `~/.claude-personal/plans/<plan-name>/manifest.md`:

````markdown
# <Plan Title>

**Split from**: `<original-path>`
**Sessions**: <N>
**Split date**: <YYYY-MM-DD>

## Sessions

| #   | Title   | Status  | File         |
| --- | ------- | ------- | ------------ |
| 1   | <title> | pending | session-1.md |
| 2   | <title> | pending | session-2.md |
| ... | ...     | ...     | ...          |

## Execution

Run `session-run` once per session, preferably in a fresh harness conversation for each:

```text
session-run ~/.claude-personal/plans/<plan-name>/
```

Claude slash-command form:

```markdown
/session-run ~/.claude-personal/plans/<plan-name>/
```

`session-run` auto-advances — it finds the next pending session, implements it, and updates the handoff. No session number needed. Repeat in new conversations until all sessions are complete.

## Token Budget

- Original plan: ~<X> lines
- Context file: ~<Y> lines
- Average session file: ~<Z> lines
- Estimated savings vs full plan per session: ~<P>%

## Execution Weight

| Session | Plan Lines | Files | Existing LoC | New Files | Subsystems | Weight |
|---------|-----------|-------|-------------|-----------|------------|--------|
| 1       | ~L        | F     | ~E          | N         | S          | W      |

Weight classification:
- **light**: < 500 existing LoC AND <= 3 files
- **heavy**: > 2000 existing LoC OR > 10 files OR >= 4 subsystems
- **medium**: everything else
````

Replace all `<placeholders>` with actual computed values.

**Note**: Omit the Execution Weight section entirely if the surface scan was skipped (< 5 file references).

### Step 8: Report Results

Print a concise summary:

1. Number of sessions extracted
2. Output directory path
3. File listing with approximate sizes
4. The ready-to-paste execution command (the "Execute All Sessions" block from manifest.md)
5. Estimated token savings percentage
6. If the Execution Weight table was generated, highlight any "heavy" session and suggest the user consider splitting it further
7. Suggest running `session-expand <output-directory>` as the next step to validate file paths, signatures, and assumptions against the actual codebase before execution

## Important Notes

- Never modify the original plan file
- Content preservation is paramount — only restructure, never rewrite
- If a session contains "Exit criteria" or "Verification" sub-sections, they belong to that session (not to context)
- Content between `---` horizontal rules that appears between context and the first session should go at the end of context.md
- Always use `session-run` in generated execution instructions — it auto-advances through sessions and runs within the active harness conversation.
