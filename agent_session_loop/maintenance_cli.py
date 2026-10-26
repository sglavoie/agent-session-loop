"""Command-line handlers and renderers for ``asl maintenance``."""

from __future__ import annotations

import argparse
import json
import sys

from . import responses, retention, retention_projection
from .cli_defaults import program_name
from .maintenance_job import JobStatus, install_job, job_status, uninstall_job
from .maintenance_models import (
    COMMAND_INSTALL,
    COMMAND_RUN,
    COMMAND_STATUS,
    COMMAND_UNINSTALL,
    MaintenanceResult,
)
from .maintenance_policy import (
    COMPACTION_AGE_DAYS,
    CORPUS_BUDGET_BYTES,
    DEFAULT_HOUR,
    DEFAULT_MAX_RUNTIME_SECONDS,
    DEFAULT_MINUTE,
    LOG_BODY_BUDGET_BYTES,
    MIN_FREE_FRACTION,
    OUTCOME_LOCK_UNAVAILABLE,
    OUTCOME_PRESSURE_RELIEVED,
    OUTCOME_UNDER_THRESHOLD,
    MaintenanceError,
)
from .maintenance_run import run_maintenance


def format_bytes(count: int) -> str:
    """Render a byte count the way `asl gc` and `asl metrics` render theirs."""
    return retention.format_bytes(count)


def _print_run(result: MaintenanceResult) -> None:
    print(f"Maintenance ({result.outcome}) for {result.state_root}")
    usage = result.usage
    print(
        f"Corpus: {format_bytes(usage.corpus_bytes)} of "
        f"{format_bytes(usage.budget_bytes)} budget; volume "
        f"{usage.free_fraction * 100:.1f}% free "
        f"(floor {usage.min_free_fraction * 100:.0f}%)"
    )
    print(f"Trigger: {result.trigger}")
    if not _print_declined(result):
        _print_plan_and_outcome(result)
    # Failures and health findings are reported whatever the outcome: a pass
    # that declined to plan can still have failed (an unreadable corpus, a
    # claim it could not take), and its exit status says so.
    for failure in result.failures:
        print(
            f"  FAILED {failure.kind} {failure.target}: {failure.error} "
            f"({format_bytes(failure.bytes_still_allocated)} still allocated)"
        )
    for finding in result.health:
        prefix = "HEALTH" if finding.strict else "note"
        print(f"{prefix} {finding.code}: {finding.detail}")


def _print_declined(result: MaintenanceResult) -> bool:
    """Explain an outcome that planned nothing; ``False`` when one did."""
    if result.outcome == OUTCOME_UNDER_THRESHOLD:
        print("Both thresholds are satisfied; nothing was planned or touched.")
        return True
    if result.outcome == OUTCOME_LOCK_UNAVAILABLE:
        print("Another maintenance pass holds the claim; this one declined.")
        return True
    if result.outcome == OUTCOME_PRESSURE_RELIEVED:
        at_claim = result.usage_at_claim
        if at_claim is not None:
            print(
                f"Under the claim the corpus measured "
                f"{format_bytes(at_claim.corpus_bytes)} with "
                f"{at_claim.free_fraction * 100:.1f}% free."
            )
        print(
            "Both thresholds were satisfied by the time this pass held the "
            "claim; nothing was touched. Use --force to maintain anyway."
        )
        return True
    return False


def _print_plan_and_outcome(result: MaintenanceResult) -> None:
    plan = result.plan
    if plan is not None:
        print(
            f"Reclaimable: {plan.reclaim.reclaimable_run_count} run(s), "
            f"{format_bytes(plan.reclaimable_bytes)}"
        )
        print(
            f"Compactable: {len(plan.compactions)} terminal newest run(s) older "
            f"than {plan.compaction_age_days:g} days, "
            f"{format_bytes(plan.removable_body_bytes)} of log body above the "
            f"{format_bytes(plan.log_body_budget_bytes)} per-run budget"
        )
        retained = plan.reclaim.retained_by_reason()
        if retained:
            print("Retained by reason:")
            for reason, totals in retained.items():
                print(
                    f"  {reason}: {totals['run_count']} run(s), "
                    f"{format_bytes(totals['bytes'])}"
                )
    if not result.applied:
        print("Re-run with --apply to perform the planned maintenance.")
    else:
        print(
            f"Removed: {len(result.removed_run_ids)} run(s), "
            f"{format_bytes(result.reclaimed_bytes)} reclaimed"
        )
        print(
            f"Compaction: {result.compressed_body_count} body(ies) compressed, "
            f"{result.removed_body_count} removed, "
            f"{format_bytes(result.compaction_reclaimed_bytes)} reclaimed"
        )
    for drop in result.revalidation_drops:
        print(
            f"  dropped {drop['run_id']} ({drop['lane']}) under the claim: "
            f"{drop['reason']}"
        )


