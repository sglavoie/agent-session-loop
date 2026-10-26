"""The last thing a run says, on the one path where nothing else can be said.

Every other boundary in the loop writes something durable — a turn record, a
commit, an event, a session file. This one writes nothing at all. It owns the
run's *final emission*: the JSON document or the block of text a caller reads
once the loop has already stopped, plus the error payload a run builds when it
never got far enough to have a loop at all.

Two halves live here, and they meet at :func:`_emit_run_payload`.

* **The payload of last resort.** :func:`_error_payload` builds a complete run
  response for a run that failed or paused before ``run_persistence`` seeded
  one — a bad flag, a locked plan, an unreadable snapshot. It is deliberately
  shaped like the payload :func:`run_payloads._initial_payload` seeds, with the
  same empty lists and the same ``None`` scalars, so the printer below cannot
  tell which of the two it was handed. :func:`_state_error_reason` is the
  classifier that decides whether a :class:`state.StateError` is a transient
  lock contention (a pause) or an unrecoverable state error (a failure).
* **The text voice.** :func:`_print_text_outcome` renders that payload for a
  human on a non-JSON run, with :func:`_print_model_effort_lines`,
  :func:`_print_commit_outcome_lines`, :func:`_commit_scope_label` and
  :func:`_last_commit` as its parts. :func:`_emit_run_payload` is the single
  exit: it attaches the interactive-help block and then routes to
  ``responses.print_json`` or to the printer, depending on ``--json``.

Durable records this boundary writes: **none**. Both halves are terminal —
:func:`_error_payload` returns a dictionary the caller persists, and the
printers only write to ``sys.stdout``/``sys.stderr``.
``tests/test_asl_run_outcome.py`` pins that as a flat prohibition, so a first
writer appearing here is a test failure rather than a review catch.

Nothing here imports the runner, and this boundary needs no Protocol seam of
its own — the fifth extraction in this program that needs none, after
``external_write_evidence``, ``workspace_evidence``, ``handoff_correction`` and
``run_payloads``. The parsed arguments and the payload enter as parameters, and
every collaborator is read at its true owner: the commit-mode, model/effort and
spark blocks at ``run_selection``, the review output path at
``review_session_contract``, the pause classification at ``pause_policy``, the
snapshot projection at ``plans_payloads``, the note lines at
``run_reporting``, the audit status line at ``supervisor_audit``, the assurance
report lines at ``assurance_report``, the bounded repository rows at
``workspaces``, and the interactive-help attachment at ``task_packet_help``.

Invariants that survive the move:

* **The final emission never kills the run.** :func:`_emit_run_payload`
  swallows ``BrokenPipeError`` and ``OSError`` from the write. A recovered run
  whose original pipe reader has exited must not crash on the way out, because
  the same payload is already durable in ``state.json`` and ``result.json``;
  the terminal is the only copy that was ever expendable.
* **The printer reads the seeded keys directly and the ``.get`` keys through
  ``.get``.** ``payload["plan_dir"]``, ``payload["ok"]``,
  ``payload["turns"]``, ``payload["completed_sessions"]`` and
  ``payload["final_snapshot"]`` are indexed without a presence check because
  ``run_payloads._initial_payload`` seeds every one of them and
  :func:`_error_payload` below passes every one of them. A field this printer
  must reach through ``.get`` is a field to seed there, not a reason to make
  this reader defensive.
* **A dry run returns early.** The ``--dry-run`` branch prints the preview and
  returns before the completed-session, commit and failure sections, so a
  preview can never render as an outcome.
* **stderr carries the failure, stdout carries the result.** The pause and
  failure sections, the turn log paths and the commit log paths go to
  ``sys.stderr``; everything else goes to ``sys.stdout``. This module is one of
  the three places in the package that resolve ``sys.stderr`` outside
  ``run_reporting``'s coordinator, and it is allowed to because it runs only
  after the run has ended, when no coordinator exists to race.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    assurance_report,
    events,
    harnesses,
    pause_policy,
    plans_errors,
    plans_files,
    plans_metadata,
    plans_models,
    plans_payloads,
    plans_registry,
    responses,
    review_session_contract,
    run_reporting,
    run_selection,
    state,
    supervisor_audit,
    task_packet_help,
    workspaces,
)


def _state_error_reason(error: state.StateError) -> tuple[str, bool]:
    message = str(error).lower()
    if "plan is locked" in message or "could not acquire lock" in message:
        return "lock-contention", True
    return "state-error", False


def _error_payload(
    args: argparse.Namespace,
    *,
    reason: str,
    message: str,
    paused: bool,
    cwd: Path | None = None,
    snapshot: plans_models.PlanSnapshot | None = None,
) -> dict[str, Any]:
    try:
        plan_dir = str(
            snapshot.plan_dir
            if snapshot is not None
            else plans_files.canonicalize_path(args.plan_dir)
        )
    except OSError:
        plan_dir = str(args.plan_dir)

    review_harness = getattr(args, "review_harness", None)
    review_requested = review_harness is not None
    payload = responses.error_payload(
        "run",
        reason=reason,
        error=message,
        paused=paused,
        details={},
        dry_run=getattr(args, "dry_run", False),
        pause_reason=reason if paused else None,
        pause_message=message if paused else "",
        pause_details={},
        commit_mode=run_selection._commit_mode_payload(args),
        note=getattr(args, "note", None),
        commits=[],
        commit_groups=[],
        plan_dir=plan_dir,
        cwd=str(cwd) if cwd is not None else str(getattr(args, "cwd", "")),
        harness=getattr(args, "harness", None),
        completed_sessions=[],
        completed_session_ids=[],
        turns=[],
        review_requested=review_requested,
        review_harness=review_harness,
        review_session_id=None,
        review_output_path=(
            str(review_session_contract.final_review_output_path(plan_dir))
            if review_requested
            else None
        ),
        review_completed=False,
        review_verdict=None,
        review_skipped_reason=reason if review_requested else None,
    )
    payload.update(run_selection._selected_model_effort_payload(args))
    payload["spark"] = run_selection._safe_initial_spark_payload(args)
    if paused:
        # Match `run_termination.set_pause`: a paused error payload carries the same
        # single-source-of-truth pause classification block.
        payload["pause_policy"] = pause_policy.classify_pause(reason)
    if snapshot is not None:
        state_dir = state.plan_state_dir(snapshot.plan_dir, args.state_dir)
        payload["state_dir"] = str(state_dir)
        payload["state_path"] = str(state_dir / events.STATE_FILE_NAME)
        payload["event_path"] = str(state_dir / events.EVENTS_FILE_NAME)
        payload["events_path"] = str(state_dir / events.EVENTS_FILE_NAME)
        payload["lock_path"] = str(
            state.lock_path_for_plan(snapshot.plan_dir, args.state_dir)
        )
        payload["initial_snapshot"] = plans_payloads.snapshot_payload(snapshot)
        payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot)
        try:
            registry = plans_registry.read_repository_registry(snapshot.plan_dir)
            metadata = plans_metadata.load_plan_metadata(snapshot.plan_dir)
        except plans_errors.PlanError:
            registry = None
            metadata = None
        if registry is not None and isinstance(
            metadata, plans_metadata.WorkspacePlanMetadata
        ):
            workspace_root = plans_files.canonicalize_path(metadata.workspace_root)
            payload.update(
                {
                    "cwd": str(workspace_root),
                    "workspace_root": str(workspace_root),
                    "registry_digest": metadata.registry_digest,
                    "repositories": [
                        {
                            "alias": repository.alias,
                            "path": repository.path,
                            "origin": repository.origin,
                            "resolved_root": str(
                                plans_files.canonicalize_path(
                                    workspace_root / repository.path
                                )
                            ),
                        }
                        for repository in registry.repositories
                    ],
                }
            )
    return payload


def _print_model_effort_lines(payload: dict[str, Any]) -> None:
    model = payload.get("model")
    model_source = payload.get("model_source")
    if model is not None and model_source is not None:
        print(f"Model: {model} ({model_source})")

    effort = payload.get("effort")
    if effort is not None:
        effort_source = payload.get("effort_source")
        if effort_source is None:
            print(f"Effort: {effort}")
        else:
            print(f"Effort: {effort} ({effort_source})")


def _commit_scope_label(commit: dict[str, Any]) -> str:
    scope = commit.get("scope")
    session = commit.get("session_id") or commit.get("session")
    if scope == "session" and session is not None:
        label = f"session {session}"
    elif scope == "end":
        label = "end"
    else:
        label = str(scope or "unknown")
    alias = commit.get("repository_alias") or commit.get("repo")
    return f"{label}, repository {alias}" if alias else label


def _print_commit_outcome_lines(payload: dict[str, Any]) -> None:
    commits = payload.get("commits")
    if not isinstance(commits, list):
        return
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        label = _commit_scope_label(commit)
        index = commit.get("commit_index", "?")
        if commit.get("success"):
            sha = commit.get("commit_sha") or "unknown"
            print(f"Created commit {sha} ({label})")
        else:
            status = commit.get("status_result", "unknown")
            print(f"Commit {index} failed ({label}): {status}")


def _last_commit(payload: dict[str, Any]) -> dict[str, Any] | None:
    commits = payload.get("commits")
    if not isinstance(commits, list) or not commits:
        return None
    commit = commits[-1]
    return commit if isinstance(commit, dict) else None


def _print_text_outcome(payload: dict[str, Any]) -> None:
    print(f"Plan: {payload['plan_dir']}")
    workspace_root = payload.get("workspace_root")
    if workspace_root is not None:
        print(f"Workspace root: {workspace_root}")
        repositories = payload.get("repositories")
        if isinstance(repositories, list):
            visible, omitted = workspaces.bounded_repository_rows(repositories)
            aliases = [
                str(row.get("alias"))
                for row in visible
                if isinstance(row, Mapping) and row.get("alias")
            ]
            if aliases:
                suffix = f" (+{omitted} more)" if omitted else ""
                print(f"Repositories: {', '.join(aliases)}{suffix}")
    if payload.get("harness") is not None:
        print(f"Harness: {payload['harness']}")
        _print_model_effort_lines(payload)
    spark = payload.get("spark")
    if isinstance(spark, Mapping):
        route = spark.get("active_route")
        route_decision = route.get("decision") if isinstance(route, Mapping) else None
        print(
            "Spark: "
            f"{'enabled' if spark.get('enabled') else 'disabled'}; "
            f"route {route_decision or 'none'}; {spark.get('reason', 'unknown')}"
        )
        if spark.get("retry_at"):
            print(f"Spark retry: {spark['retry_at']}")
        if isinstance(route, Mapping) and route.get("fallback_reason"):
            print(f"Spark fallback: {route['fallback_reason']}")
        if isinstance(route, Mapping) and route.get("suppression_cause"):
            print(f"Spark suppression: {route['suppression_cause']}")
        if spark.get("warning"):
            print(f"Spark warning: {spark['warning']}")
    if "commit_mode" in payload:
        print(f"Commit mode: {payload.get('commit_mode') or 'none'}")
    if payload.get("run_dir") is not None:
        print(f"Run dir: {payload['run_dir']}")
    if payload.get("state_path") is not None:
        print(f"State file: {payload['state_path']}")
    if payload.get("events_path") is not None:
        print(f"Events: {payload['events_path']}")
    for line in run_reporting._note_lines(payload, prefix="Note"):
        print(line)

    if payload.get("dry_run") and payload["ok"]:
        planned = payload.get("planned", {})
        planned_audit_line = supervisor_audit.status_line(planned.get("audit"))
        if planned_audit_line:
            print(planned_audit_line)
        next_pending = planned.get("next_pending")
        if next_pending is None:
            print("Next pending: none")
            print("Dry run: nothing to invoke")
            return
        print(f"Next pending: session {next_pending}")
        resolver_prompt = planned.get("resolver_prompt")
        if resolver_prompt:
            print("Resolver prompt:")
            print(resolver_prompt)
        print(f"Prompt: {planned['prompt']}")
        invocation = planned["invocation"]
        print(f"Transport: {invocation.get('transport', harnesses.TRANSPORT_EXEC)}")
        print("Argv:")
        for arg in invocation["argv"]:
            print(f"  {arg}")
        turn_plan = invocation.get("turn_plan")
        if turn_plan is not None:
            # Argv no longer states the access profile under this transport, so
            # a dry run that printed only argv would no longer show the
            # operator what the turn is actually allowed to do.
            print("Turn plan:")
            for line in json.dumps(turn_plan, indent=2, sort_keys=True).splitlines():
                print(f"  {line}")
        return

    audit_line = supervisor_audit.status_line(
        payload.get("assurance"), report=payload.get("assurance_report")
    )
    if audit_line:
        print(audit_line)
    for line in assurance_report.report_lines(payload.get("assurance_report")):
        print(line)

    for session_number in payload["completed_sessions"]:
        print(f"Completed session {session_number}")
    _print_commit_outcome_lines(payload)
    commit_groups_payload = payload.get("commit_groups")
    if isinstance(commit_groups_payload, list):
        unresolved = [
            group
            for group in commit_groups_payload
            if isinstance(group, Mapping) and group.get("status") != "complete"
        ]
        for group in unresolved[:3]:
            successful = group.get("successful_repositories")
            partial = (
                [
                    f"{row.get('alias') or row.get('key')}={row.get('commit_sha')}"
                    for row in successful
                    if isinstance(row, Mapping) and row.get("commit_sha")
                ]
                if isinstance(successful, list)
                else []
            )
            print(
                "Pending commit group: "
                f"failed {group.get('failed_alias') or 'none'}; "
                f"remaining "
                f"{', '.join(group.get('remaining_aliases') or []) or 'none'}; "
                f"partial SHAs {', '.join(partial) or 'none'}"
            )
        if len(unresolved) > 3:
            print(f"Pending commit groups: +{len(unresolved) - 3} more")

    if not payload["ok"]:
        if payload.get("paused"):
            print(f"Run paused: {payload['pause_reason']}", file=sys.stderr)
            if payload.get("pause_message"):
                print(f"Pause detail: {payload['pause_message']}", file=sys.stderr)
            details = payload.get("pause_details") or payload.get("details")
            guard = (
                details.get("workspace_write_guard")
                if isinstance(details, Mapping)
                else None
            )
            if isinstance(guard, Mapping):
                violations = guard.get("violations")
                unavailable = guard.get("unavailable")
                if violations:
                    print(
                        "Workspace scope violations retained; edit durable scope and "
                        "rerun expansion/preflight.",
                        file=sys.stderr,
                    )
                if unavailable:
                    print(
                        "Repository evidence unavailable; repair Git evidence before "
                        "retrying.",
                        file=sys.stderr,
                    )
        else:
            print(f"Run failed: {payload['reason']}", file=sys.stderr)
            error = payload.get("error")
            if error:
                print(f"Error: {error}", file=sys.stderr)
        if payload["turns"]:
            turn = payload["turns"][-1]
            print(f"Stdout: {turn['stdout_path']}", file=sys.stderr)
            print(f"Stderr: {turn['stderr_path']}", file=sys.stderr)
            print(f"Last message: {turn['last_message_path']}", file=sys.stderr)
        interactive_help = payload.get("interactive_help")
        if isinstance(interactive_help, dict):
            questions_command = interactive_help.get("questions_command")
            if questions_command:
                print(f"Questions: {questions_command}", file=sys.stderr)
        commit = _last_commit(payload)
        if commit is not None:
            print(f"Commit stdout: {commit['stdout_path']}", file=sys.stderr)
            print(f"Commit stderr: {commit['stderr_path']}", file=sys.stderr)
            print(f"Commit result: {commit['result_path']}", file=sys.stderr)
        return

    final_snapshot = payload["final_snapshot"]
    next_pending = final_snapshot["next_pending"]
    print(f"Run complete: {len(payload['completed_sessions'])} session(s) completed")
    if next_pending is None:
        print("Next pending: none")
    else:
        print(f"Next pending: session {next_pending}")
    review_output_path = payload.get("review_output_path")
    if payload.get("review_completed") and review_output_path:
        print(f"Review: {review_output_path}")
        review_verdict = payload.get("review_verdict")
        if isinstance(review_verdict, dict):
            status_line = review_verdict.get("status_line")
            if status_line:
                print(status_line)


def _emit_run_payload(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    task_packet_help._attach_interactive_help(args, payload)
    try:
        if args.json:
            responses.print_json(payload)
        else:
            _print_text_outcome(payload)
    except BrokenPipeError, OSError:
        # A recovered run whose original pipe reader exited (helper
        # early-exit) must not crash on its final emission; the same payload
        # is already durable in state.json / result.json.
        pass
