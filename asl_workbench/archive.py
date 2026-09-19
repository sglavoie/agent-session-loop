"""What archiving, restoring, and emptying mean -- shared by both surfaces.

:mod:`asl_workbench.controls` says what an operator may ask of a run that is
going. This module answers the neighbouring question for a plan that is
*finished with*: pin it, unpin it, set how long its archive is kept, archive it,
archive a batch of them, put one back, and -- the one that cannot be undone --
empty entries permanently.

Three decisions live here rather than in either surface:

* **Retention is a threshold, not a trigger.** An entry whose age has passed the
  retention window is a *candidate for cleanup*. Nothing about crossing that line
  removes it. :func:`classify_entry` says which side of the line an entry is on
  and :func:`cleanup_review` counts them; neither takes a path, opens a file, or
  reads a clock it was not handed. That is the whole of what retention now does,
  and it is deliberately expressed as a projection so it is impossible to call it
  and delete something by accident.
* **What counts as confirmation.** Two contracts, kept exactly as they were:
  archiving one plan requires the operator to retype its full path, and emptying
  archive entries requires a typed count equal to the number of entries selected.
  Both are checked before anything is touched, and a mismatch is a refusal that
  leaves the archive as it found it.
* **What a refusal is about.** A refused archive operation is refused by the
  request, by the plan's own state (something live holds it), by a durable record
  that disagrees (a restore whose destination is occupied), by an entry that is
  not there, or by the machine being unable to carry it out. Those five need five
  different things from the client, and a surface that collapses them sends a
  client back to do the one thing that cannot help.

Nothing here reads a clock, opens a file, or reaches ASL. It validates the shape
of a request and names what an answer means, which is what lets the Pi forward a
cleanup it must not itself perform.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# -- The operations this surface owns ----------------------------------------

#: Registry pins. Not archive operations, but the same registry file and the
#: same serialized read-modify-write, which is why they are named here: the
#: thing that must not be duplicated is the mutation path, not the noun.
OPERATION_PIN = "registry.pin"
OPERATION_UNPIN = "registry.unpin"
OPERATION_SET_RETENTION = "registry.retention"

#: Archive lifecycle.
OPERATION_ARCHIVE = "archive.plan"
OPERATION_ARCHIVE_MANY = "archive.plans"
OPERATION_RESTORE = "archive.restore"
OPERATION_PURGE = "archive.purge"

#: Releasing a lock an operator has proven dead. It belongs with these because
#: it is the other mutation that reaches into a plan's ASL state without going
#: through a run.
OPERATION_UNLOCK = "plan.unlock"

OPERATIONS: tuple[str, ...] = (
    OPERATION_PIN,
    OPERATION_UNPIN,
    OPERATION_SET_RETENTION,
    OPERATION_ARCHIVE,
    OPERATION_ARCHIVE_MANY,
    OPERATION_RESTORE,
    OPERATION_PURGE,
    OPERATION_UNLOCK,
)

#: The operations that destroy the last copy of something. Exactly one: an
#: archive entry holds the only remaining plan and state directories, so
#: emptying it is the end of the record. Archiving is not here -- it moves.
DESTRUCTIVE_OPERATIONS = frozenset({OPERATION_PURGE})

#: The operations that may not proceed on a caller's say-so alone. Each carries
#: a different proof because each selects its target differently: a single plan
#: is confirmed by retyping its path, a set of archive entries by typing how
#: many were selected.
CONFIRMED_OPERATIONS = frozenset({OPERATION_ARCHIVE, OPERATION_PURGE})

#: How each confirmed operation is confirmed, as data, so a surface cannot
#: accept one proof where the other was required.
CONFIRMATION_FIELD: Mapping[str, str] = {
    OPERATION_ARCHIVE: "confirm_plan_dir",
    OPERATION_PURGE: "confirm_count",
}


def requires_confirmation(operation: str) -> bool:
    """Whether ``operation`` may not proceed without an operator's proof."""
    return operation in CONFIRMED_OPERATIONS


