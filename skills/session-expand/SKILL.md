---
name: session-expand
description: Validate and expand split session files against the actual codebase. Verifies file paths, signatures, assumptions, completion contracts, and manifest readiness. Sequential-by-design - no parallel safety analysis. Takes a plan directory or single session file path.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Session Expand

## Purpose

Research the actual codebase to validate and expand claims in session files produced by `session-split`. For each session, verify that referenced file paths exist, function/struct/class names are correct, code patterns match reality, and inter-session dependencies are satisfiable. Validate each session's `## Completion Contract` with `asl contract-check`, re-resolve referenced paths/commands against the live codebase, then run final preflight with `--mark-loop-ready` so clean expanded plans are ready for `session-run`. This skill is a thin expert/manual wrapper over `agent_session_loop.prepare` (via `asl expand-ingest`): the research findings are assembled into a versioned validator result JSON that the module parses fail-closed, stores in the derived `prepare-validation.json` aggregate, renders into a concise `.expand.md` annotations file alongside each session, and uses to apply exact factual corrections to session files — malformed or incomplete findings can never mutate the plan.

This skill is sequential-by-design. It does not extract, validate, or generate parallel-safety metadata, and it does not write `parallel-validation.md`. Legacy `## Parallel Metadata` blocks and any pre-existing `parallel-validation.md` on disk are ignored.

## Input

The user provides a path as the host-agent command or skill-invocation argument. Two forms are accepted:

- **Directory path** (ends with `/` or is a directory): expand all canonical session files in the directory.
- **Single session file path** (ends with `.md`): expand only that session file.

Session IDs use `[1-9][0-9]*(\.[1-9][0-9]*)*`; session files are named `session-<ID>.md`, expansion files `session-<ID>.expand.md`.

**Skill-invocation prefix**: use `/` for Claude and Copilot, `$` for Codex; for an unknown harness reuse the prefix from the user's invocation (ask before printing a ready-to-paste invocation if none is visible). Prefix only user-facing skill invocations you print — never YAML names, file paths, or shell commands — and substitute the resolved prefix instead of printing the literal `<skill-prefix>`.

## Instructions

### Step 1: Discover and Read Plan Files

**Directory mode**:

1. Read `context.md` for the project scope and referenced files.
2. Read `manifest.md` for the session list, titles, and optional authoritative ordered `## Repositories` table. When a registry exists, also read each session's exact `Reads repositories` / `Writes repositories` scope and preserve case-sensitive aliases. Ignore any legacy `## Parallel Execution`, `### Dependency Waves`, `### Session Ownership`, `### Parallel Worker Commands`, `### Worker Prompts`, or `### Serial Blockers` sections — this skill is sequential-by-design and these blocks are no longer authoritative scheduling guidance.
3. Collect session files whose basename matches `session-<ID>.md` with `<ID>` matching `[1-9][0-9]*(\.[1-9][0-9]*)*`. Exclude `.expand.md` files.
4. Sort by dotted numeric order: compare dot-separated components as integers; when all compared components are equal, the shorter ID sorts first (`2` before `2.1`; `2.10` after `2.2`).

**Single file mode**:

1. Derive the plan directory from the file's parent; read `context.md` from there.
2. Verify the basename matches exactly `session-<ID>.md`.
3. Process only that file and write the corresponding `session-<ID>.expand.md`.

**Determine the validation root by plan mode.** Without a registry, use the legacy codebase root: derive it from `context.md` and repository/build references, use the current working directory when it matches, and ask for the repository root when a different root is required or the choice is ambiguous. If the plan names locations in more than one Git repository, no legacy root exists: never resolve the ambiguity by picking one child checkout. Stop and require the registry — `asl prepare` and `asl expand-ingest` refuse such a plan with `reason: "repository-registry-required"` regardless of which root is offered. With a registry, run `asl contract-check <plan-dir> --cwd <candidate> --json` and use its `workspace_root`; stop and ask on any `workspace-root`, `repository-root`, or `repository-origin` finding. Never silently rebind an existing plan.

