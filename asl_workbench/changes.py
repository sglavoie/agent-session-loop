"""The behaviors this program changes on purpose.

Characterization tests pin what the code does today so a refactor that changes
it fails loudly. That is the right default, and it is wrong for exactly the
cases below: the consolidation intends to change these two behaviors, and the
tests currently pinning them will have to be rewritten rather than satisfied.

Writing that down here, as data, separates the two failure modes that look
identical from inside a test run. A pinned test that breaks is either a
regression -- stop -- or one of these -- proceed, and update the pin in the
same change. A session that watched ``test_archive_list_auto_empties_expired_entries``
fail had no way to tell which, unless the intent was recorded before the work
started. So it was recorded in session 1, with the current behavior, the target
behavior, and the exact tests that encoded the current one.

Both records have since landed, and ``pinned_coverage`` moved with them: it now
names the tests that hold each behavior to its *target*. That is the same job
in the other direction -- the pins stopped being what a refactor has to break
and became what a regression would.

Each record also names the capability rows it touches, and
``tests/test_workbench_foundations.py`` checks the link both ways: no record
points at a capability the matrix lacks, and no capability claims a change that
is not registered here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

KEY_OBSERVATIONAL_READS = "observational-reads"
KEY_EXPLICIT_ARCHIVE_CLEANUP = "explicit-archive-cleanup"


class ChangeError(Exception):
    """Raised when the register is asked about a change it does not carry."""


@dataclass(frozen=True)
class IntentionalChange:
    """One behavior the consolidation changes deliberately."""

    key: str
    summary: str
    current_behavior: str
    target_behavior: str
    rationale: str
    pinned_coverage: tuple[str, ...]
    capability_keys: tuple[str, ...]


INTENTIONAL_CHANGES: tuple[IntentionalChange, ...] = (
    IntentionalChange(
        key=KEY_OBSERVATIONAL_READS,
        summary="Reads observe; they do not mutate the run's records.",
        current_behavior=(
            "Every read endpoint is observational. The catalog scan behind "
            "GET /api/queue and GET /api/plan, and every detail read behind "
            "GET /api/artifacts, GET /api/state, GET /api/events, "
            "GET /api/logs, GET /api/last-message, GET /api/result, "
            "GET /api/verification, GET /api/commits, GET /api/progress, "
            "GET /api/final-review, GET /api/questions and GET /api/recovery, "
            "now classify with reclaim=False and run no retention: polling the "
            "queue or opening a detail panel no longer reclaims a plan lock or "
            "empties an archive entry. The last automatic path is closed too: "
            "GET /api/archive reports each entry's cleanup_state and removes "
            "nothing, and workbench.maintain_archive no longer exists. "
            "Reclaim now happens only where an operator asked for it, at "
            "POST /api/unlock. The summary builders still default to "
            "reclaim=True for callers that have not been moved."
        ),
        target_behavior=(
            "A read answers from what is on disk and changes nothing. "
            "Retention runs from an explicit maintenance path, and lock "
            "reclaim happens where an operator asked for it -- the recovery "
            "flow and POST /api/unlock -- not as a side effect of a poll."
        ),
        rationale=(
            "The Pi half polls. Once the same application is read by a "
            "dashboard refreshing on a phone, a mutating read means the "
            "act of looking deletes archives and reclaims locks at whatever "
            "rate the page happens to refresh. The Pi is also explicitly a "
            "read-mostly cockpit, so a read that mutates would put the "
            "remote provider in the position of destroying records it is not "
            "the authority for."
        ),
        pinned_coverage=(
            # The archive half, landed. These replace the two tests that used
            # to pin the deleting read; they hold the same scenario to the
            # opposite outcome, so a reintroduced maintenance pass fails here.
            "scripts:tests/test_asl_workbench_server_archive.py"
            "::test_archive_list_leaves_expired_entries_in_place",
            "scripts:tests/asl_workbench_archive_cases_restore.py"
            "::test_retention_marks_expired_entries_without_removing_them",
            # The half already landed. These hold the queue and plan reads to
            # the target so the remaining archive work cannot undo them.
            "scripts:tests/test_workbench_catalog_observation.py"
            "::test_polling_the_queue_no_longer_empties_an_expired_archive_entry",
            "scripts:tests/test_workbench_catalog_observation.py"
            "::test_polling_the_queue_leaves_a_reclaimable_lock_alone",
            "scripts:tests/test_workbench_catalog_observation.py"
            "::test_reading_one_plan_leaves_a_reclaimable_lock_alone",
            "scripts:tests/test_workbench_catalog_observation.py"
            "::test_repeated_catalog_reads_leave_the_whole_state_tree_unchanged",
            # The detail half. These hold the four routes that still reclaimed
            # after the catalog work -- state, verification, questions and the
            # recovery card -- to the target.
            "scripts:tests/test_workbench_details.py"
            "::test_state_read_leaves_a_reclaimable_lock_alone",
            "scripts:tests/test_workbench_details.py"
            "::test_verification_read_leaves_a_reclaimable_lock_alone",
            "scripts:tests/test_workbench_details.py"
            "::test_recovery_read_leaves_a_reclaimable_lock_alone",
            "scripts:tests/test_workbench_details.py"
            "::test_questions_read_leaves_a_reclaimable_lock_alone",
            "scripts:tests/test_workbench_details.py"
            "::test_every_detail_read_leaves_the_whole_state_tree_unchanged",
        ),
        capability_keys=(
            "workbench-http:GET /api/queue",
            "workbench-http:GET /api/plan",
            "workbench-http:GET /api/recovery",
            "workbench-http:GET /api/state",
            "workbench-http:GET /api/verification",
            "workbench-http:GET /api/questions",
            "workbench-http:GET /api/archive",
            "persistence:asl-plan-lock",
        ),
    ),
    IntentionalChange(
        key=KEY_EXPLICIT_ARCHIVE_CLEANUP,
        summary="Archives are emptied when someone says so, not when they age.",
        current_behavior=(
            "One path empties an archive entry, and it is explicit and "
            "confirmed: POST /api/archive/delete requires confirm_count to "
            "equal the number of archive_ids supplied. The automatic path is "
            "gone -- the retention window now marks entries as cleanup "
            "candidates (cleanup_state on each entry, archive_cleanup in the "
            "listing) and removes none of them, and shortening the window "
            "marks more candidates rather than deleting anything."
        ),
        target_behavior=(
            "Permanent deletion keeps its explicit confirmation contract, and "
            "retention stops deleting on its own. Expiry marks an entry as "
            "eligible; emptying it stays an operator action with the same "
            "confirmation it has now."
        ),
        rationale=(
            "An archive entry holds the plan and state copies -- the only "
            "remaining record of a run. Automatic retention was safe while a "
            "single operator on one Mac triggered it; with a remote provider "
            "polling the same application, nothing distinguishes a deletion "
            "that an operator intended from one a refresh caused. The "
            "confirmation contract is the part worth keeping, so it is the "
            "part that stays."
        ),
        pinned_coverage=(
            "scripts:tests/asl_workbench_archive_cases_restore.py"
            "::test_retention_marks_expired_entries_without_removing_them",
            "scripts:tests/asl_workbench_archive_cases_restore.py"
            "::test_shortening_retention_marks_more_candidates_only",
            "scripts:tests/test_asl_workbench_server_archive.py"
            "::test_archive_list_leaves_expired_entries_in_place",
            # The confirmation contract is the half that did not change, so it
            # is pinned alongside: a later session reading this record must not
            # read "retention stopped deleting" as "deletion got easier".
            "scripts:tests/test_workbench_archive.py"
            "::test_purging_without_a_matching_count_removes_nothing",
        ),
        capability_keys=(
            "workbench-http:GET /api/archive",
            "workbench-http:POST /api/archive/delete",
            "workbench-http:POST /api/archive/settings",
            "workbench-browser:archive-browser",
            "persistence:workbench-archive",
        ),
    ),
)


def change_keys() -> tuple[str, ...]:
    """Every registered change key, in register order."""
    return tuple(record.key for record in INTENTIONAL_CHANGES)


def intentional_change(key: str) -> IntentionalChange:
    """The record for ``key``, or :class:`ChangeError` if it is not registered."""
    for record in INTENTIONAL_CHANGES:
        if record.key == key:
            return record
    raise ChangeError(f"no intentional change named {key!r}")


def validate_changes(
    known_capabilities: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Every structural problem with the register, as lines naming the record.

    Passing ``known_capabilities`` checks that each record points at rows the
    matrix actually carries -- the direction that catches a capability renamed
    out from under a change record.
    """
    capabilities = set(known_capabilities) if known_capabilities is not None else None
    problems: list[str] = []
    seen: set[str] = set()
    for record in INTENTIONAL_CHANGES:
        if record.key in seen:
            problems.append(f"{record.key}: duplicate key")
        seen.add(record.key)
        for field_name in (
            "summary",
            "current_behavior",
            "target_behavior",
            "rationale",
        ):
            if not getattr(record, field_name).strip():
                problems.append(f"{record.key}: empty {field_name}")
        if not record.pinned_coverage:
            problems.append(
                f"{record.key}: names no pinned coverage, so a later session "
                "cannot tell which failing test is expected"
            )
        for reference in record.pinned_coverage:
            alias, separator, path = reference.partition(":")
            if not separator or not path or not alias:
                problems.append(
                    f"{record.key}: pinned coverage {reference!r} is not an "
                    "'alias:path' reference"
                )
        if not record.capability_keys:
            problems.append(f"{record.key}: names no affected capability")
        if capabilities is not None:
            for capability_key in record.capability_keys:
                if capability_key not in capabilities:
                    problems.append(
                        f"{record.key}: names unknown capability {capability_key!r}"
                    )
    return tuple(problems)
