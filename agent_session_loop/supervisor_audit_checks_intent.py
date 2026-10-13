"""Checks that the recorded roots, repository intent and task-packet paths agree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import plans_errors, plans_transaction, preflight_footprint
from . import supervisor_audit_evidence as audit_evidence
from .supervisor_audit_models import _CheckContext, _inside, _joined, _names, _Verdict
from .supervisor_audit_vocabulary import _TURN_PATH_KEYS


def _check_root_registry_agreement(context: _CheckContext) -> _Verdict:
    plan = context.section("plan")
    metadata = audit_evidence._mapping(plan.get("metadata"))
    registry = context.section("registry")
    evidence = context.evidence(
        metadata.get("reference"),
        context.section("state").get("reference"),
        plan.get("reference"),
    )
    if audit_evidence.EVIDENCE_ROOT_WORKSPACE not in context.roots:
        return _Verdict(
            "unavailable",
            "the run declared no workspace root, so no binding can be compared "
            "against one",
            evidence,
        )

    bindings: list[tuple[str, Any]] = []
    content = audit_evidence._mapping(metadata.get("content"))
    if metadata.get("available") and content.get("workspace_root") is not None:
        bindings.append(("the plan metadata", content.get("workspace_root")))
    if registry.get("registered"):
        if registry.get("workspace_root") is not None:
            bindings.append(("the run registry", registry.get("workspace_root")))
        for entry in audit_evidence._sequence(registry.get("repositories")):
            entry = audit_evidence._mapping(entry)
            alias = entry.get("alias") or "an unnamed repository"
            # ``path`` is deliberately workspace-relative in current runner
            # state.  Containment is about the repository ASL actually
            # resolved, so only the canonical ``resolved_root`` can establish
            # it.  Older, malformed, outside-root, or redacted entries fail
            # closed instead of being made safe by resolving a relative path
            # against the auditor process's cwd.
            bindings.append(
                (f"registered repository {alias}", entry.get("resolved_root"))
            )
    if not bindings:
        return _Verdict(
            "unavailable",
            "neither the plan metadata nor the run registry recorded a workspace "
            "binding to compare against the run's root, which is what a plan "
            "that predates workspace registration looks like rather than "
            "evidence of a disagreement",
            evidence,
        )

    disagreeing = [
        (
            f"{source} records no resolved root"
            if value is None
            else f"{source} resolves to {value}"
        )
        for source, value in bindings
        if value is None or not _inside(value, audit_evidence.EVIDENCE_ROOT_WORKSPACE)
    ]
    if disagreeing:
        return _Verdict(
            "fail",
            "the run's workspace root disagrees with what its own records bind "
            f"it to: {_joined(disagreeing)}",
            evidence,
        )
    return _Verdict(
        "pass",
        f"all {len(bindings)} recorded workspace bindings resolve inside the "
        "run's workspace root",
        evidence,
    )


def _check_repository_intent(context: _CheckContext) -> _Verdict:
    plan = context.section("plan")
    evidence = context.evidence(
        plan.get("reference"), context.section("state").get("reference")
    )
    plan_root = context.roots.get(audit_evidence.EVIDENCE_ROOT_PLAN)
    workspace_root = context.roots.get(audit_evidence.EVIDENCE_ROOT_WORKSPACE)
    if not plan_root or not workspace_root or not Path(plan_root).is_dir():
        return _Verdict(
            "unavailable",
            "the run recorded no plan directory and workspace root to replay the "
            "repository-intent gate against",
            evidence,
        )
    # Replaying the gate reads the plan through ``plans_metadata``, which rolls a
    # pending publication forward as a side effect. An audit observes a run; it does not
    # finish one, so an interrupted plan is reported rather than repaired.
    if (Path(plan_root) / plans_transaction.PLAN_TRANSACTION_DIR_NAME).exists():
        return _Verdict(
            "unavailable",
            "the plan directory holds a pending publication transaction, and "
            "replaying the repository-intent gate would complete it rather than "
            "observe it",
            evidence,
        )
    exempt = [
        context.roots[name]
        for name in (
            audit_evidence.EVIDENCE_ROOT_STATE,
            audit_evidence.EVIDENCE_ROOT_RUN,
        )
        if context.roots.get(name)
    ]
    try:
        intent, report = preflight_footprint.plan_repository_intent(
            plan_root, workspace_root, extra_exempt_roots=exempt
        )
    except (OSError, ValueError, plans_errors.PlanError) as error:
        return _Verdict(
            "unavailable",
            f"the repository-intent gate could not be replayed after the run: {error}",
            evidence,
        )
    if report is None:
        if intent is not None:
            reachable = _names(Path(root).name for root in intent.git_roots)
            return _Verdict(
                "pass",
                "the legacy plan stays within its bound Git repository"
                + (f": {reachable}" if reachable else ""),
                evidence,
            )
        return _Verdict(
            "pass",
            "the plan declares a repository registry naming every repository it "
            "may reach",
            evidence,
        )
    reachable = _names(
        Path(str(root)).name
        for root in audit_evidence._sequence(report.get("git_roots"))
    )
    return _Verdict(
        "fail",
        f"the plan reaches {report.get('git_root_count')} git repositories but "
        f"declares no repository registry: {reachable}",
        evidence,
    )


def _check_task_packet_paths(context: _CheckContext) -> _Verdict:
    contradictions: list[str] = []
    references: list[str] = []
    observed = 0
    workspace_declared = audit_evidence.EVIDENCE_ROOT_WORKSPACE in context.roots
    for turn in context.turns:
        directory = turn.get("directory")
        result = audit_evidence._mapping(turn.get("result"))
        if not isinstance(directory, str) or result.get("available") is False:
            continue
        references.append(directory)
        for key, basename in _TURN_PATH_KEYS:
            value = result.get(key)
            if value is None:
                continue
            observed += 1
            expected = directory if basename is None else f"{directory}/{basename}"
            if value != expected:
                contradictions.append(
                    f"{turn.get('name')} names {value} as its {key} rather than "
                    f"{expected}"
                )
        cwd = audit_evidence._mapping(result.get("invocation")).get("cwd")
        if isinstance(cwd, str) and workspace_declared:
            observed += 1
            if not _inside(cwd, audit_evidence.EVIDENCE_ROOT_WORKSPACE):
                contradictions.append(
                    f"{turn.get('name')} ran in {cwd}, outside the run's workspace"
                )
    configured_cwd = context.section("configuration").get("cwd")
    if isinstance(configured_cwd, str) and workspace_declared:
        observed += 1
        if not _inside(configured_cwd, audit_evidence.EVIDENCE_ROOT_WORKSPACE):
            contradictions.append(
                f"the run configuration names {configured_cwd} as its working "
                "directory, outside the run's workspace"
            )
    evidence = context.evidence(*references, context.section("state").get("reference"))
    if contradictions:
        return _Verdict(
            "fail",
            f"a task packet names paths its own run contradicts: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if not observed:
        return _Verdict(
            "unavailable",
            "no turn recorded the artifact paths its packet was written from",
            evidence,
        )
    return _Verdict(
        "pass",
        f"all {observed} recorded packet paths agree with the artifacts they name",
        evidence,
    )
