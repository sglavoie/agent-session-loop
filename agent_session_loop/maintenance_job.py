"""The ASL-owned launchd job: definition, install, uninstall, and status."""

from __future__ import annotations

import contextlib
import math
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import durability, state
from .maintenance_models import (
    COMMAND_INSTALL,
    COMMAND_STATUS,
    COMMAND_UNINSTALL,
)
from .maintenance_policy import (
    DEFAULT_HOUR,
    DEFAULT_MAX_RUNTIME_SECONDS,
    DEFAULT_MINUTE,
    JOB_LABEL,
    LAUNCH_AGENTS_RELATIVE_DIR,
    LAUNCHCTL_TIMEOUT_SECONDS,
    MaintenanceError,
    job_log_path,
    manifest_path,
)
from .maintenance_run import read_manifest


def plist_path(home: str | Path | None = None) -> Path:
    """Path of the ASL-owned launchd job definition."""
    root = Path(home).expanduser() if home is not None else Path.home()
    return root / LAUNCH_AGENTS_RELATIVE_DIR / f"{JOB_LABEL}.plist"


def job_argv(
    executable: str,
    *,
    state_dir: str | Path | None = None,
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
) -> list[str]:
    """The exact command launchd runs. Bounded and machine-readable by design."""
    argv = [
        executable,
        "maintenance",
        "run",
        "--apply",
        "--max-runtime-seconds",
        f"{max_runtime_seconds:g}",
        "--json",
    ]
    if state_dir is not None:
        argv.extend(["--state-dir", str(state_dir)])
    return argv


def render_plist(
    executable: str,
    *,
    state_dir: str | Path | None = None,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
) -> bytes:
    """Render the daily job definition.

    ``RunAtLoad`` is deliberately false: an install must not start reclaiming
    disk as a side effect of being installed. The job is a background,
    low-priority process because it competes with real work for I/O, and its
    own output is appended to the state root so `status` can point at it.
    """
    log_path = job_log_path(state_dir)
    payload: dict[str, Any] = {
        "Label": JOB_LABEL,
        "ProgramArguments": job_argv(
            executable,
            state_dir=state_dir,
            max_runtime_seconds=max_runtime_seconds,
        ),
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
        "ExitTimeOut": int(max_runtime_seconds) + 60,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    return plistlib.dumps(payload, sort_keys=True)


def _launchctl(
    arguments: list[str], *, runner: Any = subprocess.run
) -> tuple[int, str]:
    try:
        completed = runner(
            ["launchctl", *arguments],
            text=True,
            capture_output=True,
            check=False,
            timeout=LAUNCHCTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)
    detail = (completed.stderr or "").strip() or (completed.stdout or "").strip()
    return completed.returncode, detail


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _resolve_executable(executable: str | None) -> str:
    """Return the absolute ``asl`` launchd should run.

    Absolute and not a bare name: a launchd job inherits no shell environment,
    so a ``PATH`` lookup at run time is exactly the failure mode this avoids.
    """
    if executable:
        candidate = Path(executable).expanduser()
        if not candidate.is_absolute():
            candidate = candidate.resolve()
        if not candidate.exists():
            raise MaintenanceError(f"{candidate} does not exist")
        return str(candidate)
    found = shutil.which("asl")
    if found is None:
        raise MaintenanceError(
            "asl was not found on PATH; pass --executable PATH with the absolute "
            "path launchd should run"
        )
    return str(Path(found).absolute())


@dataclass(frozen=True)
class JobStatus:
    """Everything install verification needs to judge the scheduled job."""

    ok: bool
    label: str
    supported: bool
    plist_path: str
    plist_present: bool
    plist_current: bool
    loaded: bool
    load_detail: str
    executable: str
    state_root: str
    manifest_path: str
    last_run: dict[str, Any]
    history: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "command": COMMAND_STATUS,
            "label": self.label,
            "supported": self.supported,
            "plist_path": self.plist_path,
            "plist_present": self.plist_present,
            "plist_current": self.plist_current,
            "loaded": self.loaded,
            "load_detail": self.load_detail,
            "executable": self.executable,
            "state_root": self.state_root,
            "manifest_path": self.manifest_path,
            "last_run": dict(self.last_run),
            "history": [dict(entry) for entry in self.history],
        }


def _platform_supported(platform: str) -> bool:
    return platform == "darwin"


def _require_schedule(hour: int, minute: int, max_runtime_seconds: float) -> None:
    """Refuse a schedule launchd would reject or silently misread.

    The parser refuses these too; this is the same check for a direct caller,
    made before anything is written so a bad value never reaches the plist.
    """
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        raise MaintenanceError(f"hour must be an integer from 0 to 23; got {hour!r}")
    if isinstance(minute, bool) or not isinstance(minute, int) or not 0 <= minute <= 59:
        raise MaintenanceError(
            f"minute must be an integer from 0 to 59; got {minute!r}"
        )
    if not math.isfinite(max_runtime_seconds) or max_runtime_seconds <= 0:
        raise MaintenanceError(
            "max_runtime_seconds must be a finite, positive number; "
            f"got {max_runtime_seconds!r}"
        )


