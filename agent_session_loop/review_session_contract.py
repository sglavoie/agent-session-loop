"""What a generated review session is, and what it must prove to pass.

A review session is not written by a human and is not reviewed before it
runs: the runner materializes it, hands it to a harness, and then has to
decide whether what came back is a real verdict or merely a file in the
right place. This module owns both halves of that contract — recognizing a
generated review session, and authoring the gates that adjudicate one.

Three things live here together because they are one contract read from
three directions:

* **Recognition.** ``contains_review_session_marker``,
  ``is_generated_review_session_file``, and
  ``generated_review_session_version`` answer whether a session file is a
  generated review and which revision of the contract it was written
  against. The run loop branches on these before it selects a harness.
* **The rendered gates.** ``_GENERATED_REVIEW_VERIFY_SOURCE`` and
  ``_GENERATED_REVIEW_ARTIFACT_VERIFY_SOURCE`` are executable text, run as
  ``python3 -c`` inside the plan directory where no package import is
  available. They are the only statement of the review contract that the
  reviewed session can actually execute, so they duplicate a structural
  subset of :mod:`agent_session_loop.review` on purpose rather than calling
  it.
* **Provenance.** ``_authenticated_generated_review_contract`` stamps
  ``generated_gate`` on exactly the checks re-rendered from this plan
  directory. Every field a rejection line carries is public, so the stamp is
  what separates "ASL's own gate rejected this" from "a contract author
  typed the same command".

This module writes no durable record. It returns payloads, contract
dataclasses, and Markdown; the caller decides what reaches disk. The one
mutation-shaped function, ``refreshed_generated_review_contract``, rewrites
a session file's text in memory and returns it.

Two invariants survive the move out of ``runner.py``:

* The two verifier sources are byte-for-byte what the runner emitted.
  ``tests/test_asl_review_session_contract.py`` pins both against fixtures
  captured from the pre-move runner, and pins the rendered gate recipe too,
  so a change that leaves the constants alone but alters the path
  substitutions still fails.
* Every rejection leaves through ``fail``, whose vocabulary is pinned equal
  to ``verification_diagnostics.GENERATED_GATE_REASONS`` by a test that extracts this
  module's source.

Nothing here imports the runner. What the loop owns enters as a parameter:
the plan directory, the session ID, and the repository aliases.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any

from . import (
    contracts,
    plans_discovery,
    plans_errors,
    plans_handoff,
    plans_session_ids,
    review,
    verification_diagnostics,
    verification_transitions,
)


def contains_review_session_marker(text: str) -> bool:
    """Return True when a session file body carries the review marker."""
    return review.contains_review_session_marker(text)


def is_generated_review_session_file(path: str | Path) -> bool:
    """Return True when `path` is a session file marked as a generated review."""
    try:
        body = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return contains_review_session_marker(body)


def generated_review_session_version(path: str | Path) -> int | None:
    """Return the generated-review version of a session file, if any.

    ``None`` for unreadable files and files without a review marker; ``1``
    for legacy sessions carrying only the base marker; otherwise the integer
    from the versioned marker.
    """
    try:
        body = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    return review.review_session_marker_version(body)


def final_review_output_path(plan_dir: str | Path) -> Path:
    """Return the path where the final review output is written."""
    return review.final_review_output_path(plan_dir)


def final_review_artifact_path(plan_dir: str | Path) -> Path:
    """Return the path where the structured review artifact is written."""
    return review.final_review_artifact_path(plan_dir)


class ReviewVerdictError(ValueError):
    """Raised when ``final-review.md`` lacks a valid status verdict."""


def parse_review_verdict(text: str) -> dict[str, Any]:
    """Parse the required final review status line into a stable payload.

    Raising wrapper over the shared :func:`review.parse_review_verdict`
    grammar, preserved for existing importers; the run loop itself reads
    verdicts through :func:`review.load_review_verdict`.
    """
    payload = review.parse_review_verdict(text)
    if not payload["ok"]:
        raise ReviewVerdictError(payload["error"])
    return {
        "status_line": payload["status_line"],
        "outcome": payload["outcome"],
        "total_findings": payload["total_findings"],
        "findings_by_severity": payload["findings_by_severity"],
    }


# Every rejection in the generated review gates leaves through ``fail``, which
# writes one structured JSON line to stderr before exiting 1. A predicate that
# exited silently told an operator only that some `python3 -c` script returned
# 1, which is the whole failure surface these gates used to expose. ``CHECK``
# is the gate's own contract name and every ``reason`` is a literal from a
# closed vocabulary, which is what the parser holds the line to; the marker is
# only its first filter, and none of the three is what authenticates the line —
# ``_authenticated_generated_review_contract`` is. Adding a reason here means
# adding it to
# ``verification_diagnostics.GENERATED_GATE_REASONS`` — a test extracts this source and
# pins the two equal.
_VALIDATOR_FAIL_HELPER = (
    "def fail(reason, **detail):",
    "    detail.update(check=CHECK, reason=reason, "
    f"{verification_diagnostics.FAILURE_DIAGNOSTIC_MARKER_KEY}="
    f"{verification_diagnostics.FAILURE_DIAGNOSTIC_MARKER_VALUE!r})",
    "    sys.stderr.write(json.dumps(detail, sort_keys=True) + '\\n')",
    "    sys.exit(1)",
)

_GENERATED_REVIEW_VERIFY_SOURCE = "\n".join(
    [
        "import json, pathlib, re, sys",
        "CHECK = 'final-review-output-evidence-based'",
        *_VALIDATOR_FAIL_HELPER,
        f"review_path = pathlib.Path({review.FINAL_REVIEW_FILENAME!r})",
        "try:",
        "    text = review_path.read_text(encoding='utf-8')",
        "except OSError as error:",
        "    fail('review-output-unreadable', path=str(review_path), error=str(error))",
        "if not text.strip(): fail('review-output-empty', path=str(review_path))",
        (
            "heading_re = re.compile("
            "r'(?im)^\\s{0,3}#{1,6}\\s+"
            r"(?:Findings?|Risks?|Concerns?|Assessment|Review|Summary)\b"
            r"|^\s*(?:Findings?|Risks?|Concerns?|Assessment|Review|Summary)\s*:\s*$'"
            ")"
        ),
        "if heading_re.search(text) is None: fail('missing-required-heading')",
        (
            "path_re = re.compile("
            r"r'(?<![\w./-])(?:[A-Za-z0-9_.-]+/)+(?:[A-Za-z0-9_.-]+)(?::\d+)?"
            r"|(?<![\w./-])(?:[A-Za-z0-9_.-]+\."
            r"(?:py|md|txt|json|toml|yaml|yml|js|ts|tsx|jsx|css|html|rst|sh|sql|ini|cfg|lock))"
            r"(?:[:#][A-Za-z0-9_.-]+)?(?::\d+)?'"
            ")"
        ),
        "refs = {match.group(0).rstrip('.,);]') for match in path_re.finditer(text)}",
        "nonempty_lines = [line.strip() for line in text.splitlines() if line.strip()]",
        "status_lines = [line.strip() for line in text.splitlines() if "
        "line.strip().startswith('Status:')]",
        "if len(status_lines) != 1 or status_lines[0] != nonempty_lines[-1]: "
        "fail('status-line-not-final', status_line_count=len(status_lines), "
        "last_line=nonempty_lines[-1] if nonempty_lines else '')",
        "status = status_lines[0]",
        "def require_evidence():",
        "    if len(refs) < 3: fail('insufficient-path-evidence', "
        "path_reference_count=len(refs), required=3)",
        "    sys.exit(0)",
        f"if status == {review.REVIEW_STATUS_ALL_CLEAR!r}: require_evidence()",
        (
            "match = re.fullmatch("
            "r'Status: ([0-9]+) Findings "
            r"\(([0-9]+) high, ([0-9]+) med, ([0-9]+) low\)', "
            "status)"
        ),
        "if match is None: fail('status-line-malformed', status=status)",
        "total, high, med, low = (int(value) for value in match.groups())",
        "if total <= 0 or high + med + low != total: "
        "fail('status-tally-inconsistent', total=total, high=high, med=med, low=low)",
        "require_evidence()",
    ]
)


# Standalone consistency gate for the structured review artifact. Runs as a
# `python3 -c` contract check inside the plan directory (no package import
# available there), so it mirrors the structural subset of
# ``review.parse_review_artifact``: the artifact must exist, parse, carry the
# supported version, and agree with the ``final-review.md`` status line on
# outcome and severity totals; every finding must carry every required key.
# The runner's post-verification verdict gate re-validates the artifact in
# depth through the shared module.
_GENERATED_REVIEW_ARTIFACT_VERIFY_SOURCE = "\n".join(
    [
        "import json, pathlib, re, sys",
        "CHECK = 'final-review-artifact-consistent'",
        *_VALIDATOR_FAIL_HELPER,
        # Mirrored from the contract parser so a finding's checks are held to
        # exactly what the repair session they materialize will accept.
        "ALLOWED_CHECK_KEYS = "
        + repr(
            {
                check_type: sorted(keys)
                for check_type, keys in sorted(contracts._ALLOWED_CHECK_KEYS.items())
            }
        ),
        "CHECK_SEMANTICS = "
        + repr(dict(sorted(contracts.CHECK_PRESENCE_SEMANTICS.items()))),
        (
            "md = pathlib.Path("
            f"{review.FINAL_REVIEW_FILENAME!r}).read_text(encoding='utf-8')"
        ),
        f"path = pathlib.Path({review.FINAL_REVIEW_ARTIFACT_FILENAME!r})",
        "if not path.is_file(): fail('artifact-missing', path=str(path))",
        "try:",
        "    artifact = json.loads(path.read_text(encoding='utf-8'))",
        "except ValueError as error:",
        "    fail('artifact-unparsable', path=str(path), error=str(error))",
        "if not isinstance(artifact, dict): "
        "fail('artifact-not-object', type=type(artifact).__name__)",
        f"EXPECTED_VERSION = {review.REVIEW_ARTIFACT_VERSION}",
        "if artifact.get('version') != EXPECTED_VERSION: "
        "fail('artifact-version-unsupported', version=artifact.get('version'), "
        "expected=EXPECTED_VERSION)",
        "outcome = artifact.get('outcome')",
        "findings = artifact.get('findings')",
        "if outcome not in ('all-clear', 'findings'): "
        "fail('artifact-outcome-invalid', outcome=outcome)",
        "if not isinstance(findings, list): "
        "fail('artifact-findings-not-list', type=type(findings).__name__)",
        (
            "required = ('id', 'severity', 'title', 'evidence', 'explanation',"
            " 'repair_group', 'done_criteria', 'verification_checks')"
        ),
        "tally = {'high': 0, 'med': 0, 'low': 0}",
        "for index, finding in enumerate(findings):",
        "    if not isinstance(finding, dict): "
        "fail('finding-not-object', index=index, type=type(finding).__name__)",
        "    missing = [key for key in required if not finding.get(key)]",
        "    if missing: fail('finding-missing-keys', index=index, "
        "finding_id=finding.get('id'), missing=missing)",
        "    if finding['severity'] not in tally: "
        "fail('finding-severity-invalid', index=index, "
        "finding_id=finding.get('id'), severity=finding['severity'])",
        "    tally[finding['severity']] += 1",
        "    entries = finding.get('verification_checks')",
        "    if not isinstance(entries, list) or not entries: "
        "fail('finding-verification-checks-empty', index=index, "
        "finding_id=finding.get('id'))",
        "    for position, entry in enumerate(entries):",
        "        if not isinstance(entry, dict): "
        "fail('verification-check-not-object', index=index, "
        "finding_id=finding.get('id'), check_index=position, "
        "type=type(entry).__name__)",
        "        allowed = ALLOWED_CHECK_KEYS.get(entry.get('type'))",
        # An unrecognized check type stays the deep validation's to reject, so
        # this gate keeps mirroring a subset of the shared module rather than
        # racing it to the same verdict from a shallower reading.
        "        if allowed is None: continue",
        "        unknown = sorted(key for key in entry if key not in allowed)",
        "        if unknown: fail('verification-check-unknown-keys', "
        "index=index, finding_id=finding.get('id'), check_index=position, "
        "check_name=entry.get('name'), unknown=unknown, allowed=allowed, "
        "semantics=CHECK_SEMANTICS.get(entry.get('type'), ''))",
        "if (outcome == 'findings') != bool(findings): "
        "fail('artifact-outcome-findings-mismatch', outcome=outcome, "
        "finding_count=len(findings))",
        ("nonempty = [line.strip() for line in md.splitlines() if line.strip()]"),
        "status = nonempty[-1] if nonempty else ''",
        f"if status == {review.REVIEW_STATUS_ALL_CLEAR!r}:",
        "    if outcome != 'all-clear': "
        "fail('status-outcome-mismatch', status=status, outcome=outcome)",
        "    sys.exit(0)",
        (
            "match = re.fullmatch(r'Status: ([0-9]+) Findings "
            r"\(([0-9]+) high, ([0-9]+) med, ([0-9]+) low\)', status)"
        ),
        "if match is None: fail('status-line-malformed', status=status)",
        "total, high, med, low = (int(value) for value in match.groups())",
        "if outcome != 'findings': "
        "fail('status-outcome-mismatch', status=status, outcome=outcome)",
        "if len(findings) != total: fail('status-total-mismatch', "
        "status_total=total, finding_count=len(findings))",
        "if tally['high'] != high or tally['med'] != med or tally['low'] != low: "
        "fail('status-tally-mismatch', status_tally={'high': high, 'med': med, "
        "'low': low}, artifact_tally=tally)",
        "sys.exit(0)",
    ]
)


def _broad_review_verification_checks(plan_dir: Path) -> list[dict[str, Any]]:
    """Every replayable gate the plan declares, as one deduplicated recipe.

    A repair session's contract covers only the findings it was created for, so
    an all-clear review that follows the last repair otherwise rests on nothing
    broader than the review's own artifact validators. Replaying the plan's
    gates over the workspace those repairs produced is what makes the all-clear
    a claim about the whole plan rather than about the last patch.

    Diagnostics are left out because they never decide whether verification
    passes, and each surviving check is renamed by its source session so two
    sessions cannot collide on a bare name.
    """
    collected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for session in plans_discovery.discover_session_files(plan_dir):
        if is_generated_review_session_file(session.path):
            continue
        contract = contracts.read_completion_contract(plan_dir, session.session_id)
        if contract is None:
            continue
        for check in contract.verify.checks:
            if check.on_failure_of or not check.review_replay:
                continue
            payload = contracts.check_payload(check)
            # A baseline expectation describes what the check did before the
            # plan started, which says nothing about the workspace it is being
            # replayed against.
            payload.pop("baseline_expectation", None)
            payload.pop("review_replay", None)
            identity = (
                payload.get("repo"),
                payload["type"],
                tuple(payload.get("argv", ())),
                payload.get("cwd"),
                payload.get("path"),
                payload.get("pattern"),
            )
            if identity in collected:
                continue
            payload["name"] = (
                f"{verification_transitions.BROAD_REVIEW_CHECK_PREFIX}:"
                f"{session.session_id.value}:{payload['name']}"
            )
            collected[identity] = payload
    return list(collected.values())


def _generated_review_gate_payloads(
    *,
    plan_dir: Path,
    repository_aliases: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The two artifact validators the runner writes into every review recipe.

    Split out from the full contract because provenance is re-derived from
    exactly this rendering: the checks that carry a failure diagnostic are the
    ones the runner authored, so authenticating them must not depend on the
    plan's replayed gates, which are authored elsewhere and change as sessions
    are added.
    """
    repository_alias = repository_aliases[0] if repository_aliases else None
    review_path = str(plan_dir / review.FINAL_REVIEW_FILENAME)
    artifact_path = str(plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME)
    # These checks read plan artifacts, which live outside every repository.
    # Inlining their absolute paths keeps the command's own cwd at the bound
    # root instead of pointing it out of the tree the run may touch.
    verify_source = _GENERATED_REVIEW_VERIFY_SOURCE.replace(
        repr(review.FINAL_REVIEW_FILENAME), repr(review_path), 1
    )
    artifact_source = _GENERATED_REVIEW_ARTIFACT_VERIFY_SOURCE.replace(
        repr(review.FINAL_REVIEW_FILENAME), repr(review_path), 1
    ).replace(repr(review.FINAL_REVIEW_ARTIFACT_FILENAME), repr(artifact_path), 1)
    cwd = "."
    if repository_alias is not None:
        artifact_source = artifact_source.replace(
            f"EXPECTED_VERSION = {review.REVIEW_ARTIFACT_VERSION}",
            f"EXPECTED_VERSION = {review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION}",
            1,
        )
        aliases_literal = repr(tuple(repository_aliases))
        severity_predicate = (
            "    if finding['severity'] not in tally: "
            "fail('finding-severity-invalid', index=index, "
            "finding_id=finding.get('id'), severity=finding['severity'])"
        )
        artifact_source = artifact_source.replace(
            severity_predicate,
            "    aliases = " + aliases_literal + "\n"
            "    if finding.get('repo') not in aliases: "
            "fail('finding-repo-unknown', index=index, "
            "finding_id=finding.get('id'), repo=finding.get('repo'), "
            "aliases=list(aliases))\n"
            "    evidence = finding.get('evidence')\n"
            "    checks = finding.get('verification_checks')\n"
            "    if not isinstance(evidence, list) or not evidence: "
            "fail('finding-evidence-empty', index=index, "
            "finding_id=finding.get('id'))\n"
            "    if any(not isinstance(item, dict) or not any(\n"
            "        isinstance(item.get('path'), str) and "
            "item['path'].startswith(alias + ':') for alias in aliases\n"
            "    ) for item in evidence): fail('finding-evidence-unqualified', "
            "index=index, finding_id=finding.get('id'), "
            "aliases=list(aliases))\n"
            "    if not isinstance(checks, list) or not checks: "
            "fail('finding-verification-checks-empty', index=index, "
            "finding_id=finding.get('id'))\n"
            "    if any(not isinstance(item, dict) or item.get('repo') "
            "not in aliases for item in checks): "
            "fail('finding-verification-check-repo-unknown', index=index, "
            "finding_id=finding.get('id'), aliases=list(aliases))\n"
            + severity_predicate,
            1,
        )

    def command_check(name: str, source: str) -> dict[str, Any]:
        check: dict[str, Any] = {
            "type": "command",
            "name": name,
            "argv": ["python3", "-c", source],
            "cwd": cwd,
            "timeout_seconds": 60,
        }
        if repository_alias is not None:
            check["repo"] = repository_alias
        return check

    return [
        command_check("final-review-output-evidence-based", verify_source),
        command_check("final-review-artifact-consistent", artifact_source),
    ]


