---
name: session-split
description: Split a multi-session plan into per-session files with conservative parallel-safety metadata and scheduling guidance. Accepts a plan file path or captured inline plan text. Also proposes session boundaries if the plan lacks session structure.
allowed-tools: Bash, Read, Write
---

# Session Split

## Purpose

Parse a multi-session plan and split it into a collision-safe persistent directory of smaller, self-contained files: shared context, individual session files with handoff templates and parallel-safety metadata, and a manifest with ready-to-paste serial and parallel-aware execution commands.

## Input

Preferred input is a plan file path. In host agent command or skill invocation usage, this value is the command argument.

Determine `<skill-prefix>` once for the active harness and use it for every user-facing skill invocation you print or write into generated files:

- Codex: `$`
- Copilot and Claude: `/`
- Unknown harness: use the prefix from the user's invocation; if no prefix is visible, ask before printing or writing a ready-to-paste skill invocation

Only prefix user-facing skill invocations. Do not prefix YAML names, skill directory paths, filenames, internal prose references to skill names, or shell commands that are not skill invocations.

When printing commands or writing generated files, substitute the resolved prefix; never print or write the literal `<skill-prefix>` placeholder.

Examples:

```
$session-split ./PLAN.md
/session-split ./PLAN.md
```

If the user invokes this skill with no readable plan path, use the most recent visible `<proposed_plan>...</proposed_plan>` block in the conversation as inline plan content. If the user pasted markdown with the invocation, use that pasted markdown. If no plan content is visible, ask the user to paste the plan text; do not invent or summarize a plan.

## Instructions

### Step 1: Discover Scripts

Find the `session-split` skill directory, resolver script, and inline capture script before doing any work. Check these locations in order:

1. `~/.codex/skills/session-split`
2. `~/.copilot/skills/session-split`
3. `~/.claude/skills/session-split`
4. `~/.claude-personal/skills/session-split`
5. `./skills/session-split`

Use this discovery snippet:

```bash
skill_dir=""
for candidate in \
  "$HOME/.codex/skills/session-split" \
  "$HOME/.copilot/skills/session-split" \
  "$HOME/.claude/skills/session-split" \
  "$HOME/.claude-personal/skills/session-split" \
  "./skills/session-split"
do
  if [ -f "$candidate/scripts/resolve-plan-output.py" ] && [ -f "$candidate/scripts/capture-inline-plan.py" ]; then
    skill_dir="$candidate"
    break
  fi
done

if [ -z "$skill_dir" ]; then
  echo "ERROR: session-split scripts not found. Checked ~/.codex/skills/session-split, ~/.copilot/skills/session-split, ~/.claude/skills/session-split, ~/.claude-personal/skills/session-split, and ./skills/session-split." >&2
  exit 1
fi
```

Stop with that clear error if either `scripts/resolve-plan-output.py` or `scripts/capture-inline-plan.py` cannot be found.

### Step 2: Resolve the Source Plan

Normalize the user's input into a real source plan file path before parsing.

**Path input**: If the user provided an argument that resolves to a readable file, use that path as `<plan-file>`.

**Inline input**: If the user did not provide a readable file path, capture inline plan content:

1. Prefer the most recent visible `<proposed_plan>...</proposed_plan>` block in the conversation.
2. Otherwise, use markdown text pasted with the invocation.
3. If no inline content is visible, ask the user to paste the plan text and stop.

Pipe the inline content to the capture script:

```bash
capture_json="$(printf '%s\n' "<inline-plan-content>" | python3 "$skill_dir/scripts/capture-inline-plan.py")"
```

If the user explicitly provides a base directory, pass it through:

```bash
capture_json="$(printf '%s\n' "<inline-plan-content>" | python3 "$skill_dir/scripts/capture-inline-plan.py" --base-dir <dir>)"
```

Extract `source_path` from `capture_json` and use it as `<plan-file>` for every later step:

```bash
plan_file="$(
  CAPTURE_JSON="$capture_json" python3 - <<'PY'
import json
import os
print(json.loads(os.environ["CAPTURE_JSON"])["source_path"])
PY
)"
```

Report the captured source path in the final summary so the user can reuse it with `plan-iterate`, `plan-ideal`, `<skill-prefix>session-expand`, or a later `<skill-prefix>session-split`.

### Step 3: Read and Parse the Plan

Read the entire plan file at `<plan-file>`.

Scan for session headings by checking each line against this pattern (case-insensitive):

```
^#{2,3}\s+Session\s+(\d+)\b
```

