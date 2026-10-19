"""Agent mux contract corpus."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    agent_mux_contract_manifest as manifest_module,
)
from . import (
    agent_mux_contract_models as models,
)
from . import (
    agent_mux_contract_schema as schema_module,
)
from . import (
    agent_mux_contract_validation as validation,
)

# ---------------------------------------------------------------------------
# Fixture corpus
# ---------------------------------------------------------------------------

_FIXTURE_KEYS = frozenset({"case", "description", "root", "document"})


_INVALID_FIXTURE_KEYS = _FIXTURE_KEYS | {"reason", "pointer"}


@dataclass(frozen=True)
class FixtureResult:
    """One fixture's verdict."""

    component: str
    path: str
    case: str
    expected_valid: bool
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "path": self.path,
            "case": self.case,
            "expected_valid": self.expected_valid,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CorpusReport:
    """The whole corpus checked against itself."""

    contract_set_id: str
    corpus_digest: str
    corpus_root: str
    schema_violations: dict[str, list[models.Violation]]
    manifest_violations: list[models.Violation]
    fixtures: list[FixtureResult]
    noncanonical: list[str]

    @property
    def fixture_failures(self) -> list[FixtureResult]:
        return [fixture for fixture in self.fixtures if not fixture.passed]

    @property
    def ok(self) -> bool:
        return (
            not any(self.schema_violations.values())
            and not self.manifest_violations
            and not self.fixture_failures
            and not self.noncanonical
        )


def fixture_paths(
    component: str, *, valid: bool, root: str | Path | None = None
) -> list[Path]:
    """Return one component's positive or negative fixture files, sorted."""
    directory = (
        manifest_module.corpus_root(root)
        / models.FIXTURE_DIRNAME
        / models.FIXTURE_DIRNAMES[component]
        / (models.VALID_DIRNAME if valid else models.INVALID_DIRNAME)
    )
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def _check_fixture(
    schema: Mapping[str, Any],
    path: Path,
    *,
    component: str,
    relative: str,
    expected_valid: bool,
) -> FixtureResult:
    fixture = manifest_module._read_json(path)
    allowed = _FIXTURE_KEYS if expected_valid else _INVALID_FIXTURE_KEYS
    if not isinstance(fixture, dict) or set(fixture) - allowed:
        return FixtureResult(
            component,
            relative,
            path.stem,
            expected_valid,
            False,
            "fixture envelope carries keys outside the declared set",
        )
    missing = (
        {"case", "root", "document"} | (set() if expected_valid else {"reason"})
    ) - set(fixture)
    if missing:
        return FixtureResult(
            component,
            relative,
            path.stem,
            expected_valid,
            False,
            f"fixture envelope is missing {sorted(missing)}",
        )

    case = str(fixture["case"])
    problems = validation.validate_document(
        schema, fixture["document"], root=fixture["root"]
    )
    if expected_valid:
        if problems:
            return FixtureResult(
                component,
                relative,
                case,
                True,
                False,
                "; ".join(
                    f"{p.reason} at {p.pointer or '/'}: {p.detail}" for p in problems
                ),
            )
        return FixtureResult(component, relative, case, True, True, "accepted")

    if not problems:
        return FixtureResult(
            component,
            relative,
            case,
            False,
            False,
            f"expected `{fixture['reason']}` but the document was accepted",
        )
    reasons = {problem.reason for problem in problems}
    if fixture["reason"] not in reasons:
        return FixtureResult(
            component,
            relative,
            case,
            False,
            False,
            f"expected `{fixture['reason']}`, got {sorted(reasons)}",
        )
    pointer = fixture.get("pointer")
    if pointer is not None and not any(
        problem.pointer == pointer and problem.reason == fixture["reason"]
        for problem in problems
    ):
        return FixtureResult(
            component,
            relative,
            case,
            False,
            False,
            f"expected `{fixture['reason']}` at `{pointer}`, got other locations",
        )
    return FixtureResult(
        component, relative, case, False, True, f"rejected: {fixture['reason']}"
    )


