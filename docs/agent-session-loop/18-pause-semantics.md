## Pause Semantics

A stop-after-current request is not a pause. When `p` is active at a lifecycle
checkpoint and any further work remains — another pending session, an
unappended `--review-harness` review, or repair children a findings review would
materialize — the run exits successfully with `ok: true`, `paused: false`, and
`reason: stopped`, then releases the plan lock. The request is consumed only at
that moment, so a run that keeps working never records it as honored. If nothing
would follow the just-completed session, the run ends with `reason:
all-complete`. Stop-after-current does not add a pause reason.

An active `waiting_for_reply` mailbox window is also not a pause. The runner
retains its lock, `paused` remains false, status/tail report healthy bounded
activity, and no `run_paused` event or pause metric is created. A received
mailbox reply continues the same session inside the same runner process. While
that runner remains alive, a bounded-wait timeout or unavailable mailbox makes
it clear the additive field before entering the ordinary pause path below. If
the runner exits before it can perform that cleanup, the persisted wait remains
nonterminal; once its plan lock is absent or stale, recovery surfaces classify
it as `expired` even before its unsupervised ceiling and synthesize the
`wait-expired` pause reason without rewriting `state.json`. That lock check is
what makes an abandoned wait recoverable promptly: the ceiling itself rolls
forward with lease activity, so a record left behind by a supervised wait can
carry a projected ceiling well in the future.

A paused run exits nonzero and leaves the plan lock released. Inspect the JSON
payload, `state.json`, `events.jsonl`, and the last turn artifacts, then rerun
the same command after resolving the issue.

Common pause reasons:

- `nonzero-exit`: harness command exited nonzero. A failed exit ends the turn
  ahead of verification and the handoff guards, so if the session file still
  claims `complete` the runner takes that claim back before it publishes the
  final snapshot: the status line returns to `pending` with a Handoff note, a
  `session_reopened` event is appended, and the turn result carries a
  `completion_reconciliation` block the pause details repeat. The work the
  session already wrote is left in place — only the unadjudicated claim is
  withdrawn, so a resume selects the session the turn actually left unfinished.
- `quota-exhausted`: the provider rejected the turn for quota and supplied a
  recognized or unrecognized reset hint; ordinary runs stop for operator
  action, while Autopilot may wait only under its explicit bounded quota policy.
- `auth-failed`: harness authentication failed; retrying without repairing
  credentials is not automatic.
- `invalid-model`: the requested harness model was rejected; choose a supported
  model or clear the override before resuming.
- `timeout`: harness exceeded `--timeout`.
- `no-status-change`: the pending session did not become complete.
- `multiple-status-changes`: more than one session changed during one turn.
- `unexpected-status-change`: a different session changed.
- `malformed-plan`: the plan could not be inspected, a final handoff status is
  missing, or derived status ledgers could not be synced.
- `unknown-status`: a handoff status is not `pending` or `complete`.
- `lock-contention`: another active runner holds the plan lock.
- `workspace-repository-contention`: another live run — or a lock recorded on
  another host — already claims a repository this plan's registry declares. The
  run refuses at startup, before any harness launches, because two agents in one
  working tree corrupt each other and no guard can attribute the writes
  afterwards. `details.repository_contention` names each conflicting plan
  directory, pid, host, run id, start time, and the shared repositories. There
  is no override flag: stop the other run, or narrow this plan's registry.
- `runner-pause`: the harness output contained a `RUNNER_PAUSE:` or paused
  runner status signal.
- `no-pending-but-incomplete`: no pending session exists, but not all sessions
  are complete.
- `commit-preflight`: auto-commit setup failed before the first harness turn or
  before a generated repair child,
  such as a dirty initial worktree, a non-git working
  directory, or an incompatible `--commit-at-end` and `--max-sessions`
  combination. On a relaunch after a recoverable pause the dirty tree is no
  longer the previous process's retained work — that is settled into a
  `session-paused` commit before it exits — so the reason now means one of two
  narrower things: work the settlement did not take — residue its
  evidence-derived staging could not stage, or, when its commit refused
  outright, everything the turn retained — or dirt an operator introduced
  between the two runs. The previous run's
  `paused_work_commit.residue` names the first case repository by repository and
  path by path; anything it does not name is the second. It remains distinct
  from `commit-failed` below, which is a durable group that could not be
  reconstructed rather than a tree that was never clean to start with.
