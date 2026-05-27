---
name: session-expand
description: Validate and expand split session files against the actual codebase. Verifies file paths, signatures, and assumptions. Takes a plan directory or single session file path.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Session Expand

## Purpose

Research the actual codebase to validate and expand claims in session files produced by `session-split`. For each session, verify that referenced file paths exist, function/struct/class names are correct, code patterns match reality, and inter-session dependencies are satisfiable. Write a concise annotations file alongside each session with findings, corrections, and concrete details (line numbers, exact signatures, patterns to reuse). Optionally fix issues in the session files.

## Input

The user provides a path as the host agent command or skill invocation argument. Two forms are accepted:

```
session-expand ~/.local/share/agent-plans/my-plan-abc123def456/
session-expand ~/.local/share/agent-plans/my-plan-abc123def456/session-2.md
```

- **Directory path**: expand all session files in the directory.
- **Single file path**: expand only that session file.

## Instructions

### Step 1: Discover and Read Plan Files

**Directory mode** (path ends with `/` or is a directory):

1. Read `context.md` to understand the project scope, target repository, and files listed.
2. Read `manifest.md` to get the session list and titles.
3. Collect all `session-*.md` files (sorted numerically by session number).

**Single file mode** (path ends with `.md`):

1. Derive the plan directory from the file's parent directory.
2. Read `context.md` from the same directory.
3. Process only the specified session file.

**Determine the codebase root** from `context.md`. Look for:

- Explicit file paths (e.g., `internal/replay/`, `src/`, `pkg/`, `cmd/`)
- Repository references or build commands that reveal the project root

If the paths in `context.md` are relative and match the current working directory, use it. If they clearly reference a different repository, inform the user and ask for the codebase root before proceeding. If ambiguous, default to the current working directory.

### Step 2: Extract Claims from Each Session

For each session file, scan all content **above** the `## Handoff` section for verifiable claims. Organize them into these categories:

**P — Path claims**: File paths in `**File:**` lines, inline backtick references, or code block comments. Collect each unique path.

**S — Signature claims**: Function, method, struct, type, interface, or class definitions shown in code blocks or described in prose. Collect the name and expected signature.

**F — Field/property claims**: Struct fields, interface methods, config keys, or constants referenced by name with expected types or values.

**D — Dependency claims**: References to packages, modules, imports, or external tools that must exist.

**B — Behavioral claims**: Assertions about how existing code works (e.g., "this method returns X", "this field is used by Y").

**N — Naming claims**: Variable names, constant names, enum values, command names referenced as already existing.

For each claim, record:

- Claim type (P/S/F/D/B/N)
- The exact text from the session
- The session section it appears in

### Step 3: Research the Codebase

Verify each claim using the most efficient tool:

**Path claims (P)**:

- Use `Glob` to check if the file exists.
- If not found, search for similar filenames nearby (different directory, different extension).
- If found elsewhere, record the correct path.

**Signature claims (S)**:

- Use `Grep` to find the function/method/type definition.
- If found, use `Read` to capture the exact current signature with line number.
- If the signature differs from the claim, record both claimed and actual.
- If not found anywhere, check whether the session intends to _create_ it — that is valid.

**Field/property claims (F)**:

- Use `Grep` to find the struct/interface/class definition.
- Use `Read` to check if the claimed field exists and its type matches.
- Record actual field names and types if they differ.

**Dependency claims (D)**:

- Check import paths exist (e.g., `Glob` for Go packages, `Grep` for import statements).
- For external tools, use `Bash` with `command -v`.

**Behavioral claims (B)**:

- Use `Read` to examine the referenced code and verify the assertion.
- Record the actual behavior with a line number range.

**Naming claims (N)**:

- Use `Grep` to confirm the name exists in the codebase.
- Record the actual location if found, or flag as missing.

**Token efficiency rules**:

- Read files once and extract multiple claims from the same file.
- Batch related Glob/Grep calls where possible.
- Do NOT re-read content already present in `context.md`.
- Stop researching a file after confirming all claims about it.
- For large sessions (>200 lines), prioritize Path and Signature claims first — these cause the most execution failures.

### Step 4: Cross-Session Dependency Check

**Skip this step when processing a single session file.**

When processing all sessions, verify:

1. **Creation order**: If session N references a file or function that session M (M < N) is supposed to create, confirm session M's scope actually covers that creation.
2. **Handoff consistency**: If a completed session has a filled Handoff section, check whether its "Files modified" list matches what later sessions assume exists.
3. **Missing gaps**: Flag anything that no session creates but a later session depends on.

