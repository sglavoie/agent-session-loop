---
name: session-expand
description: Validate and expand split session files against the actual codebase. Verifies file paths, signatures, assumptions, and parallel-safety metadata. Takes a plan directory or single session file path.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Session Expand

## Purpose

Research the actual codebase to validate and expand claims in session files produced by `session-split`. For each session, verify that referenced file paths exist, function/struct/class names are correct, code patterns match reality, and inter-session dependencies are satisfiable. In directory mode, validate parallel metadata across all sessions and write authoritative safety findings that `session-run` can use to override manifest guidance. Write a concise annotations file alongside each session with findings, corrections, concrete details (line numbers, exact signatures, patterns to reuse), and parallel-safety notes when cross-session context is available. Optionally fix issues in the session files.

## Input

The user provides a path as the host agent command or skill invocation argument. Two forms are accepted:

Determine `<skill-prefix>` once for the active harness and use it for every user-facing skill invocation you print:

- Codex: `$`
- Copilot and Claude: `/`
- Unknown harness: use the prefix from the user's invocation; if no prefix is visible, ask before printing a ready-to-paste skill invocation

Only prefix user-facing skill invocations. Do not prefix YAML names, skill directory paths, filenames, internal prose references to skill names, or shell commands that are not skill invocations.

When printing commands, substitute the resolved prefix; never print the literal `<skill-prefix>` placeholder.

Examples:

```
$session-expand ~/.local/share/agent-plans/my-plan-abc123def456/
$session-expand ~/.local/share/agent-plans/my-plan-abc123def456/session-2.md
$session-expand ~/.local/share/agent-plans/my-plan-abc123def456/session-2.1.md
/session-expand ~/.local/share/agent-plans/my-plan-abc123def456/
/session-expand ~/.local/share/agent-plans/my-plan-abc123def456/session-2.md
/session-expand ~/.local/share/agent-plans/my-plan-abc123def456/session-2.1.md
```

- **Directory path**: expand all canonical session files in the directory.
- **Single session file path**: expand only that session file.

Session IDs use `[1-9][0-9]*(\.[1-9][0-9]*)*`. Session files are named `session-<ID>.md`, and expansion files are named `session-<ID>.expand.md`.

## Instructions

### Step 1: Discover and Read Plan Files

**Directory mode** (path ends with `/` or is a directory):

1. Read `context.md` to understand the project scope, target repository, and files listed.
2. Read `manifest.md` to get the session list, titles, and optional `## Parallel Execution` scheduling guidance.
3. Collect exact session files whose basename matches `session-<ID>.md`, where `<ID>` matches `[1-9][0-9]*(\.[1-9][0-9]*)*`. Exclude expansion files such as `session-2.expand.md` and `session-2.1.expand.md`.
4. Sort session files by dotted numeric order: compare dot-separated components as integers, and when all compared components are equal, the shorter ID sorts first. For example, `2` sorts before `2.1`, and `2.10` sorts after `2.2`.

**Single file mode** (path ends with `.md`):

1. Derive the plan directory from the file's parent directory.
2. Read `context.md` from the same directory.
3. Verify the selected file basename matches exactly `session-<ID>.md`, where `<ID>` matches `[1-9][0-9]*(\.[1-9][0-9]*)*`.
4. Process only the specified session file and write the corresponding `session-<ID>.expand.md`.

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

Separately collect each session's `## Parallel Metadata` block when present:

- Safety classification
- Required prior sessions
- Allowed concurrent sessions
- Owned write paths
- Forbidden/shared paths
- Serial blockers
- Optional internal tracks

Required prior session and allowed concurrent session references may use dotted session IDs. Accept references such as `2`, `2.1`, `session 2.1`, or a comma/space-separated list of those IDs, and treat `none` as an empty list.

Treat these as scheduling claims to validate during the directory-mode cross-session check. Do not count missing parallel metadata as a path/signature/field/dependency/behavior/naming issue.

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

### Step 4: Cross-Session Dependency and Parallel Metadata Check

**Skip this step when processing a single session file.**

When processing all sessions, verify:

1. **Creation order**: If session `<ID>` references a file or function that an earlier session `<PRIOR_ID>` in dotted numeric order is supposed to create, confirm the earlier session's scope actually covers that creation.
2. **Handoff consistency**: If a completed session has a filled Handoff section, check whether its "Files modified" list matches what later sessions assume exists.
3. **Missing gaps**: Flag anything that no session creates but a later session depends on.

