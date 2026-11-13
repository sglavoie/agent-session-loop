### Redirect recovery before restarting

`redirect_restart.py` checks durable predecessor evidence before startup
reconciliation, commit recovery, another attempt, or plan completion. Each
accepted turn owes proof of termination even after its runner releases the lock
or dies. Private `redirect-predecessors/` state records the worker group and
sampled descendant identities. An uncleared predecessor is checked again on
restart without sending signals. A live group, surviving descendant, unknown
identity, foreign host, missing evidence, or process-sweep opt-out keeps
`redirect-successor-blocked` in force. A proven stopped predecessor permits
normal recovery. A new run ID or `--fresh-conversation` cannot bypass this gate.
Inspect the `evidence_path` and confirmation in the pause details, stop the
identified predecessor through the normal operator process procedure, then
rerun. Missing or corrupt historical evidence requires restoring valid evidence;
rerunning or deleting a lock does not manufacture termination proof.

An open native submission window also blocks retries and relaunches of its
session with `redirect-submission-unresolved`. Inspect provider history and
`asl control redirect-status PLAN_DIR --json`, then explicitly record the
observed outcome:

```bash
asl control resolve-redirect PLAN_DIR --operation-id ID --outcome submitted --reason "Observed in native history"
asl control resolve-redirect PLAN_DIR --operation-id ID --outcome not-submitted --reason "Confirmed never submitted"
```

Choose one outcome from evidence. `submitted` permits execution without
replaying the correction or fabricating acknowledgment. `not-submitted` releases
the correction for the next prompt, but is refused if delivery is already
recorded. The command requires a reason and refuses a live runner; the decision
is durable before any release. There is no timed or default resolution. Any
other `--outcome` is refused with reason `invalid-outcome`, in the `--json`
envelope like every other refusal of this command.

For a registered `--commit-per-session` run paused after independent repository
changes, the operator can acknowledge the historical incident and explicitly
checkpoint retained owned work:

```sh
asl acknowledge-external-changes PLAN_DIR --session SESSION_ID --repository ALIAS --json
asl acknowledge-external-changes PLAN_DIR --session SESSION_ID --repository ALIAS --accept DIGEST --reason 'Independent changes authorized by operator' --checkpoint-retained --json
```

Repeat `--repository` for each independent alias. Preview never establishes
authorship or changes Git contents, indexes, refs, or session status. Acceptance
requires explicit operator authorization, a reason, the exact reviewed digest,
complete evidence, unchanged original roots/origins, binding, registry, scope,
plan content and guard policy, and no active runner. External changes cannot be
acknowledged in a writable repository. Dirty read-only repositories needed by
the current session block recovery. The retained writable paths must match the
rejected turn's original file and index fingerprints; unrelated staged files,
new dirt or content drift block checkpoints. Linked worktrees remain under their
owning alias and use their own staging root.

Recovery persists progress before committing and records checkpoint SHAs. A
partial failure prints `resume_argv`; rerun that exact command after repairing
the failure. Recorded successes are never recommitted, and interrupted commits
require exact parent, message, paths and content proof. Acknowledgement is
recorded only after checkpoint targets are clean and unrelated evidence remains
unchanged. The rejected turn and assurance issues remain intact. Checkpoints
retain unverified work and leave the session pending; verification and review
still run on resumption.

Only the current session's read/write union needs clean repositories before a
registered per-session worker launch. This check repeats before each worker, so
a previously unused dirty repository blocks when a later session needs it.
Missing or malformed scope fails closed. Complete workspace mutation guards
remain active: new changes in unused repositories still reject the turn and
require a fresh acknowledgement. Legacy and `--commit-at-end` checks retain
their existing behavior. `asl status --json` exposes preview commands and durable
recovery progress for operator inspection and supervisor audit.
