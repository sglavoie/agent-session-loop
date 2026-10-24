"""Readiness and split-from manifest metadata."""

from __future__ import annotations

from pathlib import Path

from .literals import as_literal
from .plans_errors import (
    PlanError,
)
from .plans_files import (
    _atomic_write_text,
    _is_file,
    _raise_read_error,
    canonicalize_path,
)
from .plans_models import (
    READINESS_STATES,
    ReadinessMetadata,
    ReadinessState,
)
from .plans_transaction import (
    recover_pending_plan_transaction,
)

READINESS_PREFIX = "**Readiness**:"


READINESS_SHA_PREFIX = "**Readiness SHA-256**:"


READINESS_CHECKED_PREFIX = "**Readiness checked at**:"


#: Manifest metadata naming the document a plan was split from. Read by the
#: repository-intent gate to exempt that document — and the directory holding
#: it — wherever the plan happens to mention it. Interpolated by
#: :func:`authoring_manifest.render_manifest` so writer and reader cannot drift.
SPLIT_FROM_PREFIX = "**Split from**:"


def _strip_inline_code_ticks(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        return text[1:-1].strip()
    return text


def _leading_inline_code_or_token(value: str) -> str:
    """Return the first inline-code span, else the first whitespace token.

    :func:`_strip_inline_code_ticks` requires the *whole* value to be one span,
    which readiness metadata guarantees and hand-annotated provenance does not:
    ``` `plans/x.md` (harness plan store) ``` is the shape operators actually
    write. Only the location is wanted; the annotation beside it is prose.
    """
    text = value.strip()
    if text.startswith("`"):
        closing = text.find("`", 1)
        if closing != -1:
            return text[1:closing].strip()
    tokens = text.split()
    return tokens[0] if tokens else ""


def parse_readiness_state(value: str) -> ReadinessState:
    """Parse and validate a readiness state name."""
    return as_literal(
        value.strip().lower(), READINESS_STATES, "readiness state", error=PlanError
    )


def _is_readiness_line(line: str) -> bool:
    stripped = line.lstrip()
    return (
        stripped.startswith(READINESS_PREFIX)
        or stripped.startswith(READINESS_SHA_PREFIX)
        or stripped.startswith(READINESS_CHECKED_PREFIX)
    )


def parse_readiness_metadata(manifest_text: str) -> ReadinessMetadata | None:
    """Parse `**Readiness**`, `**Readiness SHA-256**`, `**Readiness checked at**` "
    "lines."""
    state: ReadinessState | None = None
    sha: str | None = None
    checked_at: str | None = None

    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if line.startswith(READINESS_SHA_PREFIX):
            payload = _strip_inline_code_ticks(line[len(READINESS_SHA_PREFIX) :])
            if not payload:
                raise PlanError("manifest readiness SHA-256 is empty")
            sha = payload
        elif line.startswith(READINESS_CHECKED_PREFIX):
            payload = _strip_inline_code_ticks(line[len(READINESS_CHECKED_PREFIX) :])
            if not payload:
                raise PlanError("manifest readiness checked-at timestamp is empty")
            checked_at = payload
        elif line.startswith(READINESS_PREFIX):
            payload = _strip_inline_code_ticks(line[len(READINESS_PREFIX) :])
            if not payload:
                raise PlanError("manifest readiness state is empty")
            state = parse_readiness_state(payload)

    if state is None and sha is None and checked_at is None:
        return None
    if state is None:
        raise PlanError("manifest readiness state missing")
    if sha is None:
        raise PlanError("manifest readiness SHA-256 missing")
    if checked_at is None:
        raise PlanError("manifest readiness checked-at timestamp missing")
    return ReadinessMetadata(state=state, readiness_sha256=sha, checked_at=checked_at)


def parse_split_from(manifest_text: str) -> str | None:
    """Parse the `**Split from**` pointer out of a manifest's header block.

    Scanning stops at the first `## ` heading — the same header-block boundary
    :func:`_find_readiness_insertion_index` uses — so a `**Split from**` line
    quoted inside a session narrative can never widen the exemption this value
    grants. The first match wins.

    The raw string is returned; expansion and canonicalization belong to the
    exemption layer, which is where the policy about what a pointer may exempt
    already lives. This never raises: readiness metadata is authoritative and
    must fail a malformed plan loudly, but provenance is advisory, so a value
    that cannot be parsed narrows the exemption instead of breaking the plan.
    """
    for raw_line in manifest_text.splitlines():
        if raw_line.startswith("## "):
            return None
        line = raw_line.strip()
        if line.startswith(("- ", "* ")):
            line = line[2:].lstrip()
        if not line.startswith(SPLIT_FROM_PREFIX):
            continue
        value = _leading_inline_code_or_token(line[len(SPLIT_FROM_PREFIX) :])
        return value or None
    return None


def read_readiness_metadata(plan_dir: str | Path) -> ReadinessMetadata | None:
    """Read readiness metadata from `manifest.md` in a plan directory."""
    recover_pending_plan_transaction(plan_dir)
    canonical_dir = canonicalize_path(plan_dir)
    manifest_path = canonical_dir / "manifest.md"
    if not _is_file(manifest_path):
        raise PlanError(f"missing manifest.md in plan directory: {canonical_dir}")
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as error:
        _raise_read_error(manifest_path, error)
    return parse_readiness_metadata(text)


def read_split_from(plan_dir: str | Path) -> str | None:
    """Read the `**Split from**` pointer from a plan directory's manifest.

    Returns ``None`` when the manifest is missing, unreadable, or carries no
    pointer. Unlike :func:`read_readiness_metadata` this deliberately does not
    call :func:`recover_pending_plan_transaction`: the per-turn packet gate is
    a caller, and it must neither take the transaction lock nor write. Failing
    to recover fails *closed* — a stale manifest yields no exemption, which is
    the stricter gate.
    """
    manifest_path = canonicalize_path(plan_dir) / "manifest.md"
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_split_from(text)


def _render_readiness_metadata_lines(metadata: ReadinessMetadata) -> list[str]:
    return [
        f"**Readiness**: {metadata.state}",
        f"**Readiness SHA-256**: `{metadata.readiness_sha256}`",
        f"**Readiness checked at**: {metadata.checked_at}",
    ]


def _find_readiness_insertion_index(lines: list[str]) -> int:
    h2_index = next(
        (index for index, line in enumerate(lines) if line.startswith("## ")),
        len(lines),
    )
    last_metadata = -1
    for index in range(h2_index):
        if lines[index].lstrip().startswith("**"):
            last_metadata = index
    if last_metadata == -1:
        return 0
    return last_metadata + 1


def replace_readiness_metadata_in_text(text: str, metadata: ReadinessMetadata) -> str:
    """Insert or replace readiness metadata lines in manifest text."""
    new_block = _render_readiness_metadata_lines(metadata)
    lines = text.splitlines(keepends=False)
    existing = [index for index, line in enumerate(lines) if _is_readiness_line(line)]

    if existing:
        insertion_index = existing[0]
        kept = [
            line for index, line in enumerate(lines) if not _is_readiness_line(line)
        ]
        result = kept[:insertion_index] + new_block + kept[insertion_index:]
    else:
        insertion_index = _find_readiness_insertion_index(lines)
        result = lines[:insertion_index] + new_block + lines[insertion_index:]

    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(result) + suffix


def write_readiness_metadata(plan_dir: str | Path, metadata: ReadinessMetadata) -> bool:
    """Write readiness metadata to `manifest.md`. Returns True if the file changed."""
    canonical_dir = canonicalize_path(plan_dir)
    manifest_path = canonical_dir / "manifest.md"
    if not _is_file(manifest_path):
        raise PlanError(f"missing manifest.md in plan directory: {canonical_dir}")
    try:
        original = manifest_path.read_text(encoding="utf-8")
    except OSError as error:
        _raise_read_error(manifest_path, error)
    updated = replace_readiness_metadata_in_text(original, metadata)
    if updated == original:
        return False
    _atomic_write_text(manifest_path, updated)
    return True