def is_destructive(operation: str) -> bool:
    """Whether ``operation`` ends the last remaining record of a run."""
    return operation in DESTRUCTIVE_OPERATIONS


# -- What a refusal is about -------------------------------------------------

#: The body is wrong, and resending it unchanged cannot succeed.
SCOPE_REQUEST = "request"
#: The plan's own state refuses: a live lock holds it, or the path is not a
#: plan this Workbench manages. Resolved by acting on the plan, not the body.
SCOPE_PLAN = "plan"
#: Something already on disk contradicts the request -- a restore destination
#: that exists. Resolved by reconciling with what is there.
SCOPE_CONFLICT = "conflict"
#: The entry named is not in the archive.
SCOPE_MISSING = "missing"
#: Nothing truthful can be answered right now: an unreadable registry or a
#: filesystem that refused the move. A fact about the machine.
SCOPE_UNAVAILABLE = "unavailable"

REFUSAL_SCOPES: tuple[str, ...] = (
    SCOPE_REQUEST,
    SCOPE_PLAN,
    SCOPE_CONFLICT,
    SCOPE_MISSING,
    SCOPE_UNAVAILABLE,
)

#: The HTTP status each scope earns. Stated once, here, so the local server and
#: a remote connector answering the same refusal answer it the same way.
STATUS_FOR_SCOPE: Mapping[str, int] = {
    SCOPE_REQUEST: 400,
    SCOPE_PLAN: 409,
    SCOPE_CONFLICT: 409,
    SCOPE_MISSING: 404,
    SCOPE_UNAVAILABLE: 500,
}

#: Refusal reasons. ASL mints most of these itself and they travel unrenamed,
#: because an operator reading a Workbench error and an operator reading a CLI
#: error must see the same word for the same refusal.
REASON_INVALID_REQUEST = "invalid-request"
REASON_INVALID_PLAN = "invalid-plan"
REASON_INVALID_ARCHIVE = "invalid-archive"
REASON_CONFIRMATION_MISMATCH = "confirmation-mismatch"
REASON_ACTIVE_LOCK = "active-lock"
REASON_RESTORE_CONFLICT = "restore-conflict"
REASON_NOT_FOUND = "not-found"
REASON_REGISTRY_UNREADABLE = "registry-unreadable"
REASON_STATE_ERROR = "state-error"
#: The plan directory itself does not parse -- distinct from a plan that parses
#: and is merely busy, because only one of the two is worth retrying.
REASON_MALFORMED_PLAN = "malformed-plan"
REASON_OS_ERROR = "os-error"

#: Which scope each reason belongs to. ASL's archive helpers carry their own
#: HTTP status, and where the two disagree the helper's status wins on the wire
#: -- this table is what a surface with no status vocabulary (a protocol frame,
#: a CLI exit) translates through instead.
SCOPE_FOR_REASON: Mapping[str, str] = {
    REASON_INVALID_REQUEST: SCOPE_REQUEST,
    REASON_INVALID_PLAN: SCOPE_REQUEST,
    REASON_INVALID_ARCHIVE: SCOPE_REQUEST,
    REASON_CONFIRMATION_MISMATCH: SCOPE_REQUEST,
    REASON_ACTIVE_LOCK: SCOPE_PLAN,
    REASON_RESTORE_CONFLICT: SCOPE_CONFLICT,
    REASON_NOT_FOUND: SCOPE_MISSING,
    REASON_REGISTRY_UNREADABLE: SCOPE_UNAVAILABLE,
    REASON_STATE_ERROR: SCOPE_REQUEST,
    REASON_MALFORMED_PLAN: SCOPE_REQUEST,
    REASON_OS_ERROR: SCOPE_UNAVAILABLE,
}


def scope_for_reason(reason: str) -> str:
    """Which of the five refusal scopes ``reason`` falls in.

    An unknown reason is ``unavailable`` rather than ``request``: a refusal this
    module has never heard of is not evidence that the client can fix it, and
    telling a client to rewrite a body that was fine is the worse error.
    """
    return SCOPE_FOR_REASON.get(reason, SCOPE_UNAVAILABLE)


