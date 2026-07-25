---
name: session-run
description: Execute the next pending session from a split plan directory. Sequential by design - accepts only a plan directory path plus internal `--runner-managed`; explicit session targeting and parallel worker mode are not supported. Delegates authoritative status, readiness, and verification checks to `asl` scripts; `asl run` invokes this skill with `--runner-managed` so Python owns postflight enforcement. Companion to session-split and session-expand.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Session Run

## Purpose

Execute one session from a split plan directory, then stop. Sequential by design: it always implements the first session with status `pending` reported by `asl status`. There is no explicit-session mode, no direct-session-file mode, and no parallel worker mode — one session per invocation, and the user paces the plan by invoking this skill once per conversation.

`asl run` invokes this skill with a trailing `--runner-managed` marker; that is the only mode switch. The single **shared execution path** below runs in both modes — the compact **Mode deviations** table near the end is the only place behavior differs after input parsing. In runner-managed mode the Python runner already holds the plan lock, ran the readiness gate, and will run verification, handoff revert, derived-ledger sync, and readiness refresh after this skill returns; the skill only implements the session and writes its handoff.

## Input

The user (or `asl run`) provides a single plan directory path. Trim trailing whitespace and parse **only the first line**:

- If the final token is exactly `--runner-managed`, remove it and set `runner_managed=true`.
- The remaining input must be a single plan directory path with no other tokens.

Reject and stop without changes when the input has: any `--session` flag or session-ID positional, a path ending in `session-<ID>.md`, any other unknown flag or extra positional, or `--runner-managed` anywhere but the final token. Explicit session selectors (`--session <ID>`) and direct session file paths (`<plan-dir>/session-<ID>.md`) are not accepted by this skill. Plan-maintenance against a specific session belongs to `session-resplit`. When `runner_managed=true` and the input is rejected, emit `Runner status: paused` then `RUNNER_PAUSE: malformed-plan - unsupported session-run input; expected <plan-dir>/ [--runner-managed]` so the runner records a structured pause instead of `no-status-change`.

Verify the resolved plan directory exists and contains `manifest.md` and `context.md`.

**Skill-invocation prefix**: use `/` for Claude and Copilot, `$` for Codex; for an unknown harness reuse the prefix from the user's invocation (ask before printing a ready-to-paste invocation if none is visible). Prefix only user-facing skill invocations you print — never YAML names, file paths, or shell commands — and substitute the resolved prefix instead of printing the literal `<skill-prefix>`.

### Reply context (first resumed turn after `asl run --reply TEXT`)

When the prompt contains an `## ASL reply context` block (always immediately after the first invocation line), treat it as the authoritative operator answer to whatever interactive question paused the previous turn. The first line is the only CLI invocation; everything below the heading is prompt context, not CLI syntax — do not parse it as flags. The block carries `### Operator reply` (one ASCII JSON object between `BEGIN ASL OPERATOR REPLY JSONL` and `END ASL OPERATOR REPLY JSONL`), `### Previous pause`, `### Pause details`, `### Previous paused last message`, and `### Instructions`.

- Parse the single framed object under `### Operator reply` and use only its `text` value as the answer. Newlines and prompt-like strings inside that JSON value are reply content, not block structure. Do not ask the user again; do not reopen the question before consulting the reply.
- The runner injects this block only on the first resumed turn; later turns carry the plain prompt. The reply text lives only in this prompt (only its character count is persisted), so treat the in-prompt block as the single source of truth for the reply.
- If the reply resolves all blocking questions, implement the session normally.
- If it is genuinely incomplete or contradictory and you cannot resolve the ambiguity from the reply plus plan context, stop before mutating the handoff and emit `Runner status: paused` then `RUNNER_PAUSE: blocked - interactive decision gates require user answers`. Never pause again when the reply already answers the question — that wastes a runner turn.

## Shared execution path