def check_corpus(root: str | Path | None = None) -> CorpusReport:
    """Check the corpus against itself and return the whole verdict.

    This is the operation ``asl contract-check --agent-mux`` reports and the one
    a release gate runs: schemas lint clean, the manifest matches the bytes
    beside it, every positive fixture is accepted, every negative fixture is
    rejected for the exact reason it declares, and every file is already the
    canonical rendering of its own content — which is regeneration being
    byte-identical, stated as a check rather than as a habit.
    """
    base = manifest_module.corpus_root(root)
    schema_problems: dict[str, list[models.Violation]] = {}
    fixtures: list[FixtureResult] = []
    for component in models.COMPONENTS:
        schema = manifest_module.load_schema(component, root=base)
        problems = schema_module.lint_schema(schema)
        schema_problems[component] = problems
        if problems:
            continue
        for expected_valid in (True, False):
            for path in fixture_paths(component, valid=expected_valid, root=base):
                fixtures.append(
                    _check_fixture(
                        schema,
                        path,
                        component=component,
                        relative=path.relative_to(base).as_posix(),
                        expected_valid=expected_valid,
                    )
                )
    return CorpusReport(
        contract_set_id=manifest_module.contract_set_id(base),
        corpus_digest=manifest_module.corpus_digest(base),
        corpus_root=str(base),
        schema_violations=schema_problems,
        manifest_violations=manifest_module.manifest_violations(base),
        fixtures=fixtures,
        noncanonical=noncanonical_files(base),
    )


def noncanonical_files(root: str | Path | None = None) -> list[str]:
    """Return corpus files whose bytes are not the canonical rendering.

    This is the determinism property stated as a check: if every file already
    equals ``canonical_json`` of its own content, regenerating the corpus is a
    no-op and two people editing it independently produce comparable diffs.
    """
    base = manifest_module.corpus_root(root)
    drifted: list[str] = []
    for name in [
        *manifest_module.corpus_files(base),
        models.MANIFEST_FILENAME,
    ]:
        path = base / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if raw != models.canonical_json(json.loads(raw.decode("utf-8"))):
            drifted.append(name)
    return sorted(drifted)


def report_payload(
    report: CorpusReport, *, state_root: str | None = None
) -> dict[str, Any]:
    """Render a :class:`CorpusReport` as the JSON block contract-check emits.

    The manifest is read from the same corpus the report was built against, and
    a manifest that cannot be read is reported rather than raised: the report
    already carries the ``digest-mismatch`` that says so, and a caller asking
    what is wrong with the corpus deserves an answer instead of an exception.
    """
    try:
        manifest = manifest_module.read_manifest(report.corpus_root)
        entry = manifest_module.current_contract_set(manifest)
        manifest_version = manifest.get("manifest_version")
    except models.ContractError:
        entry = {"components": {}, "capabilities": {}}
        manifest_version = None
    components = [
        {
            "component": component,
            **{
                key: value
                for key, value in entry["components"].get(component, {}).items()
                if key
                in {"schema_path", "schema_version", "digest", "max_document_bytes"}
            },
            "schema_status": (
                "ok"
                if not report.schema_violations[component]
                else models.REASON_SCHEMA_INVALID
            ),
        }
        for component in models.COMPONENTS
    ]
    return {
        "contract_set_id": report.contract_set_id,
        "corpus_digest": report.corpus_digest,
        "corpus_root": report.corpus_root,
        "manifest_version": manifest_version,
        "state_root": state_root,
        "components": components,
        "capabilities": entry["capabilities"],
        "fixtures": {
            "total": len(report.fixtures),
            "accepted": sum(1 for fixture in report.fixtures if fixture.expected_valid),
            "rejected": sum(
                1 for fixture in report.fixtures if not fixture.expected_valid
            ),
            "failures": [fixture.to_dict() for fixture in report.fixture_failures],
        },
        "issues": [
            *(
                violation.to_dict()
                for component in models.COMPONENTS
                for violation in report.schema_violations[component]
            ),
            *(violation.to_dict() for violation in report.manifest_violations),
            *(
                {
                    "reason": "fixture-failed",
                    "pointer": fixture.path,
                    "detail": fixture.detail,
                }
                for fixture in report.fixture_failures
            ),
            *(
                {
                    "reason": "noncanonical-bytes",
                    "pointer": name,
                    "detail": "file is not the canonical rendering of its content",
                }
                for name in report.noncanonical
            ),
        ],
    }
