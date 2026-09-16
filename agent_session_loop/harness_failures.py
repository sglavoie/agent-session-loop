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
# get retried automatically. A recognized category may still be ``nonzero-exit``
# when the pause reason genuinely is that and only the marker carries new
# information — ``context-overflow`` is the case. Reset/retry timestamps are
# parsed when present but never gate the retryability decision.

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

#: Marker of the launch-time transport-degradation rule, mirroring
#: :data:`MODEL_CAPACITY_MARKER` so a caller records the degradation without
#: re-matching the harness text.
TRANSPORT_DEGRADATION_MARKER = "transport-degradation"

#: Marker of the oversized-input rule. A caller that can shrink what it sent and
#: try again recognizes the refusal here rather than by re-reading the text, the
#: way :data:`MODEL_CAPACITY_MARKER` is recognized.
CONTEXT_OVERFLOW_MARKER = "context-overflow"

# How much of stderr counts as launch time. The closure is a diagnostic the
# harness prints before it starts the turn; everything after it is the turn's
# own transcript, which can quote any text — including this very message — so
# scanning the whole stream would classify a turn by what it talked about.
_DEGRADATION_SCAN_MAX_CHARS = 2000

# A structured harness log line reporting that an auxiliary transport died —
# today codex's MCP client. Anchored to the log-line shape (level, then a
# transport-named module) rather than to the prose, for the same reason the
# scan is bounded.
_TRANSPORT_DEGRADATION_RE = re.compile(
    r"^\S*\s*(?:ERROR|FATAL)\s+\S*transport\S*[^\n]*?"
    r"(?:transport channel closed|quit with fatal)",
    re.IGNORECASE | re.MULTILINE,
)

# Ordered (marker, reason, retryable, pattern) rules. First match wins, so more
# specific / higher-priority categories (quota, auth, invalid model) precede the
# broader transient-transport rule. Markers are stable slugs used to build the
# normalized fingerprint.
_FAILURE_CLASSIFICATION_RULES: tuple[tuple[str, str, bool, re.Pattern[str]], ...] = (
    (
        # First, for the same reason ``invalid-model`` precedes
        # ``model-capacity``: the refusal is about the request the caller built,
        # and a turn whose input is a transcript of ASL itself will quote "api
        # key", "quota", and "rate limit" often enough that a later rule would
        # win the race and mislabel a call that never reached the model. Staying
        # ``nonzero-exit`` and non-retryable is deliberate — nothing here can be
        # relaunched unchanged; only the marker changes, so a caller that can
        # send less has something stable to branch on.
        CONTEXT_OVERFLOW_MARKER,
        events.PAUSE_REASON_NONZERO_EXIT,
        False,
        re.compile(
            r"prompt is too long|exceeds the maximum|"
            r"context (?:window )?(?:length )?exceeded",
            re.IGNORECASE,
        ),
    ),
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
        # Match authentication *failures*, not the whole word stem. A turn's
        # final message can legitimately describe authenticated application
        # launches; treating that affirmative prose as a harness credential
        # failure hides the real process-exit provenance. The bounded clauses
        # below still cover the verb forms providers actually use when login
        # fails.
        # ``oauth`` and an expired session join the expired token for the same
        # reason: they name the credential, not the request.
        #
        # Position is load-bearing and unchanged. ``usage-limit`` still
        # precedes this rule, so a quota message that also mentions a session
        # keeps its own category, and the ``CONTEXT_OVERFLOW_MARKER`` rule
        # still precedes both.
        "authentication",
        events.PAUSE_REASON_AUTH_FAILED,
        False,
        re.compile(
            r"\b401\b|unauthorized|"
            r"(?:failed|unable|cannot|can't|could not) to authenticat\w*|"
            r"authenticat\w*(?:\s+\w+){0,3}\s+(?:failed|failure|error)|"
            r"not authenticated|unauthenticated|not logged in|"
            r"invalid api key|invalid_api_key|missing api key|api key|"
            r"please (?:log ?in|sign in)|login required|"
            r"permission denied|forbidden|\b403\b|"
            r"(?:token|session) expired|oauth|"
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
        # ASI-135. A provider 500 is the one server-side status the broad
        # transport rule below cannot claim: `\b500\b` matches a line count, a
        # dollar figure, and a port number, and a turn whose input is a
        # transcript of ASL itself will contain all three. So this rule is
        # anchored on the envelope the provider writes rather than on the
        # number -- `API Error: 5xx`, the JSON `api_error` type, or the literal
        # server-error phrase -- which application prose does not produce by
        # accident. Placed after `model-capacity` because 529 is the more
        # specific outage, and before `transport` because a provider that
        # answered with an error is not a socket that failed.
        "provider-server-error",
        events.PAUSE_REASON_TRANSPORT_FAILED,
        True,
        re.compile(
            r"api error:?\s*5\d{2}\b|"
            r"\"type\"\s*:\s*\"api_error\"|"
            r"\binternal server error\b",
            re.IGNORECASE,
        ),
    ),
    (
        "transport",
        events.PAUSE_REASON_TRANSPORT_FAILED,
        True,
        re.compile(
            # Intervening words are tolerated between "connection" and its
            # verb: the observed failure said "the socket connection was closed
            # unexpectedly" and fell through to the fail-closed ``nonzero-exit``
            # that the literal "connection closed" alternative was meant to
            # catch. The window is bounded so the two words stay one clause.
            r"connection(?:\s+\w+){0,3}\s+(?:closed|reset|refused|dropped)|"
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


@dataclass(frozen=True)
class DegradationClassification:
    """A harness that lost an auxiliary transport and ran the turn anyway.

    Deliberately not a :class:`FailureClassification`: a degradation carries no
    pause reason and no retryability, so it can never decide what happens to the
    turn. ``affected_turn`` says whether the turn also failed, which is the
    difference between a lost capability and a lost turn.
    """

    marker: str
    evidence: str
    affected_turn: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "evidence": self.evidence,
            "affected_turn": self.affected_turn,
        }


def classify_harness_degradation(
    result: harnesses.HarnessResult,
) -> DegradationClassification | None:
    """Classify a launch-time auxiliary-transport closure, or ``None``.

    Reads stderr as evidence only. The turn keeps whatever status its own output
    and exit code earned; this states what the harness lost on the way up, so a
    recurring closure is recorded rather than silently repeated.
    """
    text = (result.stderr or "")[:_DEGRADATION_SCAN_MAX_CHARS]
    match = _TRANSPORT_DEGRADATION_RE.search(text)
    if match is None:
        return None
    return DegradationClassification(
        marker=TRANSPORT_DEGRADATION_MARKER,
        evidence=_bounded_evidence(text, match),
        affected_turn=result.returncode != 0 or result.timed_out,
    )


def classify_harness_failure(
    result: harnesses.HarnessResult,
) -> FailureClassification:
    """Classify a nonzero harness turn into a stable failure category.

    Recognizes an over-long prompt, quota/session exhaustion, authentication
    failures, invalid models, transient transport failures, and interruption;
    everything else fails closed as ``nonzero-exit``. Only transport failures
    are retryable, and an over-long prompt keeps the ``nonzero-exit`` reason —
    it is named by its marker so a caller that can send less can act on it.
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
