# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
from . import review_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .review_shared import *
from .review_part_1 import *
from .review_part_2 import *


def review_verdict_payload(
    plan_dir: str | Path, state_dir: str | Path | None = None
) -> dict[str, Any]:
    """Build the authoritative ``asl review-verdict`` payload.

    Merges :func:`load_review_verdict` (the shared verdict, carrying full
    untruncated ``findings`` when the structured artifact is present) with the
    read-only continuation projection under ``review_resume``, then attaches the
    full untruncated ``summary`` (with ``summary_truncated``/
    ``summary_total_chars``) and exact ``next_steps`` remediation commands. The
    verdict's own ``ok``/``reason`` surface at the top level so a malformed or
    missing review reports a stable ``reason`` from the shared vocabulary.

    When the governing generated review session is version
    :data:`STRUCTURED_REVIEW_MIN_VERSION` or newer, a missing
    ``final-review.json`` is enforced as ``missing-review-artifact`` — matching
    runner and continuation enforcement — while legacy v1 Markdown-only reviews
    remain accepted.

    Local imports keep ``review`` free of an import cycle: ``review_continuation``
    imports this module, and ``state`` reaches its continuation evaluator lazily.
    """
    from . import events, responses, review_continuation, state

    snapshot = plans_snapshot.inspect_plan(plan_dir)
    plan_state = state.plan_state_dir(snapshot.plan_dir, state_dir)
    event_path = plan_state / events.EVENTS_FILE_NAME
    review_resume = review_continuation.evaluate_review_continuation(
        snapshot, event_path
    ).to_payload()
    review_session_version = _relevant_review_session_version(snapshot, review_resume)
    verdict = load_review_verdict(
        snapshot.plan_dir,
        require_artifact=(
            review_session_version is not None
            and review_session_version >= STRUCTURED_REVIEW_MIN_VERSION
        ),
    )
    summary, summary_total = _review_verdict_summary(verdict)
    guidance = review_continuation.review_resume_guidance(
        review_resume, review_harness_active=False
    )
    next_steps = _review_verdict_next_steps(
        verdict, plan_dir=str(snapshot.plan_dir), guidance=guidance
    )

    payload = responses.success_payload(REVIEW_VERDICT_COMMAND)
    # Merge the verdict; its ``ok``/``reason``/``error`` surface at the top
    # level, and ``findings`` (when present) is the full untruncated list.
    payload.update(verdict)
    payload["command"] = REVIEW_VERDICT_COMMAND
    payload["plan_dir"] = str(snapshot.plan_dir)
    payload["state_dir"] = str(plan_state)
    payload["events_path"] = str(event_path)
    payload["review_resume"] = review_resume
    payload["summary"] = summary
    payload["summary_truncated"] = False
    payload["summary_total_chars"] = summary_total
    payload["findings"] = verdict.get("findings")
    payload["next_steps"] = next_steps
    return payload


def review_verdict_from_args(args: argparse.Namespace) -> int:
    """Run the ``review-verdict`` subcommand from CLI arguments.

    Returns ``0`` for a valid all-clear or findings verdict and ``1`` for a
    malformed plan or any stable non-ok verdict (missing, malformed, or
    contradictory review), so shell automation can gate on the exit status.
    """
    from . import responses

    plan_dir_arg = args.plan_dir
    want_json = bool(args.json)
    try:
        payload = review_verdict_payload(plan_dir_arg, args.state_dir)
    except plans_errors.PlanError as error:
        if want_json:
            responses.print_json(
                responses.error_payload(
                    REVIEW_VERDICT_COMMAND,
                    reason="malformed-plan",
                    error=str(error),
                    paused=False,
                    plan_dir=str(plan_dir_arg),
                )
            )
        else:
            print(
                f"agent-session-loop review-verdict: {error}",
                file=sys.stderr,
            )
        return 1

    if want_json:
        responses.print_json(payload)
    else:
        _print_text_review_verdict(payload)
    # A stable missing, malformed, or contradictory verdict is a non-ok
    # payload; exit nonzero so shell automation can trust the status. Valid
    # all-clear and findings verdicts stay ``ok`` and exit zero.
    return 0 if payload.get("ok") else 1


def _print_text_review_verdict(payload: dict[str, Any]) -> None:
    print(f"Plan: {payload['plan_dir']}")
    if payload.get("ok"):
        print(f"Verdict: {payload.get('status_line')}")
        print(f"Outcome: {payload.get('outcome')}")
        print(f"Total findings: {payload.get('total_findings')}")
    else:
        print(f"Verdict: not ok ({payload.get('reason')})")
        error = payload.get("error")
        if error:
            print(f"Error: {error}")
    if payload.get("summary_total_chars"):
        print(f"Summary ({payload['summary_total_chars']} chars):")
        print(payload["summary"])
    next_steps = payload.get("next_steps") or []
    if next_steps:
        print("Next steps:")
        for step in next_steps:
            print(f"  - {step}")


