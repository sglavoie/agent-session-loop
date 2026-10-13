"""The check specification, verdict and evidence context checks run on."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from . import assurance_models_vocab
from . import supervisor_audit_evidence_constants as audit_evidence_constants
from . import supervisor_audit_evidence_helpers as audit_evidence_helpers
from .supervisor_audit_vocabulary import _CONTROL_CHAR_RE, RUN_ANCHOR


@dataclass(frozen=True)
class CheckSpec:
    """The stable identity one deterministic check reports under.

    The invariant key is the check's contract with the issue inbox: it feeds
    the finding fingerprint, so the same defect in two runs deduplicates into
    one issue with two occurrences. Renaming one splits an issue's history.

    ``replayable`` records whether the check's verdict is a function of the
    bundle alone. Almost every check reads nothing else, which is what lets
    ``audit_replay`` re-judge a recorded bundle at HEAD and call the answer
    proof. A check that also consults the live filesystem answers about the
    tree as it stands now, not as the run left it, so replaying it can report a
    transition no commit caused. Those checks still run and still report on the
    live audit path; they are excluded from the replay matrix, where a verdict
    that cannot be reproduced is worse than no verdict at all.
    """

    check: str
    lane: assurance_models_vocab.RubricLane
    category: str
    component: str
    invariant_key: str
    severity: assurance_models_vocab.Severity
    recommendation: str
    replayable: bool = True


@dataclass(frozen=True)
class _Verdict:
    """What one check concluded, before it is stamped with its spec."""

    status: assurance_models_vocab.CheckStatus
    detail: str
    evidence: tuple[str, ...]


def _detail(text: str) -> str:
    """Bound and flatten a detail so it survives ``DeterministicResult``."""
    collapsed = " ".join(_CONTROL_CHAR_RE.sub(" ", str(text)).split())
    return collapsed[: assurance_models_vocab.MAX_TEXT_CHARS] or "no detail recorded"


def _names(values: Iterable[Any], *, limit: int = 8) -> str:
    items = sorted({str(value) for value in values if value})
    if not items:
        return "none"
    shown = ", ".join(items[:limit])
    if len(items) > limit:
        return f"{shown}, and {len(items) - limit} more"
    return shown


def _inside(locator: Any, root: str) -> bool:
    return isinstance(locator, str) and (
        locator == root or locator.startswith(f"{root}/")
    )


def _joined(claims: Iterable[str]) -> str:
    return "; ".join(sorted(set(claims)))


class _CheckContext:
    """A read-only view of one evidence bundle, shared by every check.

    Every path in the bundle has already been rewritten into a symbolic
    locator, so containment and agreement are decided by string comparison
    against an approved root rather than by touching the filesystem.
    """

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = payload
        self.roots = {
            str(name): str(value)
            for name, value in audit_evidence_helpers._mapping(
                payload.get("roots")
            ).items()
        }
        self.declared = {
            str(entry.get("locator"))
            for entry in audit_evidence_helpers._sequence(payload.get("evidence"))
            if isinstance(entry, Mapping) and entry.get("locator")
        }
        self.turns = [
            turn
            for turn in audit_evidence_helpers._sequence(payload.get("turns"))
            if isinstance(turn, Mapping)
        ]
        self.records = [
            record
            for record in audit_evidence_helpers._sequence(
                self.section("events").get("records")
            )
            if isinstance(record, Mapping)
        ]
        self.omissions = [
            entry
            for entry in audit_evidence_helpers._sequence(payload.get("omissions"))
            if isinstance(entry, Mapping)
        ]

    def section(self, name: str) -> dict[str, Any]:
        return audit_evidence_helpers._mapping(self.payload.get(name))

    @property
    def events_shed(self) -> bool:
        """Whether the size cap dropped event records the checks would read."""
        return any(
            entry.get("section") == "events"
            and entry.get("reason") == audit_evidence_constants.REASON_SIZE_CAP
            for entry in self.omissions
        )

    @property
    def terminal_pending(self) -> bool:
        """Whether the run's terminal record is still owed at collection time.

        An audit that runs inside its own run reads the history before the
        terminal record exists. Absence then proves nothing, so the checks that
        read for a missing terminal record stay silent instead of reporting the
        ordering the audit itself requires.
        """
        return bool(self.section("events").get("terminal_pending"))

    @property
    def event_summary(self) -> dict[str, Any]:
        return audit_evidence_helpers._mapping(self.section("events").get("summary"))

    def turn_directories(self) -> list[str]:
        return [
            turn["directory"]
            for turn in self.turns
            if isinstance(turn.get("directory"), str)
        ]

    def evidence(self, *candidates: Any) -> tuple[str, ...]:
        """Keep the declared locators among ``candidates``, else the anchor."""
        chosen = [
            locator
            for locator in dict.fromkeys(candidates)
            if isinstance(locator, str) and locator in self.declared
        ]
        return tuple(chosen[: assurance_models_vocab.MAX_REFERENCES_PER_ITEM]) or (
            RUN_ANCHOR,
        )
