"""Compose the shell's HTML and CSS fragments without a build step.

Includes occupy a complete line and resolve relative to the including file.
Both deployments use the same reader; release packaging already retains every
HTML and CSS source under ``static/``.
"""

from __future__ import annotations

import re
from pathlib import Path

_INCLUDE = re.compile(
    rb"^(?:<!-- include: ([\w./-]+) -->|/\* include: ([\w./-]+) \*/)(?:\r?\n|$)",
    re.MULTILINE,
)


def read_static_source(path: Path, *, root: Path | None = None) -> bytes:
    """Read an asset, expanding bounded, same-bundle HTML/CSS includes.

    Missing fragments raise OSError just like missing assets. Cycles and paths
    outside the bundle are invalid source rather than an invitation to read
    arbitrary files on the host.
    """
    boundary = (root if root is not None else path.parent).resolve()

    def read(candidate: Path, ancestors: tuple[Path, ...]) -> bytes:
        candidate = candidate.resolve()
        if not candidate.is_relative_to(boundary):
            raise OSError(f"static include escapes the bundle: {candidate.name}")
        if candidate in ancestors:
            raise OSError(f"cyclic static include: {candidate.name}")
        source = candidate.read_bytes()
        if candidate.suffix not in {".html", ".css"}:
            return source

        def include(match: re.Match[bytes]) -> bytes:
            relative = (match.group(1) or match.group(2)).decode("ascii")
            return read(candidate.parent / relative, (*ancestors, candidate))

        return _INCLUDE.sub(include, source)

    return read(path, ())
