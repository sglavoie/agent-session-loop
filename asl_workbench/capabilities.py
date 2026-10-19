"""The capability/ownership matrix for the unified Workbench.

The consolidation's primary acceptance criterion is that one implementation
survives it. A plan that says so is not enough: two applications that both
work are the failure mode, and they fail by *nobody having written down* which
one owns a behavior. So every capability either half has today gets a row
here, and every row names four things:

* **owner** -- the single place the behavior lives once the consolidation is
  done. Exactly one, from :data:`OWNERS`.
* **authority** -- who remains the source of truth behind it. ASL stays the
  authority on whether a mutation is permitted and what actually happened, so
  a row owned by the portable application usually still answers
  ``agent_session_loop`` here. Ownership and authority are different
  questions, and collapsing them is how an adapter grows into a second
  implementation.
* **compatibility_adapters** -- what must keep translating an old request into
  the owner's interface. An adapter may translate; it may not reimplement.
* **regression_coverage** -- the tests that hold the behavior today, as
  ``alias:path`` references into the registered repositories, plus an explicit
  ``coverage_gap`` where the only thing holding it is a documented manual
  check. An unnamed gap is the one this program cannot afford.

Rows for the Workbench's HTTP routes and CLI entry points are *derived*
against the live code by ``tests/test_workbench_foundations.py``: the route
table in ``agent_session_loop/workbench_server.py`` and the argument parser in
``agent_session_loop/cli.py`` are read, and a surface without a row fails.
Browser workflows, integrations, and persistence boundaries are enumerated
rather than derived -- there is nothing mechanical to read them off. Their
historical origin remains recorded, but the retired Companion modules, routes,
dashboard, fixtures, and tests are deliberately absent from this final matrix.

``python3 -m asl_workbench`` renders the matrix as Markdown; ``--json`` prints
the counts and declared gaps as data instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from . import (
    capabilities_http,
    capabilities_link,
    capabilities_shell,
    capabilities_storage,
)
from .capabilities_models import (
    AUTHORITIES as AUTHORITIES,
)
from .capabilities_models import (
    AUTHORITY_ASL as AUTHORITY_ASL,
)
from .capabilities_models import (
    AUTHORITY_HOST as AUTHORITY_HOST,
)
from .capabilities_models import (
    AUTHORITY_NONE as AUTHORITY_NONE,
)
from .capabilities_models import (
    AUTHORITY_PI_SERVICE as AUTHORITY_PI_SERVICE,
)
from .capabilities_models import (
    COVERAGE_ALIASES as COVERAGE_ALIASES,
)
from .capabilities_models import (
    GAP_DEPLOYED_HOST as GAP_DEPLOYED_HOST,
)
from .capabilities_models import (
    GAP_WORKBENCH_BROWSER as GAP_WORKBENCH_BROWSER,
)
from .capabilities_models import (
    LOCAL_COVERAGE_ALIAS as LOCAL_COVERAGE_ALIAS,
)
from .capabilities_models import (
    OPERATION_BEHAVIOR_OWNERS as OPERATION_BEHAVIOR_OWNERS,
)
from .capabilities_models import (
    ORIGIN_BOTH as ORIGIN_BOTH,
)
from .capabilities_models import (
    ORIGIN_COMPANION as ORIGIN_COMPANION,
)
from .capabilities_models import (
    ORIGIN_WORKBENCH as ORIGIN_WORKBENCH,
)
from .capabilities_models import (
    ORIGINS as ORIGINS,
)
from .capabilities_models import (
    OWNER_MAC as OWNER_MAC,
)
from .capabilities_models import (
    OWNER_PI_REPO as OWNER_PI_REPO,
)
from .capabilities_models import (
    OWNER_PORTABLE as OWNER_PORTABLE,
)
from .capabilities_models import (
    OWNERS as OWNERS,
)
from .capabilities_models import (
    REPO_ROOT as REPO_ROOT,
)
from .capabilities_models import (
    SURFACE_BRIDGE as SURFACE_BRIDGE,
)
from .capabilities_models import (
    SURFACE_INTEGRATION as SURFACE_INTEGRATION,
)
from .capabilities_models import (
    SURFACE_PERSISTENCE as SURFACE_PERSISTENCE,
)
from .capabilities_models import (
    SURFACE_WORKBENCH_BROWSER as SURFACE_WORKBENCH_BROWSER,
)
from .capabilities_models import (
    SURFACE_WORKBENCH_CLI as SURFACE_WORKBENCH_CLI,
)
from .capabilities_models import (
    SURFACE_WORKBENCH_HTTP as SURFACE_WORKBENCH_HTTP,
)
from .capabilities_models import (
    SURFACE_WORKBENCH_STATIC as SURFACE_WORKBENCH_STATIC,
)
from .capabilities_models import (
    SURFACES as SURFACES,
)
from .capabilities_models import (
    Capability as Capability,
)
from .capabilities_models import (
    MatrixError as MatrixError,
)
from .compatibility import PACKAGE_NAME
from .operations import OPERATION_METHODS

CAPABILITIES: tuple[Capability, ...] = (
    *capabilities_http.CAPABILITIES,
    *capabilities_shell.CAPABILITIES,
    *capabilities_link.CAPABILITIES,
    *capabilities_storage.CAPABILITIES,
)


# -- Queries ----------------------------------------------------------------


def capability(key: str) -> Capability:
    """The row for ``key``, or :class:`MatrixError` if the matrix lacks one."""
    for row in CAPABILITIES:
        if row.key == key:
            return row
    raise MatrixError(f"no capability named {key!r}")


def by_surface(surface: str) -> tuple[Capability, ...]:
    """Every row on one surface, in matrix order."""
    if surface not in SURFACES:
        raise MatrixError(f"no surface named {surface!r}")
    return tuple(row for row in CAPABILITIES if row.surface == surface)


def surface_keys(surface: str) -> frozenset[str]:
    """The identifiers covered on one surface, with the prefix stripped.

    This is what a completeness check compares against: a route table or an
    argument parser yields identifiers, not matrix keys.
    """
    return frozenset(row.identifier for row in by_surface(surface))


def coverage_gaps() -> tuple[Capability, ...]:
    """Every row whose coverage is a documented manual check, not a test."""
    return tuple(row for row in CAPABILITIES if row.coverage_gap is not None)


def coverage_paths(alias: str) -> tuple[str, ...]:
    """Distinct repository-relative coverage paths named under one alias.

    The final matrix names only coverage for the surviving implementation in
    ``scripts``. Historical Pi evidence is retained as evidence, not as a test
    dependency for maintained behavior.
    """
    if alias not in COVERAGE_ALIASES:
        raise MatrixError(f"no coverage alias named {alias!r}")
    prefix = f"{alias}:"
    paths = {
        reference.removeprefix(prefix)
        for row in CAPABILITIES
        for reference in row.regression_coverage
        if reference.startswith(prefix)
    }
    return tuple(sorted(paths))


def local_coverage_paths() -> tuple[str, ...]:
    """Distinct ``scripts``-relative coverage paths named anywhere in the matrix."""
    return coverage_paths(LOCAL_COVERAGE_ALIAS)


# -- Validation -------------------------------------------------------------


def _coverage_problems(row: Capability) -> list[str]:
    problems: list[str] = []
    if not row.regression_coverage:
        problems.append(f"{row.key}: names no regression coverage")
    for reference in row.regression_coverage:
        alias, separator, path = reference.partition(":")
        if not separator or alias not in COVERAGE_ALIASES:
            problems.append(
                f"{row.key}: coverage {reference!r} is not an "
                f"'alias:path' reference into {list(COVERAGE_ALIASES)}"
            )
            continue
        if not path or path.startswith("/"):
            problems.append(
                f"{row.key}: coverage {reference!r} must be repository-relative"
            )
    return problems


def validate_matrix(
    *,
    repo_root: Path | None = None,
    known_changes: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Every structural problem with the matrix, as lines naming the row.

    Returns an empty tuple when the matrix holds. ``known_changes`` is the
    intentional-change register's keys; passing it checks that no row points at
    a change nobody wrote down. ``repo_root`` resolves ``scripts:`` coverage
    references against the filesystem, so a renamed test module is a failure
    here instead of a reference that reads fine and means nothing.
    """
    root = REPO_ROOT if repo_root is None else Path(repo_root)
    changes = set(known_changes) if known_changes is not None else None
    problems: list[str] = []
    seen: set[str] = set()
    if set(OPERATION_BEHAVIOR_OWNERS) != set(OPERATION_METHODS):
        problems.append("operation ownership does not cover the closed method set")
    for method, owner in OPERATION_BEHAVIOR_OWNERS.items():
        if owner not in OWNERS:
            problems.append(f"operation {method}: unknown owner {owner!r}")
    for row in CAPABILITIES:
        if row.key in seen:
            problems.append(f"{row.key}: duplicate key")
        seen.add(row.key)
        if row.surface not in SURFACES:
            problems.append(f"{row.key}: unknown surface {row.surface!r}")
        elif not row.key.startswith(f"{row.surface}:") or not row.identifier:
            problems.append(f"{row.key}: key must be '<surface>:<identifier>'")
        if row.origin not in ORIGINS:
            problems.append(f"{row.key}: unknown origin {row.origin!r}")
        if row.owner not in OWNERS:
            problems.append(f"{row.key}: unknown owner {row.owner!r}")
        if row.authority not in AUTHORITIES:
            problems.append(f"{row.key}: unknown authority {row.authority!r}")
        if not row.summary.strip():
            problems.append(f"{row.key}: empty summary")
        problems.extend(_coverage_problems(row))
        if len(set(row.intentional_changes)) != len(row.intentional_changes):
            problems.append(f"{row.key}: lists an intentional change twice")
        if changes is not None:
            for change_key in row.intentional_changes:
                if change_key not in changes:
                    problems.append(
                        f"{row.key}: names unregistered intentional change "
                        f"{change_key!r}"
                    )
    for path in local_coverage_paths():
        if not (root / path).exists():
            problems.append(
                f"coverage path does not exist: {LOCAL_COVERAGE_ALIAS}:{path}"
            )
    return tuple(problems)


