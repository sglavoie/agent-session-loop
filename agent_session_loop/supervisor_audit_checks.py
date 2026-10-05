"""Deterministic checks over a supervisor audit's evidence bundle.

Reading the bundle is a separate job from collecting it. The deterministic
checks turn known failure modes into reproducible verdicts: each answers
``pass``, ``fail``, or ``unavailable`` under a stable invariant key and names
the evidence it read. Evidence a check needed but could not see is
``unavailable``, never ``pass``, so a shed log or an unwritten result file
reads as a gap in the audit rather than as a clean run.

The same rule runs the other way for a claim built on a record's *absence*. The
size cap sheds by usefulness, not at random, so the records it keeps are the
ones that read as evidence of a problem and the records it drops are often the
ones that would refute them. A check that infers a fault from a record it
cannot find must therefore ask whether the log was shed before it counts the
absence as evidence.

The checks read only :mod:`.supervisor_audit_evidence`; the agent audit and the
:mod:`.supervisor_audit` entry module read them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import (
    assurance_models,
    commit_groups,
    events,
    pause_policy,
    plans,
    preflight,
    review_continuation,
    state,
    verification,
)
from . import supervisor_audit_evidence as audit_evidence
from .assurance_models import (
    Finding,
)

#: Historical artifact name, retained when interpreting old audit evidence.
MUTATION_ARTIFACT_NAME = "supervisor-audit-mutation.json"
WORKSPACE_CHANGE_ARTIFACT_NAME = "supervisor-audit-workspace-change.json"


#: The locator every check falls back to. ``collect_audit_input`` registers the
#: run directory first, so this is always in the evidence index, and a result
#: that read nothing else still says where it looked.
RUN_ANCHOR = audit_evidence.EVIDENCE_ROOT_RUN

#: Event names a deterministic check reads. A record that reports a failure
#: under any other name lands in the unknown-anomalies lane rather than going
#: unread, which is what keeps the rubric's last lane honest.
_CLASSIFIED_EVENT_NAMES = frozenset(
    {
        audit_evidence._RUN_STARTED_EVENT,
        events.COMMIT_FINISHED_EVENT,
        # Earned by `_check_paused_work_settlement`, which grades every field
        # of this record. It is listed here only because that check owns it:
        # the record carries the pause's reason, so the catch-all read every
        # successful settlement as an unclassified failure.
        events.PAUSED_WORK_COMMITTED_EVENT,
        events.PROCESS_SWEEP_EVENT,
        events.REVIEW_VERDICT_RETRY_EVENT,
        events.RUN_FINISHED_EVENT,
        events.RUN_PAUSED_EVENT,
        events.TURN_HANG_CLEARED_EVENT,
        events.TURN_HANG_SUSPECTED_EVENT,
        events.VERIFICATION_CHECK_FINISHED_EVENT,
        events.VERIFICATION_CHECK_SKIPPED_EVENT,
        events.VERIFICATION_FINISHED_EVENT,
        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
    }
)

#: Every outcome the runner records for a commit it ran, mirrored as literals
#: because ``runner`` imports this module and never the other way round.
#: ``success`` is listed beside the failures so a record naming a token from
#: neither set reads as an outcome nobody defined rather than as a clean one.
_COMMIT_FAILURE_RESULTS = frozenset(
    {
        "dirty-after-commit",
        "missing-head",
        "nonzero-exit",
        "status-unavailable",
        "unchanged-head",
    }
)
_COMMIT_RESULTS = _COMMIT_FAILURE_RESULTS | {"success"}

#: Every verdict the paused-work settlement records under ``result``, mirrored
#: as literals for the same reason as :data:`_COMMIT_RESULTS`. The set is
#: closed: ``commit_settlement`` picks exactly one of these on every path, so a
#: record naming anything else is an outcome nobody defined.
_PAUSED_WORK_RESULT_FAILED = "failed"
_PAUSED_WORK_RESULT_RESIDUE = "committed-with-residue"
_PAUSED_WORK_RESULT_COMMITTED = "committed"
_PAUSED_WORK_RESULTS = frozenset(
    {
        _PAUSED_WORK_RESULT_FAILED,
        "no-changes",
        _PAUSED_WORK_RESULT_RESIDUE,
        _PAUSED_WORK_RESULT_COMMITTED,
    }
)

#: The records that report one session's turn outcome. A pause names the
#: session it died on, so these are the only names its classification can
#: account for.
_PAUSED_TURN_EVENT_NAMES = frozenset(
    {
        events.TURN_EXECUTION_FINISHED_EVENT,
        events.TURN_FINISHED_EVENT,
    }
)

#: Records that describe work in progress. One of these after a run's terminal
#: record means the run kept working after it declared itself over -- unless it
#: is part of the one epilogue a run is allowed, see
#: :func:`_paused_work_settlement_group_ids`.
_POST_TERMINAL_WORK_EVENTS = frozenset(
    {
        events.COMMIT_FINISHED_EVENT,
        events.COMMIT_STARTED_EVENT,
        events.TURN_HEARTBEAT_EVENT,
        events.VERIFICATION_CHECK_FINISHED_EVENT,
        events.VERIFICATION_CHECK_STARTED_EVENT,
        events.VERIFICATION_FINISHED_EVENT,
        events.VERIFICATION_STARTED_EVENT,
    }
)

#: The records the paused-work settlement's own commit group writes. They carry
#: ``scope: "session"`` like any other session commit -- the settlement scope
#: lives on the group, not on the commit -- so they are recognized by the group
#: they name rather than by anything on the record itself.
_PAUSED_WORK_SETTLEMENT_COMMIT_EVENTS = frozenset(
    {
        events.COMMIT_FINISHED_EVENT,
        events.COMMIT_STARTED_EVENT,
    }
)

#: Turn-result keys naming an artifact, paired with the basename that artifact
#: must carry inside the turn directory. ``None`` names the directory itself.
_TURN_PATH_KEYS: tuple[tuple[str, str | None], ...] = (
    ("artifact_dir", None),
    ("stdout_path", "stdout.log"),
    ("stderr_path", "stderr.log"),
    ("last_message_path", "last-message.txt"),
    ("result_path", state.RESULT_FILE_NAME),
)

#: Lane rollup keeps the least reassuring status any of its checks returned.
_STATUS_PRECEDENCE = {"pass": 0, "unavailable": 1, "fail": 2}

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


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
    lane: assurance_models.RubricLane
    category: str
    component: str
    invariant_key: str
    severity: assurance_models.Severity
    recommendation: str
    replayable: bool = True


@dataclass(frozen=True)
class _Verdict:
    """What one check concluded, before it is stamped with its spec."""

    status: assurance_models.CheckStatus
    detail: str
    evidence: tuple[str, ...]


def _detail(text: str) -> str:
    """Bound and flatten a detail so it survives ``DeterministicResult``."""
    collapsed = " ".join(_CONTROL_CHAR_RE.sub(" ", str(text)).split())
    return collapsed[: assurance_models.MAX_TEXT_CHARS] or "no detail recorded"


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
            for name, value in audit_evidence._mapping(payload.get("roots")).items()
        }
        self.declared = {
            str(entry.get("locator"))
            for entry in audit_evidence._sequence(payload.get("evidence"))
            if isinstance(entry, Mapping) and entry.get("locator")
        }
        self.turns = [
            turn
            for turn in audit_evidence._sequence(payload.get("turns"))
            if isinstance(turn, Mapping)
        ]
        self.records = [
            record
            for record in audit_evidence._sequence(
                self.section("events").get("records")
            )
            if isinstance(record, Mapping)
        ]
        self.omissions = [
            entry
            for entry in audit_evidence._sequence(payload.get("omissions"))
            if isinstance(entry, Mapping)
        ]

    def section(self, name: str) -> dict[str, Any]:
        return audit_evidence._mapping(self.payload.get(name))

    @property
    def events_shed(self) -> bool:
        """Whether the size cap dropped event records the checks would read."""
        return any(
            entry.get("section") == "events"
            and entry.get("reason") == audit_evidence.REASON_SIZE_CAP
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
        return audit_evidence._mapping(self.section("events").get("summary"))

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
        return tuple(chosen[: assurance_models.MAX_REFERENCES_PER_ITEM]) or (
            RUN_ANCHOR,
        )


def _check_root_registry_agreement(context: _CheckContext) -> _Verdict:
    plan = context.section("plan")
    metadata = audit_evidence._mapping(plan.get("metadata"))
    registry = context.section("registry")
    evidence = context.evidence(
        metadata.get("reference"),
        context.section("state").get("reference"),
        plan.get("reference"),
    )
    if audit_evidence.EVIDENCE_ROOT_WORKSPACE not in context.roots:
        return _Verdict(
            "unavailable",
            "the run declared no workspace root, so no binding can be compared "
            "against one",
            evidence,
        )

    bindings: list[tuple[str, Any]] = []
    content = audit_evidence._mapping(metadata.get("content"))
    if metadata.get("available") and content.get("workspace_root") is not None:
        bindings.append(("the plan metadata", content.get("workspace_root")))
    if registry.get("registered"):
        if registry.get("workspace_root") is not None:
            bindings.append(("the run registry", registry.get("workspace_root")))
        for entry in audit_evidence._sequence(registry.get("repositories")):
            entry = audit_evidence._mapping(entry)
            alias = entry.get("alias") or "an unnamed repository"
            # ``path`` is deliberately workspace-relative in current runner
            # state.  Containment is about the repository ASL actually
            # resolved, so only the canonical ``resolved_root`` can establish
            # it.  Older, malformed, outside-root, or redacted entries fail
            # closed instead of being made safe by resolving a relative path
            # against the auditor process's cwd.
            bindings.append(
                (f"registered repository {alias}", entry.get("resolved_root"))
            )
    if not bindings:
        return _Verdict(
            "unavailable",
            "neither the plan metadata nor the run registry recorded a workspace "
            "binding to compare against the run's root, which is what a plan "
            "that predates workspace registration looks like rather than "
            "evidence of a disagreement",
            evidence,
        )

    disagreeing = [
        (
            f"{source} records no resolved root"
            if value is None
            else f"{source} resolves to {value}"
        )
        for source, value in bindings
        if value is None or not _inside(value, audit_evidence.EVIDENCE_ROOT_WORKSPACE)
    ]
    if disagreeing:
        return _Verdict(
            "fail",
            "the run's workspace root disagrees with what its own records bind "
            f"it to: {_joined(disagreeing)}",
            evidence,
        )
    return _Verdict(
        "pass",
        f"all {len(bindings)} recorded workspace bindings resolve inside the "
        "run's workspace root",
        evidence,
    )


def _check_repository_intent(context: _CheckContext) -> _Verdict:
    plan = context.section("plan")
    evidence = context.evidence(
        plan.get("reference"), context.section("state").get("reference")
    )
    plan_root = context.roots.get(audit_evidence.EVIDENCE_ROOT_PLAN)
    workspace_root = context.roots.get(audit_evidence.EVIDENCE_ROOT_WORKSPACE)
    if not plan_root or not workspace_root or not Path(plan_root).is_dir():
        return _Verdict(
            "unavailable",
            "the run recorded no plan directory and workspace root to replay the "
            "repository-intent gate against",
            evidence,
        )
    # Replaying the gate reads the plan through ``plans``, which rolls a pending
    # publication forward as a side effect. An audit observes a run; it does not
    # finish one, so an interrupted plan is reported rather than repaired.
    if (Path(plan_root) / plans.PLAN_TRANSACTION_DIR_NAME).exists():
        return _Verdict(
            "unavailable",
            "the plan directory holds a pending publication transaction, and "
            "replaying the repository-intent gate would complete it rather than "
            "observe it",
            evidence,
        )
    exempt = [
        context.roots[name]
        for name in (
            audit_evidence.EVIDENCE_ROOT_STATE,
            audit_evidence.EVIDENCE_ROOT_RUN,
        )
        if context.roots.get(name)
    ]
    try:
        intent, report = preflight.plan_repository_intent(
            plan_root, workspace_root, extra_exempt_roots=exempt
        )
    except (OSError, ValueError, plans.PlanError) as error:
        return _Verdict(
            "unavailable",
            f"the repository-intent gate could not be replayed after the run: {error}",
            evidence,
        )
    if report is None:
        if intent is not None:
            reachable = _names(Path(root).name for root in intent.git_roots)
            return _Verdict(
                "pass",
                "the legacy plan stays within its bound Git repository"
                + (f": {reachable}" if reachable else ""),
                evidence,
            )
        return _Verdict(
            "pass",
            "the plan declares a repository registry naming every repository it "
            "may reach",
            evidence,
        )
    reachable = _names(
        Path(str(root)).name
        for root in audit_evidence._sequence(report.get("git_roots"))
    )
    return _Verdict(
        "fail",
        f"the plan reaches {report.get('git_root_count')} git repositories but "
        f"declares no repository registry: {reachable}",
        evidence,
    )


def _check_task_packet_paths(context: _CheckContext) -> _Verdict:
    contradictions: list[str] = []
    references: list[str] = []
    observed = 0
    workspace_declared = audit_evidence.EVIDENCE_ROOT_WORKSPACE in context.roots
    for turn in context.turns:
        directory = turn.get("directory")
        result = audit_evidence._mapping(turn.get("result"))
        if not isinstance(directory, str) or result.get("available") is False:
            continue
        references.append(directory)
        for key, basename in _TURN_PATH_KEYS:
            value = result.get(key)
            if value is None:
                continue
            observed += 1
            expected = directory if basename is None else f"{directory}/{basename}"
            if value != expected:
                contradictions.append(
                    f"{turn.get('name')} names {value} as its {key} rather than "
                    f"{expected}"
                )
        cwd = audit_evidence._mapping(result.get("invocation")).get("cwd")
        if isinstance(cwd, str) and workspace_declared:
            observed += 1
            if not _inside(cwd, audit_evidence.EVIDENCE_ROOT_WORKSPACE):
                contradictions.append(
                    f"{turn.get('name')} ran in {cwd}, outside the run's workspace"
                )
    configured_cwd = context.section("configuration").get("cwd")
    if isinstance(configured_cwd, str) and workspace_declared:
        observed += 1
        if not _inside(configured_cwd, audit_evidence.EVIDENCE_ROOT_WORKSPACE):
            contradictions.append(
                f"the run configuration names {configured_cwd} as its working "
                "directory, outside the run's workspace"
            )
    evidence = context.evidence(*references, context.section("state").get("reference"))
    if contradictions:
        return _Verdict(
            "fail",
            f"a task packet names paths its own run contradicts: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if not observed:
        return _Verdict(
            "unavailable",
            "no turn recorded the artifact paths its packet was written from",
            evidence,
        )
    return _Verdict(
        "pass",
        f"all {observed} recorded packet paths agree with the artifacts they name",
        evidence,
    )


def _write_scope_session(*candidates: Any) -> str | None:
    """Name the session a write-scope record belongs to, first source wins.

    A guard record carries ``session_id`` as a string while a pause carries the
    numeric alias under ``session``, so both are normalized to text before they
    are matched against each other.
    """
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return None


def _write_scope_violated_aliases(guard: Mapping[str, Any]) -> list[str]:
    """The repositories one guard record says were written out of scope."""
    return [
        str(alias)
        for alias in (
            audit_evidence._mapping(item).get("alias")
            or audit_evidence._mapping(item).get("repository")
            for item in audit_evidence._sequence(guard.get("violations"))
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
                    for path in audit_evidence._sequence(
                        violation.get("qualified_paths")
                    )
                    if path
                )
            ),
        )
        for violation in (
            audit_evidence._mapping(item)
            for item in audit_evidence._sequence(guard.get("violations"))
        )
    ]
    return tuple(sorted(entries))


def _commit_records(context: _CheckContext) -> list[Mapping[str, Any]]:
    """Every commit the bundle records, however that commit was learned.

    Covers the runner's own commit projections and the attempts read from
    ``commit-*`` directories alike, including the entries the audit derived
    from observed head transitions, because a commit the runner never ran
    still put the write it names into history.
    """
    commits = context.section("commits")
    records = [
        audit_evidence._mapping(item)
        for item in audit_evidence._sequence(commits.get("commits"))
    ]
    records.extend(
        audit_evidence._mapping(item)
        for item in audit_evidence._sequence(commits.get("commit_groups"))
    )
    records.extend(
        audit_evidence._mapping(audit_evidence._mapping(item).get("result"))
        for item in audit_evidence._sequence(commits.get("attempts"))
    )
    return [record for record in records if record]


def _commit_repository_alias(record: Mapping[str, Any]) -> str | None:
    """The repository one commit record names, or ``None`` when it names none.

    A record filed under a linked worktree is reduced to the repository the
    worktree belongs to, because a commit in a worktree lands in that
    repository's history and that is the repository a write-scope question asks
    about.
    """
    for key in ("repository_alias", "repo", "alias", "repository_key", "key"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value.split(audit_evidence._WORKTREE_KEY_SEPARATOR, 1)[0]
    return None


def _commit_attributions(
    context: _CheckContext,
    *,
    skip: Callable[[Mapping[str, Any]], bool] | None = None,
) -> list[tuple[str | None, str | None]]:
    """The session and repository every commit in the bundle names.

    Either half may be ``None`` when the record does not say, and both readings
    matter: an unnamed session cannot be matched to a violation, and an unnamed
    repository cannot be ruled out of one.

    A commit group is descended into rather than read whole. The group is a
    container -- its per-repository detail lives in the entries beneath it, and
    the group itself names no repository -- so reading it whole would report a
    commit of unknown repository, which is the one shape that can never be
    ruled out (ASI-9). A group that names no repository anywhere committed
    nothing and contributes nothing.

    ``skip`` withholds individual commit entries, wherever they sit; a group
    whose every entry is withheld contributes nothing.
    """
    pairs: list[tuple[str | None, str | None]] = []
    for record in _commit_records(context):
        session = _write_scope_session(record.get("session_id"))
        nested = [
            audit_evidence._mapping(item)
            for item in audit_evidence._sequence(record.get("repositories"))
        ]
        if nested:
            pairs.extend(
                (
                    _write_scope_session(entry.get("session_id"), session),
                    _commit_repository_alias(entry),
                )
                for entry in nested
                if skip is None or not skip(entry)
            )
            continue
        if skip is not None and skip(record):
            continue
        aliases = [
            alias
            for alias in audit_evidence._sequence(record.get("ordered_aliases"))
            if isinstance(alias, str) and alias
        ]
        if aliases:
            pairs.extend((session, alias) for alias in aliases)
        elif "repositories" not in record and "ordered_aliases" not in record:
            pairs.append((session, _commit_repository_alias(record)))
    return pairs


def _guard_head_moves(guard: Mapping[str, Any]) -> set[tuple[str | None, Any]]:
    """The ``(repository, head after)`` pairs one guard record saw move."""
    moves: set[tuple[str | None, Any]] = set()
    for alias, change in audit_evidence._mapping(guard.get("changed")).items():
        change = audit_evidence._mapping(change)
        transition = audit_evidence._mapping(change.get("head_transition"))
        # Endpoints come from one source, as in _reconcile_observed_commits.
        source = transition if transition else change
        head_after = source.get("after" if transition else "head_after")
        before = source.get("before" if transition else "head_before")
        if isinstance(head_after, str) and head_after and head_after != before:
            moves.add(
                (
                    str(alias).split(audit_evidence._WORKTREE_KEY_SEPARATOR, 1)[0],
                    head_after,
                )
            )
    return moves


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
        result = audit_evidence._mapping(turn.get("result"))
        guard = audit_evidence._mapping(result.get("workspace_write_guard"))
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
        if audit_evidence._mapping(guard.get("unavailable")):
            blind.append(f"{turn.get('name')} could not be inventoried")

    def _pause(reason: Any, details: Mapping[str, Any]) -> None:
        """Read one scope pause, from the event log or the persisted status."""
        nonlocal observed, paused_any
        guard = audit_evidence._mapping(details.get("workspace_write_guard"))
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
        record_payload = audit_evidence._mapping(record.get("payload"))
        reason = record_payload.get("reason")
        details = audit_evidence._mapping(record_payload.get("details"))
        guard = audit_evidence._mapping(details.get("workspace_write_guard"))
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
            elif audit_evidence._mapping(guard.get("unavailable")):
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
            audit_evidence._mapping(status.get("pause_details")),
        )

    def _self_observed(entry: Mapping[str, Any]) -> bool:
        """Whether a commit is the offending turn's own head move, not a carry.

        The audit derives an ``observed-write-guard`` commit from the head
        transition the write guard saw during a turn. When that turn is the one
        the guard caught, the derived commit is the violation itself as the
        guard observed it rather than a later commit that carried the write
        forward, so counting it made every caught head move read as an escape.
        """
        if entry.get("origin") != audit_evidence.OBSERVED_COMMIT_ORIGIN:
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
    for fingerprint, aliases in unnamed.items():
        candidates = paused_fingerprints.get(fingerprint, set())
        attributed = next(iter(candidates)) if len(candidates) == 1 else None
        offenders.setdefault(attributed, set()).update(aliases)
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
    for session, aliases in sorted(offenders.items(), key=lambda item: item[0] or ""):
        who = f"session {session}" if session is not None else "an unnamed session"
        claim = f"{who} wrote outside its declared scope ({_names(aliases)})"
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
                and (committed_alias is None or committed_alias in aliases)
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


def _check_state_event_agreement(context: _CheckContext) -> _Verdict:
    status = context.section("status")
    evidence = context.evidence(
        context.section("state").get("reference"),
        context.event_summary.get("reference"),
    )
    terminal = [
        record
        for record in context.records
        if record.get("name") in audit_evidence._TERMINAL_EVENT_NAMES
    ]
    if "ok" not in status and "paused" not in status and not terminal:
        return _Verdict(
            "unavailable",
            "the run recorded neither a terminal outcome in its state nor a "
            "terminal event record",
            evidence,
        )
    paused_records = [
        record for record in terminal if record.get("name") == events.RUN_PAUSED_EVENT
    ]
    finished_records = [
        record for record in terminal if record.get("name") == events.RUN_FINISHED_EVENT
    ]

    contradictions: list[str] = []
    blind: list[str] = []

    def absent(claim: str) -> None:
        (blind if context.events_shed else contradictions).append(claim)

    if not context.terminal_pending:
        if status.get("paused") is True and not paused_records:
            absent("state records the run as paused but no run_paused record survives")
        if status.get("ok") is True and not finished_records:
            absent("state records the run as ok but no run_finished record survives")
    if paused_records and status.get("paused") is False:
        contradictions.append(
            "the event log records a pause the state projection denies"
        )
    if paused_records and status.get("ok") is True:
        contradictions.append(
            "the event log records a pause the state projection reports as success"
        )
    if (
        any(
            audit_evidence._mapping(record.get("payload")).get("ok") is False
            for record in finished_records
        )
        and status.get("ok") is True
    ):
        contradictions.append(
            "the run_finished record reports failure while the state projection "
            "reports success"
        )
    reason = status.get("pause_reason")
    for record in paused_records:
        recorded = audit_evidence._mapping(record.get("payload")).get("reason")
        if reason and recorded and recorded != reason:
            contradictions.append(
                f"state names {reason} as the pause reason while the run_paused "
                f"record names {recorded}"
            )

    if contradictions:
        return _Verdict(
            "fail",
            f"the persisted state and the event log tell different stories: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if blind:
        return _Verdict(
            "unavailable",
            f"the event log was shed under the size cap, so {_joined(blind)}",
            evidence,
        )
    return _Verdict(
        "pass",
        "the persisted status agrees with the run's terminal event records",
        evidence,
    )


#: Records that carry a commit group's full description, any one of which
#: identifies the group when the size cap shed the others.
_COMMIT_GROUP_LIFECYCLE_EVENTS = frozenset(
    {
        events.COMMIT_GROUP_STARTED_EVENT,
        events.COMMIT_GROUP_UPDATED_EVENT,
        events.COMMIT_GROUP_FINISHED_EVENT,
    }
)


def _session_paused_group_id(group: Any) -> str | None:
    """The id of a ``session-paused`` commit group, or ``None`` for any other."""
    group = audit_evidence._mapping(group)
    scope = group.get("scope") or audit_evidence._mapping(group.get("identity")).get(
        "scope"
    )
    if scope != commit_groups.GROUP_SCOPE_SESSION_PAUSED:
        return None
    group_id = group.get("group_id")
    return group_id if isinstance(group_id, str) and group_id else None


def _paused_work_settlement_group_ids(
    tail: Sequence[Mapping[str, Any]],
) -> frozenset[str]:
    """Commit-group ids the paused-work settlement opened after the terminal record.

    Settling the work a recoverable pause retained is the one thing a run does
    after it has already declared itself over. ``run_loop`` calls the
    settlement at the point every loop break converges on, deliberately
    downstream of the ``run_paused`` record it settles for -- the revert, the
    plan re-read and the pause must all have happened before it can know what
    it is committing. Its commit records are therefore post-terminal by
    construction, and reading them as a run that kept working would fail every
    run that settles anything.

    Only a block that closed earns the exemption. The settlement writes its
    ``paused_work_committed`` record last, so a group with no such record after
    it is a settlement that died mid-commit. That is work after the terminal
    record in exactly the sense this check means, and is left to fail.
    """
    opened: dict[str, int] = {}
    closed_at: list[int] = []
    for index, record in enumerate(tail):
        name = str(record.get("name") or "")
        payload = audit_evidence._mapping(record.get("payload"))
        if name == events.PAUSED_WORK_COMMITTED_EVENT:
            closed_at.append(index)
            # The close names its own group, so a settlement stays
            # recognisable when the size cap shed the record that opened it.
            group_id = _session_paused_group_id(payload.get("commit_group"))
        elif name in _COMMIT_GROUP_LIFECYCLE_EVENTS:
            group_id = _session_paused_group_id(payload.get("group"))
        else:
            continue
        if group_id is not None:
            opened.setdefault(group_id, index)
    if not opened or not closed_at:
        return frozenset()
    last_closed = closed_at[-1]
    return frozenset(
        group_id for group_id, index in opened.items() if index <= last_closed
    )


def _settles_paused_work(
    record: Mapping[str, Any], settlement_group_ids: frozenset[str]
) -> bool:
    """Whether one post-terminal record is part of a closed settlement block."""
    if not settlement_group_ids:
        return False
    if str(record.get("name") or "") not in _PAUSED_WORK_SETTLEMENT_COMMIT_EVENTS:
        return False
    return (
        audit_evidence._mapping(record.get("payload")).get("group_id")
        in settlement_group_ids
    )


def _check_terminal_artifacts(context: _CheckContext) -> _Verdict:
    problems: list[str] = []
    blind: list[str] = []
    references: list[str] = []
    for turn in context.turns:
        result = audit_evidence._mapping(turn.get("result"))
        if isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
        if result.get("available") is False:
            problems.append(
                f"{turn.get('name')} wrote no readable {state.RESULT_FILE_NAME} "
                f"({result.get('reason')})"
            )
    evidence = context.evidence(context.event_summary.get("reference"), *references)
    names = [str(record.get("name") or "") for record in context.records]
    if not names:
        if problems:
            return _Verdict("fail", _joined(problems), evidence)
        return _Verdict(
            "unavailable",
            "the run's slice of the event log holds no records to order",
            evidence,
        )

    if audit_evidence._RUN_STARTED_EVENT not in names:
        (blind if context.events_shed else problems).append(
            "the run recorded no run_started event"
        )
    elif names[0] != audit_evidence._RUN_STARTED_EVENT:
        problems.append(
            f"the run's first record is {names[0]} rather than "
            f"{audit_evidence._RUN_STARTED_EVENT}"
        )
    terminal_at = [
        index
        for index, name in enumerate(names)
        if name in audit_evidence._TERMINAL_EVENT_NAMES
    ]
    if not terminal_at:
        if not context.terminal_pending:
            (blind if context.events_shed else problems).append(
                "the run recorded no terminal event"
            )
    elif len(terminal_at) > 1:
        problems.append(f"the run recorded {len(terminal_at)} terminal events")
    settled = False
    if terminal_at:
        tail = context.records[terminal_at[-1] + 1 :]
        settlement_group_ids = _paused_work_settlement_group_ids(tail)
        settled = bool(settlement_group_ids)
        trailing = [
            str(record.get("name") or "")
            for record in tail
            if str(record.get("name") or "") in _POST_TERMINAL_WORK_EVENTS
            and not _settles_paused_work(record, settlement_group_ids)
        ]
        if trailing:
            problems.append(
                f"{len(trailing)} record(s) of work follow the run's terminal "
                f"record: {_names(trailing)}"
            )

    if problems:
        return _Verdict(
            "fail",
            f"the run's terminal artifacts are missing or out of order: "
            f"{_joined(problems)}",
            evidence,
        )
    if blind:
        return _Verdict(
            "unavailable",
            f"the event log was shed under the size cap, so {_joined(blind)} "
            "cannot be distinguished from one the cap removed",
            evidence,
        )
    closing = (
        "has not yet written the terminal record it is being audited ahead of"
        if not terminal_at
        else "closes on exactly one terminal record"
    )
    epilogue = (
        ", followed only by the settlement of the work its pause retained"
        if settled
        else ""
    )
    return _Verdict(
        "pass",
        f"the run opens on {audit_evidence._RUN_STARTED_EVENT}, "
        f"{closing}{epilogue}, and every "
        f"turn wrote a readable {state.RESULT_FILE_NAME}",
        evidence,
    )


def _is_expected_conditional_skip(entry: Any) -> bool:
    """Whether a skip is the recipe working as designed rather than a gap.

    A diagnostic that names the gates it explains and was omitted because none
    of them failed proves the recipe reached a clean result, so counting it
    against the run's success claim would make every healthy recipe look
    contradicted. Every other skip is a check that did not run.
    """

    if not isinstance(entry, Mapping):
        return False
    return bool(
        entry.get("reason") == verification.SKIP_REASON_CONDITION_NOT_MET
        and audit_evidence._sequence(entry.get("on_failure_of"))
    )


def _verification_covered_the_turn(session_verification: Mapping[str, Any]) -> bool:
    """Whether a recipe actually gated the changes the turn made.

    An expected skip is only neutral while something else still checked the
    work. A recipe that declared no gates, or that executed nothing, left the
    turn's mutation uncovered no matter how well-formed its skips were.
    """

    gate_count = audit_evidence._mapping(session_verification.get("recipe")).get(
        "gate_count"
    )
    if isinstance(gate_count, int) and gate_count <= 0:
        return False
    return bool(audit_evidence._sequence(session_verification.get("checks")))


def _superseded_turn_names(turns: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Turns a later clean completion of the same session replaced.

    The loop retries a session in place, so a turn that ended badly is
    counter-evidence only while it is still the last word on that session. Once
    a later turn completed the same session cleanly, counting the earlier
    attempt against the run's success claim would report every recovered run as
    a false success. A turn whose index or session cannot be read is never
    treated as superseded, so an unreadable projection keeps the finding.
    """
    stamped = {
        turn["name"]
        for turn in turns
        if isinstance(turn.get("name"), str)
        and isinstance(turn.get(audit_evidence.TURN_SUPERSEDED_BY_KEY), str)
    }
    return frozenset(audit_evidence._supersessions(turns)) | stamped


