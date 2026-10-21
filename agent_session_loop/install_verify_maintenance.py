"""Advisory scheduled-maintenance job check for install verification."""

from __future__ import annotations

import plistlib
from pathlib import Path
from typing import Any

from .install_verify_common import advisory, readable_path, success


def _job_state_dir(plist: Path) -> str | None:
    """Return the state root an installed maintenance job was pinned to.

    ``None`` when the job carries no ``--state-dir``, which is the ordinary
    install and means it maintains this installation's default state root.
    """
    try:
        payload = plistlib.loads(plist.read_bytes())
    except OSError, ValueError, plistlib.InvalidFileException:
        return None
    argv = payload.get("ProgramArguments") if isinstance(payload, dict) else None
    if not isinstance(argv, list) or "--state-dir" not in argv:
        return None
    index = argv.index("--state-dir") + 1
    return str(argv[index]) if index < len(argv) else None


def _check_maintenance_job(home: Path) -> dict[str, Any]:
    """Report the scheduled storage-maintenance job, advisory either way.

    Installing the job is an operator decision, not a precondition of a working
    install: ASL runs correctly on a machine that never reclaims a byte. What
    this reports instead is the thing an operator cannot see without asking —
    whether a job they believe is installed last ran, and whether that run
    failed. A stale definition or a failed last pass surfaces here rather than
    waiting for the volume to fill.
    """
    from . import maintenance_job, maintenance_policy, maintenance_run

    target = maintenance_job.plist_path(home)
    if not target.is_file():
        return advisory(
            "maintenance-job",
            f"no scheduled maintenance job at {readable_path(target)}; run "
            "`asl maintenance install` to reclaim state-corpus disk on a "
            "schedule, which ASL does not require",
            ok=True,
        )
    # Report on the corpus the *installed* job maintains, not whichever one
    # this process would default to, so a job pinned to an explicit state root
    # is never judged against somebody else's manifest.
    manifest = maintenance_run.read_manifest(_job_state_dir(target))
    last_run = manifest.get("last_run")
    if not isinstance(last_run, dict):
        return advisory(
            "maintenance-job",
            f"{maintenance_policy.JOB_LABEL} is installed but has not recorded a run "
            "yet; `asl maintenance status` reports it once the job has fired",
            ok=True,
        )
    if not last_run.get("ok", True):
        return advisory(
            "maintenance-job",
            f"{maintenance_policy.JOB_LABEL} last ran at {last_run.get('finished_at')} "
            f"and failed: {last_run.get('failure_count')} failure(s), "
            f"health {last_run.get('health_codes') or last_run.get('outcome')}",
        )
    return success(
        "maintenance-job",
        f"{maintenance_policy.JOB_LABEL} last ran at {last_run.get('finished_at')} "
        f"({last_run.get('outcome')}), reclaiming "
        f"{last_run.get('reclaimed_bytes')} bytes with no failures",
    )