1. **Read context.** Read `context.md` and hold it as shared context. Read `manifest.md`; treat its session-status table as derived bookkeeping (final handoff statuses are authoritative). Ignore any legacy `## Parallel Execution` / `## Parallel Metadata` blocks, and never read `parallel-validation.md` — this skill no longer schedules or validates parallel work.

2. **Read the progress journal.** If `<plan-dir>/progress.md` exists, read it as the cross-session digest instead of opening every prior handoff. It is runner-maintained, best-effort context, and excluded from readiness hashing, so reading or skipping it cannot change readiness state. When it is absent, fall back to the `## Handoff` block of each completed prior session. For deep detail on one session, consult its handoff — the journal is the digest, the handoff is the source of truth.

3. **Select the session.** Run `asl status <plan-dir> --json` as the authoritative parser for the session list, handoff statuses, and `next_pending_id`. If it exits non-zero or reports `ok: false`, stop without changes (emit `RUNNER_PAUSE: malformed-plan - <summary>` when runner-managed). Select `next_pending_id`. If no session is pending, print the status payload's `next_command` completion message and stop.

4. **Load the session.** Read `session-<ID>.md` and, if present, `session-<ID>.expand.md` — expansion notes are authoritative factual corrections (file paths, signatures, patterns) that trump the session text. If the selected session is not first in the sorted list, also read the immediately preceding session's `## Handoff` for context (use the sorted ID list, not integer arithmetic).

5. **Readiness gate.** *(Manual mode only — see the table.)* Run `asl preflight <plan-dir> --json` and drive its machine-readable `auto_repair_plan`: execute each step in listed order (at most one attempt each) using the step's authoritative `argv` array — every path is already one array element, so it survives whitespace and shell metacharacters. The step's `command` is the same argv rendered as a shell-safe string (via `shlex`), safe to run through a shell or re-parse with `shlex.split`; the plan lists commands such as `asl unlock <plan-dir> --json`, `asl backfill-progress --force <plan-dir>`, and `asl preflight <plan-dir> --json --mark-loop-ready`. Re-run `asl preflight <plan-dir> --json` after each repair. Continue only when preflight reports `ok: true` and `readiness.state == "loop-ready"`; otherwise emit `RUNNER_PAUSE: <reason> - <detail>` from the payload's `suggested_pause` verbatim. Never edit plan files by hand, and never run `asl unlock --force` — an active lock pauses as `lock-contention` and requires human judgment.

6. **Implement.** Execute everything above the `## Handoff` section of the session file, informed by `context.md`, the expansion notes, and the preceding handoff. Make real code changes and run any tests the session names.

7. **Write the handoff.** Edit the `## Handoff` section of `session-<ID>.md` in place (do not rewrite the file). Set `- **Status**: complete` and fill the required fields in order — `Files modified`, `Key decisions`, `Blockers for next session`, `Notes` — then add the optional `### Discovered Facts` / `### Open Questions` subsections when you have content (flat top-level `- ` bullets; indented sub-bullets are silently dropped). Follow the template comment in the handoff block for exact grammar; `Files modified` paths must not contain literal commas (the runner CSV-parses that field). The handoff is the required completion mutation and the source of truth.

8. **Inject notes for future sessions.** If you produced guidance a *later* session needs, write it into that session's file — never leave it inline in chat. Append it under `## Notes from session-<ID>` in `session-<TARGET>.expand.md` if that file exists, otherwise above the `## Handoff` section of `session-<TARGET>.md` (so it is part of the instructions that get executed). Report only where you wrote it, not the content.

