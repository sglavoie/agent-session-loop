"""Split orchestration: validates a plan and authors the whole split directory."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import contracts as _contracts
from . import (
    repository_intent,
)
from .authoring_manifest import ManifestSession, ManifestSpec, render_manifest
from .authoring_parse import parse_plan_document
from .authoring_paths import resolve_plan_output, title_from_plan
from .authoring_render import (
    contract_from_payload,
    render_context,
    render_progress_skeleton,
    render_session_file,
)
from .authoring_transaction import PlanTransaction, refresh_readiness_metadata
from .plans_errors import PlanError
from .plans_files import read_input_text
from .plans_models import ReadinessMetadata
from .workspaces import (
    RepositoryRegistry,
)

# ---------------------------------------------------------------------------
# `asl split` end-to-end orchestration
# ---------------------------------------------------------------------------

SPLIT_REASON_OK = "ok"
SPLIT_REASON_SINGLE_SESSION = "single-session"
SPLIT_REASON_MALFORMED_PLAN = "malformed-plan"
SPLIT_REASON_INPUT_ERROR = "input-error"
#: The plan id already has a split output and ``--force-new`` was not given.
SPLIT_REASON_ALREADY_SPLIT = "already-split"
SPLIT_REASON_REGISTRY_REQUIRED = repository_intent.REASON_REPOSITORY_REGISTRY_REQUIRED

#: Emitted when a plan's resolved locations reach a second Git repository.
#: Distinct from the out-of-root refusal: one root outside ``--cwd`` is a
#: binding mistake, while two roots cannot execute in legacy mode at all.
MULTI_REPOSITORY_MESSAGE = (
    "plan reaches more than one Git repository but declares no "
    "`## Repositories` table; add the registry and per-session repository "
    "scopes before splitting"
)

#: Message emitted when a plan has fewer than two sessions.
SINGLE_SESSION_MESSAGE = "splitting is not needed"


class SingleSessionError(PlanError):
    """Raised when a plan defines fewer than two sessions to split."""

    def __init__(self, session_count: int) -> None:
        self.session_count = session_count
        super().__init__(
            f"only {session_count} session found — {SINGLE_SESSION_MESSAGE}"
        )


class MissingContractError(PlanError):
    """Raised when no contract is supplied for a session being rendered."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"missing contract for session {session_id}")


class InvalidContractError(PlanError):
    """Raised when a supplied contract entry is not a well-formed object.

    A contract entry must be a JSON object carrying both a ``done_when`` array
    and a ``verify`` recipe. A non-object entry or one missing either field
    would otherwise raise ``TypeError``/``KeyError`` when the fields are indexed;
    this fails closed with a stable structured reason instead.
    """

    def __init__(self, session_id: str, detail: str) -> None:
        self.session_id = session_id
        self.detail = detail
        super().__init__(f"invalid contract for session {session_id}: {detail}")


class UnknownContractError(PlanError):
    """Raised when the contracts file keys a session the plan does not define.

    An entry no session claims would otherwise be dropped without a word, which
    hides a plan whose headings did not parse the way its author meant (a
    renumbered session, a heading the splitter read as context).
    """

    def __init__(self, session_ids: Sequence[str]) -> None:
        self.session_ids = tuple(session_ids)
        super().__init__(
            "contracts file names session(s) the plan does not define: "
            + ", ".join(self.session_ids)
        )


@dataclass(frozen=True)
class SplitResult:
    """The authoritative outcome of one deterministic ``asl split`` run."""

    output_dir: Path
    plan_id: str
    source_path: str
    session_ids: tuple[str, ...]
    files: tuple[Path, ...]
    readiness: ReadinessMetadata
    skill_prefix: str
    registry: RepositoryRegistry | None = None


