# ruff: noqa: F403, F405
from .workbench_details_models import *
from .workbench_details_models import _JSONDict


def canonical_plan_dir(plan_dir: str | Path) -> Path:
    """Canonicalize a caller-supplied plan directory, or refuse it."""
    try:
        return plans_files.canonicalize_path(plan_dir)
    except (OSError, ValueError) as error:
        raise DetailError(
            details.REASON_INVALID_PLAN,
            f"invalid plan_dir: {error}",
            details={"plan_dir": str(plan_dir)},
        ) from error


def artifact_paths(
    summary: workbench_summary_model.WorkbenchSummary, plan_dir: Path
) -> dict[str, Path | None]:
    """Every artifact this plan and run published, by name.

    ``final_review_artifact_path`` is the one entry the summary does not carry:
    ``final-review.json`` lives beside the plan rather than in its run state,
    so it is derived here. Its descriptor still reports whether it exists, so
    naming it is not a claim that it does.
    """
    return {
        "state_path": summary.state_path,
        "events_path": summary.events_path,
        "progress_path": summary.progress_path,
        "final_review_path": summary.final_review_path,
        "final_review_artifact_path": (
            plan_dir / workbench_summary_helpers.FINAL_REVIEW_ARTIFACT_FILE_NAME
        ),
        "artifact_dir": summary.artifact_dir,
        "stdout_path": summary.stdout_path,
        "stderr_path": summary.stderr_path,
        "last_message_path": summary.last_message_path,
        "result_path": summary.result_path,
    }


def build_scope(
    summary: workbench_summary_model.WorkbenchSummary,
    plan_dir: Path,
    *,
    plan_state_dir: Path,
) -> DetailScope:
    """The portable scope for one classified plan.

    The identities are the catalog's own :func:`asl_workbench.catalog.plan_key`
    and :func:`asl_workbench.catalog.run_key`, so a detail read and a cached
    catalog row name the same plan and the same run by the same string -- which
    is what lets :meth:`asl_workbench.details.DetailScope.check_run` tell a
    stale request from a current one.
    """
    key = portable_catalog.plan_key(str(plan_dir))
    return DetailScope(
        plan_key=key,
        plan_dir=str(plan_dir),
        plan_state_dir=str(plan_state_dir),
        artifacts={
            name: (str(path) if path is not None else None)
            for name, path in artifact_paths(summary, plan_dir).items()
        },
        run_key=portable_catalog.run_key(key, summary.run_id),
        run_id=summary.run_id,
    )


def open_plan(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> PlanReader:
    """Classify one plan for reading, reclaiming nothing and repairing nothing.

    The single entry point for every detail read below, which is what makes
    "detail inspection is observational" checkable in one place rather than
    remembered at ten call sites.
    """
    canonical = canonical_plan_dir(plan_dir)
    resolved_state_dir = Path(state_dir) if state_dir is not None else None
    plan_state = state.plan_state_dir(canonical, resolved_state_dir)
    discovered = workbench_registry_models.DiscoveredPlan(
        plan_dir=canonical, exists=canonical.is_dir()
    )
    summary = workbench_catalog.build_summary(discovered, state_dir=resolved_state_dir)
    return PlanReader(
        plan_dir=canonical,
        plan_state_dir=plan_state,
        summary=summary,
        scope=build_scope(summary, canonical, plan_state_dir=plan_state),
        state_dir=resolved_state_dir,
    )


def read_lock(reader: PlanReader) -> state.LockInfo | None:
    """Classify this plan's lock without removing it.

    ``reclaim=False`` is the whole point: the shared lock reader's default is
    to delete a lock it decides is stale, and a detail panel asking what the
    lock says must not be the thing that releases it. ``POST /api/unlock`` is
    where a release is asked for.
    """
    return state_locks._read_lock_path(reader.lock_path, reader.plan_dir, reclaim=False)


# ---------------------------------------------------------------------------
# Plan and artifact reads


def read_plan(
    plan_dir: str | Path,
    *,
    registry: workbench_registry_models.WorkbenchRegistry,
    state_dir: str | Path | None = None,
) -> _JSONDict:
    """One plan's triage summary, carrying its registry pin when it has one.

    The only read that consults the registry: a pin's label and launch profile
    belong to the plan the operator pinned, and a summary built without them
    would answer a deep link with a plan that had lost its own name.
    """
    canonical = canonical_plan_dir(plan_dir)
    pin = next((p for p in registry.pins if p.plan_dir == str(canonical)), None)
    discovered = workbench_registry_models.DiscoveredPlan(
        plan_dir=canonical,
        sources=("registry",) if pin is not None else (),
        label=pin.label if pin is not None else None,
        profile=pin.profile if pin is not None else None,
        exists=canonical.is_dir(),
    )
    summary = workbench_catalog.build_summary(discovered, state_dir=state_dir)
    payload = summary.to_payload()
    # The queue row names a plan by its manifest title; the detail header has
    # to as well, or opening a plan retitles it with its directory name.
    payload["title"] = workbench_catalog.read_plan_title(canonical)
    return {"plan": payload}