def _generated_review_contract_payload(
    session_id: plans_session_ids.SessionId,
    *,
    plan_dir: Path,
    repository_aliases: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "session": session_id.value,
        "checks": [
            *_generated_review_gate_payloads(
                plan_dir=plan_dir, repository_aliases=repository_aliases
            ),
            # The artifact validators supplement this gate rather than standing
            # in for it. They run first only because rejecting a malformed
            # review is cheap and replaying the plan's whole recipe is not.
            *_broad_review_verification_checks(plan_dir),
        ],
    }


def _gate_provenance_identity(
    name: str, argv: Sequence[str], cwd: str | None, repo: str | None
) -> tuple[Any, ...]:
    """What a check must reproduce exactly to be one the runner rendered."""
    return (name, tuple(argv), cwd, repo)


def _authenticated_generated_review_contract(
    contract: contracts.CompletionContract,
    *,
    plan_dir: Path,
    repository_aliases: Sequence[str] = (),
) -> contracts.CompletionContract:
    """Stamp generated-gate provenance on the review gates the runner rendered.

    A promoted failure diagnostic asserts that ASL's own review gate rejected
    an artifact, so the assertion has to rest on something no contract can say.
    Everything the rejection line carries is public — the marker literal, both
    gate names, every reason — and ``CommandCheck.name`` is whatever the
    contract's author typed, so a hand-authored command reproduces all of it.
    What it cannot reproduce is being generated here: the recipe is re-rendered
    from this plan directory and only a byte-identical command is stamped, and
    the stamp is a field the contract parser rejects, so it exists only for
    checks this function returns. The review session's own recipe is rewritten
    from the same rendering immediately before the turn runs, which is why the
    real gates always match.
    """
    expected = {
        _gate_provenance_identity(
            payload["name"], payload["argv"], payload.get("cwd"), payload.get("repo")
        )
        for payload in _generated_review_gate_payloads(
            plan_dir=plan_dir, repository_aliases=repository_aliases
        )
    }
    stamped = tuple(
        (
            dataclass_replace(check, generated_gate=True)
            if isinstance(check, contracts.CommandCheck)
            and _gate_provenance_identity(check.name, check.argv, check.cwd, check.repo)
            in expected
            else check
        )
        for check in contract.verify.checks
    )
    if stamped == contract.verify.checks:
        return contract
    return dataclass_replace(
        contract,
        verify=dataclass_replace(contract.verify, checks=stamped),
    )


