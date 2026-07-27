"""One-shot, portable process-tree sampling for turn-hang diagnostics.

A single headerless ``ps`` invocation (macOS and Linux compatible) is parsed
into a ppid->children map and walked breadth-first from the harness root pid.
Any failure — spawn error, timeout, nonzero exit, unparsable output, or an
absent root — yields ``None`` so callers stay fail-open.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import re
import subprocess
from typing import Any

PS_COMMAND: tuple[str, ...] = ("ps", "-axo", "pid=,ppid=,state=,time=,command=")
COMMAND_MAX_CHARS = 160
SAMPLE_TIMEOUT_SECONDS = 5.0

# macOS ``ps -o time`` emits ``0:00.03`` (mm:ss.ff); Linux emits
# ``[[dd-]hh:]mm:ss``. One pattern covers both.
_CPU_TIME_RE = re.compile(
    r"^(?:(?P<days>\d+)-)?(?:(?P<hours>\d+):)?"
    r"(?P<minutes>\d+):(?P<seconds>\d+(?:\.\d+)?)$"
)


def _parse_cpu_time(raw: str) -> float | None:
    match = _CPU_TIME_RE.fullmatch(raw.strip())
    if match is None:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = float(match.group("seconds"))
    return float(days * 86400 + hours * 3600 + minutes * 60) + seconds


def sample_process_tree(
    root_pid: int,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = SAMPLE_TIMEOUT_SECONDS,
) -> list[dict[str, Any]] | None:
    """Return the process tree rooted at ``root_pid`` or ``None`` on failure.

    Each entry carries ``pid``, ``ppid``, ``state``, ``cpu_seconds``, and a
    ``command`` trimmed to ``COMMAND_MAX_CHARS``, in BFS order from the root.
    """
    try:
        completed = run_command(
            list(PS_COMMAND),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:  # noqa: BLE001 - sampling is diagnostics, never a failure
        return None
    if completed.returncode != 0:
        return None

    processes: dict[int, dict[str, Any]] = {}
    children: dict[int, list[int]] = {}
    for line in (completed.stdout or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) != 5:
            continue
        raw_pid, raw_ppid, process_state, raw_time, command = parts
        try:
            pid = int(raw_pid)
            ppid = int(raw_ppid)
        except ValueError:
            continue
        cpu_seconds = _parse_cpu_time(raw_time)
        if cpu_seconds is None:
            continue
        processes[pid] = {
            "pid": pid,
            "ppid": ppid,
            "state": process_state,
            "cpu_seconds": cpu_seconds,
            "command": command[:COMMAND_MAX_CHARS],
        }
        children.setdefault(ppid, []).append(pid)

    if root_pid not in processes:
        return None

    tree: list[dict[str, Any]] = []
    queue = [root_pid]
    seen = {root_pid}
    while queue:
        pid = queue.pop(0)
        tree.append(processes[pid])
        for child_pid in children.get(pid, ()):
            if child_pid not in seen:
                seen.add(child_pid)
                queue.append(child_pid)
    return tree


def total_cpu_seconds(tree: Iterable[Mapping[str, Any]]) -> float:
    """Sum ``cpu_seconds`` across every sampled process in the tree."""
    return sum(float(entry.get("cpu_seconds") or 0.0) for entry in tree)
