"""Plan structure, manifest readiness, contract, and ambiguity sentinel checks."""

from __future__ import annotations

import re
from pathlib import Path

from . import (
    workspaces,
)
from .contracts import (
    COMPLETION_CONTRACT_HEADING,
    CompletionContract,
    parse_completion_contract,
)
from .plans_content_hash import stable_readiness_sha256
from .plans_discovery import noncanonical_session_file_suggestions
from .plans_errors import PlanError
from .plans_handoff import HANDOFF_HEADING
from .plans_models import PlanSnapshot, ReadinessMetadata, SessionFile
from .plans_readiness import parse_readiness_metadata
from .plans_session_scope import SessionScopeError
from .plans_snapshot import inspect_plan
from .preflight_models import (
    PLAN_DIRECTORY_MISSING_MESSAGE,
    PreflightCategory,
    PreflightFinding,
    _finding,
)

WORD_AMBIGUITY_SENTINELS: tuple[str, ...] = (
    "TBD",
    "TODO",
    "FIXME",
    "XXX",
)


LITERAL_AMBIGUITY_SENTINELS: tuple[str, ...] = (
    "<placeholder>",
    "<fill this in>",
    "<fill in>",
)


AMBIGUITY_SENTINELS: tuple[str, ...] = (
    *WORD_AMBIGUITY_SENTINELS,
    *LITERAL_AMBIGUITY_SENTINELS,
)


WORD_AMBIGUITY_SENTINEL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])("
    + "|".join(re.escape(sentinel) for sentinel in WORD_AMBIGUITY_SENTINELS)
    + r")(?![A-Za-z0-9_-])"
)


