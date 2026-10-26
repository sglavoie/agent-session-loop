"""Auto-repair plans and suggested pauses derived from preflight findings."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from . import (
    responses,
    workspaces,
)
from .plans_errors import PlanError
from .preflight_hint_resolution import (
    _override_check_id_for_finding,
    is_missing_plan_directory,
)
from .preflight_models import (
    PreflightCategory,
    PreflightFinding,
    PreflightReport,
    RepairHint,
)


@dataclass(frozen=True)
class AutoRepair:
    """Structured record of an auto-repair action run during preflight.

    Surfaced through the ``auto_repairs`` JSON payload field so callers
    (including the runner-managed loop-ready gate planned for session 3)
    can mirror the shape when reporting their own readiness-gate repairs.

    ``target_state`` is populated for ``manifest-readiness`` rewrites so
    consumers can tell whether the repair wrote ``"loop-ready"`` (the
    default) or preserved a ``"complete"`` readiness marker for review-
    only complete starts. Repairs that do not change readiness state
    (e.g. ``progress-journal-missing``) leave it as the empty string.
    """

    action: str
    category: PreflightCategory
    path: str
    entries_written: int
    completed_session_ids: tuple[str, ...]
    journal_replaced: bool
    target_state: str = ""


def _substitute_command_placeholders(
    text: str, *, plan_dir: str, cwd: str | None
) -> str:
    result = text.replace("<plan-dir>", shlex.quote(plan_dir))
    if cwd is not None:
        result = result.replace("<repo-root>", shlex.quote(cwd))
    return result


def repair_hint_payload(
    hint: RepairHint, *, plan_dir: str, cwd: str | None = None
) -> dict[str, Any]:
    """Serialize a :class:`RepairHint`, substituting command placeholders.

    ``<plan-dir>`` in the hint ``message``/``question_template`` becomes the
    concrete plan directory and ``<repo-root>`` becomes ``cwd`` (left as the
    placeholder when ``cwd`` is ``None``), so every emitted hint embeds its
    exact plan-dir-substituted command.
    """
    data = asdict(hint)
    data["message"] = _substitute_command_placeholders(
        data["message"], plan_dir=plan_dir, cwd=cwd
    )
    data["question_template"] = _substitute_command_placeholders(
        data["question_template"], plan_dir=plan_dir, cwd=cwd
    )
    return data


LOCK_CONTENTION_PAUSE_DETAIL = (
    "active plan lock remains; wait for the runner or unlock manually if safe"
)


_AUTO_REPAIR_HANDLED_LOCK_IDS: frozenset[str] = frozenset(
    {"env-lock.active", "env-lock.stale"}
)


#: Categories the registry-migration steps clear. Both describe one condition —
#: a legacy plan reaching repositories its single bound root does not cover — so
#: ``asl register-repositories`` plus the rebind it prints answers both at once.
#: They are only dropped from the residual blockers when those steps are
#: actually emitted (see :func:`_registry_repair`).
_AUTO_REPAIR_HANDLED_CATEGORIES: frozenset[str] = frozenset(
    {"repository-footprint", "repository-registry-required"}
)


def _blocking_findings(
    report: PreflightReport,
) -> tuple[PreflightFinding, ...]:
    return tuple(
        finding for finding in report.findings if finding.severity in {"red", "yellow"}
    )


def _residual_blocking_findings(
    report: PreflightReport, *, registry_handled: bool = False
) -> tuple[PreflightFinding, ...]:
    """Blockers not already handled by the lock/progress repair steps.

    ``registry_handled`` additionally drops
    :data:`_AUTO_REPAIR_HANDLED_CATEGORIES`; it defaults to ``False`` so a
    caller that cannot say whether the registry steps were emitted keeps
    reporting the blocker.
    """
    handled_categories = (
        _AUTO_REPAIR_HANDLED_CATEGORIES if registry_handled else frozenset()
    )
    return tuple(
        finding
        for finding in _blocking_findings(report)
        if _override_check_id_for_finding(finding) not in _AUTO_REPAIR_HANDLED_LOCK_IDS
        and finding.category != "progress-journal-missing"
        and finding.category not in handled_categories
    )


def _command_repair_step(
    argv: Sequence[str], *, category: str, pause_reason: str
) -> dict[str, Any]:
    """Build one bounded repair step from an authoritative argv list.

    ``argv`` is the single source of truth: it is surfaced verbatim as the
    ``argv`` field (so a machine consumer never re-parses a string) and
    rendered into the human/shell ``command`` via the shared shell-safe
    :func:`agent_session_loop.responses.format_command` builder, so a plan path
    with whitespace or shell metacharacters round-trips as one argument.
    """
    return {
        "command": responses.format_command(list(argv)),
        "argv": list(argv),
        "max_attempts": 1,
        "category": category,
        "pause_reason": pause_reason,
    }


@dataclass(frozen=True)
class _RegistryRepair:
    """Whether preflight may advertise the registry migration, and why not.

    ``missing_fact`` is the fact the derivation refused for, so the fallback
    pause can name the one thing a human has to supply.
    """

    steps: tuple[dict[str, Any], ...] = ()
    missing_fact: str | None = None


def _registry_repair(
    report: PreflightReport,
    *,
    plan_dir: str,
    cwd: str | None,
    state_dir: str | None,
) -> _RegistryRepair:
    """The registry migration for a registry-less multi-repository plan.

    Admissibility is the migration's own: preflight runs the same read-only
    derivation ``asl register-repositories`` runs rather than judging from
    repository topology, so a plan the command would decline — an unobservable
    ``origin``, a contract check under no registered repository, a completed
    handoff a registry would invalidate — never gets a step advertised for it.
    A refusal emits neither step and reports its missing fact instead. The
    rebind's workspace root comes from that same derivation, so the command
    mirrors the first entry of the migration's own ``next_commands``.
    """
    if cwd is None:
        return _RegistryRepair()
    driving = next(
        (
            finding.category
            for finding in _blocking_findings(report)
            if finding.category in _AUTO_REPAIR_HANDLED_CATEGORIES
        ),
        None,
    )
    if driving is None:
        return _RegistryRepair()

    from . import repository_registration  # local: it imports preflight

    try:
        derived = repository_registration.derive_registration(plan_dir, cwd=cwd)
    except PlanError, OSError, workspaces.WorkspaceError:
        # Fails closed: the command reads the same files and would fail too.
        return _RegistryRepair()
    if not isinstance(derived, repository_registration.RegistrationPlan):
        return _RegistryRepair(missing_fact=derived.missing_fact)

    register = ["asl", "register-repositories", plan_dir, "--cwd", cwd]
    rebind = [
        "asl",
        "prepare",
        plan_dir,
        "--resume",
        "--rebind-workspace",
        "--cwd",
        str(derived.workspace_root),
    ]
    if state_dir is not None:
        register.extend(["--state-dir", state_dir])
        rebind.extend(["--state-dir", state_dir])
    register.append("--json")
    return _RegistryRepair(
        steps=tuple(
            _command_repair_step(argv, category=driving, pause_reason="unsafe")
            for argv in (register, rebind)
        )
    )


def build_auto_repair_plan(
    report: PreflightReport,
    *,
    plan_dir: str,
    cwd: str | None,
    state_dir: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Ordered bounded repairs mirroring session-run Step 4a, or ``()``.

    Empty when the report is already ok. An active lock short-circuits to a
    single ``blocked`` step (never unlock, never ``--force``). Otherwise the
    plan leads with the registry migration when the derivation that migration
    runs returns a plan, then lists the applicable stale-lock,
    progress-journal, and readiness repairs in Step 4a order, each with
    ``max_attempts: 1``.
    """
    if report.ok:
        return ()

    blocking = _blocking_findings(report)
    active_lock = None
    stale_lock = None
    for finding in blocking:
        override_id = _override_check_id_for_finding(finding)
        if override_id == "env-lock.active":
            active_lock = finding
        elif override_id == "env-lock.stale":
            stale_lock = finding

    if active_lock is not None:
        return (
            {
                "blocked": True,
                "pause_reason": "lock-contention",
                "detail": LOCK_CONTENTION_PAUSE_DETAIL,
            },
        )

    registry = _registry_repair(report, plan_dir=plan_dir, cwd=cwd, state_dir=state_dir)
    steps: list[dict[str, Any]] = list(registry.steps)
    if stale_lock is not None:
        steps.append(
            _command_repair_step(
                ["asl", "unlock", plan_dir, "--json"],
                category="env-lock",
                pause_reason="lock-contention",
            )
        )
    if any(finding.category == "progress-journal-missing" for finding in blocking):
        steps.append(
            _command_repair_step(
                ["asl", "backfill-progress", "--force", plan_dir],
                category="progress-journal-missing",
                pause_reason="unsafe",
            )
        )

    residual = _residual_blocking_findings(
        report, registry_handled=bool(registry.steps)
    )
    readiness = [f for f in residual if f.category == "manifest-readiness"]
    hard = [f for f in residual if f.category != "manifest-readiness"]
    if readiness and not hard:
        steps.append(
            _command_repair_step(
                ["asl", "preflight", plan_dir, "--json", "--mark-loop-ready"],
                category="manifest-readiness",
                pause_reason="unsafe",
            )
        )
    return tuple(steps)


