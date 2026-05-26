---
name: session-split
description: Split a multi-session plan into per-session files for token-efficient execution. Accepts a plan file path or captured inline plan text. Also proposes session boundaries if the plan lacks session structure.
allowed-tools: Bash, Read, Write
---

# Session Split

## Purpose

Parse a multi-session plan and split it into a collision-safe persistent directory of smaller, self-contained files: shared context, individual session files with handoff templates, and a manifest with ready-to-paste execution commands.

## Input

Preferred input is a plan file path. In host agent command or skill invocation usage, this value is the command argument. Example:

```
/session-split ./PLAN.md
```

If the user invokes `/session-split` with no readable plan path, use the most recent visible `<proposed_plan>...</proposed_plan>` block in the conversation as inline plan content. If the user pasted markdown with the invocation, use that pasted markdown. If no plan content is visible, ask the user to paste the plan text; do not invent or summarize a plan.

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

Report the captured source path in the final summary so the user can reuse it with `plan-iterate`, `plan-ideal`, `session-expand`, or a later `/session-split`.

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
- **5–15 references**: Run sub-steps 3a–3b only (defer 3c until sessions are defined)
- **> 15 references**: Run all sub-steps

**Sub-steps:**

**3a. Extract paths**: Scan the plan for backtick-quoted paths (e.g. `` `src/foo/bar.ts` ``) and bare paths in `**File:**` lines or list items. Deduplicate the results.

**3b. Probe files**: For each extracted path, run:

```bash
wc -l <path> 2>/dev/null || echo "NEW"
```

Record a tuple for each: `(path, lines | NEW, session_number)` — where `session_number` is the session the reference appeared in (or `0` for context).

**3c. Per-session profile**: For each session compute:

- Files touched (count)
- Existing LoC sum (sum of line counts for existing files)
- New files count (count of `NEW` entries)
- Cross-cut score (number of distinct top-level directories among the paths)

**Explicit boundary**: Do NOT verify signatures, function names, or code patterns — that is `session-expand`'s job. This step only measures size and spread.

### Step 5: Handle Edge Cases

**No session markers found**: Propose session boundaries using concrete data:

1. Group work items that share files or directories into one session (use surface scan data when available)
2. Estimate session weight: plan text lines + files touched + existing LoC + subsystem spread
3. Balance sessions by weight — a session touching 15 files across 4 subsystems is far heavier than one editing 3 files in the same package; split heavy sessions, merge lightweight ones
4. Present the proposed session structure with per-session summaries: `<Title> -- <N> files, ~<L> existing lines, <subsystem(s)>`
5. If surface scan was skipped (< 5 file references), fall back to heading/affinity grouping
6. Ask the user for approval before proceeding
7. Once approved, restructure the plan content accordingly and continue to Step 6

**Single session found**: Tell the user: "Only 1 session found — splitting is not needed." Stop.

**Two or more sessions found**: Continue to Step 6.

### Step 6: Resolve Output Directory

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

### Step 7: Write context.md

Write the context lines to `<output_dir>/context.md`.

If context is empty (the plan starts directly with Session 1), write:

```markdown
# Context

_No shared context section found in the original plan._

**Original plan**: `<source_path>`
```

### Step 8: Write session-N.md Files

For each session, write `<output_dir>/session-N.md` where N is the actual session number from the heading.

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

### Step 9: Write manifest.md

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

Run `/session-run` once per session, preferably in a fresh harness conversation for each:

```text
/session-run <output_dir>/
```

Example rendered command: `/session-run ~/.local/share/agent-plans/<slug>-<hash>/`

`/session-run` auto-advances — it finds the next pending session, implements it, and updates the handoff. No session number needed. Repeat in new conversations until all sessions are complete.

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

### Step 10: Report Results

Print a concise summary:

1. Number of sessions extracted
2. Source plan path, especially when inline content was captured
3. Output directory path
4. File listing with approximate sizes
5. The ready-to-paste execution command (the "Execute All Sessions" block from manifest.md)
6. Estimated token savings percentage
7. If the Execution Weight table was generated, highlight any "heavy" session and suggest the user consider splitting it further
8. Suggest running `/session-expand <output-directory>` as the next step to validate file paths, signatures, and assumptions against the actual codebase before execution

## Important Notes

- Never modify the original plan file
- Inline plans must be saved as source markdown before splitting; do not split directly from ephemeral conversation text
- Content preservation is paramount — only restructure, never rewrite
- If a session contains "Exit criteria" or "Verification" sub-sections, they belong to that session (not to context)
- Content between `---` horizontal rules that appears between context and the first session should go at the end of context.md
- Always use `/session-run` in generated execution instructions — it auto-advances through sessions and runs within the active harness conversation.