### Step 5: Identify Patterns to Reuse

For each session, find existing codebase patterns that the implementation should follow. Record **at most 3** patterns per session — only the most directly relevant ones:

- Similar functions or methods the new code should mirror
- Test file conventions (naming, helper functions, assertion style)
- Error handling patterns in the same package or module
- Style patterns (e.g., UI framework usage, CLI command structure)

Include the file path and line range for each pattern.

### Step 6: Write Annotation Files

For each session file `session-N.md`, write `session-N.expand.md` in the same directory.

**File structure**:

```markdown
# Session N Expansion: <title>

**Expanded**: <YYYY-MM-DD>
**Codebase root**: `<path>`
**Claims checked**: <total> | **Issues found**: <count>

## Issues

<!-- Only present if issues were found -->

### ISSUE-1: <short description>

- **Type**: path-missing | signature-mismatch | field-missing | dependency-missing | behavior-incorrect | name-missing
- **Session ref**: "<quoted text from session>"
- **Actual**: <what the codebase actually shows>
- **Fix**: <concrete correction>

### ISSUE-2: ...

## Verified Claims

| Claim                | Type | Location           | Line |
| -------------------- | ---- | ------------------ | ---- |
| `FuncName` signature | S    | `path/to/file.go`  | 42   |
| `Model` struct       | F    | `path/to/model.go` | 18   |
| ...                  | ...  | ...                | ...  |

## Concrete Details

<!-- Key details the session executor will need -->

### File: `<path>`

- Lines <X>-<Y>: `<function/struct name>` — <brief description of current state>
- Pattern to follow: `<sibling function>` at line <Z>

### File: `<path>`

- ...

## Patterns to Reuse

- **<pattern name>**: `<file>:<line-range>` — <why it is relevant>
- ...

## Session Dependencies

<!-- Only for multi-session expansion -->

- **Requires from session <M>**: <what this session assumes session M created>
- **Provides for session <M>**: <what this session creates that session M needs>
```

**After writing each annotation file**, edit the corresponding `session-N.md` to add a reference. Insert a new blockquote line immediately after the `> **Before starting**:` line:

```
> **Expansion notes**: Read `session-N.expand.md` for verified line numbers, corrected paths, and patterns to reuse.
```

Use the actual session number for N. If the line already exists (idempotence), skip the edit for that file.

Rules:

- Only include claims that were checked and confirmed in the Verified Claims table. Do not list every trivial claim.
- The Concrete Details section should provide just enough information that the executor can work without re-reading source files — include only what is needed.
- Claims about code to be _created_ (new files, new functions) should be marked "N/A — to be created" in the Verified Claims table, not flagged as issues. Only flag them if they conflict with something already existing (e.g., a function name already taken with a different signature).
- Omit empty sections entirely (e.g., if no issues, omit `## Issues`; if no dependencies, omit `## Session Dependencies`).

### Step 7: Report and Offer Fixes

Print a concise summary:

1. Number of sessions expanded
2. Total claims checked and total issues found
3. Per-session one-liner: `session-N: <claims> checked / <issues> issues — <worst issue description or "clean">`
4. Blocking issues highlighted prominently (wrong file paths, missing dependencies)
5. List of annotation files created

**If issues were found**:

- Use `Edit` to immediately fix factual errors (wrong paths, incorrect signatures, wrong names) in the session `.md` files — do not ask the user first
- Only correct factual claims — never alter implementation logic, plan structure, or approach
- Report the corrections that were applied alongside the issue list

**If zero issues**:

- Tell the user the plan is validated and they can proceed to execution
- Note they can optionally read the `.expand.md` files for concrete line numbers and patterns

**In both cases**, end by printing the ready-to-paste `session-run <plan-directory>/` command so the user can proceed directly to execution without switching context.

## Important Notes

- Never modify `context.md` or `manifest.md`
- The `.expand.md` suffix keeps annotation files visually grouped with their session files
- Skip sessions whose Handoff status is not "pending" — the codebase has already been modified by that session's execution, so checking stale claims would be misleading. Note skipped sessions in the summary.
- New code claims (files or functions to be created) are valid and should not be flagged unless they conflict with existing code
- If the codebase root is a different repository from the current working directory, `cd` to it before running Glob/Grep/Read operations
- When applying fixes to session files, only correct factual errors — never rewrite implementation logic, change the approach, or restructure the plan
- Prefer `Grep` with `output_mode: "content"` and line numbers for signature verification — this gives the executor exact references
