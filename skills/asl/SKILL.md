---
name: asl
description: Autonomously supervise an `asl run` invocation end to end. Give it the exact `asl run ...` command and it starts or attaches to the run, polls structured status, surfaces pauses that need a human answer, and drives the run to completion. Companion to session-split, session-expand, and session-run.
allowed-tools: Bash, Read
---

# ASL Autopilot

## Quick Reference

- Supervise: `asl outbox supervise PLAN_DIR --supervision-id SUPERVISION_ID`
- Answer: `asl inbox reply PLAN_DIR --to QUESTION_ID --text REPLY_TEXT --wait-consumed --wait-timeout 300 --json`
- Steer: `asl inbox guidance PLAN_DIR --text GUIDANCE_TEXT --wait-delivered --json`
- Recover: `asl status PLAN_DIR --json`
- Every supervision command in one place: `asl supervisor-guide [--json]`

Read `references/supervisor-context.md` in this skill directory before
improvising a supervision command. It is the generated copy of the startup card
the harness already loaded, plus the full guide text; `asl supervisor-guide` is
authoritative whenever the two disagree; the workflow below still governs the
launch and resume.

## On-demand references

The supervise loop needs nothing else; load a reference only when its case arises:

| Read | When |
| --- | --- |
| `references/questions-and-pauses.md` | Before closing a live question, and on any pause: closure commands, the accepted/consumed distinction, live-wait timing, the helper's retry policy, and the registered-workspace and commit-group failures |
| `references/mid-run-controls.md` | Before sending guidance or a model change, adding a guard, stopping or aborting a run, or redirecting a turn the user authorized; and on a `conversation-recovery-required` or `redirect-successor-blocked` pause |
| `references/quota-and-hangs.md` | A `quota-exhausted` stop worth sleeping through (`--wait-quota-reset`), or `reason: worker_hung` |
| `references/review-findings.md` | `reason: review_findings`, or a stalled or exhausted review loop to report |
| `references/supervisor-audit.md` | The result carries an `assurance` block with findings to report |
| `references/supervise-tokens.md` | Any token other than `IDLE`, a `LOST` to diagnose, or the stdout-tail fallback when the mailbox is unavailable |
| `references/helper-internals.md` | `--spark`, a registered multi-repository `--cwd`, `asl outbox supervise` timeouts, progress heartbeats, or the helper's watcher and lease |

## Purpose

Supervise a single `asl run` invocation from start to finish: the bundled
helper starts or attaches to the user's exact command, polls `asl status --json`,
and stops for a human answer only on a decision it cannot resolve. Unless the
command carries `--no-mailbox`, it also holds a supervisor lease and surfaces
live worker questions and notices.

The helper owns command parsing, safety rejection, run start/attach/poll, pause
detection and the retry budget (payload `pause_policy`), reply resume, and
final-review verdict parsing. The runner owns repair/rereview cycles, bounded by
`asl run --max-review-fix-cycles` (default 4), `--max-repair-group-attempts` (default 2)
and `--max-review-fix-cycles-ceiling` (default 8). This skill is supervision and
Q&A only: run the helper, answer or relay what a paused run asks, and surface
remaining review findings. It never drafts, splits, or expands follow-up fix
plans, and never relaunches a capped review by quietly raising those limits.

## Input

The user provides the exact `asl run` command as the skill argument, for example:

```text
$asl asl run ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/
/asl asl run ~/.local/share/agent-plans/9e3ab88dedcf-7d2caa1117db/
```

The command may include leading environment assignments (such as
`CLAUDE_CONFIG_DIR=~/.claude-work`), quoted arguments, and normal `asl run`
flags, including the review-loop bounds (`--max-review-fix-cycles N`, deprecated
alias `--max-review-fix-plans N`). Shell operators, pipes, redirects,
`--dry-run`, and user-supplied `--reply` / `--reply-stdin` are not accepted —
the helper owns resume behavior. `--fresh-conversation` is refused because it
is a one-use authorization to discard an agent's saved conversation: the person
runs `asl run <plan-dir> --resume --fresh-conversation` themselves, then you
supervise the plan. `--no-mailbox` is accepted and disables the watcher, lease,
live guidance, notices and mailbox closure. `--spark` / `--no-spark` are
forwarded untouched (`references/helper-internals.md` has the Spark policy).

`--max-sessions N` is a **global** budget: the helper stops the whole supervised
run after `N` implementation sessions have completed, not `N` per relaunch;
without it, one `asl run` process drives every pending session (Step 4 reports
a budget stop). `asl control stop-after-current` stops gracefully without one.

For a registered multi-repository plan, `--cwd` is the common workspace root,
not one child checkout. Autopilot never infers a parent from one repository or
grants itself `--rebind-workspace` authority; see
`references/helper-internals.md` for the binding rules and the operator's move.

## Instructions

### Step 1: Run the bundled helper

The helper ships beside this skill at `<skill-dir>/scripts/asl-autopilot.py`,
where `<skill-dir>` is the directory containing this `SKILL.md`. Harnesses expose
the shared skill through a symlink, so resolve the real path before invoking.
Before launching it, mint a **launch identity** — one fresh UUID, kept for the
whole supervision:

```text
uuidgen
```

That identity is the only thing that separates this helper's terminal record
from one appended by another, overlapping helper on the same plan.

Then launch the helper **detached**, with the user's exact command and that
identity:

```text
<skill-dir>/scripts/asl-autopilot.py --command '<exact asl run command>' --supervision-id <launch-identity> --json --detach
```

`--detach` returns immediately. It prints one JSON launch handshake — `ok`,
`supervision_id`, `pid`, `plan_dir`, `result_path`, `stderr_path` — and its exit
code reports the **launch**, never the run: `0` once the fork succeeded, `1`
with `stage: "launch"` and `reason: "detach-failed"` when it did not. The run's
own verdict is only ever in the `result.json` at `result_path`, read in Step 4.
The supervision it forked is reparented to init, so it outlives the tool call,
the agent turn, and the harness session.

Never append `&`. Never rely on the harness backgrounding the command. Never
invent a redirect target or a `mktemp` directory — the helper owns where its
output lands, and `--supervision-log-dir DIR` is the only way to move it. A
foreground launch cannot work in this harness: a run lasts hours and a tool call
is capped at 600 seconds. Backgrounding it yourself is worse than useless — a
helper parented by a harness shell dies whenever that shell is reaped, at turn
end, at session end, or at a harness restart, and it dies leaving stdout empty,
which is indistinguishable from a healthy silent run.

The handshake proves only that the fork returned. **The first
`asl outbox supervise` call is the proof of launch.** If it answers
`LOST reason=helper-never-started`, the launch failed: report that to the user
and do not re-enter the loop.

#### Output contract (binding)

A healthy run is silent. Between launching the helper and the final report you
may emit text in exactly three cases: (1) a `QUESTION` record needs the user's
answer — relay it and ask; (2) supervision is blocked and cannot continue
without the user; (3) Step 4's final report.

Everything else is forbidden output: acknowledging the launch, announcing a
poll, restating a command you are about to run or just ran, summarizing an
`IDLE` line, reporting elapsed time, quoting the payload, describing what the
run "seems" to be doing, or reassuring the user that supervision continues.
When `asl outbox supervise` prints `IDLE`, print nothing and reissue the
identical command immediately. Hours of silence is the correct output.

Poll exactly one thing: `asl outbox supervise`. Never also tail the helper's
background output — that is the fallback for a dead or disabled mailbox
(`--no-mailbox`, SIGKILL, crash), and reading both doubles the noise for no
added signal.

Notices are informational. They never wake supervision, are never relayed, and
stay readable afterwards with
`asl outbox wait <plan-dir> --include notices --since 0 --timeout 0`.

**Never end the agent turn while the helper is active**, even if the harness
returns a session or cell identifier. Supervise by blocking on
`asl outbox supervise`, which long-polls the mailbox:

```text
asl outbox supervise <plan-dir> --supervision-id <launch-identity> [--state-dir DIR]
```

- Pass the same `--supervision-id` on every call; the command is reissued
  verbatim and no `--since` is ever threaded by hand.
- Exactly one of four tokens is printed, always with exit code 0.
- `IDLE` means nothing actionable happened: print nothing and reissue the
  identical command.
- `ACTION questions=<n>` carries `<n>` question records that no closure has
  answered yet, framed below the token. Surface them.
- `DONE outcome=<outcome> seq=<n>` carries this launch's own terminal record:
  stop the loop and do not reissue. `outcome: error` means the helper died
  abnormally, so its stdout may be missing or truncated: recover run state
  with `asl status <plan-dir> --json` and report the abnormal exit.
- `LOST reason=<reason>` means this launch's supervising helper is gone without
  a terminal record. Stop the loop, run `asl status <plan-dir> --json`, and
  surface to the user what died and whether the run itself is still alive.
  **Never auto-relaunch** — neither the helper nor the run.
- Absence of `DONE` is **not** proof that the helper is alive: `--no-mailbox`,
  SIGKILL, and a machine crash leave nothing behind.

Read `references/supervise-tokens.md` for what `DONE` does and does not prove,
the two `LOST` reasons and their run evidence, and the 30-second cadence of the
stdout-tail fallback.

Surface every `QUESTION` record immediately, confirm it is still open before
replying (`references/questions-and-pauses.md`), and keep supervising until a
`CLOSURE_CONSUMED` record or a terminal pause/result is observed.

Progress heartbeats go to stderr, escalating to `WARN possibly stalled` and
then to `reason: worker_hung` (`references/quota-and-hangs.md`). Review-loop
limits belong inside the `--command` string (`--max-review-fix-cycles 0` still
reviews but creates no repair sessions).

#### Mid-run controls

Guidance, model changes, guards, stops, aborts, and redirects go from a
separate shell; read `references/mid-run-controls.md` first. Guidance never
stops a run (only `asl control` does), a timed-out `--wait` is a failure to
report, and a redirect needs the user's explicit authorization.