def build_suggested_pause(
    report: PreflightReport,
    *,
    plan_dir: str,
    cwd: str | None = None,
    state_dir: str | None = None,
) -> dict[str, str] | None:
    """The pause a skill should emit if the auto-repair plan cannot clear it.

    ``None`` when the report is ok or every blocker is auto-repairable
    (stale lock, progress journal, readiness-only, or a registry migration the
    plan emits). An active lock yields a ``lock-contention`` pause; any other
    residual blocker yields ``unsafe`` with the failing categories named, and
    a nonexistent plan directory yields ``unsafe`` naming that path —
    matching Step 4a's fallback pause. A registry migration the derivation
    refused adds the fact it refused for, which is the one thing a human has
    to supply before the category can clear.
    """
    if report.ok:
        return None
    blocking = _blocking_findings(report)
    missing = next(
        (finding for finding in blocking if is_missing_plan_directory(finding)), None
    )
    if missing is not None:
        # Nothing was repaired or could be: the pause names the absent path.
        return {"reason": "unsafe", "detail": missing.message}
    if any(
        _override_check_id_for_finding(finding) == "env-lock.active"
        for finding in blocking
    ):
        return {
            "reason": "lock-contention",
            "detail": LOCK_CONTENTION_PAUSE_DETAIL,
        }
    registry = _registry_repair(report, plan_dir=plan_dir, cwd=cwd, state_dir=state_dir)
    residual = _residual_blocking_findings(
        report, registry_handled=bool(registry.steps)
    )
    hard = sorted({f.category for f in residual if f.category != "manifest-readiness"})
    if hard:
        # Automatic repair only ever covers the categories handled above, so
        # these were never attempted; the detail must not claim one failed.
        detail = f"no automatic repair clears: {', '.join(hard)}"
        if registry.missing_fact is not None:
            detail += (
                "; the registry migration refuses for missing fact "
                f"{registry.missing_fact}"
            )
        return {"reason": "unsafe", "detail": detail}
    return None
