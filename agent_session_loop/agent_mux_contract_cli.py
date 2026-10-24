"""Agent mux contract cli."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable

from . import (
    agent_mux_contract_corpus as corpus,
)
from . import (
    agent_mux_contract_manifest as manifest,
)
from . import (
    agent_mux_contract_models as models,
)

# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------


def _print_violations(label: str, violations: Iterable[models.Violation]) -> None:
    for violation in violations:
        location = violation.pointer or "/"
        print(f"  {label}: {violation.reason} at {location}: {violation.detail}")


def main(argv: list[str] | None = None) -> int:
    """Check or regenerate the canonical corpus."""
    parser = argparse.ArgumentParser(
        prog="python3 -m agent_session_loop.agent_mux_contract",
        description=(
            "Check the ASL-owned Agent Mux compatibility corpus, or regenerate "
            "its derived manifest from the schema and fixture bytes."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        help="report corpus problems without writing anything (default)",
    )
    group.add_argument(
        "--write",
        action="store_true",
        help="rewrite manifest.json from the corpus bytes",
    )
    parser.add_argument(
        "--corpus",
        default=None,
        help="corpus directory (default: the checked-in canonical corpus)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON output")
    args = parser.parse_args(argv)

    try:
        if args.write:
            path = manifest.write_manifest(args.corpus)
            print(f"wrote {path}")
        report = corpus.check_corpus(args.corpus)
    except models.ContractError as error:
        print(f"agent-mux-contract: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                corpus.report_payload(report),
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if report.ok else 1

    print(f"Contract set: {report.contract_set_id}")
    print(f"Corpus digest: {report.corpus_digest}")
    print(f"Fixtures: {len(report.fixtures)}")
    for component in models.COMPONENTS:
        _print_violations(component, report.schema_violations[component])
    _print_violations("manifest", report.manifest_violations)
    for fixture in report.fixture_failures:
        print(f"  fixture: {fixture.path}: {fixture.detail}")
    for name in report.noncanonical:
        print(f"  canonical-json: {name} is not stored in canonical form")
    if report.ok:
        print("corpus ok")
        return 0
    print("corpus has issues", file=sys.stderr)
    return 1
