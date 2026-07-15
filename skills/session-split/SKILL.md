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

Canonical parsing is owned by `agent_session_loop.authoring.parse_plan_document`. Use its source-only grammar for session detection:

- Accept case-insensitive H2-H6 ATX headings with zero to three leading spaces and required horizontal whitespace after the opening hashes.
- Require the `Session` token followed by a positive dotted-decimal ID. Source components may be zero-padded, but the parser canonicalizes them numerically (`Session 02.03` becomes ID `2.3`); a zero component is invalid.
- Accept a bare ID or a non-empty title introduced by `:`, `-`, `–`, or `—`, with optional horizontal whitespace around the separator. Accept optional ATX closing hashes when horizontal whitespace precedes them.
- Treat headings inside matching backtick or tilde fences as content. Outside fences, surface the parser's line-specific `PlanError` directly for any malformed H2-H6 candidate beginning with the `Session` token; do not reinterpret it as ordinary content or as a plan with no markers.

The parser splits the plan into shared context lines plus per-session blocks and rejects duplicate canonical IDs. Step 8 runs it for actual file generation, so do not re-derive boundaries by hand. If existing headings match this grammar, proceed without asking for normalization approval and without rewriting the source plan merely to canonicalize their spelling, level, separator, or ID padding.

For your own reasoning in Steps 4–5 (surface scan, boundary proposal), read the plan normally and note which session each piece of content belongs to; the parser is the source of truth for the final boundaries.

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

**No session markers found**: Enter this workflow only when canonical parsing succeeds with zero explicit session headings. Parser errors are malformed-plan errors, not an invitation to propose boundaries. Propose session boundaries using concrete data:

1. Group work items that share files or directories into one session (use surface scan data when available)
2. Estimate session weight: plan text lines + files touched + existing LoC + subsystem spread
3. Balance sessions by weight — a session touching 15 files across 4 subsystems is far heavier than one editing 3 files in the same package; split heavy sessions, merge lightweight ones
4. Present the proposed session structure with per-session summaries: `<Title> -- <N> files, ~<L> existing lines, <subsystem(s)>`
5. If surface scan was skipped (< 5 file references), fall back to heading/affinity grouping
6. Ask the user for approval before proceeding
7. Once approved, write the restructured plan — original content unchanged, but organized under canonical `## Session N: <title>` headings — and capture it as a new durable source with `capture-inline-plan.py` (Step 2). Use that captured `source_path` as `<plan-file>` for Step 6 onward so the generated directory traces to a real source file.

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
- `plan_id`
- `source_path`
- `source_fingerprint`
- `content_sha256`
- `content_hash_prefix`
- `fingerprint_prefix`
- `slug`
- `hash_prefix`

Field meanings:

- `plan_id` — the unsuffixed stable base identifier, `<content_hash_prefix>-<fingerprint_prefix>`. It is the basis for the directory name but not the guaranteed directory basename: the resolver may append `-2`, `-3`, and later collision suffixes, so use `output_dir` for the actual path.
- `content_hash_prefix` — the leading `HASH_PREFIX_LENGTH` characters of the content SHA-256; identifies the plan content.
- `fingerprint_prefix` — the leading `HASH_PREFIX_LENGTH` characters of the source fingerprint; identifies the source path. Equals `hash_prefix`, which is retained as a backward-compatible alias.
- `slug` and `hash_prefix` — retained as compatibility metadata for existing JSON consumers. `hash_prefix` is the source-fingerprint prefix (the same value as `fingerprint_prefix`), not the content-hash prefix. Do not build directory names from `slug`.

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

Use `output_dir` for every generated file path — it is the only authoritative path. Do not rebuild the directory from `slug`, `plan_id`, or any other field; the resolver already applied collision suffixes, so a reconstructed path can silently disagree with the real one. Use `source_path` as the canonical original plan path in generated content. Do not derive an alternate directory from the plan filename.

### Step 7: Author Completion Contracts

Contracts are the only generated content the skill still authors; everything else is rendered deterministically in Step 8. Write one contract per session to a temp JSON file (`contracts_file`), keyed by canonical session id:

```json
{
  "1": {
    "done_when": ["<one concrete human-readable exit criterion derived from the session>"],
    "verify": {
      "session": "1",
      "checks": [
        {"type": "command", "argv": ["python3", "-m", "unittest", "-q"], "timeout_seconds": 60}
      ]
    }
  }
}
```

Contract authoring rules (validated by `agent_session_loop.authoring.contract_from_payload`, which fails closed on any malformed entry):