This matches `## Session 1: Title`, `### Session 2: Title`, `## Session 3`, etc.

**Code fence awareness**: Track whether you are inside a fenced code block (lines between matching ``` or ~~~ delimiters). Skip any heading matches found inside code blocks — they are content, not structure.

Record:

- **context**: all lines from the start of the file up to (but not including) the first session heading
- **sessions**: for each session, capture the session number, title (text after the colon if any), heading level, and all lines from that heading until the next session heading or end of file

### Step 4: Surface Scan

Extracts file paths from the plan text, checks existence and line counts, and builds a per-session complexity profile.

**Proportionality rule** (keeps small plans fast):

- **< 5 file references total**: Skip this entire step
- **5–15 references**: Run sub-steps 4a–4b only (defer 4c until sessions are defined)
- **> 15 references**: Run all sub-steps

**Sub-steps:**

**4a. Extract paths**: Scan the plan for backtick-quoted paths (e.g. `` `src/foo/bar.ts` ``) and bare paths in `**File:**` lines or list items. Deduplicate the results.

**4b. Probe files**: For each extracted path, run:

```bash
wc -l <path> 2>/dev/null || echo "NEW"
```

Record a tuple for each: `(path, lines | NEW, session_number)` — where `session_number` is the session the reference appeared in (or `0` for context).

**4c. Per-session profile**: For each session compute:

- Files touched (count)
- Existing LoC sum (sum of line counts for existing files)
- New files count (count of `NEW` entries)
- Cross-cut score (number of distinct top-level directories among the paths)

**Explicit boundary**: Do NOT verify signatures, function names, or code patterns — that is `session-expand`'s job. This step only measures size and spread.

### Step 5: Parallel Safety Analysis

Build conservative parallel metadata after the surface scan and before any generated files are written. Parallelism is optional guidance; architectural correctness and collision avoidance always win.

**Inputs to consider:**

- Parsed session order, titles, and content
- Surface scan path references when available
- Explicit dependency language such as "depends on", "requires", "after", "before", "blocks", "unblocks", "handoff from", or "must run after"
- Shared context contracts and global invariants
- Write-path ownership claims in the plan, especially words such as "edit", "update", "modify", "create", "delete", "own", "owns", "scope", or "files modified"

**Build dependency edges:**

- Add an edge from session A to session B when B explicitly depends on A or must run after A.
- Add an edge when B consumes generated output, APIs, schemas, contracts, or helper code produced by A.
- Add an edge for overlapping write ownership, including exact path overlap and parent/child path overlap.
- Add an edge for shared risky files unless the plan clearly assigns one session exclusive ownership.
- Do not treat numeric session order alone as a dependency; use the plan text and shared context.

**Classify each session:**

- `parallel-safe`: concrete owned write paths, no broad or risky shared writes, and no dependency edge against any other session in the same wave.
- `parallel-candidate`: likely separable, but ownership, generated output, or dependency evidence is incomplete and should be validated by `session-expand` before concurrent execution.
- `serial-required`: has an unresolved dependency, overlapping ownership, broad directory ownership, shared risky files, or global side effects that make concurrent execution unsafe.

Treat these as not parallel-safe by default: unknown ownership, broad directory edits, shared configs, schemas, migrations, lockfiles, generated files, package-manager metadata, build-system files, parent/child path overlap, repo-wide rewrites, and instructions that ask workers to clean up or refactor outside their owned paths.

**Build dependency waves:**

- Topologically group sessions into waves using dependency edges.
- Sessions inside the same wave may be listed as safe to run together only when every session is `parallel-safe`, all owned write paths are disjoint, and no risky shared path appears in more than one session.
- Keep `parallel-candidate` sessions out of safe concurrent sets; list them separately as candidates that need manual confirmation or `session-expand` validation.
- Put any session with `serial-required` in its own wave unless a later validation step proves a narrower boundary.

Record for each session:

- Safety classification
- Required prior sessions
- Allowed concurrent sessions
- Owned write paths
- Forbidden/shared paths
- Serial blockers or uncertainty reasons
- Optional intra-session tracks, but only when the session itself contains disjoint sub-work with clear ownership

### Step 6: Handle Edge Cases

**No session markers found**: Propose session boundaries using concrete data:

1. Group work items that share files or directories into one session (use surface scan data when available)
2. Estimate session weight: plan text lines + files touched + existing LoC + subsystem spread
3. Balance sessions by weight — a session touching 15 files across 4 subsystems is far heavier than one editing 3 files in the same package; split heavy sessions, merge lightweight ones
4. Present the proposed session structure with per-session summaries: `<Title> -- <N> files, ~<L> existing lines, <subsystem(s)>`
5. If surface scan was skipped (< 5 file references), fall back to heading/affinity grouping
6. Ask the user for approval before proceeding
7. Once approved, restructure the plan content accordingly and continue to Step 7

**Single session found**: Tell the user: "Only 1 session found — splitting is not needed." Stop.

**Two or more sessions found**: Continue to Step 7.

### Step 7: Resolve Output Directory

Run the resolver and use its JSON output as the source of truth for all generated file paths:

```bash
resolver_json="$(python3 "$skill_dir/scripts/resolve-plan-output.py" "<plan-file>")"
```

If the user explicitly provides a base directory, pass it through to the resolver with `--base-dir <dir>`. Use the same base directory passed to `capture-inline-plan.py` when the source plan came from inline content.

The resolver creates a fresh collision-safe directory and never overwrites an existing output directory, so do not ask overwrite prompts.

Extract these fields from `resolver_json` and keep them available for later steps:

- `output_dir`
- `source_path`
- `source_fingerprint`
- `content_sha256`
- `slug`
- `hash_prefix`

If you need to parse fields in shell without extra dependencies, use Python stdlib:

```bash
output_dir="$(
  RESOLVER_JSON="$resolver_json" python3 - <<'PY'
import json
import os
print(json.loads(os.environ["RESOLVER_JSON"])["output_dir"])
PY
)"
```

Use `output_dir` for every generated file path. Use `source_path` as the canonical original plan path in generated content. Do not derive an alternate directory from the plan filename.

### Step 8: Write context.md

Write the context lines to `<output_dir>/context.md`.

If context is empty (the plan starts directly with Session 1), write:

```markdown
# Context

