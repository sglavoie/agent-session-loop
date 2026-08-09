---
name: session-split
description: Split a multi-session plan into per-session files for serial execution by `session-run`. Accepts a plan file path or captured inline plan text. Also proposes session boundaries if the plan lacks session structure.
allowed-tools: Bash, Read, Write
---

# Session Split

## Purpose

Parse a multi-session plan and split it into a collision-safe persistent directory of smaller, self-contained files: shared context, individual session files with handoff templates, and a manifest with a ready-to-paste serial execution command. The generated plan is serial-only by design — `session-run` auto-advances through sessions in dotted numeric order.

All file generation is owned by the `asl split` subcommand, which chains the deterministic authoring API (`agent_session_loop.authoring`) end to end. This skill authors only the per-session completion contracts and (optionally) an analysis note; it never hand-writes `context.md`, `session-N.md`, `manifest.md`, or `progress.md`.

## Input

Preferred input is a plan file path; in host-agent command or skill-invocation usage this is the command argument.

**Skill-invocation prefix**: use `/` for Claude and Copilot, `$` for Codex; for an unknown harness reuse the prefix from the user's invocation (ask before printing a ready-to-paste invocation if none is visible). Prefix only user-facing skill invocations you print — never YAML names, file paths, or shell commands — and substitute the resolved prefix instead of printing the literal `<skill-prefix>`. `asl split` resolves its own generation prefix: pass `--skill-prefix auto` (detect the invoking harness) or `--skill-prefix {codex,claude,copilot}` when detection is ambiguous.

Examples:

```
$session-split ./PLAN.md
/session-split ./PLAN.md
```

If the user invokes this skill with no readable plan path, use the most recent visible `<proposed_plan>...</proposed_plan>` block in the conversation as inline plan content. If the user pasted markdown with the invocation, use that. If no plan content is visible, ask the user to paste the plan text; do not invent or summarize a plan.

## Instructions

### Step 1: Resolve the Source Plan

Normalize the user's input into a real source plan before splitting. (`asl split` performs all file generation; assume `asl` is on `PATH`.)

**Path input**: If the argument resolves to a readable file, use it as `<plan-file>` and pass it to `asl split --plan-file <plan-file>` in Step 6.

**Inline input**: If no readable file path was given, capture inline plan content:

1. Prefer the most recent visible `<proposed_plan>...</proposed_plan>` block in the conversation.
2. Otherwise, use markdown text pasted with the invocation.
3. If no inline content is visible, ask the user to paste the plan text and stop.

Pipe the captured content to `asl split --inline-stdin` in Step 6 (it persists the pasted markdown as a durable source file before splitting). Do not split directly from ephemeral conversation text.

If the user explicitly provides a base directory, pass it through with `--base-dir <dir>` in Step 6; it applies to both the captured inline source and the split output. `asl split --json` reports the resolved `source_path`; report it so the user can reuse it with `plan-iterate`, `plan-ideal`, `<skill-prefix>session-expand`, or a later `<skill-prefix>session-split`.

### Step 2: Validate and Repair the Source Plan

Session detection, boundaries, and heading canonicalization are owned by `agent_session_loop.authoring.parse_plan_document` (which `asl split` runs in Step 6) — its source-only grammar is authoritative, so do not re-derive boundaries by hand. Proceed without asking for normalization approval and without rewriting the source merely to canonicalize a heading's spelling, level, separator, or ID padding. Read the plan normally so you can reason about Steps 3–4; the parser remains the source of truth for the final boundaries.

Validate before authoring anything. `asl split --check` runs the same parser, requires no contracts, writes nothing, and — unlike the fail-fast parser — reports every malformed heading in one payload:

```bash
asl split --check --plan-file "<plan-file>" --json
```

For inline input, pipe the captured content to `asl split --check --inline-stdin --json` instead; `--check` never persists a source file, so re-checking is free.

Read the payload:

- `ok: true` — record `session_ids` (the canonical ids to key contracts by in Step 5). When the payload also carries ordered `repositories` and `registry_digest`, retain that registry context through Steps 3–7; the parser has already validated the exact top-level `## Repositories` table and every session's `Reads repositories` / `Writes repositories` scope. Continue to Step 3.
- `reason: single-session` — apply the Step 4 single-session outcome and stop.
- `reason: malformed-plan` — repair the plan, then re-check. Each finding carries `line`, `category`, `message`, `source_line`, and `suggested_line`.