def _print_status(status: JobStatus) -> None:
    print(f"Maintenance job: {status.label}")
    if not status.plist_present:
        freshness = "absent"
    else:
        freshness = "present, current" if status.plist_current else "present, stale"
    print(f"Definition: {status.plist_path} ({freshness})")
    print(f"Loaded: {status.loaded} — {status.load_detail}")
    print(f"State root: {status.state_root}")
    print(f"Manifest: {status.manifest_path}")
    last_run = status.last_run
    if not last_run:
        print("Last run: never (no manifest has been written)")
        return
    print(
        f"Last run: {last_run.get('finished_at')} — {last_run.get('outcome')} "
        f"(trigger {last_run.get('trigger')}, ok={last_run.get('ok')})"
    )
    print(
        f"Reclaimed: {format_bytes(int(last_run.get('reclaimed_bytes') or 0))} from "
        f"{last_run.get('removed_run_count') or 0} run(s), plus "
        f"{format_bytes(int(last_run.get('compaction_reclaimed_bytes') or 0))} "
        "from compaction"
    )
    failures = last_run.get("failures")
    if isinstance(failures, list) and failures:
        print(f"Failures: {len(failures)}")
        for failure in failures:
            if isinstance(failure, dict):
                print(
                    f"  {failure.get('kind')} {failure.get('target')}: "
                    f"{failure.get('error')}"
                )
    else:
        print("Failures: 0")
    health = last_run.get("health")
    if isinstance(health, list):
        for finding in health:
            if isinstance(finding, dict):
                prefix = "HEALTH" if finding.get("strict") else "note"
                print(f"{prefix} {finding.get('code')}: {finding.get('detail')}")


def _fail(command: str, error: Exception, args: argparse.Namespace) -> int:
    """Report a leaf that could not run, in the common error envelope.

    ``command`` is the hyphenated JSON identifier (``maintenance-run``); the
    text form names the leaf the way it was typed (``asl maintenance run:``).
    """
    if getattr(args, "json", False):
        responses.print_json(
            responses.error_payload(
                command, reason=_failure_reason(error), error=str(error)
            )
        )
    else:
        leaf = command.replace("-", " ", 1)
        print(f"{program_name()} {leaf}: {error}", file=sys.stderr)
    return 1


def _failure_reason(error: Exception) -> str:
    if isinstance(error, MaintenanceError):
        return "maintenance-error"
    if isinstance(error, OSError):
        return "os-error"
    return "input-error"


def run_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl maintenance run``."""
    try:
        result = run_maintenance(
            getattr(args, "state_dir", None),
            apply=bool(getattr(args, "apply", False)),
            force=bool(getattr(args, "force", False)),
            max_age_days=getattr(
                args, "max_age_days", retention_projection.DEFAULT_MAX_AGE_DAYS
            ),
            max_runtime_seconds=getattr(
                args, "max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS
            ),
            # Stated rather than defaulted: the shipped policy is what this
            # command applies, and naming it keeps the constants the single
            # place it changes.
            compaction_age_days=COMPACTION_AGE_DAYS,
            log_body_budget_bytes=LOG_BODY_BUDGET_BYTES,
            budget_bytes=CORPUS_BUDGET_BYTES,
            min_free_fraction=MIN_FREE_FRACTION,
        )
    except (MaintenanceError, OSError, ValueError) as error:
        return _fail(COMMAND_RUN, error, args)
    if getattr(args, "json", False):
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        _print_run(result)
    return 0 if result.ok else 1


def install_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl maintenance install``."""
    try:
        payload = install_job(
            executable=getattr(args, "executable", None),
            state_dir=getattr(args, "state_dir", None),
            hour=getattr(args, "hour", DEFAULT_HOUR),
            minute=getattr(args, "minute", DEFAULT_MINUTE),
            max_runtime_seconds=getattr(
                args, "max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS
            ),
            bootstrap=not getattr(args, "no_bootstrap", False),
        )
    except (MaintenanceError, OSError, ValueError) as error:
        return _fail(COMMAND_INSTALL, error, args)
    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"Installed {payload['label']} at {payload['plist_path']}")
        print(
            f"Runs daily at {payload['hour']:02d}:{payload['minute']:02d}: "
            f"{' '.join(payload['argv'])}"
        )
        print(f"Output: {payload['log_path']}")
        print(f"Bootstrapped: {payload['bootstrapped']} — {payload['detail']}")
    return 0 if payload["ok"] else 1


def uninstall_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl maintenance uninstall``."""
    try:
        payload = uninstall_job(
            state_dir=getattr(args, "state_dir", None),
            bootstrap=not getattr(args, "no_bootstrap", False),
        )
    except (MaintenanceError, OSError, ValueError) as error:
        return _fail(COMMAND_UNINSTALL, error, args)
    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"Removed: {payload['plist_removed']} ({payload['plist_path']})")
        print(f"Booted out: {payload['booted_out']} — {payload['detail']}")
        if payload["error"]:
            print(payload["error"])
        print(
            f"Evidence is untouched; the manifest stays at {payload['manifest_path']}"
        )
    return 0 if payload["ok"] else 1


def status_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl maintenance status``."""
    try:
        status = job_status(
            state_dir=getattr(args, "state_dir", None),
            executable=getattr(args, "executable", None),
            probe=not getattr(args, "no_probe", False),
        )
    except (MaintenanceError, OSError, ValueError) as error:
        return _fail(COMMAND_STATUS, error, args)
    if getattr(args, "json", False):
        print(json.dumps(status.to_dict(), sort_keys=True))
    else:
        _print_status(status)
    return 0 if status.ok else 1