### Step 2: Extract Claims from Each Session

For each session file, scan all content **above** the `## Handoff` section for verifiable claims, organized into:

**P — Path claims**: file paths in `**File:**` lines, inline backtick references, or code-block comments.

**S — Signature claims**: function, method, struct, type, interface, or class definitions shown in code or prose. Record name and expected signature.

**F — Field/property claims**: struct fields, interface methods, config keys, or constants referenced by name with expected types/values.

**D — Dependency claims**: packages, modules, imports, or external tools that must exist.

**B — Behavioral claims**: assertions about how existing code works (e.g., "this method returns X").

**N — Naming claims**: variable/constant/enum/command names referenced as already existing.

**R — Removal/alteration claims**: language that takes existing behavior away or changes it rather than describing new code — "remove", "delete", "drop", "replace", "no longer", "stop doing", "simplify to", "collapse into" — and corrections phrased as fixes to how existing code behaves. Record the symbol, file, or behavior the session intends to remove or alter. This is the Chesterton's fence claim type: an R claim is not answered by confirming the target exists (that is a P/S/N claim), it is answered by establishing why it exists.

For each claim, record the type (P/S/F/D/B/N/R), the exact text from the session, and the section it appears in. In registered mode, also record its `repo` alias from the qualified `Alias:path` claim or session context and require it to be in that session's declared read scope; do not guess an alias for ambiguous unqualified prose. Ignore any legacy `## Parallel Metadata` block — do not extract, validate, or surface its fields.

Separately collect each session's `## Completion Contract` section when present: the `### Done when` bullets, the `### Verify` fenced ```json``` block, and any top-level `"session"` field. Treat the contract as its own structural artifact, validated in Step 3 under "Contract claims (C)"; do not double-report `argv`/`path`/`pattern` references from inside it as P/S/F/D/B/N/R claims.

In **directory mode**, also note the manifest's readiness metadata block (`**Readiness**`, `**Readiness SHA-256**`, `**Readiness checked at**`) for the Step 7 preflight; do not treat missing readiness metadata as a path/signature claim issue.

### Step 3: Research the Codebase

Verify each claim using the most efficient tool:

- **Path (P)**: `Glob` for existence; if missing, search for similar filenames nearby (different directory/extension); record the correct path if found elsewhere.
- **Signature (S)**: `Grep` for the definition; `Read` the exact current signature with line number; record claimed vs actual if they differ; if absent, check whether the session intends to _create_ it (valid).
- **Field (F)**: `Grep` the definition; `Read` to confirm the field exists and the type matches; record actual names/types if different.
- **Dependency (D)**: `Glob`/`Grep` for import paths; `command -v <tool>` for external tools.
- **Behavioral (B)**: `Read` the referenced code and verify the assertion; record actual behavior with a line-number range.
- **Naming (N)**: `Grep` to confirm the name exists; record the location or flag missing.
- **Removal (R)**: establish the origin rationale before the session removes or alters the code. Three sources, in order of cost: `git log -S '<symbol>' -- <path>` for the introducing commit and its message, `git log -L <start>,<end>:<path>` (or `git blame -L`) for the surrounding range when `-S` returns nothing useful, and a `Grep` for tests that cover the behavior. Record the rationale — commit subject, blame range, or covering test path — in the claim's `location`, since that is where the renderer surfaces it. When none of the three establishes why the code exists, emit a `removal-unjustified` issue rather than passing the claim silently: an unexplained fence is a finding, not a clean check.

**Contract claims (C)** (per session, when Step 2 captured a `## Completion Contract`):

Do not re-implement the contract schema. Run the authoritative static checker, which wraps `agent_session_loop.contracts.parse_completion_contract` and resolves every check against the codebase without executing any command:

```bash
asl contract-check <plan-dir> --cwd <root> --json
```

Here `<root>` is the legacy repository root or registered workspace root.