**Repair loop** (at most 3 rounds):

1. For every finding with a non-null `suggested_line`, replace that line with the suggestion. These have exactly one correct rewrite, so apply them silently — this is canonicalization, not restructuring.
2. For every finding with `suggested_line: null`, the repair would change what the plan means (inventing an id, renumbering a duplicate, dropping a title). Derive a concrete proposal from the finding's `message` and the session's own body, then ask the user to approve it. Batch all such findings from the round into one question.
3. Edit only the flagged heading lines. Never alter body content, never reorder, merge, or renumber sessions beyond an approved repair, and never write to the original plan file.
4. Re-run `asl split --check` on the repaired text. Repairing one heading can expose a finding the fail-fast parser had masked (a duplicate id only becomes visible once both headings parse), so keep looping while findings remain and rounds are left.

If findings remain after 3 rounds, or the user declines a repair, surface the failure `reason`/`error` verbatim and stop.

Once any repair has been applied, the repaired text — not the original file — is the split source: pipe it to `asl split --inline-stdin` in Step 6, which persists it as a durable content-hash-addressed source file. Report the user's original path alongside the resolved `source_path`.

For any malformed `Session` heading the parser raises a line-specific `PlanError`; `asl split` returns it verbatim with reason `malformed-plan`, never as ordinary content.

### Step 3: Surface Scan

