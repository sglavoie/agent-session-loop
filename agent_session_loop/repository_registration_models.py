# ruff: noqa: F401
from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from . import (
    authoring_manifest,
    authoring_parse,
    authoring_paths,
    authoring_render,
    authoring_source_models,
    authoring_transaction,
    plans_discovery,
    plans_errors,
    plans_handoff,
    plans_models,
    plans_readiness,
    plans_registry,
    plans_session_scope,
    preflight_footprint,
    repository_intent,
    repository_scope,
    repository_scope_registry,
    repository_scope_targets,
    responses,
    state_locks,
)
from . import (
    contracts as _contracts,
)
from .contracts import (
    CommandCheck,
    CompletionContract,
    VerificationCheck,
    VerifyRecipe,
    read_completion_contract,
)
from .plans_files import canonicalize_path
from .plans_session_scope import SessionScope
from .plans_snapshot import inspect_plan
from .workspaces import (
    Repository,
    RepositoryRegistry,
    WorkspaceError,
    canonicalize_filesystem_path,
)

REPOSITORIES_HEADING = "## Repositories"

_HORIZONTAL_RULE_RE = re.compile(r"^ {0,3}([-*_])(?: *\1){2,} *$")


RegistrationStatus = Literal["applied", "already-registered", "not-required", "refused"]

MissingFact = Literal[
    "workspace-root",
    "repository-origin",
    "contract-path-outside-registry",
    "command-check-repo",
    "completed-handoff-paths",
]

#: The complete status vocabulary, in a stable order for reports and tests.
REGISTRATION_STATUSES: tuple[str, ...] = (
    "applied",
    "already-registered",
    "not-required",
    "refused",
)

#: Every fact whose absence refuses the migration, in a stable order.
MISSING_FACTS: tuple[str, ...] = (
    "workspace-root",
    "repository-origin",
    "contract-path-outside-registry",
    "command-check-repo",
    "completed-handoff-paths",
)


@dataclass(frozen=True)
class RegistrationPlan:
    """The computed migration, derived entirely from disk."""

    workspace_root: Path
    registry: RepositoryRegistry
    scopes: Mapping[str, SessionScope]
    contracts: Mapping[str, CompletionContract]


@dataclass(frozen=True)
class RegistrationResult:
    """The outcome of a migration that was refused, skipped, or applied.

    ``markdown`` carries source-plan mode's output: the migrated plan when the
    registry was applied, the input verbatim when no migration was needed, and
    ``None`` for a refusal or for plan-directory mode, which writes files.
    """

    status: RegistrationStatus
    missing_fact: MissingFact | None = None
    detail: str = ""
    workspace_root: Path | None = None
    repositories: tuple[Repository, ...] = ()
    sessions_changed: tuple[str, ...] = ()
    files: tuple[Path, ...] = ()
    readiness: str | None = None
    next_commands: tuple[str, ...] = ()
    markdown: str | None = None


@dataclass(frozen=True)
class _SessionSource:
    """One session's text and parsed contract, whatever holds them."""

    session_id: str
    text: str
    contract: CompletionContract | None
