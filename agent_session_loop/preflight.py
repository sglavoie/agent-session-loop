"""Preflight checks for split session plan directories.

The preflight engine inspects a plan directory and surfaces structured
findings that downstream tooling (``asl preflight`` and the guided repair
skill) can route to humans or machines. Each finding carries a severity
(``red``, ``yellow``, ``green``), a stable ``category`` string, and the
file/line/session context needed to localize the issue.

Session 5 covers the core checks that run against the durable plan files
alone: plan structure validation, canonical session-id filenames, manifest
readiness metadata (state + stable SHA-256), per-session completion
contracts, ambiguity sentinels in session bodies, and lightweight path
hygiene on contract ``path`` fields plus a scope lint for overly broad
verification commands. Session 6 adds environment-aware
checks that depend on the runtime working directory, the requested
harness, the active commit mode, and the operational lock state: repo
root, git worktree status plus a clean-tree check when a commit mode is
requested, harness executable availability, stale or active locks,
optional Agent Mux ``amx`` hook discovery, and a coarse turn-budget
estimate based on pending session count. Commit modes need no external
executable check: auto-commit re-enters this checkout as ``asl commit``,
so cleanliness of the tree is the only environment requirement left.
``run_core_preflight`` remains
environment-free; ``run_environment_preflight`` exposes only the
environment findings; ``run_preflight`` aggregates both into a single
report. Registered plans additionally resolve one workspace context, validate
and capture fail-closed evidence for every declared checkout beneath the
non-Git-capable common parent, and reuse those roots for command, baseline,
and commit-mode checks. Undeclared checkouts beneath the workspace root are
never inspected, so an unrelated broken sibling cannot block an otherwise
valid plan.
Session 13 attaches machine-readable repair hints at the report
level — every finding resolves to a stable :class:`RepairHint` via
:func:`repair_hint_for_finding`, and runtime pause reasons emitted by
``asl run``/``asl verify`` (``no-contract``, ``malformed-contract``,
``verify-failed`` plus its per-check ``failure_reason``) resolve via
:func:`runtime_repair_hint`. ``ok`` is reserved as the gate signal: it
is ``True`` only when no red **or** yellow finding was emitted, matching
the discipline ``asl verify`` already uses for its JSON ``ok`` field.
"""

from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import (
    baseline,
    harness_compatibility,
    harnesses,
    plans,
    progress_repair,
    repository_evidence,
    repository_intent,
    repository_scope,
    responses,
    state,
    workspaces,
)
from .baseline_cache import BASELINE_RESULTS_FILENAME, BaselineCache
from .contracts import (
    COMPLETION_CONTRACT_HEADING,
    CommandCheck,
    CompletionContract,
    FileExistsCheck,
    RegexInFileCheck,
    parse_completion_contract,
)
from .literals import as_optional_literal
from .plans import (
    HANDOFF_HEADING,
    PlanError,
    PlanMetadata,
    PlanSnapshot,
    ProgressEntry,
    ReadinessMetadata,
    ReadinessState,
    SessionFile,
    SessionScopeError,
    canonicalize_path,
    inspect_plan,
    load_plan_metadata,
    noncanonical_session_file_suggestions,
    parse_readiness_metadata,
    progress_path,
    read_progress_entries,
    read_repository_registry,
    read_split_from,
    stable_readiness_sha256,
    write_readiness_metadata,
)
from .repository_intent import RepositoryIntent
from .verification import (
    VerificationContext,
    diagnose_command,
    diagnose_command_scope,
)

PreflightSeverity = Literal["red", "yellow", "green"]
PreflightCategory = Literal[
    "plan-structure",
    "canonical-session-id",
    "manifest-readiness",
    "missing-contract",
    "malformed-contract",
    "ambiguity-sentinel",
    "path-reference",
    "repository-footprint",
    "repository-registry-required",
    "verification-scope",
    "verification-duplicate",
    "command-not-runnable",
    "progress-journal-missing",
    "baseline-missing",
    "baseline-stale",
    "baseline-unexpected",
    "baseline-timeout",
    "baseline-near-timeout",
    "workspace-root",
    "repository-registry",
    "repository-root",
    "repository-origin",
    "repository-overlap",
    "repository-scope",
    "repository-contract",
    "env-repo-root",
    "env-git",
    "env-harness",
    "env-lock",
    "env-amx",
    "env-turn-budget",
]

PREFLIGHT_CATEGORIES: tuple[PreflightCategory, ...] = (
    "plan-structure",
    "canonical-session-id",
    "manifest-readiness",
    "missing-contract",
    "malformed-contract",
    "ambiguity-sentinel",
    "path-reference",
    "repository-footprint",
    "repository-registry-required",
    "verification-scope",
    "verification-duplicate",
    "command-not-runnable",
    "progress-journal-missing",
    "baseline-missing",
    "baseline-stale",
    "baseline-unexpected",
    "baseline-timeout",
    "baseline-near-timeout",
    "workspace-root",
    "repository-registry",
    "repository-root",
    "repository-origin",
    "repository-overlap",
    "repository-scope",
    "repository-contract",
    "env-repo-root",
    "env-git",
    "env-harness",
    "env-lock",
    "env-amx",
    "env-turn-budget",
)

#: Categories no ``--allow-not-loop-ready`` escape hatch may waive. A plan that
#: reaches outside its bound root has no snapshot covering the other location,
#: so a run there is unguarded rather than merely unreviewed. The same holds for
#: a plan whose declared intent reaches several repositories without a registry:
#: the missing registry is what would have bound those repositories in the first
#: place, so no readiness marking can stand in for it.
NON_BYPASSABLE_PREFLIGHT_CATEGORIES: frozenset[str] = frozenset(
    {"repository-footprint", "repository-registry-required"}
)

CommitMode = Literal["per-session", "end"]
COMMIT_MODES: tuple[CommitMode, ...] = ("per-session", "end")

TURN_BUDGET_YELLOW_THRESHOLD = 25
TURN_BUDGET_DEFAULT_MAX_PENDING_SESSIONS = TURN_BUDGET_YELLOW_THRESHOLD - 1
TURNS_PER_SESSION_ESTIMATE = 3

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


@dataclass(frozen=True)
class PreflightFinding:
    """Structured outcome for a single preflight observation."""

    severity: PreflightSeverity
    category: PreflightCategory
    message: str
    session_id: str | None = None
    path: str | None = None
    line: int | None = None
    detail: str = ""


FixCategory = Literal[
    "plan-recreate",
    "filename-rename",
    "manifest-update",
    "plan-edit",
    "invocation-fix",
    "env-install",
    "env-state",
    "session-recipe-edit",
    "informational",
]


@dataclass(frozen=True)
class RepairHint:
    """Machine-readable repair guidance for a preflight finding or pause reason.

    ``category`` is a :data:`PreflightCategory` value for preflight-derived
    hints and a stable runtime reason string (``"no-contract"``,
    ``"malformed-contract"``, ``"verify-failed"``) for runtime hints. The
    ``check_id`` is unique per hint and stable across releases so guided
    skills can cache fix decisions. ``question_template`` is rendered
    verbatim by the consuming skill and supports ``str.format`` keys drawn
    from :class:`PreflightFinding` field names (``session_id``, ``path``,
    ``line``, ``detail``, ``message``).
    """

    category: str
    check_id: str
    severity_hint: PreflightSeverity
    message: str
    question_template: str
    fix_category: FixCategory


REPAIR_HINTS: dict[PreflightCategory, RepairHint] = {
    "plan-structure": RepairHint(
        category="plan-structure",
        check_id="plan-structure.recreate",
        severity_hint="red",
        message=(
            "Recreate the plan directory with a valid context.md, manifest.md, "
            "and at least one canonical session-N.md file."
        ),
        question_template=(
            "Plan directory at {path} is unreadable or missing required files. "
            "Should I recreate it from the source plan?"
        ),
        fix_category="plan-recreate",
    ),
    "canonical-session-id": RepairHint(
        category="canonical-session-id",
        check_id="canonical-session-id.rename",
        severity_hint="red",
        message=(
            "Rename the non-canonical session file to the canonical session-N.md "
            "form shown in the finding detail."
        ),
        question_template=(
            "Session filename at {path} is non-canonical ({detail}). Apply the rename?"
        ),
        fix_category="filename-rename",
    ),
    "manifest-readiness": RepairHint(
        category="manifest-readiness",
        check_id="manifest-readiness.repair",
        severity_hint="red",
        message=(
            "Re-run `asl preflight <plan-dir> --json --mark-loop-ready` to "
            "refresh the manifest readiness block, or fix the manifest by hand."
        ),
        question_template=(
            "Manifest readiness metadata needs attention: {message}. "
            "Should I rerun preflight to refresh it?"
        ),
        fix_category="manifest-update",
    ),
    "missing-contract": RepairHint(
        category="missing-contract",
        check_id="missing-contract.add",
        severity_hint="red",
        message=(
            "Add a `## Completion Contract` section to the session file with "
            "`### Done when` bullets and a fenced ```json``` Verify recipe."
        ),
        question_template=(
            "Session {session_id} is missing a Completion Contract. "
            "Should I help you draft one?"
        ),
        fix_category="plan-edit",
    ),
    "malformed-contract": RepairHint(
        category="malformed-contract",
        check_id="malformed-contract.repair",
        severity_hint="red",
        message=(
            "Fix the Completion Contract syntax reported by the parser error "
            "in the finding message."
        ),
        question_template=(
            "Session {session_id} has a malformed Completion Contract: {message}. "
            "Should I walk you through the fix?"
        ),
        fix_category="plan-edit",
    ),
    "ambiguity-sentinel": RepairHint(
        category="ambiguity-sentinel",
        check_id="ambiguity-sentinel.replace",
        severity_hint="yellow",
        message=(
            "Replace the ambiguity sentinel (TBD/TODO/FIXME/etc.) with "
            "concrete instructions before running the session."
        ),
        question_template=(
            "Session {session_id} contains an ambiguity sentinel at line {line}: "
            "{detail}. Resolve it now?"
        ),
        fix_category="plan-edit",
    ),
    "path-reference": RepairHint(
        category="path-reference",
        check_id="path-reference.normalize",
        severity_hint="yellow",
        message=(
            "Rewrite the relative contract path so it stays beneath the bound "
            "root without '..' traversal."
        ),
        question_template=(
            "Session {session_id} relative contract path traverses outside "
            "--cwd ({detail}). Rewrite it?"
        ),
        fix_category="plan-edit",
    ),
    "repository-footprint": RepairHint(
        category="repository-footprint",
        check_id="repository-footprint.register",
        severity_hint="red",
        message=(
            "This plan reaches outside the single root it is bound to, so it "
            "cannot stay in legacy single-repository mode. Run "
            "`asl register-repositories <plan-dir> --cwd <repo-root>` to "
            "migrate it: the command derives from disk the `## Repositories` "
            "table, the per-session **Reads repositories**: and **Writes "
            "repositories**: scopes, and the `repo` alias plus "
            "repository-relative `path`/`cwd` on every contract check, then "
            "prints the `asl prepare --resume --rebind-workspace` command to "
            "run next. It writes nothing and names the missing fact when a "
            "fact cannot be observed."
        ),
        question_template=(
            "Plan reaches outside its bound root ({detail}). Should I run "
            "`asl register-repositories` to migrate it to a registered "
            "workspace?"
        ),
        fix_category="plan-edit",
    ),
    "repository-registry-required": RepairHint(
        category="repository-registry-required",
        check_id="repository-registry-required.declare",
        severity_hint="red",
        message=(
            "This plan intends to reach more than one Git repository, so it "
            "cannot run in legacy single-repository mode and no single child "
            "checkout may be selected as its root. Run "
            "`asl register-repositories <plan-dir> --cwd <repo-root>` to "
            "declare the registry: the command derives from disk the "
            "top-level `## Repositories` table, the per-session **Reads "
            "repositories**: and **Writes repositories**: scopes, and the "
            "`repo` alias plus repository-relative `path`/`cwd` attributing "
            "every contract check, then prints the `asl prepare --resume "
            "--rebind-workspace` command to run next. It writes nothing and "
            "names the missing fact when a fact cannot be observed."
        ),
        question_template=(
            "Plan intends to reach several Git repositories with no "
            "`## Repositories` registry ({detail}). Should I run "
            "`asl register-repositories` to declare the registry and "
            "per-session repository scopes?"
        ),
        fix_category="plan-edit",
    ),
    "verification-scope": RepairHint(
        category="verification-scope",
        check_id="verification-scope.narrow",
        severity_hint="red",
        message=(
            "Narrow the verification recipe so it targets only the package(s) "
            "this session actually changes; ASL session verification must not "
            "run repo-wide `./operator health` commands."
        ),
        question_template=(
            "Session {session_id} contains an overly broad verification "
            "command ({detail}). Should I narrow the recipe?"
        ),
        fix_category="plan-edit",
    ),
    "verification-duplicate": RepairHint(
        category="verification-duplicate",
        check_id="verification-duplicate.remove",
        severity_hint="yellow",
        message=(
            "Remove the duplicate command check. Keep one authoritative gate, "
            "or make a genuinely narrower check conditional with "
            "`on_failure_of` when it adds failure diagnostics."
        ),
        question_template=(
            "Session {session_id} repeats an identical verification command "
            "({detail}). Remove the duplicate?"
        ),
        fix_category="plan-edit",
    ),
    "command-not-runnable": RepairHint(
        category="command-not-runnable",
        check_id="command-not-runnable.fix",
        severity_hint="red",
        message=(
            "Fix the contract command so it can run in this environment: use "
            "`python3` (not `python`) if only python3 is on PATH, install the "
            "missing runner, or switch to the repo's actual test convention "
            "(e.g. `python3 -m unittest`)."
        ),
        question_template=(
            "Session {session_id} contract command is not runnable ({detail}). "
            "Should I fix the recipe?"
        ),
        fix_category="plan-edit",
    ),
    "progress-journal-missing": RepairHint(
        category="progress-journal-missing",
        check_id="progress-journal-missing.backfill",
        severity_hint="yellow",
        message=(
            "Run `asl backfill-progress --force <plan-dir>` to reconstruct "
            "missing progress.md, or the same command when the journal "
            "already exists but is stale or malformed."
        ),
        question_template=(
            "Progress journal problem: {message}. "
            "Should I run `asl backfill-progress` to reconstruct it?"
        ),
        fix_category="manifest-update",
    ),
    "baseline-missing": RepairHint(
        category="baseline-missing",
        check_id="baseline-missing.refresh",
        severity_hint="red",
        message=(
            "Record baseline evidence before the first session by running "
            "`asl baseline <plan-dir> --cwd <repo-root>`; the readiness gate "
            "requires current evidence when no session is complete."
        ),
        question_template=(
            "Session {session_id} declares a baseline expectation but no "
            "evidence is recorded ({detail}). Should I run `asl baseline`?"
        ),
        fix_category="manifest-update",
    ),
    "baseline-stale": RepairHint(
        category="baseline-stale",
        check_id="baseline-stale.refresh",
        severity_hint="yellow",
        message=(
            "Refresh stale baseline evidence with "
            "`asl baseline <plan-dir> --cwd <repo-root>`; the recorded "
            "repository fingerprint no longer matches the working tree."
        ),
        question_template=(
            "Session {session_id} baseline evidence is stale ({detail}). "
            "Should I re-run `asl baseline` to refresh it?"
        ),
        fix_category="manifest-update",
    ),
    "baseline-unexpected": RepairHint(
        category="baseline-unexpected",
        check_id="baseline-unexpected.fix",
        severity_hint="red",
        message=(
            "Resolve the baseline mismatch: fix the pre-existing failure, or "
            "correct the check's `baseline_expectation` in the session recipe "
            "so it matches reality."
        ),
        question_template=(
            "Session {session_id} baseline check did not match its declared "
            "expectation ({detail}). Should I fix the recipe or the code?"
        ),
        fix_category="session-recipe-edit",
    ),
    "baseline-timeout": RepairHint(
        category="baseline-timeout",
        check_id="baseline-timeout.fix",
        severity_hint="red",
        message=(
            "The baseline check exceeded its budget. Narrow the recipe to the "
            "session's own targeted tests, or raise the budget only if the "
            "slow command is genuinely required."
        ),
        question_template=(
            "Session {session_id} baseline check timed out ({detail}). "
            "Should I narrow the recipe?"
        ),
        fix_category="session-recipe-edit",
    ),
    "baseline-near-timeout": RepairHint(
        category="baseline-near-timeout",
        check_id="baseline-near-timeout.split",
        severity_hint="yellow",
        message=(
            "The baseline check ran within 80% of its budget. Consider "
            "narrowing the recipe to targeted tests so it does not begin "
            "timing out as the repository grows."
        ),
        question_template=(
            "Session {session_id} baseline check is near its timeout budget "
            "({detail}). Should I narrow the recipe?"
        ),
        fix_category="session-recipe-edit",
    ),
    "workspace-root": RepairHint(
        category="workspace-root",
        check_id="workspace-root.use-cwd",
        severity_hint="red",
        message=(
            "Pass the existing common workspace directory via `--cwd`; for a "
            "registered plan it does not need to be a Git repository."
        ),
        question_template=(
            "Workspace root {path} is invalid ({message}). Which directory "
            "should I use as --cwd?"
        ),
        fix_category="invocation-fix",
    ),
    "repository-registry": RepairHint(
        category="repository-registry",
        check_id="repository-registry.repair",
        severity_hint="red",
        message=(
            "Repair the manifest `## Repositories` table so aliases, relative "
            "paths, and credential-free expected origins are valid and unique."
        ),
        question_template=(
            "The repository registry is invalid ({message}). Should I repair "
            "the manifest table?"
        ),
        fix_category="plan-edit",
    ),
    "repository-root": RepairHint(
        category="repository-root",
        check_id="repository-root.repair",
        severity_hint="red",
        message=(
            "Restore the registered checkout as an exact Git worktree root "
            "with readable Git evidence."
        ),
        question_template=(
            "A registered repository root is invalid ({message}). Should I "
            "inspect the checkout layout?"
        ),
        fix_category="env-state",
    ),
    "repository-origin": RepairHint(
        category="repository-origin",
        check_id="repository-origin.correct-checkout",
        severity_hint="red",
        message=(
            "Use the checkout whose credential-free origin matches the registry, "
            "or correct the registry if the declared origin is wrong."
        ),
        question_template=(
            "A registered repository has the wrong origin ({message}). Should I "
            "inspect the checkout and manifest entry?"
        ),
        fix_category="env-state",
    ),
    "repository-overlap": RepairHint(
        category="repository-overlap",
        check_id="repository-overlap.separate",
        severity_hint="red",
        message=(
            "Use disjoint repository paths and exact Git top-levels; nested or "
            "duplicate registered roots are not supported."
        ),
        question_template=(
            "Registered repository roots overlap ({message}). Should I repair "
            "the registry paths?"
        ),
        fix_category="plan-edit",
    ),
    "repository-scope": RepairHint(
        category="repository-scope",
        check_id="repository-scope.repair",
        severity_hint="red",
        message=(
            "Add exactly one valid Reads repositories field and Writes "
            "repositories field; writes must be a subset of reads and all aliases "
            "must exist in the registry."
        ),
        question_template=(
            "Session {session_id} has invalid repository scope ({message}). "
            "Should I repair its scope fields?"
        ),
        fix_category="plan-edit",
    ),
    "repository-contract": RepairHint(
        category="repository-contract",
        check_id="repository-contract.repair",
        severity_hint="red",
        message=(
            "Give every registered completion check a valid repository alias and "
            "keep path/cwd values canonical and beneath that repository."
        ),
        question_template=(
            "Session {session_id} has an invalid repository-aware contract "
            "({message}). Should I repair the recipe?"
        ),
        fix_category="plan-edit",
    ),
    "env-repo-root": RepairHint(
        category="env-repo-root",
        check_id="env-repo-root.use-cwd",
        severity_hint="red",
        message=(
            "Pass an existing repository directory via `--cwd` when invoking "
            "the command."
        ),
        question_template=(
            "Repo root {path} does not exist or is not a directory. "
            "Which path should I use as --cwd?"
        ),
        fix_category="invocation-fix",
    ),
    "env-git": RepairHint(
        category="env-git",
        check_id="env-git.repair",
        severity_hint="red",
        message=(
            "Run from inside a git working tree, or resolve the git status "
            "problem shown in the finding detail."
        ),
        question_template=("Git environment problem: {message}. Should I try a fix?"),
        fix_category="env-state",
    ),
    "env-harness": RepairHint(
        category="env-harness",
        check_id="env-harness.install-cli",
        severity_hint="red",
        message=(
            "Install the harness CLI on PATH, or set "
            "`agent_session_loop/harnesses.json` to point at the right executable."
        ),
        question_template=(
            "Harness CLI problem: {message}. Should I help configure harnesses.json?"
        ),
        fix_category="env-install",
    ),
    "env-lock": RepairHint(
        category="env-lock",
        check_id="env-lock.repair",
        severity_hint="red",
        message=(
            "Run `asl unlock <plan-dir> --json` to clear a stale lock, or "
            "wait for the active runner to finish."
        ),
        question_template=(
            "Lock contention: {message}. "
            "Should I run `asl unlock <plan-dir> --json` or wait for the "
            "runner?"
        ),
        fix_category="env-state",
    ),
    "env-amx": RepairHint(
        category="env-amx",
        check_id="env-amx.optional",
        severity_hint="green",
        message=(
            "Agent Mux integration is optional and never blocks loop-ready. "
            "No fix required."
        ),
        question_template=(
            "Agent Mux hooks are optional ({message}). No action is required."
        ),
        fix_category="informational",
    ),
    "env-turn-budget": RepairHint(
        category="env-turn-budget",
        check_id="env-turn-budget.healthy",
        severity_hint="green",
        message=("Turn budget is within the expected range. No fix required."),
        question_template=(
            "Turn budget is healthy ({message}). No action is required."
        ),
        fix_category="informational",
    ),
}