(In single-file mode add `--session <ID>`.) Each check produces a row with `status` (`ok`, `schema-error`, `missing-contract`, or `factual-issue`), a `reason` (`command-not-runnable`, `file-missing`, `regex-not-matched`, or `bare-python`), and a human-readable `detail`. For every non-`ok` row, copy its `detail` verbatim into that session's `contract_problems` (Step 6) with `contract_checked: true`:

- `schema-error` — the parser names the offending field, session id, and rule (e.g. `session 2 contract check #1 'type' must be one of: command, file_exists, regex_in_file`); surface it verbatim.
- `missing-contract` — ask the user to add a `## Completion Contract` section. The canonical split skill always emits one, so a missing contract means the session file was edited (do not flag a session deliberately split without a skeleton).
- `factual-issue` / `bare-python` — flag "use `python3`" (an unversioned `python` is frequently absent even where `python3` exists). Only unversioned `python` is flagged; versioned `python3 -c`/`python3 -m`/script invocations are valid and never raise this reason.
- `factual-issue` / `command-not-runnable` — the authoritative red (unresolved executable) / yellow (non-importable module) finding also comes from the final `asl preflight` in Step 7; surface the contract-check `detail` rather than pre-declaring the tool installed.
- `factual-issue` / `file-missing` or `regex-not-matched` — a missing file or a pattern that matches zero times is almost always stale.

Also flag the default skeleton placeholder bullet (`<one concrete human-readable exit criterion derived from the session>` or any angle-bracketed `<...>` text in `Done when`) as a contract problem asking for a real criterion. Bundle every `ok` contract check into the per-session verified-claims tally, but do not surface `ok` checks as separate "Verified Claims" rows — reserve that table for the P/S/F/D/B/N/R taxonomy.

**Token efficiency rules**:

- Read files once and extract multiple claims from the same file.
- Batch related Glob/Grep calls where possible.
- Do NOT re-read content already present in `context.md`.
- Stop researching a file after confirming all claims about it.
- For large sessions (>200 lines), prioritize Path and Signature claims first — these cause the most execution failures.
- Cap R research: `git log` is the most expensive tool in this skill, so budget one `git log -S` plus at most one range read (`git log -L` or `git blame -L`) plus one test `Grep` per removed symbol. Research removals of code that already exists on disk only — never run history commands against code an earlier session in the same plan creates, which has no history to read and belongs in a `to_be_created` claim instead.

### Step 4: Cross-Session Dependency Check

**Skip this step when processing a single session file.**

When processing all sessions, verify the serial work-session ordering is internally consistent. This is the only cross-session sanity check the skill performs; concurrency analysis, ownership overlap detection, and parallel classification are intentionally absent because the execution model is sequential-by-design.

1. **Creation order**: if session `<ID>` references a file/function an earlier session `<PRIOR_ID>` in dotted numeric order should create, confirm the earlier session's scope actually covers that creation.
2. **Handoff consistency**: if a completed session has a filled Handoff, check its "Files modified" list matches what later sessions assume exists.
3. **Missing gaps**: flag anything no session creates but a later session depends on.

Record only "later session assumes earlier work that no prior session covers" findings. Do not classify sessions as `parallel-safe`/`serial-required`/`blocked`, validate ownership overlap, or emit any parallel-validation outputs. The manifest readiness block captured in Step 2 is a snapshot only — the final directory-mode `asl preflight --mark-loop-ready` (Step 7) is the authoritative readiness gate and the only place this skill may advance readiness. Do not edit readiness lines by hand.

Also advise, without inventing a preflight failure, when contracts do not exercise the claimed environment or integration, when an expensive check is needlessly repeated across sessions, or when the first real integration proof is late. Keep factual corrections separate from scope or design choices: factual errors flow through `corrections`; boundary, scope, and contract tradeoffs require the user’s explicit decision and remain advisory until then.

### Step 5: Identify Patterns to Reuse

For each session, find existing codebase patterns the implementation should follow. Record **at most 3** per session — only the most directly relevant:

- Similar functions or methods the new code should mirror.
- Test file conventions (naming, helper functions, assertion style).
- Error-handling patterns in the same package or module.
- Style patterns (UI framework usage, CLI command structure).