#: A deferred broad-check failure is closed by a later all-clear review
#: (``resolved``), still waiting on a repair loop the run never finished
#: (``pending``), or accepted without either (``unresolved``).
_DeferralResolution = Literal["resolved", "pending", "unresolved"]


def _recorded_review_cycles(context: _CheckContext) -> tuple[Mapping[str, Any], ...]:
    """Return ordered cycle evidence, preferring the durable state projection."""
    projected = audit_evidence._sequence(context.section("review").get("review_cycles"))
    if projected:
        return tuple(audit_evidence._mapping(item) for item in projected)
    return tuple(
        audit_evidence._mapping(record.get("payload"))
        for record in context.records
        if record.get("name") == events.REVIEW_CYCLE_EVENT
    )


def _cycle_is_all_clear(cycle: Mapping[str, Any]) -> bool:
    """Whether one recorded review cycle closed the loop with nothing left."""
    return review_continuation.cycle_is_all_clear(cycle)


def _run_is_in_flight(context: _CheckContext) -> bool:
    """Whether the run stopped short of an outcome rather than reaching one."""
    status = context.section("status")
    if status.get("paused") is True:
        return True
    names = {record.get("name") for record in context.records}
    if events.RUN_PAUSED_EVENT in names:
        return True
    return events.RUN_FINISHED_EVENT not in names and "ok" not in status


