"""Structured failure diagnostics promoted from ASL's own generated gates.

A failed command may write a bounded JSON rejection as the last line of its
stderr. Only a command the runner generated itself, naming its own check and a
reason from a closed vocabulary, has that line promoted onto the check result.
"""

from __future__ import annotations

import json
from typing import Any

from .contracts import CommandCheck

#: Longest stderr line still considered for a structured diagnostic. The
#: producers are ASL's own generated gates, whose rejections run to tens of
#: bytes; the cap is what keeps an arbitrary command from pushing unbounded
#: structure into the check record, the event log, and the audit bundle.
_FAILURE_DIAGNOSTIC_MAX_BYTES = 2048

#: Key naming a failed command's structured rejection wherever it travels.
FAILURE_DIAGNOSTIC_KEY = "failure_diagnostic"

#: The marker a generated ASL gate stamps on its rejection line, and the value
#: it stamps (``review_session_contract._VALIDATOR_FAIL_HELPER``). It is a
#: cheap first filter,
#: not authentication: the literal is public and every command can print it, so
#: what a promoted line rests on is the runner's own provenance stamp, then the
#: vocabulary below. It is stripped once read, since it says nothing about the
#: failure.
FAILURE_DIAGNOSTIC_MARKER_KEY = "asl_diagnostic"
FAILURE_DIAGNOSTIC_MARKER_VALUE = "generated-gate/1"

#: The generated review gates' own names. Each is both the check name the
#: runner puts in the contract and the ``CHECK`` constant that gate stamps on
#: its rejection line, and a promoted diagnostic must name the check that is
#: actually executing. A name is authored text and authenticates nothing on its
#: own — ``CommandCheck.generated_gate`` is what does that — so this set exists
#: to bound the value that travels, not to identify the producer.
GENERATED_GATE_CHECK_NAMES = frozenset(
    {
        "final-review-artifact-consistent",
        "final-review-output-evidence-based",
    }
)

#: Every rejection reason those gates can report. Bounding ``reason`` to a
#: closed set — rather than to a token *shape* — is what makes forgery
#: worthless: the marker is copyable and a check may be named anything, but
#: neither buys a field that can hold an application identifier, an opaque
#: token, or a proprietary string. ``tests/test_asl_verification.py`` extracts
#: this vocabulary from the rendered gate sources and pins it equal, so a gate
#: that grows a reason fails there instead of silently losing its diagnostic.
GENERATED_GATE_REASONS = frozenset(
    {
        "artifact-findings-not-list",
        "artifact-missing",
        "artifact-not-object",
        "artifact-outcome-findings-mismatch",
        "artifact-outcome-invalid",
        "artifact-unparsable",
        "artifact-version-unsupported",
        "finding-evidence-empty",
        "finding-evidence-unqualified",
        "finding-missing-keys",
        "finding-not-object",
        "finding-repo-unknown",
        "finding-severity-invalid",
        "finding-verification-check-repo-unknown",
        "finding-verification-checks-empty",
        "insufficient-path-evidence",
        "missing-required-heading",
        "review-output-empty",
        "review-output-unreadable",
        "status-line-malformed",
        "status-line-not-final",
        "status-outcome-mismatch",
        "status-tally-inconsistent",
        "status-tally-mismatch",
        "status-total-mismatch",
        "verification-check-not-object",
        "verification-check-unknown-keys",
    }
)

#: The only diagnostic fields that are control-plane text, each with the closed
#: vocabulary it must draw from. Everything a gate attaches beyond them is
#: drawn from the artifact it rejected — the generated gates pass ``status=``,
#: ``last_line=`` and ``error=`` — so it is application content and is withheld
#: like any other command output.
_DIAGNOSTIC_CONTROL_PLANE_VOCABULARY = {
    "check": GENERATED_GATE_CHECK_NAMES,
    "reason": GENERATED_GATE_REASONS,
}


def is_control_plane_diagnostic_field(key: str | None, value: Any) -> bool:
    """Whether a ``failure_diagnostic`` member may travel as control-plane text.

    The audit boundary asks this of every field it projects, so the definition
    of what is ASL schema and what is application output lives in one place
    rather than being restated by each consumer.
    """
    # The collector asks this of whatever a recorded bundle holds, so the
    # string test comes first: a nested structure is unhashable and would
    # raise on set membership rather than being withheld.
    vocabulary = _DIAGNOSTIC_CONTROL_PLANE_VOCABULARY.get(key or "")
    return vocabulary is not None and isinstance(value, str) and value in vocabulary


def _parse_failure_diagnostic(
    stderr_preview: str, *, check: CommandCheck
) -> dict[str, Any] | None:
    """Read a failed command's last stderr line as a structured rejection.

    Provenance is decided first and decides everything: a promoted diagnostic
    says ASL's own review gate rejected an artifact, and only
    ``check.generated_gate`` — set by the runner on the recipe it rendered
    itself, and unsettable from any contract — can say that. Every value on the
    line is public, so a hand-authored command can reproduce the marker, either
    gate name and any reason verbatim; what it cannot reproduce is a command
    the runner generated.

    Past that fence the line must still be a bounded JSON object carrying the
    marker, naming the executing check as its own ``check``, and reporting a
    ``reason`` from the generated gates' closed vocabulary — so a gate that
    misreports its own identity, or a reason ASL never writes, still travels
    as nothing.
    """
    if not check.generated_gate:
        return None
    lines = [line.strip() for line in stderr_preview.splitlines() if line.strip()]
    if not lines:
        return None
    candidate = lines[-1]
    if len(candidate.encode("utf-8")) > _FAILURE_DIAGNOSTIC_MAX_BYTES:
        return None
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get(FAILURE_DIAGNOSTIC_MARKER_KEY) != FAILURE_DIAGNOSTIC_MARKER_VALUE:
        return None
    if parsed.get("check") != check.name:
        return None
    if not all(
        is_control_plane_diagnostic_field(key, parsed.get(key))
        for key in _DIAGNOSTIC_CONTROL_PLANE_VOCABULARY
    ):
        return None
    return {
        key: value
        for key, value in parsed.items()
        if key != FAILURE_DIAGNOSTIC_MARKER_KEY
    }
