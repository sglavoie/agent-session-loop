"""Plan authoring and structural blockers ahead of validation."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path

from . import (
    authoring_manifest,
    authoring_paths,
    authoring_render,
    authoring_source_models,
    authoring_transaction,
    contracts,
    harnesses,
)
from .authoring_transaction import PlanTransaction
from .contracts import COMPLETION_CONTRACT_HEADING
from .plans_errors import PlanError
from .plans_models import ReadinessMetadata
from .prepare_results import PREPARE_STAGE_STRUCTURE, PrepareBlocker


def _strip_contract_lines(lines: list[str]) -> list[str]:
    """Remove the ``## Completion Contract`` section from a session body."""
    start = contracts._find_heading_index(
        lines, COMPLETION_CONTRACT_HEADING, 0, len(lines)
    )
    if start is None:
        return lines
    end = contracts._section_end(lines, start, max_level=2, end=len(lines))
    return lines[:start] + lines[end:]


def _structure_blockers(
    parsed: authoring_source_models.ParsedPlan,
) -> tuple[list[PrepareBlocker], dict[str, contracts.CompletionContract]]:
    """Gate the source plan: >= 2 sessions, each with a parseable contract."""
    blockers: list[PrepareBlocker] = []
    contracts_by_id: dict[str, contracts.CompletionContract] = {}
    if len(parsed.sessions) < 2:
        blockers.append(
            PrepareBlocker(
                stage=PREPARE_STAGE_STRUCTURE,
                message=(
                    f"plan defines {len(parsed.sessions)} explicit session "
                    "heading(s); at least two supported H2-H6 "
                    "'Session <id>' headings are required"
                ),
            )
        )
    for session in parsed.sessions:
        try:
            contract = contracts.parse_completion_contract(
                "\n".join(session.lines),
                session_id=session.session_id,
                repository_registry=parsed.registry,
            )
        except PlanError as error:
            blockers.append(
                PrepareBlocker(
                    stage=PREPARE_STAGE_STRUCTURE,
                    session_id=session.session_id.value,
                    message=f"completion contract is malformed: {error}",
                )
            )
            continue
        if contract is None:
            blockers.append(
                PrepareBlocker(
                    stage=PREPARE_STAGE_STRUCTURE,
                    session_id=session.session_id.value,
                    message="session has no '## Completion Contract' section",
                )
            )
            continue
        contracts_by_id[session.session_id.value] = contract
    return blockers, contracts_by_id


def _author_plan_directory(
    source_path: str,
    text: str,
    parsed: authoring_source_models.ParsedPlan,
    contracts_by_id: dict[str, contracts.CompletionContract],
    *,
    base_dir: str | Path | None,
    force_new: bool = False,
    harness: str | None = None,
) -> Path:
    """Author the split plan directory deterministically, in one transaction.

    Sessions render exactly as ``asl split`` renders them: trimmed instruction
    lines with a registered plan's scope fields lifted into the session header,
    and a manifest whose ``session-run`` command carries ``harness``'s skill
    prefix.
    """
    skill_prefix = next(
        (
            prefix
            for name, prefix in harnesses.skill_prefixes().items()
            if name == harness
        ),
        "/",
    )
    resolver = authoring_paths.resolve_plan_output(
        source_path, base_dir, force_new=force_new
    )
    output_dir = Path(str(resolver["output_dir"]))

    transaction = PlanTransaction(output_dir)
    transaction.stage(
        "context.md",
        authoring_render.render_context(
            parsed.context_lines, str(resolver["source_path"])
        ),
    )
    transaction.stage("progress.md", authoring_render.render_progress_skeleton())

    manifest_sessions: list[authoring_manifest.ManifestSession] = []
    for index, session in enumerate(parsed.sessions):
        contract = contracts_by_id[session.session_id.value]
        transaction.stage(
            session.session_id.filename,
            authoring_render.render_session_file(
                session_id=session.session_id,
                title=session.title,
                instruction_lines=_strip_contract_lines(
                    list(session.instruction_lines)
                ),
                heading_level=session.heading_level,
                is_first=index == 0,
                previous_session_id=(
                    parsed.sessions[index - 1].session_id if index else None
                ),
                contract=contract,
                scope=session.scope,
                repository_registry=parsed.registry,
            ),
        )
        manifest_sessions.append(
            authoring_manifest.ManifestSession(
                session_id=session.session_id,
                title=session.title or f"Session {session.session_id.value}",
            )
        )

    transaction.stage(
        "manifest.md",
        authoring_manifest.render_manifest(
            authoring_manifest.ManifestSpec(
                plan_title=authoring_paths.title_from_plan(text),
                source_path=str(resolver["source_path"]),
                output_dir=str(output_dir),
                plan_id=str(resolver["plan_id"]),
                source_fingerprint=str(resolver["source_fingerprint"]),
                content_sha256=str(resolver["content_sha256"]),
                sessions=tuple(manifest_sessions),
                split_date=datetime.now(UTC).date().isoformat(),
                readiness=ReadinessMetadata(
                    state="draft",
                    readiness_sha256="pending",
                    checked_at="pending",
                ),
                registry=parsed.registry,
                skill_prefix=skill_prefix,
            )
        ),
    )
    transaction.commit()
    authoring_transaction.refresh_readiness_metadata(output_dir, state="draft")
    return output_dir


def _refresh_draft_readiness(plan_dir: Path) -> None:
    """Best-effort readiness resync so a preserved directory stays consistent."""
    with contextlib.suppress(PlanError):
        authoring_transaction.refresh_readiness_metadata(plan_dir, state="draft")
