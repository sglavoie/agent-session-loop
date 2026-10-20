"""Shared helpers for the ``test_asl_harness_compatibility*`` modules."""

from __future__ import annotations

import stat
from pathlib import Path

from tests import asl_live_redirect_acceptance as live


def _write_fake_cli(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _version_script(text: str) -> str:
    return f"#!/bin/sh\nprintf '%s\\n' {text!r}\n"


class _ExplodingAdapter:
    """A real adapter with its `run` replaced by a tripwire.

    The live acceptance runner's central promise is that it costs nothing until
    someone opts in. The only way to check a promise about something *not*
    happening is to make it loud when it does. Everything except `run` is
    delegated to the genuine adapter so version resolution still behaves
    exactly as it does in production.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def run(self, **kwargs: object) -> object:
        raise AssertionError(
            "the live acceptance runner launched a harness turn it was not "
            "authorised to launch"
        )


def _acceptance(
    harness: str,
    *,
    version: str | None,
    statuses: dict[str, str],
) -> live.HarnessAcceptance:
    return live.HarnessAcceptance(
        harness=harness,  # type: ignore[arg-type]
        executable=f"/nowhere/{harness}",
        version=version,
        compatibility="supported",
        compatibility_reason="pinned",
        capabilities={},
        results=[
            live.ScenarioResult(key=key, status=status, detail="synthetic")
            for key, status in statuses.items()
        ],
        limitations=[],
        seconds=1.0,
    )
