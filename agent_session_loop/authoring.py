"""Central plan authoring and transaction API.

This module centralizes the deterministic construction and mutation of split
plan directories. Previously the rendering rules lived only in the
``session-split`` / ``session-resplit`` skill markdown (executed by an LLM) and
the collision-safe path helpers lived only in standalone scripts. This module
turns all of that into importable, unit-tested Python:

- **Inline-source capture and output resolution** — importable equivalents of
  ``capture-inline-plan.py`` and ``resolve-plan-output.py``. The scripts remain
  as thin wrappers (migrated in a later session); the canonical logic lives
  here so callers get one deterministic implementation.
- **Fenced-code-aware session parsing** — split a plan document into shared
  context plus per-session blocks, honoring ```` ``` ````/``~~~`` fences so a
  ``## Session`` line inside a code block is never mistaken for structure.
- **Rendering** — deterministic ``context.md``, ``session-<id>.md``,
  ``manifest.md``, and ``progress.md`` output. Original session content is
  preserved verbatim except for standalone heading-level normalization.
- **Deterministic preparation mode** — session rendering may require an explicit
  parseable ``## Completion Contract`` and fails closed otherwise.
- **Atomic multi-file transactions** — stage many files and commit them so a
  failure part-way through leaves no partially authored plan.
- **Manifest table / previous-session / archive / readiness helpers** — the
  small structural mutations shared by split and resplit.

Canonical session identities come from :mod:`agent_session_loop.plans`
(``SessionId``); contract parsing/validation comes from
:mod:`agent_session_loop.contracts`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from . import contracts as _contracts
from . import plans
from .plans import (
    PlanError,
    ReadinessMetadata,
    SessionId,
    canonicalize_path,
    parse_session_id,
    stable_readiness_sha256,
    write_readiness_metadata,
)


DEFAULT_AGENT_PLANS_DIR_NAME = "agent-plans"
HASH_PREFIX_LENGTH = 12

#: Matches ``## Session 1: Title``/``### Session 2.1`` outside code fences.
SESSION_HEADING_RE = re.compile(
    rf"^(#{{2,3}})\s+Session\s+({plans.SESSION_ID_PATTERN})\b\s*(?::\s*(.*?))?\s*$",
    re.IGNORECASE,
)
#: Matches an opening/closing fenced-code delimiter (``` or ~~~, 3+ chars).
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

CONTEXT_REFERENCE_LINE = (
    "> **Before starting**: Read `context.md` in this directory for shared "
    "project context."
)
PREVIOUS_SESSION_REFERENCE_TEMPLATE = (
    "> **Previous session**: Read the **Handoff** section at the bottom of "
    "`{filename}`."
)
HANDOFF_TEMPLATE = (
    "## Handoff\n"
    "\n"
    "<!-- Fill this section when the session is complete -->\n"
    "\n"
    "- **Status**: pending\n"
    "- **Files modified**: (none yet)\n"
    "- **Key decisions**: (none yet)\n"
    "- **Blockers for next session**: (none)\n"
    "- **Notes**:\n"
)
EMPTY_CONTEXT_TEMPLATE = (
    "# Context\n"
    "\n"
    "_No shared context section found in the original plan._\n"
    "\n"
    "**Original plan**: `{source_path}`\n"
)
ARCHIVE_ROOT_NAME = "resplit-archive"


# ---------------------------------------------------------------------------
# Inline-source capture and output resolution (importable APIs)
# ---------------------------------------------------------------------------


def default_base_dir() -> str:
    """Return the persistent agent-plans base directory.

    Resolution order mirrors the standalone scripts: ``AGENT_PLANS_DIR`` wins,
    then ``$XDG_DATA_HOME/agent-plans``, then ``~/.local/share/agent-plans``.
    ``$TMPDIR`` is intentionally never consulted (macOS purges it on reboot).
    """
    agent_plans_dir = os.environ.get("AGENT_PLANS_DIR")
    if agent_plans_dir:
        return os.path.realpath(os.path.expanduser(agent_plans_dir))

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        base = os.path.join(
            os.path.expanduser(xdg_data_home), DEFAULT_AGENT_PLANS_DIR_NAME
        )
    else:
        base = os.path.join(
            os.path.expanduser("~/.local/share"), DEFAULT_AGENT_PLANS_DIR_NAME
        )
    return os.path.realpath(base)


def _resolve_base_dir(base_dir: str | os.PathLike[str] | None) -> str:
    if base_dir is None:
        return default_base_dir()
    return os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(base_dir))))


def slugify(value: str) -> str:
    """Collapse a string to a filesystem-safe ``[a-z0-9-]`` slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "plan"