- `done_when` needs at least one non-trivial bullet. Use the session's own goal (from its title or first sentence); never leave generic placeholders that read like `<criterion>`.
- `verify.checks` must be non-empty. Check `type` is one of `command`, `file_exists`, `regex_in_file`; `command` checks take a non-empty `argv` list (no shell strings) and `timeout_seconds <= 3600`; `file_exists` / `regex_in_file` take repo-relative paths (and a Python-`re`-valid `pattern`). `name` is optional and auto-assigned as `check-<index>` when omitted.
- The `verify.session` field must match the session id exactly (no leading zeroes, dotted form).
- When choosing the contract command, make the repo's actual tooling authoritative over plan prose: never emit bare `python` (always `python3`); prefer the test convention evidenced by the Step 4 surface scan (a `justfile`/`Makefile`/CI recipe, or the existing test-file style and imports) over a runner named only in prose; and when prose names a runner such as `pytest` with no corresponding evidence in the repo, keep the `python3 -m unittest -q` stdlib default rather than trusting the prose. A `command` check whose `argv[0]` does not resolve — or whose `python -m <module>` is not importable — is rejected by the `command-not-runnable` preflight check, so an unrunnable default fails the readiness gate before a session runs.
- Never emit ambiguity sentinels (`TBD`, `TODO`, `FIXME`, `XXX`, `<placeholder>`, `<fill in>`); `asl preflight` flags them as yellow `ambiguity-sentinel` findings.

Optionally, also write an analysis markdown file (`analysis_file`) holding the `## Token Budget` and `## Execution Weight` sections computed from the Step 4 surface scan (omit Execution Weight if the scan was skipped). It is appended verbatim to the manifest. Do not include parallel-execution sections of any kind.

### Step 8: Generate the Plan Directory via the Authoring API

All file generation is delegated to `agent_session_loop.authoring` — fence-aware parsing, dotted-ID canonicalization (`Session 02.03` becomes `session-2.3.md`, never with leading zeroes), body-heading normalization for accepted H3-H6 session headings, verbatim content preservation, previous-session references, contract rendering, the `progress.md` skeleton, the serial-only manifest, atomic multi-file commit, and the readiness refresh. Do not hand-write any generated file.

```bash
generation_json="$(
  SPLIT_PLAN_FILE="$plan_file" \
  SPLIT_OUTPUT_DIR="$output_dir" \
  SPLIT_RESOLVER_JSON="$resolver_json" \
  SPLIT_CONTRACTS_FILE="$contracts_file" \
  SPLIT_SKILL_PREFIX="<skill-prefix>" \
  SPLIT_ANALYSIS_FILE="${analysis_file:-}" \
  python3 - <<'PY'
import json
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

from agent_session_loop import authoring, plans

plan_file = Path(os.environ["SPLIT_PLAN_FILE"])
output_dir = Path(os.environ["SPLIT_OUTPUT_DIR"])
resolver = json.loads(os.environ["SPLIT_RESOLVER_JSON"])
contracts_by_id = json.loads(
    Path(os.environ["SPLIT_CONTRACTS_FILE"]).read_text(encoding="utf-8")
)
prefix = os.environ["SPLIT_SKILL_PREFIX"]
analysis_file = os.environ.get("SPLIT_ANALYSIS_FILE") or None

text = plan_file.read_text(encoding="utf-8")
parsed = authoring.parse_plan_document(text)
if len(parsed.sessions) < 2:
    raise SystemExit(f"expected at least 2 sessions, found {len(parsed.sessions)}")

transaction = authoring.PlanTransaction(output_dir)
transaction.stage(
    "context.md",
    authoring.render_context(parsed.context_lines, resolver["source_path"]),
)
transaction.stage("progress.md", authoring.render_progress_skeleton())

manifest_sessions = []
for index, session in enumerate(parsed.sessions):
    session_id = session.session_id.value
    spec = contracts_by_id[session_id]
    contract = authoring.contract_from_payload(
        session_id, spec["done_when"], spec["verify"]
    )
    transaction.stage(
        session.session_id.filename,
        authoring.render_session_file(
            session_id=session_id,
            title=session.title,
            instruction_lines=session.instruction_lines,
            heading_level=session.heading_level,
            is_first=index == 0,
            previous_session_id=(
                parsed.sessions[index - 1].session_id if index else None
            ),
            contract=contract,
        ),
    )
    manifest_sessions.append(
        authoring.ManifestSession(
            session_id=session.session_id,
            title=session.title or f"Session {session_id}",
        )
    )

analysis_note = (
    Path(analysis_file).read_text(encoding="utf-8") if analysis_file else None
)
transaction.stage(
    "manifest.md",
    authoring.render_manifest(
        authoring.ManifestSpec(
            plan_title=authoring.title_from_plan(text),
            source_path=resolver["source_path"],
            output_dir=str(output_dir),
            plan_id=resolver["plan_id"],
            source_fingerprint=resolver["source_fingerprint"],
            content_sha256=resolver["content_sha256"],
            sessions=tuple(manifest_sessions),
            split_date=datetime.now(timezone.utc).date().isoformat(),
            readiness=plans.ReadinessMetadata(
                state="draft", readiness_sha256="pending", checked_at="pending"
            ),
            skill_prefix=prefix,
            execution_note=analysis_note,
        )
    ),
)
written = transaction.commit()
metadata = authoring.refresh_readiness_metadata(output_dir, state="draft")
print(
    json.dumps(
        {
            "session_ids": list(parsed.session_ids),
            "files": [str(path) for path in written],
            "readiness_sha256": metadata.readiness_sha256,
            "readiness_checked_at": metadata.checked_at,
        },
        sort_keys=True,
    )
)
PY
)"
```

