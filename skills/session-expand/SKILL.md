---
name: session-expand
description: Validate and expand split session files against the actual codebase. Verifies file paths, signatures, assumptions, completion contracts, and manifest readiness. Sequential-by-design - no parallel safety analysis. Takes a plan directory or single session file path.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Session Expand

## Purpose

Research the actual codebase to validate and expand claims in session files produced by `session-split`. For each session, verify that referenced file paths exist, function/struct/class names are correct, code patterns match reality, and inter-session dependencies are satisfiable. Validate each session's `## Completion Contract` against the schema enforced by `agent_session_loop.contracts.parse_completion_contract`, re-resolve referenced paths/commands against the live codebase, record a manifest readiness snapshot for operator visibility, then run final preflight with `--mark-loop-ready` so clean expanded plans are ready for `session-run`. Write a concise annotations file alongside each session with findings, corrections, concrete details (line numbers, exact signatures, patterns to reuse), and contract issues when the contract is malformed or its references are stale. Optionally fix factual issues in the session files.

This skill is sequential-by-design. It does not extract, validate, or generate parallel-safety metadata, and it does not write `parallel-validation.md`. Legacy `## Parallel Metadata` blocks in split sessions and any pre-existing `parallel-validation.md` on disk are ignored.

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
2. Read `manifest.md` to get the session list and titles. Ignore any legacy `## Parallel Execution`, `### Dependency Waves`, `### Session Ownership`, `### Parallel Worker Commands`, `### Worker Prompts`, or `### Serial Blockers` sections that may still exist on disk — this skill is sequential-by-design and these blocks are no longer authoritative scheduling guidance.
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

If a session file still contains a legacy `## Parallel Metadata` block, ignore it. Do not extract its fields, validate them, or surface them as findings — this skill no longer participates in parallel scheduling.

Separately collect each session's `## Completion Contract` section when present:

- The raw text under `### Done when` (bullet list of human-readable exit criteria).
- The fenced ```json``` block under `### Verify` (the executable recipe).
- The optional top-level `"session"` JSON field when one is supplied.

Treat the contract as its own structural artifact validated in Step 3 under "Contract claims (C)". Do not extract `argv`, `path`, or `pattern` references from inside the contract as P/S/F/D/B/N claims — they are validated by the contract-specific checks instead so the same path or command is not double-reported.

In **directory mode**, also extract the manifest's readiness metadata block when present: the `**Readiness**`, `**Readiness SHA-256**`, and `**Readiness checked at**` lines emitted by `session-split` (Step 9a). Hold the recorded SHA for the Step 4 readiness check. Do not treat missing readiness metadata as a path/signature claim issue; surface it through the readiness check instead.

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

**Contract claims (C)** (per session, when a `## Completion Contract` section was captured in Step 2):

The parser at `agent_session_loop.contracts.parse_completion_contract` is the authoritative schema enforcer; do not re-implement its rules in this skill. Call it directly via a small `Bash` heredoc that loads the package the same way `asl` does (the `asl` wrapper on `$PATH` resolves to the package). Pass the raw session text and the canonical session id derived from the filename, then turn each raised `PlanError` into a "Contract Issues" entry. Example shape:

```bash
python3 - <<'PY' "<plan-dir>/session-<ID>.md" "<ID>"
import sys, pathlib
asl = pathlib.Path(__import__("shutil").which("asl")).resolve()
sys.path.insert(0, str(asl.parents[3]))
from agent_session_loop import contracts
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
try:
    contract = contracts.parse_completion_contract(text, session_id=sys.argv[2])
except Exception as error:
    print(f"SCHEMA: {error}")
    raise SystemExit(0)
if contract is None:
    print("MISSING")
    raise SystemExit(0)
for check in contract.verify.checks:
    print(f"OK {check.kind} {check.name}")
PY
```

Use the resulting `SCHEMA:` / `MISSING` / `OK …` lines to populate the "Contract Issues" section of the expansion file. Specifically:

- Report `SCHEMA:` messages verbatim under "Contract Issues" — the parser names the offending field, session id, and rule (e.g. `session 2 contract check #1 'type' must be one of: command, file_exists, regex_in_file`).
- Report `MISSING` as a "Contract Issues" entry asking the user to add a `## Completion Contract` section. Do not flag this when the session was deliberately split without a contract skeleton; the canonical split skill always emits one, so missing contracts indicate the session file was edited.
- For each successfully parsed `command` check, run `command -v <argv[0]>` and surface a yellow factual issue when the executable is not on `$PATH`. Allow well-known names (e.g. `python3`, `pytest`, `just`, `ruff`, `npm`, `cargo`, `go`) — when those are missing, the user almost certainly needs to install the tool before `asl run` is possible.
- For each successfully parsed `file_exists` check, resolve `path` against the codebase root. Missing files become factual issues, not schema issues.
- For each successfully parsed `regex_in_file` check, resolve `path` against the codebase root and run `re.search` over the file contents. A missing file is a factual issue. A pattern that matches zero times is also a factual issue (the pattern is almost always stale).
- If the JSON object includes a top-level `"session"` field, the parser already rejects mismatches; surface a clearer suggestion when the parser raises, naming both the wrong value and the canonical id derived from the filename and session heading.
- Flag the default skeleton placeholder bullet (`<one concrete human-readable exit criterion derived from the session>` or any other angle-bracketed `<...>` text in `Done when`) as a "Contract Issues" entry asking the user to replace it with a real criterion. The preflight ambiguity scanner already catches this, but surfacing it during expansion gives the user the fix near the rest of the contract feedback.

Bundle every successfully parsed contract into the per-session verified-claims tally so the summary counts reflect the work, but do not surface the OK checks as separate "Verified Claims" rows — they would be noisy. Reserve the "Verified Claims" table for the P/S/F/D/B/N taxonomy.

**Token efficiency rules**:

- Read files once and extract multiple claims from the same file.
- Batch related Glob/Grep calls where possible.
- Do NOT re-read content already present in `context.md`.
- Stop researching a file after confirming all claims about it.
- For large sessions (>200 lines), prioritize Path and Signature claims first — these cause the most execution failures.

### Step 4: Cross-Session Dependency and Readiness Check

**Skip this step when processing a single session file.**

When processing all sessions, verify that the serial work-session ordering is internally consistent. This is the only cross-session sanity check the skill performs; concurrency analysis, ownership overlap detection, and parallel classification are intentionally absent because the execution model is sequential-by-design.

1. **Creation order**: If session `<ID>` references a file or function that an earlier session `<PRIOR_ID>` in dotted numeric order is supposed to create, confirm the earlier session's scope actually covers that creation.
2. **Handoff consistency**: If a completed session has a filled Handoff section, check whether its "Files modified" list matches what later sessions assume exists.
3. **Missing gaps**: Flag anything that no session creates but a later session depends on.

Record only "later session assumes earlier work that no prior session covers" findings. Do not classify sessions as `parallel-safe`, `parallel-candidate`, `serial-required`, or `blocked`, do not validate ownership overlap, do not check forbidden/shared paths, and do not emit any parallel-validation outputs.

**Manifest readiness check (directory mode only)**:

When the manifest readiness block was captured in Step 2, recompute the structural hash by calling `agent_session_loop.plans.stable_readiness_sha256(plan_dir)` via the same `python3 -` heredoc pattern used for contract validation. Compare it to the SHA recorded in the manifest:

- If the recorded SHA is missing or malformed, flag it as a "Plan Readiness" finding asking the user to re-run the split skill's Step 9a or to run `asl preflight --mark-loop-ready` after repair.
- If the recorded SHA differs from the recomputed value, flag it as a "Plan Readiness" finding identical in shape to `asl preflight`'s red `manifest-readiness` finding. Hash divergence at expansion time almost always means a session file was edited (structural content or contract skeleton) between `session-split` and `session-expand`.
- If the recorded SHA matches the recomputed value, do not emit a finding; the plan is structurally consistent.

Record the readiness state (`draft`, `reviewed`, `loop-ready`, `running`, `complete`) alongside the SHA comparison so the report can echo it. This is a snapshot for the expansion report; `session-run` must validate the current manifest before implementation because the final preflight step below may refresh readiness. Do not edit readiness lines directly during this check; the final directory-mode preflight step below is the only place this skill may advance readiness, and it must do so by invoking `asl preflight --mark-loop-ready`.

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