_No shared context section found in the original plan._

**Original plan**: `<source_path>`
```

### Step 9: Write session-N.md Files

For each session, write `<output_dir>/session-N.md` where N is the canonical session number from the heading. Normalize numeric IDs by parsing each dotted component as an integer before naming files: `Session 01` writes `session-1.md`, `Session 02.03` writes `session-2.3.md`, and leading zeroes must never appear in filenames, expansion references, manifest tables, or execution commands.

**File structure**:

```markdown
# Session N: <title>

> **Before starting**: Read `context.md` in this directory for shared project context.
> **Previous session**: Read the **Handoff** section at the bottom of `session-<N-1>.md`.

## Parallel Metadata

- **Safety classification**: `<parallel-safe | parallel-candidate | serial-required>`
- **Required prior sessions**: `<none | session numbers and reasons>`
- **Allowed concurrent sessions**: `<none | session numbers>`
- **Owned write paths**: `<paths this session may edit>`
- **Forbidden/shared paths**: `<paths that must not be edited, or require coordinator ownership>`
- **Serial blockers**: `<none | dependency or ownership reasons>`
- **Optional internal tracks**: `<none | disjoint track names and owned paths>`

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
- Insert the Parallel Metadata block above the preserved original session content
- **Heading normalization**: if the original session heading was `###` level, promote all headings within the session by one level so the file reads naturally as a standalone document (session title becomes `#`, its `####` sub-sections become `##`, etc.)
- Preserve fenced code blocks verbatim
- If `Optional internal tracks` is not `none`, add an `### Internal Track Worker Prompts` subsection directly below the metadata with one prompt per track.
- Emit intra-session worker prompts only when optional internal tracks have disjoint ownership. Track workers must not edit `manifest.md`, `context.md`, session handoffs, or files outside their ownership; the coordinator integrates results, updates the session handoff, and lets `session-run` sync derived status tables.

### Step 10: Write manifest.md

Write `<output_dir>/manifest.md`:

````markdown
# <Plan Title>

**Split from**: `<source_path>`
**Output directory**: `<output_dir>`
**Source fingerprint**: `<source_fingerprint>`
**Content SHA-256**: `<content_sha256>`
**Sessions**: <N>
**Split date**: <YYYY-MM-DD>

## Sessions

| #   | Title   | Status  | File         |
| --- | ------- | ------- | ------------ |
| 1   | <title> | pending | session-1.md |
| 2   | <title> | pending | session-2.md |
| ... | ...     | ...     | ...          |

## Execution

Run `<skill-prefix>session-run` once per session, preferably in a fresh harness conversation for each:

```text
<skill-prefix>session-run <output_dir>/
```

Example rendered command: `<skill-prefix>session-run ~/.local/share/agent-plans/<slug>-<hash>/`