_CATEGORY_HINT_OVERRIDES: dict[str, RepairHint] = {
    "repository-footprint.multi-repo": RepairHint(
        category="repository-footprint",
        check_id="repository-footprint.multi-repo",
        severity_hint="red",
        message=(
            "The plan names locations in more than one Git repository while "
            "bound to a single root, so nothing snapshots the other "
            "repositories. Run `asl register-repositories <plan-dir> --cwd "
            "<repo-root>` to declare them: the command derives the same "
            "`## Repositories` table preflight reports as "
            "`repository_footprint.suggested_registry_table`, adds the "
            "per-session **Reads repositories**: and **Writes "
            "repositories**: scopes and the `repo` alias plus "
            "repository-relative `path`/`cwd` on every contract check, then "
            "prints the `asl prepare --resume --rebind-workspace` command to "
            "run next."
        ),
        question_template=(
            "Plan spans several repositories with no registry ({detail}). "
            "Should I run `asl register-repositories` to migrate it to a "
            "registered workspace?"
        ),
        fix_category="plan-edit",
    ),
    "repository-footprint.out-of-root": RepairHint(
        category="repository-footprint",
        check_id="repository-footprint.out-of-root",
        severity_hint="red",
        message=(
            "A contract check points outside the root this plan is bound to, "
            "where no snapshot or write guard applies. Either rewrite the "
            "target as a path beneath the bound root, or run "
            "`asl register-repositories <plan-dir> --cwd <repo-root>` to "
            "declare the other location: the command derives the "
            "`## Repositories` table, the per-session **Reads repositories**: "
            "and **Writes repositories**: scopes, and the `repo` alias plus "
            "repository-relative `path`/`cwd` on every contract check, then "
            "prints the `asl prepare --resume --rebind-workspace` command to "
            "run next. It applies only to a plan reaching several "
            "repositories; a single-repository escape has to be rewritten."
        ),
        question_template=(
            "Contract target escapes the bound root ({detail}). Rewrite it, or "
            "run `asl register-repositories` to migrate the plan to a "
            "registered workspace?"
        ),
        fix_category="plan-edit",
    ),
    "manifest-readiness.missing": RepairHint(
        category="manifest-readiness",
        check_id="manifest-readiness.missing",
        severity_hint="red",
        message=(
            "Add **Readiness**, **Readiness SHA-256**, and **Readiness checked "
            "at** lines to manifest.md, or run "
            "`asl preflight <plan-dir> --json --mark-loop-ready`."
        ),
        question_template=(
            "Manifest is missing readiness metadata. "
            "Should I run `asl preflight <plan-dir> --json --mark-loop-ready` "
            "to add it?"
        ),
        fix_category="manifest-update",
    ),
    "manifest-readiness.sha-mismatch": RepairHint(
        category="manifest-readiness",
        check_id="manifest-readiness.sha-mismatch",
        severity_hint="red",
        message=(
            "Re-run `asl preflight <plan-dir> --json --mark-loop-ready` after "
            "reviewing plan edits; the recorded SHA-256 no longer matches the "
            "plan content."
        ),
        question_template=(
            "Manifest readiness SHA-256 is stale ({detail}). "
            "Should I re-mark the plan loop-ready?"
        ),
        fix_category="manifest-update",
    ),
    "manifest-readiness.state-not-loop-ready": RepairHint(
        category="manifest-readiness",
        check_id="manifest-readiness.state-not-loop-ready",
        severity_hint="yellow",
        message=(
            "Advance readiness state from `draft` or `reviewed` to "
            "`loop-ready` with `asl preflight <plan-dir> --json "
            "--mark-loop-ready` once all checks pass."
        ),
        question_template=(
            "Plan readiness state is not loop-ready ({message}). "
            "Should I advance it to loop-ready?"
        ),
        fix_category="manifest-update",
    ),
    "env-git.outside-worktree": RepairHint(
        category="env-git",
        check_id="env-git.outside-worktree",
        severity_hint="red",
        message=(
            "Run from inside a git working tree, or pass `--cwd` pointing at one."
        ),
        question_template=(
            "Repo root {path} is not inside a git working tree. "
            "Which path should I use as --cwd?"
        ),
        fix_category="invocation-fix",
    ),
    "env-git.dirty-worktree": RepairHint(
        category="env-git",
        check_id="env-git.dirty-worktree",
        severity_hint="yellow",
        message=(
            "Stash or commit dirty files before requesting "
            "`--commit-per-session` or `--commit-at-end`."
        ),
        question_template=(
            "Git worktree is dirty and commit-mode {detail} is requested. "
            "Stash, commit, or drop the commit-mode flag?"
        ),
        fix_category="env-state",
    ),
    "env-lock.stale": RepairHint(
        category="env-lock",
        check_id="env-lock.stale",
        severity_hint="yellow",
        message=("Run `asl unlock <plan-dir> --json` to clear the stale lock."),
        question_template=(
            "A stale lock is present at {path} ({detail}). "
            "Clear it with `asl unlock <plan-dir> --json`?"
        ),
        fix_category="env-state",
    ),
    "env-lock.active": RepairHint(
        category="env-lock",
        check_id="env-lock.active",
        severity_hint="red",
        message=(
            "Wait for the active runner to finish, or run "
            "`asl unlock <plan-dir> --json` if you are certain the pid is "
            "gone. Never pass `--force` from an automated repair."
        ),
        question_template=(
            "An active lock is held at {path} ({detail}). "
            "Wait for the runner or force-unlock?"
        ),
        fix_category="env-state",
    ),
    # The check id says `split` because an invariant key feeds an issue
    # fingerprint and renaming one splits the issue's history in the inbox. The
    # remedy it once named did the opposite of what the finding asks: resplitting
    # replaces one pending parent with several pending children, so it raises the
    # very count the finding is about.
    "env-turn-budget.split": RepairHint(
        category="env-turn-budget",
        check_id="env-turn-budget.split",
        severity_hint="yellow",
        message=(
            "Bound the run with `asl run --max-sessions N` so it stops inside "
            "the turn budget, complete or close pending sessions to bring the "
            "count down, or rerun preflight with `--max-pending-sessions N` "
            "when the larger serial plan is intentional."
        ),
        question_template=(
            "Pending session count exceeds the turn-budget threshold "
            "({message}). Cap `asl run --max-sessions`, close pending "
            "sessions, or raise `--max-pending-sessions`?"
        ),
        fix_category="env-state",
    ),
    "command-not-runnable.executable": RepairHint(
        category="command-not-runnable",
        check_id="command-not-runnable.executable",
        severity_hint="red",
        message=(
            "The contract command's executable is not on PATH. Use `python3` "
            "instead of `python` if only python3 is installed, install the "
            "missing binary, or switch to a runnable command."
        ),
        question_template=(
            "Session {session_id} contract command's executable is not on PATH "
            "({detail}). Should I fix the recipe?"
        ),
        fix_category="plan-edit",
    ),
    "command-not-runnable.module": RepairHint(
        category="command-not-runnable",
        check_id="command-not-runnable.module",
        severity_hint="yellow",
        message=(
            "The contract command runs `python -m <module>` for a module that "
            "is not importable here. Install the module, or prefer the repo's "
            "actual test convention (e.g. `python3 -m unittest`)."
        ),
        question_template=(
            "Session {session_id} contract command runs a module that is not "
            "importable ({detail}). Should I fix the recipe?"
        ),
        fix_category="plan-edit",
    ),
}


