---
name: session-split
description: Split a multi-session plan into per-session files for serial execution by `session-run`. Accepts a plan file path or captured inline plan text. Also proposes session boundaries if the plan lacks session structure.
allowed-tools: Bash, Read, Write
---

# Session Split

## Purpose

Parse a multi-session plan and split it into a collision-safe persistent directory of smaller, self-contained files: shared context, individual session files with handoff templates, and a manifest with a ready-to-paste serial execution command. The generated plan is serial-only by design — `session-run` auto-advances through sessions in dotted numeric order.

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

For each session, write `<output_dir>/session-N.md` where N is the canonical session number from the heading. Normalize numeric IDs by parsing each dotted component as an integer before naming files: `Session 01` writes `session-1.md`, `Session 02.03` writes `session-2.3.md`, and leading zeroes must never appear in filenames, expansion references, manifest tables, or execution commands.

**File structure**:

````markdown
# Session N: <title>

> **Before starting**: Read `context.md` in this directory for shared project context.
> **Previous session**: Read the **Handoff** section at the bottom of `session-<N-1>.md`.

---

<original session content here>

---

## Completion Contract

### Done when

- <one concrete human-readable exit criterion derived from the session>

### Verify

```json
{
  "session": "<canonical session id>",
  "checks": [
    {
      "type": "command",
      "argv": ["python3", "-m", "unittest", "-q"],
      "timeout_seconds": 60
    }
  ]
}
```

---

## Handoff

<!-- Fill this section when the session is complete -->

- **Status**: pending
- **Files modified**: (none yet)
- **Key decisions**: (none yet)
- **Blockers for next session**: (none)
- **Notes**:
````

Rules:

- For Session 1, omit the "Previous session" line
- Preserve all original content exactly — do not summarize, rewrite, or reformat
- Do not emit a `## Parallel Metadata` block, internal-track prompts, or any other parallel-scheduling scaffolding. The generated plan is serial-only and `session-run` ignores legacy parallel sections even if they appear on disk.
- **Heading normalization**: if the original session heading was `###` level, promote all headings within the session by one level so the file reads naturally as a standalone document (session title becomes `#`, its `####` sub-sections become `##`, etc.)
- Preserve fenced code blocks verbatim

**Completion contract emission rules** (the contract is consumed by `agent_session_loop.contracts.parse_completion_contract` and by `asl verify` / `asl run` verification enforcement, so the shape must be exact):

- Place the `## Completion Contract` block between the preserved original content and the `## Handoff` block, separated from both by `---` rules as shown.
- The contract must always sit above the final `## Handoff` heading; the parser ignores anything inside the handoff.
- `### Done when` must contain at least one non-trivial bullet. Use the session's own goal (from its title or first sentence) to populate the first bullet; do not leave generic placeholders that read like `<criterion>`.
- `### Verify` must contain exactly one fenced code block whose language is `json` (or no language tag). Other languages are rejected by the parser.
- The JSON object must include a non-empty `"checks"` list. The default skeleton emits one `"command"` check running `python3 -m unittest -q` because the package is stdlib-only. When choosing the contract command, make the repo's actual tooling authoritative over plan prose: never emit bare `python` (always `python3`, since a bare `python` is frequently not on PATH); prefer the test convention evidenced by the Step 4 surface scan (a `justfile`/`Makefile`/CI recipe, or the existing test-file style and imports) over a runner named only in prose; and when prose names a runner such as `pytest` with no corresponding evidence in the repo, keep the `python3 -m unittest -q` default rather than trusting the prose. A `command` check whose `argv[0]` does not resolve — or whose `python -m <module>` is not importable — is now rejected by the `command-not-runnable` preflight check, so an unrunnable default fails the readiness gate before a session runs. Replace the skeleton with the session-appropriate command, `file_exists` path, or `regex_in_file` pattern when the surface scan or session content makes a better default obvious.
- The optional top-level `"session"` field must match the enclosing canonical session id exactly (no leading zeroes, dotted form). Always emit it so `session-expand` can confirm contract/file alignment.
- Check `"type"` must be one of `command`, `file_exists`, `regex_in_file`. For `command`, `argv` must be a non-empty list of non-empty strings (no shell strings); `timeout_seconds` defaults to `60` and must be `<= 3600`. For `file_exists`, emit a repo-relative `path`. For `regex_in_file`, emit a repo-relative `path` and a Python-`re.compile`-valid `pattern`.
- Check `name` is optional. When omitted, the parser assigns `check-<index>` automatically; when supplied, names must be unique within the recipe.
- Never emit ambiguity sentinels (`TBD`, `TODO`, `FIXME`, `XXX`, `<placeholder>`, `<fill in>`) above the handoff. They would be caught by `asl preflight` as yellow `ambiguity-sentinel` findings.

### Step 8a: Write progress.md Skeleton

