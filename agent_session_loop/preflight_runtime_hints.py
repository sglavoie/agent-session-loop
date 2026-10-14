"""Repair hints for pause reasons emitted by ``asl run`` and ``asl verify``."""

from __future__ import annotations

from .preflight_models import RepairHint

RUNTIME_REPAIR_HINTS: dict[str, RepairHint] = {
    "no-contract": RepairHint(
        category="no-contract",
        check_id="runtime.no-contract",
        severity_hint="red",
        message=(
            "Add a `## Completion Contract` section to the session file. The "
            "runner refuses to advance sessions without one."
        ),
        question_template=(
            "Runner paused: session has no Completion Contract. "
            "Should I help you add one?"
        ),
        fix_category="plan-edit",
    ),
    "malformed-contract": RepairHint(
        category="malformed-contract",
        check_id="runtime.malformed-contract",
        severity_hint="red",
        message=(
            "Fix the Completion Contract JSON. Preflight normally catches "
            "this, so check whether the session was edited after the plan was "
            "marked loop-ready."
        ),
        question_template=(
            "Runner paused: malformed Completion Contract. "
            "Should I walk you through the fix?"
        ),
        fix_category="plan-edit",
    ),
    "verify-failed.command-failed": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-failed",
        severity_hint="red",
        message=(
            "Investigate the failing command's exit code; fix the underlying "
            "issue or correct the recipe."
        ),
        question_template=(
            "Verification command exited non-zero. "
            "Show me the stderr and propose a fix?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.command-timeout": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-timeout",
        severity_hint="red",
        message=(
            "Raise the recipe timeout in the Completion Contract, or fix the "
            "underlying slowness in the command."
        ),
        question_template=(
            "Verification command timed out. "
            "Should I raise the timeout or investigate the slowness?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.command-not-found": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-not-found",
        severity_hint="red",
        message=(
            "The contract's command names an executable that is not on PATH "
            "— commonly a wrong interpreter (bare `python` instead of "
            "`python3`) or a tool that is not installed (e.g. `pytest`). Fix "
            "the interpreter/tool in the recipe (name `argv[0]` and its likely "
            "replacement), install the missing binary, or switch to the repo's "
            "own convention (e.g. `python3 -m unittest`). This class is caught "
            "earlier by the `command-not-runnable` preflight check, so seeing "
            "it at verify time usually means the plan predates that gate or "
            "the environment changed after preflight."
        ),
        question_template=(
            "Verification command's executable was not found on PATH (likely a "
            "bare `python` that should be `python3`, or an uninstalled tool). "
            "Fix the interpreter/tool in the recipe or install the binary?"
        ),
        fix_category="env-install",
    ),
    "verify-failed.command-error": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-error",
        severity_hint="red",
        message=(
            "Resolve the OS-level error that prevented command execution "
            "(permissions, missing cwd, etc.) and re-run."
        ),
        question_template=(
            "Verification command could not be executed. "
            "Should I diagnose the OS error?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.command-overbroad": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-overbroad",
        severity_hint="red",
        message=(
            "Narrow the verification command to package-scoped checks before "
            "retrying; ASL refuses repo-wide `./operator health` gates."
        ),
        question_template=(
            "Verification command is too broad for ASL session execution. "
            "Should I narrow the recipe?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.file-missing": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.file-missing",
        severity_hint="red",
        message=(
            "Create the expected artifact, or fix the contract `path` field "
            "to point at the right location."
        ),
        question_template=(
            "Expected artifact is missing. "
            "Should I create the file or correct the contract path?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.file-error": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.file-error",
        severity_hint="red",
        message=(
            "Resolve the OS-level error preventing file read/stat "
            "(permissions, encoding, etc.) and re-run."
        ),
        question_template=(
            "Verification could not read the artifact. Should I diagnose the OS error?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.regex-not-matched": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.regex-not-matched",
        severity_hint="red",
        message=(
            "Update the file to contain the expected pattern, or relax the "
            "regex in the recipe."
        ),
        question_template=(
            "Verification regex did not match. "
            "Should I update the file or loosen the pattern?"
        ),
        fix_category="session-recipe-edit",
    ),
    # Pauses blocked on a workspace or environment precondition
    # (``pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS``). These refusals come
    # from the runner's own gates, whose pause details carry findings rather
    # than repair hints, so the reason is the only key available and each hint
    # has to name the workspace change that lets the same gate pass next run.
    "commit-preflight": RepairHint(
        category="commit-preflight",
        check_id="runtime.commit-preflight",
        severity_hint="red",
        message=(
            "Commit preflight blocked startup or continuation: a repository "
            "it would commit does not meet the worktree requirements. The "
            "changes may predate the run or come from an earlier turn. Inspect "
            "the pause message and details.commit_repositories to locate the "
            "affected checkout and establish who owns the changes before "
            "committing, stashing, or discarding any work. Re-run once the "
            "worktree is ready; a reply alone cannot clear this gate."
        ),
        question_template=(
            "Runner paused: commit preflight found the workspace unready "
            "({message}). Should I walk through the dirty repositories?"
        ),
        fix_category="env-state",
    ),
    "commit-failed": RepairHint(
        category="commit-failed",
        check_id="runtime.commit-failed",
        severity_hint="red",
        message=(
            "The session's commit did not land. Re-run the failing `git` "
            "command in <repo-root> to see the error in full: a hook that "
            "rejected the commit, an unresolved merge/rebase state, or an "
            "index lock left by another process. Clear that condition and "
            "re-run; the next run first retries the outstanding commit group "
            "before it selects another session."
        ),
        question_template=(
            "Runner paused: the commit failed ({message}). "
            "Should I diagnose the git error?"
        ),
        fix_category="env-state",
    ),
    "repository-registry-required": RepairHint(
        category="repository-registry-required",
        check_id="runtime.repository-registry-required",
        severity_hint="red",
        message=(
            "This plan reaches more than one Git repository, so it cannot run "
            "in legacy single-repository mode. Run `asl register-repositories "
            "<plan-dir> --cwd <repo-root>` to derive and write the "
            "`## Repositories` registry, then the `asl prepare <plan-dir> "
            "--resume --rebind-workspace` command it prints. Where the plan "
            "need not reach the second repository at all, retarget its paths "
            "into the bound one instead — that is the cheaper fix."
        ),
        question_template=(
            "Runner paused: the plan reaches several repositories with no "
            "registry ({message}). Should I run the migration?"
        ),
        fix_category="plan-edit",
    ),
    "workspace-repository-contention": RepairHint(
        category="workspace-repository-contention",
        check_id="runtime.workspace-repository-contention",
        severity_hint="red",
        message=(
            "Another live run already claims a repository this run declares, "
            "and two agents writing one working tree cannot be made safe. "
            "Wait for the other run to finish, stop it, or move this plan to "
            "its own git worktree and re-derive the binding with `asl prepare "
            "<plan-dir> --resume --rebind-workspace`."
        ),
        question_template=(
            "Runner paused: a repository is claimed by another live run "
            "({message}). Should I find the other run?"
        ),
        fix_category="env-state",
    ),
    "not-loop-ready": RepairHint(
        category="not-loop-ready",
        check_id="runtime.not-loop-ready",
        severity_hint="red",
        message=(
            "The readiness gate refused the plan. Run `asl preflight "
            "<plan-dir> --cwd <repo-root> --json`, clear every red finding it "
            "reports, then `asl preflight <plan-dir> --json "
            "--mark-loop-ready`. The gate payload on this pause already lists "
            "the blocking findings and their own repair hints."
        ),
        question_template=(
            "Runner paused: the plan is not loop-ready ({message}). "
            "Should I drive the preflight repairs?"
        ),
        fix_category="manifest-update",
    ),
    "required-checks-incomplete": RepairHint(
        category="required-checks-incomplete",
        check_id="runtime.required-checks-incomplete",
        severity_hint="red",
        message=(
            "The run reached the end with required verification still short of "
            "passed, so it was refused the success it would otherwise have "
            "reported. The pause payload names each one under "
            "`details.deferred_unresolved` (broad gates a findings review "
            "deferred and no later all-clear review closed) and "
            "`details.unpassed_verifications`. Make those gates pass — usually "
            "by letting the review repair loop finish, raising "
            "`--max-review-fix-cycles`, or fixing the code the gate reports on "
            "— then re-run. No reply clears this: the checks have to pass."
        ),
        question_template=(
            "Runner paused: required checks never reached passed ({message}). "
            "Should I walk through the outstanding gates?"
        ),
        fix_category="env-state",
    ),
    "review-not-clear": RepairHint(
        category="review-not-clear",
        check_id="runtime.review-not-clear",
        severity_hint="red",
        message=(
            "The run finished the plan's sessions but holds no all-clear "
            "review, so it may not report the plan complete. Re-run with "
            "`--review-harness` so the generated review session executes; if a "
            "review did run and returned findings, let its repair cycles "
            "converge (raise `--max-review-fix-cycles` when the cap was the "
            "limit) until a later review comes back all-clear."
        ),
        question_template=(
            "Runner paused: the final review is missing or not all-clear "
            "({message}). Should I drive the review to completion?"
        ),
        fix_category="env-state",
    ),
    "guard-protected-path-changed": RepairHint(
        category="guard-protected-path-changed",
        check_id="runtime.guard-protected-path-changed",
        severity_hint="red",
        message=(
            "A protected-path guard froze a path at its turn-start state and "
            "the workspace no longer matches. The run kept every filesystem "
            "change on purpose and returned the session to pending. Read the "
            "matched guard IDs under `details.protected_path_guard."
            "matched_paths` and the qualified before/after content under "
            "`.evidence`, diff each named path in "
            "<repo-root>, then either restore it to its turn-start content or "
            "keep the change deliberately. Re-run once the workspace matches "
            "that decision; guards are run-scoped, so the guard is not carried "
            "into the next run unless it is added again. No reply clears this: "
            "a mailbox answer cannot restore a file."
        ),
        question_template=(
            "Runner paused: a protected path changed ({message}). "
            "Should I walk through the before/after evidence?"
        ),
        fix_category="env-state",
    ),
    "guard-protected-path-unavailable": RepairHint(
        category="guard-protected-path-unavailable",
        check_id="runtime.guard-protected-path-unavailable",
        severity_hint="red",
        message=(
            "A protected-path guard was active but the turn-start workspace "
            "snapshot it compares against is missing or unreadable, so the "
            "guard failed closed rather than claim a protection it could not "
            "check. The pause explains what was missing under "
            "`details.protected_path_guard.detail`. Repair the evidence "
            "capture itself — "
            "confirm each repository under <repo-root> is a readable git "
            "worktree with no interrupted merge/rebase or index lock, and that "
            "`git -C <repo-root> status --short` succeeds — then re-run. No "
            "reply clears this: the snapshot has to become readable."
        ),
        question_template=(
            "Runner paused: the protected-path snapshot was unreadable "
            "({message}). Should I diagnose the repository evidence?"
        ),
        fix_category="env-state",
    ),
    "conversation-recovery-required": RepairHint(
        category="conversation-recovery-required",
        check_id="runtime.conversation-recovery-required",
        severity_hint="red",
        message=(
            "The pending session's saved harness conversation cannot be "
            "resumed exactly, so the run refused to open a second one behind "
            "your back. `details.conversation_recovery` names the selection "
            "state and, when the binding is what changed, each field that no "
            "longer matches. Where the mismatch is accidental — a different "
            "`--cwd`, a re-derived repository registry, a different harness — "
            "re-run with the original values and the saved conversation is "
            "picked up unchanged. Where the conversation is genuinely gone or "
            "the change is intended, re-run with `--fresh-conversation` to "
            "authorize one fresh start: files and outstanding feedback are "
            "retained and the new turn opens with recovery context, but the "
            "agent's accumulated reasoning is lost. That authorization is "
            "one-use and is never persisted or replayed."
        ),
        question_template=(
            "Runner paused: the saved conversation cannot be resumed "
            "({message}). Should I compare the recorded binding?"
        ),
        fix_category="env-state",
    ),
    "redirect-successor-blocked": RepairHint(
        category="redirect-successor-blocked",
        check_id="runtime.redirect-successor-blocked",
        severity_hint="red",
        message=(
            "An urgent correction force-stopped the turn and the stopped "
            "worker could not be shown gone, so the run refused to start a "
            "successor that would share the workspace with it. "
            "`details.termination` carries the reason: `remaining_pids` are "
            "descendants still answering under the identity they were sampled "
            "with, `unverified_pids` are ones whose identity could not be read "
            "at all, `ownership_unknown` means a probe was refused, and "
            "`predecessor-uncollected` means the stopped worker was never "
            "reaped by the turn that owned it. Check those pids with "
            "`ps -p <pid> -o pid,ppid,lstart,command`; a survivor has to be "
            "ended and a reused pid is already harmless. One reason names a "
            "setting rather than a process: `process-sweep-opt-out` means "
            "`ASL_PROCESS_SWEEP_DISABLED` withheld the descendant evidence, so "
            "nothing is known to be running and nothing can be shown stopped "
            "either -- unset it to let the next run collect that evidence. "
            "No reply clears this "
            "and no flag overrides it: the next run re-probes, and a machine "
            "that has since gone quiet simply proceeds."
        ),
        question_template=(
            "Runner paused: the force-stopped worker could not be shown gone "
            "({message}). Should I identify the surviving processes?"
        ),
        fix_category="env-state",
    ),
    "redirect-delivery-unrecordable": RepairHint(
        category="redirect-delivery-unrecordable",
        check_id="runtime.redirect-delivery-unrecordable",
        severity_hint="red",
        message=(
            "The turn was carrying an urgent correction and the redirect "
            "journal would not record that its prompt was about to be sent, so "
            "the run refused to send it. That refusal is deliberate: a "
            "submission the journal cannot show would be indistinguishable "
            "afterwards from one that never happened, and the next run would "
            "hand the same correction to a second worker. "
            "`details.operation_ids` names the corrections still held and the "
            "preceding `redirect-continuation` event carries the journal's own "
            "reason. Check the state directory the journal lives in -- "
            "`df -h` for a full filesystem, `mount` and the directory's mode "
            "for a read-only or unwritable one -- then re-run. The words are "
            "still in the journal and the next turn carries them; nothing was "
            "lost and no reply is needed."
        ),
        question_template=(
            "Runner paused: the redirect journal could not record this turn's "
            "submission ({message}). Should I check the state directory?"
        ),
        fix_category="env-state",
    ),
}


def runtime_repair_hint(
    reason: str, *, failure_reason: str | None = None
) -> RepairHint | None:
    """Resolve a runner/verify pause reason to its repair hint.

    ``reason`` is the stable ``asl run``/``asl verify`` pause string
    (``"no-contract"``, ``"malformed-contract"``, ``"verify-failed"``).
    For ``verify-failed``, pass the per-check ``failure_reason`` (e.g.,
    ``"command-not-found"``) to resolve a more specific hint. Returns
    ``None`` for unknown reasons so callers can decide whether to fall
    back to a generic message.
    """
    if reason == "verify-failed" and failure_reason is not None:
        return RUNTIME_REPAIR_HINTS.get(f"verify-failed.{failure_reason}")
    return RUNTIME_REPAIR_HINTS.get(reason)
