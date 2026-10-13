"""Mailbox and guidance projections embedded in status payloads."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import plans_errors, plans_progress

#: How many guidance records the status projection describes. Status is a
#: bounded snapshot, not a mailbox reader: a long run's ledger can hold
#: hundreds of records and the operator question this answers -- "did what I
#: just said get through?" -- is always about the recent end.
MAILBOX_STATUS_GUIDANCE_CAP = 20


def _guidance_delivery_projection(view: Any) -> list[dict[str, Any]]:
    """Describe what became of the most recent guidance, one entry each.

    The projection exists so an operator does not have to infer delivery from
    counts. ``state`` is the record's current standing, and ``latest_attempt``
    is the evidence behind it: which turn, which harness, which channel, and
    what the transport said. A record nothing has ever attempted has no latest
    attempt at all, which is exactly the shape of ``queued``.

    Bodies never appear here. ``asl status`` is read by anyone who can see the
    run, while guidance is addressed to its worker; the record's id and seq are
    enough to look it up through ``asl inbox``.
    """
    from . import (
        mailbox_vocabulary,
    )

    entries: list[dict[str, Any]] = []
    guidance = [
        message
        for message in view.messages
        if message.kind == mailbox_vocabulary.KIND_GUIDANCE
    ]
    for message in guidance[-MAILBOX_STATUS_GUIDANCE_CAP:]:
        latest = view.latest_guidance_delivery(message.id)
        entries.append(
            {
                "id": message.id,
                "seq": message.seq,
                "created_at": message.created_at,
                "state": view.guidance_delivery_state(message.id),
                "latest_attempt": (
                    None
                    if latest is None
                    else {
                        "delivery_id": latest.id,
                        "seq": latest.seq,
                        "delivery_state": latest.delivery_state,
                        "reason": latest.delivery_reason,
                        "channel": latest.channel,
                        "harness": latest.harness,
                        "run_id": latest.run_id,
                        "session_id": latest.session_id,
                        "turn_index": latest.turn_index,
                        "recorded_at": latest.created_at,
                    }
                ),
            }
        )
    return entries


def _guidance_state_summary(entry: Mapping[str, Any]) -> str:
    """Render one projection entry the way the mailbox vocabulary defines it."""
    from . import (
        mailbox_vocabulary,
    )

    attempt = entry.get("latest_attempt")
    attempt = attempt if isinstance(attempt, dict) else {}
    return mailbox_vocabulary.describe_guidance_delivery(
        str(entry.get("state")),
        delivery_reason=attempt.get("reason"),
        harness=attempt.get("harness"),
    )


def _mailbox_status_payload(
    plan_dir: Path,
    state_dir: str | Path | None,
    active_run_id: str | None = None,
) -> dict[str, Any]:
    """Project the append-only mailbox and lease without mutating either file."""
    # Local import avoids a module cycle: mailbox path resolution depends on
    # this module's state-root helpers, while status only needs read-only views.
    from . import (
        mailbox,
        mailbox_constants,
        mailbox_cursors,
        mailbox_leases,
        mailbox_models,
    )

    mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
    lease_path = mailbox_cursors.lease_path_for_plan(plan_dir, state_dir)
    mailbox_present = mailbox_path.exists()
    lease_present = lease_path.exists()
    payload: dict[str, Any] = {
        "present": mailbox_present,
        "active": False,
        "path": str(mailbox_path),
        "lease_present": lease_present,
        "lease_path": str(lease_path),
        "size_bytes": 0,
        "max_bytes": mailbox_constants.MAILBOX_MAX_BYTES,
        "full": False,
        "accepts_questions": True,
        "accepts_replies": True,
        "accepts_resolutions": True,
        "last_seq": 0,
        "pending_guidance": 0,
        "guidance": [],
        "unanswered_questions": 0,
        "open_questions": 0,
        "resolutions": 0,
        "notices": 0,
        "supervision_ends": 0,
        "rejected_records": 0,
        "error": None,
        "lease_updated_at": None,
        "lease_fresh": False,
        "lease_reason": None,
        "lease_error": None,
    }

    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
    except mailbox_models.MailboxError as error:
        # read_mailbox only returns an absent view for FileNotFoundError, so an
        # exception means something exists at the path but could not be read.
        payload["present"] = True
        payload["size_bytes"] = None
        payload["full"] = None
        payload["accepts_questions"] = None
        payload["accepts_replies"] = None
        payload["accepts_resolutions"] = None
        payload["last_seq"] = None
        payload["pending_guidance"] = None
        payload["guidance"] = None
        payload["unanswered_questions"] = None
        payload["open_questions"] = None
        payload["resolutions"] = None
        payload["notices"] = None
        payload["supervision_ends"] = None
        payload["rejected_records"] = None
        payload["error"] = str(error)
    else:
        counts = view.counts
        payload.update(
            {
                "present": (
                    mailbox_present or mailbox_path.exists() or view.size_bytes > 0
                ),
                "size_bytes": view.size_bytes,
                "full": view.full,
                "accepts_questions": view.accepts_questions,
                "accepts_replies": view.accepts_replies,
                "accepts_resolutions": view.accepts_resolutions,
                "last_seq": view.last_seq,
                "pending_guidance": counts.pending_guidance,
                "guidance": _guidance_delivery_projection(view),
                "unanswered_questions": counts.unanswered_questions,
                "open_questions": len(view.open_questions(active_run_id)),
                "resolutions": counts.resolutions,
                "notices": counts.notices,
                "supervision_ends": counts.supervision_ends,
                "rejected_records": counts.rejected,
            }
        )
        if view.rejected:
            first = view.rejected[0]
            noun = "record" if len(view.rejected) == 1 else "records"
            payload["error"] = (
                f"{len(view.rejected)} rejected mailbox {noun}; "
                f"first at seq {first.seq}: {first.reason}"
            )

    try:
        lease = mailbox_leases.read_lease(plan_dir, state_dir=state_dir)
    except mailbox_models.MailboxError as error:
        payload["lease_present"] = True
        payload["lease_reason"] = error.reason
        payload["lease_error"] = str(error)
    else:
        lease_present = lease.reason != mailbox_constants.LEASE_MISSING
        payload["lease_present"] = lease_present
        payload["lease_updated_at"] = lease.updated_at
        payload["lease_fresh"] = lease.fresh
        payload["lease_reason"] = (
            None if lease.reason == mailbox_constants.LEASE_MISSING else lease.reason
        )
        if lease.reason not in (
            None,
            mailbox_constants.LEASE_MISSING,
            mailbox_constants.LEASE_EXPIRED,
        ):
            payload["lease_error"] = lease.reason

    payload["active"] = bool(payload["present"] or payload["lease_fresh"])
    return payload


def _progress_journal_payload(plan_dir: Path) -> dict[str, Any]:
    """Summarize the runner-managed `progress.md` journal for `asl status`.

    Shape stays stable across the three states a journal can be in so JSON
    consumers can branch cleanly: absent (`present=False`), readable
    (`entry_count`/`last_appended` populated), or present-but-malformed
    (`error` set, counts None).
    """
    journal_path = plans_progress.progress_path(plan_dir)
    if not journal_path.exists():
        return {
            "present": False,
            "path": str(journal_path),
            "entry_count": None,
            "last_appended": None,
            "error": None,
        }
    try:
        entries = plans_progress.read_progress_entries(plan_dir)
    except plans_errors.PlanError as error:
        return {
            "present": True,
            "path": str(journal_path),
            "entry_count": None,
            "last_appended": None,
            "error": str(error),
        }
    last_appended = entries[-1].completed_at if entries else None
    return {
        "present": True,
        "path": str(journal_path),
        "entry_count": len(entries),
        "last_appended": last_appended,
        "error": None,
    }
