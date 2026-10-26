# ASL Autopilot: live questions and pauses

On-demand reference for Step 2 of the `asl` skill (`../SKILL.md`): the full
closure contract for live mailbox questions, the helper's pause and retry
policy, and the registered-workspace failures that are operator-visible rather
than retryable.

## Handle notices, live questions, and pauses

First distinguish an informational notice, a live mailbox question, and a
terminal pause. A `NOTICE id=...` record is nonblocking at-least-once progress:
observe or relay it, but never reply to it and never turn it into a user-owned
decision. A `QUESTION id=...` progress record and `waiting_for_reply` status are
nonterminal, healthy bounded activity. Close a live question from a separate
shell with exactly one of the commands below:

```text
asl inbox reply <plan-dir> --to <question-id> --text '<answer>' [--state-dir DIR] --json
asl inbox resolve <plan-dir> --to <question-id> --action continue [--note '<note>'] [--state-dir DIR] --json
asl inbox resolve <plan-dir> --to <question-id> --action pause [--note '<note>'] [--state-dir DIR] --json
```

Add `--wait-consumed --wait-timeout 300` when closing a live question so the
command can confirm the receipt. A successful append is only **accepted** until
`consumed: true` and a receipt are returned; a consumption timeout remains a
successful append with `consumed: false` and `timed_out: true`. Never call an
unconsumed closure “delivered,” and keep polling the helper after either
outcome.

Use `--text-stdin` or `--note-stdin` when a body should not appear in argv. A
reply supplies a factual answer. A `continue` resolution supplies no answer and
resumes the same session; its optional note is context, never an answer. A
`pause` resolution supplies no answer and deliberately converts the live wait
into the ordinary terminal paused-run recovery path. The first concurrent reply
or resolution wins, and the runner consumes that closure once. Do not stop the
helper, invoke `--reply-stdin`, or report a terminal pause while an unresolved
live wait is active. The runner waits five minutes initially.
Lease freshness is checked only at an active deadline: a fresh lease extends
the wait by 60 seconds, while an absent or stale lease ends it — unless the
runner detected a system suspend, which grants the supervisor a 90-second
awake grace to re-prove liveness before a stale lease ends the wait. Inside
that grace a stale lease neither ends the wait nor extends it, and the grace
never stretches the ceiling. The 30-minute ceiling bounds unsupervised time,
not wall clock: it rolls forward with every fresh lease, so keeping the
supervisor alive keeps the wait alive, and a wait nobody watches still ends
within 30 minutes of the last proof.
An already-committed reply or resolution takes
precedence over mailbox exhaustion and is consumed before exhaustion can end
the wait. If no closure is available, timeout, mailbox
fullness/unavailability, or a stale lease at an active deadline that outlasts
any post-suspend grace falls
back to the existing terminal pause flow below; there is no mailbox-only
terminal status.

Retry mechanics are helper-owned — see the result payload's `pause_policy`.
The helper auto-retries recoverable same-session pauses (`verify-failed`, `transport-failed`, `timeout`, `no-status-change`) up to twice per session and reason. Only `transport-failed` backs off before relaunching (5 seconds, then 30 seconds); the other recoverable pauses retry immediately. An unclassified `nonzero-exit` pause is never retried. Escalation follows the block's `user_action`: `reply` means an operator answer can clear the pause, so it goes through the mailbox wait; `inspect` means no answer can and the environment has to change instead, so it escalates immediately; `null` is a recoverable pause that needs no human. Three kinds of pause are `inspect` — the non-retryable harness failures that carry no question context (`quota-exhausted`, `auth-failed`, `invalid-model`); the pauses blocked on a workspace or environment precondition (`commit-preflight`, `commit-failed`, `repository-registry-required`, `workspace-repository-contention`, `not-loop-ready`, `required-checks-incomplete`, `review-not-clear`, `guard-protected-path-changed`, `guard-protected-path-unavailable`, `conversation-recovery-required`, `redirect-delivery-unrecordable`); and the verdicts a post-turn guard reached along with the turn outcomes nobody asked a question about (`workspace-write-scope-violation`, `workspace-write-guard-unavailable`, `review-guard-unavailable`, `review-mutated-worktree`, `plan-artifact-tampered`, `unexpected-status-change`, `multiple-status-changes`, `nonzero-exit`, `malformed-plan`, `malformed-review-repair`, `interrupted`, `redirect-successor-blocked`, `redirect-submission-unresolved`). The dividing principle is whether a mailbox answer can make the run proceed: a dirty worktree stays dirty however the operator answers. For that third group the same principle reads from the other side — the mailbox wait is entered only from the runner-pause disposition, so a guard verdict advertising `reply` was advertising an answer it had no channel to receive. An `inspect` pause carries its own `remediation` entry naming the workspace change that clears it, so read `action_required.remediation` before escalating. `requires_user` covers every pause a machine cannot retry, and `policy_version` distinguishes a schema change from a real contradiction when a persisted block is compared against a fresh classification; `policy_version: 3` is the revision that moved the blocked-precondition reasons off `reply`, and `policy_version: 4` the one that moved the guard verdicts and unclassifiable turn outcomes off it too.