After every `session-N.md` has been written, write a skeleton `<output_dir>/progress.md` containing only the `PROGRESS_HEADER` constant (header line + `<!-- agent-session-loop progress-journal v1 -->` schema-version comment + descriptive paragraph). This way fresh plans never trigger the yellow `progress-journal-missing` preflight finding once Session 1 completes, and the runner has a well-formed file to append the first entry to.

Reuse `agent_session_loop.plans.PROGRESS_HEADER` verbatim — do not paraphrase the header or rewrite the schema-version comment by hand. `append_progress_entry` matches the version comment via regex, so any drift in the literal will cause a `PlanError` at the first runner append.

```bash
PROGRESS_OUTPUT_DIR="$output_dir" python3 - <<'PY'
import os
import shutil
import sys
from pathlib import Path

asl_path = shutil.which("asl")
if asl_path is not None:
    repo_root = Path(asl_path).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

from agent_session_loop.plans import PROGRESS_HEADER, progress_path

plan_dir = Path(os.environ["PROGRESS_OUTPUT_DIR"])
progress_path(plan_dir).write_text(PROGRESS_HEADER, encoding="utf-8")
PY
```

Behavior requirements:

- The skeleton must contain only the header — no `## Session …` entries. The runner appends the first session block when Session 1 completes and verification passes.
- `progress.md` is **runner-managed** and **excluded** from `plan_content_sha256` / `stable_readiness_sha256`. Do not list it in the manifest's `## Sessions` table; it is derived bookkeeping like `context.md` status, not a unit of work.
- Authors must never write to `progress.md` from a session; only the runner's `append_progress_entry` mutates it (`session-run` Step 6d documents this for the agent side). If a plan loses or never had a journal, `asl backfill-progress <plan-dir>` reconstructs one from completed handoffs; if a stale or malformed journal already exists, use `asl backfill-progress --force <plan-dir>` so the CLI replaces it atomically.
- If `agent_session_loop` cannot be imported (running the skill outside this repo without an installed `asl`), fail fast with a clear error pointing to `asl --version`; do not write a placeholder file. A malformed journal would surface as a yellow `progress-journal-missing` finding once Session 1 completes.

### Step 9: Write manifest.md

Write `<output_dir>/manifest.md`. The three readiness metadata lines are required so that `asl preflight` can parse the manifest immediately; their values are populated in Step 9a below.

````markdown
# <Plan Title>

**Split from**: `<source_path>`
**Output directory**: `<output_dir>`
**Source fingerprint**: `<source_fingerprint>`
**Content SHA-256**: `<content_sha256>`
**Sessions**: <N>
**Split date**: <YYYY-MM-DD>
**Readiness**: draft
**Readiness SHA-256**: `<readiness_sha256>`
**Readiness checked at**: <readiness_checked_at>

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

`<skill-prefix>session-run` auto-advances — it finds the next pending session from final session handoff statuses, implements it, updates the handoff, and syncs the recognized derived status table in `manifest.md`. No session number is needed and no explicit session selector is accepted; the plan is serial-only by design. Repeat in new conversations until all sessions are complete.

Session handoff statuses are the source of truth. The `manifest.md` status table is derived bookkeeping for humans and automation.

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

Do not emit a `## Parallel Execution` section, `### Dependency Waves`, `### Session Ownership`, `### Parallel Worker Commands`, `### Worker Prompts`, or `### Serial Blockers`. The generated manifest is serial-only and `session-run` rejects explicit `--session` selectors.

**Note**: Omit the Execution Weight section entirely if the surface scan was skipped (< 5 file references).

### Step 9a: Populate Readiness Metadata

After every `session-N.md`, `context.md`, and `manifest.md` file has been written, compute the stable readiness SHA-256 for the just-written directory and overwrite the two placeholder fields (`<readiness_sha256>` and `<readiness_checked_at>`) in `manifest.md`. The readiness state stays at `draft`; only `asl preflight --mark-loop-ready` (after the user repairs all yellow/red findings) may advance the state to `loop-ready`.

Use `agent_session_loop.plans.stable_readiness_sha256` so the hash matches the one `asl preflight` recomputes. The `asl` CLI wrapper exposes the repository root that holds the `agent_session_loop` package, so locating it from `$(command -v asl)` works on any machine where this loop is installed:

```bash
readiness_payload="$(
  OUTPUT_DIR="$output_dir" python3 - <<'PY'
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

asl_path = shutil.which("asl")
if asl_path is not None:
    repo_root = Path(asl_path).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

from agent_session_loop.plans import (
    ReadinessMetadata,
    write_readiness_metadata,
    stable_readiness_sha256,
)

plan_dir = os.environ["OUTPUT_DIR"]
sha = stable_readiness_sha256(plan_dir)
checked_at = datetime.now(timezone.utc).isoformat()
metadata = ReadinessMetadata(
    state="draft",
    readiness_sha256=sha,
    checked_at=checked_at,
)
write_readiness_metadata(plan_dir, metadata)
print(f"{sha}\t{checked_at}")
PY
)"
```

