---
name: session-resplit
description: Split one pending session in an existing split plan into nested serial child sessions while preserving plan metadata, locks, archives, and rerun guidance.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Session Resplit

## Purpose

Replace one pending session in an existing split plan with nested child sessions such as `session-8.1.md`, `session-8.2.md`, and `session-8.3.md`. Use this when one pending session is too large or needs clearer boundaries, but the rest of the plan should remain intact.

Sequential by design: generated children are serial work units with parseable completion contracts. This skill neither reads nor emits parallel scheduling metadata; legacy `parallel-validation.md` files and any `## Parallel Metadata` blocks left on disk are ignored.

Companion to `session-split` (creates the plan), `session-expand` (verifies facts, writes `session-<ID>.expand.md`), and `session-run` (executes one pending session at a time). This skill rewrites one pending parent into nested pending children, then tells the user to rerun `session-expand`.

Unlike `session-run` (serial-only, directory auto-advance), this skill is plan maintenance, so it keeps targeted parent selection (`--session <ID>`, direct `session-<ID>.md` paths) — those let an operator pick which pending parent to rewrite without changing the serial execution contract.

## Input

**Skill-invocation prefix**: use `/` for Claude and Copilot, `$` for Codex; for an unknown harness reuse the prefix from the user's invocation (ask before printing a ready-to-paste invocation if none is visible). Prefix only user-facing skill invocations you print or write into generated files — never YAML names, file paths, or shell commands — and substitute the resolved prefix instead of printing the literal `<skill-prefix>`. Pass it to the helper as `--skill-prefix {$|/}`.

Examples:

```
$session-resplit
$session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ --session 8 --parts 3
/session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/session-8.md
/session-resplit ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/ 4
```