def status_for_scope(scope: str) -> int:
    """The HTTP status one refusal scope earns."""
    return STATUS_FOR_SCOPE.get(scope, 500)


class ArchiveRequestError(Exception):
    """An archive request refused on its own terms, before anything was read."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        scope: str = SCOPE_REQUEST,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.scope = scope
        self.details: dict[str, Any] = dict(details or {})


# -- Request shapes ----------------------------------------------------------

#: The characters an archive id may not contain. An id names a directory under
#: the archive root, so anything that could leave that root is not an id.
_ARCHIVE_ID_FORBIDDEN = ("/", "\\", "\0")


def valid_archive_id(archive_id: Any) -> bool:
    """Whether ``archive_id`` names an entry without escaping the archive root.

    The single definition of the rule. ``agent_session_loop.workbench`` calls
    this rather than keeping its own copy, so a traversal the provider would
    reject cannot be one a remote caller is allowed to submit.
    """
    if not isinstance(archive_id, str) or not archive_id.strip():
        return False
    if archive_id in (".", ".."):
        return False
    return not any(char in archive_id for char in _ARCHIVE_ID_FORBIDDEN)


def require_retention_days(value: Any) -> int:
    """Narrow a retention window to a positive whole number of days.

    A bool is rejected explicitly: ``True`` is an ``int`` that would set the
    retention window to one day, which is both wrong and destructive-adjacent.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArchiveRequestError(
            REASON_INVALID_REQUEST,
            "archive_retention_days must be an integer",
        )
    if value < 1:
        raise ArchiveRequestError(
            REASON_INVALID_REQUEST,
            "archive_retention_days must be a positive integer",
            details={"archive_retention_days": value},
        )
    return value


def require_string_list(raw: Any, *, label: str) -> list[str]:
    """Narrow a batch selection to a non-empty list of non-empty strings.

    Shape only. Whether an entry names something that exists is the provider's
    answer, reported per item, because a purge of five ids where one has already
    been emptied is a partial success rather than a bad request.
    """
    if not isinstance(raw, list):
        raise ArchiveRequestError(
            REASON_INVALID_REQUEST, f"missing required list field {label!r}"
        )
    if not raw:
        raise ArchiveRequestError(
            REASON_INVALID_REQUEST, f"field {label!r} must not be empty"
        )
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise ArchiveRequestError(
                REASON_INVALID_REQUEST,
                f"field {label!r} must contain non-empty strings",
            )
        values.append(item)
    return values


def require_archive_ids(raw: Any) -> list[str]:
    """The purge selection, shaped. Named for the field it validates."""
    return require_string_list(raw, label="archive_ids")


def require_count_confirmation(archive_ids: Sequence[str], confirm_count: Any) -> int:
    """Hold a permanent purge to a typed count matching what was selected.

    The contract is unchanged and deliberately so: it is the part of automatic
    retention's replacement that was already right. Checked before any entry is
    touched, so a mismatch leaves the archive exactly as it was.
    """
    if isinstance(confirm_count, bool) or not isinstance(confirm_count, int):
        raise ArchiveRequestError(
            REASON_INVALID_REQUEST, "confirm_count must be an integer"
        )
    if confirm_count != len(archive_ids):
        raise ArchiveRequestError(
            REASON_CONFIRMATION_MISMATCH,
            "confirm_count must match the number of archive_ids",
            details={
                "confirm_count": confirm_count,
                "archive_ids": len(archive_ids),
            },
        )
    return confirm_count


def require_path_confirmation(canonical: str, confirmed: str) -> str:
    """Hold a single-plan archive to the operator retyping its full path.

    Both paths are canonical by the time they reach here: comparing the raw
    strings would let ``.``, a symlink, or a trailing slash pass for a
    confirmation the operator never actually typed.
    """
    if canonical != confirmed:
        raise ArchiveRequestError(
            REASON_CONFIRMATION_MISMATCH,
            "confirm_plan_dir must match plan_dir after canonicalization",
            details={"plan_dir": canonical, "confirm_plan_dir": confirmed},
        )
    return canonical