- `commit-failed`: a durable commit group could not be reconstructed. An
  unreadable ledger is the one commit failure that still pauses, because
  continuing would risk committing work twice or losing the record of what
  already landed. A repository's `asl commit --yes --no-verify` that does not
  create a clean new commit — a nonzero exit, a zero-exit no-op that leaves
  `HEAD` unchanged, or a command that changes `HEAD` but leaves tracked or
  untracked Git status behind — no longer pauses; it is recorded, retried, and
  reported. Runs recorded before that change, and the Recovery Center entries
  built from them, still carry this reason with details identifying the failed
  alias, successful repository SHAs, remaining aliases, and repo-qualified
  artifacts.
- `harness-unavailable`: a runtime-selected future harness could not be resolved
  before the next turn started. The previous completed turn remains recorded,
  the lock is released, and `pause_details` identifies the session, requested
  harness, configured executable, resolved executable when available, config
  path, and error.
- `not-loop-ready`: the plan's `manifest.md` `**Readiness**` is not
  `loop-ready` after the runner attempted any eligible derived repair, and the
  run did not pass `--allow-not-loop-ready`. The pause payload includes the
  readiness state, preflight finding summary, a `repair_hints` list carrying the
  substituted command for each blocking finding, and `readiness_gate_repair`
  when a repair was attempted. The same reason also covers the non-bypassable case:
  when any red finding is in a non-bypassable category the run pauses even with
  `--allow-not-loop-ready`, and `pause_details.blocker` lists those categories
  (currently `repository-footprint` and `repository-registry-required`).
- `unsafe`, `blocked`, `dependent`, `unresolved-prerequisite`: a
  `RUNNER_PAUSE: <reason> - ...` line from the invoked `session-run` skill
  carried a structured pause reason. The runner preserves that reason instead
  of collapsing the pause to `runner-pause`. `blocked` is the operator-answer
  case: the paused JSON payload also carries an `interactive_help` block whose
  `questions_command`, `reply_command`, and `rendered_prompt` mirror what `asl
  questions` would print, so a wrapping coordinator can route the operator
  conversation without re-reading turn artifacts.
- `empty-reply`, `no-paused-run`, `missing-question-context`,
  `reply-context-mismatch`: `asl run --reply TEXT` rejected the resume. The
  first is a CLI-only validation (empty or whitespace argument) raised before
  the plan lock is acquired. The other three come from
  `qna.load_qna_context` before the runner sends the first turn:
  `no-paused-run` (the last turn was not paused or the plan has never run),
  `missing-question-context` (the paused turn produced no `last-message.txt`
  body to feed the resolver prompt), and `reply-context-mismatch` (the paused
  turn references a session whose current pending status no longer matches the
  plan, typically because another runner already advanced past it).
- `wait-expired`: `asl questions` synthesized reply-oriented recovery for an
  orphaned nonterminal wait whose unsupervised ceiling passed or whose owning
  runner lock is absent or stale. This recovery reason is never written into `state.json`;
  the persisted wait remains available for read-only status and recovery
  projection.
- `verify-failed`: `asl verify` returned non-zero after a completed session.
  The pause payload includes the verification `reason` and per-check results.
- `no-contract`: the completed session lacks a `## Completion Contract` and the
  run was not started with `--allow-not-loop-ready`.
- `malformed-contract`: the completed session's contract failed to parse or
  validate (see `agent_session_loop.contracts` errors).
- `review-harness-required`: the plan contains a pending generated review
  session (a `session-<id>.md` carrying the `<!-- asl:generated-review-session -->`
  marker) but the run was started without `--review-harness`. Rerun with
  `--review-harness codex|claude|copilot`.
- `workspace-write-scope-violation`: a registered implementation turn changed
  a registered repository outside its exact write aliases, or added/removed
  one. A write in a repository the plan never registered is not a violation,
  so a concurrent `asl run` in a sibling checkout cannot raise this pause.
  The guard compares against the fresh pre-turn vector, so unchanged dirty
  baseline content is not adopted as a turn mutation. The pause retains all
  content, returns the selected handoff to pending, precedes the turn's own
  timeout/failure/pause and all verification/commit work, and carries a
  `workspace_write_guard` block with alias-qualified paths, topology changes,
  read/write aliases, and the required scope/expansion/preflight repair flags.
- `repository-registry-required`: a legacy run resolved declarations reaching
  more than one Git top-level. Raised by the runner's own repository hard gate,
  either at run entry (after root and workspace resolution, before the lock and
  before any subprocess) or per session task packet (before its turn launches),
  so it fires even when a `loop-ready` plan short-circuits preflight. It is not
  waived by `--allow-not-loop-ready`. `pause_details.repository_intent` is the
  bounded, credential-redacted intent report; its `git_roots` names the
  conflicting repositories. The fix is to declare the registry and per-session
  scopes, never to widen `--cwd` to their common parent.
