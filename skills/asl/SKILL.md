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

The helper owns command parsing, safety rejection, run start/attach/poll, pause
detection and the recoverable-pause retry budget (see payload `pause_policy`),
reply resume, and final-review verdict parsing. The runner itself owns automatic
repair/rereview cycles: after a findings review it materializes same-plan repair
sessions, executes them, and rereviews, bounded by
`asl run --max-review-fix-cycles` (default 2). This skill is supervision and Q&A
only — run the helper; when a paused run needs an answer, infer a defensible
reply or ask the user, then resume; and when it reports remaining review
findings, surface them to the user. It never drafts, splits, or expands
follow-up fix plans.

## Input

The user provides the exact `asl run` command as the skill argument, for example:

```text
$asl asl run ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/
/asl asl run ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/
```

The command may include leading environment assignments (such as
`CLAUDE_CONFIG_DIR=~/.claude-personal`), quoted arguments, and normal `asl run`
flags — including `--max-review-fix-cycles N` (deprecated alias
`--max-review-fix-plans N`), which bounds the runner's own repair/rereview
cycles. Shell operators, pipes, redirects, `--dry-run`, and user-supplied
`--reply` / `--reply-stdin` are not accepted — the helper owns resume behavior.

## Instructions

### Step 1: Run the bundled helper

The helper ships beside this skill at `<skill-dir>/scripts/asl-autopilot.py`,
where `<skill-dir>` is the directory containing this `SKILL.md`. Harnesses expose
the shared skill through a symlink, so resolve the real path before invoking.
Run it with the user's exact command:

```text
<skill-dir>/scripts/asl-autopilot.py --command '<exact asl run command>' --json
```

`--json` emits a single machine-readable result on stdout; progress heartbeats
go to stderr. Each heartbeat carries live progress — the running session label,
`(n/total)`, and `idle <n>s` since the child last emitted output — and escalates
to a `WARN possibly stalled` line once idle time crosses the stall threshold. To
override the runner's cycle cap at launch, include `--max-review-fix-cycles N`
inside the `--command` string (`0` still reviews but creates no repair sessions).

### Step 2: Handle pauses

Retry mechanics are helper-owned — see the result payload's `pause_policy`.
The helper auto-retries recoverable same-session pauses (`verify-failed`, `transport-failed`, `timeout`, `no-status-change`) up to twice per session and reason. Only `transport-failed` backs off before relaunching (5 seconds, then 30 seconds); the other recoverable pauses retry immediately. An unclassified `nonzero-exit` pause is never retried. When a pause genuinely needs an answer, the result carries `reason: needs_agent_reply` with a `questions` block holding the resolver prompt, the paused agent's last message, the pause metadata, and artifact paths.

Decide the reply with discipline:

- **Infer the answer yourself only when it is clearly defensible** from the
  plan, the repository, and the run artifacts referenced in the `questions`
  block. Read those artifacts before deciding.
- **Otherwise, relay the question to the user and ask** — do not guess when the
  answer depends on user intent, a trade-off, or information not present in the
  plan/repo/artifacts.

Resume by streaming the chosen reply on stdin. The helper launches
`asl run --reply-stdin`; the reply never appears in argv:

```text
printf '%s' '<reply text>' | <skill-dir>/scripts/asl-autopilot.py --command '<same exact command>' --json --reply-stdin
```

If the helper reports `reason: user_required` (a foreign active lock) or a
`blocked` / `status-error` stop, surface that to the user rather than resuming. A
paused run may also cite a `command-not-runnable` readiness finding (an
unresolved `argv[0]`, or a non-importable `python -m <module>`); surface it
rather than resuming.

### Step 3: Handle review findings

When the supervised command used `--review-harness`, a clean completion still
carries the review verdict. An all-clear review returns `reason: complete` with a
`review` block whose `outcome` is `all-clear`; nothing more is needed. When the
review reports findings, the helper returns `reason: review_findings` with a
`review` block holding `review_output_path` (the `final-review.md` to read in
full), `status_line`, `total_findings`, `findings_by_severity`, and a bounded
`summary`.

`review_findings` means no automatic continuation remains for the command's
current cap and artifact state. Do **not** draft, split, or expand a
follow-up fix plan. Instead:

1. Read the full `final-review.md` at `review_output_path`, then run
   `asl review-verdict <plan-dir> --json` for the authoritative verdict and relay
   its `next_steps` remediation commands.
2. Hand the remaining findings to the user with the status line and the run's
   recorded cycle disposition (`review_fix_disposition`). When the payload's
   `review_resume` block reports `higher-cap-required` or `artifact-invalid`,
   include its minimum cap or artifact-remediation detail so the user can resume
   the same plan.

### Step 4: Report

Summarize the outcome: which run was supervised, whether it completed or paused,
and any blocker that needs the user's attention.

## Notes

- One `asl run` command per invocation.
- The helper — not the skill prose — owns command parsing, safety rejection,
  run start/attach, polling, the retry budget (`pause_policy`), reply handling,
  and final-review verdict parsing. Keep the skill workflow thin.
- Automatic repair/rereview cycles are runner-owned, bounded by
  `asl run --max-review-fix-cycles` (default 2; `--max-review-fix-plans` is a
  deprecated alias). Corrected artifacts and higher caps resume the same plan;
  this skill never creates follow-up fix plans.
- Keep the Q&A discipline: infer only clearly defensible replies from the
  plan/repo/artifacts; otherwise relay the question and ask the user.