def _generated_review_contract_section(
    session_id: plans_session_ids.SessionId,
    *,
    plan_dir: Path,
    repository_aliases: Sequence[str] = (),
) -> str:
    artifact_version = (
        review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION
        if repository_aliases
        else review.REVIEW_ARTIFACT_VERSION
    )
    contract_json = json.dumps(
        _generated_review_contract_payload(
            session_id,
            plan_dir=plan_dir,
            repository_aliases=repository_aliases,
        ),
        indent=2,
    )
    return "\n".join(
        [
            "## Completion Contract",
            "",
            "### Done when",
            "",
            (
                f"- `{review.FINAL_REVIEW_FILENAME}` exists in the plan directory and "
                "contains non-whitespace text, at least three distinct "
                "source/doc/test path-like references, and at least one "
                "review-oriented section heading, and ends with exactly one "
                "valid `Status:` verdict line."
            ),
            (
                f"- `{review.FINAL_REVIEW_ARTIFACT_FILENAME}` exists in the plan "
                "directory, parses as version "
                f"{artifact_version} "
                "JSON with complete findings, and agrees with the "
                f"`{review.FINAL_REVIEW_FILENAME}` status line on outcome and "
                "severity totals."
            ),
            "",
            "### Verify",
            "",
            "```json",
            contract_json,
            "```",
            "",
        ]
    )