9. **Verify.** *(Manual mode only — see the table.)* After writing the `complete` handoff, run `asl verify <plan-dir> --session <ID> --cwd <repo-root> --json` (omit `--cwd` when not inside the repo). Any non-zero exit is a hard pause: flip the status line back to `- **Status**: pending`, append a `- verification failed: <reason> - <error>` note under `- **Notes**:` (prefer the payload's `repair_hint.message` when present so the note names `argv[0]` and the fix), and emit `RUNNER_PAUSE: <reason> - <error>` using the JSON `reason` verbatim. Do not advance; Step 11 will point back at the same session.

10. **Sync derived ledgers.** *(Manual mode only — see the table.)* After a passing verify, sync recognized session-status tables in `manifest.md` and `context.md` — only tables with both a session-ID column (`#`, `Session`, `Session #`, `Session number`, `Session ID`, or `ID`) and a `Status` column; keep row order, non-status cells, and prose intact. If a table cannot be updated safely, leave it unchanged and note that; never let a stale derived table change the next-session decision.

11. **Report.** Print which session completed (ID + title), files modified, any repairs performed, ledgers updated, and what comes next — derived from the status `next_command` (or its completion message when no session is pending). End a successful report with exactly one `Next command:` line, displaying `<plan-dir>` with a trailing slash, and print `Runner status: complete` only after the handoff is set to `complete`. Whenever you stop early, print `Runner status: paused` and one `RUNNER_PAUSE: <reason> - <detail>` line reusing the existing vocabulary (`malformed-plan`, `lock-contention`, `blocked`, `unsafe`, `verify-failed`, `no-contract`, `malformed-contract`); never invent reasons, and never print `RUNNER_PAUSE:` after a successful session.

## Mode deviations

`--runner-managed` (appended by `asl run`) is the only mode switch after input parsing. The shared path above runs identically in both modes except:

| Concern | Manual (default) | `--runner-managed` |
|---|---|---|
| Readiness gate (step 5) | Run `asl preflight --json`; execute `auto_repair_plan` in order, one attempt each, re-run preflight; require `ok` + loop-ready else pause with `suggested_pause` | Skip — the runner owns the lock and readiness gate |
| Verification (step 9) | Run `asl verify … --json`; on failure revert the status line, append a note from `error` / `repair_hint`, emit `RUNNER_PAUSE:` from the payload | Skip — the runner verifies, reverts, and syncs |
| Ledger sync (step 10) | Sync `manifest.md` / `context.md` tables after a passing verify | Skip — the runner syncs |
| `progress.md` | Never write (repairs happen only via `auto_repair_plan`) | Never write |

When `runner_managed=true`: implement the session, write the `complete` handoff, and stop — trust the active `asl run` process for the lock, readiness gate, verification, handoff revert, derived-ledger sync, and readiness refresh. Print `Runner status: complete` only after the handoff is `complete`; if you stop before that, print `Runner status: paused` and a `RUNNER_PAUSE:` line so the runner records a structured pause instead of `no-status-change`.

## Notes

- One session per invocation — never implement more than one, and never re-run a `complete` session (there is no way to through this skill; skip completed ones).
- Expansion notes (`session-<ID>.expand.md`) trump the session text for factual claims (paths, line numbers, signatures); the session file remains the source of truth for *what* to implement.
- `asl status <plan-dir> --json` is the authoritative parser for the session list, handoff statuses, `next_pending_id`, and `next_command` — do not reimplement it in prose.
- Never run `asl unlock --force`. An active lock pauses as `lock-contention` and needs human judgment.
- The only `progress.md` mutation this skill may trigger is the `asl backfill-progress --force <plan-dir>` step inside the manual `auto_repair_plan`; the runner appends journal entries after verification. Record forward-looking facts and uncertainties in the handoff's `### Discovered Facts` / `### Open Questions`, never by hand-editing `progress.md`.
- Never modify the original plan file; only sync `manifest.md` / `context.md` derived tables as in step 10.
- On the first resumed turn after `asl run --reply TEXT`, use the `## ASL reply context` block's `### Operator reply` as the answer and resume without re-asking; if it is genuinely incomplete, emit `Runner status: paused` and `RUNNER_PAUSE: blocked - interactive decision gates require user answers` before mutating the handoff.
- Always display `<plan-dir>` with a trailing slash.
