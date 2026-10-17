"""Shared fixtures for support asl guards."""

from __future__ import annotations

import stat
import threading
from pathlib import Path
from typing import Any

FAKE_GIT_SOURCE = """#!/bin/sh
echo "fake git $*"
exit 0
"""


def install_fake_git(bin_dir: Path, name: str = "git") -> Path:
    """Install a harmless stand-in for a command an operator would guard."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / name
    fake.write_text(FAKE_GIT_SOURCE, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return fake


class _RecordingPersistence:
    """The coordination persistence seam, keeping events and dropping state."""

    def __init__(self, append_event) -> None:
        self.lock = threading.RLock()
        self.append_event = append_event
        self.states: list[dict[str, Any]] = []

    def write_state(self, payload: dict[str, Any]) -> None:
        self.states.append(dict(payload))
