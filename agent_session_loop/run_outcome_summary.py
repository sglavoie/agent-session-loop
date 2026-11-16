"""What a finished or paused run amounted to, said in the plan's own terms.

``run_outcome`` prints the record of a run: paths, modes, commits. This
module supplies the reading of it that a person returning to the terminal
wants first: one summary line naming the plan by its title, and, for a
``verify-failed`` pause, which check failed and how its output ended.
Everything here is derived from the payload the run already built and the
artifacts it already wrote; nothing is decided here.
"""

from __future__ import annotations

import gzip
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import plans_files
from .formatting import count_noun, format_duration_seconds

#: How many closing lines of each of a failed check's streams the pause shows.
_FAILED_CHECK_TAIL_LINES = 4
#: How many failed checks the pause describes before counting the rest.
_FAILED_CHECK_LIMIT = 3


def _artifact_tail(path: object, *, lines: int = _FAILED_CHECK_TAIL_LINES) -> list[str]:
    """The last non-blank lines of a captured (possibly gzipped) check stream.

    Best effort: a missing, unreadable or binary artifact contributes nothing,
    because the path to it is printed either way.
    """
    if not isinstance(path, str) or not path:
        return []
    try:
        raw = Path(path).read_bytes()
        if path.endswith(".gz"):
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8", errors="replace")
    except OSError, EOFError, ValueError:
        return []
    kept = [line.rstrip() for line in text.splitlines() if line.strip()]
    return [line[:200] for line in kept[-lines:]]


def failed_check_lines(payload: Mapping[str, Any]) -> list[str]:
    """Name each failed verification check and show how its output ended.

    A ``verify-failed`` pause used to name only the reason and three log
    paths; the check that failed, and the lines it failed with, are what the
    reader needs first.
    """
    details = payload.get("pause_details") or payload.get("details")
    checks = details.get("checks") if isinstance(details, Mapping) else None
    if not isinstance(checks, list):
        return []
    failed = [
        check
        for check in checks
        if isinstance(check, Mapping) and check.get("status") == "fail"
    ]
    lines: list[str] = []
    for check in failed[:_FAILED_CHECK_LIMIT]:
        lines.append(
            f"Failed check {check.get('name') or 'unnamed'}: "
            f"{check.get('detail') or check.get('failure_reason') or 'failed'}"
        )
        # Both streams: a test runner reports its failures on stdout, a
        # compiler on stderr, and the check does not say which it is.
        for label, key in (
            ("out", "stdout_artifact_path"),
            ("err", "stderr_artifact_path"),
        ):
            tail = _artifact_tail(check.get(key))
            lines.extend(f"  {label} | {line}" for line in tail)
            if tail:
                lines.append(f"  full {label}: {check[key]}")
    if len(failed) > _FAILED_CHECK_LIMIT:
        lines.append(f"(+{len(failed) - _FAILED_CHECK_LIMIT} more failed checks)")
    return lines


def summary_line(payload: Mapping[str, Any]) -> str | None:
    """One line saying what the run amounted to, in the plan's own name.

    Title, how far the plan now is, how long the run took, how many commits
    it made, and the review verdict when there was one: the things a person
    returning to the terminal wants before any path or hash.
    """
    title = plans_files.read_plan_title(payload["plan_dir"])
    parts: list[str] = []
    snapshot = payload.get("final_snapshot")
    counts = snapshot.get("counts") if isinstance(snapshot, Mapping) else None
    if isinstance(counts, Mapping):
        complete = counts.get("complete", 0)
        total = complete + counts.get("pending", 0) + counts.get("unknown", 0)
        parts.append(f"{complete} of {total} sessions complete")
    duration = payload.get("duration_seconds")
    if isinstance(duration, int | float) and not isinstance(duration, bool):
        parts.append(f"ran {format_duration_seconds(duration)}")
    commits = payload.get("commits")
    if isinstance(commits, list):
        made = sum(
            1
            for commit in commits
            if isinstance(commit, Mapping) and commit.get("success")
        )
        if made:
            parts.append(count_noun(made, "commit"))
    verdict = payload.get("review_verdict")
    if payload.get("review_completed") and isinstance(verdict, Mapping):
        status = str(verdict.get("status_line") or "").removeprefix("Status: ")
        if status:
            parts.append(f"review: {status}")
    if not parts:
        return None
    return f"Summary: {title or Path(payload['plan_dir']).name} — {'; '.join(parts)}"
