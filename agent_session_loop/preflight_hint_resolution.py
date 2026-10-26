"""Resolve a preflight finding to its repair hint, honouring per-check overrides."""

from __future__ import annotations

from .preflight_models import (
    PLAN_DIRECTORY_MISSING_MESSAGE,
    PreflightFinding,
    RepairHint,
)
from .preflight_repair_hints import REPAIR_HINTS

_CATEGORY_HINT_OVERRIDES: dict[str, RepairHint] = {
    # A path that names nothing is far more often a typo or a moved plan than
    # a lost one, so the hint asks for the path before offering a re-split.
    "plan-structure.missing": RepairHint(
        category="plan-structure",
        check_id="plan-structure.missing",
        severity_hint="red",
        message=(
            "No plan directory exists at this path. Check PLAN_DIR for a typo, "
            "pass the plan's current location if it moved, or split the source "
            "plan with session-split if it was never created."
        ),
        question_template=(
            "No plan directory exists at {path}. Which path should I use as PLAN_DIR?"
        ),
        fix_category="invocation-fix",
    ),
    "repository-footprint.multi-repo": RepairHint(
        category="repository-footprint",
        check_id="repository-footprint.multi-repo",
        severity_hint="red",
        message=(
            "The plan names locations in more than one Git repository while "
            "bound to a single root, so nothing snapshots the other "
            "repositories. Run `asl register-repositories <plan-dir> --cwd "
            "<repo-root>` to declare them: the command derives the same "
            "`## Repositories` table preflight reports as "
            "`repository_footprint.suggested_registry_table`, adds the "
            "per-session **Reads repositories**: and **Writes "
            "repositories**: scopes and the `repo` alias plus "
            "repository-relative `path`/`cwd` on every contract check, then "
            "prints the `asl prepare --resume --rebind-workspace` command to "
            "run next."
        ),
        question_template=(
            "Plan spans several repositories with no registry ({detail}). "
            "Should I run `asl register-repositories` to migrate it to a "
            "registered workspace?"
        ),
        fix_category="plan-edit",
    ),
    "repository-footprint.out-of-root": RepairHint(
        category="repository-footprint",
        check_id="repository-footprint.out-of-root",
        severity_hint="red",
        message=(
            "A contract check points outside the root this plan is bound to, "
            "where no snapshot or write guard applies. Either rewrite the "
            "target as a path beneath the bound root, or run "
            "`asl register-repositories <plan-dir> --cwd <repo-root>` to "
            "declare the other location: the command derives the "
            "`## Repositories` table, the per-session **Reads repositories**: "
            "and **Writes repositories**: scopes, and the `repo` alias plus "
            "repository-relative `path`/`cwd` on every contract check, then "
            "prints the `asl prepare --resume --rebind-workspace` command to "
            "run next. It applies only to a plan reaching several "
            "repositories; a single-repository escape has to be rewritten."
        ),
        question_template=(
            "Contract target escapes the bound root ({detail}). Rewrite it, or "
            "run `asl register-repositories` to migrate the plan to a "
            "registered workspace?"
        ),
        fix_category="plan-edit",
    ),
    "manifest-readiness.missing": RepairHint(
        category="manifest-readiness",
        check_id="manifest-readiness.missing",
        severity_hint="red",
        message=(
            "Add **Readiness**, **Readiness SHA-256**, and **Readiness checked "
            "at** lines to manifest.md, or run "
            "`asl preflight <plan-dir> --json --mark-loop-ready`."
        ),
        question_template=(
            "Manifest is missing readiness metadata. "
            "Should I run `asl preflight <plan-dir> --json --mark-loop-ready` "
            "to add it?"
        ),
        fix_category="manifest-update",
    ),
    "manifest-readiness.sha-mismatch": RepairHint(
        category="manifest-readiness",
        check_id="manifest-readiness.sha-mismatch",
        severity_hint="red",
        message=(
            "Re-run `asl preflight <plan-dir> --json --mark-loop-ready` after "
            "reviewing plan edits; the recorded SHA-256 no longer matches the "
            "plan content."
        ),
        question_template=(
            "Manifest readiness SHA-256 is stale ({detail}). "
            "Should I re-mark the plan loop-ready?"
        ),
        fix_category="manifest-update",
    ),
    "manifest-readiness.state-not-loop-ready": RepairHint(
        category="manifest-readiness",
        check_id="manifest-readiness.state-not-loop-ready",
        severity_hint="yellow",
        message=(
            "Advance readiness state from `draft` or `reviewed` to "
            "`loop-ready` with `asl preflight <plan-dir> --json "
            "--mark-loop-ready` once all checks pass."
        ),
        question_template=(
            "Plan readiness state is not loop-ready ({message}). "
            "Should I advance it to loop-ready?"
        ),
        fix_category="manifest-update",
    ),
    "env-git.outside-worktree": RepairHint(
        category="env-git",
        check_id="env-git.outside-worktree",
        severity_hint="red",
        message=(
            "Run from inside a git working tree, or pass `--cwd` pointing at one."
        ),
        question_template=(
            "Repo root {path} is not inside a git working tree. "
            "Which path should I use as --cwd?"
        ),
        fix_category="invocation-fix",
    ),
    "env-git.dirty-worktree": RepairHint(
        category="env-git",
        check_id="env-git.dirty-worktree",
        severity_hint="yellow",
        message=(
            "Stash or commit dirty files before requesting "
            "`--commit-per-session` or `--commit-at-end`."
        ),
        question_template=(
            "Git worktree is dirty and commit-mode {detail} is requested. "
            "Stash, commit, or drop the commit-mode flag?"
        ),
        fix_category="env-state",
    ),
    "env-lock.stale": RepairHint(
        category="env-lock",
        check_id="env-lock.stale",
        severity_hint="yellow",
        message=("Run `asl unlock <plan-dir> --json` to clear the stale lock."),
        question_template=(
            "A stale lock is present at {path} ({detail}). "
            "Clear it with `asl unlock <plan-dir> --json`?"
        ),
        fix_category="env-state",
    ),
    "env-lock.active": RepairHint(
        category="env-lock",
        check_id="env-lock.active",
        severity_hint="red",
        message=(
            "Wait for the active runner to finish, or run "
            "`asl unlock <plan-dir> --json` if you are certain the pid is "
            "gone. Never pass `--force` from an automated repair."
        ),
        question_template=(
            "An active lock is held at {path} ({detail}). "
            "Wait for the runner or force-unlock?"
        ),
        fix_category="env-state",
    ),
    # The check id says `split` because an invariant key feeds an issue
    # fingerprint and renaming one splits the issue's history in the inbox. The
    # remedy it once named did the opposite of what the finding asks: resplitting
    # replaces one pending parent with several pending children, so it raises the
    # very count the finding is about.
    "env-turn-budget.split": RepairHint(
        category="env-turn-budget",
        check_id="env-turn-budget.split",
        severity_hint="yellow",
        message=(
            "Bound the run with `asl run --max-sessions N` so it stops inside "
            "the turn budget, complete or close pending sessions to bring the "
            "count down, or rerun preflight with `--max-pending-sessions N` "
            "when the larger serial plan is intentional."
        ),
        question_template=(
            "Pending session count exceeds the turn-budget threshold "
            "({message}). Cap `asl run --max-sessions`, close pending "
            "sessions, or raise `--max-pending-sessions`?"
        ),
        fix_category="env-state",
    ),
    "command-not-runnable.executable": RepairHint(
        category="command-not-runnable",
        check_id="command-not-runnable.executable",
        severity_hint="red",
        message=(
            "The contract command's executable is not on PATH. Use `python3` "
            "instead of `python` if only python3 is installed, install the "
            "missing binary, or switch to a runnable command."
        ),
        question_template=(
            "Session {session_id} contract command's executable is not on PATH "
            "({detail}). Should I fix the recipe?"
        ),
        fix_category="plan-edit",
    ),
    "command-not-runnable.module": RepairHint(
        category="command-not-runnable",
        check_id="command-not-runnable.module",
        severity_hint="yellow",
        message=(
            "The contract command runs `python -m <module>` for a module that "
            "is not importable here. Install the module, or prefer the repo's "
            "actual test convention (e.g. `python3 -m unittest`)."
        ),
        question_template=(
            "Session {session_id} contract command runs a module that is not "
            "importable ({detail}). Should I fix the recipe?"
        ),
        fix_category="plan-edit",
    ),
}