def split_plan(
    *,
    plan_file: str | os.PathLike[str],
    contracts_by_id: Mapping[str, Mapping[str, Any]],
    base_dir: str | os.PathLike[str] | None = None,
    skill_prefix: str = "/",
    analysis_note: str | None = None,
    now: datetime | None = None,
    force_new: bool = False,
) -> SplitResult:
    """Author a split plan directory from a source plan and Step 7 contracts.

    Chains the existing authoring APIs end to end: read the source plan, parse
    it into shared context plus per-session blocks
    (:func:`parse_plan_document`), build one validated
    :class:`~agent_session_loop.contracts.CompletionContract` per session from
    ``contracts_by_id`` (:func:`contract_from_payload`), stage context.md /
    progress.md / session files / manifest.md through one atomic
    :class:`PlanTransaction`, and refresh readiness metadata to ``draft``.

    Contracts are validated *before* the collision-safe output directory is
    created, so a :class:`MissingContractError` (no contract for a session) or a
    malformed contract leaves no empty directory behind; so does an
    :class:`UnknownContractError` (a contract keyed to no parsed session). A
    leading UTF-8 byte-order mark is dropped before parsing. A plan with fewer than
    two sessions raises :class:`SingleSessionError`; a malformed source heading
    raises the line-specific :class:`PlanError` from the parser verbatim.
    """
    canonical_source = os.path.realpath(
        os.path.abspath(os.path.expanduser(os.fspath(plan_file)))
    )
    text = read_input_text(canonical_source, label="source plan")

    parsed = parse_plan_document(text)
    if len(parsed.sessions) < 2:
        raise SingleSessionError(len(parsed.sessions))

    # Build (and validate) every contract before creating any directory so a
    # missing or malformed contract never leaves a stranded empty output dir.
    contracts: dict[str, _contracts.CompletionContract] = {}
    for session in parsed.sessions:
        session_id = session.session_id.value
        spec = contracts_by_id.get(session_id)
        if spec is None:
            raise MissingContractError(session_id)
        # Validate the entry as an object with both fields before indexing so a
        # non-object entry or a missing field fails closed as a PlanError rather
        # than escaping as an uncaught TypeError/KeyError.
        if not isinstance(spec, Mapping):
            raise InvalidContractError(
                session_id,
                f"expected a JSON object, got {type(spec).__name__}",
            )
        missing = [field for field in ("done_when", "verify") if field not in spec]
        if missing:
            raise InvalidContractError(
                session_id,
                f"missing required field(s): {', '.join(missing)}",
            )
        contracts[session_id] = contract_from_payload(
            session_id,
            spec["done_when"],
            spec["verify"],
            repository_registry=parsed.registry,
        )
    unknown = sorted(set(contracts_by_id) - set(contracts))
    if unknown:
        raise UnknownContractError(unknown)

    resolver = resolve_plan_output(canonical_source, base_dir, force_new=force_new)
    output_dir = Path(str(resolver["output_dir"]))

    transaction = PlanTransaction(output_dir)
    transaction.stage(
        "context.md",
        render_context(parsed.context_lines, str(resolver["source_path"])),
    )
    transaction.stage("progress.md", render_progress_skeleton())

    manifest_sessions: list[ManifestSession] = []
    for index, session in enumerate(parsed.sessions):
        session_id = session.session_id.value
        transaction.stage(
            session.session_id.filename,
            render_session_file(
                session_id=session_id,
                title=session.title,
                instruction_lines=session.instruction_lines,
                heading_level=session.heading_level,
                is_first=index == 0,
                previous_session_id=(
                    parsed.sessions[index - 1].session_id if index else None
                ),
                contract=contracts[session_id],
                scope=session.scope,
                repository_registry=parsed.registry,
            ),
        )
        manifest_sessions.append(
            ManifestSession(
                session_id=session.session_id,
                title=session.title or f"Session {session_id}",
            )
        )

    split_date = (now or datetime.now(UTC)).date().isoformat()
    transaction.stage(
        "manifest.md",
        render_manifest(
            ManifestSpec(
                plan_title=title_from_plan(text),
                source_path=str(resolver["source_path"]),
                output_dir=str(output_dir),
                plan_id=str(resolver["plan_id"]),
                source_fingerprint=str(resolver["source_fingerprint"]),
                content_sha256=str(resolver["content_sha256"]),
                sessions=tuple(manifest_sessions),
                split_date=split_date,
                readiness=ReadinessMetadata(
                    state="draft",
                    readiness_sha256="pending",
                    checked_at="pending",
                ),
                registry=parsed.registry,
                skill_prefix=skill_prefix,
                execution_note=analysis_note,
            )
        ),
    )
    written = transaction.commit()
    metadata = refresh_readiness_metadata(output_dir, state="draft")

    return SplitResult(
        output_dir=output_dir,
        plan_id=str(resolver["plan_id"]),
        source_path=str(resolver["source_path"]),
        session_ids=parsed.session_ids,
        files=written,
        readiness=metadata,
        skill_prefix=skill_prefix,
        registry=parsed.registry,
    )