`policy_version: 5` also makes `verify-failed` require inspection when `verification_repair_exhausted` is true; an exhausted repair budget is never automatically retried. When a pause genuinely needs an answer, the result carries `reason: needs_agent_reply` with a `questions` block holding the resolver prompt, the paused agent's last message, the pause metadata, and artifact paths.

Choose the closure conservatively, on two axes rather than one:

- **Epistemic — is the answer defensible?** Is it clearly derivable from the
  plan, the repository, and the run artifacts referenced in the `questions`
  block? This is circle of competence: inside it the answer is a fact to read
  from those artifacts; outside it the answer is a guess, so relay it.
- **Consequence — is what the answer causes reversible?** A **type-2 door** is
  cheap to undo (an implementation detail, a naming choice, a retry, a wording
  fix) and may be answered autonomously once defensible. A **type-1 door** is
  hard or impossible to undo (a schema decision, a data migration, a deletion, a
  published artifact, anything the completion contract bakes in) and relays to
  the user **even when the answer is defensible** — the cost of being wrong,
  not the confidence of being right, decides who answers.

The resulting rule: defensible and reversible → reply; defensible but
irreversible → relay; not defensible → relay; plainly nonblocking → resolve
`continue`; should become ordinary paused recovery → resolve `pause`. The four
closures in full:

- Resolve `continue` autonomously only when the worker explicitly labels the
  message nonblocking or its informational nature is plain. Do not use a fake
  reply for an answerless continuation.
- Send a factual reply only when it is clearly defensible from the plan, the
  repository, and the run artifacts referenced in the `questions` block, **and**
  what it sets in motion is a type-2 door. Read those artifacts before deciding.
- Relay ambiguous decisions to the user and wait for their explicit answer when
  intent, a trade-off, or information outside the plan/repository/artifacts is
  required — and relay a type-1 door on the same terms even when the artifacts
  answer it.
- Resolve `pause` only when the live wait should deliberately become ordinary
  paused-run recovery instead of continuing in place.

After a terminal `needs_agent_reply` stop, resume by streaming the chosen reply
on stdin. The helper launches `asl run --reply-stdin`; the reply never appears
in argv:

```text
printf '%s' '<reply text>' | <skill-dir>/scripts/asl-autopilot.py --command '<same exact command>' --supervision-id <new-launch-identity> --json --reply-stdin --detach
```

The detached parent reads that reply to the end before forking, so it reaches
the run even though the forked supervision's own stdin is `/dev/null`.

Every helper launch is its own supervision: mint a fresh launch identity for it
and use that identity in the wait loop that follows, so the previous launch's
terminal record cannot end this one.

If the helper reports `reason: user_required` (a foreign active lock) or a
`blocked` / `status-error` stop, surface that to the user rather than resuming. A
paused run may also cite a `command-not-runnable` readiness finding (an
unresolved `argv[0]`, or a non-importable `python -m <module>`); surface it
rather than resuming.

Registered workspace guard and commit-group failures are also operator-visible,
not generic retry candidates:

- `workspace-write-scope-violation` retains the worker's changes, restores the
  handoff to pending, and reports qualified `Alias:path` evidence for writes in
  read-only or out-of-scope registered repositories and for added/removed
  registered repositories. A repository the plan never registered cannot raise
  it. Do not revert or adopt that delta; the operator must restore it or
  durably repair scope and refresh preparation evidence. Independent changes
  have an explicit operator-authorized recovery: preview with
  `asl acknowledge-external-changes PLAN_DIR --session SESSION_ID --repository ALIAS --json`,
  review the evidence, then accept its digest with a reason.
  `--checkpoint-retained` separately authorizes verified retained-path commits.
  Never infer independent authorship, acknowledge a writable alias, or recommit
  recorded checkpoint successes. Checkpoints leave the session pending.
- `workspace-write-guard-unavailable` means complete Git evidence could not be
  captured for a registered repository. Treat it fail-closed and surface the
  affected aliases/topology. The failing node always sits inside the registry,
  so the repair is that repository's own evidence — there is no exclusion that
  waives it.
- `commit-failed` may represent a partial multi-repository commit group. Read
  the unresolved group, successful per-repository SHAs, failed alias, remaining
  aliases, and qualified artifacts from status/recovery output. Never undo or
  recommit recorded successes; after the failing repository is repaired,
  rerunning the same command resumes the durable group before another worker.

## Before replying to a returned question

`outbox supervise` filters its batch to questions no closure has answered, but a
question can still finish between the wake and the reply. Before replying to any
returned question, confirm it is still open: a `WAIT_ENDED` or
`CLOSURE_CONSUMED` record for that id, or an `asl inbox reply` rejected with
`question-not-waiting`, means it is already finished and must not be answered
again.

After queuing a reply or resolution, continue supervising until a
`CLOSURE_CONSUMED` record, an equivalent consumed receipt, or a terminal
pause/result is observed.