def is_missing_plan_directory(finding: PreflightFinding) -> bool:
    """Whether ``finding`` reports that ``PLAN_DIR`` does not exist at all."""
    return finding.category == "plan-structure" and finding.message.startswith(
        PLAN_DIRECTORY_MISSING_MESSAGE
    )


def _override_check_id_for_finding(finding: PreflightFinding) -> str:
    """Return the override check_id for a finding, or '' if no override applies."""
    if finding.category == "plan-structure":
        if is_missing_plan_directory(finding):
            return "plan-structure.missing"
        return ""
    if finding.category == "manifest-readiness":
        if "missing readiness" in finding.message:
            return "manifest-readiness.missing"
        if "SHA-256 does not match" in finding.message:
            return "manifest-readiness.sha-mismatch"
        if "readiness state is" in finding.message:
            return "manifest-readiness.state-not-loop-ready"
        return ""
    if finding.category == "env-git":
        if "not inside a git repository" in finding.message:
            return "env-git.outside-worktree"
        if "worktree is dirty" in finding.message:
            return "env-git.dirty-worktree"
        return ""
    if finding.category == "env-lock":
        if "stale lock" in finding.message:
            return "env-lock.stale"
        if "active lock" in finding.message:
            return "env-lock.active"
        return ""
    if finding.category == "env-turn-budget":
        if finding.severity == "yellow":
            return "env-turn-budget.split"
        return ""
    if finding.category == "repository-footprint":
        if "escapes the bound root" in finding.message:
            return "repository-footprint.out-of-root"
        if "git repositories" in finding.message:
            return "repository-footprint.multi-repo"
        return ""
    if finding.category == "command-not-runnable":
        if "executable is not on PATH" in finding.message:
            return "command-not-runnable.executable"
        if "module is not importable" in finding.message:
            return "command-not-runnable.module"
        return ""
    return ""


def repair_hint_for_finding(finding: PreflightFinding) -> RepairHint:
    """Resolve a preflight finding to its canonical repair hint.

    Sub-case overrides (keyed by ``check_id``) win over the category-level
    default in :data:`REPAIR_HINTS`.
    """
    override_id = _override_check_id_for_finding(finding)
    if override_id and override_id in _CATEGORY_HINT_OVERRIDES:
        return _CATEGORY_HINT_OVERRIDES[override_id]
    return REPAIR_HINTS[finding.category]