- `workspace-write-guard-unavailable`: the runner could not establish a legacy
  run's single Git repository or capture complete Git/ref/history evidence for
  a legacy or registered repository before or after an implementation turn. A
  before-turn failure spends no harness turn. An
  after-turn failure retains content, returns the handoff to pending, and
  outranks every harness-result branch because the runner cannot prove scope.
  Every such failure names the unavailable repository evidence and stays
  fail-closed; repair that evidence rather than excluding anything. The same
  reason covers an external write the guard could not classify: every path a
  turn's task packet, session file, and last message name is classified into
  one of `control-plane`, `workspace-repository`,
  `workspace-unregistered-path`, `unregistered-repository`,
  `plan-provenance`, `system-path`, `ephemeral-path`, `unresolved`, or
  `unclassified`, and an `unclassified` path whose mtime shows it was written
  during the turn enters the `unavailable` lane under
  `external-write-unclassified`. One merely named, or last written before the
  turn began, is not charged and does not enter that lane, but it is still
  named: it is reported under `unclassified_unwritten` with the reason
  `external-write-unclassified-unwritten`, so a nonzero
  `classified.unclassified` count beside an empty `unclassified` lane is
  explained rather than reading as a guard that saw something and then said
  nothing about it. The verdict rides on the guard evidence as
  `external_writes`, carrying the per-class counts, the classification of each
  named path, and before/after digests for every control-plane location plus
  the `control_plane_mutated` list — so a permitted control-plane mutation is
  evidenced rather than assumed, and an audit-store write can no longer pass as
  workspace-clean silence.
  When the task intentionally writes external data, an operator can relaunch
  with repeatable `--external-write-root /absolute/directory` flags. Roots must
  already exist and cannot be relative, globs, the home directory, or an
  ancestor of the working directory. ASL pins canonical roots at admission,
  persists them in `run_config` for `--resume`, and records bounded before/after
  digests under `external_writes.authorized` and the
  `authorized-external-data` classification. Root redirection and unreadable
  roots remain unavailable; a symlink child pointing outside an approved root
  receives no authority from that root. Repository and control-plane
  classifications take precedence, and read-only reviews gain no write access.
  An explicit flag on resume replaces the saved root list. Unlisted paths and
  all existing repository/protected-path guards remain enforced. Approval does
  not retroactively clear a rejected turn, commit retained work, or complete a
  session; the next run uses a fresh guarded baseline.
- `guard-protected-path-changed`: an implementation turn finished with a path
  an operator froze through `asl control guard add --protect-path` differing
  from its turn-start state. Checked after `workspace-write-scope-violation`
  and before every result and commit branch, so no violating turn is published.
  The pause retains all content, returns the session to pending, and carries a
  `protected_path_guard` block with the matched guard IDs, the matched qualified
  paths, the write guard's before/after evidence for their repositories, and the
  per-lane coverage each guard had. Repair by restoring the path or removing the
  guard, then resume; there is no mailbox reply that resolves it.
- `guard-protected-path-unavailable`: a protected-path guard was active but the
  turn produced no turn-start comparison it could read, so it could neither
  clear nor convict the turn. Fail-closed for the same reason as
  `workspace-write-guard-unavailable`: repair the evidence rather than dropping
  the guard. Also an inspect-and-repair pause.
- `conversation-recovery-required`: the durable conversation bound to this plan
  session exists but cannot be continued — the ledger holds no confirmed
  conversation for it, the recorded binding no longer matches this run's
  workspace or access profile, the conversation was retired, or the ledger
  itself is unreadable. Continuity is exact-or-refusal, so the run stops rather
  than opening a second conversation for a session that already has one. The
  pause carries a `conversation_recovery` block with the disposition, the
  selection state, the session and harness, and the named mismatched binding
  fields. Repair by re-running with the workspace and harness the conversation
  was recorded under, or by authorizing one fresh start with
  `--fresh-conversation`; there is no mailbox reply that resolves it and no
  automatic retry that clears it, because discarding an agent's accumulated
  context is an operator decision.