Include the file path and line range for each. Prefer `Grep` with `output_mode: "content"` and line numbers so the executor gets exact references.

### Step 6: Emit Validator Output via `asl expand-ingest`

Do not hand-write `session-<ID>.expand.md` files and do not hand-edit session files to apply corrections. All validation, rendering, and plan mutation flow through `agent_session_loop.prepare` behind `asl expand-ingest`: the LLM researches (Steps 2–5) and supplies structured findings; Python validates them fail-closed and owns every file write.

Assemble the findings for each session into a validator result JSON payload. Legacy plans use `prepare.LEGACY_PREPARE_VALIDATION_VERSION` = 1 with `codebase_root`. Registered plans must use `prepare.PREPARE_VALIDATION_VERSION` = 2 with the canonical `workspace_root` and the exact ordered repository vector echoed from the authoritative registry and resolved roots:

```json
{
  "version": 1,
  "codebase_root": "<codebase root from Step 1>",
  "sessions": [
    {
      "session_id": "<ID>",
      "title": "<session title>",
      "claims": [
        {"claim": "...", "claim_type": "P|S|F|D|B|N|R", "location": "path/to/file.go", "line": 42, "to_be_created": false},
        {"claim": "session removes `legacyRetry`", "claim_type": "R", "location": "path/to/file.go — added by a1b2c3d 'retry flapping backends'; covered by path/to/file_test.go:88", "line": 42, "to_be_created": false}
      ],
      "issues": [{"issue_type": "path-missing|signature-mismatch|field-missing|dependency-missing|behavior-incorrect|name-missing|repository-ambiguous|removal-unjustified", "summary": "...", "session_ref": "<quoted text>", "actual": "...", "fix": "..."}],
      "details": [{"path": "path/to/file.go", "notes": ["Lines X-Y: ..."]}],
      "patterns": [{"name": "...", "location": "file:line-range", "reason": "..."}],
      "dependencies": [{"direction": "requires|provides", "session_id": "<ID>", "description": "..."}],
      "contract_checked": true,
      "contract_problems": [{"message": "<verbatim detail from Step 3 asl contract-check>", "suggested_fix": "..."}],
      "corrections": [{"expected_text": "<exact original text>", "replacement_text": "<exact corrected text>", "reason": "..."}],
      "spark": {"eligible": false, "reason": "<concise, evidence-based>"}
    }
  ]
}
```

Registered top-level shape:

```json
{
  "version": 2,
  "workspace_root": "/canonical/workspace",
  "repositories": [
    {"alias": "SVC", "path": "tundra_svc", "origin": "github.com/tundra/SVC", "root": "/canonical/workspace/tundra_svc"}
  ],
  "sessions": [
    {
      "session_id": "1",
      "title": "Example",
      "claims": [
        {"claim": "session drops the `SVC` legacy retry shim", "claim_type": "R", "location": "internal/retry/shim.go — added by a1b2c3d 'retry flapping backends'; covered by internal/retry/shim_test.go:120", "line": 31, "to_be_created": false, "repo": "SVC"}
      ],
      "issues": [
        {"issue_type": "removal-unjustified", "summary": "no origin rationale found for removing `parseLegacyHeader`", "session_ref": "remove the legacy header parser", "actual": "git log -S found no introducing commit inside the checkout's history and no test covers it", "fix": "state why the parser exists, or confirm with the user that it is dead code before the session removes it", "repo": "SVC"}
      ],
      "details": [],
      "patterns": [],
      "dependencies": [],
      "contract_checked": false,
      "contract_problems": [],
      "corrections": [],
      "spark": {"eligible": true, "reason": "single-file removal with a settled decision and an exact completion contract"}
    }
  ]
}
```

For version 2, every object in `claims`, `issues`, `details`, `patterns`,
`dependencies`, and `corrections` must also carry a `repo` alias from that
session's read scope, and all locations are repository-relative or qualified
when rendered. In either version, report a path that could belong to more than
one repository, or that resolves outside the bound root, as a
`repository-ambiguous` issue instead of selecting an alias.

