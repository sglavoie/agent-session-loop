"""Bounded validator task packets and the prompt digest that identifies them."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import plans, workspaces
from .plans import PlanError, SessionId, canonicalize_path, parse_session_id
from .prepare_models import (
    CLAIM_TYPES,
    DEFAULT_VALIDATION_BATCH_SIZE,
    ISSUE_TYPES,
    LEGACY_PREPARE_VALIDATION_VERSION,
    MAX_PATTERNS_PER_SESSION,
    PREPARE_VALIDATION_VERSION,
    REPOSITORY_AMBIGUITY_ISSUE,
    VALIDATION_PACKET_HEADING_PREFIX,
    ValidatorRepository,
    _require_legacy_repository_footprint,
    _require_legacy_repository_intent,
)


def batch_sessions(
    session_ids: Iterable[int | str | SessionId],
    batch_size: int = DEFAULT_VALIDATION_BATCH_SIZE,
) -> tuple[tuple[str, ...], ...]:
    """Split session ids into bounded, order-preserving validation batches."""
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size < 1
    ):
        raise PlanError("validation batch size must be a positive integer")
    ordered = [parse_session_id(session_id).value for session_id in session_ids]
    return tuple(
        tuple(ordered[start : start + batch_size])
        for start in range(0, len(ordered), batch_size)
    )


@dataclass(frozen=True)
class ValidationTaskPacket:
    """Compact, harness-neutral worker packet for one validation batch.

    Mirrors the runner's session task packet conventions: plan contents are
    referenced as absolute paths to read, never inlined, and the packet embeds
    no readiness/lock/mutation policy — the caller owns parsing, storage, and
    every plan mutation through this module's fail-closed entry points.
    """

    plan_dir: str
    codebase_root: str
    session_ids: tuple[str, ...]
    session_paths: tuple[str, ...]
    context_path: str
    repositories: tuple[ValidatorRepository, ...] = ()
    session_read_repositories: tuple[tuple[ValidatorRepository, ...], ...] = ()
    version: int = LEGACY_PREPARE_VALIDATION_VERSION

    def render(self) -> str:
        joined_ids = ", ".join(self.session_ids)
        root_label = (
            "workspace" if self.version == PREPARE_VALIDATION_VERSION else "codebase"
        )
        lines: list[str] = [
            f"{VALIDATION_PACKET_HEADING_PREFIX} — sessions {joined_ids}",
            "",
            (
                f"You are a preparation validator. Research the {root_label} at "
                f"{self.codebase_root} and validate the claims made by the "
                "session files below. Return ONLY a JSON object — no prose, "
                "no fences. Do not edit any file: the caller validates your "
                "output and owns every plan mutation."
            ),
            (
                "Check claims against the environment and integrations they "
                "claim to exercise. Keep factual mismatches distinct from "
                "planning advice: do not invent a validation issue, weaken a "
                "contract, or propose a scope change solely because a broad "
                "gate repeats or integration occurs late."
            ),
            "",
            "## Read (open these paths; contents are not embedded)",
            f"- Shared context: {self.context_path}",
        ]
        if self.version == PREPARE_VALIDATION_VERSION:
            lines.extend(
                [
                    "",
                    "## Authoritative workspace registry",
                    f"- Workspace root: {self.codebase_root}",
                ]
            )
            for repository in self.repositories:
                lines.append(
                    f"- {repository.alias}: path={repository.path}; "
                    f"origin={repository.origin}; root={repository.root}"
                )
            lines.extend(
                [
                    "- Repository read scopes are advisory context, not filesystem "
                    "isolation.",
                    "",
                    "## Session packets",
                ]
            )
            for session_id, session_path, repositories in zip(
                self.session_ids,
                self.session_paths,
                self.session_read_repositories,
                strict=False,
            ):
                roots = ", ".join(
                    f"{repository.alias}={repository.root}"
                    for repository in repositories
                )
                lines.append(
                    f"- Session {session_id}: {session_path}; declared reads: {roots}"
                )
        else:
            for session_id, session_path in zip(
                self.session_ids, self.session_paths, strict=False
            ):
                lines.append(f"- Session {session_id}: {session_path}")
        lines.extend(
            [
                "",
                "## Output schema (fail-closed; unknown keys are rejected)",
                self._schema_summary(),
                (
                    "- One sessions[] entry per session above: {session_id, "
                    "title, claims, issues, details, patterns, dependencies, "
                    "contract_checked, contract_problems, corrections}."
                ),
                (
                    "- Every sessions[] entry must include spark: {eligible, "
                    "reason}. Set eligible=true only for a small explicit scope "
                    "with settled decisions, sufficient local context, and a clear "
                    "completion contract. Set it false for broad refactors, public "
                    "API/schema changes, migrations, high-risk work, or unresolved "
                    "uncertainty. reason must be concise and evidence-based."
                ),
                (
                    "- claims[]: {claim, claim_type, location, line, "
                    "to_be_created} with claim_type in "
                    f"{'/'.join(CLAIM_TYPES)} "
                    "(path/signature/field/dependency/behavior/naming/"
                    "removal). Only include claims you verified against the "
                    "codebase; mark code the session will create with "
                    "to_be_created=true and line=null. line is otherwise a "
                    "single positive integer or null. An R claim covers behavior the "
                    "session removes or alters: put the origin rationale "
                    "(commit, blame range, or covering test) in its location, "
                    "and raise removal-unjustified when no rationale can be "
                    "established."
                ),
                (
                    "- issues[]: {issue_type, summary, session_ref, actual, fix} "
                    f"with issue_type in: {', '.join(ISSUE_TYPES)}."
                ),
                "- details[]: {path, notes[]} — evidence bullets per file.",
                (
                    "- patterns[]: {name, location, reason} — at most "
                    f"{MAX_PATTERNS_PER_SESSION} per session."
                ),
                (
                    "- dependencies[]: {direction: requires|provides, "
                    "session_id, description}."
                ),
                (
                    "- contract_problems[]: {message, suggested_fix}; set "
                    "contract_checked=true when you evaluated the session's "
                    "## Completion Contract."
                ),
                (
                    "- corrections[]: {expected_text, replacement_text, reason} "
                    "— exact factual text replacements only (wrong paths, "
                    "names, signatures). expected_text must appear exactly "
                    "once in the session instructions. Never propose design "
                    "changes or contract-body edits; report those as issues "
                    "or contract_problems instead."
                ),
            ]
        )
        if self.version == PREPARE_VALIDATION_VERSION:
            lines.extend(
                [
                    (
                        "- Every claims[], issues[], details[], patterns[], "
                        "dependencies[], and corrections[] entry must carry repo, "
                        "using one of that session's declared read aliases; "
                        "contract_problems[] entries may carry it too."
                    ),
                    (
                        "- Use repository-relative locations in those entries. "
                        "Report an unqualified path claim that could refer to more "
                        "than one declared read repository as an issue with "
                        f"issue_type={REPOSITORY_AMBIGUITY_ISSUE}."
                    ),
                ]
            )
        else:
            lines.extend(
                [
                    (
                        f"- {self.codebase_root} is the only bound root for this "
                        "plan. Use locations relative to it."
                    ),
                    (
                        "- Report any path that resolves outside that root, or "
                        "that names a second repository, as an issue with "
                        f"issue_type={REPOSITORY_AMBIGUITY_ISSUE}."
                    ),
                ]
            )
        return "\n".join(lines)

    def _schema_summary(self) -> str:
        if self.version == PREPARE_VALIDATION_VERSION:
            repositories = json.dumps(
                [repository.to_dict() for repository in self.repositories],
                ensure_ascii=True,
                separators=(",", ":"),
            )
            return (
                f'{{"version": {self.version}, '
                f'"workspace_root": "{self.codebase_root}", '
                f'"repositories": {repositories}, "sessions": [...]}}'
            )
        return (
            f'{{"version": {self.version}, '
            f'"codebase_root": "{self.codebase_root}", "sessions": [...]}}'
        )


def _validation_prompt_digest(version: int) -> str:
    """Digest the rendered packet prompt for one validator schema version.

    Rendering runs over fixed placeholder inputs so the digest tracks the
    instructions a worker is given and nothing else: it moves when the prompt
    text, the schema summary, or an interpolated enum changes, and stays put
    across plans, repositories, and batch composition.
    """
    repositories = tuple(
        ValidatorRepository(
            alias=f"alias-{index}",
            path=f"path-{index}",
            origin=f"origin-{index}",
            root=f"/root/{index}",
        )
        for index in (1, 2)
    )
    packet = ValidationTaskPacket(
        plan_dir="/plan",
        codebase_root="/root",
        session_ids=("1", "2"),
        session_paths=("/plan/session-1.md", "/plan/session-2.md"),
        context_path="/plan/context.md",
        repositories=repositories,
        session_read_repositories=(repositories, repositories),
        version=version,
    )
    return hashlib.sha256(packet.render().encode("utf-8")).hexdigest()


def _validator_repositories(
    workspace: workspaces.ResolvedWorkspace,
) -> tuple[ValidatorRepository, ...]:
    return tuple(
        ValidatorRepository(
            alias=repository.alias,
            path=repository.repository.path,
            origin=repository.origin,
            root=str(repository.root),
        )
        for repository in workspace.repositories
    )


def build_validation_packet(
    plan_dir: str | Path,
    session_ids: Iterable[int | str | SessionId],
    *,
    codebase_root: str | Path,
) -> ValidationTaskPacket:
    """Build the compact validator packet for one bounded session batch.

    Every referenced session file must exist; a missing file raises
    :class:`PlanError` so a typoed batch fails before any worker is launched.
    """
    canonical_dir = canonicalize_path(plan_dir)
    ordered = [parse_session_id(session_id) for session_id in session_ids]
    if not ordered:
        raise PlanError("validation packet requires at least one session id")

    registry = plans.read_repository_registry(canonical_dir)
    resolved_repositories: tuple[ValidatorRepository, ...] = ()
    repositories_by_alias: dict[str, ValidatorRepository] = {}
    packet_version = LEGACY_PREPARE_VALIDATION_VERSION
    packet_root = os.fspath(codebase_root)
    if registry is not None:
        try:
            workspace = workspaces.resolve_workspace(codebase_root, registry)
        except workspaces.WorkspaceError as error:
            raise PlanError(f"invalid validation workspace: {error}") from error
        resolved_repositories = _validator_repositories(workspace)
        packet_root = str(workspace.root)
        repositories_by_alias = {
            repository.alias: repository for repository in resolved_repositories
        }
        packet_version = PREPARE_VALIDATION_VERSION
    else:
        _require_legacy_repository_footprint(canonical_dir, codebase_root)
        _require_legacy_repository_intent(canonical_dir, codebase_root)

    session_paths: list[str] = []
    session_read_repositories: list[tuple[ValidatorRepository, ...]] = []
    for session_id in ordered:
        session_path = canonical_dir / session_id.filename
        if not session_path.is_file():
            raise PlanError(f"missing session file: {session_path}")
        session_paths.append(str(session_path))
        if registry is not None:
            try:
                scope = plans.parse_session_scope(
                    session_path.read_text(encoding="utf-8"), registry.aliases
                )
            except OSError as error:
                raise PlanError(f"cannot read {session_path}: {error}") from error
            session_read_repositories.append(
                tuple(repositories_by_alias[alias] for alias in scope.reads)
            )

    return ValidationTaskPacket(
        plan_dir=str(canonical_dir),
        codebase_root=packet_root,
        session_ids=tuple(session_id.value for session_id in ordered),
        session_paths=tuple(session_paths),
        context_path=str(canonical_dir / "context.md"),
        repositories=resolved_repositories,
        session_read_repositories=tuple(session_read_repositories),
        version=packet_version,
    )
