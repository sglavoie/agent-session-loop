"""Pure path-target extraction for repository-footprint detection.

:func:`extract_path_targets` and :func:`targets_from_checks` are pure. They
find path-shaped values and classify each as ``hard`` (an absolute path, a
``~/`` path, a qualified ``Alias:path`` handoff entry, or a contract target)
or ``soft`` (bare folder-shaped prose). Filesystem resolution lives in
:mod:`repository_scope`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from .contract_models import fenced_line_flags
from .contracts import (
    CommandCheck,
    CompletionContract,
    FileExistsCheck,
    RegexInFileCheck,
    VerificationCheck,
)

TargetKind = Literal["contract-path", "contract-cwd", "handoff", "prose"]
TargetConfidence = Literal["hard", "soft"]

_PATH_SEGMENT = r"[A-Za-z0-9._+@%-]+"
_ABSOLUTE_PATH_RE = re.compile(
    rf"(?<![\w./~-])(?:~/|/)(?:{_PATH_SEGMENT}/)*{_PATH_SEGMENT}/?"
)
_HANDOFF_RE = re.compile(
    rf"(?<![\w./:-])([A-Za-z][A-Za-z0-9_-]*):(?!/)((?:{_PATH_SEGMENT}/)*{_PATH_SEGMENT})"
)
_RELATIVE_PATH_RE = re.compile(rf"(?<![\w./~:-])(?:{_PATH_SEGMENT}/)+{_PATH_SEGMENT}/?")
#: Sentence punctuation the segment grammar swallows when a path ends a clause.
_TRAILING_PUNCTUATION = ".,;:!?)]}'\"`"


@dataclass(frozen=True)
class PathTarget:
    """One path-shaped value found in a plan, with where it came from."""

    value: str
    kind: TargetKind
    line: int
    confidence: TargetConfidence

    def as_dict(self) -> dict[str, object]:
        return {
            "line": self.line,
            "text": self.value,
            "kind": self.kind,
            "confidence": self.confidence,
        }


def extract_path_targets(text: str) -> tuple[PathTarget, ...]:
    """Extract path-shaped values from plan prose, skipping fenced blocks.

    Fenced blocks hold illustrative snippets, so a path there is not evidence.
    Real contract targets never depend on this scan; they arrive through
    :func:`targets_from_checks`.
    """
    targets: list[PathTarget] = []
    lines = text.splitlines()
    # The authoring parser's fence rule, so a quoted ``` example inside a
    # longer fence neither closes it nor turns its snippet into evidence.
    for index, (line, fenced) in enumerate(
        zip(lines, fenced_line_flags(lines), strict=True), start=1
    ):
        if fenced:
            continue
        targets.extend(_extract_line_targets(line, index))
    return tuple(targets)


def _extract_line_targets(line: str, line_number: int) -> list[PathTarget]:
    found: list[PathTarget] = []
    consumed: list[tuple[int, int]] = []

    for match in _ABSOLUTE_PATH_RE.finditer(line):
        value = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        if not _is_path_shaped(value):
            continue
        consumed.append(match.span())
        found.append(
            PathTarget(value=value, kind="prose", line=line_number, confidence="hard")
        )

    for match in _HANDOFF_RE.finditer(line):
        if _overlaps(match.span(), consumed):
            continue
        consumed.append(match.span())
        found.append(
            PathTarget(
                value=match.group(0).rstrip(_TRAILING_PUNCTUATION),
                kind="handoff",
                line=line_number,
                confidence="hard",
            )
        )

    for match in _RELATIVE_PATH_RE.finditer(line):
        if _overlaps(match.span(), consumed):
            continue
        value = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        if "/" not in value:
            continue
        found.append(
            PathTarget(
                value=value,
                kind="prose",
                line=line_number,
                confidence="soft",
            )
        )

    return found


def _overlaps(span: tuple[int, int], consumed: Sequence[tuple[int, int]]) -> bool:
    start, end = span
    return any(
        start < other_end and other_start < end for other_start, other_end in consumed
    )


def _is_path_shaped(value: str) -> bool:
    """Reject a bare root-level token such as ``/opt`` or a lone ``~/``."""
    trimmed = value.rstrip("/")
    if trimmed.startswith("~"):
        return len(trimmed) > 2
    return trimmed.count("/") >= 2


def targets_from_checks(
    checks: Iterable[VerificationCheck], *, line: int = 0
) -> tuple[PathTarget, ...]:
    """Turn contract check locations into hard targets.

    A contract target is the strongest available evidence: it is a location
    the session is required to produce or inspect, not a passing mention.
    """
    targets: list[PathTarget] = []
    for check in checks:
        if isinstance(check, CommandCheck):
            if check.cwd is not None:
                targets.append(
                    PathTarget(
                        value=check.cwd,
                        kind="contract-cwd",
                        line=line,
                        confidence="hard",
                    )
                )
        elif isinstance(check, (FileExistsCheck, RegexInFileCheck)):
            targets.append(
                PathTarget(
                    value=check.path,
                    kind="contract-path",
                    line=line,
                    confidence="hard",
                )
            )
    return tuple(targets)


def targets_from_contract(contract: CompletionContract) -> tuple[PathTarget, ...]:
    return targets_from_checks(contract.verify.checks)