In both versions an `R` claim carries the Step 3 origin rationale inside its
`location` (there is no dedicated rationale field — the renderer surfaces
`location` verbatim in the Verified Claims table), and a removal whose rationale
could not be established is reported as a `removal-unjustified` issue naming the
symbol, the history commands that came back empty, and what the session must
state before removing it. Both values are accepted by
`prepare.CLAIM_TYPES` / `prepare.ISSUE_TYPES`; anything outside those tuples is
rejected fail-closed with nothing written.

Every session entry carries `spark: {eligible, reason}` in both versions. Set `eligible: true` only for a small explicit scope with settled decisions, sufficient local context, and a clear completion contract; set it `false` for broad refactors, public API or schema changes, migrations, high-risk work, or unresolved uncertainty. The `reason` must be concise and evidence-based. Emit this marker for every session, whether or not the run will use `--spark`: an omitted marker is accepted silently and degrades that session to baseline routing forever, so omission is indistinguishable from a deliberate "ineligible" verdict. `asl expand-ingest --json` reports `spark_missing`, the ingested session ids whose marker was dropped; a non-empty list means that payload must be re-ingested carrying it.

The parser rejects (fail-closed, `PlanError`, nothing written) unknown keys, empty required strings, unknown `claim_type`/`issue_type`/`direction`, invalid/duplicate session ids, and more than 3 `patterns` per session. Claims about code to be _created_ set `"to_be_created": true` (no `line`) and render as "N/A — to be created" instead of being flagged as issues. `contract_problems` requires `contract_checked: true` and takes the Step 3 findings verbatim. `corrections` are exact factual text replacements only (`expected_text != replacement_text`) — never design changes, plan restructuring, or contract-body edits; report those as `issues` or `contract_problems`.

Process sessions in bounded batches (`prepare.DEFAULT_VALIDATION_BATCH_SIZE` = 4 sessions per payload; `prepare.batch_sessions` semantics). Write each batch payload to a JSON file and ingest it:

```bash
asl expand-ingest <plan-dir> --batch-file <batch-payload>.json --json
```

`asl expand-ingest` parses the payload fail-closed **before** any disk write, merges it into `prepare-validation.json` (the derived aggregate, excluded from the stable readiness hash by construction), renders every validated session's `session-<ID>.expand.md`, idempotently inserts the `> **Expansion notes**:` reference line into `session-<ID>.md`, and applies each proposed correction — all in one atomic transaction. Re-running a batch is a no-op for already-rendered content. Its `--json` payload reports `sessions`, `files`, `corrections`, `spark_missing`, and `correction_summary` (`applied` / `already_applied` / `blocked`). A correction applies only when its `expected_text` matches exactly once outside the `## Completion Contract` and `## Handoff` sections; every other outcome is `blocked` with a stable reason (`not-found`, `ambiguous`, `contract-body`, `handoff-body`, `structural`) and left unapplied. Collect the `applied` fixes and surface every `blocked` outcome to the user as a blocker requiring manual judgment. A malformed payload fails with `reason: "validator-schema"` and the `PlanError` message verbatim, before any correction reaches disk.

The renderer emits one bullet per contract problem as `<session-id>: <issue>: <suggested fix>`; when `contract_checked` is true and `contract_problems` is empty it emits the literal `_no contract issues found_` (distinguishing "checked and clean" from "not checked"), and omits the section entirely when `contract_checked` is false. Supply empty lists rather than placeholder content — empty sections are omitted automatically. Do not emit a `## Parallel Safety` section or write `parallel-validation.md`; leave any pre-existing copy untouched.

### Step 7: Report and Offer Fixes

In **directory mode**, after every other check is complete and corrections have been ingested, invoke final preflight once with `Bash`:

```text
asl preflight <plan-dir> --cwd <root> --json --mark-loop-ready
```