## Contract Issues

<!-- Always emitted when the session had a `## Completion Contract` section to evaluate. Omit when no contract was present and Step 2 captured no contract content (e.g., single-session mode on a session that deliberately had its skeleton removed); the report should mention the omission. -->

- `<session-id>: <issue>: <suggested fix>`
- ...

_no contract issues found_
```

Use one bullet per issue in the form `<session-id>: <issue>: <suggested fix>`. When there are zero issues, replace the bullet list with the literal italic line `_no contract issues found_` so downstream readers can distinguish "checked and clean" from "not checked".

Do not emit a `## Parallel Safety` section in expansion files. Do not write a `parallel-validation.md` file in any mode; this skill no longer participates in parallel scheduling, so the artifact would be misleading. If `parallel-validation.md` already exists on disk from an older expansion run, leave it untouched — it is ignored by `session-run` and by `agent_session_loop.plans.stable_readiness_sha256`.

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
- Do not edit `manifest.md` by hand. The only allowed manifest readiness mutation is the final `asl preflight --mark-loop-ready` command in Step 7.

### Step 7: Report and Offer Fixes

In **directory mode**, after every other check is complete and any factual fixes have been applied, invoke final preflight once with `Bash`:

```text
asl preflight <plan-dir> --cwd <codebase-root> --json --mark-loop-ready
```

Use the codebase root resolved in Step 1. If the codebase root is unknown, omit `--cwd` and say so in the summary. Treat the JSON response as the authoritative cross-cutting finding set and readiness gate — this skill should not re-implement every preflight rule. The command is expected to repair manifest-readiness-only findings (missing readiness lines, stale SHA, or `draft`/`reviewed` state) **and** auto-backfill stale/missing `progress.md` (force semantics) when the only non-readiness blockers are `progress-journal-missing` findings; any such auto-repair surfaces in the JSON payload's `auto_repairs` array with `action: "rewrite"`. Once those derived blockers are cleared, `--mark-loop-ready` marks the plan `loop-ready` when no other red/yellow findings remain. Capture and surface in the summary:

- The total counts from `findings_by_severity` (red / yellow / green).
- The `reason` field (e.g., `preflight-red`, `preflight-yellow`, `ok`, `loop-ready-marked`).
- `marked_loop_ready` and the final `readiness.state`.
- A short list of categories present in the red and yellow findings (e.g., `manifest-readiness`, `missing-contract`, `malformed-contract`, `ambiguity-sentinel`, `path-reference`).

Gate the ready-to-run recommendation on this final preflight result. If the command exits non-zero, `marked_loop_ready` is false, or the final JSON `ok` is false, do **not** print the execution commands (`session-run` / `asl run`) as the next step; instead print the blocking red/yellow categories and recommend `<skill-prefix>plan-preflight-iterate <plan-dir>` before execution. Green informational findings such as `env-amx` do not block. Skip this invocation in single-session mode, and mention that complete plan-level finding counts and auto-marking require directory-mode expansion.

Print a concise summary:

1. Number of sessions expanded
2. Total claims checked and total issues found
3. Per-session one-liner: `session-<ID>: <claims> checked / <issues> issues — <worst issue description or "clean">`
4. Blocking issues highlighted prominently (wrong file paths, missing dependencies)
5. Contract issues summary: total contract issues found across the plan and a per-session breakdown only when non-zero; cite the canonical source (`agent_session_loop.contracts.parse_completion_contract`) when surfacing schema errors
6. Plan readiness summary (directory mode): recorded state, divergence outcome, and recomputed SHA prefix (first 12 hex chars) when divergence was detected
7. Final preflight snapshot (directory mode): red/yellow/green counts, `reason`, `marked_loop_ready`, final readiness state, and whether `session-run` is clean to start
8. List of annotation files created

**If issues were found**:

- Use `Edit` to immediately fix factual errors (wrong paths, incorrect signatures, wrong names) in the session `.md` files — do not ask the user first
- Only correct factual claims — never alter implementation logic, plan structure, or approach
- Report the corrections that were applied alongside the issue list
- Do **not** auto-edit contract bodies. Contract issues are emitted as suggestions in the "Contract Issues" section of each expansion file; the user must repair them by re-running `session-split` Step 9a, editing the contract by hand, or using `<skill-prefix>plan-preflight-iterate <plan-dir>` before final preflight can mark the plan loop-ready.