def install_job(
    *,
    executable: str | None = None,
    home: str | Path | None = None,
    state_dir: str | Path | None = None,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
    bootstrap: bool = True,
    platform: str = sys.platform,
    launchctl: Any = subprocess.run,
) -> dict[str, Any]:
    """Write and load the ASL-owned daily maintenance job."""
    if not _platform_supported(platform):
        raise MaintenanceError(
            f"scheduled maintenance is a launchd job and {platform} has no "
            "launchd; run `asl maintenance run --apply` from this platform's own "
            "scheduler instead"
        )
    _require_schedule(hour, minute, max_runtime_seconds)
    resolved = _resolve_executable(executable)
    document = render_plist(
        resolved,
        state_dir=state_dir,
        hour=hour,
        minute=minute,
        max_runtime_seconds=max_runtime_seconds,
    )
    target = plist_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        durability.write_bytes_file(target, document)
    except OSError as error:
        raise MaintenanceError(f"could not write {target}: {error}") from error
    state.state_root(state_dir).mkdir(parents=True, exist_ok=True)

    loaded = False
    detail = "not bootstrapped (--no-bootstrap)"
    if bootstrap:
        _launchctl(["bootout", f"{_domain()}/{JOB_LABEL}"], runner=launchctl)
        code, detail = _launchctl(
            ["bootstrap", _domain(), str(target)], runner=launchctl
        )
        loaded = code == 0
        if not loaded and not detail:
            detail = f"launchctl bootstrap exited {code}"
    return {
        "ok": loaded or not bootstrap,
        "command": COMMAND_INSTALL,
        "label": JOB_LABEL,
        "plist_path": str(target),
        "executable": resolved,
        "argv": job_argv(
            resolved, state_dir=state_dir, max_runtime_seconds=max_runtime_seconds
        ),
        "hour": int(hour),
        "minute": int(minute),
        "max_runtime_seconds": max_runtime_seconds,
        "state_root": str(state.state_root(state_dir)),
        "log_path": str(job_log_path(state_dir)),
        "bootstrapped": loaded,
        "detail": detail,
    }


def uninstall_job(
    *,
    home: str | Path | None = None,
    state_dir: str | Path | None = None,
    bootstrap: bool = True,
    platform: str = sys.platform,
    launchctl: Any = subprocess.run,
) -> dict[str, Any]:
    """Unload and remove the job, leaving every byte of evidence in place.

    Uninstalling stops the schedule and nothing else. The manifest and the job
    log stay where they are, because the record of what maintenance did to a
    corpus outlives the decision to stop maintaining it automatically.
    """
    target = plist_path(home)
    detail = "not booted out (--no-bootstrap)"
    booted_out = False
    if bootstrap and _platform_supported(platform):
        code, detail = _launchctl(
            ["bootout", f"{_domain()}/{JOB_LABEL}"], runner=launchctl
        )
        booted_out = code == 0
    removed = False
    error = ""
    try:
        target.unlink()
        removed = True
    except FileNotFoundError:
        error = "no job definition was installed"
    except OSError as os_error:
        error = str(os_error)
    return {
        "ok": not error or error == "no job definition was installed",
        "command": COMMAND_UNINSTALL,
        "label": JOB_LABEL,
        "plist_path": str(target),
        "plist_removed": removed,
        "booted_out": booted_out,
        "detail": detail,
        "error": error,
        "manifest_path": str(manifest_path(state_dir)),
    }


def job_status(
    *,
    home: str | Path | None = None,
    state_dir: str | Path | None = None,
    executable: str | None = None,
    probe: bool = True,
    platform: str = sys.platform,
    launchctl: Any = subprocess.run,
) -> JobStatus:
    """Report job presence, the last run, reclaimed bytes, and failures."""
    target = plist_path(home)
    present = target.is_file()
    resolved = ""
    with contextlib.suppress(MaintenanceError):
        resolved = _resolve_executable(executable)
    current = False
    if present and resolved:
        with contextlib.suppress(OSError):
            current = target.read_bytes() == render_plist(resolved, state_dir=state_dir)
    loaded = False
    detail = "launchctl was not probed"
    supported = _platform_supported(platform)
    if probe and supported:
        code, detail = _launchctl(
            ["print", f"{_domain()}/{JOB_LABEL}"], runner=launchctl
        )
        loaded = code == 0
        if loaded:
            detail = f"{JOB_LABEL} is loaded in {_domain()}"
    elif not supported:
        detail = f"{platform} has no launchd; no scheduled job is possible here"

    manifest = read_manifest(state_dir)
    last_run = manifest.get("last_run")
    history = manifest.get("history")
    return JobStatus(
        # A machine with no job installed is reported, not failed: install is an
        # operator decision and `status` is how they check it, not a gate.
        ok=not _status_failures(last_run),
        label=JOB_LABEL,
        supported=supported,
        plist_path=str(target),
        plist_present=present,
        plist_current=current,
        loaded=loaded,
        load_detail=detail,
        executable=resolved,
        state_root=str(state.state_root(state_dir)),
        manifest_path=str(manifest_path(state_dir)),
        last_run=last_run if isinstance(last_run, dict) else {},
        history=tuple(entry for entry in history if isinstance(entry, dict))
        if isinstance(history, list)
        else (),
    )


def _status_failures(last_run: Any) -> bool:
    if not isinstance(last_run, dict):
        return False
    return not last_run.get("ok", True)