def _final_handoff_start(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == HANDOFF_HEADING:
            return index
    return None


def _read_session_text(session: SessionFile) -> str | None:
    try:
        return session.path.read_text(encoding="utf-8")
    except OSError:
        return None


def _check_plan_structure(
    canonical_dir: Path,
    repository_registry: workspaces.RepositoryRegistry | None = None,
) -> tuple[tuple[PreflightFinding, ...], PlanSnapshot | None]:
    findings: list[PreflightFinding] = []

    if not canonical_dir.exists():
        findings.append(
            _finding(
                "red",
                "plan-structure",
                f"{PLAN_DIRECTORY_MISSING_MESSAGE}: {canonical_dir}",
                path=str(canonical_dir),
            )
        )
        return tuple(findings), None

    if not canonical_dir.is_dir():
        findings.append(
            _finding(
                "red",
                "plan-structure",
                f"plan path is not a directory: {canonical_dir}",
                path=str(canonical_dir),
            )
        )
        return tuple(findings), None

    try:
        snapshot = inspect_plan(
            canonical_dir,
            repository_registry=repository_registry,
        )
    except SessionScopeError as error:
        findings.append(
            _finding(
                "red",
                "repository-scope",
                error.reason,
                session_id=error.session_id,
                path=str(error.path or canonical_dir),
                line=error.line_number,
                detail=error.source_line,
            )
        )
        return tuple(findings), None
    except PlanError as error:
        findings.append(
            _finding(
                "red",
                "plan-structure",
                str(error),
                path=str(canonical_dir),
            )
        )
        return tuple(findings), None

    return tuple(findings), snapshot


def _check_canonical_session_ids(canonical_dir: Path) -> tuple[PreflightFinding, ...]:
    try:
        suggestions = noncanonical_session_file_suggestions(canonical_dir)
    except PlanError as error:
        return (
            _finding(
                "red",
                "canonical-session-id",
                f"cannot inspect session filenames: {error}",
                path=str(canonical_dir),
            ),
        )

    findings: list[PreflightFinding] = []
    for source, target in suggestions:
        findings.append(
            _finding(
                "red",
                "canonical-session-id",
                f"non-canonical session filename: {source}",
                path=str(canonical_dir / source),
                detail=f"rename to {target}",
            )
        )
    return tuple(findings)


def _sessions_for_stable_hash(
    snapshot: PlanSnapshot,
) -> tuple[SessionFile, ...]:
    return tuple(status.session for status in snapshot.sessions)


def _check_manifest_readiness_block(
    snapshot: PlanSnapshot,
) -> tuple[tuple[PreflightFinding, ...], ReadinessMetadata | None, str]:
    sessions = _sessions_for_stable_hash(snapshot)
    expected_hash = stable_readiness_sha256(snapshot.plan_dir, sessions)

    try:
        manifest_text = snapshot.manifest_path.read_text(encoding="utf-8")
    except OSError as error:
        finding = _finding(
            "red",
            "manifest-readiness",
            f"cannot read manifest.md: {error}",
            path=str(snapshot.manifest_path),
        )
        return (finding,), None, expected_hash

    try:
        readiness = parse_readiness_metadata(manifest_text)
    except PlanError as error:
        finding = _finding(
            "red",
            "manifest-readiness",
            f"manifest readiness metadata is malformed: {error}",
            path=str(snapshot.manifest_path),
        )
        return (finding,), None, expected_hash

    findings: list[PreflightFinding] = []

    if readiness is None:
        findings.append(
            _finding(
                "red",
                "manifest-readiness",
                "manifest is missing readiness metadata",
                path=str(snapshot.manifest_path),
                detail=(
                    "add **Readiness**, **Readiness SHA-256**, and "
                    "**Readiness checked at** lines"
                ),
            )
        )
        return tuple(findings), None, expected_hash

    if readiness.readiness_sha256 != expected_hash:
        findings.append(
            _finding(
                "red",
                "manifest-readiness",
                "manifest readiness SHA-256 does not match current plan content",
                path=str(snapshot.manifest_path),
                detail=(
                    f"recorded {readiness.readiness_sha256}; expected {expected_hash}"
                ),
            )
        )

    if readiness.state in {"draft", "reviewed"}:
        findings.append(
            _finding(
                "yellow",
                "manifest-readiness",
                f"plan readiness state is {readiness.state!r}",
                path=str(snapshot.manifest_path),
                detail="run preflight to advance to loop-ready when checks pass",
            )
        )

    return tuple(findings), readiness, expected_hash


def _instructions_range(lines: list[str]) -> tuple[int, int]:
    handoff_start = _final_handoff_start(lines)
    end = handoff_start if handoff_start is not None else len(lines)
    return 0, end


def _check_session_contracts(
    snapshot: PlanSnapshot,
    repository_registry: workspaces.RepositoryRegistry | None = None,
) -> tuple[tuple[PreflightFinding, ...], dict[str, CompletionContract]]:
    findings: list[PreflightFinding] = []
    contracts: dict[str, CompletionContract] = {}

    for status in snapshot.sessions:
        session = status.session
        text = _read_session_text(session)
        if text is None:
            findings.append(
                _finding(
                    "red",
                    "missing-contract",
                    f"cannot read session file: {session.path}",
                    session_id=session.id,
                    path=str(session.path),
                )
            )
            continue

        try:
            contract = parse_completion_contract(
                text,
                session_id=session.session_id,
                repository_registry=repository_registry,
            )
        except PlanError as error:
            category: PreflightCategory = (
                "repository-contract"
                if repository_registry is not None
                else "malformed-contract"
            )
            findings.append(
                _finding(
                    "red",
                    category,
                    f"session {session.id} completion contract is malformed: {error}",
                    session_id=session.id,
                    path=str(session.path),
                )
            )
            continue

        if contract is None:
            findings.append(
                _finding(
                    "red",
                    "missing-contract",
                    f"session {session.id} is missing a {COMPLETION_CONTRACT_HEADING} "
                    f"section",
                    session_id=session.id,
                    path=str(session.path),
                    detail=(
                        "add Done when bullets and a fenced ```json``` Verify recipe"
                    ),
                )
            )
            continue

        contracts[session.id] = contract

    return tuple(findings), contracts


def _check_ambiguity_sentinels(
    snapshot: PlanSnapshot,
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    for status in snapshot.sessions:
        session = status.session
        text = _read_session_text(session)
        if text is None:
            continue
        lines = text.splitlines()
        start, end = _instructions_range(lines)
        for line_index in range(start, end):
            line = lines[line_index]
            sentinel = _matching_ambiguity_sentinel(line)
            if sentinel is not None:
                findings.append(
                    _finding(
                        "yellow",
                        "ambiguity-sentinel",
                        f"session {session.id} contains ambiguity sentinel "
                        f"{sentinel!r}",
                        session_id=session.id,
                        path=str(session.path),
                        line=line_index + 1,
                        detail=line.strip()[:200],
                    )
                )

    return tuple(findings)


def _matching_ambiguity_sentinel(line: str) -> str | None:
    # A word sentinel (TODO/FIXME/...) is only an unresolved *placeholder* when it
    # LEADS a clause as a marker ("TODO: revisit", "- FIXME me later"). Cleanup
    # sessions legitimately *reference* code anchors mid-sentence ("replace the bare
    # `FIXME`", "the TODO in the reset route", "Anchor/FIXME cleanups"); those are not
    # ambiguity and must not block loop-ready. We treat a match as a reference (skip)
    # when it sits inside inline code (backticks) or anywhere but a marker position.
    code_spans = [(m.start(), m.end()) for m in re.finditer(r"`[^`]*`", line)]
    for word_match in WORD_AMBIGUITY_SENTINEL_PATTERN.finditer(line):
        start = word_match.start()
        if any(span_start <= start < span_end for span_start, span_end in code_spans):
            continue
        if _is_sentinel_marker_position(line[:start]):
            return word_match.group(1)

    for sentinel in LITERAL_AMBIGUITY_SENTINELS:
        if sentinel in line:
            return sentinel

    return None


# Characters that form structural markdown prefixes (list bullets, headings,
# blockquotes, ordered-list numbers, emphasis runs). A sentinel preceded only by
# these leads its line and is therefore a marker rather than a reference.
_SENTINEL_MARKER_PREFIX = re.compile(r"[\s>#*_`+.)\-0-9]*")


_SENTINEL_CLAUSE_TERMINATORS = ".:;!?"


def _is_sentinel_marker_position(before: str) -> bool:
    stripped = before.rstrip()
    if stripped == "":
        return True
    # Only structural prefix chars precede the sentinel -> it leads the line.
    if _SENTINEL_MARKER_PREFIX.fullmatch(stripped) is not None:
        return True
    # Otherwise it is a marker only when it opens a fresh clause after a terminator.
    return stripped[-1] in _SENTINEL_CLAUSE_TERMINATORS