Then validate parallel metadata using all session files, `manifest.md`, and the verified claim results:

1. **Metadata completeness**: Confirm each `## Parallel Metadata` block uses known fields and one of these classifications: `parallel-safe`, `parallel-candidate`, or `serial-required`. Flag unknown classifications or missing required fields.
2. **Required prior sessions**: Check whether implementation instructions, generated-output usage, handoff references, or verified dependencies imply prior sessions that are absent from `Required prior sessions`. Record the added required prior sessions.
3. **Allowed concurrency**: For each session ID listed in `Allowed concurrent sessions`, confirm neither session requires the other, neither shares overlapping write ownership, and neither touches a forbidden/shared path owned by the other.
4. **Owned write paths**: Treat exact path overlap, parent/child path overlap, broad directory ownership, generated-output directories, shared configs, schemas, migrations, lockfiles, package-manager metadata, build-system files, and repo-wide rewrite instructions as unsafe unless the plan assigns a single clear owner and the other sessions avoid that path.
5. **Forbidden/shared paths**: Confirm forbidden/shared paths are not also listed as owned write paths by the same session and are not claimed as allowed concurrent write scope by another session.
6. **Safety classification**: Keep `parallel-safe` only when ownership is concrete, dependencies are satisfied by prior sessions or absent, and allowed concurrent sessions are demonstrably disjoint. Treat `parallel-candidate` and `serial-required` as not safe for concurrent execution unless validation proves narrower ownership and dependency boundaries. Flag uncertain or broad ownership as unsafe instead of inferring safety.
7. **Internal tracks**: If `Optional internal tracks` are present, validate that track-owned paths are disjoint and do not include handoff files, `manifest.md`, `context.md`, or files outside the session's owned write paths.

Record these outcomes:

- Unsafe overlaps
- Dependency violations
- Ambiguous or broad ownership
- Sessions that must be treated serially
- Sessions that are blocked, unsafe to run now, ownership-conflicted, or dependent on unresolved work
- Any required prior sessions added by validation

`serial-required` means "not safe for concurrent execution"; it does not by itself block explicit serial execution after all required prior sessions are complete. Only mark a session blocked when validation identifies an unresolved dependency, ownership conflict, or unsafe run condition.

### Step 5: Identify Patterns to Reuse

For each session, find existing codebase patterns that the implementation should follow. Record **at most 3** patterns per session — only the most directly relevant ones:

- Similar functions or methods the new code should mirror
- Test file conventions (naming, helper functions, assertion style)
- Error handling patterns in the same package or module
- Style patterns (e.g., UI framework usage, CLI command structure)

Include the file path and line range for each pattern.

### Step 6: Write Annotation Files

For each session file `session-<ID>.md`, write `session-<ID>.expand.md` in the same directory. Preserve the exact dotted ID from the source filename in annotation filenames, headings, dependency references, and blockquotes.

**File structure**:

```markdown
# Session <ID> Expansion: <title>

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

- **Requires from session <ID>**: <what this session assumes session <ID> created>
- **Provides for session <ID>**: <what this session creates that session <ID> needs>

## Parallel Safety

<!-- Only for directory-mode expansion when cross-session context is available -->

- **Validated classification**: <parallel-safe | parallel-candidate | serial-required | blocked>
- **Required prior sessions**: <none | session IDs and reasons, including validation-added requirements>
- **Allowed concurrent sessions**: <none | session IDs validated as disjoint>
- **Ownership findings**: <none | unsafe overlaps, broad ownership, ambiguous ownership, forbidden/shared path conflicts>
- **Session-run markers**: <none | blocked | unsafe to run now | ownership-conflicted | dependent on unresolved work>
```

**Directory-mode parallel validation file**:

When processing a full plan directory and cross-session context is available, also write `<plan-dir>/parallel-validation.md`. Do not write this file in single-session mode.

Use this structure:

```markdown
# Parallel Validation

**Expanded**: <YYYY-MM-DD>
**Scope**: <all session files checked>

## Summary

- **Safe concurrent sessions**: <none | validated sets>
- **Serial-only sessions**: <none | sessions and reasons>
- **Blocked sessions**: <none | sessions and reasons>

## Session Results

### Session <ID>: <title>

- **Validated classification**: <parallel-safe | parallel-candidate | serial-required | blocked>
- **Required prior sessions**: <none | session IDs and reasons>
- **Allowed concurrent sessions**: <none | session IDs>
- **Run condition**: <safe to run now | unsafe to run now | blocked | ownership-conflicted | dependent on unresolved work>
- **Findings**: <none | concise findings>

## Unsafe Overlaps

- <none | session/path/reason>

## Dependency Violations

- <none | session/reason/added required prior sessions>

## Ambiguous Ownership

- <none | session/path/reason>

## Session-Run Precedence

`session-run` must treat this file as authoritative over manifest and session-file parallel metadata for dependency, ownership, and safety claims. Handoff status lines in session files remain the source of truth for completion.
```

