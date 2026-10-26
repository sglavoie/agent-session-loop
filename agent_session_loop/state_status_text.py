"""Human-readable rendering of the ``status`` payload."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import workspaces
from .formatting import format_duration_seconds
from .state_mailbox_status import _guidance_state_summary
from .state_payloads import _session_label_from_payload


def _run_outcome_line(runner_state: dict[str, Any]) -> str | None:
    """How the recorded run ended, or stands paused, as one ``Run state`` line.

    The verb follows ``paused`` first, then ``ok``: a run that ended without
    succeeding and without pausing (an abandoned one, say) stopped. A run that
    has neither finished nor paused prints nothing here; the active-turn and
    staleness lines describe it.
    """
    reason = runner_state.get("reason") or "unknown"
    finished_at = runner_state.get("run_finished_at")
    if runner_state.get("paused"):
        paused_at = runner_state.get("paused_at") or finished_at
        when = f" at {paused_at}" if paused_at else ""
        return f"Run state: paused ({reason}){when}"
    if not finished_at:
        return None
    if runner_state.get("ok"):
        return f"Run state: finished {reason} at {finished_at}"
    return f"Run state: stopped ({reason}) at {finished_at}"


def _print_text_status(payload: dict[str, Any]) -> None:
    print(f"Plan title: {payload['plan_title']}")
    print(f"Plan: {payload['plan_dir']}")
    print(f"Plan hash: {payload['plan_hash']}")
    print(f"Content SHA-256: {payload['content_sha256']}")
    print(f"State dir: {payload['state_dir']}")
    print(f"State file: {payload['state_path']}")
    print(f"Events: {payload['events_path']}")
    recovery = payload.get("external_change_recovery")
    if isinstance(recovery, dict) and recovery.get("preview_command"):
        print(f"External-change recovery preview: {recovery['preview_command']}")
    spark = payload.get("spark")
    if isinstance(spark, dict):
        enabled = "enabled" if spark.get("enabled") else "disabled"
        line = (
            f"Spark: {enabled}; route "
            f"{(spark.get('active_route') or {}).get('decision', 'none')}; "
            f"availability {spark.get('reason', 'unknown')}"
        )
        if spark.get("retry_at"):
            line += f" until {spark['retry_at']}"
        route = spark.get("active_route")
        if isinstance(route, dict) and route.get("fallback_reason"):
            line += f"; fallback {route['fallback_reason']}"
        if isinstance(route, dict) and route.get("suppression_cause"):
            line += f"; suppression {route['suppression_cause']}"
        print(line)
        if spark.get("warning"):
            print(f"Spark warning: {spark['warning']}")
    if payload.get("workspace_root") is not None:
        print(f"Workspace root: {payload['workspace_root']}")
        print(f"Registry digest: {payload.get('registry_digest')}")
        repositories = payload.get("repositories")
        if isinstance(repositories, list):
            print("Repositories:")
            visible, omitted = workspaces.bounded_repository_rows(repositories)
            for repository in visible:
                if isinstance(repository, dict):
                    print(
                        f"  {repository.get('alias')}: "
                        f"{repository.get('resolved_root')}"
                    )
            if omitted:
                print(f"  ... {omitted} more repositories")

    lock = payload["lock"]
    if not lock["present"]:
        print("Lock: none")
    elif lock["active"]:
        print(f"Lock: active pid {lock['pid']} ({lock['path']})")
    else:
        print(f"Lock: stale ({lock['reason']}) ({lock['path']})")

    runner_state = payload.get("runner_state", {})
    if runner_state.get("present") and runner_state.get("error"):
        print(f"Run state: unreadable ({runner_state['error']})")
    elif runner_state.get("present"):
        run_id = runner_state.get("run_id") or "unknown"
        run_started_at = runner_state.get("run_started_at") or "unknown"
        print(f"Run state: run {run_id} started {run_started_at}")
        outcome = _run_outcome_line(runner_state)
        if outcome:
            print(outcome)
        pause_message = runner_state.get("pause_message")
        if runner_state.get("paused") and isinstance(pause_message, str):
            print(f"Pause detail: {pause_message}")
        if runner_state.get("stale_unfinished"):
            stale_reason = runner_state.get("stale_reason") or "stale unfinished"
            print(f"Run state: stale/orphaned ({stale_reason})")
        abandoned = runner_state.get("abandoned")
        if isinstance(abandoned, dict):
            detected_at = abandoned.get("detected_at") or "unknown"
            trigger = abandoned.get("trigger") or "unknown"
            lock_reason = abandoned.get("lock_reason") or "no lock file"
            print(
                f"Run state: abandoned (detected {detected_at} "
                f"by {trigger}: {lock_reason})"
            )
            prior_reason = abandoned.get("prior_reason")
            if prior_reason:
                print(f"Run state: reason before abandonment was {prior_reason}")
        active_finalization = runner_state.get("active_finalization")
        if isinstance(active_finalization, dict):
            phase = active_finalization.get("phase") or "unknown"
            elapsed = active_finalization.get("elapsed_seconds")
            elapsed_text = (
                f"{float(elapsed):.0f}s" if isinstance(elapsed, int | float) else "?"
            )
            hint = (
                "; repeat `asl control abort` to cancel it"
                if active_finalization.get("cancellable")
                else ""
            )
            print(
                f"Run state: finalizing ({phase}, {elapsed_text} elapsed, "
                f"still holds the lock){hint}"
            )

    from . import guards

    command_guards = payload.get("command_guards")
    if isinstance(command_guards, dict) and command_guards.get("active"):
        coverage = command_guards.get("coverage")
        mechanism = (
            coverage.get("mechanism") if isinstance(coverage, dict) else "command gate"
        )
        print(f"Run guards: {command_guards['count']} active ({mechanism})")
        for entry in command_guards["active"]:
            protected = entry.get("protected_path")
            if isinstance(protected, dict):
                rendered = (
                    f"protect {protected.get('qualifier')}:{protected.get('pattern')}"
                )
            else:
                tokens = " ".join(str(token) for token in entry.get("command", ()))
                rendered = f"deny {tokens}"
            lanes = entry.get("coverage")
            if isinstance(lanes, dict):
                # The armed lanes, named. A guard with no armed lane is not a
                # guard, and an operator reading "1 active" has to be able to
                # tell that case from an enforced one without opening the state.
                armed = ", ".join(
                    lane
                    for lane, verdict in sorted(lanes.items())
                    if isinstance(verdict, dict)
                    and verdict.get("status") != guards.COVERAGE_UNSUPPORTED
                )
                rendered += f" [{armed or 'no armed lane'}]"
            print(f"  {entry.get('id')}: {rendered}")

    # Reported, never produced: this prints what the last run's audit already
    # recorded and what the shared inbox already holds.
    from . import assurance_report

    audit_line = assurance_report.status_line(
        payload.get("runner_state", {}).get("assurance"),
        report=payload.get("assurance_report"),
    )
    if audit_line:
        print(audit_line)
    for line in assurance_report.report_lines(payload.get("assurance_report")):
        print(line)

    mailbox_status = payload.get("mailbox")
    if isinstance(mailbox_status, dict):
        mailbox_error = mailbox_status.get("error")
        if mailbox_error:
            print(f"Mailbox: unreadable/malformed ({mailbox_error})")
        elif mailbox_status.get("active"):
            full_suffix = ", full" if mailbox_status.get("full") else ""
            # Non-closure writes stop before the shared closure reserve. Report
            # reply and resolution capacity independently because their minimum
            # valid record sizes differ.
            if not full_suffix and mailbox_status.get("accepts_questions") is False:
                accepts_replies = mailbox_status.get("accepts_replies") is True
                accepts_resolutions = mailbox_status.get("accepts_resolutions") is True
                if accepts_replies and accepts_resolutions:
                    full_suffix = (
                        ", non-closure writes refused "
                        "(replies and resolutions still accepted)"
                    )
                elif accepts_replies:
                    full_suffix = (
                        ", non-closure writes refused (only replies still accepted)"
                    )
                else:
                    full_suffix = (
                        ", non-closure writes refused "
                        "(reply and resolution capacity exhausted)"
                    )
            open_questions = mailbox_status.get("open_questions")
            unanswered = mailbox_status.get("unanswered_questions")
            # Disclose only the historical remainder beyond what the live run
            # can act on, so stale records read as history at a glance.
            history_suffix = (
                f" ({unanswered - open_questions} unanswered historical)"
                if isinstance(open_questions, int)
                and isinstance(unanswered, int)
                and unanswered > open_questions
                else ""
            )
            print(
                "Mailbox: active"
                f"{full_suffix}, seq {mailbox_status.get('last_seq')}, "
                f"{mailbox_status.get('pending_guidance')} pending guidance, "
                f"{open_questions} open questions{history_suffix}, "
                f"{mailbox_status.get('resolutions')} resolutions, "
                f"{mailbox_status.get('notices')} notices, "
                f"{mailbox_status.get('supervision_ends')} supervision ends, "
                f"{mailbox_status.get('size_bytes')}/"
                f"{mailbox_status.get('max_bytes')} bytes"
            )
            latest_guidance = mailbox_status.get("guidance")
            if isinstance(latest_guidance, list) and latest_guidance:
                # Only the last record, and only when there is one: the whole
                # projection is in `--json`, and the line an operator wants at
                # a glance is what became of the thing they just said.
                entry = latest_guidance[-1]
                print(
                    f"Latest guidance: {entry.get('id')} (seq {entry.get('seq')}) "
                    f"{_guidance_state_summary(entry)}"
                )
        elif mailbox_status.get("lease_present"):
            print("Mailbox: inactive (supervisor lease is not fresh)")
        else:
            print("Mailbox: disabled")

        if mailbox_status.get("lease_error"):
            print(
                f"Mailbox lease: malformed/unreadable ({mailbox_status['lease_error']})"
            )
        elif mailbox_status.get("lease_fresh"):
            print(
                "Mailbox lease: fresh, updated "
                f"{mailbox_status.get('lease_updated_at') or 'unknown'}"
            )
        elif mailbox_status.get("lease_present"):
            reason = mailbox_status.get("lease_reason") or "stale"
            updated_at = mailbox_status.get("lease_updated_at") or "unknown"
            print(f"Mailbox lease: stale ({reason}), updated {updated_at}")
        else:
            print("Mailbox lease: absent")

    progress_journal = payload.get("progress_journal")
    if isinstance(progress_journal, dict):
        if not progress_journal.get("present"):
            print("Progress journal: not initialized")
        elif progress_journal.get("error"):
            print(f"Progress journal: unreadable ({progress_journal['error']})")
        else:
            entry_count = progress_journal.get("entry_count") or 0
            last_appended = progress_journal.get("last_appended")
            entry_word = "entry" if entry_count == 1 else "entries"
            if last_appended:
                print(
                    f"Progress journal: {entry_count} {entry_word}, "
                    f"last appended {last_appended}"
                )
            else:
                print(f"Progress journal: {entry_count} {entry_word}")

    active_turn = payload.get("active_turn")
    if isinstance(active_turn, dict):
        print(
            "Active turn: "
            f"session {_session_label_from_payload(active_turn)} "
            f"turn {active_turn.get('turn_index')} "
            f"elapsed {format_duration_seconds(active_turn.get('elapsed_seconds'))} "
            f"idle {format_duration_seconds(active_turn.get('idle_seconds'))}"
        )
        print(f"  Last activity: {active_turn.get('last_activity_at') or 'none'}")
        action_required = active_turn.get("action_required")
        if isinstance(action_required, dict):
            print(
                "  Action required: "
                f"{action_required.get('reason')} "
                f"({action_required.get('source')})"
            )
        print(f"  Stdout: {active_turn.get('stdout_path')}")
        print(f"  Stderr: {active_turn.get('stderr_path')}")
        print(f"  Last message: {active_turn.get('last_message_path')}")
        print(f"  Result: {active_turn.get('result_path')}")

    active_verification = payload.get("active_verification")
    if isinstance(active_verification, dict):
        check_count = active_verification.get("check_count")
        check_index = active_verification.get("current_check_index")
        if check_index is None:
            check_position = f"-/{check_count}" if check_count is not None else "-"
        else:
            check_position = (
                f"{int(check_index) + 1}/{check_count}"
                if check_count is not None
                else str(int(check_index) + 1)
            )
        check_name = active_verification.get("current_check_name") or "pending"
        check_elapsed = format_duration_seconds(
            active_verification.get("check_elapsed_seconds")
        )
        print(
            "Active verification: "
            f"session {_session_label_from_payload(active_verification)} "
            f"turn {active_verification.get('turn_index')} "
            f"check {check_position} {check_name} "
            f"elapsed "
            f"{format_duration_seconds(active_verification.get('elapsed_seconds'))} "
            f"check elapsed {check_elapsed}"
        )
        check_kind = active_verification.get("current_check_kind")
        if check_kind:
            print(f"  Check kind: {check_kind}")
        heartbeat_count = active_verification.get("heartbeat_count")
        if heartbeat_count is not None:
            print(f"  Heartbeats: {heartbeat_count}")

    waiting_for_reply = payload.get("waiting_for_reply")
    if isinstance(waiting_for_reply, dict):
        waiting_status = payload.get("waiting_for_reply_status") or "unknown"
        question_id = waiting_for_reply.get("question_id") or "unknown"
        print(
            "Waiting for reply: "
            f"question {question_id} "
            f"session {_session_label_from_payload(waiting_for_reply)} "
            f"turn {waiting_for_reply.get('turn_index')} "
            f"since {waiting_for_reply.get('started_at') or 'unknown'}"
        )
        base_deadline = waiting_for_reply.get("base_deadline")
        ceiling_deadline = waiting_for_reply.get("unsupervised_deadline_at")
        ceiling_label = "unsupervised"
        if ceiling_deadline is None:
            ceiling_deadline = waiting_for_reply.get("hard_deadline")
            ceiling_label = "hard"
        if base_deadline or ceiling_deadline:
            print(
                "  Deadlines: "
                f"base {base_deadline or 'unknown'}, "
                f"{ceiling_label} {ceiling_deadline or 'unknown'}"
            )
        print(f"  Status: {waiting_status}")

    print("Sessions:")
    for session in payload["sessions"]:
        session_id = session.get("id", session.get("number", "unknown"))
        print(f"  {session_id}: {session['status']} {Path(session['path']).name}")

    if payload["next_pending"] is None:
        print("Next pending: none")
    else:
        next_pending = payload.get("next_pending_id") or payload["next_pending"]
        print(f"Next pending: session {next_pending}")

    next_command = payload.get("next_command")
    if isinstance(next_command, dict):
        if next_command.get("complete"):
            print(f"Next command: {next_command.get('message')}")
        else:
            for step in next_command.get("next_steps", []):
                for harness_name, command in (step.get("invocations") or {}).items():
                    print(f"Next command ({harness_name}): {command}")

    orphaned = payload.get("orphaned_state_candidates")
    if orphaned:
        count = len(orphaned)
        plural = "y" if count == 1 else "ies"
        print(f"Orphaned state: {count} candidate director{plural} from a moved plan")
        for candidate in orphaned:
            print(
                f"  {candidate['state_dir']} "
                f"(old plan {candidate['old_plan_dir']}); "
                f"migrate with: {candidate['migrate_command']}"
            )
