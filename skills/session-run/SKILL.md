---
name: session-run
description: Execute the next pending session from a split plan directory. Auto-advances through sessions one at a time. Companion to session-split and session-expand.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

# Session Run

## Purpose

Execute the next pending session from a split plan directory. Picks the first session with status `pending`, implements it, and updates the handoff. One session per invocation — the user controls pacing by invoking this skill once per conversation.

## Input

The user provides a plan directory path as the host agent command or skill invocation argument. Example:

```
session-run /tmp/agent-plans/tranquil-singing-lantern-abc123def456/
```

## Instructions

### Step 1: Parse Input

The plan directory is the provided path. Trim any trailing whitespace. Verify the directory exists and contains `manifest.md` and `context.md`.

### Step 2: Read Context

Read `context.md` from the plan directory. Hold this as shared context for the session implementation.

### Step 3: Find Next Pending Session

Run a single grep to extract all status lines (important, use the full path to grep to avoid issues):

```bash
/usr/bin/grep -h "^\- \*\*Status\*\*:" <plan-dir>/session-*.md
```

Parse the output to build a list of `(session-number, status)` pairs. Find the first session with status `pending`.

- If **no sessions are pending** (all are `complete`): print `Next command: All sessions implemented - nothing left to run.` and stop.
- If **no session files exist**: print an error and stop.

### Step 4: Load Session

Read `session-N.md` for the selected session.

Also check for `session-N.expand.md`. If it exists, read it — this contains verified file paths, corrected signatures, and codebase patterns discovered by `session-expand`. Treat expansion notes as authoritative corrections to the session plan.

If this is not Session 1, also read the **Handoff** section from `session-<N-1>.md` for context from the previous session.

### Step 5: Implement

Execute the session's instructions — everything above the `## Handoff` section in the session file. Use the context from `context.md`, expansion notes (if any), and previous session handoff (if any) to inform the implementation.

Follow all instructions in the session file faithfully. Make real code changes, run tests if specified, and handle edge cases described in the session.

### Step 6: Update Handoff

Edit the Handoff section in `session-N.md` with actual results:

- **Status**: complete
- **Files modified**: list each file with a brief description of what changed
- **Key decisions**: any notable choices made during implementation
- **Blockers for next session**: issues the next session should be aware of
- **Notes**: anything else relevant

Use the Edit tool to update the handoff in place — do not rewrite the entire file.

### Step 6b: Inject Notes for Future Sessions

If during implementation you produced guidance, corrections, or discoveries that a **future** session needs (e.g., "Notes for S3", "Heads-up for S4", "S5 should use the new helper at X"), do **not** leave them inline in your chat response. They must be written into the target session file so the future `session-run` invocation actually sees them.

For each note targeted at session M (where M > N):

- If `session-M.expand.md` exists, append the note to it under a clear heading like `## Notes from session-N`. Expansion files are the right home for verified facts, corrected paths, and codebase discoveries.
- If `session-M.expand.md` does not exist, append the note to `session-M.md` under a `## Notes from session-N` section placed **above** the `## Handoff` section (so it is part of the instructions that get executed, not the handoff results).

Use the Edit tool to make these injections. Never just print "Notes for S3: ..." in the report — that information will be lost the moment the conversation ends.

The current session's own handoff (Step 6) is for *results of this session*. Step 6b is for *instructions/context for later sessions*. Keep these separate.

### Step 7: Report

Print a brief summary:

1. Which session was completed (number and title)
2. Files modified (short list)
3. What session comes next, or "All sessions implemented" if this was the last one
4. Any blockers or notes for the next session — and, for each, name the file you injected it into (e.g., "Injected note for S3 into session-3.expand.md"). Do not put the note content itself in the report; the report only confirms where it was written.

End every successful report with exactly one `Next command:` line:

- If another pending session remains, print `Next command: session-run <plan-dir>/`
- If no pending sessions remain, print `Next command: All sessions implemented - nothing left to run.`

Before printing this line, re-check the session statuses after the current handoff update so the next action reflects the actual updated plan directory state.

Use the original plan directory argument provided by the user for `<plan-dir>`, trimmed of whitespace and displayed with a trailing slash for directory readability.

## Important Notes

- One session per invocation — never implement more than one session
- Always read `context.md` before starting implementation
- Expansion notes (`session-N.expand.md`) take precedence over the session file for factual claims (file paths, line numbers, function signatures)
- The session file is the source of truth for what to implement; expansion notes only correct factual details
- Never modify the original plan file or `context.md`
- If a session's status is already `complete`, skip it and move to the next pending one