- `review-mutated-worktree`: the review harness changed repo content outside
  the plan directory. The pre/post fingerprint covers the working tree's
  `HEAD`, tracked diff, the separate `--cached` (index) diff — so staging a
  mutation and then restoring the working file to its `HEAD` bytes still trips
  the guard — (with the plan directory and any
  `--review-guard-exclude` paths excluded from both diffs), and untracked nodes
  outside those
  paths (file bytes; a symlink's kind and link text, never the link
  target's bytes; or an untracked embedded repository's own fingerprint,
  taken the same way), gitignored ones excluded unless
  `--review-guard-include-ignored` is passed — except for the untracked
  `.gitignore` files git can apply, which are fingerprinted under either mode
  even when their own rules hide them. `.git/info/exclude`, the
  `core.excludesFile` setting, and the file it selects are fingerprinted the
  same way under either mode, and neither the plan directory nor
  `--review-guard-exclude` removes them. Any change trips the guard. Every
  probe is rooted at the repository root, so a nested `--cwd` narrows nothing. The pause message names up to three
  changed paths, and the pause details carry `changed_paths` (`tracked`,
  `untracked_added`, `untracked_removed`, `untracked_changed`,
  `worktree_ignore`, `ignore_config`, `head_changed`)
  plus the `excluded` allowlist and the `include_ignored` mode beside the raw
  fingerprints. The implementation
  turns that ran before the review are preserved. This pause outranks the
  review turn's own failure: a mutating review that also timed out, exited
  nonzero, was interrupted, emitted `RUNNER_PAUSE`, or corrupted the plan
  reports `review-mutated-worktree` (never retryable) instead of the reason it
  would otherwise have paused with.
- `review-guard-unavailable`: a git probe the mutation guard depends on failed
  (git itself unrunnable, or a nonzero `git diff`, `git diff --name-only`,
  either of their `--cached` counterparts, `git ls-files --others`, or
  `git ls-files -v`), or a path git reported could not be
  fingerprinted (`untracked-path-unreadable`, `untracked-link-unreadable`,
  `untracked-node-unsupported`, and the `index-flagged-` counterparts of all
  three for an `assume-unchanged` or `skip-worktree` entry), or a path git named
  as changed but rendered no patch for (`tracked-patch-missing`,
  `index-patch-missing`), so the guard cannot
  prove the review turn left the worktree alone. Failing probes and unreadable paths are never turned into
  empty evidence — dropping a path would hide every later change to it. The
  pause details carry a `review_mutation_guard` block with `phase` (`before` or
  `after` the turn) and `unavailable` (`reason`, `argv`, the offending `path`
  for a per-path failure, and the git `returncode`/`stderr` when there is one). A failure on the post-turn capture
  reverts the review session to pending so a resume re-runs it under a working
  guard.
- `plan-artifact-tampered`: the turn changed a `session-<id>.md` or
  `session-<id>.expand.md` belonging to a session its task packet did not
  select. Every session artifact except the selected session's is hashed before
  the harness launches and re-hashed after it returns; `manifest.md` and
  `context.md` are excluded because the runner rewrites them itself, and the
  review-handoff files the runner normalizes after the turn are re-baselined
  rather than blamed on the turn. A turn that ends on a result branch — a
  timeout, an interruption, an abort, a structured pause, a nonzero exit, or a
  plan the runner could not inspect — is compared before that branch runs, and
  the guard outranks it: those outcomes either retry or loop straight into
  another turn, which would adopt the tampered packet as the new baseline. The
  `plan_artifact_guard` block then names the outcome it outranked in
  `result_disposition`. A turn that reaches the status-change guards is
  compared behind them instead, so completing the wrong session still reports
  `unexpected-status-change`. The pause details and the turn's `result.json`
  carry a `plan_artifact_guard` block with `changed_artifacts`,
  `digests_before`, `digests_after`, and `runner_written`. The selected session
  is reverted to pending, so a resume cannot bank a completion earned against a
  plan the turn rewrote underneath itself.
  A turn an earlier finalization guard already ended — the workspace write
  guard, the legacy implementation write guard, the review mutation guard, or a
  protected-path conviction — still reaches this guard, in adjudicate-only mode.
  Staying inside a declared write scope and leaving the other packets alone are
  different questions, and a turn can fail both at once; before that the earlier
  pause ended the turn first and the changed packets stayed in the plan with no
  verdict against them anywhere. The adjudicated block is marked
  `adjudicated_only`, names the reason that did end the turn in
  `deferred_to_pause_reason`, and is added to that pause's details rather than
  published under a reason of its own, so the remediation an operator reads
  stays the first guard's. It records `reverted_to_pending` as `false`: whatever
  ended the turn first already withdrew the session if it needed withdrawing,
  and a second revert would append a second failure note for one turn. The
  `turn_rejected` event it appends carries `adjudicated_only` and
  `changed_artifacts` in its `detail`.
- `interrupted`: the run was interrupted by Ctrl+C.