**If zero issues**:

- Tell the user the plan is validated and can proceed to execution only if final preflight marked or confirmed loop-ready — either via `session-run` (one session at a time, in the current conversation) or the `asl` autopilot (autonomous end-to-end supervision)
- Note they can optionally read the `.expand.md` files for concrete line numbers and patterns
- When final preflight fails despite zero expansion issues, report the remaining preflight categories and recommend `<skill-prefix>plan-preflight-iterate <plan-dir>` before `session-run`.

Only when final preflight succeeds with `ok: true` and readiness is `loop-ready`, end by printing both ready-to-paste next-step commands so the user can proceed directly to execution without switching context:

- `<skill-prefix>session-run <plan-directory>/` — run the next pending session in the current conversation, one session at a time (the default when the operator wants to stay in the loop between sessions).
- `<skill-prefix>asl asl run <plan-directory>/` — hand the whole run to the `asl` autopilot, which supervises `asl run` end to end and pauses only for human decisions it cannot resolve.

Otherwise, end with the ready-to-paste `<skill-prefix>plan-preflight-iterate <plan-directory>/` command. Note that `asl run <plan-directory>` self-heals stale/missing `progress.md` and recomputable `manifest.md` readiness metadata before pausing (surfaced via `readiness_gate_repair` in the run JSON), so the iterate skill is only required when a non-derived blocker (contracts, paths, locks, etc.) remains.

## Important Notes

- Never modify `context.md`; never modify `manifest.md` except through the final `asl preflight --mark-loop-ready` invocation in directory mode
- The `.expand.md` suffix keeps annotation files visually grouped with their session files
- Skip sessions whose Handoff status is not "pending" — the codebase has already been modified by that session's execution, so checking stale claims would be misleading. Note skipped sessions in the summary.
- This skill is sequential-by-design. It does not extract or validate `## Parallel Metadata` blocks, does not emit `## Parallel Safety` sections, and does not write `parallel-validation.md` in any mode.
- Legacy `parallel-validation.md`, `## Parallel Execution`, `### Dependency Waves`, `### Session Ownership`, `### Parallel Worker Commands`, `### Worker Prompts`, `### Serial Blockers`, and per-session `## Parallel Metadata` blocks may still exist on disk for older plans; read them only as context if helpful, never as authoritative scheduling guidance, and never regenerate them.
- New code claims (files or functions to be created) are valid and should not be flagged unless they conflict with existing code
- If the codebase root is a different repository from the current working directory, `cd` to it before running Glob/Grep/Read operations
- When applying fixes to session files, only correct factual errors — never rewrite implementation logic, change the approach, or restructure the plan
- Prefer `Grep` with `output_mode: "content"` and line numbers for signature verification — this gives the executor exact references
- Contract validation calls `agent_session_loop.contracts.parse_completion_contract` directly — never re-implement schema rules in this skill. Surface `PlanError` messages verbatim under "Contract Issues" so the wording stays aligned with `asl preflight` and `asl verify`.
- Contract issues are advisory: surface them in the `.expand.md` file's `## Contract Issues` section, but do **not** auto-edit contract bodies or default skeleton placeholders. The user repairs these and re-runs this skill or `<skill-prefix>plan-preflight-iterate <plan-dir>` so final preflight can clear the findings and mark loop-ready.
- The readiness SHA recomputation must use `agent_session_loop.plans.stable_readiness_sha256(plan_dir)` so the comparison matches the value `asl preflight` records and validates. The hash ignores legacy `parallel-validation.md`, manifest `## Parallel Execution` blocks, and per-session `## Parallel Metadata` blocks. Hash divergence is a cross-cutting plan finding, not a per-session finding.
- The `asl preflight --json --mark-loop-ready` invocation at the end of directory-mode expansion is the readiness gate for `session-run`. It may include environment findings (`env-lock`, `env-amx`, `env-turn-budget`); red/yellow findings block the ready-to-run command, while green findings are informational.