Behavior notes:

- The transaction is atomic: a failure while staging or committing leaves no partially authored plan, and a missing/malformed contract for any session (`contract_from_payload` raises) aborts generation before any file lands.
- `render_session_file` runs in deterministic preparation mode (`require_contract=True`) — sessions without contracts cannot be rendered, which is what keeps `asl preflight` `missing-contract` findings from ever appearing in fresh splits.
- `progress.md` is the header-only skeleton (`plans.PROGRESS_HEADER`). It is runner-managed, excluded from `plan_content_sha256` / `stable_readiness_sha256`, and never listed in the manifest session table. Only the runner's `append_progress_entry` mutates it; `asl backfill-progress [--force] <plan-dir>` is the manual recovery path.
- The manifest is serial-only (no `## Parallel Execution`, waves, ownership, or worker-prompt sections) and always ships as `**Readiness**: draft` with a stable SHA computed by the same `plans.stable_readiness_sha256` that `asl preflight` recomputes. Only `asl preflight --mark-loop-ready` (typically run by directory-mode `session-expand`) advances the state.
- If `agent_session_loop` cannot be imported (running the skill outside this repo without an installed `asl`), fail fast with a clear error pointing to `asl --version`; do not fall back to hand-writing files.

### Step 9: Report Results

Print a concise summary:

1. Number of sessions extracted
2. Source plan path, especially when inline content was captured
3. Output directory path and its `Plan ID` (`<content_hash_prefix>-<fingerprint_prefix>`)
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
- Deterministic generation is owned by `agent_session_loop.authoring` (Step 8). This skill authors session boundaries and completion contracts; it never hand-writes `context.md`, `session-N.md`, `manifest.md`, or `progress.md` content.
- If a session contains "Exit criteria" or "Verification" sub-sections, they belong to that session (not to context)
- Content between `---` horizontal rules that appears between context and the first session should go at the end of context.md
- Always use `<skill-prefix>session-run` in generated execution instructions — it auto-advances through sessions and runs within the active harness conversation.
- The generated plan is serial-only by design. Do not emit `## Parallel Metadata` blocks, dependency waves, worker prompts, or any explicit `--session N` execution commands. `session-run` only accepts directory auto-advance and rejects explicit session selectors.
- Do not generate a `parallel-validation.md` file. The readiness hash ignores it even when a legacy copy lingers on disk.
- Every emitted `session-N.md` must include a parseable `## Completion Contract` above its `## Handoff`. Plans without contracts will be rejected by `asl preflight` (red `missing-contract` findings) and by `asl run`'s verification gate, which pauses with `reason="no-contract"` unless `--allow-not-loop-ready` is passed.
- `manifest.md` must carry the three readiness lines (`**Readiness**`, `**Readiness SHA-256**`, `**Readiness checked at**`) with a stable SHA produced by `agent_session_loop.plans.stable_readiness_sha256`. The initial state is always `draft`; directory-mode `session-expand` or manual `asl preflight --mark-loop-ready` may advance it after validation.
- Every split must emit a skeleton `progress.md` containing only `agent_session_loop.plans.PROGRESS_HEADER`. The journal is runner-managed, excluded from `plan_content_sha256` / `stable_readiness_sha256`, and never appears in the manifest session table. Missing journals trigger a yellow `progress-journal-missing` preflight finding once at least one session is `complete`.
