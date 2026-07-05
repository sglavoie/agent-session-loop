---
name: asl
description: Autonomously supervise an `asl run` invocation end to end. Give it the exact `asl run ...` command and it starts or attaches to the run, polls structured status, surfaces pauses that need a human answer, and drives the run to completion. Companion to session-split, session-expand, and session-run.
allowed-tools: Bash, Read
---

# ASL Autopilot

## Purpose

Supervise a single `asl run` invocation from start to finish. The user hands
this skill the exact `asl run ...` command they would type; the skill runs the
bundled helper, which starts or attaches to the run, polls `asl status --json`,
streams concise progress, and stops for a human answer only when the run pauses
on an interactive decision it cannot resolve.

The helper owns command parsing, run start/attach/poll, pause detection, the
recoverable-pause retry budget, reply resume, and final-review verdict parsing.
The skill workflow stays thin: run the helper; when it reports that a paused run
needs an answer, infer a defensible reply or ask the user, then resume; and when
it reports review findings, open a bounded follow-up fix cycle.

## Input

The user provides the exact `asl run` command as the skill argument, for example:

```text
$asl asl run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/
/asl asl run ~/.local/share/agent-plans/tranquil-singing-lantern-abc123def456/
```

The command may include leading environment assignments (such as
`CLAUDE_CONFIG_DIR=~/.claude-personal`), quoted arguments, and normal `asl run`
flags. Shell operators, pipes, redirects, `--dry-run`, and user-supplied
`--reply` / `--reply-stdin` are not accepted — the helper owns resume behavior.

## Instructions

### Step 1: Locate the bundled helper

The helper ships beside this skill at `scripts/asl-autopilot.py`. Resolve it
relative to this `SKILL.md` so the correct copy runs regardless of which harness
package linked the skill:

```text
<skill-dir>/scripts/asl-autopilot.py
```

`<skill-dir>` is the directory containing this `SKILL.md`. Because harnesses
expose the shared skill through a symlink, resolve the real path before
invoking the helper.

### Step 2: Run the helper

Invoke the helper with the user's exact command:

```text
<skill-dir>/scripts/asl-autopilot.py --command '<exact asl run command>' --json
```

Pass `--json` so the helper emits a single machine-readable result on stdout;
progress heartbeats go to stderr. Read the JSON result to decide what to report.

Each stderr heartbeat carries live progress — the running session label,
`(n/total)`, and `idle <n>s` since the child last emitted output — and escalates
to a `WARN possibly stalled` line once idle time crosses the stall threshold, so
you can tell a healthy-but-slow run from a genuinely stuck one and act on it.

### Step 3: Handle pauses

The helper auto-retries recoverable same-session pauses (`verify-failed`,
`nonzero-exit`, `timeout`, `no-status-change`) up to twice per session and
reason, so it only escalates when a pause genuinely needs an answer. When it
does, the result carries `reason: needs_agent_reply` with a `questions` block
holding the resolver prompt, the paused agent's last message, the pause
metadata, and artifact paths.

Decide the reply with discipline:

- **Infer the answer yourself only when it is clearly defensible** from the
  plan, the repository, and the run artifacts referenced in the `questions`
  block. Read those artifacts before deciding.
- **Otherwise, relay the question to the user and ask** — do not guess when the
  answer depends on user intent, a trade-off, or information not present in the
  plan/repo/artifacts.

Resume by streaming the chosen reply (inferred or user-supplied) on stdin. The
helper launches `asl run --reply-stdin`; the reply never appears in argv:

```text
printf '%s' '<reply text>' | <skill-dir>/scripts/asl-autopilot.py --command '<same exact command>' --json --reply-stdin
```

If the helper reports `reason: user_required` (a foreign active lock) or a
`blocked`/`status-error` stop, surface that to the user rather than resuming.

### Step 4: Handle review findings

When the supervised command used `--review-harness`, a clean completion still
carries the review verdict. An all-clear review returns `reason: complete` with
a `review` block whose `outcome` is `all-clear`; nothing more is needed. When the
review reports findings, the helper returns `reason: review_findings` with a
`review` block holding:

- `review_output_path` — the `final-review.md` file to read in full.
- `status_line` — e.g. `Status: 3 Findings (1 high, 1 med, 1 low)`.
- `total_findings` and `findings_by_severity`.
- `summary` — a bounded excerpt of the review body.

On `review_findings`, open a bounded follow-up fix cycle:

1. Read the full `final-review.md` at `review_output_path`.
2. Draft a follow-up fix plan that addresses the findings, then split and expand
   it with the existing skills:

   ```text
   /session-split <fix-plan-file>
   /session-expand <new-plan-dir>/
   ```

3. Supervise the new plan by rerunning this helper against the generated plan
   directory (a fresh `asl run <new-plan-dir>/ --review-harness ...` command).
4. **Limit automatic review-fix loops to 2 generated follow-up plans.** If a
   second follow-up plan still comes back with `review_findings`, stop
   auto-generating plans and hand the remaining findings to the user for a
   decision instead of looping again.

### Step 5: Report

Summarize the outcome: which run was supervised, whether it completed or paused,
and any blocker that needs the user's attention.

## Notes

- One `asl run` command per invocation.
- The helper — not the skill prose — owns command parsing, safety rejection,
  run start/attach, polling, reply handling, and final-review verdict parsing.
  Keep the skill workflow thin.
- Automatic review-fix cycles are bounded to 2 generated follow-up plans; after
  that, unresolved findings go to the user rather than another auto-loop.
- The preflight/`asl run` readiness gate now rejects a plan whose completion-contract
  `command` checks are not runnable (an unresolved `argv[0]`, or a non-importable
  `python -m <module>`), so a paused run may cite a `command-not-runnable` finding —
  surface it to the user rather than resuming.