Behavior requirements:

- Compute the SHA after every other generated file is finalized; the hash depends on the contents of the session, context, manifest, and expansion (if any) files. `parallel-validation.md` is no longer hashed even if a legacy copy is present on disk. Recomputing earlier produces a hash that the next `asl preflight` will treat as a red `manifest-readiness` finding.
- Do not write a `parallel-validation.md` file. It is not generated by this skill and `agent_session_loop.plans.stable_readiness_sha256` ignores it.
- Always emit `**Readiness**: draft`. Do not advance the state in `session-split`; directory-mode `session-expand` runs final `asl preflight --mark-loop-ready` after validation, and manual preflight may do the same after repair.
- The timestamp must be a UTC ISO-8601 string. The parser only checks non-emptiness, but consumers (preflight repair hints, guided skill) display the value verbatim.
- If `agent_session_loop` cannot be imported (for example when running the skill outside this repo), fail fast with a clear error pointing to `asl --version`; do not write a placeholder SHA. A placeholder would cause `asl preflight` to emit a red `manifest-readiness` finding and block `--mark-loop-ready` until the user repairs the manifest by hand.

### Step 10: Report Results

Print a concise summary:

1. Number of sessions extracted
2. Source plan path, especially when inline content was captured
3. Output directory path
4. File listing with approximate sizes
5. The ready-to-paste serial execution command (the `## Execution` block from manifest.md)
6. Estimated token savings percentage
7. If the Execution Weight table was generated, highlight any "heavy" session and suggest the user consider splitting it further
8. State that the plan ships as `**Readiness**: draft` with a stable readiness SHA-256, and that `session-expand` will run final preflight with `--mark-loop-ready` so a clean plan is ready for `session-run`.
9. Confirm that a skeleton `progress.md` (header only) was written to the output directory. Note that the runner appends one entry per completed session automatically; authors and `session-run` workers never edit the file by hand, and it is excluded from the readiness hash. Mention `asl progress <plan-dir>` (and `--tail`, `--json`) as the inspection surface; for the rare case where the journal is missing or stale and `asl run` / `asl preflight --mark-loop-ready` cannot self-heal it (e.g., the operator wants to inspect the rebuild before continuing), `asl backfill-progress <plan-dir>` and `asl backfill-progress --force <plan-dir>` remain available as the manual fallback.
10. Suggest running `<skill-prefix>session-expand <output-directory>` next to validate file paths, signatures, contract entries, assumptions, and final loop-ready preflight against the actual codebase before execution. After expansion, `asl run` is the normal entry point; it self-heals derived progress/readiness drift before pausing, so an operator only needs `asl preflight --mark-loop-ready` (or the interactive `plan-preflight-iterate` skill) when a non-derived blocker remains.

## Important Notes

- Never modify the original plan file
- Inline plans must be saved as source markdown before splitting; do not split directly from ephemeral conversation text
- Content preservation is paramount — only restructure, never rewrite
- If a session contains "Exit criteria" or "Verification" sub-sections, they belong to that session (not to context)
- Content between `---` horizontal rules that appears between context and the first session should go at the end of context.md
- Always use `<skill-prefix>session-run` in generated execution instructions — it auto-advances through sessions and runs within the active harness conversation.
- The generated plan is serial-only by design. Do not emit `## Parallel Metadata` blocks, dependency waves, worker prompts, or any explicit `--session N` execution commands. `session-run` only accepts directory auto-advance and rejects explicit session selectors.
- Do not generate a `parallel-validation.md` file. The readiness hash ignores it even when a legacy copy lingers on disk.
- Every emitted `session-N.md` must include a parseable `## Completion Contract` above its `## Handoff`. Plans without contracts will be rejected by `asl preflight` (red `missing-contract` findings) and by `asl run`'s verification gate, which pauses with `reason="no-contract"` unless `--allow-not-loop-ready` is passed.
- `manifest.md` must carry the three readiness lines (`**Readiness**`, `**Readiness SHA-256**`, `**Readiness checked at**`) with a stable SHA produced by `agent_session_loop.plans.stable_readiness_sha256`. The initial state is always `draft`; directory-mode `session-expand` or manual `asl preflight --mark-loop-ready` may advance it after validation.
- Every split must emit a skeleton `progress.md` containing only `agent_session_loop.plans.PROGRESS_HEADER`. The journal is runner-managed, excluded from `plan_content_sha256` / `stable_readiness_sha256`, and never appears in the manifest session table. Missing journals trigger a yellow `progress-journal-missing` preflight finding once at least one session is `complete`.