`<skill-prefix>session-run` auto-advances — it finds the next pending session from final session handoff statuses, implements it, updates the handoff, and syncs recognized derived status tables in `manifest.md` and `context.md`. No session number needed. Repeat in new conversations until all sessions are complete.

Session handoff statuses are the source of truth. The `manifest.md` status table is derived bookkeeping for humans and automation.

## Parallel Execution

Parallel execution is optional. Use it only when the safety analysis marks sessions as `parallel-safe`; otherwise use the serial command above.

Do not run concurrent auto-advance commands:

```text
<skill-prefix>session-run <output_dir>/
```

Concurrent auto-picking can race on the same pending session. Explicit session execution is required for parallel workers and depends on `<skill-prefix>session-run <output_dir>/ --session N` support.

### Dependency Waves

| Wave | Sessions | Safe concurrent set | Notes |
| ---- | -------- | ------------------- | ----- |
| 1    | <sessions> | <session numbers or none> | <dependency and ownership summary> |

### Session Ownership

| Session | Classification | Owned write paths | Required prior sessions | Allowed concurrent sessions | Serial blockers |
| ------- | -------------- | ----------------- | ----------------------- | --------------------------- | --------------- |
| 1       | <classification> | <paths> | <none/session numbers> | <none/session numbers> | <none/reasons> |

### Parallel Worker Commands

Emit one command block per session listed in a safe concurrent set. Use these only for sessions listed in a safe concurrent set:

```text
<skill-prefix>session-run <output_dir>/ --session <N>
```

Example:

```text
<skill-prefix>session-run <output_dir>/ --session 2
```

### Worker Prompts

Emit one ready-to-use prompt per parallel-safe worker with all placeholders replaced:

```text
You are implementing Session <N> from <output_dir>/session-<N>.md.
Read context.md, session-<N>.md, and session-<N>.expand.md if present.
You own only these write paths: <owned paths>.
Do not edit these shared or forbidden paths: <forbidden/shared paths>.
You are not alone in the codebase; do not revert or overwrite changes outside your ownership.
Run the session verification that applies to your scope.
Update the Handoff section of session-<N>.md when complete. In explicit parallel runs, leave shared manifest/context status tables for the final bookkeeping sync unless this is the last pending session.
```

### Serial Blockers

- <session>: <reason, or "none">

## Token Budget

- Original plan: ~<X> lines
- Context file: ~<Y> lines
- Average session file: ~<Z> lines
- Estimated savings vs full plan per session: ~<P>%

## Execution Weight

| Session | Plan Lines | Files | Existing LoC | New Files | Subsystems | Weight |
| ------- | ---------- | ----- | ------------ | --------- | ---------- | ------ |
| 1       | ~L         | F     | ~E           | N         | S          | W      |

Weight classification:

- **light**: < 500 existing LoC AND <= 3 files
- **heavy**: > 2000 existing LoC OR > 10 files OR >= 4 subsystems
- **medium**: everything else
````

Replace all `<placeholders>` with actual computed values.

**Note**: Omit the Execution Weight section entirely if the surface scan was skipped (< 5 file references).

### Step 11: Report Results

Print a concise summary:

1. Number of sessions extracted
2. Source plan path, especially when inline content was captured
3. Output directory path
4. File listing with approximate sizes
5. The ready-to-paste execution command (the "Execute All Sessions" block from manifest.md)
6. Estimated token savings percentage
7. If the Execution Weight table was generated, highlight any "heavy" session and suggest the user consider splitting it further
8. Mention whether the manifest includes a Parallel Execution section and whether any sessions are `parallel-safe`
9. Suggest running `<skill-prefix>session-expand <output-directory>` as the next step to validate file paths, signatures, ownership, dependencies, and assumptions against the actual codebase before execution

## Important Notes

- Never modify the original plan file
- Inline plans must be saved as source markdown before splitting; do not split directly from ephemeral conversation text
- Content preservation is paramount — only restructure, never rewrite
- If a session contains "Exit criteria" or "Verification" sub-sections, they belong to that session (not to context)
- Content between `---` horizontal rules that appears between context and the first session should go at the end of context.md
- Always use `<skill-prefix>session-run` in generated execution instructions — it auto-advances through sessions and runs within the active harness conversation.
- Never promote parallel execution above correctness; uncertain ownership or dependencies must become `parallel-candidate` or `serial-required`.
- Parallel worker commands must use explicit `--session N`; never recommend concurrent auto-advance invocations.
