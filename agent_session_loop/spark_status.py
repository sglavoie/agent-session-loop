"""Read the active Codex account's Spark rate-limit bucket without a turn."""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

SPARK_MODEL = "gpt-5.3-codex-spark"
SOURCE = "codex-app-server"


@dataclass(frozen=True)
class SparkStatus:
    state: Literal["available", "blocked", "unknown"]
    reason: str
    retry_at: str | None = None
    source: str = SOURCE

    @property
    def attempt_allowed(self) -> bool:
        return self.state != "blocked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "attempt_allowed": self.attempt_allowed,
            "reason": self.reason,
            "retry_at": self.retry_at,
            "source": self.source,
        }


def _bucket_status(result: object) -> SparkStatus:
    if not isinstance(result, dict):
        return SparkStatus("unknown", "invalid-response")
    buckets = result.get("rateLimitsByLimitId")
    candidates: list[dict[str, Any]] = []
    if isinstance(buckets, dict):
        candidates.extend(
            value for value in buckets.values() if isinstance(value, dict)
        )
    single = result.get("rateLimits")
    if isinstance(single, dict):
        candidates.append(single)
    bucket = next(
        (
            item
            for item in candidates
            if isinstance(item.get("limitName"), str)
            and item["limitName"].casefold() == SPARK_MODEL
        ),
        None,
    )
    if bucket is None:
        return SparkStatus("unknown", "spark-bucket-missing")
    named_windows = {
        name: item
        for name in ("primary", "secondary")
        if isinstance((item := bucket.get(name)), dict)
    }
    windows = list(named_windows.values())
    exhausted = [
        item
        for item in windows
        if isinstance(item.get("usedPercent"), (int, float))
        and not isinstance(item.get("usedPercent"), bool)
        and item["usedPercent"] >= 100
    ]
    reached_type = bucket.get("rateLimitReachedType")
    reached = reached_type not in (None, "", "none")
    if exhausted or reached:
        required = list(exhausted)
        if reached:
            named = (
                [
                    item
                    for name, item in named_windows.items()
                    if name in reached_type.casefold()
                ]
                if isinstance(reached_type, str)
                else []
            )
            required.extend(named or windows)
        resets = [
            item["resetsAt"]
            for item in required
            if isinstance(item.get("resetsAt"), (int, float))
            and not isinstance(item.get("resetsAt"), bool)
        ]
        retry_at = None
        if resets:
            with contextlib.suppress(OverflowError, OSError, ValueError):
                retry_at = (
                    datetime.fromtimestamp(max(resets), UTC)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
        return SparkStatus(
            "blocked", "rate-limit-reached" if reached else "window-exhausted", retry_at
        )
    if not any(
        isinstance(item.get("usedPercent"), (int, float))
        and not isinstance(item.get("usedPercent"), bool)
        for item in windows
    ):
        return SparkStatus("unknown", "spark-usage-missing")
    return SparkStatus("available", "rate-limit-available")


def read_status(*, executable: str | None = None, timeout: float = 5.0) -> SparkStatus:
    """Make one bounded, read-only App Server request using Codex authentication."""
    codex = executable or shutil.which("codex")
    if codex is None:
        return SparkStatus("unknown", "codex-unavailable")
    requests = (
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "spark_status",
                    "title": "Spark Status",
                    "version": "1.0.0",
                }
            },
        },
        {"method": "initialized"},
        {"method": "account/rateLimits/read", "id": 2},
    )
    process: subprocess.Popen[bytes] | None = None
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            [codex, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(
            b"".join((json.dumps(item) + "\n").encode() for item in requests)
        )
        process.stdin.flush()
        pending = b""
        while (remaining := deadline - time.monotonic()) > 0:
            readable, _, _ = select.select([process.stdout], [], [], remaining)
            if not readable:
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    reply = json.loads(line)
                except UnicodeDecodeError, json.JSONDecodeError:
                    continue
                if isinstance(reply, dict) and reply.get("id") == 2:
                    if "error" in reply:
                        return SparkStatus("unknown", "rate-limit-read-failed")
                    return _bucket_status(reply.get("result"))
        return SparkStatus("unknown", "rate-limit-read-timeout")
    except OSError, ValueError:
        return SparkStatus("unknown", "app-server-unavailable")
    finally:
        if process is not None:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                process.wait()
