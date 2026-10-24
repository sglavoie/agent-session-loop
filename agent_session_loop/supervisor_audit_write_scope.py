"""Write-scope analysis and the scope-escape checks built on it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import events
from . import supervisor_audit_evidence_constants as audit_evidence_constants
from . import supervisor_audit_evidence_helpers as audit_evidence_helpers
from .supervisor_audit_commit_records import (
    _commit_attributions,
    _commit_repository_alias,
    _guard_head_moves,
    _write_scope_session,
)
from .supervisor_audit_models import _CheckContext, _joined, _names, _Verdict


def _write_scope_violated_aliases(guard: Mapping[str, Any]) -> list[str]:
    """The repositories one guard record says were written out of scope."""
    return [
        str(alias)
        for alias in (
            audit_evidence_helpers._mapping(item).get("alias")
            or audit_evidence_helpers._mapping(item).get("repository")
            for item in audit_evidence_helpers._sequence(guard.get("violations"))
        )
        if alias
    ]


#: The pause reasons a write-guard record can arrive under. Both stop the run on
#: the guard's own verdict; the unavailable one is chosen when the same record
#: also failed to inventory something, which does not make the violations it
#: does name any less paused-upon.
_WRITE_SCOPE_PAUSE_REASONS = (
    events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
    events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
)


_ViolationFingerprint = tuple[tuple[str, str, str, tuple[str, ...]], ...]


def _write_scope_violation_fingerprint(
    guard: Mapping[str, Any],
) -> _ViolationFingerprint:
    """Reduce one guard record's violations to a comparable identity.

    Two records describe the same out-of-scope write only if they name the same
    repositories and the same paths inside them, so that is all the fingerprint
    keeps. The order the guard happened to list them in is not evidence of
    anything and is normalized away.
    """
    entries = [
        (
            str(violation.get("kind") or ""),
            str(violation.get("alias") or ""),
            str(violation.get("repository") or ""),
            tuple(
                sorted(
                    str(path)
                    for path in audit_evidence_helpers._sequence(
                        violation.get("qualified_paths")
                    )
                    if path
                )
            ),
        )
        for violation in (
            audit_evidence_helpers._mapping(item)
            for item in audit_evidence_helpers._sequence(guard.get("violations"))
        )
    ]
    return tuple(sorted(entries))


@dataclass(frozen=True)
class _WriteScopeAnalysis:
    """One reading of the write guard, split by whether the guard held.

    ``caught`` and ``escaped`` are disjoint by construction: every out-of-scope
    write lands in exactly one of them, so the two checks that report this
    analysis can never both name the same write.
    """

    caught: tuple[str, ...]
    escaped: tuple[str, ...]
    blind: tuple[str, ...]
    observed: int
    evidence: tuple[str, ...]


def _analyze_write_scope(context: _CheckContext) -> _WriteScopeAnalysis:
    """Judge every write-guard record, separating a catch from an escape.

    A write is *caught* only when the guard recorded the violation, reverted
    the session to pending, paused the run on
    ``workspace-write-scope-violation``, and no commit that could have carried
    the write followed: that is a working guard, not a containment failure. A
    commit could have carried it when it names the offending session and either
    a repository the violation names or no repository at all. Anything
    weaker is an *escape* — the change reached the tree unauthorized. Blindness
    is neither, because a guard that could not look proves nothing either way.

    Violations are grouped by session rather than reported per record. The
    runner writes one guard record into the turn result and the same record
    into the pause it raised, and a resumed run replays that pause with no turn
    of its own, so grouping is what keeps one out-of-scope write one claim.

    A violation no record attributes is correlated to the session named by a
    pause that recorded the very same violation; if nothing durable names one,
    it can still be caught, but only in a bundle that recorded no commit at
    all, because there is no session to match a commit against and claiming the
    guard held is a claim about evidence that was never checked.
    """
    escaped: list[str] = []
    blind: list[str] = []
    references: list[str] = []
    observed = 0
    offenders: dict[str | None, set[str]] = {}
    reverted: set[str | None] = set()
    unnamed: dict[_ViolationFingerprint, set[str]] = {}
    unnamed_reverted: set[_ViolationFingerprint] = set()
    paused_fingerprints: dict[_ViolationFingerprint, set[str]] = {}
    paused_sessions: set[str] = set()
    paused_any = False
    offending_turns: set[str] = set()
    offending_heads: set[tuple[str | None, Any]] = set()

    def _claim(
        session: str | None,
        aliases: list[str],
        fingerprint: _ViolationFingerprint,
        was_reverted: bool,
    ) -> None:
        """File one guard record's violation under the session it names.

        An unattributed record is filed under the violation it describes rather
        than under a shared unnamed bucket, because what it describes is the
        only thing that can later tie it to a session.
        """
        if session is None:
            unnamed.setdefault(fingerprint, set()).update(aliases)
            if was_reverted:
                unnamed_reverted.add(fingerprint)
            return
        offenders.setdefault(session, set()).update(aliases)
        if was_reverted:
            reverted.add(session)

    for turn in context.turns:
        result = audit_evidence_helpers._mapping(turn.get("result"))
        guard = audit_evidence_helpers._mapping(result.get("workspace_write_guard"))
        if not guard:
            continue
        observed += 1
        if isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
        aliases = _write_scope_violated_aliases(guard)
        if aliases:
            session = _write_scope_session(
                guard.get("session_id"), result.get("session_id")
            )
            if isinstance(turn.get("name"), str):
                offending_turns.add(turn["name"])
            offending_heads.update(_guard_head_moves(guard))
            _claim(
                session,
                aliases,
                _write_scope_violation_fingerprint(guard),
                guard.get("reverted_to_pending") is True,
            )
        if audit_evidence_helpers._mapping(guard.get("unavailable")):
            blind.append(f"{turn.get('name')} could not be inventoried")

    def _pause(reason: Any, details: Mapping[str, Any]) -> None:
        """Read one scope pause, from the event log or the persisted status."""
        nonlocal observed, paused_any
        guard = audit_evidence_helpers._mapping(details.get("workspace_write_guard"))
        observed += 1
        if reason == events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE:
            blind.append("the write guard reported itself unavailable")
        aliases = _write_scope_violated_aliases(guard)
        # A guard that both found violations and failed to inventory
        # something pauses under the unavailable reason, because that is the
        # worse of the two. The pause is still the run stopping on this guard
        # record, and reading only the scope-violation reason reported a run
        # that did pause as one that never did (ASI-9). An unavailable pause
        # naming no violation is only blindness, and corroborates nothing.
        scope_pause = reason == events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION
        if not (aliases or scope_pause):
            return
        paused_any = True
        session = _write_scope_session(
            guard.get("session_id"),
            details.get("session_id"),
            details.get("session"),
        )
        if session is not None:
            paused_sessions.add(session)
        if aliases:
            offending_heads.update(_guard_head_moves(guard))
            fingerprint = _write_scope_violation_fingerprint(guard)
            if session is not None:
                paused_fingerprints.setdefault(fingerprint, set()).add(session)
            _claim(
                session,
                aliases,
                fingerprint,
                guard.get("reverted_to_pending") is True,
            )

    pause_recorded = False
    for record in context.records:
        name = record.get("name")
        record_payload = audit_evidence_helpers._mapping(record.get("payload"))
        reason = record_payload.get("reason")
        details = audit_evidence_helpers._mapping(record_payload.get("details"))
        guard = audit_evidence_helpers._mapping(details.get("workspace_write_guard"))
        if name == events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT:
            observed += 1
            repair_aliases = _write_scope_violated_aliases(guard)
            if repair_aliases:
                # The repair is the run's second chance at containment: a
                # widened scope that still excludes a retained change means the
                # write outlived the guard rather than being held by it.
                escaped.append(
                    "the widened write scope still excludes retained changes "
                    f"({_names(repair_aliases)})"
                )
            elif audit_evidence_helpers._mapping(guard.get("unavailable")):
                blind.append("the widened write scope could not be inventoried")
            elif guard.get("passed") is not True:
                blind.append(
                    "the write-scope repair did not record a passing guard result"
                )
        elif name != events.RUN_PAUSED_EVENT:
            continue
        else:
            pause_recorded = True
            if reason in _WRITE_SCOPE_PAUSE_REASONS:
                _pause(reason, details)

    # The persisted status carries the same pause the event log does, so a
    # bundle whose event records the size cap dropped still holds the pause
    # there. It is read only when the log kept no pause of its own, so one
    # pause is never counted twice.
    status = context.section("status")
    if (
        context.events_shed
        and not pause_recorded
        and status.get("pause_reason") in _WRITE_SCOPE_PAUSE_REASONS
    ):
        _pause(
            status.get("pause_reason"),
            audit_evidence_helpers._mapping(status.get("pause_details")),
        )

    def _self_observed(entry: Mapping[str, Any]) -> bool:
        """Whether a commit is the offending turn's own head move, not a carry.

        The audit derives an ``observed-write-guard`` commit from the head
        transition the write guard saw during a turn. When that turn is the one
        the guard caught, the derived commit is the violation itself as the
        guard observed it rather than a later commit that carried the write
        forward, so counting it made every caught head move read as an escape.
        """
        if entry.get("origin") != audit_evidence_constants.OBSERVED_COMMIT_ORIGIN:
            return False
        if entry.get("turn") in offending_turns:
            return True
        return (
            _commit_repository_alias(entry),
            entry.get("head_after"),
        ) in offending_heads

    attributions = _commit_attributions(context, skip=_self_observed)
    any_commit = bool(attributions)

    # The runner writes one guard record into the turn result and the same
    # record into the pause it raises, so a bundle where only the pause carries
    # the session id splits one write into a named claim and an unnamed one.
    # Rejoining those halves needs proof that they are halves of one write, and
    # the guard's own violation list — the repositories it names and the paths
    # inside them — is the only durable evidence of that. An unnamed record
    # describing something no pause described is a second, distinct violation
    # that keeps its own unattributed reading, and a violation two pauses
    # attribute to two sessions makes the choice a guess, so it stays unnamed
    # too and is judged as ambiguous below.
    for fingerprint, unnamed_aliases in unnamed.items():
        candidates = paused_fingerprints.get(fingerprint, set())
        attributed = next(iter(candidates)) if len(candidates) == 1 else None
        offenders.setdefault(attributed, set()).update(unnamed_aliases)
        if fingerprint in unnamed_reverted:
            reverted.add(attributed)

    def _paused_for(session: str | None) -> bool:
        """Whether a scope-violation pause corroborates this session's catch.

        A pause names the session it stopped, so the match is per session. A
        bundle that recorded the pause without attributing it falls back to the
        run-level fact, because an unattributable pause is still a pause and
        reading it as none would report a working guard as an escape.
        """
        if session is not None and paused_sessions:
            return session in paused_sessions
        return paused_any

    pause_unknown = context.events_shed and not paused_any
    caught: list[str] = []
    for session, offending_aliases in sorted(
        offenders.items(), key=lambda item: item[0] or ""
    ):
        who = f"session {session}" if session is not None else "an unnamed session"
        claim = f"{who} wrote outside its declared scope ({_names(offending_aliases)})"
        # A commit naming the offending session cannot predate the guard, which
        # runs after the turn it judges, so its presence says the run carried
        # the out-of-scope write forward instead of holding it. The commit must
        # also be able to carry *this* write: a commit writes one repository's
        # history, so one in a repository the violation does not name cannot
        # have carried it and does not defeat the catch (ASI-9). A commit whose
        # repository the record does not say still can, and is counted. A
        # violation no record attributes cannot be matched against a commit by
        # session at all, so every commit in the bundle is a candidate for
        # having carried it and the catch is conceded rather than claimed on
        # evidence that was never checked.
        if session is None:
            uncommitted = not any_commit
            commit_note = (
                "the run recorded commits that cannot be ruled out as carrying "
                "the unattributed violation"
            )
        else:
            uncommitted = not any(
                committed_session == session
                and (committed_alias is None or committed_alias in offending_aliases)
                for committed_session, committed_alias in attributions
            )
            commit_note = (
                "a commit for that session followed that could have carried the write"
            )
        missing = [
            (key, note)
            for key, note, held in (
                (
                    "reverted",
                    "the session was not reverted to pending",
                    session in reverted,
                ),
                (
                    "paused",
                    "the run never paused on the violation",
                    _paused_for(session),
                ),
                ("committed", commit_note, uncommitted),
            )
            if not held
        ]
        # The pause is the one fact here that lives only in the event log, so a
        # bundle whose event records the size cap dropped cannot say the run
        # never paused -- only that this bundle does not hold the record. Where
        # that is the sole thing standing between the guard and a catch, the
        # honest reading is that containment could not be judged, not that a
        # write escaped (ASI-9).
        if pause_unknown and [key for key, _ in missing] == ["paused"]:
            blind.append(
                f"{claim}, and the event records the size cap dropped are where "
                "the pause that would settle it would be"
            )
        elif missing:
            escaped.append(f"{claim} and {', '.join(note for _, note in missing)}")
        else:
            caught.append(
                f"{claim}, and the guard reverted it to pending and paused the "
                "run before anything was committed"
            )

    return _WriteScopeAnalysis(
        caught=tuple(caught),
        escaped=tuple(escaped),
        blind=tuple(blind),
        observed=observed,
        evidence=context.evidence(
            *references, context.section("state").get("reference")
        ),
    )


def _check_scope_escape(context: _CheckContext) -> _Verdict:
    """Fail only for a write that got past the guard, never for one it held."""
    analysis = _analyze_write_scope(context)
    if analysis.escaped:
        return _Verdict("fail", _joined(analysis.escaped), analysis.evidence)
    if analysis.blind:
        return _Verdict(
            "unavailable",
            "the workspace write guard could not judge containment: "
            f"{_joined(analysis.blind)}",
            analysis.evidence,
        )
    if not analysis.observed:
        return _Verdict(
            "unavailable",
            "no turn recorded a workspace write guard result, so scope "
            "containment cannot be judged",
            analysis.evidence,
        )
    if analysis.caught:
        return _Verdict(
            "pass",
            f"the guard held every out-of-scope write across {analysis.observed} "
            "recorded write-guard and repair results, so none of them reached "
            "the tree unauthorized",
            analysis.evidence,
        )
    return _Verdict(
        "pass",
        f"all {analysis.observed} recorded write-guard and repair results kept "
        "every retained write inside the session's declared scope",
        analysis.evidence,
    )


def _check_scope_guard_catch(context: _CheckContext) -> _Verdict:
    """Report the writes the guard stopped, which are a plan defect, not a leak.

    Never ``unavailable``: a guard that could not look is reported by
    :func:`_check_scope_escape`, which owns the critical reading, and both
    checks share one lane so the rollup keeps the worse of the two.

    That division decides the *status* and must not decide the *sentence*. A
    guard can refuse a turn through its blind lane alone -- an unclassified
    external write leaves ``violations`` empty while the turn is still reverted
    to pending and the run still paused -- and a clear verdict reading "no
    session had to be stopped" then states the opposite of what the run
    recorded (ASI-140). The clear reading is therefore narrowed to what this
    check can actually name, and blindness is said out loud along with which
    check owns it, so the report never answers for a place it did not look.
    """
    analysis = _analyze_write_scope(context)
    if analysis.caught:
        return _Verdict("fail", _joined(analysis.caught), analysis.evidence)
    if analysis.blind:
        return _Verdict(
            "pass",
            "no session had to be stopped for a write this check could "
            f"attribute to a declared scope, but {_joined(analysis.blind)}, so "
            "whether one was stopped for an unattributable write is "
            "scope-escape's to report",
            analysis.evidence,
        )
    return _Verdict(
        "pass",
        "no session had to be stopped for writing outside its declared scope",
        analysis.evidence,
    )