### Step 2: Handle notices, live questions, and pauses

A `NOTICE id=...` record is nonblocking at-least-once progress: never reply to
it and never turn it into a user-owned decision. A `QUESTION id=...` record and
`waiting_for_reply` status are nonterminal, healthy bounded activity. Close a
live question from a separate shell with exactly one `asl inbox reply` or
`asl inbox resolve --action continue|pause`, adding
`--wait-consumed --wait-timeout 300`, and keep supervising after either
outcome. Read `references/questions-and-pauses.md` before the first closure:
it holds the commands, the accepted-versus-consumed distinction, the live-wait
timing, the helper-owned retry policy (`pause_policy`), and the registered
workspace failures that are never generic retry candidates.

Choose the closure on two axes — is the answer defensible from the plan, the
repository, and the run artifacts, and is what it causes reversible? Defensible
and reversible → reply; defensible but irreversible → relay; not defensible →
relay; plainly nonblocking → resolve `continue`; should become ordinary paused
recovery → resolve `pause`.

After a terminal `needs_agent_reply` stop, resume by streaming the chosen reply
on stdin, under a fresh launch identity. The helper launches
`asl run --reply-stdin`; the reply never appears in argv:

```text
printf '%s' '<reply text>' | <skill-dir>/scripts/asl-autopilot.py --command '<same exact command>' --supervision-id <new-launch-identity> --json --reply-stdin --detach
```

If the helper reports `reason: user_required` (a foreign active lock) or a
`blocked` / `status-error` stop, surface that to the user rather than resuming.
`quota-exhausted` and `reason: worker_hung` have their own procedures in
`references/quota-and-hangs.md`; never send a signal for a hang without the
user's approval.

### Step 3: Handle review findings

`reason: complete` with a `review` block whose `outcome` is `all-clear` needs
nothing more. On `reason: review_findings`: Do **not** draft, split, or expand a
follow-up fix plan, or relaunch with a raised cap; read
`references/review-findings.md` for what to hand back to the user.

### Step 4: Report

The detached helper's structured result is the `result.json` at the handshake's
`result_path`, not this turn's stdout; `stderr_path` holds the progress
heartbeats. Read `result_path` once `DONE` ends the loop, and fall back to
`asl status <plan-dir> --json` when the outcome was `error` and that file may be
truncated or absent.

Summarize which run was supervised, whether it completed or paused, and any
blocker that needs the user.

`reason: max-sessions` (the `--max-sessions N` budget spent) and
`reason: stopped` (the runner honored an out-of-band `asl control` request) both
carry `ok: true` with `completed` and `pending` counts and are an
**intentional, clean early stop** — never report either as a pause, a failure,
or `blocked`, and never auto-resume, reply, or run `asl questions`. Report the
progress (e.g. "completed N of M sessions; K pending") and say that re-running
the same command continues from the remaining sessions, or runs the pending
review when the stop landed after the last session.

`stop_kind` names which request produced that stop: `stop-after-current`
stopped at the session boundary, while `abort` ended the turn in flight and
reverted that session to `pending`, so it is *not* counted in `completed`. Say
so when reporting an abort, and never infer `stopped` or a reverted session
from having sent one (`references/mid-run-controls.md` has the full rules).

#### Report the supervisor audit

When the result carries a display-only `assurance` block, relay it and stop
there: say what its `issue_ids` found and hand the user its `list_command`
(`asl assurance list`); report `partial: true` findings as incomplete. The
audit is advisory and never changed the run's outcome.
This skill reports assurance and never drives it: never pass, add, or remove
the audit flags of the user's `asl run` command (`--supervisor-audit`,
`--audit-harness`, `--audit-timeout`, and the rest), and never process, dispose
of, or plan an issue.
`references/supervisor-audit.md` has the field list and the full rules.

## Notes

- One `asl run` command per invocation.
- On `repair-group-exhausted` or `cycle-cap-exhausted`, report the
  recurring `repair_group` instead of relaunching with higher caps.
- Preserve registered workspace semantics across launch, attach, replies,
  combined review, and recovery: use the bound workspace root, retain ordered
  repository aliases and qualified paths, never rebind automatically, and
  preserve partial commit SHAs.
- Mental models: read the sibling reference `../references/mental-models.md` on
  demand rather than inlining it, and cite at most 3 by name. This skill's
  assigned models are **reversibility (type-1 / type-2 doors)** and **circle of
  competence** — the two axes of the closure rule above — plus **absence of
  evidence**, the rule that a missing `DONE` record is not proof the helper is
  alive. Its operating rule: any claim about run health — including answering
  "is it still working?" — must cite a fresh `asl outbox supervise` token or a
  fresh `asl status <plan-dir> --json`, never the absence of output. Silence is
  what a healthy run and a run that never started look like alike.
- Mailbox failure is fail-open for supervision: continue owning the child and
  let the runner use the ordinary pause/resume flow. Never invent a mailbox
  reply, truncate the append-only log, delete a lease, or add Workbench
  mailbox/model-control endpoints.
