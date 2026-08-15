"""Evidence-based classification of a nonzero harness exit.

A leaf module on purpose: it imports only the harness result type, the pause
reason vocabulary, and the retry-hint parser, so every caller that has to read
a failing harness turn — the runner's session loop and the supervisor audit
alike — classifies it the same way without either importing the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import events, harnesses, retry_hints

# A nonzero harness exit is turned into a stable, evidence-based category rather
# than a generic ``nonzero-exit`` pause. The classifier is fail-closed: only
# positively recognized transport failures are retryable; quota, auth, and
# invalid-model failures are always non-retryable; and anything unrecognized
# stays ``nonzero-exit`` (non-retryable) so legacy and novel failures never
# get retried automatically. Reset/retry timestamps are parsed when present but
# never gate the retryability decision.

# Upper bound on the bounded evidence preview kept on a classification. Long
# harness output must never bloat a turn result or pause event.
FAILURE_EVIDENCE_MAX_CHARS = 280
# Upper bound on the combined harness text scanned for classification markers.
# Matching only the head keeps classification cheap and bounded regardless of
# how much output a failing harness produced.
_FAILURE_SCAN_MAX_CHARS = 20000
_WHITESPACE_RE = re.compile(r"\s+")

#: Marker of the provider-capacity rule. A caller that must record the outage
#: recognizes it here rather than by re-reading the harness text.
MODEL_CAPACITY_MARKER = "model-capacity"

# Ordered (marker, reason, retryable, pattern) rules. First match wins, so more
# specific / higher-priority categories (quota, auth, invalid model) precede the
# broader transient-transport rule. Markers are stable slugs used to build the
# normalized fingerprint.
_FAILURE_CLASSIFICATION_RULES: tuple[tuple[str, str, bool, re.Pattern[str]], ...] = (
    (
        "usage-limit",
        events.PAUSE_REASON_QUOTA_EXHAUSTED,
        False,
        re.compile(
            r"usage limit|rate limit|rate.?limited|too many requests|"
            r"\b429\b|quota|session limit|weekly limit|"
            r"\d+-hour limit|limit reached|reached your .*limit|"
            r"you've (?:hit|reached) your|insufficient_quota|"
            r"out of (?:credits|tokens)",
            re.IGNORECASE,
        ),
    ),
    (
        "authentication",
        events.PAUSE_REASON_AUTH_FAILED,
        False,
        re.compile(
            r"\b401\b|unauthorized|authentication|not logged in|"
            r"invalid api key|invalid_api_key|missing api key|api key|"
            r"please (?:log ?in|sign in)|login required|"
            r"permission denied|forbidden|\b403\b|token expired|"
            r"credentials",
            re.IGNORECASE,
        ),
    ),
    (
        "invalid-model",
        events.PAUSE_REASON_INVALID_MODEL,
        False,
        re.compile(
            r"invalid model|unknown model|model not found|no such model|"
            r"model_not_found|unsupported model|does not exist.*model|"
            r"issue with the selected model|"
            r"model .* (?:not found|is not available|does not exist|may not exist)",
            re.IGNORECASE,
        ),
    ),
    (
        # After ``invalid-model`` so a typo'd model name still fails fast and
        # non-retryably, and before the broad transport rule so a provider that
        # says it is out of capacity is retried on its own backoff rather than
        # read as a socket failure.
        MODEL_CAPACITY_MARKER,
        events.PAUSE_REASON_TRANSPORT_FAILED,
        True,
        re.compile(
            r"at capacity|over capacity|no capacity|insufficient capacity|"
            r"capacity constraints|server_?overloaded|overloaded_error|"
            r"\boverloaded\b|try a different model|model is (?:currently )?busy",
            re.IGNORECASE,
        ),
    ),
    (
        "transport",
        events.PAUSE_REASON_TRANSPORT_FAILED,
        True,
        re.compile(
            r"connection reset|connection refused|connection closed|"
            r"econnreset|econnrefused|etimedout|"
            r"\b50[234]\b|bad gateway|service unavailable|"
            r"gateway timeout|temporarily unavailable|"
            r"network error|stream (?:error|disconnected)|"
            r"read ECONNRESET|socket hang up|"
            r"unexpected eof|tls handshake|dns",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True)
class FailureClassification:
    """A stable, evidence-based classification of a nonzero harness turn.

    ``reason`` is one of the ``events.PAUSE_REASON_*`` harness-failure
    categories. ``retryable`` says whether a supervisor may relaunch the same
    session; it is derived solely from the category, never from ``retry_at``.
    ``fingerprint`` is a normalized signature for per-session retry accounting.
    ``evidence`` is a bounded preview of the matched output. ``retry_at`` is a
    best-effort reset hint (``None`` when absent) and is advisory only.
    ``marker`` is the rule slug the fingerprint was built from, so a caller can
    react to one category without re-matching the harness text.
    """

    reason: str
    retryable: bool
    fingerprint: str
    evidence: str
    returncode: int | None = None
    retry_at: str | None = None
    marker: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "retryable": self.retryable,
            "fingerprint": self.fingerprint,
            "evidence": self.evidence,
            "returncode": self.returncode,
            "retry_at": self.retry_at,
            "marker": self.marker,
        }


def _failure_scan_text(result: harnesses.HarnessResult) -> str:
    """Combine the harness streams most likely to carry a failure signal.

    The last message and stderr come first because harnesses surface the
    operative error there; stdout is appended for completeness. The result is
    bounded so classification stays cheap regardless of output volume.
    """
    parts = [result.last_message or "", result.stderr or "", result.stdout or ""]
    combined = "\n".join(part for part in parts if part)
    return combined[:_FAILURE_SCAN_MAX_CHARS]


def _bounded_evidence(text: str, match: re.Match[str] | None) -> str:
    """Return a whitespace-collapsed, bounded preview around ``match``.

    Falls back to the head of ``text`` when there is no match (the fail-closed
    ``nonzero-exit`` path still records what the harness said).
    """
    if match is not None:
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end == -1:
            line_end = len(text)
        snippet = text[line_start:line_end]
    else:
        snippet = text
    snippet = _WHITESPACE_RE.sub(" ", snippet).strip()
    if len(snippet) > FAILURE_EVIDENCE_MAX_CHARS:
        snippet = snippet[: FAILURE_EVIDENCE_MAX_CHARS - 1].rstrip() + "…"
    return snippet


def _parse_retry_at(text: str) -> str | None:
    """Extract a best-effort reset/retry hint, or ``None`` when absent."""
    return retry_hints.extract_retry_hint(text)


def classify_harness_failure(
    result: harnesses.HarnessResult,
) -> FailureClassification:
    """Classify a nonzero harness turn into a stable failure category.

    Recognizes quota/session exhaustion, authentication failures, invalid
    models, transient transport failures, and interruption; everything else
    fails closed as ``nonzero-exit``. Only transport failures are retryable.
    """
    returncode = result.returncode
    if result.interrupted:
        return FailureClassification(
            reason=events.PAUSE_REASON_INTERRUPTED,
            retryable=False,
            fingerprint=f"{events.PAUSE_REASON_INTERRUPTED}:user",
            evidence="run interrupted by user",
            returncode=returncode,
            retry_at=None,
            marker="user",
        )

    text = _failure_scan_text(result)
    for marker, reason, retryable, pattern in _FAILURE_CLASSIFICATION_RULES:
        match = pattern.search(text)
        if match is None:
            continue
        retry_at = (
            _parse_retry_at(text)
            if reason == events.PAUSE_REASON_QUOTA_EXHAUSTED
            else None
        )
        return FailureClassification(
            reason=reason,
            retryable=retryable,
            fingerprint=f"{reason}:{marker}",
            evidence=_bounded_evidence(text, match),
            returncode=returncode,
            retry_at=retry_at,
            marker=marker,
        )

    # Fail closed: unrecognized nonzero exits are never retried automatically.
    return FailureClassification(
        reason=events.PAUSE_REASON_NONZERO_EXIT,
        retryable=False,
        fingerprint=f"{events.PAUSE_REASON_NONZERO_EXIT}:exit-{returncode}",
        evidence=_bounded_evidence(text, None),
        returncode=returncode,
        retry_at=None,
        marker=f"exit-{returncode}",
    )