Extract file paths from the plan text, check existence and line counts, and build a per-session complexity profile. This step measures size and spread only — do NOT verify signatures, function names, or code patterns (that is `session-expand`'s job).

**Proportionality rule** (keeps small plans fast):

- **< 5 file references total**: skip this entire step
- **5–15 references**: run sub-steps 3a–3b only (defer 3c until sessions are defined)
- **> 15 references**: run all sub-steps

**3a. Extract paths**: Scan for backtick-quoted paths (e.g. `` `src/foo/bar.ts` ``) and bare paths in `**File:**` lines or list items. Deduplicate.

For a registered source, treat `Alias:path` as the canonical prose location and
resolve it beneath that alias's declared workspace-relative path. Do not guess
which repository owns an unqualified path that could exist in more than one
session read alias; record it as ambiguous for contract authoring and final
preflight. The registry read scope is advisory research context, while the
write scope is the durable mutation authority the runner will enforce.

**3b. Probe files**: For each extracted path, run `wc -l <path> 2>/dev/null || echo "NEW"`. Record a tuple `(path, lines | NEW, session_number)` — where `session_number` is the session the reference appeared in (or `0` for context).

**3c. Per-session profile**: For each session compute files touched (count), existing LoC sum, new-file count (`NEW` entries), and cross-cut score (distinct top-level directories among the paths).

### Step 4: Handle Edge Cases

**No session markers found**: Enter this workflow only when canonical parsing succeeds with zero explicit session headings — that is, `asl split --check` reported no findings and an empty `session_ids`. Parser errors are malformed-plan errors, not an invitation to propose boundaries; repair them mechanically in Step 2 instead. Propose session boundaries using concrete data:

1. Group work items that share files or directories into one session (use surface-scan data when available).
2. Estimate session weight: plan text lines + files touched + existing LoC + subsystem spread.
3. Balance sessions by weight — a session touching 15 files across 4 subsystems is far heavier than one editing 3 files in the same package; split heavy sessions, merge lightweight ones.
4. Present the proposed structure with per-session summaries: `<Title> -- <N> files, ~<L> existing lines, <subsystem(s)>`.
5. If the surface scan was skipped (< 5 file references), fall back to heading/affinity grouping.
6. Ask the user for approval before proceeding.
7. Once approved, write the restructured plan — original content unchanged, organized under canonical `## Session N: <title>` headings — save it as a new source markdown file, and pass that path to `asl split --plan-file` in Step 6 so the generated directory traces to a real source file.

**Single session found**: `asl split --check` (and `asl split`) reports reason `single-session`. Tell the user "Only 1 session found — splitting is not needed." and stop.

**Two or more sessions found**: Continue to Step 5.

### Step 5: Author Completion Contracts

Contracts are the only generated content this skill authors; everything else is rendered deterministically by `asl split`. Write one contract per session to a temp JSON file (`contracts_file`), keyed by the canonical session ids reported by the final clean `asl split --check` (never by a hand-read of the plan), and pass it as `--contracts-file`:

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

- `done_when` needs at least one non-trivial bullet derived from the session's own goal (title or first sentence); never leave generic placeholders that read like `<criterion>`.
- `verify.checks` must be non-empty. `type` is one of `command`, `file_exists`, `regex_in_file`; `command` checks take a non-empty `argv` list (no shell strings) and `timeout_seconds <= 3600`; `file_exists` / `regex_in_file` take a path (and a Python-`re`-valid `pattern`). In a registered plan, put `"repo": "Alias"` on every check and keep every `path` and command `cwd` repository-relative, non-absolute, and free of upward `..` traversal; the alias must be in that session's read scope. In a legacy plan, checks may omit `repo` and retain repo-relative or absolute `path` / `cwd` behavior. `name` is optional and auto-assigned as `check-<index>`.
- Author one authoritative gate for each logical verification surface. A check without `on_failure_of` is a gate and determines whether the contract passes. Do not emit both a focused test command and a broader command that already includes those tests as unconditional gates; prefer the repo's evidenced aggregate command when it is appropriately scoped to the session.
- A genuinely narrower troubleshooting check may set `on_failure_of` to a non-empty list of named gates. It then runs only when at least one named gate fails, is reported as a diagnostic, and never changes the aggregate pass/fail decision. Dependencies must name unconditional gates in the same recipe; self-references, unknown names, and diagnostic-to-diagnostic chains are invalid. Keep diagnostics only when their failure evidence is worth the extra runtime. Exact duplicate `argv`/`cwd` pairs are a yellow `verification-duplicate` preflight finding.
- `verify.session` must match the session id exactly (no leading zeroes, dotted form).
- `verify.parallel` is optional and defaults to `false`. Set it to `true` only when a phase has two or more `command` checks that cannot interfere with each other — no shared output directory, port, database, temp path, or lock. The runner executes gates first and eligible diagnostics afterward; within either phase it runs command checks concurrently (at most four at a time) while reporting results in declared order. When in doubt leave it unset; sequential is always correct, and baseline recording ignores the flag.
- Make the repo's actual tooling authoritative over plan prose: never emit bare `python` (always `python3`); prefer the test convention evidenced by the Step 3 surface scan (a `justfile`/`Makefile`/CI recipe, or the existing test-file style and imports) over a runner named only in prose; and when prose names a runner such as `pytest` with no corresponding evidence in the repo, keep the `python3 -m unittest -q` stdlib default. A `command` check whose `argv[0]` does not resolve — or whose `python -m <module>` is not importable — is rejected by the `command-not-runnable` preflight check, so an unrunnable default fails the readiness gate before a session runs.
- Never emit ambiguity sentinels (`TBD`, `TODO`, `FIXME`, `XXX`, `<placeholder>`, `<fill in>`); `asl preflight` flags them as yellow `ambiguity-sentinel` findings.

A session listed in the plan but missing from `contracts_file` makes `asl split` fail with reason `missing-contract:<id>` before any file lands; supply every session's contract. A present-but-malformed entry (not a JSON object, or missing `done_when`/`verify`) fails the same way with reason `invalid-contract:<id>`, also before any file lands.

Optionally, also write an analysis markdown file and pass it as `--analysis-file`. It holds the `## Token Budget` and `## Execution Weight` sections computed from the Step 3 surface scan (omit Execution Weight if the scan was skipped) and is appended verbatim to the manifest. Do not include parallel-execution sections of any kind.

### Step 6: Generate the Plan Directory via `asl split`

Run `asl split` with the source resolved in Step 1 and the contracts authored in Step 5, using `--json` and reading its output as the source of truth. `asl split` resolves a fresh collision-safe `output_dir` itself and never overwrites an existing one, so do not pre-create it or ask overwrite prompts; use the reported `output_dir` for every path you print (collision suffixes may have been applied, so never rebuild the path from `plan_id` or the source filename).

**Path input** (an unrepaired plan file):

```bash
asl split \
  --plan-file "<plan-file>" \
  --contracts-file "$contracts_file" \
  --skill-prefix auto \
  ${analysis_file:+--analysis-file "$analysis_file"} \
  ${base_dir:+--base-dir "$base_dir"} \
  --json
```

**Inline input** (pipe the captured plan content on stdin — also the path for any plan repaired in Step 2):

```bash
printf '%s\n' "<inline-plan-content>" | asl split \
  --inline-stdin \
  --contracts-file "$contracts_file" \
  --skill-prefix auto \
  ${analysis_file:+--analysis-file "$analysis_file"} \
  ${base_dir:+--base-dir "$base_dir"} \
  --json
```

The transaction is atomic: a failure while staging or committing leaves no partially authored plan, and a missing/malformed contract for any session aborts generation before any file lands. On success the `--json` payload reports `output_dir`, `plan_id` (`<content_hash_prefix>-<fingerprint_prefix>`), `source_path`, `session_ids`, `files`, `readiness` (always `draft` with a stable SHA), `next_steps` (per-harness `session-expand` invocations), and the resolved `skill_prefix`. On failure it reports `ok: false` with a stable `reason`: `single-session`, `malformed-plan` (line-specific `PlanError` verbatim), `missing-contract:<id>`, `invalid-contract:<id>` (a present contract entry that is not a JSON object or is missing `done_when`/`verify`), or `input-error`. Do not hand-write any generated file; surface any failure `reason`/`error` to the user and stop.

### Step 7: Report Results

Echo the `asl split --json` output as a concise summary:

1. Number of sessions extracted (`session_ids`).
2. Source plan path (`source_path`), especially when inline content was captured. If Step 2 repaired any heading, also print the user's original path and one `line <N>: <category>` line per repair so the change is auditable.
3. Output directory (`output_dir`) and its `Plan ID` (`plan_id`). For a registered plan, also print the ordered alias/path registry from `repositories` and its credential-free `registry_digest`; do not print credentials or invent resolved roots during splitting.
4. File listing (`files`) with approximate sizes.
5. The ready-to-paste serial execution command (the `## Execution` block from `manifest.md`).
6. Estimated token savings percentage; if an Execution Weight table was generated, highlight any "heavy" session and suggest splitting it further.
7. State that the plan ships as `**Readiness**: draft` with a stable readiness SHA-256, that a skeleton runner-managed `progress.md` was written (inspect with `asl progress <plan-dir>`, `--tail`, `--json`; `asl backfill-progress [--force] <plan-dir>` is the manual recovery path), and that `session-expand` will run final preflight with `--mark-loop-ready`.
8. Suggest running `<skill-prefix>session-expand <output-dir>` next (the `next_steps` field already renders the per-harness invocation) to validate paths, signatures, contracts, assumptions, and final loop-ready preflight before execution. After expansion, `asl run` is the normal entry point and self-heals derived progress/readiness drift before pausing, so an operator only needs `asl preflight --mark-loop-ready` (or `plan-preflight-iterate`) when a non-derived blocker remains.

## Important Notes

- Never modify the original plan file; only restructure (never rewrite) content, and save inline plans as source markdown before splitting (`asl split --inline-stdin` does this). A plan repaired in Step 2 follows the same rule: the repaired text is persisted as a new source file, and the user's original is left byte-identical.
- Deterministic generation is owned by `asl split` / `agent_session_loop.authoring`. This skill authors only session boundaries and completion contracts; it never hand-writes `context.md`, `session-N.md`, `manifest.md`, or `progress.md`.
- The generated plan is serial-only. Do not emit `## Parallel Metadata`, dependency waves, worker prompts, or explicit `--session N` commands; do not generate `parallel-validation.md` (the readiness hash ignores it even when a legacy copy lingers). `session-run` accepts only directory auto-advance.
- Every emitted `session-N.md` carries a parseable `## Completion Contract` above its `## Handoff`; plans without contracts are rejected by `asl preflight` (red `missing-contract`) and by `asl run`'s verification gate (`reason="no-contract"` unless `--allow-not-loop-ready`).
- Registered plans carry the authoritative ordered registry only in `manifest.md`. Their sessions retain exact read/write scope fields, their completion checks are repository-attributed, and completed handoffs use `Alias:path` entries limited to write aliases. Legacy plans keep the v1 single-root syntax and JSON compatibility surface.
- `manifest.md` ships `**Readiness**: draft` with a stable SHA from `agent_session_loop.plans.stable_readiness_sha256`; only `asl preflight --mark-loop-ready` (typically via directory-mode `session-expand`) advances it after validation.
- `progress.md` is a runner-managed header-only skeleton (`agent_session_loop.plans.PROGRESS_HEADER`), excluded from `plan_content_sha256` / `stable_readiness_sha256`, and never appears in the manifest session table.