Use explicit blocking language in `parallel-validation.md` when `session-run` should fail closed for explicit-mode execution: `blocked`, `unsafe to run now`, `ownership-conflicted`, `dependent on unresolved work`, or `required prior sessions`.

**After writing each annotation file**, edit the corresponding `session-<ID>.md` to add a reference. Insert a new blockquote line immediately after the `> **Before starting**:` line:

```
> **Expansion notes**: Read `session-<ID>.expand.md` for verified line numbers, corrected paths, and patterns to reuse.
```

Use the actual session ID from the filename. If the line already exists with the same expansion filename (idempotence), skip the edit for that file.

Rules:

- Only include claims that were checked and confirmed in the Verified Claims table. Do not list every trivial claim.
- The Concrete Details section should provide just enough information that the executor can work without re-reading source files — include only what is needed.
- Claims about code to be _created_ (new files, new functions) should be marked "N/A — to be created" in the Verified Claims table, not flagged as issues. Only flag them if they conflict with something already existing (e.g., a function name already taken with a different signature).
- Omit empty sections entirely (e.g., if no issues, omit `## Issues`; if no dependencies, omit `## Session Dependencies`).
- Include `## Parallel Safety` only when directory-mode expansion had enough cross-session context to validate the selected session against the rest of the plan.
- Do not rewrite `manifest.md`; `parallel-validation.md` and `## Parallel Safety` sections are authoritative corrections to manifest/session parallel guidance.

### Step 7: Report and Offer Fixes

Print a concise summary:

1. Number of sessions expanded
2. Total claims checked and total issues found
3. Per-session one-liner: `session-<ID>: <claims> checked / <issues> issues — <worst issue description or "clean">`
4. Blocking issues highlighted prominently (wrong file paths, missing dependencies)
5. Parallel validation summary when directory-mode validation was performed
6. List of annotation files created and whether `parallel-validation.md` was written

**If issues were found**:

- Use `Edit` to immediately fix factual errors (wrong paths, incorrect signatures, wrong names) in the session `.md` files — do not ask the user first
- Only correct factual claims — never alter implementation logic, plan structure, or approach
- Report the corrections that were applied alongside the issue list

**If zero issues**:

- Tell the user the plan is validated and they can proceed to execution
- Note they can optionally read the `.expand.md` files for concrete line numbers and patterns
- If directory-mode parallel validation found no unsafe overlaps or unresolved dependencies, report the validated concurrent sets; otherwise recommend the serial fallback or explicit sessions that remain safe.

**In both cases**, end by printing the ready-to-paste `<skill-prefix>session-run <plan-directory>/` command so the user can proceed directly to execution without switching context.

## Important Notes

- Never modify `context.md` or `manifest.md`
- The `.expand.md` suffix keeps annotation files visually grouped with their session files
- Skip sessions whose Handoff status is not "pending" — the codebase has already been modified by that session's execution, so checking stale claims would be misleading. Note skipped sessions in the summary.
- Generate `parallel-validation.md` only in directory mode, after all session files and manifest guidance are available. In single-session mode, mention that complete cross-session parallel validation requires directory-mode expansion.
- Treat `parallel-validation.md` and `## Parallel Safety` expansion sections as authoritative corrections for `session-run`; do not change the manifest to reflect validation results.
- When parallel safety is uncertain, broad, or ownership-conflicted, prefer `serial-required` or blocked validation language over optimistic concurrency.
- `serial-required` restricts concurrent execution. It does not block explicit serial execution after required prior sessions are complete unless validation records an unresolved dependency or ownership conflict.
- New code claims (files or functions to be created) are valid and should not be flagged unless they conflict with existing code
- If the codebase root is a different repository from the current working directory, `cd` to it before running Glob/Grep/Read operations
- When applying fixes to session files, only correct factual errors — never rewrite implementation logic, change the approach, or restructure the plan
- Prefer `Grep` with `output_mode: "content"` and line numbers for signature verification — this gives the executor exact references