# -- Retention as a threshold ------------------------------------------------

#: The entry is inside its retention window.
CLEANUP_RETAINED = "retained"
#: The entry has passed its retention window: an operator may empty it, and
#: nothing else will. This is the state that used to be a deletion.
CLEANUP_ELIGIBLE = "eligible"
#: The entry records no readable ``archived_at``, so its age is unknown. It is
#: never a candidate: "we cannot tell how old this is" must not become "empty
#: it", which is the precise mistake an age-driven sweep makes silently.
CLEANUP_UNDATED = "undated"

CLEANUP_STATES: tuple[str, ...] = (
    CLEANUP_RETAINED,
    CLEANUP_ELIGIBLE,
    CLEANUP_UNDATED,
)


@dataclass(frozen=True)
class RetentionVerdict:
    """Which side of the retention threshold one archive entry is on."""

    archive_id: str
    state: str
    age_seconds: float | None
    eligible_at: str | None

    @property
    def candidate(self) -> bool:
        """Whether an operator could choose to empty this entry today."""
        return self.state == CLEANUP_ELIGIBLE

    def to_payload(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "cleanup_state": self.state,
            "cleanup_candidate": self.candidate,
            "age_seconds": self.age_seconds,
            "eligible_at": self.eligible_at,
        }


@dataclass(frozen=True)
class CleanupReview:
    """What retention has to say about an archive, and nothing it did.

    There is no ``removed_count`` here and that is the point: the projection a
    surface renders after this change cannot report a deletion, because reading
    the archive no longer performs one.
    """

    retention_days: int
    verdicts: tuple[RetentionVerdict, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(v.archive_id for v in self.verdicts if v.candidate)

    @property
    def candidate_count(self) -> int:
        return len(self.candidate_ids)

    @property
    def undated_count(self) -> int:
        return sum(1 for v in self.verdicts if v.state == CLEANUP_UNDATED)

    def verdict(self, archive_id: str) -> RetentionVerdict | None:
        for candidate in self.verdicts:
            if candidate.archive_id == archive_id:
                return candidate
        return None

    def to_payload(self) -> dict[str, Any]:
        return {
            "retention_days": self.retention_days,
            "candidate_count": self.candidate_count,
            "candidate_ids": list(self.candidate_ids),
            "undated_count": self.undated_count,
            "entry_count": len(self.verdicts),
            "warnings": list(self.warnings),
        }


def parse_timestamp(value: Any) -> datetime | None:
    """Read an archive ISO timestamp, or ``None`` when it cannot be read.

    A naive value is read as UTC, matching what the archive writes. Returning
    ``None`` rather than raising is what makes an unreadable timestamp an
    ``undated`` entry instead of an error that hides every other entry.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def classify_entry(
    archive_id: str,
    archived_at: Any,
    *,
    now: datetime,
    retention_days: int,
) -> RetentionVerdict:
    """Which side of the retention threshold one entry is on.

    ``now`` is passed in rather than read, so the verdict is a function of the
    entry and the window alone. That is what lets the same classification run on
    the Pi against a cached listing without the Pi's clock deciding what a Mac
    is allowed to delete.
    """
    retention = timedelta(days=max(0, retention_days))
    archived = parse_timestamp(archived_at)
    if archived is None:
        return RetentionVerdict(archive_id, CLEANUP_UNDATED, None, None)
    reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    age = reference - archived
    eligible_at = (archived + retention).isoformat()
    state = CLEANUP_ELIGIBLE if age >= retention else CLEANUP_RETAINED
    return RetentionVerdict(archive_id, state, age.total_seconds(), eligible_at)


def cleanup_review(
    entries: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    retention_days: int,
    warnings: Sequence[str] = (),
) -> CleanupReview:
    """Classify a whole archive listing against the live retention window.

    ``entries`` are payload mappings rather than provider objects, so a remote
    provider holding a cached listing classifies it with the same code the Mac
    uses on the real one. The window comes from the registry at call time, so a
    shortened retention makes more entries candidates immediately -- what it no
    longer does is make them disappear.
    """
    verdicts: list[RetentionVerdict] = []
    extra: list[str] = list(warnings)
    for entry in entries:
        archive_id = entry.get("archive_id")
        if not isinstance(archive_id, str) or not archive_id:
            extra.append("archive: entry without an archive_id; skipping")
            continue
        verdict = classify_entry(
            archive_id,
            entry.get("archived_at"),
            now=now,
            retention_days=retention_days,
        )
        if verdict.state == CLEANUP_UNDATED:
            extra.append(
                f"archive[{archive_id}]: unreadable archived_at "
                f"{entry.get('archived_at')!r}; age unknown"
            )
        verdicts.append(verdict)
    return CleanupReview(
        retention_days=retention_days,
        verdicts=tuple(verdicts),
        warnings=tuple(extra),
    )


# -- Bulk results ------------------------------------------------------------

#: A bulk archive or purge reports one row per requested item, never a single
#: verdict for the batch. One blocked plan must not decide the fate of the four
#: beside it, and an operator who asked for five needs to know which two moved.
BULK_RESULT_FIELDS: tuple[str, ...] = ("archived", "reason", "error")


@dataclass(frozen=True)
class BulkOutcome:
    """One item's result inside a batch, succeeded or refused."""

    target: str
    ok: bool
    reason: str
    error: str | None = None
    result: Mapping[str, Any] | None = None

    def to_payload(self, *, target_field: str, ok_field: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            target_field: self.target,
            ok_field: self.ok,
            "reason": self.reason,
            "error": self.error,
        }
        if self.result is not None:
            payload["result"] = dict(self.result)
        return payload


def bulk_counts(outcomes: Sequence[BulkOutcome]) -> tuple[int, int]:
    """``(succeeded, failed)`` across a batch, counted from the rows."""
    succeeded = sum(1 for outcome in outcomes if outcome.ok)
    return succeeded, len(outcomes) - succeeded


__all__ = [
    "BULK_RESULT_FIELDS",
    "CLEANUP_ELIGIBLE",
    "CLEANUP_RETAINED",
    "CLEANUP_STATES",
    "CLEANUP_UNDATED",
    "CONFIRMATION_FIELD",
    "CONFIRMED_OPERATIONS",
    "DESTRUCTIVE_OPERATIONS",
    "OPERATIONS",
    "OPERATION_ARCHIVE",
    "OPERATION_ARCHIVE_MANY",
    "OPERATION_PIN",
    "OPERATION_PURGE",
    "OPERATION_RESTORE",
    "OPERATION_SET_RETENTION",
    "OPERATION_UNLOCK",
    "OPERATION_UNPIN",
    "REASON_ACTIVE_LOCK",
    "REASON_CONFIRMATION_MISMATCH",
    "REASON_INVALID_ARCHIVE",
    "REASON_INVALID_PLAN",
    "REASON_INVALID_REQUEST",
    "REASON_MALFORMED_PLAN",
    "REASON_NOT_FOUND",
    "REASON_OS_ERROR",
    "REASON_REGISTRY_UNREADABLE",
    "REASON_RESTORE_CONFLICT",
    "REASON_STATE_ERROR",
    "REFUSAL_SCOPES",
    "SCOPE_CONFLICT",
    "SCOPE_FOR_REASON",
    "SCOPE_MISSING",
    "SCOPE_PLAN",
    "SCOPE_REQUEST",
    "SCOPE_UNAVAILABLE",
    "STATUS_FOR_SCOPE",
    "ArchiveRequestError",
    "BulkOutcome",
    "CleanupReview",
    "RetentionVerdict",
    "bulk_counts",
    "classify_entry",
    "cleanup_review",
    "is_destructive",
    "parse_timestamp",
    "require_archive_ids",
    "require_count_confirmation",
    "require_path_confirmation",
    "require_retention_days",
    "require_string_list",
    "requires_confirmation",
    "scope_for_reason",
    "status_for_scope",
    "valid_archive_id",
]