Use the legacy repository root or registered workspace root resolved in Step 1; if it is unknown, omit `--cwd` and say so in the summary. Treat the JSON response as the authoritative cross-cutting finding set and readiness gate — do not re-implement preflight rules. In registered mode, require its ordered workspace projection to match the registry and surface root, origin, overlap, scope, or repository-ambiguous findings rather than weakening them. It repairs manifest-readiness-only findings (missing readiness lines, stale SHA, `draft`/`reviewed` state) **and** auto-backfills stale/missing `progress.md` (force semantics, surfaced in the payload's `auto_repairs` array with `action: "rewrite"`) when the only non-readiness blockers are `progress-journal-missing` findings, then marks the plan `loop-ready` when no other red/yellow findings remain. Drive the ready-to-run gate off the payload's `reason` (e.g. `preflight-red`, `preflight-yellow`, `ok`, `loop-ready-marked`), `marked_loop_ready`, `readiness.state`, `next_steps`, and `findings_by_severity` (red/yellow/green counts). Skip this invocation in single-file mode, and say that plan-level finding counts and auto-marking require directory-mode expansion.

Print a concise summary:

1. Number of sessions expanded.
2. Total claims checked and total issues found, with the `R` (removal) claims researched and the `removal-unjustified` issues raised called out as their own subtotals.
3. Per-session one-liner: `session-<ID>: <claims> checked / <issues> issues — <worst issue or "clean">`.
4. Blocking issues highlighted prominently (wrong file paths, missing dependencies), and every `removal-unjustified` finding listed by symbol — a removal nobody can justify is a decision for the user, not a clean check.
5. Contract issues: total across the plan and a per-session breakdown when non-zero; cite `agent_session_loop.contracts.parse_completion_contract` for schema errors.
6. Correction outcomes: the `applied` fixes and every `blocked` outcome with its reason (`not-found`, `ambiguous`, `contract-body`, `handoff-body`, `structural`).
7. Final preflight snapshot (directory mode): red/yellow/green counts, `reason`, `marked_loop_ready`, final `readiness.state`, and whether `session-run` is clean to start.
8. List of annotation files created.

Factual errors (wrong paths, incorrect signatures, wrong names) are fixed exclusively through the `corrections` ingested in Step 6 — do not ask first, never bypass the module with direct `Edit` calls on session files, and never alter implementation logic, plan structure, or approach. Do **not** auto-edit contract bodies or default skeleton placeholders: contract issues are advisory suggestions in each `.expand.md`'s `## Contract Issues` section; the user repairs them by re-running `session-split`, editing the contract by hand, or using `<skill-prefix>plan-preflight-iterate <plan-dir>`.

Gate the ready-to-run recommendation on this final preflight result. Only when final preflight succeeds with `ok: true` and readiness is `loop-ready`, end by printing both ready-to-paste next-step commands so the user can proceed without switching context:

- `<skill-prefix>session-run <plan-dir>/` — run the next pending session in the current conversation, one session at a time (the default when the operator wants to stay in the loop between sessions).
- `<skill-prefix>asl asl run <plan-dir>/ --harness claude --review-harness codex [--max-review-fix-cycles N]` — hand the whole run to the `asl` autopilot, which supervises `asl run` end to end (Claude for implementation, Codex for final review), pausing only for human decisions it cannot resolve. Default to `--harness claude --review-harness codex` unless the user requested different harnesses, and append the `[--max-review-fix-cycles N]` placeholder whenever a `--review-harness` is present. Do not append these flags to the `session-run` invocation; `session-run` accepts only the plan directory and runs in the current conversation's active harness.

Otherwise (the command exits non-zero, `marked_loop_ready` is false, or the final JSON `ok` is false), do **not** print the execution commands; print the blocking red/yellow categories (e.g. `manifest-readiness`, `missing-contract`, `malformed-contract`, `ambiguity-sentinel`, `path-reference`) and end with the ready-to-paste `<skill-prefix>plan-preflight-iterate <plan-dir>/` command. Note that `asl run <plan-dir>/ --harness claude --review-harness codex [--max-review-fix-cycles N]` self-heals stale/missing `progress.md` and recomputable readiness metadata before pausing (surfaced via `readiness_gate_repair` in the run JSON), so the iterate skill is only required when a non-derived blocker (contracts, paths, locks) remains. Green informational findings such as `env-amx` do not block.

## Important Notes

- Mental models: read the sibling reference `../references/mental-models.md` on demand (the same relative sibling read `plan-ideal/SKILL.md:29` uses) rather than inlining it, and cite at most 3 models by name per report. This skill's assigned models are **Chesterton's fence** — why does this code exist, before a session removes or alters it, which is what an `R` claim answers and what `removal-unjustified` records the absence of — and **map vs territory**, the reason every claim is re-checked against the repository instead of trusted from the plan.
- Never modify `context.md`; never modify `manifest.md` except through the final `asl preflight --mark-loop-ready` invocation in directory mode.
- Skip sessions whose Handoff status is not "pending" — the codebase has already been modified by that session's execution, so checking stale claims would mislead. Note skipped sessions in the summary.
- New code claims (files or functions to be created) are valid and should not be flagged unless they conflict with existing code.
- This skill is sequential-by-design: it does not extract or validate `## Parallel Metadata`, emit `## Parallel Safety` sections, or write `parallel-validation.md`. Read any legacy parallel blocks only as context, never as authoritative scheduling guidance, and never regenerate them.
- All expansion writes, reference-line insertions, and factual fixes flow through `asl expand-ingest` (`agent_session_loop.prepare`) — never hand-write `.expand.md` files or `Edit` session files directly. `prepare-validation.json` is a derived, regenerable aggregate excluded from the stable readiness hash, never authoritative over the session files themselves.
- Emit version 2 whenever the `asl contract-check` payload carries `repositories`; `expand-ingest` refuses version 1 otherwise, with `reason: "repository-footprint"` when the plan reaches beyond one repository while declaring no registry. That refusal is repaired by declaring the registry (its `repository_footprint.suggested_registry_table` is the block to paste), not by re-emitting the payload.
- `expand-ingest` also refuses with `reason: "repository-registry-required"` when the plan *intends* to reach more than one repository — including contract targets no session has created yet, which the footprint check cannot see. The `repository_intent.git_roots` list names the repositories to register. Repair it by running `asl register-repositories <plan-dir> --cwd <bound-root> --json` — `--cwd` is the root the plan is currently bound to, and the workspace root is what the command derives from it — which produces the top-level `## Repositories` table, the per-session `**Reads repositories**:` and `**Writes repositories**:` scopes, and a `repo` alias plus repository-relative `path`/`cwd` on every contract check from disk, then follow its `next_commands` (`asl prepare <plan-dir> --resume --rebind-workspace --cwd <workspace-root>` first). Never hand-write a row the command did not derive, and never narrow the plan to whichever checkout happens to be the current working directory. A `refused` result names the one missing fact (`workspace-root`, `repository-origin`, `contract-path-outside-registry`, `command-check-repo`, or `completed-handoff-paths`) and writes zero bytes; surface that fact and stop. `completed-handoff-paths` is the part-way plan: a registry makes a completed handoff's bare `Files modified` entries illegal, so qualify each one as `Alias:path` and re-run the migration.
- Contract validation is owned by `asl contract-check` (`agent_session_loop.contracts.parse_completion_contract`); surface its `detail` verbatim under "Contract Issues" so the wording stays aligned with `asl preflight` and `asl verify`. Contract issues are advisory — do not auto-edit contract bodies or skeleton placeholders.
- The readiness SHA and manifest-readiness finding are owned by the final `asl preflight` (`agent_session_loop.plans.stable_readiness_sha256`); hash divergence at expansion time almost always means a session file was edited between `session-split` and `session-expand`. The hash ignores legacy `parallel-validation.md`, manifest `## Parallel Execution` blocks, and per-session `## Parallel Metadata` blocks.
- The `asl preflight --json --mark-loop-ready` invocation ending directory-mode expansion is the readiness gate for `session-run`; red/yellow findings block the ready-to-run command, while green findings (`env-amx`, `env-lock`, `env-turn-budget`) are informational.