def _deferred_review_resolution(
    context: _CheckContext, session_id: Any
) -> _DeferralResolution:
    """How a deferred findings review ended: cleared, still open, or unearned.

    Only a later all-clear review closes a deferral. But a run that paused
    before its repair loop converged never got the chance to record one, and
    grading that as a false deferral claim reads an unfinished run as a
    dishonest one. Such a run is ``pending``: evidence the audit has not seen
    yet rather than evidence that contradicts the run.
    """
    cycles = _recorded_review_cycles(context)
    if review_continuation.deferred_review_is_cleared(
        cycles, review_session_id=session_id
    ):
        return "resolved"
    # Deliberately not gated on a repairs-materialized source: a run can pause
    # while its only cycle is malformed, and that is still an unfinished repair
    # loop rather than an unearned acceptance.
    if _run_is_in_flight(context):
        return "pending"
    return "unresolved"


def _accepted_verification_is_accounted_for(
    context: _CheckContext,
    verification_payload: Mapping[str, Any],
    *,
    session_id: Any,
) -> _DeferralResolution:
    """Validate deferral identity, raw failures, and its later all-clear.

    Identity and name validation stay a hard ``unresolved``: a malformed
    acceptance is a defect whether or not the run ever finished.
    """
    acceptance = audit_evidence._deferred_review_acceptance(verification_payload)
    if acceptance is None:
        return "unresolved"
    deferred = verification_payload.get("deferred_failures")
    if deferred is not None:
        failures = [
            audit_evidence._mapping(item) for item in audit_evidence._sequence(deferred)
        ]
        names = [failure.get("name") for failure in failures]
        if names != list(
            audit_evidence._sequence(acceptance.get("deferred_failure_names"))
        ):
            return "unresolved"
        if any(
            not isinstance(name, str)
            or not name.startswith(audit_evidence._BROAD_REVIEW_CHECK_PREFIX)
            for name in names
        ):
            return "unresolved"
    return _deferred_review_resolution(context, session_id)


def _review_cycles_ended_all_clear(context: _CheckContext) -> bool:
    """Whether the last review cycle the run recorded closed every finding."""
    cycles = _recorded_review_cycles(context)
    return bool(cycles) and _cycle_is_all_clear(cycles[-1])


def _check_misleading_success(context: _CheckContext) -> _Verdict:
    status = context.section("status")
    finished = [
        record
        for record in context.records
        if record.get("name") == events.RUN_FINISHED_EVENT
    ]
    claimed = status.get("ok") is True or any(
        audit_evidence._mapping(record.get("payload")).get("ok") is True
        for record in finished
    )

    counter: list[str] = []
    references: list[str] = []
    superseded = _superseded_turn_names(context.turns)
    # A successful operator stop claims that the stop was honored. It does not
    # claim the interrupted work completed (ASI-23). Require both abort records
    # and scope this exception to the exact turn they identify.
    stopped_turns = (
        _aborted_turns(context) if status.get("reason") == "stopped" else set()
    )
    for turn in context.turns:
        result = audit_evidence._mapping(turn.get("result"))
        guard = audit_evidence._mapping(result.get("workspace_write_guard"))
        # A retry replaces the attempt, never the breach: a turn that wrote
        # outside its declared scope stays counter-evidence however cleanly the
        # session finished afterwards.
        stopped = (
            (result.get("session"), result.get("turn_index")) in stopped_turns
            and result.get("status_result") in {"nonzero-exit", "runner-pause"}
            and turn.get("rejected") is not True
            and not result.get("plan_error")
            and not audit_evidence._guard_shows_failure(guard)
            and audit_evidence._mapping(result.get("verification")).get("passed")
            is not False
        )
        anomalous = (
            turn.get("anomalous") is True
            and turn.get("name") not in superseded
            and not stopped
        )
        contradicts = anomalous or bool(
            audit_evidence._sequence(guard.get("violations"))
        )
        if anomalous:
            counter.append(f"{turn.get('name')} recorded an anomalous result")
        if audit_evidence._sequence(guard.get("violations")):
            counter.append(f"{turn.get('name')} exceeded its declared write scope")
        if contradicts and isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
    for session in audit_evidence._sequence(
        context.section("verification").get("sessions")
    ):
        session_verification = audit_evidence._mapping(
            audit_evidence._mapping(session).get("verification")
        )
        # The whole entry, not just its failure: a coverage gap in a superseded
        # turn's recipe reappears on the turn that replaced it.
        if audit_evidence._mapping(session).get("turn") in superseded:
            continue
        if (
            session_verification.get("passed") is False
            # A deferral whose repair loop the run never finished is unfinished
            # evidence, not a contradiction of the success the run claims.
            and _accepted_verification_is_accounted_for(
                context,
                session_verification,
                session_id=audit_evidence._mapping(session).get("session_id")
                or session_verification.get("session"),
            )
            == "unresolved"
        ):
            counter.append(
                "verification failed for "
                f"{audit_evidence._mapping(session).get('turn')}"
            )
        skipped = audit_evidence._sequence(session_verification.get("skipped_checks"))
        unexplained = [
            entry for entry in skipped if not _is_expected_conditional_skip(entry)
        ]
        if unexplained:
            counter.append(
                f"{len(unexplained)} verification check(s) were skipped for "
                f"{audit_evidence._mapping(session).get('turn')} "
                "without a condition that "
                "explains the skip"
            )
        elif skipped and not _verification_covered_the_turn(session_verification):
            counter.append(
                "every verification check for "
                f"{audit_evidence._mapping(session).get('turn')} was "
                "skipped, so nothing gated the changes it made"
            )
    skipped_records = [
        audit_evidence._mapping(record.get("payload"))
        for record in context.records
        if record.get("name") == events.VERIFICATION_CHECK_SKIPPED_EVENT
    ]
    unexplained_records = [
        payload
        for payload in skipped_records
        if not _is_expected_conditional_skip(payload)
    ]
    if unexplained_records:
        counter.append(
            f"{len(unexplained_records)} verification check(s) recorded themselves "
            "as skipped without a condition that explains the skip"
        )
    elif skipped_records and not context.events_shed:
        # Absence of a ``verification_check_finished`` record is only evidence
        # in a log the cap kept whole. Shedding is not neutral here: a skipped
        # record outranks a finished one, so a shed bundle keeps exactly the
        # half of the pair that reads as a coverage gap and drops the half that
        # would refute it (ASI-23).
        executed_turns = {
            audit_evidence._mapping(record.get("payload")).get("turn_index")
            for record in context.records
            if record.get("name") == events.VERIFICATION_CHECK_FINISHED_EVENT
        }
        uncovered = sorted(
            {
                str(payload.get("turn_index"))
                for payload in skipped_records
                if payload.get("turn_index") not in executed_turns
            }
        )
        if uncovered:
            counter.append(
                f"every verification check recorded for turn(s) "
                f"{_joined(uncovered)} was skipped, so nothing gated the changes "
                "they made"
            )
    if status.get("paused") is True:
        counter.append("the state projection records the run as paused")
    if any(record.get("name") == events.RUN_PAUSED_EVENT for record in context.records):
        counter.append("the event log records the run as paused")
    verdict = audit_evidence._mapping(context.section("review").get("review_verdict"))
    # The verdict is rewritten per cycle, so this is what the last review
    # *found*, not what it left open. Only the cycle log says whether a later
    # all-clear closed them.
    found = verdict.get("total_findings")
    if (
        isinstance(found, int)
        and found > 0
        and not _review_cycles_ended_all_clear(context)
    ):
        counter.append(
            f"the recorded review verdict reports {found} finding(s) and no later "
            "all-clear review closed them"
        )

    evidence = context.evidence(context.section("state").get("reference"), *references)
    if claimed and counter:
        return _Verdict(
            "fail",
            f"the run reports success its own evidence contradicts: {_joined(counter)}",
            evidence,
        )
    if claimed:
        return _Verdict(
            "pass",
            "nothing the run recorded contradicts the success it claims",
            evidence,
        )
    if "ok" not in status and not finished:
        return _Verdict(
            "unavailable",
            "the run recorded no outcome to weigh against its own evidence",
            evidence,
        )
    return _Verdict(
        "pass", "the run claimed no success, so no success is unearned", evidence
    )