# Cross-cycle finding identity. Rereview cycles need to recognize a finding
# that survived its repair session even when the regenerated review shifts
# line numbers or rewords nothing else, so the fingerprint hashes only the
# normalized severity, title, evidence paths, and stable symbols — never ids
# (regenerated per review) or line numbers (drift between reviews).
FINDING_FINGERPRINT_PREFIX = "finding"

# Drift-tolerant repeat identity. The fingerprint above hashes evidence paths
# and the repository alias, so a finding that survives its repair but is
# re-reported against a moved path, a renamed helper, or a different alias
# earns a fresh fingerprint and reads as newly introduced. The repeat key drops
# exactly those two drifting dimensions and adds the repair group, which is the
# reviewer's own statement of "one fix closes these", so a finding that comes
# back under any evidence still collides with the attempt that failed to close
# it. It is deliberately coarser than the fingerprint and is only ever used to
# widen repeat detection, never to narrow it.
FINDING_REPEAT_KEY_PREFIX = "repeat"

# ``path/to/file.py:12`` / ``file.py:12-34`` / ``file.py:12:8`` trailing
# location suffixes, and prose ``line 12`` / ``lines 12-34`` references.
_EVIDENCE_LINE_SUFFIX_RE = re.compile(r"(:[0-9]+(?:[:-][0-9]+)*)+$")
_TITLE_LINE_REF_RE = re.compile(
    r"(?::[0-9]+(?:[:-][0-9]+)*)|(?:\blines?\s+[0-9]+(?:\s*[-–]\s*[0-9]+)?\b)",
    re.IGNORECASE,
)


def _normalized_title(title: str) -> str:
    stripped = _TITLE_LINE_REF_RE.sub(" ", title)
    return " ".join(stripped.split()).lower()


def _normalized_evidence_path(path: str) -> str:
    return _EVIDENCE_LINE_SUFFIX_RE.sub("", path.strip())


def finding_fingerprint(finding: dict[str, Any]) -> str:
    """Return the stable ``finding:<16hex>`` fingerprint for a finding.

    Hashes the repository alias when present, normalized severity, title (line
    references removed, whitespace collapsed, lowercased), sorted unique
    evidence paths (trailing ``:line`` suffixes removed), and sorted unique
    evidence symbols. Findings without repository attribution retain the
    pre-repository identity byte-for-byte. Two findings that differ only in id,
    explanation, repair grouping, or line-number drift therefore share one
    fingerprint.
    """
    severity = str(finding.get("severity", "")).strip().lower()
    title = _normalized_title(str(finding.get("title", "")))
    repository = str(finding.get("repo", "")).strip()
    paths: set[str] = set()
    symbols: set[str] = set()
    evidence = finding.get("evidence")
    if isinstance(evidence, list):
        for entry in evidence:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if isinstance(path, str) and path.strip():
                paths.add(_normalized_evidence_path(path))
            symbol = entry.get("symbol")
            if isinstance(symbol, str) and symbol.strip():
                symbols.add(symbol.strip())
    identity_fields: dict[str, Any] = {
        "severity": severity,
        "title": title,
        "evidence_paths": sorted(paths),
        "symbols": sorted(symbols),
    }
    if repository:
        identity_fields["repository"] = repository
    identity = json.dumps(identity_fields, sort_keys=True)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{FINDING_FINGERPRINT_PREFIX}:{digest}"


def finding_fingerprints(
    findings: Any,
) -> tuple[str, ...] | None:
    """Fingerprint a structured findings list, or ``None`` when absent.

    Returns one fingerprint per finding in order (duplicates preserved) so
    callers can align fingerprints with findings; ``None`` when ``findings``
    is not a list (v1 Markdown-only verdicts carry no structured findings).
    """
    if not isinstance(findings, list):
        return None
    return tuple(
        finding_fingerprint(finding)
        for finding in findings
        if isinstance(finding, dict)
    )


def finding_repeat_key(finding: dict[str, Any]) -> str:
    """Return the ``repeat:<16hex>`` cross-cycle repeat key for a finding.

    Hashes normalized severity, normalized title, and the normalized repair
    group. Evidence paths, evidence symbols, and the repository alias are
    excluded so a finding reintroduced under moved or re-aliased evidence still
    matches the cycle that tried to close it.
    """
    severity = str(finding.get("severity", "")).strip().lower()
    title = _normalized_title(str(finding.get("title", "")))
    repair_group = " ".join(str(finding.get("repair_group", "")).split()).lower()
    identity = json.dumps(
        {"severity": severity, "title": title, "repair_group": repair_group},
        sort_keys=True,
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{FINDING_REPEAT_KEY_PREFIX}:{digest}"


def finding_repeat_keys(findings: Any) -> tuple[str, ...] | None:
    """Repeat-key a structured findings list, or ``None`` when absent.

    Mirrors :func:`finding_fingerprints` so the two lists stay index-aligned
    with each other and with the findings they describe.
    """
    if not isinstance(findings, list):
        return None
    return tuple(
        finding_repeat_key(finding) for finding in findings if isinstance(finding, dict)
    )
