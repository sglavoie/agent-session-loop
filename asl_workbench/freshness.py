"""Three independent questions about one observation, kept independent.

Transport liveness, projection age, and projection completeness fail in
different ways and for different reasons, and collapsing them into one "is it
up" flag is how a cockpit ends up confidently wrong. A connector can be
heartbeating perfectly while the process behind it stopped projecting; a
projection can be current and still be missing rows because the snapshot hit a
frame ceiling. So :class:`Freshness` keeps all three answers and derives a
single label from them, rather than computing the label and discarding the
evidence.

Two rules are worth stating outright, because both are the difference between
a dashboard that is honest and one that invents endings:

* **A partial view never proves an omitted run disappeared.** Absence is
  evidence only when the observation that omitted the run claimed to be whole,
  and was recent enough to be describing the present. That is
  :attr:`Freshness.absence_proves_removal`, and
  :mod:`asl_workbench.store_workbench` is the caller that acts on it.

* **Truncation does not block a reply.** The rows that did arrive are as
  current as any other row. It is the rows that did *not* arrive that are
  unknown, and no reply is being sent to those.

The evaluator takes an :class:`~asl_workbench.observation.ObservationPolicy`
rather than a service configuration, so the same function answers for the local
Workbench, for a remote connector, and for a test with no configuration at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .observation import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_PARTIAL,
    COMPLETENESS_UNKNOWN,
    ObservationPolicy,
    completeness_of,
    proves_removal,
)

# -- Vocabulary -------------------------------------------------------------

#: Transport and projection are both current.
FRESHNESS_LIVE = "live"
#: Something is behind, but not far enough behind to have been given up on.
FRESHNESS_STALE = "stale"
#: Nothing has been observed, or what was observed is too old to describe now.
FRESHNESS_LOST = "lost"
#: The source speaks a contract this application does not read.
FRESHNESS_INCOMPATIBLE = "incompatible"
FRESHNESS_LABELS: tuple[str, ...] = (
    FRESHNESS_LIVE,
    FRESHNESS_STALE,
    FRESHNESS_LOST,
    FRESHNESS_INCOMPATIBLE,
)

#: Worst label wins when several sources report. Ordered from healthiest.
_FRESHNESS_RANK: dict[str, int] = {
    FRESHNESS_LIVE: 0,
    FRESHNESS_STALE: 1,
    FRESHNESS_INCOMPATIBLE: 2,
    FRESHNESS_LOST: 3,
}

REASON_NO_HEARTBEAT = "no connector heartbeat has been received"
REASON_STALE_HEARTBEAT = "connector heartbeat is stale"
REASON_NO_SNAPSHOT = "no ASL snapshot has been received"
REASON_STALE_PROJECTION = "ASL projection is stale"
REASON_TRUNCATED = "snapshot was truncated; some runs are not represented"
REASON_INCOMPATIBLE = "ASL contract is not supported by this deployment"
REASON_OBSERVATION_ONLY = "this deployment is running in observation-only mode"


@dataclass(frozen=True)
class Freshness:
    """One evaluated observation, kept in its three separate parts."""

    label: str
    transport_fresh: bool
    projection_fresh: bool
    completeness: str
    transport_age_seconds: float | None
    projection_age_seconds: float | None
    replies_enabled: bool
    reasons: tuple[str, ...]

    @property
    def absence_proves_removal(self) -> bool:
        """Whether a run missing from this observation may be treated as gone."""
        return proves_removal(self.completeness, projection_fresh=self.projection_fresh)

    def as_document(self) -> dict[str, object]:
        return {
            "freshness": self.label,
            "transport_fresh": self.transport_fresh,
            "projection_fresh": self.projection_fresh,
            "transport_age_seconds": self.transport_age_seconds,
            "projection_age_seconds": self.projection_age_seconds,
            "completeness": self.completeness,
            "absence_proves_removal": self.absence_proves_removal,
            "replies_enabled": self.replies_enabled,
            "reasons": list(self.reasons),
        }


def evaluate(
    *,
    now: float,
    policy: ObservationPolicy,
    last_heartbeat_at: float | None,
    last_snapshot_at: float | None,
    truncated: bool | None,
    compatible: bool = True,
) -> Freshness:
    """Classify the newest observation a source produced.

    ``None`` for either timestamp means nothing has been observed yet, which is
    treated as lost rather than fresh: the application has to be able to start
    up knowing that it does not know.
    """
    transport_age = None if last_heartbeat_at is None else now - last_heartbeat_at
    projection_age = None if last_snapshot_at is None else now - last_snapshot_at
    reasons: list[str] = []

    transport_fresh = (
        transport_age is not None and transport_age <= policy.observation_stale_seconds
    )
    if transport_age is None:
        reasons.append(REASON_NO_HEARTBEAT)
    elif not transport_fresh:
        reasons.append(REASON_STALE_HEARTBEAT)

    projection_fresh = (
        projection_age is not None
        and projection_age <= policy.observation_stale_seconds
    )
    if projection_age is None:
        reasons.append(REASON_NO_SNAPSHOT)
    elif not projection_fresh:
        reasons.append(REASON_STALE_PROJECTION)

    completeness = completeness_of(truncated)
    if completeness == COMPLETENESS_PARTIAL:
        reasons.append(REASON_TRUNCATED)

    if not compatible:
        reasons.append(REASON_INCOMPATIBLE)

    threshold = policy.lost_observation_alert_seconds
    lost = (
        transport_age is None
        or projection_age is None
        or transport_age > threshold
        or projection_age > threshold
    )
    if not compatible:
        label = FRESHNESS_INCOMPATIBLE
    elif lost:
        label = FRESHNESS_LOST
    elif transport_fresh and projection_fresh:
        label = FRESHNESS_LIVE
    else:
        label = FRESHNESS_STALE

    # Truncation is deliberately absent from this expression. See the module
    # docstring: a truncated view's missing rows are unknown, and no reply is
    # being sent to a row nobody can see.
    replies_enabled = (
        compatible and transport_fresh and projection_fresh and policy.replies_permitted
    )
    if policy.observation_only and compatible:
        reasons.append(REASON_OBSERVATION_ONLY)

    return Freshness(
        label=label,
        transport_fresh=transport_fresh,
        projection_fresh=projection_fresh,
        completeness=completeness,
        transport_age_seconds=transport_age,
        projection_age_seconds=projection_age,
        replies_enabled=replies_enabled,
        reasons=tuple(reasons),
    )


# -- Incident thresholds ----------------------------------------------------


def lost_observation_incident(
    *,
    now: float,
    policy: ObservationPolicy,
    active_work_last_seen_at: float | None,
) -> bool:
    """Whether losing sight of active work has crossed the alert threshold.

    Only active work counts. A corpus with nothing running is not an incident
    however long it stays silent, so the caller passes a timestamp only when it
    has active work it can no longer see.
    """
    if active_work_last_seen_at is None:
        return False
    return now - active_work_last_seen_at > policy.lost_observation_alert_seconds


def unanswered_question_incident(
    *, now: float, policy: ObservationPolicy, asked_at: float | None
) -> bool:
    """Whether a live question has gone unanswered past the alert threshold."""
    if asked_at is None:
        return False
    return now - asked_at > policy.unanswered_question_alert_seconds


def expired_summary(
    *, now: float, observed_at: float, policy: ObservationPolicy
) -> bool:
    """Whether a stored observation has aged out of the retention window."""
    return now - observed_at > policy.summary_retention_seconds


# -- Aggregation ------------------------------------------------------------


def aggregate_completeness(values: Iterable[str]) -> str:
    """The weakest completeness among several sources.

    Partial beats unknown beats complete, because a cockpit has to render one
    banner and the honest one is the worst true statement about any source.
    """
    seen = set(values)
    if not seen:
        return COMPLETENESS_UNKNOWN
    if COMPLETENESS_PARTIAL in seen:
        return COMPLETENESS_PARTIAL
    if COMPLETENESS_UNKNOWN in seen:
        return COMPLETENESS_UNKNOWN
    return COMPLETENESS_COMPLETE


def aggregate_freshness(labels: Sequence[str]) -> str:
    """The worst freshness among several sources, or ``lost`` when there are none.

    No sources is ``lost`` rather than ``live`` for the same reason an
    unobserved timestamp is: a deployment that has heard from nobody knows
    nothing, and the label has to say so.
    """
    if not labels:
        return FRESHNESS_LOST
    return max(labels, key=lambda label: _FRESHNESS_RANK.get(label, 3))


__all__ = [
    "FRESHNESS_INCOMPATIBLE",
    "FRESHNESS_LABELS",
    "FRESHNESS_LIVE",
    "FRESHNESS_LOST",
    "FRESHNESS_STALE",
    "Freshness",
    "aggregate_completeness",
    "aggregate_freshness",
    "evaluate",
    "expired_summary",
    "lost_observation_incident",
    "unanswered_question_incident",
]