# -- Rendering --------------------------------------------------------------


def render_matrix(rows: Sequence[Capability] | None = None) -> str:
    """The matrix as Markdown tables, one per surface.

    A rendered artifact rather than a checked-in document: the data is the
    source of truth, so there is no generated file to go stale.
    """
    selected = CAPABILITIES if rows is None else tuple(rows)
    grouped: dict[str, list[Capability]] = {surface: [] for surface in SURFACES}
    for row in selected:
        grouped.setdefault(row.surface, []).append(row)
    lines = ["# Capability and ownership matrix", ""]
    for surface in SURFACES:
        present = grouped.get(surface) or []
        if not present:
            continue
        lines.append(f"## {surface}")
        lines.append("")
        lines.append(
            "| Capability | Origin | Owner | Authority | Adapters | Coverage "
            "| Intentional change |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for row in present:
            adapters = "; ".join(row.compatibility_adapters) or "-"
            coverage = "; ".join(row.regression_coverage) or "-"
            if row.coverage_gap is not None:
                coverage = f"{coverage} (gap: {row.coverage_gap})"
            changed = "; ".join(row.intentional_changes) or "-"
            lines.append(
                f"| `{row.identifier}` | {row.origin} | {row.owner} | "
                f"{row.authority} | {adapters} | {coverage} | {changed} |"
            )
        lines.append("")
    return "\n".join(lines)


def summary_counts() -> dict[str, int]:
    """Row counts per surface, for a one-line inventory check."""
    return {surface: len(by_surface(surface)) for surface in SURFACES}


def main(argv: Sequence[str] | None = None) -> int:
    """Render the matrix, or report its problems and refuse.

    Validation runs before either output: a matrix that does not satisfy
    :func:`validate_matrix` is not worth printing, and printing it anyway is
    how an inventory starts being trusted after it stopped being true.
    """
    parser = argparse.ArgumentParser(
        prog=f"python3 -m {PACKAGE_NAME}",
        description="Render the capability and ownership matrix.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the summary counts as JSON instead of Markdown tables",
    )
    options = parser.parse_args(list(argv) if argv is not None else None)
    problems = validate_matrix()
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    if options.json:
        print(json.dumps(payload(), indent=2, sort_keys=True))
    else:
        print(render_matrix())
    return 0


def payload() -> dict[str, object]:
    """The matrix as plain data, for a caller that wants to render it itself."""
    counts: Mapping[str, int] = summary_counts()
    return {
        "total": len(CAPABILITIES),
        "by_surface": dict(counts),
        "coverage_gaps": [row.key for row in coverage_gaps()],
        "operation_behavior_owners": dict(OPERATION_BEHAVIOR_OWNERS),
    }
