"""What a re-attempted session still has to answer for from its earlier tries.

A session is not always one turn. The run loop re-attempts the *same* session
inside one run on four paths -- a corrected malformed plan, a runner pause the
loop absorbs, a nonzero exit it retries, and a verification repair cycle -- and
since Session 37 each of those relaunches *continues* the previous attempt's
conversation rather than starting a new one. The work is therefore one piece of
work spread across several turns.

Every post-turn guard, though, is scoped to a turn. ``capture_turn_baselines``
takes a fresh before-image at the top of each one, so attempt 2's write-guard
evidence describes only what attempt 2 changed. Attempt 1's retained bytes are
still on disk -- nothing reverted them, because the pause that ended attempt 1
was recoverable -- but they have vanished from the record every downstream
consumer reads:

* ``commit_settlement._registered_session_commit_targets`` derives *every*
  staging pathspec from that record, so the per-session commit stages attempt
  2's files and silently leaves attempt 1's uncommitted.
* ``commit_settlement._paused_work_commit_targets`` reads the same record, so a
  later recoverable pause settles the same narrowed set.
* ``write_guards._protected_path_changed_paths`` reads it too, so a path rule an
  operator armed between the two attempts adjudicates attempt 2's changes and
  never sees attempt 1's.

This module is the ledger that closes that gap: a run-scoped record of what each
attempt at a session was observed to change, and one merge that projects the
accumulated observation back into the turn's own guard record before anything
downstream reads it. One record, cumulative; every consumer fixed at once
because every consumer already reads that one record.

Four rules the merge holds to, and the reason for each:

* **Deltas accumulate; state refreshes.** ``changed`` is a statement about work
  performed, so it is unioned across attempts. ``violations``, ``unavailable``,
  ``added``, ``removed``, ``external_writes`` and the scope fields are
  statements about the workspace *as this attempt found it*, so the newest
  observation replaces the older one outright. Unioning ``added`` would be the
  clearest way to get this wrong: a topology note that was true one attempt ago
  is not evidence about the tree the commit is about to run in.
* **The newest observation of a path wins.** Where both attempts saw the same
  repository the merge keeps the earliest ``head_before`` and the latest
  ``head_after``, and composes the two ref transitions rather than concatenating
  them. Carrying a stale ``after`` forward would be treating an old reading as
  proof about bytes a later attempt has since rewritten.
* **A merge never upgrades a verdict.** Only a record that passed, convicted
  nothing and left nothing unobserved is eligible on either side; anything else
  refuses the merge and returns ``None``. A guard that could not see is not a
  guard that saw nothing, and an older sighting must never dress it up as one.
* **A merge that carries nothing rewrites nothing.** ``None`` also means "the
  accumulated view equals the one you already have", so the caller can skip the
  durable rewrite instead of re-persisting an identical record.

The ledger is deliberately run-scoped and in-memory, mirrored into the run
payload for audit rather than read back from it. Continuation is a property of
one run's control flow: a *new* run over the same plan re-reads the tree it
finds, and the bytes a previous run retained are already covered there by
``commit_settlement._dirty_commit_targets``. Rehydrating this ledger across runs
would let a stale path list drive a commit in a tree nothing had re-observed.

Nothing here imports the runner or ``run_loop``. The ledger enters those modules
as a field on their own contexts; this module knows only mappings.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: Where the run payload carries the per-session attempt summaries. The
#: cumulative guard records themselves stay in memory: each is already written
#: durably as part of its own turn's result artifact, and duplicating them into
#: the state record would grow it by the size of the workspace on every retry.
SESSION_ATTEMPTS_PAYLOAD_KEY = "session_attempts"

#: The block the merge adds to a guard record to say it is cumulative. Present
#: only on a record that actually carried something forward, so its absence is
#: the honest statement that the record describes exactly one turn.
CONTINUATION_EVIDENCE_KEY = "continuation"

#: The per-repository change categories that name paths. Unioned across
#: attempts, in this order, because they are what
#: ``repository_evidence._qualified_paths`` and
#: ``commit_settlement.STAGEABLE_CHANGE_CATEGORIES`` read back out.
MERGED_CHANGE_CATEGORIES: tuple[str, ...] = (
    "tracked",
    "untracked_added",
    "untracked_changed",
    "untracked_removed",
    "worktree_ignore",
    "ignore_config",
)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AttemptSummary:
    """One attempt at a session, as the run record has to be able to read it.

    Deliberately a summary rather than the guard record itself: what an operator
    reading the state file needs is which turns worked on this session and how
    much each of them was seen to touch, and the record that proves it is the
    turn's own result artifact, which is already durable and already linked by
    ``turn_index``.
    """

    turn_index: int
    disposition: str
    repositories: tuple[str, ...]
    qualified_path_count: int

    def to_payload(self) -> dict[str, Any]:
        """Render this attempt as the plain JSON the state record carries."""
        return {
            "turn_index": self.turn_index,
            "disposition": self.disposition,
            "repositories": list(self.repositories),
            "qualified_paths": self.qualified_path_count,
        }


#: The disposition recorded for a turn that reached the status guards rather
#: than a terminating result branch -- a verification repair cycle's attempt,
#: or the attempt that completes the session.
DISPOSITION_SETTLED = "settled"


class SessionAttempts:
    """Every attempt this run has made at each session, keyed by session id.

    Two things are kept per session: the cumulative guard record the newest
    attempt ended with, which is what the next attempt merges against, and the
    ordered summaries the run payload publishes. Only the newest record is
    retained because each one is *already* cumulative -- merging it forward is
    idempotent -- which keeps the ledger flat instead of quadratic in retries.

    Retirement is by session id and happens when the session completes, for the
    same reason a completed session's conversation is retired: there is no turn
    left to continue, so a later attempt at that id (a reopened repair group, a
    re-run) must start from what it can observe rather than inherit a path list
    from work that is already committed.
    """

    def __init__(self) -> None:
        self._latest: dict[str, dict[str, Any]] = {}
        self._summaries: dict[str, list[AttemptSummary]] = {}

    def latest(self, session_id: str) -> dict[str, Any] | None:
        """The cumulative guard record the previous attempt at ``session_id`` left."""
        return self._latest.get(session_id)

    def summaries(self, session_id: str) -> tuple[AttemptSummary, ...]:
        """Every attempt recorded for ``session_id``, oldest first."""
        return tuple(self._summaries.get(session_id, ()))

    def record(
        self,
        *,
        session_id: str,
        turn_index: int,
        disposition: str | None,
        guard: Mapping[str, Any] | None,
    ) -> None:
        """Record one attempt that got far enough to retain work.

        Called from the single site that arms retained work, which is what
        makes the ledger's meaning precise: a turn a post-turn guard refused
        never reaches it, so a refused attempt's paths can never be carried
        into a later attempt's commit scope.
        """
        summary = AttemptSummary(
            turn_index=turn_index,
            disposition=disposition or DISPOSITION_SETTLED,
            repositories=_changed_repositories(guard),
            qualified_path_count=len(_all_qualified_paths(guard)),
        )
        self._summaries.setdefault(session_id, []).append(summary)
        if isinstance(guard, Mapping) and _mergeable(guard):
            self._latest[session_id] = dict(guard)

    def retire(self, session_id: str) -> tuple[AttemptSummary, ...]:
        """Drop the session's accumulated evidence and return what it held."""
        self._latest.pop(session_id, None)
        return tuple(self._summaries.pop(session_id, ()))

    def to_payload(self) -> dict[str, Any]:
        """Render the ledger as the plain JSON the run state record carries."""
        return {
            session_id: [summary.to_payload() for summary in summaries]
            for session_id, summaries in sorted(self._summaries.items())
            if summaries
        }


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------