Accepted input forms (the helper's `target`/`count`/`--session`/`--parts` arguments cover all of them):

- **No argument**: infer the plan directory, split the first pending session in dotted numeric order, infer the child count.
- **Integer child count only**: infer the plan directory and first pending session; create exactly that many children.
- **Plan directory** (optionally plus a child count): split the first pending session there.
- **Direct session file** (optionally plus a child count): split exactly that pending `session-<ID>.md`.
- **`<plan-dir>/ --session <ID>`** (optionally `--parts <N>` or a trailing `<N>`): split exactly `session-<ID>.md`.

Session IDs must match `[1-9][0-9]*(\.[1-9][0-9]*)*`. Child counts must be integers from 2 through 20.

Reject malformed inputs and stop without modifying files: unknown flags or extra positionals; more than one plan directory or session file; both a direct session file and `--session`; `--session`/`--parts` without a valid value; multiple child counts; a count below 2 or above 20.

## Inference Rules

Resolve missing values conservatively:

1. **Plan directory**: a direct session file → its parent directory; a plan directory → itself; no path → walk upward from the cwd until a directory holds `context.md`, `manifest.md`, and at least one exact `session-<ID>.md`, or use the single readable plan directory visible in the conversation. If none is unique, ask the user and stop. The helper's `--json` reason `ambiguous-input` also fires when nothing can be inferred.
2. **Target session**: a direct session file or `--session <ID>` selects the exact parent; otherwise the first pending session in dotted numeric order (final handoff statuses are authoritative — read them via `asl status <plan-dir> --json`, whose `sessions[]` array reports each `id`/`status` and whose `next_pending_id` names the default parent).
3. **Child count**: an integer argument or `--parts <N>` is authoritative. Without one, choose 2 through 5 from the parent instructions — 2 for a short session, 3 for medium multi-step work, 4 or 5 only when the parent clearly contains separable subsystems or phases. If no meaningful split is possible, stop with `RESPLIT_PAUSE: cannot-split - parent instructions are too small or indivisible`.

Never infer by reading or modifying the original source plan outside the split plan directory. The split plan directory is the unit of work.

## Instructions

### Step 1: Delegate the mechanics to the helper

All resplit mutations go through `skills/session-resplit/scripts/resplit-session.py` (also reachable through the installed skill directory), which delegates to `agent_session_loop.authoring` — the same rendering, table-mutation, transaction, and readiness API `session-split` uses. It owns input parsing, target/count inference, plan inspection, target validation, lock acquisition, parent archival, atomic child generation, derived-ledger updates, and readiness refresh. If the helper is missing, stop with a clear error; never hand-write child files, hand-edit manifest tables, or reimplement its schemas.

Your job is the semantic work the helper cannot do: choosing good child boundaries, writing child instructions, and allocating the parent's completion contract across children (Step 2). Supply that through `--child-spec`; everything mechanical belongs to Python.

Run the helper once resolved:

```bash
python3 skills/session-resplit/scripts/resplit-session.py <plan-dir-or-session-file> \
  [--session <ID>] [--parts <N>] [--child-spec <file>] --skill-prefix {$|/} --json
```

Read the `--json` payload as the source of truth: on success `{ok:true, parent_id, children, child_files, archive_dir, archived_files, ledger_updates, reference_updates, readiness, next_command, next_steps}`; on a recoverable refusal `{ok:false, reason, detail, remediation}`. Surface `remediation` and stop — do not retry blindly.

### Step 2: Plan the child sessions and allocate the contract

Read the selected parent session's content above its final `## Handoff`. The helper preserves the title/intent, `> **Before starting**` context requirement, expansion-note and previous-session references, and all implementation instructions; it strips any `## Parallel Metadata` block and renders each child deterministically (`## Lineage`, `## Parent Scope`, `## Child Scope`, `## Completion Contract`, `## Handoff`) via `authoring.render_session_file`. Do not author child markdown by hand — supply semantics through `--child-spec`.

Create children that are useful execution units, not arbitrary slices. Prefer boundaries based on files, subsystems, phases, tests, or explicit deliverables; when a compact parent paragraph lists deliverables after a colon or in bullets, split on those before falling back to generic phases.

**Child spec schema** (`--child-spec <file>` takes a JSON list, or an object with a `children` list; one entry per child, in child-ID order):

```json
{
  "title": "<child title>",
  "instructions": "<child-scope instructions markdown>",
  "done_when": ["<explicit exit criterion>", "..."],
  "inherit_checks": ["<parent check name>", "..."],
  "checks": [{"name": "...", "type": "command|file_exists|regex_in_file", "...": "..."}]
}
```

Contract allocation rules (enforced by the helper; violations fail closed as `ambiguous-contract`/`ambiguous-input` before any file is touched):

- Every child needs explicit `done_when` criteria and at least one verification check via `inherit_checks` (parent check names) and/or `checks` (explicit replacement objects, same JSON shape as contract checks).
- If any child inherits parent checks, **every** parent check must be inherited by at least one child; leaving one unallocated is `ambiguous-contract`. If no child inherits, every child must carry its own `checks` (the explicit replacement of the parent recipe).
- `inherit_checks` naming an undefined parent check, legacy `{title, instructions}`-only specs, and empty allocations are rejected (`ambiguous-contract`); unknown spec keys are rejected (`ambiguous-input`).

Without `--child-spec`, the helper derives every child contract from the parent's own `## Completion Contract`: each child inherits the full verify recipe and the last child also inherits the parent's `Done when`. A parent with no parseable contract is rejected as `no-contract` (the helper never inserts a `python3 -m unittest` placeholder). Prefer explicit specs whenever the parent's checks split meaningfully.

Child IDs are `<PARENT_ID>.1` through `<PARENT_ID>.<N>`; the first child inherits the parent's previous-session reference (or the session immediately preceding it) and each later child points at the prior child — the helper handles this. If the parent had expansion notes, do not copy stale line numbers into child instructions as facts; the helper's lineage section marks the archived parent expansion as historical context pending a `session-expand` rerun.

### Step 3: Lock

The helper acquires the same per-plan `agent-session-loop` lock before mutating files, self-clears stale locks by the normal rules, and releases the lock after all edits or any failure. Because stale locks are already self-cleared, `reason: lock-contention` (JSON) / `RESPLIT_PAUSE: lock-contention` (text) always means an *active* lock held by a live runner: wait for that runner to finish, or investigate and stop it, then re-run — never force-remove an active lock or run a manual unlock.

### Step 4: Report

Echo the helper's `--json` payload as a concise summary: parent resplit, children created, archive directory (`resplit-archive/session-<PARENT_ID>-<UTC_TIMESTAMP>/`), files archived, derived ledgers updated (`manifest.md` sessions count + status rows + `## Resplit Notes`; clear `context.md` status tables), and downstream references repaired. Note that the parent files are archived (never left at the plan root) and readiness was reset to `draft` with a recomputed SHA, so any parent expansion is now stale.

The helper prints its own runner signals — `RESPLIT_STATUS: complete` then a single `Next command:` line on success, or `RESPLIT_STATUS: paused` / `RESPLIT_PAUSE: <reason> - <detail>` / `RESPLIT_REMEDIATION: <fix>` on a recoverable stop. Stable reasons: `malformed-plan`, `unknown-status`, `lock-contention`, `blocked`, `collision`, `cannot-split`, `ambiguous-input`, `no-contract`, `ambiguous-contract`. Relay them verbatim.

Because resplitting invalidates the parent expansion and resets readiness to `draft`, the next command is normally `<skill-prefix>session-expand <plan-dir>/` (the helper's `next_command`/`next_steps` already render it) so child expansion notes are written, derived contracts are refined against the codebase, and readiness can be re-marked loop-ready before `session-run`.

## Important Notes

- Resplit exactly one pending parent per invocation; never mark a parent `complete` to sideline it (archive it), never overwrite existing child sessions or expansion files, and never leave both the parent and its children at the plan root.
- Never modify the original source plan referenced by `manifest.md`; treat final handoff statuses as authoritative over derived manifest tables.
- Preserve dotted session IDs everywhere: filenames, headings, dependencies, lineage, examples, and reports.
- Never author child files, manifest tables, or contracts by hand — the helper and `agent_session_loop.authoring` own those schemas. This skill supplies boundaries, instructions, and contract allocation only.
- Child contracts are never placeholders: derived from the parent contract or supplied per child. Ambiguous allocation fails closed (`no-contract` / `ambiguous-contract`) instead of inserting a `python3 -m unittest` default.
- Do not read or rewrite `parallel-validation.md` or touch legacy dependency-wave/ownership tables — both this skill and `session-run` ignore them, and their rows stay intact even when they reference the resplit parent.
- After resplitting, rerun `session-expand` before `session-run`.
