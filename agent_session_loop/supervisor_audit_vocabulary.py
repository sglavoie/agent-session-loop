"""Event sets, result names and artifact names the deterministic audit checks share."""

from __future__ import annotations

import re

from . import events, state
from . import supervisor_audit_evidence_constants as audit_evidence_constants

#: Historical artifact name, retained when interpreting old audit evidence.
MUTATION_ARTIFACT_NAME = "supervisor-audit-mutation.json"


WORKSPACE_CHANGE_ARTIFACT_NAME = "supervisor-audit-workspace-change.json"


#: The locator every check falls back to. ``collect_audit_input`` registers the
#: run directory first, so this is always in the evidence index, and a result
#: that read nothing else still says where it looked.
RUN_ANCHOR = audit_evidence_constants.EVIDENCE_ROOT_RUN


#: Event names a deterministic check reads. A record that reports a failure
#: under any other name lands in the unknown-anomalies lane rather than going
#: unread, which is what keeps the rubric's last lane honest.
_CLASSIFIED_EVENT_NAMES = frozenset(
    {
        audit_evidence_constants._RUN_STARTED_EVENT,
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