def merge_session_write_evidence(
    prior: Mapping[str, Any] | None,
    evidence: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Project an earlier attempt's changes into this attempt's guard record.

    ``None`` means "use the record you already have", and covers three distinct
    cases on purpose, because the caller's response to all three is the same:
    there is no earlier attempt, one of the two records is not eligible to be
    merged, or the accumulated view is identical to the current one.
    """
    if not isinstance(evidence, Mapping) or not isinstance(prior, Mapping):
        return None
    if not _mergeable(prior) or not _mergeable(evidence):
        return None
    prior_changed = prior.get("changed")
    changed = evidence.get("changed")
    if not isinstance(prior_changed, Mapping) or not isinstance(changed, Mapping):
        return None

    merged_changed: dict[str, Any] = {}
    carried: list[str] = []
    extended: list[str] = []
    # Sorted by repository key, which is how ``compare_workspace_snapshots``
    # and ``_workspace_write_guard_evidence`` order the map this replaces: a
    # merged record has to read like a record, not like a merge.
    for repository in sorted(set(prior_changed) | set(changed)):
        current_record = changed.get(repository)
        earlier_record = prior_changed.get(repository)
        if not isinstance(current_record, Mapping):
            if not isinstance(earlier_record, Mapping):
                continue
            # Nothing re-observed this repository, so the earlier observation
            # is still the only one there is and it stands unamended.
            merged_changed[repository] = dict(earlier_record)
            carried.append(repository)
            continue
        if not isinstance(earlier_record, Mapping):
            merged_changed[repository] = dict(current_record)
            continue
        merged_changed[repository] = _merge_repository(earlier_record, current_record)
        extended.append(repository)

    if not carried and not _extends_any(prior_changed, changed, extended):
        return None

    merged = dict(evidence)
    merged["changed"] = merged_changed
    merged["authorized_aliases"] = _merged_authorized_aliases(
        prior, evidence, merged_changed
    )
    merged[CONTINUATION_EVIDENCE_KEY] = {
        "merged": True,
        "carried_repositories": carried,
        "extended_repositories": extended,
    }
    return merged


def _mergeable(record: Mapping[str, Any]) -> bool:
    """Whether a record is clean enough for its changes to be carried.

    A failed, convicted or partially blind record is refused on both sides. On
    the prior side because a refusal ends the run rather than re-attempting, so
    such a record can only mean the ledger holds something it should not; on the
    current side because merging into a refusal would blur the verdict an
    operator has to act on with paths from a turn that is no longer in question.

    ``added`` and ``removed`` are deliberately *not* tested. A passing record can
    carry both -- adding a linked worktree of a repository the session may write
    is authorized work -- and they are topology state rather than a delta, so the
    merge never reads them from the prior record and copies the current record's
    verbatim. Refusing here on their account would drop the earlier attempt's
    paths from a commit for a reason that has nothing to do with those paths.
    """
    return (
        record.get("passed") is True
        and not record.get("violations")
        and not record.get("unavailable")
        and isinstance(record.get("changed"), Mapping)
    )


def _extends_any(
    prior_changed: Mapping[str, Any],
    changed: Mapping[str, Any],
    extended: Sequence[str],
) -> bool:
    """Whether merging a shared repository actually added anything."""
    for repository in extended:
        earlier = prior_changed.get(repository)
        current = changed.get(repository)
        if not isinstance(earlier, Mapping) or not isinstance(current, Mapping):
            continue
        if _merge_repository(earlier, current) != dict(current):
            return True
    return False


def _merge_repository(
    earlier: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Compose two observations of one repository into one.

    The current record is the base -- its ``alias``, ``repository`` and
    ``worktree_of`` are how every consumer resolves the checkout the commit will
    run in, and those come from the observation that was actually taken against
    the tree the commit is about to touch.
    """
    merged = dict(current)
    for category in MERGED_CHANGE_CATEGORIES:
        merged[category] = _union_strings(earlier.get(category), current.get(category))
    merged["qualified_paths"] = _union_strings(
        earlier.get("qualified_paths"), current.get("qualified_paths")
    )
    if "head_before" in earlier:
        merged["head_before"] = earlier.get("head_before")
    if "head_before" in merged or "head_after" in merged:
        merged["head_changed"] = merged.get("head_before") != merged.get("head_after")
    # Only where one of the two records actually carries a ref transition. The
    # comparison emits these keys on every real record, but inventing them on a
    # record that has none would make a merge that carried nothing look like one
    # that carried something, and the caller reads exactly that difference to
    # decide whether a durable rewrite is owed.
    if any(
        key in earlier or key in current
        for key in ("refs_added", "refs_removed", "refs_changed")
    ):
        merged.update(_compose_ref_transitions(earlier, current))
    return merged


def _compose_ref_transitions(
    earlier: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Compose two ref transitions into the one they amount to end to end.

    Concatenating the two lists would report a ref that a repair attempt created
    and the next attempt deleted as both added and removed, and a ref that moved
    twice as having moved to its intermediate value. Composing states instead
    means the merged record says what a reader of the *session* would see: where
    each ref stood before the session's first attempt and where it stands now.
    """
    # A ref only the later attempt mentions started the session where that
    # attempt found it; one only the earlier attempt mentions still stands where
    # the earlier attempt left it, because nothing has touched it since.
    before = {**_ref_states(current, "before"), **_ref_states(earlier, "before")}
    after = {**_ref_states(earlier, "after"), **_ref_states(current, "after")}
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    moved: list[dict[str, Any]] = []
    for name in sorted(set(before) | set(after)):
        start = before.get(name)
        end = after.get(name)
        if start is None and end is not None:
            added.append({"ref": name, "after": end})
        elif start is not None and end is None:
            removed.append({"ref": name, "before": start})
        elif start is not None and end is not None and start != end:
            moved.append({"ref": name, "before": start, "after": end})
    return {"refs_added": added, "refs_removed": removed, "refs_changed": moved}


def _ref_states(record: Mapping[str, Any], phase: str) -> dict[str, str | None]:
    """Where each ref this record mentions stood at ``phase``.

    ``None`` is the ref not existing at that end of the transition, which is
    exactly what ``refs_added``'s missing ``before`` and ``refs_removed``'s
    missing ``after`` mean.
    """
    states: dict[str, str | None] = {}
    for key in ("refs_added", "refs_removed", "refs_changed"):
        entries = record.get(key)
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("ref")
            if not isinstance(name, str):
                continue
            value = entry.get(phase)
            states[name] = str(value) if isinstance(value, str) else None
    return states


def _merged_authorized_aliases(
    prior: Mapping[str, Any],
    evidence: Mapping[str, Any],
    merged_changed: Mapping[str, Any],
) -> list[str]:
    """Which aliases the cumulative record is authorized to have written.

    ``commit_settlement._registered_session_commit_targets`` refuses a record
    whose authorized aliases are not exactly the changed aliases *in workspace
    registration order*, so a registered record recomputes the list from the
    registry the current attempt recorded rather than concatenating two lists.
    A legacy record registers nothing and names one pseudo-alias, so it keeps
    the union of what the two attempts claimed, still gated on something having
    changed at all.
    """
    changed_aliases = {
        alias
        for entry in merged_changed.values()
        if isinstance(entry, Mapping)
        for alias in (entry.get("alias"),)
        if isinstance(alias, str)
    }
    registered = evidence.get("registered_repositories")
    if isinstance(registered, list) and registered:
        return [
            alias
            for entry in registered
            if isinstance(entry, Mapping)
            for alias in (entry.get("alias"),)
            if isinstance(alias, str) and alias in changed_aliases
        ]
    union = list(
        dict.fromkeys(
            [
                *_strings(evidence.get("authorized_aliases")),
                *_strings(prior.get("authorized_aliases")),
            ]
        )
    )
    return union if merged_changed else []


# ---------------------------------------------------------------------------
# Reading a guard record
# ---------------------------------------------------------------------------


def _changed_repositories(guard: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(guard, Mapping):
        return ()
    changed = guard.get("changed")
    if not isinstance(changed, Mapping):
        return ()
    return tuple(sorted(str(repository) for repository in changed))


def _all_qualified_paths(guard: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(guard, Mapping):
        return ()
    changed = guard.get("changed")
    if not isinstance(changed, Mapping):
        return ()
    paths: list[str] = []
    for entry in changed.values():
        if isinstance(entry, Mapping):
            paths.extend(_strings(entry.get("qualified_paths")))
    return tuple(dict.fromkeys(paths))


def _strings(values: Any) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    return [str(value) for value in values]


def _union_strings(earlier: Any, current: Any) -> list[str]:
    """Union two path lists, earlier first, order-preserving and deduplicated."""
    return _ordered(_strings(earlier) + _strings(current))


def _ordered(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