def slug_from_source_path(source_path: str | os.PathLike[str]) -> str:
    """Build a slug from a plan filename stem."""
    filename = os.path.basename(os.fspath(source_path))
    stem, _ext = os.path.splitext(filename)
    return slugify(stem or filename)


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def sha256_file(source_path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(source_path, "rb") as plan_file:
        for chunk in iter(lambda: plan_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(source_path: str | os.PathLike[str], content_hash: str) -> str:
    """Fingerprint a plan by canonical source path plus content hash."""
    canonical_path = os.path.realpath(os.path.abspath(os.fspath(source_path)))
    fingerprint_input = canonical_path.encode("utf-8", "surrogateescape")
    fingerprint_input += b"\0"
    fingerprint_input += content_hash.encode("ascii")
    return hashlib.sha256(fingerprint_input).hexdigest()


def extract_inline_plan(raw_input: str) -> str:
    """Return the last ``<proposed_plan>`` block, else the raw input.

    Raises :class:`ValueError` when no non-empty plan content is present.
    """
    matches = re.findall(
        r"<proposed_plan>\s*(.*?)\s*</proposed_plan>",
        raw_input,
        flags=re.IGNORECASE | re.DOTALL,
    )
    content = (matches[-1] if matches else raw_input).strip()
    if not content:
        raise ValueError("no plan content found on stdin")
    return content + "\n"


def title_from_plan(content: str) -> str:
    """Return the first ``# H1`` title in a plan, or ``"plan"``."""
    for line in content.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1)
    return "plan"


def _write_source_file(
    content: str, sources_dir: str, slug: str, content_hash: str
) -> tuple[str, bool]:
    os.makedirs(sources_dir, exist_ok=True)

    hash_prefix = content_hash[:HASH_PREFIX_LENGTH]
    collision_index = 1
    while True:
        suffix = "" if collision_index == 1 else f"-{collision_index}"
        path = os.path.join(sources_dir, f"{slug}-{hash_prefix}{suffix}.md")
        try:
            with open(path, "x", encoding="utf-8") as source_file:
                source_file.write(content)
            return path, True
        except FileExistsError:
            with open(path, encoding="utf-8") as source_file:
                if source_file.read() == content:
                    return path, False
            collision_index += 1


def capture_inline_plan(
    raw_input: str, base_dir: str | os.PathLike[str] | None = None
) -> dict[str, object]:
    """Persist pasted plan markdown as a durable source file.

    Importable equivalent of ``capture-inline-plan.py``; returns the same
    JSON-serializable keys so the script can become a thin wrapper.
    """
    selected_base_dir = _resolve_base_dir(base_dir)
    sources_dir = os.path.join(selected_base_dir, "sources")
    content = extract_inline_plan(raw_input)
    title = title_from_plan(content)
    slug = slugify(title)
    content_hash = sha256_text(content)
    source_path, created = _write_source_file(content, sources_dir, slug, content_hash)

    return {
        "base_dir": selected_base_dir,
        "sources_dir": sources_dir,
        "source_path": os.path.realpath(source_path),
        "title": title,
        "slug": slug,
        "content_sha256": content_hash,
        "hash_prefix": content_hash[:HASH_PREFIX_LENGTH],
        "created": created,
    }


def _create_output_dir(base_dir: str, plan_id: str) -> tuple[str, int]:
    os.makedirs(base_dir, exist_ok=True)

    collision_index = 1
    while True:
        suffix = "" if collision_index == 1 else f"-{collision_index}"
        candidate = os.path.join(base_dir, f"{plan_id}{suffix}")
        try:
            os.mkdir(candidate)
        except FileExistsError:
            collision_index += 1
            continue
        return candidate, collision_index


def resolve_plan_output(
    plan_file: str | os.PathLike[str],
    base_dir: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Create a fresh collision-safe output directory for a split plan.

    Importable equivalent of ``resolve-plan-output.py``; returns the same
    JSON-serializable keys so the script can become a thin wrapper.
    """
    canonical_source = os.path.realpath(
        os.path.abspath(os.path.expanduser(os.fspath(plan_file)))
    )
    selected_base_dir = _resolve_base_dir(base_dir)
    slug = slug_from_source_path(canonical_source)
    content_hash = sha256_file(canonical_source)
    fingerprint = source_fingerprint(canonical_source, content_hash)
    content_hash_prefix = content_hash[:HASH_PREFIX_LENGTH]
    fingerprint_prefix = fingerprint[:HASH_PREFIX_LENGTH]
    plan_id = f"{content_hash_prefix}-{fingerprint_prefix}"
    output_dir, collision_index = _create_output_dir(selected_base_dir, plan_id)

    return {
        "output_dir": output_dir,
        "base_dir": selected_base_dir,
        "slug": slug,
        "source_path": canonical_source,
        "content_sha256": content_hash,
        "source_fingerprint": fingerprint,
        "content_hash_prefix": content_hash_prefix,
        "hash_prefix": fingerprint_prefix,
        "fingerprint_prefix": fingerprint_prefix,
        "plan_id": plan_id,
        "collision_index": collision_index,
    }


# ---------------------------------------------------------------------------
# Fenced-code-aware session parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedSession:
    """One session block parsed from a plan document.

    ``lines`` includes every source line from the session heading (exclusive of
    the heading itself) up to the next session heading or end of file, verbatim.
    ``heading_level`` is 2 for ``## Session`` and 3 for ``### Session``.
    """

    session_id: SessionId
    title: str | None
    heading_level: int
    lines: tuple[str, ...]

    @property
    def instruction_lines(self) -> tuple[str, ...]:
        """Body lines with leading/trailing blank lines and ``---`` rules trimmed."""
        return tuple(_trim_body(list(self.lines)))


@dataclass(frozen=True)
class ParsedPlan:
    """A plan document parsed into shared context plus ordered sessions."""

    context_lines: tuple[str, ...]
    sessions: tuple[ParsedSession, ...]

    @property
    def session_ids(self) -> tuple[str, ...]:
        return tuple(session.session_id.value for session in self.sessions)


def _fence_delimiter(line: str) -> str | None:
    match = _FENCE_RE.match(line)
    if match is None:
        return None
    return match.group(1)[0] * 3  # normalize to the 3-char family marker


def parse_plan_document(text: str) -> ParsedPlan:
    """Split a plan into shared context and per-session blocks (fence-aware).

    Session headings (`## Session <id>` / `### Session <id>`) found inside a
    fenced code block are treated as content, never as structure. Duplicate
    session IDs raise :class:`PlanError`.
    """
    lines = text.splitlines()
    context: list[str] = []
    sessions: list[ParsedSession] = []

    current: dict | None = None
    fence: str | None = None
    seen_ids: set[str] = set()

    def _flush(end: int) -> None:
        if current is None:
            return
        body = lines[current["body_start"] : end]
        sessions.append(
            ParsedSession(
                session_id=current["session_id"],
                title=current["title"],
                heading_level=current["heading_level"],
                lines=tuple(body),
            )
        )

    for index, line in enumerate(lines):
        delimiter = _fence_delimiter(line)
        if delimiter is not None:
            if fence is None:
                fence = delimiter
            elif fence == delimiter:
                fence = None
            # A different fence family inside an open fence is content; ignore.

        heading = None if fence is not None else SESSION_HEADING_RE.match(line)
        if heading is None:
            if current is None:
                context.append(line)
            continue

        _flush(index)
        session_id = parse_session_id(heading.group(2))
        if session_id.value in seen_ids:
            raise PlanError(
                f"duplicate session heading for id {session_id.value!r}"
            )
        seen_ids.add(session_id.value)
        raw_title = (heading.group(3) or "").strip()
        current = {
            "session_id": session_id,
            "title": raw_title or None,
            "heading_level": len(heading.group(1)),
            "body_start": index + 1,
        }

    _flush(len(lines))

    return ParsedPlan(
        context_lines=tuple(context),
        sessions=tuple(sessions),
    )


# ---------------------------------------------------------------------------
# Heading normalization
# ---------------------------------------------------------------------------


def _trim_body(lines: list[str]) -> list[str]:
    """Drop leading/trailing blank lines and surrounding ``---`` rules."""
    start = 0
    end = len(lines)
    while start < end and (lines[start].strip() == "" or lines[start].strip() == "---"):
        start += 1
    while end > start and (
        lines[end - 1].strip() == "" or lines[end - 1].strip() == "---"
    ):
        end -= 1
    return lines[start:end]


def normalize_heading_levels(lines: list[str], heading_level: int) -> list[str]:
    """Promote body headings so a standalone session file reads naturally.

    When the original session heading was ``###`` (or deeper), every heading in
    the body is shifted so the session's own level maps to ``#`` (the rendered
    title). A ``###`` session therefore turns a ``####`` sub-heading into ``##``.
    Level-2 (``## Session``) bodies are preserved verbatim. Headings inside
    fenced code blocks are never rewritten.
    """
    if heading_level <= 2:
        return list(lines)

    shift = 1 - heading_level  # e.g. level 3 -> -2
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        delimiter = _fence_delimiter(line)
        if delimiter is not None:
            if fence is None:
                fence = delimiter
            elif fence == delimiter:
                fence = None
            out.append(line)
            continue
        if fence is None:
            level = _heading_level(line)
            if level > 0:
                new_level = max(1, level + shift)
                stripped = line.lstrip()
                leading = line[: len(line) - len(stripped)]
                out.append(f"{leading}{'#' * new_level}{stripped[level:]}")
                continue
        out.append(line)
    return out


def _heading_level(line: str) -> int:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return 0
    level = 0
    while level < len(stripped) and stripped[level] == "#":
        level += 1
    if level == 0 or level == len(stripped) or stripped[level] != " ":
        return 0
    return level


# ---------------------------------------------------------------------------
# Completion-contract rendering
# ---------------------------------------------------------------------------


def _check_to_json(check: _contracts.VerificationCheck) -> dict[str, object]:
    payload: dict[str, object] = {"name": check.name, "type": check.kind}
    if isinstance(check, _contracts.CommandCheck):
        payload["argv"] = list(check.argv)
        payload["timeout_seconds"] = _render_number(check.timeout_seconds)
        if check.cwd is not None:
            payload["cwd"] = check.cwd
    elif isinstance(check, _contracts.FileExistsCheck):
        payload["path"] = check.path
    elif isinstance(check, _contracts.RegexInFileCheck):
        payload["path"] = check.path
        payload["pattern"] = check.pattern
    if check.baseline_expectation != _contracts.DEFAULT_BASELINE_EXPECTATION:
        payload["baseline_expectation"] = check.baseline_expectation
    return payload


def _render_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def contract_from_payload(
    session_id: int | str | SessionId,
    done_when: tuple[str, ...] | list[str],
    verify_payload: dict,
) -> _contracts.CompletionContract:
    """Build a validated :class:`CompletionContract` from JSON-shaped inputs.

    ``verify_payload`` uses the same shape as a contract's fenced ``Verify``
    JSON (``{"session": ..., "checks": [...]}``; the ``session`` key is
    optional and must match when present). Raises :class:`PlanError` on any
    malformed input, mirroring the markdown contract parser.
    """
    canonical_id = parse_session_id(session_id)
    bullets = tuple(str(item).strip() for item in done_when if str(item).strip())
    if not bullets:
        raise PlanError(
            f"session {canonical_id.value} contract requires at least one "
            "'Done when' bullet"
        )
    recipe = _contracts._parse_verify_payload(verify_payload, session_id=canonical_id)
    return _contracts.CompletionContract(
        session_id=canonical_id, done_when=bullets, verify=recipe
    )


def render_completion_contract(contract: _contracts.CompletionContract) -> str:
    """Render a ``## Completion Contract`` block that round-trips through the parser."""
    lines: list[str] = [
        _contracts.COMPLETION_CONTRACT_HEADING,
        "",
        _contracts.DONE_WHEN_HEADING,
        "",
    ]
    if contract.done_when:
        lines.extend(f"- {item}" for item in contract.done_when)
    else:
        raise PlanError("completion contract requires at least one 'Done when' bullet")
    lines.extend(["", _contracts.VERIFY_HEADING, "", "```json"])

    payload = {
        "session": contract.session_id.value,
        "checks": [_check_to_json(check) for check in contract.verify.checks],
    }
    lines.extend(json.dumps(payload, indent=2).splitlines())
    lines.append("```")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# File rendering
# ---------------------------------------------------------------------------


def context_reference_line() -> str:
    return CONTEXT_REFERENCE_LINE


def previous_session_reference_line(previous_session_id: int | str | SessionId) -> str:
    """Render the ``> **Previous session**`` handoff-reference line."""
    filename = parse_session_id(previous_session_id).filename
    return PREVIOUS_SESSION_REFERENCE_TEMPLATE.format(filename=filename)


def render_context(
    context_lines: tuple[str, ...] | list[str],
    source_path: str | os.PathLike[str],
) -> str:
    """Render ``context.md`` from the plan's shared-context lines."""
    trimmed = _trim_leading_trailing_blanks(list(context_lines))
    if not trimmed:
        return EMPTY_CONTEXT_TEMPLATE.format(source_path=os.fspath(source_path))
    return "\n".join(trimmed) + "\n"


def _trim_leading_trailing_blanks(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and lines[start].strip() == "":
        start += 1
    while end > start and lines[end - 1].strip() == "":
        end -= 1
    return lines[start:end]


def render_session_file(
    *,
    session_id: int | str | SessionId,
    title: str | None,
    instruction_lines: tuple[str, ...] | list[str],
    heading_level: int = 2,
    is_first: bool = False,
    previous_session_id: int | str | SessionId | None = None,
    contract: _contracts.CompletionContract | None = None,
    require_contract: bool = True,
) -> str:
    """Render a full ``session-<id>.md`` file.

    Original instruction content is preserved verbatim except for standalone
    heading-level normalization (see :func:`normalize_heading_levels`). When
    ``require_contract`` is set (deterministic preparation mode), a parseable
    ``contract`` is mandatory and its absence fails closed.
    """
    canonical_id = parse_session_id(session_id)
    if require_contract and contract is None:
        raise PlanError(
            f"session {canonical_id.value} requires an explicit completion contract"
        )
    if contract is not None and contract.session_id != canonical_id:
        raise PlanError(
            "contract session id "
            f"{contract.session_id.value!r} does not match session "
            f"{canonical_id.value!r}"
        )

    title_text = (title or "").strip()
    heading = (
        f"# {canonical_id.display_label}: {title_text}"
        if title_text
        else f"# {canonical_id.display_label}"
    )

    parts: list[str] = [heading, "", CONTEXT_REFERENCE_LINE]
    if not is_first and previous_session_id is not None:
        parts.append(previous_session_reference_line(previous_session_id))
    parts.append("")
    parts.append("---")
    parts.append("")

    body = normalize_heading_levels(
        _trim_body(list(instruction_lines)), heading_level
    )
    parts.extend(body)
    parts.append("")
    parts.append("---")
    parts.append("")

    if contract is not None:
        parts.append(render_completion_contract(contract).rstrip("\n"))
        parts.append("")
        parts.append("---")
        parts.append("")

    parts.append(HANDOFF_TEMPLATE.rstrip("\n"))
    return "\n".join(parts) + "\n"


def render_progress_skeleton() -> str:
    """Return the header-only ``progress.md`` skeleton for a fresh plan."""
    return plans.PROGRESS_HEADER


# ---------------------------------------------------------------------------
# Manifest rendering and table updates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestSession:
    """One row in a manifest ``## Sessions`` table."""

    session_id: SessionId
    title: str
    status: plans.StatusValue = "pending"

    @property
    def filename(self) -> str:
        return self.session_id.filename


def render_manifest_session_row(session: ManifestSession) -> str:
    return (
        f"| {session.session_id.value} | {session.title} | "
        f"{session.status} | {session.filename} |"
    )


def render_manifest_sessions_table(sessions: tuple[ManifestSession, ...]) -> str:
    lines = [
        "| # | Title | Status | File |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(render_manifest_session_row(session) for session in sessions)
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ManifestSpec:
    """Inputs for deterministic ``manifest.md`` rendering."""

    plan_title: str
    source_path: str
    output_dir: str
    plan_id: str
    source_fingerprint: str
    content_sha256: str
    sessions: tuple[ManifestSession, ...]
    split_date: str
    readiness: ReadinessMetadata
    skill_prefix: str = "/"
    execution_note: str | None = None


def render_manifest(spec: ManifestSpec) -> str:
    """Render a serial-only ``manifest.md`` including readiness metadata lines."""
    prefix = spec.skill_prefix
    run_command = f"{prefix}session-run {spec.output_dir}/"
    lines: list[str] = [
        f"# {spec.plan_title}",
        "",
        f"**Split from**: `{spec.source_path}`",
        f"**Output directory**: `{spec.output_dir}`",
        f"**Plan ID**: `{spec.plan_id}`",
        f"**Source fingerprint**: `{spec.source_fingerprint}`",
        f"**Content SHA-256**: `{spec.content_sha256}`",
        f"**Sessions**: {len(spec.sessions)}",
        f"**Split date**: {spec.split_date}",
        f"**Readiness**: {spec.readiness.state}",
        f"**Readiness SHA-256**: `{spec.readiness.readiness_sha256}`",
        f"**Readiness checked at**: {spec.readiness.checked_at}",
        "",
        "## Sessions",
        "",
        render_manifest_sessions_table(spec.sessions).rstrip("\n"),
        "",
        "## Execution",
        "",
        (
            f"Run `{prefix}session-run` once per session, preferably in a fresh "
            "harness conversation for each:"
        ),
        "",
        "```text",
        run_command,
        "```",
        "",
        f"Example rendered command: `{run_command}`",
        "",
        (
            f"`{prefix}session-run` auto-advances through pending sessions in "
            "dotted numeric order. Session handoff statuses are authoritative; "
            "this manifest table is derived bookkeeping."
        ),
    ]
    if spec.execution_note:
        lines.extend(["", spec.execution_note.rstrip("\n")])
    return "\n".join(lines) + "\n"


def replace_manifest_sessions_count(manifest_text: str, total_sessions: int) -> str:
    """Rewrite the manifest's ``**Sessions**: N`` metadata line, if present."""
    return re.sub(
        r"(?m)^(\*\*Sessions\*\*:[ \t]*)\d+[ \t]*$",
        rf"\g<1>{total_sessions}",
        manifest_text,
    )


def _find_header(headers: list[str], names: set[str]) -> int | None:
    for index, header in enumerate(headers):
        if header in names:
            return index
    return None


def _replace_cell_content(cell: str, value: str) -> str:
    match = re.fullmatch(r"(\s*)(.*?)(\s*)", cell)
    if match is None:
        return value
    return f"{match.group(1)}{value}{match.group(3)}"


def _join_table_row(leading: str, cells: list[str]) -> str:
    return f"{leading}|{'|'.join(cells)}|"


def _replace_status_table_rows(
    block: list[str],
    session_id: SessionId,
    replacements: tuple[ManifestSession, ...],
) -> tuple[list[str], bool]:
    parsed_header = plans._split_table_row(block[0])
    assert parsed_header is not None
    headers = [plans._normalize_table_header(cell) for cell in parsed_header[1]]
    session_index = _find_header(headers, plans.SESSION_STATUS_TABLE_HEADERS)
    status_index = _find_header(headers, {"status"})
    title_index = _find_header(headers, {"title"})
    file_index = _find_header(headers, {"file"})
    if session_index is None or status_index is None:
        return block, False

    changed = False
    updated = block[:2]
    for row in block[2:]:
        parsed = plans._split_table_row(row)
        if parsed is None:
            updated.append(row)
            continue
        leading, cells = parsed
        if max(session_index, status_index) >= len(cells):
            updated.append(row)
            continue
        if plans._session_id_from_cell(cells[session_index]) != session_id:
            updated.append(row)
            continue

        for replacement in replacements:
            new_cells = list(cells)
            new_cells[session_index] = _replace_cell_content(
                new_cells[session_index], replacement.session_id.value
            )
            new_cells[status_index] = _replace_cell_content(
                new_cells[status_index], replacement.status
            )
            if title_index is not None and title_index < len(new_cells):
                new_cells[title_index] = _replace_cell_content(
                    new_cells[title_index], replacement.title
                )
            if file_index is not None and file_index < len(new_cells):
                new_cells[file_index] = _replace_cell_content(
                    new_cells[file_index], replacement.filename
                )
            updated.append(_join_table_row(leading, new_cells))
        changed = True
    return updated, changed


def replace_session_status_rows(
    text: str,
    session_id: int | str | SessionId,
    replacements: tuple[ManifestSession, ...] | list[ManifestSession],
) -> tuple[str, bool]:
    """Replace one session's row in every recognized session-status table.

    Each matching row is replaced with one row per ``replacements`` entry.
    Unrelated cells keep their padding; only the session-id, status, and (when
    present) title/file columns are rewritten. Tables without both a session-id
    column and a ``Status`` column are left untouched, as are legacy
    dependency-wave and ownership tables.
    """
    canonical_id = parse_session_id(session_id)
    replacement_rows = tuple(replacements)
    lines = text.splitlines()
    updated: list[str] = []
    changed = False
    index = 0

    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            updated.append(lines[index])
            index += 1
            continue

        block_start = index
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            index += 1
        block = lines[block_start:index]
        parsed_header = plans._split_table_row(block[0]) if len(block) >= 2 else None
        parsed_separator = plans._split_table_row(block[1]) if len(block) >= 2 else None
        if (
            parsed_header is None
            or parsed_separator is None
            or not plans._is_separator_row(parsed_separator[1])
        ):
            updated.extend(block)
            continue

        new_block, block_changed = _replace_status_table_rows(
            block, canonical_id, replacement_rows
        )
        updated.extend(new_block)
        changed = changed or block_changed

    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(updated) + suffix, changed


def replace_previous_session_reference(
    text: str,
    old_session_id: int | str | SessionId,
    new_session_id: int | str | SessionId,
) -> tuple[str, bool]:
    """Repoint a ``> **Previous session**`` reference line at a new session file."""
    old_line = previous_session_reference_line(old_session_id)
    new_line = previous_session_reference_line(new_session_id)
    pattern = re.compile(rf"(?m)^{re.escape(old_line)}$")
    updated = pattern.sub(lambda _match: new_line, text)
    return updated, updated != text


def add_manifest_session_row(manifest_text: str, session: ManifestSession) -> str:
    """Append a session row to the manifest's ``## Sessions`` status table.

    Reuses :mod:`plans` table primitives so the inserted row matches the
    existing header/separator alignment. Raises :class:`PlanError` when no
    session-status table is present.
    """
    lines = manifest_text.splitlines(keepends=False)
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            index += 1
            continue
        block_start = index
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            index += 1
        block = lines[block_start:index]
        if _is_session_status_table(block):
            new_row = render_manifest_session_row(session)
            leading = block[0][: len(block[0]) - len(block[0].lstrip())]
            updated = lines[:index] + [f"{leading}{new_row.lstrip()}"] + lines[index:]
            suffix = "\n" if manifest_text.endswith("\n") else ""
            return "\n".join(updated) + suffix

    raise PlanError("manifest has no session-status table to update")


def _is_session_status_table(block: list[str]) -> bool:
    if len(block) < 2:
        return False
    header = plans._split_table_row(block[0])
    separator = plans._split_table_row(block[1])
    if header is None or separator is None:
        return False
    if not plans._is_separator_row(separator[1]):
        return False
    headers = [plans._normalize_table_header(cell) for cell in header[1]]
    if "status" not in headers:
        return False
    return any(name in plans.SESSION_STATUS_TABLE_HEADERS for name in headers)


# ---------------------------------------------------------------------------
# Archive paths and readiness refresh
# ---------------------------------------------------------------------------


def _timestamp_slug(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def archive_dir_name(session_id: int | str | SessionId, now: datetime) -> str:
    """Deterministic archive subdirectory name for a resplit parent session."""
    canonical_id = parse_session_id(session_id)
    return f"session-{canonical_id.value}-{_timestamp_slug(now)}"


def create_archive_dir(
    plan_dir: str | os.PathLike[str],
    session_id: int | str | SessionId,
    now: datetime,
) -> Path:
    """Create a collision-safe archive directory under ``resplit-archive/``."""
    canonical_dir = canonicalize_path(plan_dir)
    archive_root = canonical_dir / ARCHIVE_ROOT_NAME
    archive_root.mkdir(exist_ok=True)

    base_name = archive_dir_name(session_id, now)
    candidate = archive_root / base_name
    collision_index = 2
    while True:
        try:
            candidate.mkdir()
        except FileExistsError:
            candidate = archive_root / f"{base_name}-{collision_index}"
            collision_index += 1
            continue
        return candidate


def refresh_readiness_metadata(
    plan_dir: str | os.PathLike[str],
    *,
    state: plans.ReadinessState = "draft",
    checked_at: str | None = None,
    now: datetime | None = None,
) -> ReadinessMetadata:
    """Recompute the stable readiness SHA and write manifest readiness lines.

    ``checked_at`` may be supplied for deterministic callers; otherwise a UTC
    ISO-8601 timestamp is derived from ``now`` (defaulting to the current time).
    """
    canonical_dir = canonicalize_path(plan_dir)
    sha = stable_readiness_sha256(canonical_dir)
    if checked_at is None:
        stamp = now if now is not None else datetime.now(timezone.utc)
        checked_at = stamp.astimezone(timezone.utc).isoformat()
    metadata = ReadinessMetadata(
        state=state, readiness_sha256=sha, checked_at=checked_at
    )
    write_readiness_metadata(canonical_dir, metadata)
    return metadata


# ---------------------------------------------------------------------------
# Atomic multi-file transaction
# ---------------------------------------------------------------------------


@dataclass
class _StagedFile:
    path: Path
    text: str


class PlanTransaction:
    """Stage many plan files and commit them atomically.

    Files are first written to sibling ``.<name>.tmp`` temporaries; only once
    every temporary is written successfully are they ``os.replace``-d into
    place. A failure during the temp-write phase touches no destination at all,
    and a failure during the replace phase restores every already-replaced
    destination from an in-memory backup. Either way a failed multi-file write
    leaves no partially authored plan.

    Intended to run while the plan lock is held (the transaction itself does not
    acquire the lock).
    """

    def __init__(self, plan_dir: str | os.PathLike[str]) -> None:
        self.plan_dir = canonicalize_path(plan_dir)
        self._staged: list[_StagedFile] = []

    def stage(self, relative_path: str | os.PathLike[str], text: str) -> None:
        """Stage ``text`` to be written at ``relative_path`` under the plan dir."""
        path = (self.plan_dir / os.fspath(relative_path)).resolve()
        self._staged.append(_StagedFile(path=path, text=text))

    @property
    def staged_paths(self) -> tuple[Path, ...]:
        return tuple(item.path for item in self._staged)

    def commit(self) -> tuple[Path, ...]:
        """Write all staged files atomically; roll back fully on any failure."""
        temp_paths: list[Path] = []
        try:
            for item in self._staged:
                item.path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = item.path.with_name(f".{item.path.name}.tmp")
                tmp_path.write_text(item.text, encoding="utf-8")
                temp_paths.append(tmp_path)
        except OSError as error:
            for tmp_path in temp_paths:
                _silent_unlink(tmp_path)
            raise PlanError(f"failed to stage plan write: {error}") from error

        backups: list[tuple[Path, bytes | None]] = []
        replaced: list[Path] = []
        try:
            for item, tmp_path in zip(self._staged, temp_paths):
                original = _read_bytes_if_exists(item.path)
                backups.append((item.path, original))
                os.replace(tmp_path, item.path)
                replaced.append(item.path)
        except OSError as error:
            self._rollback(backups, replaced, temp_paths)
            raise PlanError(f"failed to commit plan write: {error}") from error

        return tuple(item.path for item in self._staged)

    @staticmethod
    def _rollback(
        backups: list[tuple[Path, bytes | None]],
        replaced: list[Path],
        temp_paths: list[Path],
    ) -> None:
        replaced_set = set(replaced)
        for path, original in backups:
            if path not in replaced_set:
                continue
            if original is None:
                _silent_unlink(path)
            else:
                try:
                    path.write_bytes(original)
                except OSError:
                    pass
        for tmp_path in temp_paths:
            _silent_unlink(tmp_path)


def _read_bytes_if_exists(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