def _check_retry_resume(context: _CheckContext) -> _Verdict:
    status = context.section("status")
    evidence = context.evidence(
        context.section("state").get("reference"),
        context.event_summary.get("reference"),
    )
    reason = status.get("pause_reason")
    paused = status.get("paused") is True or any(
        record.get("name") == events.RUN_PAUSED_EVENT for record in context.records
    )
    recorded = audit_evidence._mapping(status.get("pause_policy"))
    if paused and reason and not recorded:
        return _Verdict(
            "unavailable",
            f"the run paused for {reason} but recorded no retry classification to "
            "check against the pause policy",
            evidence,
        )

    contradictions: list[str] = []
    # A block written under an earlier policy version classified the same keys
    # by different rules, so diffing it against today's classifier reports a
    # schema change as a contradiction.
    if recorded and recorded.get("policy_version") == pause_policy.POLICY_VERSION:
        expected = pause_policy.classify_pause(
            reason if isinstance(reason, str) else None
        )
        for key in ("reason", "recoverable", "requires_user", "max_retries"):
            if recorded.get(key) != expected[key]:
                contradictions.append(
                    f"the recorded retry policy gives {key}={recorded.get(key)!r} "
                    f"where the pause classifier gives {expected[key]!r}"
                )
    retries: dict[str, list[dict[str, Any]]] = {}
    for record in context.records:
        if record.get("name") != events.REVIEW_VERDICT_RETRY_EVENT:
            continue
        payload = audit_evidence._mapping(record.get("payload"))
        retries.setdefault(str(payload.get("session")), []).append(payload)
    for session, attempts in sorted(retries.items()):
        budgets = [
            payload["max_attempts"]
            for payload in attempts
            if isinstance(payload.get("max_attempts"), int)
        ]
        if not budgets:
            contradictions.append(
                f"session {session} recorded {len(attempts)} retry attempt(s) "
                "without naming the budget they were spent against"
            )
            continue
        budget = min(budgets)
        if len(attempts) > budget:
            contradictions.append(
                f"session {session} was relaunched {len(attempts)} times against a "
                f"budget of {budget}"
            )
        beyond = [
            payload["attempt"]
            for payload in attempts
            if isinstance(payload.get("attempt"), int) and payload["attempt"] > budget
        ]
        if beyond:
            contradictions.append(
                f"session {session} recorded retry attempt {max(beyond)} beyond its "
                f"budget of {budget}"
            )

    if contradictions:
        return _Verdict(
            "fail",
            f"the run's retry and resume records are inconsistent: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if not recorded and not retries:
        if not paused:
            return _Verdict(
                "pass",
                "the run neither paused nor retried, so no retry budget was spent",
                evidence,
            )
        return _Verdict(
            "unavailable",
            "the run paused without recording either a reason or a retry "
            "classification",
            evidence,
        )
    return _Verdict(
        "pass",
        f"the recorded retry classification matches the pause policy and "
        f"{len(retries)} session(s) stayed inside their retry budget",
        evidence,
    )


def _harness_failure_classification(context: _CheckContext) -> dict[str, Any]:
    """The classification the run attached to the harness failure it paused on.

    Empty when the run did not pause on a harness failure, which is every pause
    this invariant has nothing to say about.
    """
    status = context.section("status")
    if status.get("paused") is not True:
        return {}
    details = audit_evidence._mapping(status.get("pause_details"))
    return audit_evidence._mapping(details.get("failure_classification"))


def _classified_failure_sessions(context: _CheckContext) -> set[Any]:
    """Sessions whose turn failure the run named a reason for when it paused.

    The pause carries the classification for the turn it died on, and that
    classification is what the harness-failure check grades. Counting the same
    turn again as evidence nothing looked at reports a named failure as an
    unknown one, whether or not anything will relaunch it.
    """
    if not _harness_failure_classification(context).get("reason"):
        return set()
    session = audit_evidence._mapping(
        context.section("status").get("pause_details")
    ).get("session")
    return set() if session is None else {session}


def _restore_refusal_records(context: _CheckContext) -> list[dict[str, Any]]:
    return [
        payload
        for record in context.records
        if record.get("name") == events.CONVERSATION_CONTINUATION_EVENT
        and (payload := audit_evidence._mapping(record.get("payload"))).get(
            "disposition"
        )
        == "restore-refused"
    ]


def _check_conversation_restore_refusal(context: _CheckContext) -> _Verdict:
    """Grade the refusal that bypasses the ordinary harness-failure classifier."""
    status = context.section("status")
    details = audit_evidence._mapping(status.get("pause_details"))
    refusal = audit_evidence._mapping(details.get("conversation_restore_refused"))
    records = _restore_refusal_records(context)
    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    if not records:
        return _Verdict(
            "unavailable"
            if refusal and context.events_shed
            else "fail"
            if refusal
            else "pass",
            "the restore refusal has no conversation event to account for it"
            if refusal
            else "the run recorded no refused conversation restore",
            evidence,
        )
    problems: list[str] = []
    missing_exit = False
    if (
        status.get("paused") is not True
        or status.get("pause_reason")
        != events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
        or status.get("ok") is True
    ):
        problems.append("the refused restore did not leave the run paused for recovery")
    for record in records:
        session = record.get("session_id", record.get("session"))
        turn_index = record.get("turn_index")
        if (
            not isinstance(session, (int, str))
            or isinstance(session, bool)
            or not str(session)
            or not isinstance(turn_index, int)
            or isinstance(turn_index, bool)
            or turn_index < 1
        ):
            problems.append("a restore refusal names no session or turn")
        if (
            record.get("retired") is not True
            or record.get("error")
            or not isinstance(record.get("conversation"), str)
            or not record.get("conversation")
            or not isinstance(record.get("detail"), str)
            or not record.get("detail")
        ):
            problems.append("a refused conversation was not explained and retired")
        if str(details.get("session_id", details.get("session"))) != str(
            session
        ) or any(
            refusal.get(key) != record.get(key)
            for key in ("conversation", "disposition", "retired", "detail", "error")
        ):
            problems.append("the recovery pause does not match the recorded refusal")
        exits = [
            payload
            for event in context.records
            if event.get("name") == events.TURN_EXECUTION_FINISHED_EVENT
            and str(
                (payload := audit_evidence._mapping(event.get("payload"))).get(
                    "session_id", payload.get("session")
                )
            )
            == str(session)
            and payload.get("turn_index") == turn_index
        ]
        if not exits:
            missing_exit = True
            if not context.events_shed:
                problems.append("the refused restore has no matching turn exit")
        for exit_record in exits:
            code = exit_record.get("returncode")
            if (
                not isinstance(code, int)
                or isinstance(code, bool)
                or code == 0
                or code != details.get("returncode")
                or exit_record.get("timed_out")
                or exit_record.get("interrupted")
                or exit_record.get("aborted")
                or exit_record.get("plan_readable") is False
            ):
                problems.append("the refused turn's exit has an unexplained outcome")
    if missing_exit and not problems:
        return _Verdict(
            "unavailable",
            "the refused turn's exit was shed from the evidence",
            evidence,
        )
    return _Verdict(
        "fail" if problems else "pass",
        _joined(problems)
        if problems
        else (
            f"{len(records)} refused restore(s) retired the unavailable conversation "
            "and paused for explicit recovery"
        ),
        evidence,
    )


def _abort_turn_records(context: _CheckContext, action: str) -> list[dict[str, Any]]:
    """The run-control payloads carrying one turn-abort action, in order."""
    payloads: list[dict[str, Any]] = []
    for record in context.records:
        if record.get("name") != events.RUN_CONTROL_EVENT:
            continue
        payload = audit_evidence._mapping(record.get("payload"))
        if payload.get("action") == action:
            payloads.append(payload)
    return payloads


def _aborted_turns(context: _CheckContext) -> set[tuple[Any, Any]]:
    """The turns the run killed because an operator asked it to.

    Keyed by session and turn index together, so the exemption covers the one
    turn the abort named rather than every turn that session ever ran.
    """
    requested = {
        (payload.get("session"), payload.get("turn_index"))
        for payload in _abort_turn_records(
            context, events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED
        )
        if payload.get("session") is not None and payload.get("turn_index") is not None
    }
    return {
        (payload.get("session"), payload.get("turn_index"))
        for payload in _abort_turn_records(
            context, events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED
        )
        if payload.get("abort_reason")
        and (payload.get("session"), payload.get("turn_index")) in requested
    }


def _check_operator_abort(context: _CheckContext) -> _Verdict:
    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    honored = _abort_turn_records(context, events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED)
    aborted_exits = [
        audit_evidence._mapping(record.get("payload"))
        for record in context.records
        if record.get("name") == events.TURN_EXECUTION_FINISHED_EVENT
        and audit_evidence._mapping(record.get("payload")).get("aborted") is True
    ]
    if not honored and not aborted_exits:
        return _Verdict(
            "pass",
            "the run killed no turn on an operator abort",
            evidence,
        )
    requested = {
        (payload.get("session"), payload.get("turn_index"))
        for payload in _abort_turn_records(
            context, events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED
        )
    }
    contradictions: list[str] = []
    acknowledged = {
        (
            str(payload.get("session_id", payload.get("session"))),
            payload.get("turn_index"),
        )
        for payload in honored
    }
    missing = [
        payload
        for payload in aborted_exits
        if (
            str(payload.get("session_id", payload.get("session"))),
            payload.get("turn_index"),
        )
        not in acknowledged
    ]
    for payload in missing if not context.events_shed else ():
        contradictions.append(
            f"session {payload.get('session_id', payload.get('session'))!r} "
            f"turn {payload.get('turn_index')!r} records an aborted exit "
            "without an abort acknowledgment"
        )
    for payload in honored:
        turn = (payload.get("session"), payload.get("turn_index"))
        if turn not in requested:
            contradictions.append(
                f"session {turn[0]!r} turn {turn[1]!r} was killed on an abort no "
                "recorded request asked for"
            )
        elif not payload.get("abort_reason"):
            contradictions.append(
                f"session {turn[0]!r} turn {turn[1]!r} was killed on an abort "
                "that names no reason"
            )
    if contradictions:
        return _Verdict(
            "fail",
            f"the run killed a turn without an accountable abort behind it: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if missing and context.events_shed:
        return _Verdict(
            "unavailable",
            "aborted turn exits lack acknowledgments in an event log shed "
            "under the size cap",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{len(honored)} turn(s) were killed on an operator abort the run "
        "recorded a request and a reason for",
        evidence,
    )


def _check_retryable_transport_failure(context: _CheckContext) -> _Verdict:
    status = context.section("status")
    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    classification = _harness_failure_classification(context)
    if not classification:
        return _Verdict(
            "pass",
            "the run did not pause on a harness failure, so no retry was owed "
            "to any classification",
            evidence,
        )

    reason = classification.get("reason")
    retryable = classification.get("retryable")
    policy = audit_evidence._mapping(status.get("pause_policy"))
    if not policy:
        return _Verdict(
            "unavailable",
            f"the run classified its harness failure as {reason!r} but recorded "
            "no pause policy to judge that classification against",
            evidence,
        )

    contradictions: list[str] = []
    expected_retryable = reason in events.RETRYABLE_HARNESS_FAILURE_REASONS
    if isinstance(retryable, bool) and retryable is not expected_retryable:
        contradictions.append(
            f"the failure is classified retryable={retryable!r} while its reason "
            f"{reason!r} is {'' if expected_retryable else 'not '}one the "
            "classifier marks retryable"
        )
    if retryable is True:
        # A retryable failure is one the supervisor may relaunch unattended.
        # Either half of that going missing strands the run: a policy that
        # refuses the relaunch, or one that sends it to a human who has no
        # answer to give.
        if policy.get("recoverable") is not True:
            contradictions.append(
                "the failure is classified retryable while its pause policy "
                f"gives recoverable={policy.get('recoverable')!r}, so nothing "
                "will relaunch it"
            )
        if policy.get("requires_user") is not False:
            contradictions.append(
                "the failure is classified retryable while its pause policy "
                f"gives requires_user={policy.get('requires_user')!r}, so it "
                "waits on a person no reply can help"
            )
        budget = policy.get("max_retries")
        if not isinstance(budget, int) or budget <= 0:
            contradictions.append(
                f"the failure is classified retryable while its pause policy "
                f"grants max_retries={budget!r}"
            )

    if contradictions:
        return _Verdict(
            "fail",
            f"the run's harness-failure classification contradicts the policy it "
            f"paused under: {_joined(contradictions)}",
            evidence,
        )
    if retryable is True:
        fingerprint = classification.get("fingerprint") or reason
        return _Verdict(
            "pass",
            f"the turn failed with the retryable {fingerprint} "
            f"classification and the run paused under a policy granting "
            f"{policy.get('max_retries')} supervisor relaunch(es), which is where "
            "the retry is owed rather than inside this run",
            evidence,
        )
    return _Verdict(
        "pass",
        f"the turn failed with the non-retryable {reason!r} classification and "
        "the run paused rather than relaunching it",
        evidence,
    )


def _check_review_independence(context: _CheckContext) -> _Verdict:
    review_section = context.section("review")
    run_state = context.section("state")
    evidence = context.evidence(run_state.get("reference"))
    if not review_section and not audit_evidence._sequence(
        context.section("verification").get("sessions")
    ):
        return _Verdict(
            "unavailable",
            "the run recorded neither a review verdict nor a verification result "
            "to judge independence by",
            evidence,
        )

    verdict = review_section.get("review_verdict")
    harness = run_state.get("harness")
    review_harness = review_section.get("review_harness")
    contradictions: list[str] = []
    if verdict and harness and review_harness == harness:
        contradictions.append(f"the {harness} harness reviewed its own work")
    if verdict == "approved" and review_section.get("review_completed") is False:
        contradictions.append("a verdict was approved without the review completing")
    if contradictions:
        return _Verdict(
            "fail",
            f"the run's review did not stand apart from the work it graded: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if not verdict:
        return _Verdict(
            "unavailable",
            "the run recorded no review verdict, so review independence cannot be "
            "judged",
            evidence,
        )
    if not review_harness:
        return _Verdict(
            "unavailable",
            "the run recorded a review verdict without naming the harness that "
            "produced it",
            evidence,
        )
    if not harness:
        return _Verdict(
            "unavailable",
            "the run recorded a review verdict without naming the harness that "
            "did the work the review graded",
            evidence,
        )
    return _Verdict(
        "pass",
        f"the {review_harness} review harness is independent of the {harness} "
        "implementation harness",
        evidence,
    )


def _verification_turn_label(key: tuple[Any, Any]) -> str:
    turn_index = key[1]
    return "an unidentified turn" if turn_index is None else f"turn {turn_index}"


def _check_verification_outcomes(context: _CheckContext) -> _Verdict:
    """Reconcile every failing check against the outcome its recipe reported.

    A verification only earns ``passed`` when every failure it saw was a
    conditional diagnostic, which by construction does not gate. So a failing
    check is accounted for in exactly two ways: the recipe reported the failure,
    or the recipe absorbed it as a diagnostic. Anything else is a verification
    that watched a check fail and said nothing. Both the turn result and the
    event records are read, because either can survive without the other.

    Event records carry the role each executed check played, so a failure is
    reconciled against what that check actually was. Records collected before
    the role was emitted fall back to the count of diagnostics that ran, which
    bounds how many failures a recipe could have absorbed.
    """

    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    executed: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    outcomes: dict[tuple[Any, Any], dict[str, Any]] = {}
    for record in context.records:
        name = record.get("name")
        if name not in (
            events.VERIFICATION_CHECK_FINISHED_EVENT,
            events.VERIFICATION_FINISHED_EVENT,
        ):
            continue
        payload = audit_evidence._mapping(record.get("payload"))
        key = (payload.get("run_id"), payload.get("turn_index"))
        if name == events.VERIFICATION_FINISHED_EVENT:
            outcomes[key] = payload
        else:
            executed.setdefault(key, []).append(payload)

    sessions = [
        audit_evidence._mapping(session)
        for session in audit_evidence._sequence(
            context.section("verification").get("sessions")
        )
        if audit_evidence._mapping(audit_evidence._mapping(session).get("verification"))
    ]
    if not executed and not outcomes and not sessions:
        return _Verdict(
            "unavailable",
            "the run recorded no verification outcomes to reconcile",
            evidence,
        )

    problems: list[str] = []
    blind: list[str] = []
    failing_total = 0
    for session in sessions:
        outcome = audit_evidence._mapping(session.get("verification"))
        if outcome.get("passed") is False:
            acceptance = outcome.get("acceptance")
            if acceptance is not None:
                deferred = [
                    audit_evidence._mapping(entry)
                    for entry in audit_evidence._sequence(
                        outcome.get("deferred_failures")
                    )
                ]
                failing_total += len(deferred)
                resolution = _accepted_verification_is_accounted_for(
                    context,
                    outcome,
                    session_id=session.get("session_id") or outcome.get("session"),
                )
                if resolution == "unresolved":
                    problems.append(
                        f"the failed verification recorded for {session.get('turn')} "
                        "claims broad-check deferral without a matching repair cycle "
                        "and later all-clear review"
                    )
                elif resolution == "pending":
                    blind.append(
                        f"the broad-check failures deferred by "
                        f"{session.get('turn')} reached no all-clear review "
                        "before the run stopped"
                    )
            continue
        if outcome.get("passed") is not True:
            continue
        failed = [
            entry
            for entry in audit_evidence._sequence(outcome.get("checks"))
            if audit_evidence._payload_shows_failure(audit_evidence._mapping(entry))
        ]
        failing_total += len(failed)
        absorbed = len(audit_evidence._sequence(outcome.get("diagnostic_failures")))
        if len(failed) > absorbed:
            problems.append(
                f"the verification recorded for {session.get('turn')} passed while "
                f"{len(failed)} of its check(s) failed and only {absorbed} of those "
                "was a diagnostic"
            )
    for key in sorted(
        set(executed) | set(outcomes), key=lambda item: (str(item[0]), str(item[1]))
    ):
        label = _verification_turn_label(key)
        failures = [
            payload
            for payload in executed.get(key, ())
            if audit_evidence._payload_shows_failure(payload)
        ]
        failing_total += len(failures)
        outcome = outcomes.get(key)
        if outcome is None:
            if failures:
                target = (
                    blind
                    if context.events_shed or context.terminal_pending
                    else problems
                )
                target.append(
                    f"{len(failures)} failing check record(s) for {label} never "
                    f"reached a {events.VERIFICATION_FINISHED_EVENT} record"
                )
            continue
        if outcome.get("passed") is False:
            acceptance = outcome.get("acceptance")
            if acceptance is not None:
                failing_total += len(failures)
                accepted_names = list(
                    audit_evidence._sequence(
                        audit_evidence._mapping(acceptance).get(
                            "deferred_failure_names"
                        )
                    )
                )
                failure_names = [failure.get("name") for failure in failures]
                resolution = _accepted_verification_is_accounted_for(
                    context,
                    outcome,
                    session_id=outcome.get("session_id") or outcome.get("session"),
                )
                # A deferral naming checks that never failed is a defect no
                # amount of unfinished repair loop excuses.
                if accepted_names != failure_names:
                    resolution = "unresolved"
                if resolution == "unresolved":
                    problems.append(
                        f"verification for {label} claims broad-check deferral "
                        "without matching failed checks, a repair cycle, and a "
                        "later all-clear review"
                    )
                elif resolution == "pending":
                    blind.append(
                        f"the broad-check failures deferred by {label} reached no "
                        "all-clear review before the run stopped"
                    )
            continue
        if outcome.get("passed") is not True:
            continue
        diagnostics = outcome.get("diagnostic_count")
        diagnostics = diagnostics if isinstance(diagnostics, int) else 0
        skipped = outcome.get("skipped_count")
        skipped = skipped if isinstance(skipped, int) else 0
        # Only a conditional diagnostic is ever skipped, and only a diagnostic
        # that actually ran can absorb a failure — the declared count on its own
        # lets a skipped diagnostic stand in for a gate that failed.
        absorbable = max(diagnostics - skipped, 0)
        roles = [payload.get("role") for payload in failures]
        gate_failures = sum(1 for role in roles if role == verification.CHECK_ROLE_GATE)
        diagnostic_failures = sum(
            1 for role in roles if role == verification.CHECK_ROLE_DIAGNOSTIC
        )
        unidentified = len(roles) - gate_failures - diagnostic_failures
        if gate_failures:
            problems.append(
                f"verification for {label} passed while {gate_failures} of its "
                "check record(s) reported a failure against a gate"
            )
        elif unidentified > max(absorbable - diagnostic_failures, 0):
            problems.append(
                f"verification for {label} passed while {unidentified} check "
                f"record(s) of unidentified role reported a failure against "
                f"{absorbable} executed diagnostic(s)"
            )
        executed_count = outcome.get("executed_count")
        passed_count = outcome.get("passed_count")
        if isinstance(executed_count, int) and isinstance(passed_count, int):
            unpassed = executed_count - passed_count
            if unpassed > absorbable:
                problems.append(
                    f"verification for {label} passed while its own counts leave "
                    f"{unpassed} of {executed_count} check(s) unpassed against "
                    f"{absorbable} executed diagnostic(s)"
                )

    if problems:
        return _Verdict(
            "fail",
            f"the run's verification outcomes do not account for the checks that "
            f"failed under them: {_joined(problems)}",
            evidence,
        )
    if blind:
        return _Verdict(
            "unavailable",
            f"the run's verification records are incomplete, so {_joined(blind)} "
            "cannot be distinguished from evidence the audit never saw",
            evidence,
        )
    return _Verdict(
        "pass",
        f"every one of the {failing_total} failing verification check(s) is "
        "accounted for by the outcome of the recipe that ran it",
        evidence,
    )


def _check_mutation_commit_guards(context: _CheckContext) -> _Verdict:
    commits = context.section("commits")
    registry = context.section("registry")
    scopes = context.section("scopes")
    contradictions: list[str] = []
    references: list[str] = []

    activity = [
        f"{audit_evidence.EVIDENCE_ROOT_RUN}/{name}"
        for name in (MUTATION_ARTIFACT_NAME, WORKSPACE_CHANGE_ARTIFACT_NAME)
        if f"{audit_evidence.EVIDENCE_ROOT_RUN}/{name}" in context.declared
    ]
    references.extend(activity)

    writable = {
        str(alias)
        for session in audit_evidence._sequence(scopes.get("sessions"))
        for alias in audit_evidence._sequence(
            audit_evidence._mapping(session).get("writes")
        )
    }
    attempts = [
        audit_evidence._mapping(item)
        for item in audit_evidence._sequence(commits.get("attempts"))
    ]
    # A commit the guard observed is by definition one the runner never ran, so
    # the commit mode it would have run under is absent for the same reason the
    # commit is: nobody asked the runner to make it.
    ran = [
        attempt
        for attempt in attempts
        if attempt.get("origin") != audit_evidence.OBSERVED_COMMIT_ORIGIN
    ]
    repair_commit_mode = commits.get("repair_commit_mode")
    if ran and not commits.get("commit_mode") and repair_commit_mode != "per-child":
        contradictions.append(
            f"the run made {len(ran)} commit attempt(s) without recording the "
            "commit mode they ran under"
        )
    for attempt in attempts:
        if isinstance(attempt.get("reference"), str):
            references.append(attempt["reference"])
        result = audit_evidence._mapping(attempt.get("result"))
        if result.get("available") is False:
            contradictions.append(
                f"{attempt.get('name')} wrote no readable commit result"
            )
            continue
        alias = result.get("repository_alias")
        if scopes.get("available") and alias and str(alias) not in writable:
            contradictions.append(
                f"{attempt.get('name')} committed to {alias}, which no session "
                "declares as writable"
            )

    guarded = 0
    head_moves = 0
    history_rewrites = 0
    unguarded: list[str] = []
    for turn in context.turns:
        result = audit_evidence._mapping(turn.get("result"))
        if result.get("available") is False or turn.get("anomalous") is True:
            continue
        # Registered workspaces guard review turns as well. Legacy runs have a
        # separate read-only review guard, but every implementation turn must
        # carry repository evidence even when commit mode is disabled.
        requires_guard = registry.get("registered") or (
            result.get("session_kind") == "implementation"
        )
        if not requires_guard:
            continue
        if isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
        guard = audit_evidence._mapping(result.get("workspace_write_guard"))
        if not guard:
            unguarded.append(str(turn.get("name")))
            continue
        guarded += 1
        for raw_changes in audit_evidence._mapping(guard.get("changed")).values():
            changes = audit_evidence._mapping(raw_changes)
            if changes.get("head_changed") is not True:
                continue
            head_moves += 1
            transition = audit_evidence._mapping(changes.get("head_transition"))
            if not transition:
                contradictions.append(
                    f"{turn.get('name')} changed HEAD without recording its "
                    "history transition"
                )
                continue
            relation = transition.get("relation")
            if relation not in {
                "created",
                "deleted",
                "fast-forward",
                "rewind",
                "rewrite",
            }:
                contradictions.append(
                    f"{turn.get('name')} recorded an invalid HEAD transition"
                )
                continue
            if relation in {"deleted", "rewind", "rewrite"}:
                history_rewrites += 1
    if unguarded:
        contradictions.append(
            f"{_names(unguarded)} completed implementation work without a "
            "write-guard result"
        )

    evidence = context.evidence(*references, context.section("state").get("reference"))
    if contradictions:
        return _Verdict(
            "fail",
            f"the run's mutation and commit guards left a gap: "
            f"{_joined(contradictions)}",
            evidence,
        )
    if activity:
        return _Verdict(
            "unavailable",
            "the audit recorded workspace activity, but snapshot comparisons "
            "identify no writer; this does not establish an auditor breach",
            evidence,
        )
    if not attempts and not guarded:
        return _Verdict(
            "unavailable",
            "the run recorded neither a commit attempt nor a write-guard result to "
            "judge its mutation guards by",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{guarded} guarded turn(s), {len(attempts)} commit attempt(s), "
        f"{head_moves} worker HEAD move(s), and {history_rewrites} history "
        "rewrite(s) stayed inside the run's declared write scope",
        evidence,
    )


def _worker_commits_added(guard: Mapping[str, Any]) -> list[str] | None:
    """Name what a worker turn put on HEAD, or ``None`` if that is unreadable.

    ``None`` is not "nothing". A guard that saw the tip move but recorded no
    count for it knows a commit happened and cannot say how many, and reading
    that as none would turn the one case worth reporting into a clean answer.

    The relation travels with the count because the two say different things.
    A ``fast-forward`` counts commits the worker wrote; a ``rewrite`` counts
    every commit the new tip reaches that the old one did not, which is the
    whole rewritten range rather than the turn's own work. Both outlive a
    refused turn, and a sentence that reported them as the same number would
    overstate one of them.
    """
    added: list[str] = []
    for raw_changes in audit_evidence._mapping(guard.get("changed")).values():
        changes = audit_evidence._mapping(raw_changes)
        if changes.get("head_changed") is not True:
            continue
        transition = audit_evidence._mapping(changes.get("head_transition"))
        count = transition.get("commits_added")
        relation = transition.get("relation")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return None
        if not isinstance(relation, str) or not relation:
            return None
        if count:
            added.append(f"{count} commit(s) by {relation}")
    return added


def _check_commit_after_verification(context: _CheckContext) -> _Verdict:
    """Grade whether a turn the run refused left commits of its own behind.

    ASL commits a session's work after verification accepts it, which is what
    makes the history a record of work that passed. A worker that runs ``git
    commit`` inside its own turn moves that commit ahead of the gate, and
    nothing walks it back: the write guard's before/after comparison is taken
    before the runner would commit anything, so a tip that moved there moved
    under the worker. When the turn is then refused -- the harness exited
    nonzero, the guard reverted it, or authoritative verification failed --
    the session goes back to ``pending`` while the commit stays on the branch,
    and the next attempt rebuilds the same work on top of it (ASI-142).

    The run's own mutation check counts those moves and reports them as having
    stayed inside the declared write scope, which is true and is not this
    question. Scope asks *where* the worker wrote; this asks whether what it
    wrote outlived the turn that was rejected. Both belong to the same lane, so
    the rollup keeps the worse of the two.

    Never ``unavailable``. A turn that recorded no write guard at all recorded
    no HEAD telemetry either, and demanding it here would be a second opinion
    about a gap ``_check_mutation_commit_guards`` already owns -- its
    recommendation is precisely to capture fingerprints around every
    implementation turn. What blindness costs is stated in the clear verdict
    instead, so the report never answers for a place it did not look.
    """
    references: list[str] = []
    retained: list[str] = []
    blind: list[str] = []
    for turn in context.turns:
        result = audit_evidence._mapping(turn.get("result"))
        if result.get("available") is False:
            continue
        name = str(turn.get("name"))
        if isinstance(turn.get("directory"), str):
            references.append(turn["directory"])
        guard = audit_evidence._mapping(result.get("workspace_write_guard"))
        if not guard:
            blind.append(name)
            continue
        # `anomalous` is the loop's own refusal, correlated from the event
        # stream by `_mark_rejected_turns`; `_turn_shows_failure` is the turn
        # result's. A verification that failed after a clean exit is only in
        # the second, and it is the shape ASI-142 was reported for.
        if not (
            turn.get("anomalous") is True or audit_evidence._turn_shows_failure(result)
        ):
            continue
        added = _worker_commits_added(guard)
        if added is None:
            blind.append(name)
            continue
        if added:
            retained.append(f"{name} left {_joined(added)} on HEAD")

    evidence = context.evidence(*references, context.section("state").get("reference"))
    if retained:
        return _Verdict(
            "fail",
            "the run refused a turn and kept the commits it had already made: "
            f"{_joined(retained)}",
            evidence,
        )
    if blind:
        return _Verdict(
            "pass",
            "no turn the run refused kept a commit this check could read off a "
            f"write-guard record, but {_joined(blind)} recorded no readable "
            "HEAD telemetry, so whether a refused turn committed there is "
            "mutation-and-commit's to report",
            evidence,
        )
    return _Verdict(
        "pass",
        "no turn the run refused left a commit of its own on HEAD",
        evidence,
    )


def _commit_failure_classification(payload: Mapping[str, Any]) -> str | None:
    """The token the run gave a failed commit, explicit key before outcome."""
    for key in ("failure_classification", "status_result"):
        value = payload.get(key)
        if isinstance(value, str) and value and value != "success":
            return value
    return None


def _commit_failure_reason(payload: Mapping[str, Any]) -> str | None:
    """Where the run said why a commit failed.

    The bundle carries no command output by design, so a reason is either named
    in the record itself or written to a stream the record points at and sizes.
    A failure with neither left nothing for an auditor to read.
    """
    for key in ("failure_reason", "exec_error", "status_error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for path_key, size_key in (
        ("stderr_path", "stderr_chars"),
        ("stdout_path", "stdout_chars"),
    ):
        path = payload.get(path_key)
        size = payload.get(size_key)
        if isinstance(path, str) and path and isinstance(size, int) and size > 0:
            return path
    return None


def _check_commit_failure_accounting(context: _CheckContext) -> _Verdict:
    """Grade the run's own account of every commit it recorded as failed.

    A failed commit is recorded once, in ``commit_finished``, and that record
    already carries the outcome that classified the failure and the streams the
    failure was written to. Reading them here is what lets the unknown-anomalies
    catch-all stop reporting a failed commit: this check grades every record that
    catch-all would have selected and the zero-exit failures it would have
    missed, so a record that names no classification, contradicts its own exit
    code, or points at no reason still surfaces by name.
    """
    problems: list[str] = []
    references: list[str] = []
    recorded = 0
    failures = 0
    for record in context.records:
        if record.get("name") != events.COMMIT_FINISHED_EVENT:
            continue
        recorded += 1
        payload = audit_evidence._mapping(record.get("payload"))
        if not audit_evidence._commit_shows_failure(payload):
            continue
        failures += 1
        label = audit_evidence._commit_record_label(payload)
        references.append(str(payload.get("artifact_dir")))
        status_result = payload.get("status_result")
        returncode = payload.get("returncode")
        if _commit_failure_classification(payload) is None:
            problems.append(f"{label} failed without classifying the failure")
        elif isinstance(status_result, str) and status_result not in _COMMIT_RESULTS:
            problems.append(
                f"{label} recorded {status_result}, which is no commit outcome ASL "
                "writes"
            )
        elif (
            isinstance(status_result, str)
            and isinstance(returncode, int)
            and (returncode != 0) != (status_result == "nonzero-exit")
        ):
            problems.append(
                f"{label} recorded {status_result} against exit {returncode}"
            )
        if payload.get("success") is True:
            problems.append(f"{label} claimed success while recording a failure")
        if _commit_failure_reason(payload) is None:
            problems.append(f"{label} failed without recording why")

    evidence = context.evidence(
        *references,
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    if problems:
        return _Verdict(
            "fail",
            f"the run left a commit failure unaccounted for: {_joined(problems)}",
            evidence,
        )
    if context.events_shed:
        return _Verdict(
            "unavailable",
            "the event log was shed under the size cap, so the run's commit "
            "results cannot be read in full",
            evidence,
        )
    if not context.records:
        return _Verdict(
            "unavailable",
            "the run recorded no events, so it recorded no commit outcome to grade",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{failures} of {recorded} recorded commit(s) failed, each naming its "
        "classification and where its reason was written",
        evidence,
    )


def _unresolved_commit_group_rows(context: _CheckContext) -> list[dict[str, Any]]:
    """The commit groups the run ended still owing, by its own account.

    ``unresolved_commit_groups`` is the field the runner writes as it closes,
    so its presence is the run stating the answer directly. A bundle without it
    predates that field, and is read from the durable group rows instead —
    restricted to groups the coordinator marked ``failed``, because a
    ``pending`` row in an older bundle is as likely to be a group still in
    flight as one that was lost.
    """
    section = context.section("commits")
    if "unresolved_commit_groups" in section:
        return [
            audit_evidence._mapping(row)
            for row in audit_evidence._sequence(section["unresolved_commit_groups"])
        ]
    return [
        audit_evidence._mapping(row)
        for row in audit_evidence._sequence(section.get("commit_groups"))
        if audit_evidence._mapping(row).get("status")
        == commit_groups.GROUP_STATUS_FAILED
    ]


def _unresolved_group_label(row: Mapping[str, Any]) -> str:
    session_id = row.get("session_id")
    scope = "the end-of-run commit" if session_id is None else f"session {session_id}"
    alias = row.get("failed_alias")
    return f"{scope} in {alias}" if alias else scope


def _check_commit_group_resolution(context: _CheckContext) -> _Verdict:
    """Grade whether the run committed everything it set out to commit.

    A failed commit does not pause the run where it fails: the group is marked
    failed, retried at the next commit boundary, and the run carries on. That
    is only safe while a group still owed when the run ends is impossible to
    miss, so this check is where the loss is named. Without it the one failure
    mode soft-fail introduces — a run that finishes reporting success while a
    session's changes were never committed — would be graded by nothing.

    A run that spent the whole retry budget now pauses at its next selection
    boundary rather than working past the loss, and it still reaches here
    owing the group, which is deliberate: the barrier decides that nothing
    further is built on top of the missing commit, not that the commit was
    made. The changes are still uncommitted and an operator still has to
    settle them or relaunch into the recovery path, so a paused run earns the
    same failing verdict a finished one does (ASI-138).
    """
    unresolved = _unresolved_commit_group_rows(context)
    references = [
        str(attempt.get("reference"))
        for attempt in audit_evidence._sequence(
            context.section("commits").get("attempts")
        )
        if isinstance(attempt, Mapping)
    ]
    evidence = context.evidence(
        context.section("state").get("reference"),
        *references,
    )
    if unresolved:
        return _Verdict(
            "fail",
            "the run ended owing commits it never made: "
            f"{_joined([_unresolved_group_label(row) for row in unresolved])}",
            evidence,
        )
    if not context.section("state"):
        return _Verdict(
            "unavailable",
            "the run's state projection could not be read, so nothing says "
            "whether the commits it owed were made",
            evidence,
        )
    return _Verdict(
        "pass",
        "every commit group the run opened reached completion",
        evidence,
    )


def _paused_work_settlement_label(payload: Mapping[str, Any]) -> str:
    session_id = payload.get("session_id")
    return (
        "the paused-work settlement"
        if session_id is None
        else f"session {session_id}'s paused-work settlement"
    )


def _paused_work_settlement_reason(payload: Mapping[str, Any]) -> str | None:
    """Where a failed settlement wrote why it failed, if it wrote it anywhere.

    The two places are the pair ``run_reporting`` already reads when it prints
    the failure to an operator: the exception the settlement caught, and the
    outcome the underlying commit classified itself with.
    """
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error
    status_result = audit_evidence._mapping(payload.get("commit")).get("status_result")
    if isinstance(status_result, str) and status_result.strip():
        return status_result
    return None


def _check_paused_work_settlement(context: _CheckContext) -> _Verdict:
    """Grade the one commit a run makes that no accepted turn produced.

    A recoverable pause leaves the failed turn's changes in the worktree, and
    the settlement is the run's single chance to commit them before the process
    exits. Nothing downstream re-derives what it did: the next run meets either
    a clean tree or unexplained dirt at its startup commit gate, and only this
    record says which was intended.

    So the record is graded on its own terms rather than trusted. It must name
    an outcome the settlement actually writes; a failure must say where its
    reason was written and must leave the group among the ones the run ended
    owing, which ``run_loop`` guarantees by ordering the settlement ahead of the
    terminal record; and a success must agree with itself about residue, since
    ``committed`` and ``committed-with-residue`` differ by nothing else. The
    record must also be mirrored into state, because a record in the log with no
    mirror is a run that died between the append and the write, and every reader
    but this one reads the mirror.

    Grading it here is what earns ``paused_work_committed`` its place in
    :data:`_CLASSIFIED_EVENT_NAMES`. The catch-all reported every settlement as
    an unclassified failure, because the record carries the pause's reason; the
    exemption is only sound while this check owns the record instead.
    """
    problems: list[str] = []
    settlements = [
        audit_evidence._mapping(record.get("payload"))
        for record in context.records
        if record.get("name") == events.PAUSED_WORK_COMMITTED_EVENT
    ]
    commits = context.section("commits")
    mirror = audit_evidence._mapping(commits.get(audit_evidence.PAUSED_WORK_COMMIT_KEY))
    unresolved_scopes = {
        row.get("scope") for row in _unresolved_commit_group_rows(context)
    }
    for payload in settlements:
        label = _paused_work_settlement_label(payload)
        result = payload.get("result")
        if not isinstance(result, str) or result not in _PAUSED_WORK_RESULTS:
            problems.append(
                f"{label} recorded {result!r}, which is no settlement outcome ASL "
                "writes"
            )
        elif result == _PAUSED_WORK_RESULT_FAILED:
            if _paused_work_settlement_reason(payload) is None:
                problems.append(f"{label} failed without recording why")
            if commit_groups.GROUP_SCOPE_SESSION_PAUSED not in unresolved_scopes:
                problems.append(
                    f"{label} failed, but the run ended owing no paused-work "
                    "commit group for a later run to recover"
                )
        else:
            residue = audit_evidence._sequence(
                audit_evidence._mapping(payload.get("residue")).get("repositories")
            )
            if result == _PAUSED_WORK_RESULT_RESIDUE and not residue:
                problems.append(
                    f"{label} reported residue while naming no repository it was "
                    "left in"
                )
            elif result == _PAUSED_WORK_RESULT_COMMITTED and residue:
                problems.append(
                    f"{label} reported a clean commit while naming "
                    f"{len(residue)} repository(ies) it left dirty"
                )
        if not mirror:
            problems.append(f"{label} was never mirrored into the run's state")
        elif mirror.get("result") != result:
            problems.append(
                f"{label} recorded {result!r} while state mirrors "
                f"{mirror.get('result')!r}"
            )

    evidence = context.evidence(
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    if problems:
        return _Verdict(
            "fail",
            f"the run's settlement of its paused work does not account for "
            f"itself: {_joined(problems)}",
            evidence,
        )
    if not settlements:
        if context.events_shed:
            return _Verdict(
                "unavailable",
                "the event log was shed under the size cap, so a settlement the "
                "run made cannot be distinguished from one it never made",
                evidence,
            )
        return _Verdict(
            "pass",
            "the run left no paused work behind, so it settled none",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{len(settlements)} paused-work settlement(s) recorded an outcome ASL "
        "writes, agreed with the run's state, and accounted for what they left "
        "in the worktree",
        evidence,
    )


def _check_resource_anomalies(context: _CheckContext) -> _Verdict:
    anomalies: list[str] = []
    references: list[str] = []
    suspected = 0
    cleared = 0
    automatically_recovered = 0
    for record in context.records:
        name = record.get("name")
        payload = audit_evidence._mapping(record.get("payload"))
        if name == events.TURN_HANG_SUSPECTED_EVENT:
            suspected += 1
        elif name == events.TURN_HANG_CLEARED_EVENT:
            cleared += 1
            block = audit_evidence._mapping(payload.get("hang_suspected"))
            recovery = audit_evidence._mapping(block.get("recovery"))
            if audit_evidence._sequence(recovery.get("signaled_pids")):
                automatically_recovered += 1
        elif name == events.PROCESS_SWEEP_EVENT:
            if audit_evidence._sequence(payload.get("kill_failed")):
                anomalies.append(
                    "a post-turn process sweep failed to kill a process it owned"
                )
            if audit_evidence._sequence(payload.get("survivors_after")):
                anomalies.append("processes survived a post-turn sweep")
    if suspected > cleared:
        anomalies.append(f"{suspected - cleared} suspected hang(s) were never cleared")
    for turn in context.turns:
        result = audit_evidence._mapping(turn.get("result"))
        stalled = [
            label
            for key, label in (
                ("timed_out", "timed out"),
                ("interrupted", "was interrupted"),
            )
            if result.get(key) is True
        ]
        for label in stalled:
            anomalies.append(f"{turn.get('name')} {label}")
        if stalled and isinstance(turn.get("directory"), str):
            references.append(turn["directory"])

    evidence = context.evidence(
        *references,
        context.event_summary.get("reference"),
        context.section("state").get("reference"),
    )
    if anomalies:
        return _Verdict(
            "fail",
            f"the run stalled or leaked processes: {_joined(anomalies)}",
            evidence,
        )
    if context.events_shed:
        return _Verdict(
            "unavailable",
            "the event log was shed under the size cap, so the run's process "
            "sweeps cannot be read in full",
            evidence,
        )
    if not context.records and not context.turns:
        return _Verdict(
            "unavailable",
            "the run recorded neither events nor turns to judge stalls and timeouts by",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{len(context.turns)} turn(s) ran without a timeout, an interrupt, an "
        "uncleared hang, or a surviving process; "
        f"{automatically_recovered} post-result hang(s) recovered automatically",
        evidence,
    )


def _worker_pause_exit_verdict(
    context: _CheckContext, payload: Mapping[str, Any], result: Mapping[str, Any]
) -> _Verdict:
    """Account for one failed exit carrying a worker's own pause declaration.

    This grades its recorded disposition, not why the harness needed to be
    stopped. Resource checks still own shutdown, stalls, and process sweeps.
    """
    identity = (
        str(payload.get("session_id", payload.get("session"))),
        payload.get("turn_index"),
    )
    evidence = context.evidence(context.event_summary.get("reference"))
    problems = []
    if _write_scope_session(payload.get("session_id", payload.get("session"))) is None:
        problems.append("the failed exit names no session")
    if (
        not isinstance(identity[1], int)
        or isinstance(identity[1], bool)
        or identity[1] < 1
        or identity
        != (
            str(result.get("session_id", result.get("session"))),
            result.get("turn_index"),
        )
        or payload.get("returncode") != result.get("returncode")
    ):
        problems.append("the failed exit and its turn result disagree")
    if payload.get("timed_out") or payload.get("interrupted") or payload.get("aborted"):
        problems.append("the exit records a timeout, interruption, or abort")
    declaration = result.get("runner_pause")
    if not isinstance(declaration, str) or not declaration.strip():
        problems.append("the worker declared no pause reason")
    if result.get("runner_pause_source") not in {"last_message", "stdout"}:
        problems.append("the pause declaration came from no trusted stream")
    status = context.section("status")
    details = audit_evidence._mapping(status.get("pause_details"))
    session = str(details.get("session_id", details.get("session")))
    if (
        status.get("paused") is not True
        or status.get("ok") is True
        or session != identity[0]
    ):
        problems.append("the run is not holding that failed session under a pause")
    if result.get("status_result") == "complete":
        problems.append("the failed turn also claims accepted completion")

    guard = audit_evidence._mapping(result.get("workspace_write_guard"))
    guard_refused = (
        status.get("pause_reason")
        in {
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
        }
        and guard.get("reason") == status.get("pause_reason")
        and guard.get("passed") is False
        and guard.get("reverted_to_pending") is True
    )
    if not guard_refused:
        if (
            status.get("pause_message") != declaration
            or details.get("source") != result.get("runner_pause_source")
            or result.get("status_result")
            not in {status.get("pause_reason"), "runner-pause"}
        ):
            problems.append("the run never acknowledged that worker pause")
        if result.get("pending_completed_ids") or result.get("pending_completed"):
            problems.append("the worker pause still carries an unreconciled completion")

    if problems:
        return _Verdict("fail", _joined(problems), evidence)
    return _Verdict(
        "pass",
        "the failed exit matches a trusted worker pause and its held or "
        "rejected disposition",
        evidence,
    )


def _worker_pause_exits(
    context: _CheckContext,
) -> list[tuple[dict[str, Any], dict[str, Any], _Verdict]]:
    candidates = []
    for record in context.records:
        if record.get("name") != events.TURN_EXECUTION_FINISHED_EVENT:
            continue
        payload = audit_evidence._mapping(record.get("payload"))
        code = payload.get("returncode")
        if (
            not isinstance(code, int)
            or isinstance(code, bool)
            or code == 0
            or payload.get("aborted") is True
        ):
            continue
        identity = (
            str(payload.get("session_id", payload.get("session"))),
            payload.get("turn_index"),
        )
        for turn in context.turns:
            result = audit_evidence._mapping(turn.get("result"))
            if (
                identity
                == (
                    str(result.get("session_id", result.get("session"))),
                    result.get("turn_index"),
                )
                and result.get("runner_pause") is not None
            ):
                candidates.append(
                    (
                        payload,
                        result,
                        _worker_pause_exit_verdict(context, payload, result),
                    )
                )
    return candidates


def _check_worker_pause_exits(context: _CheckContext) -> _Verdict:
    candidates = _worker_pause_exits(context)
    problems = [
        verdict.detail for _, _, verdict in candidates if verdict.status != "pass"
    ]
    evidence = context.evidence(
        context.event_summary.get("reference"), *context.turn_directories()
    )
    if problems:
        return _Verdict(
            "fail",
            f"worker pause exits have contradictory dispositions: {_joined(problems)}",
            evidence,
        )
    return _Verdict(
        "pass",
        f"{len(candidates)} recorded failed exit(s) carrying worker pauses "
        "match their held or rejected dispositions; shutdown causes remain "
        "separately audited",
        evidence,
    )


def _check_unclassified_anomalies(context: _CheckContext) -> _Verdict:
    summary = context.event_summary
    evidence = context.evidence(
        summary.get("reference"), context.section("state").get("reference")
    )
    unread: list[str] = []
    unparsable = summary.get(
        audit_evidence.UNPARSABLE_LINE_COUNT_KEY,
        summary.get(audit_evidence.LEGACY_UNPARSABLE_LINE_COUNT_KEY),
    )
    if isinstance(unparsable, int) and unparsable > 0:
        unread.append(f"{unparsable} event log line(s) would not parse")
    for omission in context.omissions:
        if omission.get("reason") in (
            audit_evidence.REASON_UNREADABLE,
            audit_evidence.REASON_MALFORMED,
        ):
            unread.append(
                f"{omission.get('count')} {omission.get('section')} artifact(s) were "
                f"{omission.get('reason')}"
            )
    unclassified: dict[str, int] = {}
    classified = _classified_failure_sessions(context)
    aborted = _aborted_turns(context)
    worker_pauses = {
        (
            str(payload.get("session_id", payload.get("session"))),
            payload.get("turn_index"),
        )
        for payload, _, verdict in _worker_pause_exits(context)
        if verdict.status == "pass"
    }
    restored = (
        {
            (
                str(record.get("session_id", record.get("session"))),
                record.get("turn_index"),
            )
            for record in _restore_refusal_records(context)
        }
        if _check_conversation_restore_refusal(context).status == "pass"
        else set()
    )
    for record in context.records:
        name = str(record.get("name") or "")
        if name in _CLASSIFIED_EVENT_NAMES:
            continue
        payload = audit_evidence._mapping(record.get("payload"))
        # `_check_operator_abort` owns every explicitly aborted exit, including
        # missing acknowledgments. Such a defect belongs to that named check.
        if (
            name == events.TURN_EXECUTION_FINISHED_EVENT
            and payload.get("aborted") is True
        ):
            continue
        if (
            name == events.TURN_EXECUTION_FINISHED_EVENT
            and (
                str(payload.get("session_id", payload.get("session"))),
                payload.get("turn_index"),
            )
            in worker_pauses
        ):
            continue
        if (
            name == events.TURN_EXECUTION_FINISHED_EVENT
            and restored
            and isinstance(payload.get("turn_index"), int)
            and (
                str(payload.get("session_id", payload.get("session"))),
                payload.get("turn_index"),
            )
            in restored
        ):
            continue
        # Narrowed to the session the pause names, because a run reports the
        # outcome of every session it ran and only one of them is the failure
        # the pause classified.
        if name in _PAUSED_TURN_EVENT_NAMES and payload.get("session") in classified:
            continue
        if (
            name in _PAUSED_TURN_EVENT_NAMES
            and (
                payload.get("session"),
                payload.get("turn_index"),
            )
            in aborted
        ):
            continue
        if audit_evidence._payload_shows_failure(payload):
            unclassified[name] = unclassified.get(name, 0) + 1
    for name in sorted(unclassified):
        unread.append(
            f"{unclassified[name]} {name} record(s) report a failure no "
            "deterministic check classifies"
        )

    if unread:
        return _Verdict(
            "fail",
            f"the run holds evidence no deterministic check accounts for: "
            f"{_joined(unread)}",
            evidence,
        )
    if not context.records and not context.omissions:
        return _Verdict(
            "unavailable",
            "the run recorded no events and no omissions, so there is nothing to "
            "classify",
            evidence,
        )
    return _Verdict(
        "pass",
        "every recorded failure is accounted for by a named deterministic check",
        evidence,
    )


_CHECKS: tuple[tuple[CheckSpec, Callable[[_CheckContext], _Verdict]], ...] = (
    (
        CheckSpec(
            check="worker-pause-exits",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="worker-pause-disposition",
            invariant_key="lifecycle.worker-pause-exit-accounting",
            severity="medium",
            recommendation=(
                "Match the failed turn exit to its trusted worker pause and "
                "recorded held or rejected disposition before treating it "
                "as explained."
            ),
        ),
        _check_worker_pause_exits,
    ),
    (
        CheckSpec(
            check="root-registry-agreement",
            lane="identity-authority",
            category="identity",
            component="workspace-registry",
            invariant_key="identity.root-registry-agreement",
            severity="critical",
            recommendation=(
                "Re-bind the plan to the workspace it actually ran against, or "
                "re-register the repositories under the recorded root, before "
                "trusting any scope decision this run made."
            ),
        ),
        _check_root_registry_agreement,
    ),
    (
        CheckSpec(
            check="repository-intent",
            lane="identity-authority",
            category="identity",
            component="repository-intent",
            invariant_key="identity.repository-intent",
            severity="critical",
            recommendation=(
                "Declare a `## Repositories` table in manifest.md naming every "
                "repository the plan reaches, then re-run preflight."
            ),
            # The only check that replays the gate rather than reading its
            # recorded verdict: it walks the plan directory as it stands now.
            # A plan edited, published or deleted after the run answers for the
            # tree the replay is standing in, not for the run being judged.
            replayable=False,
        ),
        _check_repository_intent,
    ),
    (
        CheckSpec(
            check="task-packet-paths",
            lane="task-packet-coherence",
            category="packet",
            component="turn-artifacts",
            invariant_key="packet.artifact-path-agreement",
            severity="high",
            recommendation=(
                "Reconcile the paths written into the turn result with the "
                "directory the turn actually ran from."
            ),
        ),
        _check_task_packet_paths,
    ),
    (
        CheckSpec(
            check="scope-escape",
            lane="task-packet-coherence",
            category="packet",
            component="workspace-write-guard",
            invariant_key="packet.write-scope-containment",
            severity="critical",
            recommendation=(
                "Widen the session's declared writes or revert the out-of-scope "
                "change; do not resume until the guard passes on its own."
            ),
        ),
        _check_scope_escape,
    ),
    # Split from containment rather than expressed as a severity on the verdict:
    # severity lives on the spec, so one issue key cannot oscillate between
    # critical and medium across occurrences and corrupt the inbox's dedup
    # history for the escape that matters.
    (
        CheckSpec(
            check="scope-guard-catch",
            lane="task-packet-coherence",
            category="packet",
            component="workspace-write-guard-catch",
            invariant_key="packet.write-scope-guard-catch",
            severity="medium",
            recommendation=(
                "Widen the child's durable write scope to the aliases its own "
                "findings name, rerun expansion and preflight, then resume."
            ),
        ),
        _check_scope_guard_catch,
    ),
    (
        CheckSpec(
            check="retry-resume",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="pause-policy",
            invariant_key="lifecycle.retry-and-resume-consistency",
            severity="medium",
            recommendation=(
                "Persist the classification `pause_policy.classify_pause` returns "
                "for the recorded reason, and keep relaunches inside the budget "
                "the retry records name."
            ),
        ),
        _check_retry_resume,
    ),
    (
        CheckSpec(
            check="retryable-transport-failure",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="harness-failure-classifier",
            invariant_key="lifecycle.retryable-transport-failure",
            severity="high",
            recommendation=(
                "Keep the harness-failure classification and the pause policy "
                "telling the same story: a failure marked retryable must pause "
                "under a recoverable policy that needs no user and grants at "
                "least one relaunch, since the supervisor performs the retry "
                "rather than the run itself."
            ),
        ),
        _check_retryable_transport_failure,
    ),
    (
        CheckSpec(
            check="conversation-restore-refusal",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="conversation-recovery",
            invariant_key="lifecycle.conversation-restore-refusal",
            severity="high",
            recommendation=(
                "Retire the refused conversation and pause for explicit recovery; "
                "record the refusal and matching turn exit without claiming completion."
            ),
        ),
        _check_conversation_restore_refusal,
    ),
    (
        CheckSpec(
            check="operator-abort",
            lane="lifecycle-retry-resume",
            category="lifecycle",
            component="run-control",
            invariant_key="lifecycle.operator-abort",
            severity="high",
            recommendation=(
                "Record the request and the reason behind every turn the run "
                "kills, so a killed turn traces to the operator who asked for "
                "it rather than reading as an unexplained failure."
            ),
        ),
        _check_operator_abort,
    ),
    (
        CheckSpec(
            check="review-independence",
            lane="verification-review-independence",
            category="review",
            component="review-harness",
            invariant_key="review.independent-verification",
            severity="high",
            recommendation=(
                "Run the review under a harness other than the one that did the "
                "work, and record a verdict only once the review completes."
            ),
        ),
        _check_review_independence,
    ),
    (
        CheckSpec(
            check="verification-outcomes",
            lane="verification-review-independence",
            category="verification",
            component="verification-recipe",
            invariant_key="verification.failure-accounting",
            severity="high",
            recommendation=(
                "Reconcile the recipe's outcome with the checks that ran under "
                "it: a verification may only pass when every failure it recorded "
                "belongs to a conditional diagnostic."
            ),
        ),
        _check_verification_outcomes,
    ),
    (
        CheckSpec(
            check="mutation-commit-guards",
            lane="mutation-commit-guards",
            category="guards",
            component="mutation-and-commit",
            invariant_key="guards.mutation-and-commit",
            severity="critical",
            recommendation=(
                "Capture and record repository fingerprints around every "
                "implementation turn, including legacy and commit-mode-none runs; "
                "classify worker HEAD moves and commit only inside established "
                "repository write scope."
            ),
        ),
        _check_mutation_commit_guards,
    ),
    # The check above asks where a worker's writes landed; this one asks
    # whether the commits among them outlived the turn that was refused. The
    # fingerprint is deliberately the one the agent lane already reports this
    # under, so a run graded by both lanes still folds into one issue.
    (
        CheckSpec(
            check="commit-after-verification",
            lane="mutation-commit-guards",
            category="guards",
            component="commit-promotion",
            invariant_key="guards.commit-after-verification",
            severity="high",
            recommendation=(
                "Decide what the commits a refused turn left are: re-running "
                "the session builds the same work again on top of them, so "
                "either remove them before the next attempt or accept that the "
                "branch carries work no gate passed. Keeping the commit out of "
                "the worker's turn is what stops the choice from recurring."
            ),
        ),
        _check_commit_after_verification,
    ),
    (
        CheckSpec(
            check="commit-failure-accounting",
            lane="mutation-commit-guards",
            category="guards",
            component="commit-failure",
            invariant_key="guards.commit-failure-accounting",
            severity="medium",
            recommendation=(
                "Record the outcome that classified the failed commit and the "
                "stream its reason was written to, so a failed commit is read "
                "from its own record instead of landing in the catch-all."
            ),
        ),
        _check_commit_failure_accounting,
    ),
    # Split from failure accounting rather than folded into it: that check asks
    # whether a failed commit explained itself, this one asks whether the
    # changes ever landed. A soft-failed commit can pass the first and fail
    # this one, and the inbox must be able to tell those two defects apart.
    (
        CheckSpec(
            check="commit-group-resolution",
            lane="mutation-commit-guards",
            category="guards",
            component="commit-group-resolution",
            invariant_key="guards.commit-group-resolution",
            severity="high",
            recommendation=(
                "Commit the changes the named group still owes, or rerun the "
                "plan so the recovery path completes that group, before "
                "treating the run's changes as landed."
            ),
        ),
        _check_commit_group_resolution,
    ),
    # The settlement is graded apart from both because it is the one commit no
    # accepted turn produced: it commits work a failed turn retained, under a
    # pause, after the run's terminal record. Whether it explained itself and
    # whether it landed are the same question here, and neither of the checks
    # above reads the record that answers it.
    (
        CheckSpec(
            check="paused-work-settlement",
            lane="mutation-commit-guards",
            category="guards",
            component="paused-work-settlement",
            invariant_key="guards.paused-work-settlement",
            severity="high",
            recommendation=(
                "Read the run's paused_work_commit record before relaunching: "
                "commit or discard what the pause left in the worktree, so the "
                "next run's startup commit gate does not meet it as unexplained "
                "dirt."
            ),
        ),
        _check_paused_work_settlement,
    ),
    (
        CheckSpec(
            check="state-event-agreement",
            lane="truthfulness-observability",
            category="observability",
            component="state-and-events",
            invariant_key="observability.state-event-agreement",
            severity="high",
            recommendation=(
                "Write the terminal event and the state projection under one lock "
                "so an operator reading either sees the same outcome."
            ),
        ),
        _check_state_event_agreement,
    ),
    (
        CheckSpec(
            check="terminal-artifacts",
            lane="truthfulness-observability",
            category="observability",
            component="terminal-artifacts",
            invariant_key="observability.terminal-artifacts",
            severity="medium",
            recommendation=(
                "Ensure every turn writes its result file and that the run opens "
                "on run_started and closes on exactly one terminal record."
            ),
        ),
        _check_terminal_artifacts,
    ),
    (
        CheckSpec(
            check="misleading-success",
            lane="truthfulness-observability",
            category="observability",
            component="success-claim",
            invariant_key="observability.success-is-earned",
            severity="high",
            recommendation=(
                "Withhold the success claim until the anomalies, failed checks, "
                "and skipped checks it contradicts are resolved or explained."
            ),
        ),
        _check_misleading_success,
    ),
    (
        CheckSpec(
            check="resource-anomalies",
            lane="resources-stalls",
            category="resources",
            component="process-lifecycle",
            invariant_key="resources.stall-and-timeout",
            severity="medium",
            recommendation=(
                "Trace the stalled turn or surviving process to the descendant "
                "that outlived its turn, and close the handle that kept it alive."
            ),
        ),
        _check_resource_anomalies,
    ),
    (
        CheckSpec(
            check="unclassified-anomalies",
            lane="unknown-anomalies",
            category="anomalies",
            component="unclassified-evidence",
            invariant_key="anomalies.unclassified-evidence",
            severity="low",
            recommendation=(
                "Read the unclassified records directly, then either repair the "
                "damaged evidence or add a deterministic check that owns them."
            ),
        ),
        _check_unclassified_anomalies,
    ),
)

#: Every deterministic check, in the order results are reported.
DETERMINISTIC_CHECK_SPECS: tuple[CheckSpec, ...] = tuple(spec for spec, _ in _CHECKS)


def run_deterministic_checks(
    bundle: audit_evidence.AuditInput | Mapping[str, Any],
) -> tuple[assurance_models.DeterministicResult, ...]:
    """Judge one evidence bundle against every reproducible check.

    Each check answers ``pass``, ``fail``, or ``unavailable`` under a stable
    invariant key and names the evidence it read. Evidence a check needed but
    could not see yields ``unavailable``: an audit that cannot look is never
    the same thing as an audit that looked and found nothing.
    """
    payload = (
        bundle.payload if isinstance(bundle, audit_evidence.AuditInput) else bundle
    )
    context = _CheckContext(payload)
    return tuple(
        assurance_models.DeterministicResult(
            check=spec.check,
            lane=spec.lane,
            status=verdict.status,
            invariant_key=spec.invariant_key,
            evidence=verdict.evidence,
            detail=_detail(verdict.detail),
        )
        for spec, verdict in ((spec, run(context)) for spec, run in _CHECKS)
    )


def findings_from_results(
    results: Iterable[assurance_models.DeterministicResult],
) -> tuple[Finding, ...]:
    """Raise each failing check into a finding under the check's fingerprint."""
    specs = {spec.check: spec for spec in DETERMINISTIC_CHECK_SPECS}
    findings = []
    for result in results:
        spec = specs.get(result.check)
        if result.status != "fail" or spec is None:
            continue
        findings.append(
            Finding(
                category=spec.category,
                component=spec.component,
                invariant_key=spec.invariant_key,
                lane=spec.lane,
                severity=spec.severity,
                summary=result.detail or f"the {spec.check} check failed",
                recommendation=spec.recommendation,
                evidence=result.evidence,
            )
        )
    return tuple(findings)


def lane_rollup(
    results: Iterable[assurance_models.DeterministicResult],
) -> dict[str, str]:
    """Reduce results to one status per rubric lane, keeping the worst.

    A lane no check reported on rolls up as ``unavailable`` rather than as
    ``pass``, so a gap in coverage reads as a gap.
    """
    worst: dict[str, str] = {}
    for result in results:
        current = worst.get(result.lane)
        if (
            current is None
            or _STATUS_PRECEDENCE[result.status] > _STATUS_PRECEDENCE[current]
        ):
            worst[result.lane] = result.status
    return {
        lane: worst.get(lane, "unavailable") for lane in assurance_models.RUBRIC_LANES
    }