RUNTIME_REPAIR_HINTS: dict[str, RepairHint] = {
    "no-contract": RepairHint(
        category="no-contract",
        check_id="runtime.no-contract",
        severity_hint="red",
        message=(
            "Add a `## Completion Contract` section to the session file. The "
            "runner refuses to advance sessions without one."
        ),
        question_template=(
            "Runner paused: session has no Completion Contract. "
            "Should I help you add one?"
        ),
        fix_category="plan-edit",
    ),
    "malformed-contract": RepairHint(
        category="malformed-contract",
        check_id="runtime.malformed-contract",
        severity_hint="red",
        message=(
            "Fix the Completion Contract JSON. Preflight normally catches "
            "this, so check whether the session was edited after the plan was "
            "marked loop-ready."
        ),
        question_template=(
            "Runner paused: malformed Completion Contract. "
            "Should I walk you through the fix?"
        ),
        fix_category="plan-edit",
    ),
    "verify-failed.command-failed": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-failed",
        severity_hint="red",
        message=(
            "Investigate the failing command's exit code; fix the underlying "
            "issue or correct the recipe."
        ),
        question_template=(
            "Verification command exited non-zero. "
            "Show me the stderr and propose a fix?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.command-timeout": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-timeout",
        severity_hint="red",
        message=(
            "Raise the recipe timeout in the Completion Contract, or fix the "
            "underlying slowness in the command."
        ),
        question_template=(
            "Verification command timed out. "
            "Should I raise the timeout or investigate the slowness?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.command-not-found": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-not-found",
        severity_hint="red",
        message=(
            "The contract's command names an executable that is not on PATH "
            "— commonly a wrong interpreter (bare `python` instead of "
            "`python3`) or a tool that is not installed (e.g. `pytest`). Fix "
            "the interpreter/tool in the recipe (name `argv[0]` and its likely "
            "replacement), install the missing binary, or switch to the repo's "
            "own convention (e.g. `python3 -m unittest`). This class is caught "
            "earlier by the `command-not-runnable` preflight check, so seeing "
            "it at verify time usually means the plan predates that gate or "
            "the environment changed after preflight."
        ),
        question_template=(
            "Verification command's executable was not found on PATH (likely a "
            "bare `python` that should be `python3`, or an uninstalled tool). "
            "Fix the interpreter/tool in the recipe or install the binary?"
        ),
        fix_category="env-install",
    ),
    "verify-failed.command-error": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-error",
        severity_hint="red",
        message=(
            "Resolve the OS-level error that prevented command execution "
            "(permissions, missing cwd, etc.) and re-run."
        ),
        question_template=(
            "Verification command could not be executed. "
            "Should I diagnose the OS error?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.command-overbroad": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-overbroad",
        severity_hint="red",
        message=(
            "Narrow the verification command to package-scoped checks before "
            "retrying; ASL refuses repo-wide `./operator health` gates."
        ),
        question_template=(
            "Verification command is too broad for ASL session execution. "
            "Should I narrow the recipe?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.file-missing": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.file-missing",
        severity_hint="red",
        message=(
            "Create the expected artifact, or fix the contract `path` field "
            "to point at the right location."
        ),
        question_template=(
            "Expected artifact is missing. "
            "Should I create the file or correct the contract path?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.file-error": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.file-error",
        severity_hint="red",
        message=(
            "Resolve the OS-level error preventing file read/stat "
            "(permissions, encoding, etc.) and re-run."
        ),
        question_template=(
            "Verification could not read the artifact. Should I diagnose the OS error?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.regex-not-matched": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.regex-not-matched",
        severity_hint="red",
        message=(
            "Update the file to contain the expected pattern, or relax the "
            "regex in the recipe."
        ),
        question_template=(
            "Verification regex did not match. "
            "Should I update the file or loosen the pattern?"
        ),
        fix_category="session-recipe-edit",
    ),
    # Pauses blocked on a workspace or environment precondition
    # (``pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS``). These refusals come
    # from the runner's own gates, whose pause details carry findings rather
    # than repair hints, so the reason is the only key available and each hint
    # has to name the workspace change that lets the same gate pass next run.
    "commit-preflight": RepairHint(
        category="commit-preflight",
        check_id="runtime.commit-preflight",
        severity_hint="red",
        message=(
            "Commit preflight refused to start the run: the repositories it "
            "would commit are not in the state it requires — most often "
            "uncommitted changes already present before the run. Inspect "
            "`git -C <repo-root> status --short` plus each repository the "
            "pause names under `details.dirty_repositories`, then commit, "
            "stash, or discard that work and re-run. No reply can clear this: "
            "the worktree has to change."
        ),
        question_template=(
            "Runner paused: commit preflight found the workspace unready "
            "({message}). Should I walk through the dirty repositories?"
        ),
        fix_category="env-state",
    ),
    "commit-failed": RepairHint(
        category="commit-failed",
        check_id="runtime.commit-failed",
        severity_hint="red",
        message=(
            "The session's commit did not land. Re-run the failing `git` "
            "command in <repo-root> to see the error in full: a hook that "
            "rejected the commit, an unresolved merge/rebase state, or an "
            "index lock left by another process. Clear that condition and "
            "re-run; the next run first retries the outstanding commit group "
            "before it selects another session."
        ),
        question_template=(
            "Runner paused: the commit failed ({message}). "
            "Should I diagnose the git error?"
        ),
        fix_category="env-state",
    ),
    "repository-registry-required": RepairHint(
        category="repository-registry-required",
        check_id="runtime.repository-registry-required",
        severity_hint="red",
        message=(
            "This plan reaches more than one Git repository, so it cannot run "
            "in legacy single-repository mode. Run `asl register-repositories "
            "<plan-dir> --cwd <repo-root>` to derive and write the "
            "`## Repositories` registry, then the `asl prepare <plan-dir> "
            "--resume --rebind-workspace` command it prints. Where the plan "
            "need not reach the second repository at all, retarget its paths "
            "into the bound one instead — that is the cheaper fix."
        ),
        question_template=(
            "Runner paused: the plan reaches several repositories with no "
            "registry ({message}). Should I run the migration?"
        ),
        fix_category="plan-edit",
    ),
    "workspace-repository-contention": RepairHint(
        category="workspace-repository-contention",
        check_id="runtime.workspace-repository-contention",
        severity_hint="red",
        message=(
            "Another live run already claims a repository this run declares, "
            "and two agents writing one working tree cannot be made safe. "
            "Wait for the other run to finish, stop it, or move this plan to "
            "its own git worktree and re-derive the binding with `asl prepare "
            "<plan-dir> --resume --rebind-workspace`."
        ),
        question_template=(
            "Runner paused: a repository is claimed by another live run "
            "({message}). Should I find the other run?"
        ),
        fix_category="env-state",
    ),
    "not-loop-ready": RepairHint(
        category="not-loop-ready",
        check_id="runtime.not-loop-ready",
        severity_hint="red",
        message=(
            "The readiness gate refused the plan. Run `asl preflight "
            "<plan-dir> --cwd <repo-root> --json`, clear every red finding it "
            "reports, then `asl preflight <plan-dir> --json "
            "--mark-loop-ready`. The gate payload on this pause already lists "
            "the blocking findings and their own repair hints."
        ),
        question_template=(
            "Runner paused: the plan is not loop-ready ({message}). "
            "Should I drive the preflight repairs?"
        ),
        fix_category="manifest-update",
    ),
    "required-checks-incomplete": RepairHint(
        category="required-checks-incomplete",
        check_id="runtime.required-checks-incomplete",
        severity_hint="red",
        message=(
            "The run reached the end with required verification still short of "
            "passed, so it was refused the success it would otherwise have "
            "reported. The pause payload names each one under "
            "`details.deferred_unresolved` (broad gates a findings review "
            "deferred and no later all-clear review closed) and "
            "`details.unpassed_verifications`. Make those gates pass — usually "
            "by letting the review repair loop finish, raising "
            "`--max-review-fix-cycles`, or fixing the code the gate reports on "
            "— then re-run. No reply clears this: the checks have to pass."
        ),
        question_template=(
            "Runner paused: required checks never reached passed ({message}). "
            "Should I walk through the outstanding gates?"
        ),
        fix_category="env-state",
    ),
    "review-not-clear": RepairHint(
        category="review-not-clear",
        check_id="runtime.review-not-clear",
        severity_hint="red",
        message=(
            "The run finished the plan's sessions but holds no all-clear "
            "review, so it may not report the plan complete. Re-run with "
            "`--review-harness` so the generated review session executes; if a "
            "review did run and returned findings, let its repair cycles "
            "converge (raise `--max-review-fix-cycles` when the cap was the "
            "limit) until a later review comes back all-clear."
        ),
        question_template=(
            "Runner paused: the final review is missing or not all-clear "
            "({message}). Should I drive the review to completion?"
        ),
        fix_category="env-state",
    ),
    "guard-protected-path-changed": RepairHint(
        category="guard-protected-path-changed",
        check_id="runtime.guard-protected-path-changed",
        severity_hint="red",
        message=(
            "A protected-path guard froze a path at its turn-start state and "
            "the workspace no longer matches. The run kept every filesystem "
            "change on purpose and returned the session to pending. Read the "
            "matched guard IDs under `details.protected_path_guard."
            "matched_paths` and the qualified before/after content under "
            "`.evidence`, diff each named path in "
            "<repo-root>, then either restore it to its turn-start content or "
            "keep the change deliberately. Re-run once the workspace matches "
            "that decision; guards are run-scoped, so the guard is not carried "
            "into the next run unless it is added again. No reply clears this: "
            "a mailbox answer cannot restore a file."
        ),
        question_template=(
            "Runner paused: a protected path changed ({message}). "
            "Should I walk through the before/after evidence?"
        ),
        fix_category="env-state",
    ),
    "guard-protected-path-unavailable": RepairHint(
        category="guard-protected-path-unavailable",
        check_id="runtime.guard-protected-path-unavailable",
        severity_hint="red",
        message=(
            "A protected-path guard was active but the turn-start workspace "
            "snapshot it compares against is missing or unreadable, so the "
            "guard failed closed rather than claim a protection it could not "
            "check. The pause explains what was missing under "
            "`details.protected_path_guard.detail`. Repair the evidence "
            "capture itself — "
            "confirm each repository under <repo-root> is a readable git "
            "worktree with no interrupted merge/rebase or index lock, and that "
            "`git -C <repo-root> status --short` succeeds — then re-run. No "
            "reply clears this: the snapshot has to become readable."
        ),
        question_template=(
            "Runner paused: the protected-path snapshot was unreadable "
            "({message}). Should I diagnose the repository evidence?"
        ),
        fix_category="env-state",
    ),
    "conversation-recovery-required": RepairHint(
        category="conversation-recovery-required",
        check_id="runtime.conversation-recovery-required",
        severity_hint="red",
        message=(
            "The pending session's saved harness conversation cannot be "
            "resumed exactly, so the run refused to open a second one behind "
            "your back. `details.conversation_recovery` names the selection "
            "state and, when the binding is what changed, each field that no "
            "longer matches. Where the mismatch is accidental — a different "
            "`--cwd`, a re-derived repository registry, a different harness — "
            "re-run with the original values and the saved conversation is "
            "picked up unchanged. Where the conversation is genuinely gone or "
            "the change is intended, re-run with `--fresh-conversation` to "
            "authorize one fresh start: files and outstanding feedback are "
            "retained and the new turn opens with recovery context, but the "
            "agent's accumulated reasoning is lost. That authorization is "
            "one-use and is never persisted or replayed."
        ),
        question_template=(
            "Runner paused: the saved conversation cannot be resumed "
            "({message}). Should I compare the recorded binding?"
        ),
        fix_category="env-state",
    ),
    "redirect-successor-blocked": RepairHint(
        category="redirect-successor-blocked",
        check_id="runtime.redirect-successor-blocked",
        severity_hint="red",
        message=(
            "An urgent correction force-stopped the turn and the stopped "
            "worker could not be shown gone, so the run refused to start a "
            "successor that would share the workspace with it. "
            "`details.termination` carries the reason: `remaining_pids` are "
            "descendants still answering under the identity they were sampled "
            "with, `unverified_pids` are ones whose identity could not be read "
            "at all, `ownership_unknown` means a probe was refused, and "
            "`predecessor-uncollected` means the stopped worker was never "
            "reaped by the turn that owned it. Check those pids with "
            "`ps -p <pid> -o pid,ppid,lstart,command`; a survivor has to be "
            "ended and a reused pid is already harmless. One reason names a "
            "setting rather than a process: `process-sweep-opt-out` means "
            "`ASL_PROCESS_SWEEP_DISABLED` withheld the descendant evidence, so "
            "nothing is known to be running and nothing can be shown stopped "
            "either -- unset it to let the next run collect that evidence. "
            "No reply clears this "
            "and no flag overrides it: the next run re-probes, and a machine "
            "that has since gone quiet simply proceeds."
        ),
        question_template=(
            "Runner paused: the force-stopped worker could not be shown gone "
            "({message}). Should I identify the surviving processes?"
        ),
        fix_category="env-state",
    ),
    "redirect-delivery-unrecordable": RepairHint(
        category="redirect-delivery-unrecordable",
        check_id="runtime.redirect-delivery-unrecordable",
        severity_hint="red",
        message=(
            "The turn was carrying an urgent correction and the redirect "
            "journal would not record that its prompt was about to be sent, so "
            "the run refused to send it. That refusal is deliberate: a "
            "submission the journal cannot show would be indistinguishable "
            "afterwards from one that never happened, and the next run would "
            "hand the same correction to a second worker. "
            "`details.operation_ids` names the corrections still held and the "
            "preceding `redirect-continuation` event carries the journal's own "
            "reason. Check the state directory the journal lives in -- "
            "`df -h` for a full filesystem, `mount` and the directory's mode "
            "for a read-only or unwritable one -- then re-run. The words are "
            "still in the journal and the next turn carries them; nothing was "
            "lost and no reply is needed."
        ),
        question_template=(
            "Runner paused: the redirect journal could not record this turn's "
            "submission ({message}). Should I check the state directory?"
        ),
        fix_category="env-state",
    ),
}


def _override_check_id_for_finding(finding: PreflightFinding) -> str:
    """Return the override check_id for a finding, or '' if no override applies."""
    if finding.category == "manifest-readiness":
        if "missing readiness" in finding.message:
            return "manifest-readiness.missing"
        if "SHA-256 does not match" in finding.message:
            return "manifest-readiness.sha-mismatch"
        if "readiness state is" in finding.message:
            return "manifest-readiness.state-not-loop-ready"
        return ""
    if finding.category == "env-git":
        if "not inside a git repository" in finding.message:
            return "env-git.outside-worktree"
        if "worktree is dirty" in finding.message:
            return "env-git.dirty-worktree"
        return ""
    if finding.category == "env-lock":
        if "stale lock" in finding.message:
            return "env-lock.stale"
        if "active lock" in finding.message:
            return "env-lock.active"
        return ""
    if finding.category == "env-turn-budget":
        if finding.severity == "yellow":
            return "env-turn-budget.split"
        return ""
    if finding.category == "repository-footprint":
        if "escapes the bound root" in finding.message:
            return "repository-footprint.out-of-root"
        if "git repositories" in finding.message:
            return "repository-footprint.multi-repo"
        return ""
    if finding.category == "command-not-runnable":
        if "executable is not on PATH" in finding.message:
            return "command-not-runnable.executable"
        if "module is not importable" in finding.message:
            return "command-not-runnable.module"
        return ""
    return ""


def repair_hint_for_finding(finding: PreflightFinding) -> RepairHint:
    """Resolve a preflight finding to its canonical repair hint.

    Sub-case overrides (keyed by ``check_id``) win over the category-level
    default in :data:`REPAIR_HINTS`.
    """
    override_id = _override_check_id_for_finding(finding)
    if override_id and override_id in _CATEGORY_HINT_OVERRIDES:
        return _CATEGORY_HINT_OVERRIDES[override_id]
    return REPAIR_HINTS[finding.category]


def runtime_repair_hint(
    reason: str, *, failure_reason: str | None = None
) -> RepairHint | None:
    """Resolve a runner/verify pause reason to its repair hint.

    ``reason`` is the stable ``asl run``/``asl verify`` pause string
    (``"no-contract"``, ``"malformed-contract"``, ``"verify-failed"``).
    For ``verify-failed``, pass the per-check ``failure_reason`` (e.g.,
    ``"command-not-found"``) to resolve a more specific hint. Returns
    ``None`` for unknown reasons so callers can decide whether to fall
    back to a generic message.
    """
    if reason == "verify-failed" and failure_reason is not None:
        return RUNTIME_REPAIR_HINTS.get(f"verify-failed.{failure_reason}")
    return RUNTIME_REPAIR_HINTS.get(reason)


@dataclass(frozen=True)
class RepositorySummary:
    """Ordered declared and resolved identity for one workspace repository."""

    alias: str
    path: str
    origin: str
    resolved_root: str | None = None


@dataclass(frozen=True)
class PreflightReport:
    """Aggregated preflight result for a plan directory."""

    plan_dir: Path
    findings: tuple[PreflightFinding, ...] = ()
    snapshot: PlanSnapshot | None = None
    readiness: ReadinessMetadata | None = None
    expected_readiness_sha256: str | None = None
    contracts_by_session: dict[str, CompletionContract] = field(default_factory=dict)
    repository_registry: workspaces.RepositoryRegistry | None = None
    verification_context: VerificationContext | None = None
    repositories: tuple[RepositorySummary, ...] = ()
    repository_footprint: repository_scope.RepositoryFootprint | None = None
    repository_intent: RepositoryIntent | None = None

    @property
    def ok(self) -> bool:
        return not any(
            finding.severity in {"red", "yellow"} for finding in self.findings
        )

    @property
    def red_findings(self) -> tuple[PreflightFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "red")

    @property
    def yellow_findings(self) -> tuple[PreflightFinding, ...]:
        return tuple(
            finding for finding in self.findings if finding.severity == "yellow"
        )

    @property
    def green_findings(self) -> tuple[PreflightFinding, ...]:
        return tuple(
            finding for finding in self.findings if finding.severity == "green"
        )

    @property
    def repair_hints(self) -> tuple[RepairHint, ...]:
        """One :class:`RepairHint` per finding, in finding order."""
        return tuple(repair_hint_for_finding(f) for f in self.findings)


def _finding(
    severity: PreflightSeverity,
    category: PreflightCategory,
    message: str,
    *,
    session_id: str | None = None,
    path: str | None = None,
    line: int | None = None,
    detail: str = "",
) -> PreflightFinding:
    return PreflightFinding(
        severity=severity,
        category=category,
        message=message,
        session_id=session_id,
        path=path,
        line=line,
        detail=detail,
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


def _repository_summaries(
    registry: workspaces.RepositoryRegistry | None,
    context: VerificationContext | None = None,
) -> tuple[RepositorySummary, ...]:
    if registry is None:
        return ()
    resolved_roots: dict[str, str] = {}
    if context is not None and context.workspace is not None:
        resolved_roots = {
            repository.alias: str(repository.root)
            for repository in context.workspace.repositories
        }
    return tuple(
        RepositorySummary(
            alias=repository.alias,
            path=repository.path,
            origin=repository.origin,
            resolved_root=resolved_roots.get(repository.alias),
        )
        for repository in registry
    )


def _check_repository_registry(
    canonical_dir: Path,
) -> tuple[
    tuple[PreflightFinding, ...],
    workspaces.RepositoryRegistry | None,
]:
    manifest_path = canonical_dir / "manifest.md"
    if not manifest_path.is_file():
        return (), None
    try:
        registry = read_repository_registry(canonical_dir)
    except PlanError as error:
        message = str(error)
        category: PreflightCategory = (
            "repository-overlap"
            if "overlap" in message or "paths must be unique" in message
            else "repository-registry"
        )
        return (
            _finding(
                "red",
                category,
                message,
                path=str(manifest_path),
            ),
        ), None
    return (), registry


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
                f"plan directory does not exist: {canonical_dir}",
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


def _workspace_error_category(message: str) -> PreflightCategory:
    if "origin" in message:
        return "repository-origin"
    if "overlap" in message:
        return "repository-overlap"
    if "workspace root" in message or "outside the workspace" in message:
        return "workspace-root"
    return "repository-root"


def _resolve_preflight_context(
    *,
    plan_dir: Path,
    cwd: str | Path | None,
    registry: workspaces.RepositoryRegistry | None,
) -> tuple[
    VerificationContext | None,
    tuple[PreflightFinding, ...],
    tuple[RepositorySummary, ...],
]:
    summaries = _repository_summaries(registry)
    if cwd is None:
        return None, (), summaries

    canonical_cwd = canonicalize_path(cwd)
    if registry is None:
        if not canonical_cwd.is_dir():
            return None, (), summaries
        return (
            VerificationContext.legacy(canonical_cwd, plan_dir=plan_dir),
            (),
            summaries,
        )

    if not canonical_cwd.exists():
        return (
            None,
            (
                _finding(
                    "red",
                    "workspace-root",
                    f"workspace root does not exist: {canonical_cwd}",
                    path=str(canonical_cwd),
                ),
            ),
            summaries,
        )
    if not canonical_cwd.is_dir():
        return (
            None,
            (
                _finding(
                    "red",
                    "workspace-root",
                    f"workspace root is not a directory: {canonical_cwd}",
                    path=str(canonical_cwd),
                ),
            ),
            summaries,
        )

    try:
        workspace = workspaces.resolve_workspace(canonical_cwd, registry)
    except workspaces.WorkspaceError as error:
        message = str(error)
        return (
            None,
            (
                _finding(
                    "red",
                    _workspace_error_category(message),
                    message,
                    path=str(canonical_cwd),
                ),
            ),
            summaries,
        )

    context = VerificationContext.resolved_workspace(workspace)
    summaries = _repository_summaries(registry, context)
    inventory_findings: list[PreflightFinding] = []
    registered_by_path = {
        repository.repository.path: repository for repository in workspace.repositories
    }
    for repository in registered_by_path.values():
        fingerprint = repository_evidence.capture_repository_fingerprint(
            repository.root,
            plan_dir,
        )
        unavailable = repository_evidence.repository_evidence_unavailable(fingerprint)
        if unavailable is not None or not fingerprint.get("git"):
            inventory_findings.append(
                _finding(
                    "red",
                    "repository-root",
                    "repository evidence is unavailable for registered "
                    f"repository {repository.alias}",
                    path=str(repository.root),
                    detail=str(unavailable or {"reason": "repository-unavailable"}),
                )
            )

    return context, tuple(inventory_findings), summaries


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


def _check_path_references(
    contracts_by_session: dict[str, CompletionContract],
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    for session_id in sorted(contracts_by_session):
        contract = contracts_by_session[session_id]
        for check in contract.verify.checks:
            path_value: str | None = None
            if isinstance(check, (FileExistsCheck, RegexInFileCheck)):
                path_value = check.path
            else:
                continue

            candidate = Path(path_value)
            if candidate.is_absolute():
                continue

            normalized_parts = tuple(candidate.parts)
            if ".." in normalized_parts:
                findings.append(
                    _finding(
                        "yellow",
                        "path-reference",
                        (
                            f"session {session_id} check {check.name!r} escapes "
                            "the repo root with '..'"
                        ),
                        session_id=session_id,
                        detail=f"path: {path_value}",
                    )
                )

    return tuple(findings)


def _footprint_targets_by_session(
    contracts_by_session: dict[str, CompletionContract],
    snapshot: PlanSnapshot | None,
) -> dict[str, tuple[repository_scope.PathTarget, ...]]:
    """Collect contract and prose path evidence, keyed by session id."""
    targets: dict[str, list[repository_scope.PathTarget]] = {}
    for session_id, contract in contracts_by_session.items():
        targets.setdefault(session_id, []).extend(
            repository_scope.targets_from_checks(contract.verify.checks)
        )
    if snapshot is not None:
        for status in snapshot.sessions:
            text = _read_session_text(status.session)
            if text is None:
                continue
            targets.setdefault(status.session.id, []).extend(
                repository_scope.extract_path_targets(text)
            )
    return {
        session_id: tuple(session_targets)
        for session_id, session_targets in targets.items()
    }


def plan_repository_footprint(
    plan_dir: str | Path, root: str | Path
) -> tuple[repository_scope.RepositoryFootprint | None, dict[str, Any] | None]:
    """Judge a split plan directory against ``root``, contracts included.

    Returns ``(None, None)`` for a registered plan, whose registry already
    governs where it may reach. The second element is the serialized verdict,
    non-``None`` exactly when the plan may no longer stay in legacy mode.
    """
    canonical_dir = canonicalize_path(plan_dir)
    registry = read_repository_registry(canonical_dir)
    if registry is not None:
        return None, None
    snapshot = inspect_plan(canonical_dir)
    _, contracts_by_session = _check_session_contracts(snapshot, registry)
    targets_by_session = _footprint_targets_by_session(contracts_by_session, snapshot)
    footprint = repository_scope.resolve_footprint(
        [target for targets in targets_by_session.values() for target in targets],
        root,
        plan_dir=canonical_dir,
        provenance_roots=plan_provenance_exempt_roots(canonical_dir),
    )
    payload = repository_scope.footprint_payload(
        targets_by_session, footprint, registry_present=False
    )
    return footprint, payload


def _check_repository_footprint(
    contracts_by_session: dict[str, CompletionContract],
    snapshot: PlanSnapshot | None,
    root: str | Path,
    registry: workspaces.RepositoryRegistry | None,
    plan_dir: str | Path | None = None,
) -> tuple[tuple[PreflightFinding, ...], repository_scope.RepositoryFootprint | None]:
    """Refuse a legacy plan whose evidence reaches outside its bound root.

    A registered plan is already governed by the registry, so it is skipped
    here. Absolute targets that stay inside ``root`` remain green: the
    judgement is about reach, not about path style.
    """
    if registry is not None:
        return (), None
    targets_by_session = _footprint_targets_by_session(contracts_by_session, snapshot)
    provenance_roots = plan_provenance_exempt_roots(plan_dir)
    footprint = repository_scope.resolve_footprint(
        [target for targets in targets_by_session.values() for target in targets],
        root,
        plan_dir=plan_dir,
        provenance_roots=provenance_roots,
    )
    findings: list[PreflightFinding] = []
    for session_id in sorted(targets_by_session):
        session_footprint = repository_scope.resolve_footprint(
            targets_by_session[session_id],
            footprint.root,
            plan_dir=plan_dir,
            provenance_roots=provenance_roots,
        )
        for target in session_footprint.out_of_root:
            findings.append(
                _finding(
                    "red",
                    "repository-footprint",
                    (
                        f"session {session_id} {target.kind} target escapes the "
                        "bound root"
                    ),
                    session_id=session_id,
                    line=target.line or None,
                    detail=f"{target.value} is outside {footprint.root}",
                )
            )
    if len(footprint.repo_roots) > 1:
        findings.append(
            _finding(
                "red",
                "repository-footprint",
                (
                    f"plan spans {len(footprint.repo_roots)} git repositories "
                    "but manifest.md declares no `## Repositories` registry"
                ),
                detail=", ".join(str(repo) for repo in footprint.repo_roots),
            )
        )
    return tuple(findings), footprint


def _repository_intent_candidates(
    plan_dir: Path | None,
    contracts_by_session: dict[str, CompletionContract],
    snapshot: PlanSnapshot | None,
    root: str | Path,
) -> tuple[repository_intent.IntentCandidate, ...]:
    """Collect every location a legacy plan declares it will reach.

    Unlike footprint targets, these include locations that do not exist yet:
    a contract ``path`` naming a file a session has still to create resolves
    through its nearest existing ancestor, so the repository owning it is
    nameable before the session runs.
    """
    candidates: list[repository_intent.IntentCandidate] = []
    for session_id in sorted(contracts_by_session):
        candidates.extend(
            repository_intent.candidates_from_checks(
                contracts_by_session[session_id].verify.checks
            )
        )
    if snapshot is not None:
        for status in snapshot.sessions:
            text = _read_session_text(status.session)
            if text is not None:
                candidates.extend(repository_intent.candidates_from_plan_document(text))
    if plan_dir is not None:
        for name in ("context.md", "manifest.md"):
            try:
                text = (plan_dir / name).read_text(encoding="utf-8")
            except OSError:
                continue
            candidates.extend(repository_intent.candidates_from_plan_document(text))
        try:
            metadata = load_plan_metadata(plan_dir)
        except PlanError:
            metadata = None
        if metadata is not None:
            candidates.extend(
                repository_intent.candidates_from_plan_metadata(metadata.to_dict())
            )
    candidates.extend(repository_intent.candidates_from_execution_roots([root]))
    return tuple(candidates)


def plan_provenance_exempt_roots(plan_dir: str | Path | None) -> tuple[Path, ...]:
    """Trees a plan's declared source pointer exempts from the intent gate.

    Public because the runner's per-turn packet gate needs the same answer the
    plan-level gate reaches, and both must derive it from the manifest rather
    than from a filter applied to one line shape.
    """
    if plan_dir is None:
        return ()
    return repository_intent.provenance_exempt_roots(read_split_from(plan_dir))


def _check_repository_intent(
    contracts_by_session: dict[str, CompletionContract],
    snapshot: PlanSnapshot | None,
    root: str | Path,
    registry: workspaces.RepositoryRegistry | None,
    plan_dir: Path | None = None,
) -> tuple[tuple[PreflightFinding, ...], RepositoryIntent | None]:
    """Refuse a legacy plan that intends to reach several repositories.

    A registered plan already names its repositories, so it is skipped here.
    The verdict is a property of the whole plan rather than of one session:
    the registry that is missing is a plan-level declaration.
    """
    if registry is not None:
        return (), None
    intent = repository_intent.resolve_intent(
        _repository_intent_candidates(plan_dir, contracts_by_session, snapshot, root),
        root=root,
        exempt_roots=() if plan_dir is None else (plan_dir,),
        provenance_roots=plan_provenance_exempt_roots(plan_dir),
    )
    if not intent.registry_required:
        return (), intent
    finding = _finding(
        "red",
        "repository-registry-required",
        (
            f"plan intends to reach {len(intent.git_roots)} git repositories "
            "but manifest.md declares no `## Repositories` table"
        ),
        detail=", ".join(
            repository_intent.redact_credentials(str(git_root))
            for git_root in intent.git_roots
        ),
    )
    return (finding,), intent


def plan_repository_intent(
    plan_dir: str | Path,
    root: str | Path,
    *,
    extra_exempt_roots: Iterable[str | Path] = (),
) -> tuple[RepositoryIntent | None, dict[str, Any] | None]:
    """Judge a split plan directory's repository intent against ``root``.

    Returns ``(None, None)`` for a registered plan, whose registry already
    names every repository it may reach. The second element is the serialized
    intent report, non-``None`` exactly when a registry is required.

    ``extra_exempt_roots`` names further trees that are ASL's own artifacts
    rather than plan-owned source — the runner passes its state and run
    directories, which an operator may legitimately keep inside an unrelated
    checkout.
    """
    canonical_dir = canonicalize_path(plan_dir)
    registry = read_repository_registry(canonical_dir)
    if registry is not None:
        return None, None
    snapshot = inspect_plan(canonical_dir)
    _, contracts_by_session = _check_session_contracts(snapshot, registry)
    intent = repository_intent.resolve_intent(
        _repository_intent_candidates(
            canonical_dir, contracts_by_session, snapshot, root
        ),
        root=root,
        exempt_roots=(canonical_dir, *extra_exempt_roots),
        provenance_roots=plan_provenance_exempt_roots(canonical_dir),
    )
    if not intent.registry_required:
        return intent, None
    return intent, repository_intent.intent_report(intent)


def _check_verification_scope(
    contracts_by_session: dict[str, CompletionContract],
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    for session_id in sorted(contracts_by_session):
        contract = contracts_by_session[session_id]
        for check in contract.verify.checks:
            if not isinstance(check, CommandCheck):
                continue
            diagnostic = diagnose_command_scope(check)
            if diagnostic is None:
                continue
            findings.append(
                _finding(
                    "red",
                    "verification-scope",
                    (
                        f"session {session_id} check {check.name!r} "
                        "verification command is overly broad"
                    ),
                    session_id=session_id,
                    detail=diagnostic.detail,
                )
            )

    return tuple(findings)


def _check_duplicate_verification_commands(
    contracts_by_session: dict[str, CompletionContract],
) -> tuple[PreflightFinding, ...]:
    """Flag identical command argv/cwd pairs within one session recipe."""

    findings: list[PreflightFinding] = []
    for session_id in sorted(contracts_by_session):
        contract = contracts_by_session[session_id]
        first_by_identity: dict[
            tuple[str | None, tuple[str, ...], str], CommandCheck
        ] = {}
        for check in contract.verify.checks:
            if not isinstance(check, CommandCheck):
                continue
            identity = (check.repo, check.argv, check.cwd or ".")
            first = first_by_identity.get(identity)
            if first is None:
                first_by_identity[identity] = check
                continue
            cwd = check.cwd or "."
            findings.append(
                _finding(
                    "yellow",
                    "verification-duplicate",
                    (
                        f"session {session_id} checks {first.name!r} and "
                        f"{check.name!r} run the same command"
                    ),
                    session_id=session_id,
                    detail=(f"argv: {list(check.argv)!r}; cwd: {cwd!r}"),
                )
            )
    return tuple(findings)


def _check_command_runnability(
    contracts_by_session: dict[str, CompletionContract],
    cwd: str | Path | None,
    *,
    context: VerificationContext | None = None,
    which: Any | None = None,
    module_probe: Any | None = None,
) -> tuple[PreflightFinding, ...]:
    """Flag contract command checks that cannot run in this environment.

    Skipped entirely when ``cwd is None`` (mirrors the environment cwd guard):
    without a repo root there is no interpreter/PATH context to probe against.
    Otherwise every :class:`CommandCheck` is passed to :func:`diagnose_command`
    (fail-open by design), mapping ``executable-not-found`` to a red finding and
    ``module-not-importable`` to a yellow finding. Both block ``--mark-loop-ready``.

    ``which`` and ``module_probe`` are optional injectable seams forwarded to
    :func:`diagnose_command`; when ``None`` the helper's own defaults
    (``shutil.which`` / a bounded import probe) are used.
    """
    if cwd is None:
        return ()

    verification_context = context or VerificationContext.legacy(cwd)
    diagnose_kwargs: dict[str, Any] = {}
    if which is not None:
        diagnose_kwargs["which"] = which
    if module_probe is not None:
        diagnose_kwargs["module_probe"] = module_probe

    findings: list[PreflightFinding] = []

    for session_id in sorted(contracts_by_session):
        contract = contracts_by_session[session_id]
        for check in contract.verify.checks:
            if not isinstance(check, CommandCheck):
                continue
            try:
                resolved = verification_context.resolve_check(check)
            except PlanError as error:
                findings.append(
                    _finding(
                        "red",
                        "repository-contract",
                        f"session {session_id} check {check.name!r} is invalid",
                        session_id=session_id,
                        detail=str(error),
                    )
                )
                continue
            diagnostic = diagnose_command(
                check,
                repo_root=resolved.resolved_root,
                **diagnose_kwargs,
            )
            if diagnostic is None:
                continue
            if diagnostic.reason == "executable-not-found":
                findings.append(
                    _finding(
                        "red",
                        "command-not-runnable",
                        (
                            f"session {session_id} check {check.name!r} "
                            "executable is not on PATH"
                        ),
                        session_id=session_id,
                        detail=diagnostic.detail,
                    )
                )
            else:
                findings.append(
                    _finding(
                        "yellow",
                        "command-not-runnable",
                        (
                            f"session {session_id} check {check.name!r} "
                            "module is not importable"
                        ),
                        session_id=session_id,
                        detail=diagnostic.detail,
                    )
                )

    return tuple(findings)


def _any_session_complete(snapshot: PlanSnapshot | None) -> bool:
    if snapshot is None:
        return False
    return any(session.status == "complete" for session in snapshot.sessions)


def _check_baselines(
    contracts_by_session: dict[str, CompletionContract],
    cwd: str | Path | None,
    snapshot: PlanSnapshot | None,
    *,
    plan_dir: Path,
    context: VerificationContext | None = None,
) -> tuple[PreflightFinding, ...]:
    """Evaluate recorded baseline evidence against declared expectations.

    This reads ``baseline-results.json`` only — it never runs commands, so an
    ordinary ``asl preflight`` stays fast and side-effect free (use
    ``asl baseline`` to populate the evidence). Skipped when ``cwd is None``
    (the identity key needs a working directory) or when no check declares a
    baseline expectation.

    Findings are emitted only at *initial run start* — that is, when no session
    is complete. Once implementation has begun (any session complete), baseline
    evidence is intentionally not re-validated: the repository has legitimately
    changed and a declared expected-fail check is meant to pass at final
    verification, not before. This mirrors the run readiness gate's
    "require current baseline evidence when no session is complete" rule while
    honoring "do not invalidate execution after Session 1".
    """
    if cwd is None:
        return ()
    collected = baseline.collect_baseline_checks(contracts_by_session)
    if not collected:
        return ()
    if _any_session_complete(snapshot):
        # Implementation has started; do not re-gate on baseline evidence.
        return ()

    verification_context = context or VerificationContext.legacy(cwd)
    canonical_cwd = verification_context.root
    cache = BaselineCache.load(plan_dir / BASELINE_RESULTS_FILENAME)
    refresh_hint = (
        "run `"
        + responses.format_command(
            ["asl", "baseline", str(plan_dir), "--cwd", str(canonical_cwd)]
        )
        + "`"
    )

    findings: list[PreflightFinding] = []
    seen_keys: set[str] = set()
    current_evidence = baseline.capture_baseline_repository_evidence(
        verification_context, plan_dir
    )
    for session_id, check in collected:
        key = baseline.baseline_identity_key(
            check,
            canonical_cwd,
            context=verification_context,
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        record = cache.get(key)
        expectation = getattr(check, "baseline_expectation", "skip")
        if record is None:
            findings.append(
                _finding(
                    "red",
                    "baseline-missing",
                    (
                        f"session {session_id} check {check.name!r} declares "
                        f"baseline_expectation={expectation!r} but has no "
                        "recorded evidence"
                    ),
                    session_id=session_id,
                    detail=refresh_hint,
                )
            )
            continue
        result = record.result
        if not baseline.baseline_evidence_is_current(
            result,
            verification_context,
            plan_dir,
            current_evidence=current_evidence,
        ):
            findings.append(
                _finding(
                    "yellow",
                    "baseline-stale",
                    (
                        f"session {session_id} check {check.name!r} baseline "
                        "evidence predates the current repository state"
                    ),
                    session_id=session_id,
                    detail=refresh_hint,
                )
            )
            continue
        # A record produced under a total-budget overrun is never loop-ready
        # evidence, even when it was stored as satisfied (e.g. by an older
        # `asl baseline` that only flagged the overrun without failing it).
        if bool(result.get("total_budget_exceeded", False)):
            findings.append(
                _finding(
                    "red",
                    "baseline-timeout",
                    (
                        f"session {session_id} check {check.name!r} baseline "
                        "run exceeded the total baseline budget"
                    ),
                    session_id=session_id,
                    detail=str(result.get("detail", "")),
                )
            )
            continue
        status = str(result.get("status", ""))
        if status == "timeout":
            findings.append(
                _finding(
                    "red",
                    "baseline-timeout",
                    (
                        f"session {session_id} check {check.name!r} baseline "
                        "run timed out"
                    ),
                    session_id=session_id,
                    detail=str(result.get("detail", "")),
                )
            )
            continue
        if not bool(result.get("satisfied", False)):
            findings.append(
                _finding(
                    "red",
                    "baseline-unexpected",
                    (
                        f"session {session_id} check {check.name!r} baseline "
                        f"outcome {status!r} does not satisfy "
                        f"baseline_expectation={expectation!r}"
                    ),
                    session_id=session_id,
                    detail=str(result.get("detail", "")),
                )
            )
            continue
        if bool(result.get("near_timeout", False)):
            findings.append(
                _finding(
                    "yellow",
                    "baseline-near-timeout",
                    (
                        f"session {session_id} check {check.name!r} baseline "
                        "run used over 80% of its timeout budget"
                    ),
                    session_id=session_id,
                    detail=(
                        f"{result.get('duration_seconds', 0)}s of "
                        f"{result.get('budget_seconds', 0)}s budget"
                    ),
                )
            )

    return tuple(findings)


def _format_progress_session_ids(session_ids: tuple[str, ...]) -> str:
    if not session_ids:
        return "none"
    return ", ".join(session_ids)


def _progress_journal_mismatch_detail(
    expected_completed_ids: tuple[str, ...],
    entries: tuple[ProgressEntry, ...],
) -> str:
    observed_ids = tuple(entry.session_id.value for entry in entries)
    details: list[str] = []
    if observed_ids != expected_completed_ids:
        details.append(
            "expected complete sessions: "
            f"{_format_progress_session_ids(expected_completed_ids)}; "
            "observed journal entries: "
            f"{_format_progress_session_ids(observed_ids)}"
        )
    non_complete = tuple(
        f"{entry.session_id.value}={entry.status}"
        for entry in entries
        if entry.status != "complete"
    )
    if non_complete:
        details.append(
            "non-complete journal entry statuses: " + ", ".join(non_complete)
        )
    return "; ".join(details)


def _check_progress_journal(
    snapshot: PlanSnapshot,
) -> tuple[PreflightFinding, ...]:
    """Check that the runner-managed progress journal is initialized and current.

    Emits a single ``yellow`` finding when at least one session is complete
    and the journal is missing, or when the journal exists but is
    unreadable (version mismatch, malformed), stale, duplicated,
    out-of-order, or missing completed sessions. Plans with no completed
    sessions and no journal are skipped because the runner will create the
    journal on the next successful append.
    """
    journal_path = progress_path(snapshot.plan_dir)
    expected_completed_ids = tuple(
        status.id for status in snapshot.sessions if status.status == "complete"
    )

    if not journal_path.is_file():
        if not expected_completed_ids:
            return ()
        return (
            _finding(
                "yellow",
                "progress-journal-missing",
                "progress journal not initialized; run `asl backfill-progress`",
                path=str(journal_path),
            ),
        )

    try:
        entries = read_progress_entries(snapshot.plan_dir)
    except PlanError as error:
        return (
            _finding(
                "yellow",
                "progress-journal-missing",
                (
                    "progress journal unreadable; run "
                    f"`asl backfill-progress --force`: {error}"
                ),
                path=str(journal_path),
            ),
        )
    mismatch_detail = _progress_journal_mismatch_detail(
        expected_completed_ids,
        entries,
    )
    if mismatch_detail:
        return (
            _finding(
                "yellow",
                "progress-journal-missing",
                (
                    "progress journal stale or incomplete; run "
                    "`asl backfill-progress --force`"
                ),
                path=str(journal_path),
                detail=mismatch_detail,
            ),
        )
    return ()


def run_core_preflight(plan_dir: str | Path) -> PreflightReport:
    """Run the core preflight checks against a split plan directory.

    Structural failures short-circuit deeper checks: if the plan directory is
    missing or unreadable, only a ``plan-structure`` finding is returned. When
    structure validates, the report also includes canonical session-id checks,
    manifest readiness metadata, per-session completion contracts, ambiguity
    sentinels, lightweight contract path-reference checks, and verification
    scope linting. Severity is
    surfaced via ``PreflightFinding.severity``; ``PreflightReport.ok`` is
    ``True`` only when every finding is green.
    """
    canonical_dir = canonicalize_path(plan_dir)
    findings: list[PreflightFinding] = []

    registry_findings, repository_registry = _check_repository_registry(canonical_dir)
    findings.extend(registry_findings)
    structure_findings, snapshot = _check_plan_structure(
        canonical_dir,
        repository_registry,
    )
    findings.extend(structure_findings)

    if not canonical_dir.is_dir():
        return PreflightReport(
            plan_dir=canonical_dir,
            findings=tuple(findings),
            repository_registry=repository_registry,
            repositories=_repository_summaries(repository_registry),
        )

    canonical_findings = _check_canonical_session_ids(canonical_dir)
    findings.extend(canonical_findings)

    if snapshot is None:
        return PreflightReport(
            plan_dir=canonical_dir,
            findings=tuple(findings),
            repository_registry=repository_registry,
            repositories=_repository_summaries(repository_registry),
        )

    readiness_findings, readiness, expected_hash = _check_manifest_readiness_block(
        snapshot
    )
    findings.extend(readiness_findings)

    contract_findings, contracts_by_session = _check_session_contracts(
        snapshot,
        repository_registry,
    )
    findings.extend(contract_findings)

    findings.extend(_check_ambiguity_sentinels(snapshot))
    findings.extend(_check_path_references(contracts_by_session))
    footprint_findings, footprint = _check_bound_repository_footprint(
        canonical_dir,
        contracts_by_session,
        snapshot,
        repository_registry,
    )
    findings.extend(footprint_findings)
    intent_findings, intent = _check_bound_repository_intent(
        canonical_dir,
        contracts_by_session,
        snapshot,
        repository_registry,
    )
    findings.extend(intent_findings)
    findings.extend(_check_verification_scope(contracts_by_session))
    findings.extend(_check_duplicate_verification_commands(contracts_by_session))
    findings.extend(_check_progress_journal(snapshot))

    return PreflightReport(
        plan_dir=canonical_dir,
        findings=tuple(findings),
        snapshot=snapshot,
        readiness=readiness,
        expected_readiness_sha256=expected_hash,
        contracts_by_session=contracts_by_session,
        repository_registry=repository_registry,
        repositories=_repository_summaries(repository_registry),
        repository_footprint=footprint,
        repository_intent=intent,
    )


def _check_bound_repository_footprint(
    plan_dir: Path,
    contracts_by_session: dict[str, CompletionContract],
    snapshot: PlanSnapshot | None,
    registry: workspaces.RepositoryRegistry | None,
) -> tuple[tuple[PreflightFinding, ...], repository_scope.RepositoryFootprint | None]:
    """Judge the footprint against the root the plan already recorded.

    The stored v1 ``cwd`` is the root an out-of-root target contradicts, so
    ``asl preflight <dir>`` catches the escape with no ``--cwd`` argument. With
    no v1 binding there is no root to judge against, and the check stays
    silent rather than guessing one.
    """
    if registry is not None:
        return (), None
    try:
        metadata = load_plan_metadata(plan_dir)
    except PlanError:
        return (), None
    if not isinstance(metadata, PlanMetadata):
        return (), None
    return _check_repository_footprint(
        contracts_by_session, snapshot, metadata.cwd, registry, plan_dir
    )


def _check_bound_repository_intent(
    plan_dir: Path,
    contracts_by_session: dict[str, CompletionContract],
    snapshot: PlanSnapshot | None,
    registry: workspaces.RepositoryRegistry | None,
) -> tuple[tuple[PreflightFinding, ...], RepositoryIntent | None]:
    """Judge repository intent against the root the plan already recorded.

    With no v1 binding there is no root to anchor relative declarations
    against, and the check stays silent rather than guessing one.
    """
    if registry is not None:
        return (), None
    try:
        metadata = load_plan_metadata(plan_dir)
    except PlanError:
        return (), None
    if not isinstance(metadata, PlanMetadata):
        return (), None
    return _check_repository_intent(
        contracts_by_session, snapshot, metadata.cwd, registry, plan_dir
    )


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError, OSError:
        return None


def _check_env_repo_root(cwd: Path) -> tuple[PreflightFinding, ...]:
    if not cwd.exists():
        return (
            _finding(
                "red",
                "env-repo-root",
                f"cwd does not exist: {cwd}",
                path=str(cwd),
            ),
        )
    if not cwd.is_dir():
        return (
            _finding(
                "red",
                "env-repo-root",
                f"cwd is not a directory: {cwd}",
                path=str(cwd),
            ),
        )
    return ()


def _check_env_git(
    cwd: Path,
    commit_mode: CommitMode | None,
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    inside = _run_git(cwd, ["rev-parse", "--is-inside-work-tree"])
    if inside is None:
        return (
            _finding(
                "red",
                "env-git",
                "git executable not available on PATH",
                path=str(cwd),
            ),
        )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        detail = (inside.stderr.strip() or inside.stdout.strip())[:200]
        return (
            _finding(
                "red",
                "env-git",
                f"cwd is not inside a git repository: {cwd}",
                path=str(cwd),
                detail=detail,
            ),
        )

    if commit_mode in COMMIT_MODES:
        status = _run_git(cwd, ["status", "--porcelain"])
        if status is None or status.returncode != 0:
            detail = ""
            if status is not None:
                detail = (status.stderr.strip() or status.stdout.strip())[:200]
            findings.append(
                _finding(
                    "yellow",
                    "env-git",
                    "could not inspect git status",
                    path=str(cwd),
                    detail=detail,
                )
            )
        elif status.stdout:
            findings.append(
                _finding(
                    "yellow",
                    "env-git",
                    "git worktree is dirty; commit-mode runs require a clean tree",
                    path=str(cwd),
                    detail=f"commit-mode: {commit_mode}",
                )
            )

    return tuple(findings)


def _check_registered_env_git(
    context: VerificationContext,
    commit_mode: CommitMode | None,
    snapshot: PlanSnapshot | None = None,
) -> tuple[PreflightFinding, ...]:
    if context.workspace is None or commit_mode not in COMMIT_MODES:
        return ()
    findings: list[PreflightFinding] = []
    needed = None
    committed = None
    if commit_mode == "per-session":
        try:
            if snapshot is None:
                raise PlanError("current session scope is unavailable")
            needed = plans.needed_repository_aliases(
                snapshot, context.workspace.registry
            )
            committed = plans.commit_repository_aliases(
                snapshot, context.workspace.registry
            )
        except (PlanError, ValueError, OSError) as error:
            return (_finding("red", "env-git", str(error)),)
    for repository in context.workspace.repositories:
        if needed is not None and repository.alias not in needed:
            continue
        # A tree the next session only reads is inspected but may stay dirty:
        # its commit stages write aliases only (see commit_repository_aliases).
        clean_required = committed is None or repository.alias in committed
        if needed is not None and clean_required:
            try:
                linked_roots = repository_evidence.linked_worktrees(repository.root)
                registered_roots = {r.root for r in context.workspace.repositories}
                for linked in linked_roots:
                    if linked in registered_roots:
                        continue
                    linked_status = _run_git(linked, ["status", "--porcelain"])
                    if (
                        linked_status is None
                        or linked_status.returncode != 0
                        or linked_status.stdout
                    ):
                        findings.append(
                            _finding(
                                "yellow",
                                "env-git",
                                f"needed repository {repository.alias} linked tree "
                                "is dirty or unavailable",
                                path=str(linked),
                                detail=f"commit-mode: {commit_mode}",
                            )
                        )
            except repository_evidence.RepositoryEvidenceError as error:
                findings.append(_finding("red", "env-git", str(error)))
        status = _run_git(repository.root, ["status", "--porcelain"])
        if status is None or status.returncode != 0:
            detail = ""
            if status is not None:
                detail = (status.stderr.strip() or status.stdout.strip())[:200]
            findings.append(
                _finding(
                    "yellow",
                    "env-git",
                    f"could not inspect git status for {repository.alias}",
                    path=str(repository.root),
                    detail=detail,
                )
            )
        elif status.stdout and not clean_required:
            findings.append(
                _finding(
                    "green",
                    "env-git",
                    (
                        f"repository {repository.alias} is dirty but read-only "
                        "for the next session; its changes are not committed"
                    ),
                    path=str(repository.root),
                    detail=f"commit-mode: {commit_mode}",
                )
            )
        elif status.stdout:
            findings.append(
                _finding(
                    "yellow",
                    "env-git",
                    (
                        f"repository {repository.alias} is dirty; commit-mode "
                        "runs require written registered trees to be clean"
                    ),
                    path=str(repository.root),
                    detail=f"commit-mode: {commit_mode}",
                )
            )
    return tuple(findings)


def _check_env_harness(harness: str) -> tuple[PreflightFinding, ...]:
    # Matched out of the vocabulary rather than tested against it: the match is
    # the permitted value, so the rest of the check carries a `HarnessName`
    # instead of the `str` it was handed. Unlike `as_literal` this reports
    # rather than raises — preflight's job is to hand back a red finding.
    harness_name = next(
        (name for name in harnesses.HARNESS_NAMES if name == harness), None
    )
    if harness_name is None:
        return (
            _finding(
                "red",
                "env-harness",
                f"unsupported harness: {harness!r}",
                detail=f"choices: {', '.join(harnesses.HARNESS_NAMES)}",
            ),
        )
    try:
        adapter = harnesses.get_adapter(harness_name)
    except harnesses.HarnessError as error:
        return (
            _finding(
                "red",
                "env-harness",
                f"cannot construct harness adapter for {harness!r}: {error}",
            ),
        )

    resolved = adapter.resolve_executable()
    if resolved is None:
        return (
            _finding(
                "red",
                "env-harness",
                f"{harness} executable not found on PATH",
                detail=f"executable: {adapter.executable}",
            ),
        )
    return _check_env_harness_version(harness_name, resolved)


def _check_env_harness_version(
    harness: harnesses.HarnessName, executable: str
) -> tuple[PreflightFinding, ...]:
    """Classify the resolved harness CLI against the pinned support window.

    The severities are asymmetric on purpose, and the asymmetry is the whole
    policy. A known-incompatible release is ``red``: it blocks, because a run
    whose harness is known broken produces evidence nobody can trust. An
    unrecognised release is ``green``: it is reported on every preflight and in
    the preflight JSON, but it does not block. ``yellow`` is not available for
    this - :func:`_blocking_findings` treats yellow as a blocker, so warning in
    yellow would refuse to run on every harness release that lands after this
    build, which is the outage the "unknown warns" rule exists to avoid.
    """
    compatibility = harness_compatibility.check_executable(harness, executable)
    if compatibility.status == "incompatible":
        return (
            _finding(
                "red",
                "env-harness",
                f"{harness} {compatibility.version} is known-incompatible",
                detail=compatibility.detail,
            ),
        )
    if compatibility.status == "unknown":
        return (
            _finding(
                "green",
                "env-harness",
                f"unverified {harness} version (warning, not a blocker)",
                detail=compatibility.detail,
            ),
        )
    return ()


def _check_env_lock(
    plan_dir: Path,
    state_dir: str | Path | None,
) -> tuple[PreflightFinding, ...]:
    try:
        lock_info = state.read_lock_info(plan_dir, state_dir)
    except OSError as error:
        return (
            _finding(
                "yellow",
                "env-lock",
                f"could not read lock info: {error}",
                path=str(plan_dir),
            ),
        )
    if lock_info is None:
        return ()
    if lock_info.stale:
        return (
            _finding(
                "yellow",
                "env-lock",
                f"stale lock present at {lock_info.path}",
                path=str(lock_info.path),
                detail=lock_info.reason,
            ),
        )
    return (
        _finding(
            "red",
            "env-lock",
            f"active lock held by pid {lock_info.pid} at {lock_info.path}",
            path=str(lock_info.path),
            detail=lock_info.reason,
        ),
    )


def _check_env_amx() -> tuple[PreflightFinding, ...]:
    amx_path = shutil.which("amx")
    if amx_path is None:
        return (
            _finding(
                "green",
                "env-amx",
                "amx hook indicator not on PATH (Agent Mux hooks disabled)",
                detail="optional",
            ),
        )
    return (
        _finding(
            "green",
            "env-amx",
            f"amx hook indicator available at {amx_path}",
            detail="Agent Mux hooks enabled",
        ),
    )


def _turn_budget_max_pending_sessions(max_pending_sessions: int | None) -> int:
    if max_pending_sessions is None:
        return TURN_BUDGET_DEFAULT_MAX_PENDING_SESSIONS
    if max_pending_sessions <= 0:
        raise ValueError("max_pending_sessions must be a positive integer")
    return max_pending_sessions


def check_env_turn_budget(
    snapshot: PlanSnapshot,
    *,
    max_pending_sessions: int | None = None,
) -> tuple[PreflightFinding, ...]:
    """Return the turn-budget and session-inventory findings for a snapshot.

    Public because the runner re-evaluates exactly these two after review
    repairs reopen sessions: the startup gate saw the plan as authored, and
    plan growth is what invalidates them.
    """
    total = len(snapshot.sessions)
    pending = sum(1 for status in snapshot.sessions if status.status == "pending")
    pending_limit = _turn_budget_max_pending_sessions(max_pending_sessions)
    if pending == 0:
        return (
            _finding(
                "green",
                "env-turn-budget",
                "all sessions complete; no turns required",
                detail=f"total sessions: {total}",
            ),
        )
    estimated_turns = pending * TURNS_PER_SESSION_ESTIMATE
    if pending > pending_limit:
        return (
            _finding(
                "yellow",
                "env-turn-budget",
                (
                    f"{pending} pending sessions exceeds max pending sessions "
                    f"{pending_limit}; pass --max-pending-sessions to override "
                    "or chunk the plan"
                ),
                detail=(
                    f"total sessions: {total}; rough turn budget "
                    f"~{estimated_turns} ({TURNS_PER_SESSION_ESTIMATE} turns/session); "
                    f"max pending sessions {pending_limit}"
                ),
            ),
        )
    return (
        _finding(
            "green",
            "env-turn-budget",
            (
                f"{pending} session(s) pending; rough turn budget "
                f"~{estimated_turns} turns"
            ),
            detail=(
                f"total sessions: {total}; "
                f"{TURNS_PER_SESSION_ESTIMATE} turns/session estimate; "
                f"max pending sessions {pending_limit}"
            ),
        ),
    )


def _run_environment_checks(
    *,
    plan_dir: Path,
    snapshot: PlanSnapshot | None,
    cwd: str | Path | None,
    harness: str | None,
    commit_mode: CommitMode | None,
    state_dir: str | Path | None,
    max_pending_sessions: int | None = None,
    repository_registry: workspaces.RepositoryRegistry | None = None,
    verification_context: VerificationContext | None = None,
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    if cwd is not None:
        canonical_cwd = canonicalize_path(cwd)
        if repository_registry is None:
            repo_findings = _check_env_repo_root(canonical_cwd)
            findings.extend(repo_findings)
            if not repo_findings:
                findings.extend(_check_env_git(canonical_cwd, commit_mode))
        elif verification_context is not None:
            findings.extend(
                _check_registered_env_git(verification_context, commit_mode, snapshot)
            )

    if harness is not None:
        findings.extend(_check_env_harness(harness))

    findings.extend(_check_env_lock(plan_dir, state_dir))
    findings.extend(_check_env_amx())

    if snapshot is not None:
        findings.extend(
            check_env_turn_budget(
                snapshot,
                max_pending_sessions=max_pending_sessions,
            )
        )

    return tuple(findings)


def run_environment_preflight(
    plan_dir: str | Path,
    *,
    cwd: str | Path | None = None,
    harness: str | None = None,
    commit_mode: CommitMode | None = None,
    state_dir: str | Path | None = None,
    max_pending_sessions: int | None = None,
) -> PreflightReport:
    """Run only the environment preflight checks against a plan directory.

    Environment checks are compositional: ``cwd``, ``harness``, and
    ``commit_mode`` are all optional, and each one only triggers the
    checks that depend on it. Lock, ``amx``, and turn-budget checks run
    whenever the plan directory exists. Core plan checks (structure,
    contracts, readiness) are intentionally **not** invoked here; use
    ``run_preflight`` to aggregate both.
    """
    canonical_dir = canonicalize_path(plan_dir)
    registry_findings, repository_registry = _check_repository_registry(canonical_dir)
    snapshot: PlanSnapshot | None = None
    if canonical_dir.is_dir():
        try:
            snapshot = inspect_plan(
                canonical_dir,
                repository_registry=repository_registry,
            )
        except PlanError:
            snapshot = None

    verification_context, workspace_findings, repositories = _resolve_preflight_context(
        plan_dir=canonical_dir,
        cwd=cwd,
        registry=repository_registry,
    )

    env_findings = _run_environment_checks(
        plan_dir=canonical_dir,
        snapshot=snapshot,
        cwd=cwd,
        harness=harness,
        commit_mode=commit_mode,
        state_dir=state_dir,
        max_pending_sessions=max_pending_sessions,
        repository_registry=repository_registry,
        verification_context=verification_context,
    )
    return PreflightReport(
        plan_dir=canonical_dir,
        findings=registry_findings + workspace_findings + env_findings,
        snapshot=snapshot,
        repository_registry=repository_registry,
        verification_context=verification_context,
        repositories=repositories,
    )


RESUME_READINESS_STATES: tuple[ReadinessState, ...] = ("loop-ready", "running")
_PREFLIGHT_REGISTRY_UNSET = object()


def run_resume_preflight(
    plan_dir: str | Path,
    *,
    snapshot: PlanSnapshot | None,
    cwd: str | Path | None = None,
    harness: str | None = None,
    commit_mode: CommitMode | None = None,
    state_dir: str | Path | None = None,
    max_pending_sessions: int | None = None,
) -> PreflightReport | None:
    """Run an environment-only preflight when plan checks are redundant.

    Eligible only when a plan snapshot is supplied, the manifest carries
    parseable readiness metadata whose state is in
    :data:`RESUME_READINESS_STATES`, and the recorded readiness SHA-256 still
    matches the locally recomputed stable hash. That equality proves the plan
    files have not changed since preflight last cleared them, so every check
    whose only inputs are those files repeats a known-good verdict: contract
    parsing, ambiguity sentinels, path references, verification-scope linting,
    and the per-command runnability probes (~95% of a full preflight).

    The checks whose inputs live *outside* the readiness hash — the progress
    journal, recorded baseline evidence, and the repository footprint, which
    depends on the run's ``cwd`` rather than on plan content alone — are not
    covered by that proof and are not skipped by this optimization; they move
    to :func:`run_unhashed_state_checks`, which a caller runs to decide whether
    this reduced report may be trusted.

    Returns ``None`` when any precondition fails so callers fall back to the
    full :func:`run_preflight`. ``contracts_by_session`` is always empty: the
    runner re-reads the contract of each session as it starts it.
    """
    if snapshot is None:
        return None

    try:
        _, readiness, expected_hash = _check_manifest_readiness_block(snapshot)
    except PlanError:
        return None
    if readiness is None:
        return None
    if readiness.state not in RESUME_READINESS_STATES:
        return None
    if readiness.readiness_sha256 != expected_hash:
        return None

    canonical_dir = canonicalize_path(plan_dir)
    try:
        repository_registry = read_repository_registry(canonical_dir)
    except PlanError:
        return None
    verification_context, workspace_findings, repositories = _resolve_preflight_context(
        plan_dir=canonical_dir,
        cwd=cwd,
        registry=repository_registry,
    )
    findings = _run_environment_checks(
        plan_dir=canonical_dir,
        snapshot=snapshot,
        cwd=cwd,
        harness=harness,
        commit_mode=commit_mode,
        state_dir=state_dir,
        max_pending_sessions=max_pending_sessions,
        repository_registry=repository_registry,
        verification_context=verification_context,
    )
    return PreflightReport(
        plan_dir=canonical_dir,
        findings=workspace_findings + findings,
        snapshot=snapshot,
        readiness=readiness,
        expected_readiness_sha256=expected_hash,
        contracts_by_session={},
        repository_registry=repository_registry,
        verification_context=verification_context,
        repositories=repositories,
    )


def run_unhashed_state_checks(
    plan_dir: str | Path,
    *,
    snapshot: PlanSnapshot,
    cwd: str | Path | None = None,
    repository_registry: workspaces.RepositoryRegistry
    | None
    | object = _PREFLIGHT_REGISTRY_UNSET,
    context: VerificationContext | None = None,
) -> tuple[PreflightFinding, ...]:
    """Check the plan state that ``stable_readiness_sha256`` does not cover.

    ``progress.md`` and ``baseline-results.json`` are deliberately outside the
    readiness hash, so a matching SHA proves nothing about them. A caller that
    accepts a :func:`run_resume_preflight` report has to confirm them here or
    it silently drops the runner's progress auto-repair and its
    stale-baseline gate. Completion contracts are re-parsed because the
    baseline check needs them and a malformed contract must stay fail-closed.
    The repository footprint is rechecked for the same reason: the readiness
    hash covers the plan, not the ``cwd`` the run binds it to.

    Any returned finding means the reduced report cannot stand in for a full
    one, so the caller falls back to :func:`run_preflight`.
    """
    canonical_dir = canonicalize_path(plan_dir)
    if repository_registry is _PREFLIGHT_REGISTRY_UNSET:
        try:
            resolved_registry = read_repository_registry(canonical_dir)
        except PlanError as error:
            return (
                _finding(
                    "red",
                    "repository-registry",
                    str(error),
                    path=str(canonical_dir / "manifest.md"),
                ),
            )
    else:
        resolved_registry = repository_registry
    if resolved_registry is not None and not isinstance(
        resolved_registry,
        workspaces.RepositoryRegistry,
    ):
        raise TypeError("repository_registry must be a RepositoryRegistry or None")
    workspace_findings: tuple[PreflightFinding, ...] = ()
    verification_context = context
    if verification_context is None:
        verification_context, workspace_findings, _repositories = (
            _resolve_preflight_context(
                plan_dir=canonical_dir,
                cwd=cwd,
                registry=resolved_registry,
            )
        )
    contract_findings, contracts_by_session = _check_session_contracts(
        snapshot,
        resolved_registry,
    )
    footprint_findings: tuple[PreflightFinding, ...] = ()
    intent_findings: tuple[PreflightFinding, ...] = ()
    if cwd is not None:
        footprint_findings, _footprint = _check_repository_footprint(
            contracts_by_session,
            snapshot,
            cwd,
            resolved_registry,
            canonical_dir,
        )
        intent_findings, _intent = _check_repository_intent(
            contracts_by_session,
            snapshot,
            cwd,
            resolved_registry,
            canonical_dir,
        )
    baseline_findings: tuple[PreflightFinding, ...] = ()
    if resolved_registry is None or verification_context is not None:
        baseline_findings = _check_baselines(
            contracts_by_session,
            cwd,
            snapshot,
            plan_dir=canonical_dir,
            context=verification_context,
        )
    return (
        workspace_findings
        + contract_findings
        + footprint_findings
        + intent_findings
        + _check_progress_journal(snapshot)
        + baseline_findings
    )


def run_preflight(
    plan_dir: str | Path,
    *,
    cwd: str | Path | None = None,
    harness: str | None = None,
    commit_mode: CommitMode | None = None,
    state_dir: str | Path | None = None,
    max_pending_sessions: int | None = None,
) -> PreflightReport:
    """Aggregate core and environment preflight findings into one report.

    Core findings are produced first via ``run_core_preflight`` and reuse
    the same plan snapshot when feeding the environment checks, so
    ``inspect_plan`` runs at most once per call.
    """
    core_report = run_core_preflight(plan_dir)
    verification_context, workspace_findings, repositories = _resolve_preflight_context(
        plan_dir=core_report.plan_dir,
        cwd=cwd,
        registry=core_report.repository_registry,
    )
    env_findings = _run_environment_checks(
        plan_dir=core_report.plan_dir,
        snapshot=core_report.snapshot,
        cwd=cwd,
        harness=harness,
        commit_mode=commit_mode,
        state_dir=state_dir,
        max_pending_sessions=max_pending_sessions,
        repository_registry=core_report.repository_registry,
        verification_context=verification_context,
    )
    footprint_findings: tuple[PreflightFinding, ...] = ()
    footprint = core_report.repository_footprint
    if footprint is None and cwd is not None:
        footprint_findings, footprint = _check_repository_footprint(
            core_report.contracts_by_session,
            core_report.snapshot,
            cwd,
            core_report.repository_registry,
            core_report.plan_dir,
        )
    intent_findings: tuple[PreflightFinding, ...] = ()
    intent = core_report.repository_intent
    if intent is None and cwd is not None:
        intent_findings, intent = _check_repository_intent(
            core_report.contracts_by_session,
            core_report.snapshot,
            cwd,
            core_report.repository_registry,
            core_report.plan_dir,
        )
    command_findings: tuple[PreflightFinding, ...] = ()
    baseline_findings: tuple[PreflightFinding, ...] = ()
    if core_report.repository_registry is None or verification_context is not None:
        command_findings = _check_command_runnability(
            core_report.contracts_by_session,
            cwd,
            context=verification_context,
        )
        baseline_findings = _check_baselines(
            core_report.contracts_by_session,
            cwd,
            core_report.snapshot,
            plan_dir=core_report.plan_dir,
            context=verification_context,
        )
    return PreflightReport(
        plan_dir=core_report.plan_dir,
        findings=(
            core_report.findings
            + footprint_findings
            + intent_findings
            + workspace_findings
            + env_findings
            + command_findings
            + baseline_findings
        ),
        snapshot=core_report.snapshot,
        readiness=core_report.readiness,
        expected_readiness_sha256=core_report.expected_readiness_sha256,
        contracts_by_session=core_report.contracts_by_session,
        repository_registry=core_report.repository_registry,
        verification_context=verification_context,
        repositories=repositories,
        repository_footprint=footprint,
        repository_intent=intent,
    )


PREFLIGHT_EXIT_REASON_OK = "ok"
PREFLIGHT_EXIT_REASON_RED = "preflight-red"
PREFLIGHT_EXIT_REASON_YELLOW = "preflight-yellow"
PREFLIGHT_EXIT_REASON_MARKED = "loop-ready-marked"
PREFLIGHT_EXIT_REASON_MARK_REFUSED = "loop-ready-refused"
PREFLIGHT_EXIT_REASON_MARK_FAILED = "loop-ready-write-failed"


@dataclass(frozen=True)
class AutoRepair:
    """Structured record of an auto-repair action run during preflight.

    Surfaced through the ``auto_repairs`` JSON payload field so callers
    (including the runner-managed loop-ready gate planned for session 3)
    can mirror the shape when reporting their own readiness-gate repairs.

    ``target_state`` is populated for ``manifest-readiness`` rewrites so
    consumers can tell whether the repair wrote ``"loop-ready"`` (the
    default) or preserved a ``"complete"`` readiness marker for review-
    only complete starts. Repairs that do not change readiness state
    (e.g. ``progress-journal-missing``) leave it as the empty string.
    """

    action: str
    category: PreflightCategory
    path: str
    entries_written: int
    completed_session_ids: tuple[str, ...]
    journal_replaced: bool
    target_state: str = ""


# ── Repair-hint command substitution ───────────────────────────────────────
#
# Static hints in ``REPAIR_HINTS``/``_CATEGORY_HINT_OVERRIDES`` carry
# ``<plan-dir>`` (and ``<repo-root>`` for baseline evidence) placeholders in
# any embedded command so a single definition works for every plan. The
# serialized payload substitutes the concrete plan directory (and cwd) so
# consuming skills receive a ready-to-run command, not a template. Each
# substituted path is shell-quoted (:func:`shlex.quote`) so a plan or repo
# path containing whitespace or shell metacharacters stays a single argument
# in the emitted command; ordinary paths are left untouched.


def _substitute_command_placeholders(
    text: str, *, plan_dir: str, cwd: str | None
) -> str:
    result = text.replace("<plan-dir>", shlex.quote(plan_dir))
    if cwd is not None:
        result = result.replace("<repo-root>", shlex.quote(cwd))
    return result


def repair_hint_payload(
    hint: RepairHint, *, plan_dir: str, cwd: str | None = None
) -> dict[str, Any]:
    """Serialize a :class:`RepairHint`, substituting command placeholders.

    ``<plan-dir>`` in the hint ``message``/``question_template`` becomes the
    concrete plan directory and ``<repo-root>`` becomes ``cwd`` (left as the
    placeholder when ``cwd`` is ``None``), so every emitted hint embeds its
    exact plan-dir-substituted command.
    """
    data = asdict(hint)
    data["message"] = _substitute_command_placeholders(
        data["message"], plan_dir=plan_dir, cwd=cwd
    )
    data["question_template"] = _substitute_command_placeholders(
        data["question_template"], plan_dir=plan_dir, cwd=cwd
    )
    return data


# ── Manual-mode auto-repair plan (mirrors session-run Step 4a) ──────────────
#
# ``asl preflight --json`` emits an ordered ``auto_repair_plan`` and a
# ``suggested_pause`` so a skill can drive the exact bounded repairs that
# ``skills/session-run/SKILL.md`` Step 4a prescribes, without re-encoding the
# ordering or pause vocabulary in prose:
#   1. a required registry -> `asl register-repositories <plan-dir> --cwd
#      <bound-root> --json` then `asl prepare <plan-dir> --resume
#      --rebind-workspace --cwd <workspace-root>`; the migration writes the
#      plan files and the rebind republishes the workspace-bound evidence.
#   2. stale env-lock -> `asl unlock <plan-dir> --json` (one attempt); an
#      active lock never unlocks/`--force` — it emits a ``blocked`` step and a
#      ``lock-contention`` suggested pause.
#   3. progress-journal-missing -> `asl backfill-progress --force <plan-dir>`.
#   4. readiness-only residue -> `asl preflight <plan-dir> --json
#      --mark-loop-ready`.
# Any residual blocker the plan cannot clear becomes an ``unsafe`` suggested
# pause so the skill emits ``RUNNER_PAUSE: <reason> - <detail>`` verbatim.

LOCK_CONTENTION_PAUSE_DETAIL = (
    "active plan lock remains; wait for the runner or unlock manually if safe"
)

_AUTO_REPAIR_HANDLED_LOCK_IDS: frozenset[str] = frozenset(
    {"env-lock.active", "env-lock.stale"}
)

#: Categories the registry-migration steps clear. Both describe one condition —
#: a legacy plan reaching repositories its single bound root does not cover — so
#: ``asl register-repositories`` plus the rebind it prints answers both at once.
#: They are only dropped from the residual blockers when those steps are
#: actually emitted (see :func:`_registry_repair`).
_AUTO_REPAIR_HANDLED_CATEGORIES: frozenset[str] = frozenset(
    {"repository-footprint", "repository-registry-required"}
)


def _blocking_findings(
    report: PreflightReport,
) -> tuple[PreflightFinding, ...]:
    return tuple(
        finding for finding in report.findings if finding.severity in {"red", "yellow"}
    )


def _residual_blocking_findings(
    report: PreflightReport, *, registry_handled: bool = False
) -> tuple[PreflightFinding, ...]:
    """Blockers not already handled by the lock/progress repair steps.

    ``registry_handled`` additionally drops
    :data:`_AUTO_REPAIR_HANDLED_CATEGORIES`; it defaults to ``False`` so a
    caller that cannot say whether the registry steps were emitted keeps
    reporting the blocker.
    """
    handled_categories = (
        _AUTO_REPAIR_HANDLED_CATEGORIES if registry_handled else frozenset()
    )
    return tuple(
        finding
        for finding in _blocking_findings(report)
        if _override_check_id_for_finding(finding) not in _AUTO_REPAIR_HANDLED_LOCK_IDS
        and finding.category != "progress-journal-missing"
        and finding.category not in handled_categories
    )


def _command_repair_step(
    argv: Sequence[str], *, category: str, pause_reason: str
) -> dict[str, Any]:
    """Build one bounded repair step from an authoritative argv list.

    ``argv`` is the single source of truth: it is surfaced verbatim as the
    ``argv`` field (so a machine consumer never re-parses a string) and
    rendered into the human/shell ``command`` via the shared shell-safe
    :func:`agent_session_loop.responses.format_command` builder, so a plan path
    with whitespace or shell metacharacters round-trips as one argument.
    """
    return {
        "command": responses.format_command(list(argv)),
        "argv": list(argv),
        "max_attempts": 1,
        "category": category,
        "pause_reason": pause_reason,
    }


@dataclass(frozen=True)
class _RegistryRepair:
    """Whether preflight may advertise the registry migration, and why not.

    ``missing_fact`` is the fact the derivation refused for, so the fallback
    pause can name the one thing a human has to supply.
    """

    steps: tuple[dict[str, Any], ...] = ()
    missing_fact: str | None = None


def _registry_repair(
    report: PreflightReport,
    *,
    plan_dir: str,
    cwd: str | None,
    state_dir: str | None,
) -> _RegistryRepair:
    """The registry migration for a registry-less multi-repository plan.

    Admissibility is the migration's own: preflight runs the same read-only
    derivation ``asl register-repositories`` runs rather than judging from
    repository topology, so a plan the command would decline — an unobservable
    ``origin``, a contract check under no registered repository, a completed
    handoff a registry would invalidate — never gets a step advertised for it.
    A refusal emits neither step and reports its missing fact instead. The
    rebind's workspace root comes from that same derivation, so the command
    mirrors the first entry of the migration's own ``next_commands``.
    """
    if cwd is None:
        return _RegistryRepair()
    driving = next(
        (
            finding.category
            for finding in _blocking_findings(report)
            if finding.category in _AUTO_REPAIR_HANDLED_CATEGORIES
        ),
        None,
    )
    if driving is None:
        return _RegistryRepair()

    from . import repository_registration  # local: it imports preflight

    try:
        derived = repository_registration.derive_registration(plan_dir, cwd=cwd)
    except PlanError, OSError, workspaces.WorkspaceError:
        # Fails closed: the command reads the same files and would fail too.
        return _RegistryRepair()
    if not isinstance(derived, repository_registration.RegistrationPlan):
        return _RegistryRepair(missing_fact=derived.missing_fact)

    register = ["asl", "register-repositories", plan_dir, "--cwd", cwd]
    rebind = [
        "asl",
        "prepare",
        plan_dir,
        "--resume",
        "--rebind-workspace",
        "--cwd",
        str(derived.workspace_root),
    ]
    if state_dir is not None:
        register.extend(["--state-dir", state_dir])
        rebind.extend(["--state-dir", state_dir])
    register.append("--json")
    return _RegistryRepair(
        steps=tuple(
            _command_repair_step(argv, category=driving, pause_reason="unsafe")
            for argv in (register, rebind)
        )
    )


def build_auto_repair_plan(
    report: PreflightReport,
    *,
    plan_dir: str,
    cwd: str | None,
    state_dir: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Ordered bounded repairs mirroring session-run Step 4a, or ``()``.

    Empty when the report is already ok. An active lock short-circuits to a
    single ``blocked`` step (never unlock, never ``--force``). Otherwise the
    plan leads with the registry migration when the derivation that migration
    runs returns a plan, then lists the applicable stale-lock,
    progress-journal, and readiness repairs in Step 4a order, each with
    ``max_attempts: 1``.
    """
    if report.ok:
        return ()

    blocking = _blocking_findings(report)
    active_lock = None
    stale_lock = None
    for finding in blocking:
        override_id = _override_check_id_for_finding(finding)
        if override_id == "env-lock.active":
            active_lock = finding
        elif override_id == "env-lock.stale":
            stale_lock = finding

    if active_lock is not None:
        return (
            {
                "blocked": True,
                "pause_reason": "lock-contention",
                "detail": LOCK_CONTENTION_PAUSE_DETAIL,
            },
        )

    registry = _registry_repair(report, plan_dir=plan_dir, cwd=cwd, state_dir=state_dir)
    steps: list[dict[str, Any]] = list(registry.steps)
    if stale_lock is not None:
        steps.append(
            _command_repair_step(
                ["asl", "unlock", plan_dir, "--json"],
                category="env-lock",
                pause_reason="lock-contention",
            )
        )
    if any(finding.category == "progress-journal-missing" for finding in blocking):
        steps.append(
            _command_repair_step(
                ["asl", "backfill-progress", "--force", plan_dir],
                category="progress-journal-missing",
                pause_reason="unsafe",
            )
        )

    residual = _residual_blocking_findings(
        report, registry_handled=bool(registry.steps)
    )
    readiness = [f for f in residual if f.category == "manifest-readiness"]
    hard = [f for f in residual if f.category != "manifest-readiness"]
    if readiness and not hard:
        steps.append(
            _command_repair_step(
                ["asl", "preflight", plan_dir, "--json", "--mark-loop-ready"],
                category="manifest-readiness",
                pause_reason="unsafe",
            )
        )
    return tuple(steps)


def build_suggested_pause(
    report: PreflightReport,
    *,
    plan_dir: str,
    cwd: str | None = None,
    state_dir: str | None = None,
) -> dict[str, str] | None:
    """The pause a skill should emit if the auto-repair plan cannot clear it.

    ``None`` when the report is ok or every blocker is auto-repairable
    (stale lock, progress journal, readiness-only, or a registry migration the
    plan emits). An active lock yields a ``lock-contention`` pause; any other
    residual blocker yields ``unsafe`` with the failing categories named —
    matching Step 4a's fallback pause. A registry migration the derivation
    refused adds the fact it refused for, which is the one thing a human has
    to supply before the category can clear.
    """
    if report.ok:
        return None
    blocking = _blocking_findings(report)
    if any(
        _override_check_id_for_finding(finding) == "env-lock.active"
        for finding in blocking
    ):
        return {
            "reason": "lock-contention",
            "detail": LOCK_CONTENTION_PAUSE_DETAIL,
        }
    registry = _registry_repair(report, plan_dir=plan_dir, cwd=cwd, state_dir=state_dir)
    residual = _residual_blocking_findings(
        report, registry_handled=bool(registry.steps)
    )
    hard = sorted({f.category for f in residual if f.category != "manifest-readiness"})
    if hard:
        detail = f"automatic plan repair failed: {', '.join(hard)}"
        if registry.missing_fact is not None:
            detail += (
                "; the registry migration refuses for missing fact "
                f"{registry.missing_fact}"
            )
        return {"reason": "unsafe", "detail": detail}
    return None


def _finding_payload(finding: PreflightFinding) -> dict[str, Any]:
    return asdict(finding)


def _readiness_payload(readiness: ReadinessMetadata | None) -> dict[str, Any] | None:
    if readiness is None:
        return None
    return asdict(readiness)


def _counts_by_severity(report: PreflightReport) -> dict[str, int]:
    return {
        "red": len(report.red_findings),
        "yellow": len(report.yellow_findings),
        "green": len(report.green_findings),
    }


def _primary_error_reason(report: PreflightReport) -> str:
    if report.red_findings:
        return PREFLIGHT_EXIT_REASON_RED
    return PREFLIGHT_EXIT_REASON_YELLOW


def _preflight_payload_fields(
    report: PreflightReport,
    *,
    plan_dir_arg: str,
    cwd: str | None,
    harness: str | None,
    commit_mode: str | None,
    state_dir: str | None,
    max_pending_sessions: int | None,
    mark_loop_ready: bool,
    marked: bool | None = None,
    mark_error: str | None = None,
    auto_repairs: tuple[AutoRepair, ...] = (),
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "plan_dir": plan_dir_arg,
        "plan_dir_resolved": str(report.plan_dir),
        "cwd": cwd,
        "harness": harness,
        "commit_mode": commit_mode,
        "state_dir": state_dir,
        "max_pending_sessions": max_pending_sessions,
        "ok": report.ok,
        "findings": [_finding_payload(finding) for finding in report.findings],
        "findings_by_severity": _counts_by_severity(report),
        "readiness": _readiness_payload(report.readiness),
        "expected_readiness_sha256": report.expected_readiness_sha256,
        "mark_loop_ready_requested": mark_loop_ready,
        "marked_loop_ready": bool(marked) if marked is not None else False,
        "repair_hints": [
            repair_hint_payload(hint, plan_dir=plan_dir_arg, cwd=cwd)
            for hint in report.repair_hints
        ],
        "auto_repairs": [asdict(repair) for repair in auto_repairs],
        "auto_repair_plan": list(
            build_auto_repair_plan(
                report, plan_dir=plan_dir_arg, cwd=cwd, state_dir=state_dir
            )
        ),
        "suggested_pause": build_suggested_pause(
            report, plan_dir=plan_dir_arg, cwd=cwd, state_dir=state_dir
        ),
    }
    if report.repository_registry is not None:
        fields.update(
            {
                "workspace_root": (
                    str(report.verification_context.root)
                    if report.verification_context is not None
                    else cwd
                ),
                "registry_digest": report.repository_registry.digest,
                "repositories": [
                    asdict(repository) for repository in report.repositories
                ],
            }
        )
    footprint_payload = _repository_footprint_payload(report)
    if footprint_payload is not None:
        fields["repository_footprint"] = footprint_payload
    intent_payload = _repository_intent_payload(report)
    if intent_payload is not None:
        fields["repository_intent"] = intent_payload
    if mark_error is not None:
        fields["mark_error"] = mark_error
    return fields


def _repository_footprint_payload(report: PreflightReport) -> dict[str, Any] | None:
    if report.repository_footprint is None:
        return None
    return repository_scope.footprint_payload(
        _footprint_targets_by_session(report.contracts_by_session, report.snapshot),
        report.repository_footprint,
        registry_present=report.repository_registry is not None,
    )


def _repository_intent_payload(report: PreflightReport) -> dict[str, Any] | None:
    """Serialize intent only when it is the reason a plan is refused.

    Every plan has an intent; only a multi-repository one is news, and the
    report is bounded so a wide plan cannot flood the payload.
    """
    if report.repository_intent is None:
        return None
    if not report.repository_intent.registry_required:
        return None
    return repository_intent.intent_report(report.repository_intent)


def _format_finding_text(finding: PreflightFinding) -> str:
    severity_marker = f"[{finding.severity.upper()}]"
    session_label = f" {finding.session_id}" if finding.session_id else ""
    line = f"  {severity_marker} {finding.category}{session_label}: {finding.message}"
    if finding.path:
        location = finding.path
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        line += f" ({location})"
    return line


def _print_text_preflight(
    report: PreflightReport,
    *,
    plan_dir_arg: str,
    cwd: str | None,
    harness: str | None,
    commit_mode: str | None,
    max_pending_sessions: int | None,
    marked: bool,
    mark_loop_ready: bool,
    mark_error: str | None,
    auto_repairs: tuple[AutoRepair, ...] = (),
) -> None:
    counts = _counts_by_severity(report)
    session_count = len(report.snapshot.sessions) if report.snapshot is not None else 0
    print(f"Plan: {plan_dir_arg}")
    print(f"Sessions: {session_count}")
    if report.repository_registry is not None:
        if cwd is not None:
            print(f"Workspace root: {cwd}")
        print("Repositories:")
        visible, omitted = workspaces.bounded_repository_rows(report.repositories)
        for repository in visible:
            resolved = (
                f" -> {repository.resolved_root}"
                if repository.resolved_root is not None
                else ""
            )
            print(
                f"  {repository.alias}: {repository.path}{resolved} "
                f"({repository.origin})"
            )
        if omitted:
            print(f"  ... {omitted} more repositories")
    elif cwd is not None:
        print(f"Repo root: {cwd}")
    if harness is not None:
        print(f"Harness: {harness}")
    if commit_mode is not None:
        print(f"Commit mode: {commit_mode}")
    if max_pending_sessions is not None:
        print(f"Max pending sessions: {max_pending_sessions}")
    print(
        f"Findings: {len(report.findings)} "
        f"(red {counts['red']}, yellow {counts['yellow']}, green {counts['green']})"
    )
    for finding in report.findings:
        print(_format_finding_text(finding))
    for repair in auto_repairs:
        print(_format_auto_repair_text(repair))
    if report.ok:
        print("Preflight: ok")
    else:
        print(
            f"Preflight: failed ({counts['red']} red, {counts['yellow']} yellow)",
            file=sys.stderr,
        )
    if mark_loop_ready:
        if marked:
            print("Loop-ready: marked")
        elif mark_error is not None:
            print(f"Loop-ready: not marked ({mark_error})", file=sys.stderr)
        else:
            print(
                "Loop-ready: refused (preflight not ok)",
                file=sys.stderr,
            )


def _format_auto_repair_text(repair: AutoRepair) -> str:
    if repair.category == "progress-journal-missing":
        noun = "entry" if repair.entries_written == 1 else "entries"
        return (
            f"Auto-repair: rebuilt progress journal "
            f"({repair.entries_written} {noun} written)"
        )
    return f"Auto-repair: {repair.action} ({repair.category})"


def _mark_loop_ready(
    report: PreflightReport,
) -> tuple[bool, str | None]:
    non_bypassable = sorted(
        {
            finding.category
            for finding in report.findings
            if finding.severity == "red"
            and finding.category in NON_BYPASSABLE_PREFLIGHT_CATEGORIES
        }
    )
    if non_bypassable:
        return False, (
            "preflight raised a non-bypassable finding "
            f"({', '.join(non_bypassable)}); readiness cannot be marked until "
            "it is resolved"
        )
    if report.expected_readiness_sha256 is None:
        return False, "no readiness sha-256 was computed; cannot mark loop-ready"
    metadata = ReadinessMetadata(
        state="loop-ready",
        readiness_sha256=report.expected_readiness_sha256,
        checked_at=datetime.now(UTC).isoformat(),
    )
    try:
        write_readiness_metadata(report.plan_dir, metadata)
    except PlanError as error:
        return False, str(error)
    return True, None


def _mark_loop_ready_blockers(
    report: PreflightReport,
) -> tuple[PreflightFinding, ...]:
    """Return findings that must be fixed before readiness can be marked.

    Manifest-readiness findings are precisely what ``--mark-loop-ready`` repairs:
    missing metadata, stale SHA values, and draft/reviewed states. All other red
    or yellow findings still block marking.
    """
    return tuple(
        finding
        for finding in report.findings
        if finding.severity in {"red", "yellow"}
        and finding.category != "manifest-readiness"
    )


def _mark_loop_ready_refusal_message(
    blockers: tuple[PreflightFinding, ...],
) -> str:
    red = sum(1 for finding in blockers if finding.severity == "red")
    yellow = sum(1 for finding in blockers if finding.severity == "yellow")
    categories = ", ".join(sorted({finding.category for finding in blockers}))
    return (
        f"preflight still has {red} red and {yellow} yellow "
        f"non-readiness finding(s): {categories}"
    )


def preflight_from_args(args: argparse.Namespace) -> int:
    """Run the ``preflight`` subcommand from CLI arguments."""
    plan_dir_arg = args.plan_dir
    cwd_arg: str | None = args.cwd
    harness_arg: str | None = args.harness
    # The two flags feeding `commit_mode` are `store_const`, so the CLI can
    # only ever supply a permitted value; narrowing here is what makes that
    # true for a fabricated namespace too, and keeps an unrecognized mode from
    # quietly skipping the git checks that `commit_mode in COMMIT_MODES` gates.
    commit_mode_arg = as_optional_literal(
        args.commit_mode, COMMIT_MODES, "--commit-per-session/--commit-at-end"
    )
    state_dir_arg: str | None = args.state_dir
    max_pending_sessions_arg: int | None = getattr(
        args,
        "max_pending_sessions",
        None,
    )
    want_json: bool = args.json
    mark_loop_ready: bool = args.mark_loop_ready

    if max_pending_sessions_arg is not None and max_pending_sessions_arg <= 0:
        message = "--max-pending-sessions must be a positive integer"
        if want_json:
            responses.print_json(
                responses.error_payload(
                    "preflight",
                    reason="input-error",
                    error=message,
                    paused=False,
                    plan_dir=plan_dir_arg,
                    cwd=cwd_arg,
                    harness=harness_arg,
                    commit_mode=commit_mode_arg,
                    state_dir=state_dir_arg,
                    max_pending_sessions=max_pending_sessions_arg,
                )
            )
        else:
            print(f"agent-session-loop preflight: {message}", file=sys.stderr)
        return 1

    try:
        report = run_preflight(
            plan_dir_arg,
            cwd=cwd_arg,
            harness=harness_arg,
            commit_mode=commit_mode_arg,
            state_dir=state_dir_arg,
            max_pending_sessions=max_pending_sessions_arg,
        )
    except PlanError as error:
        if want_json:
            responses.print_json(
                responses.error_payload(
                    "preflight",
                    reason="plan-structure",
                    error=str(error),
                    paused=False,
                    plan_dir=plan_dir_arg,
                    cwd=cwd_arg,
                    harness=harness_arg,
                    commit_mode=commit_mode_arg,
                    state_dir=state_dir_arg,
                )
            )
        else:
            print(
                f"agent-session-loop preflight: {error}",
                file=sys.stderr,
            )
        return 1

    marked = False
    mark_error: str | None = None
    mark_write_failed = False
    auto_repairs: list[AutoRepair] = []
    if mark_loop_ready:
        blockers = _mark_loop_ready_blockers(report)
        if blockers and all(
            blocker.category == "progress-journal-missing" for blocker in blockers
        ):
            try:
                repair_result = progress_repair.repair_progress_journal(report.plan_dir)
            except PlanError, OSError:
                # Leave blockers in place; the refusal path below will surface
                # them to the caller. Auto-repair is best-effort.
                pass
            else:
                auto_repairs.append(
                    AutoRepair(
                        action=repair_result.action,
                        category="progress-journal-missing",
                        path=str(repair_result.path),
                        entries_written=repair_result.entries_written,
                        completed_session_ids=repair_result.completed_session_ids,
                        journal_replaced=repair_result.journal_replaced,
                    )
                )
                try:
                    report = run_preflight(
                        plan_dir_arg,
                        cwd=cwd_arg,
                        harness=harness_arg,
                        commit_mode=commit_mode_arg,
                        state_dir=state_dir_arg,
                        max_pending_sessions=max_pending_sessions_arg,
                    )
                except PlanError as error:
                    mark_error = str(error)
                    mark_write_failed = True
                else:
                    blockers = _mark_loop_ready_blockers(report)
        if not mark_write_failed:
            if blockers:
                mark_error = _mark_loop_ready_refusal_message(blockers)
            else:
                marked, mark_error = _mark_loop_ready(report)
                mark_write_failed = mark_error is not None
                if marked:
                    try:
                        report = run_preflight(
                            plan_dir_arg,
                            cwd=cwd_arg,
                            harness=harness_arg,
                            commit_mode=commit_mode_arg,
                            state_dir=state_dir_arg,
                            max_pending_sessions=max_pending_sessions_arg,
                        )
                    except PlanError as error:
                        mark_error = str(error)
                        mark_write_failed = True

    payload_fields = _preflight_payload_fields(
        report,
        plan_dir_arg=str(plan_dir_arg),
        cwd=cwd_arg,
        harness=harness_arg,
        commit_mode=commit_mode_arg,
        state_dir=state_dir_arg,
        max_pending_sessions=max_pending_sessions_arg,
        mark_loop_ready=mark_loop_ready,
        marked=marked,
        mark_error=mark_error,
        auto_repairs=tuple(auto_repairs),
    )

    if want_json:
        if report.ok:
            if mark_loop_ready and not marked:
                reason = (
                    PREFLIGHT_EXIT_REASON_MARK_FAILED
                    if mark_error is not None
                    else PREFLIGHT_EXIT_REASON_MARK_REFUSED
                )
                responses.print_json(
                    responses.error_payload(
                        "preflight",
                        reason=reason,
                        error=mark_error or "loop-ready marking did not complete",
                        paused=False,
                        **payload_fields,
                    )
                )
            else:
                reason = (
                    PREFLIGHT_EXIT_REASON_MARKED if marked else PREFLIGHT_EXIT_REASON_OK
                )
                responses.print_json(
                    responses.success_payload(
                        "preflight",
                        reason=reason,
                        **payload_fields,
                    )
                )
        else:
            if mark_loop_ready and mark_write_failed:
                responses.print_json(
                    responses.error_payload(
                        "preflight",
                        reason=PREFLIGHT_EXIT_REASON_MARK_FAILED,
                        error=mark_error or "loop-ready marking did not complete",
                        paused=False,
                        **payload_fields,
                    )
                )
            else:
                responses.print_json(
                    responses.error_payload(
                        "preflight",
                        reason=_primary_error_reason(report),
                        error=(
                            f"{len(report.red_findings)} red, "
                            f"{len(report.yellow_findings)} yellow finding(s)"
                        ),
                        paused=False,
                        **payload_fields,
                    )
                )
    else:
        _print_text_preflight(
            report,
            plan_dir_arg=str(plan_dir_arg),
            cwd=cwd_arg,
            harness=harness_arg,
            commit_mode=commit_mode_arg,
            max_pending_sessions=max_pending_sessions_arg,
            marked=marked,
            mark_loop_ready=mark_loop_ready,
            mark_error=mark_error,
            auto_repairs=tuple(auto_repairs),
        )

    if not report.ok:
        return 1
    if mark_loop_ready and not marked:
        return 1
    return 0