def refreshed_generated_review_contract(
    text: str,
    session_id: plans_session_ids.SessionId,
    *,
    plan_dir: Path,
    repository_aliases: Sequence[str] = (),
) -> str:
    """Refresh only a pending generated review's marker and verify recipe."""
    expected_version = (
        review.REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION
        if repository_aliases
        else review.GENERATED_REVIEW_SESSION_VERSION
    )
    marker = review.review_session_version_marker(expected_version)
    version_pattern = re.compile(
        r"<!--\s*asl:generated-review-session\s+version=[0-9]+\s*-->"
    )
    if version_pattern.search(text):
        refreshed = version_pattern.sub(marker, text, count=1)
    else:
        refreshed = text.replace(
            review.REVIEW_SESSION_MARKER,
            f"{review.REVIEW_SESSION_MARKER}\n{marker}",
            1,
        )

    contract_at = refreshed.find(contracts.COMPLETION_CONTRACT_HEADING)
    handoff_at = refreshed.find(plans_handoff.HANDOFF_HEADING, contract_at + 1)
    if contract_at < 0 or handoff_at < 0:
        raise plans_errors.PlanError(
            "generated review is missing its Completion Contract or Handoff"
        )
    section = _generated_review_contract_section(
        session_id,
        plan_dir=plan_dir,
        repository_aliases=repository_aliases,
    )
    return refreshed[:contract_at] + section + refreshed[handoff_at:]
