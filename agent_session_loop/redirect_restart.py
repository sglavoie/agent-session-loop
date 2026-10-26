"""Durable restart gates for stopped predecessors and uncertain submissions.

Evidence is private runner state, separate from the body-free public journal.
A cleared predecessor cannot return to life; every uncleared predecessor is
probed again without sending a signal. Missing evidence never grants a pass.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import socket
from pathlib import Path
from typing import Any

from . import (
    durability,
    process_tree,
    redirect_store_delivery,
    redirect_store_journal,
    redirect_store_models,
    state,
    state_locks,
)


def evidence_dir(
    plan_dir: str | Path,
    target: redirect_store_models.RedirectTarget,
    state_dir: str | Path | None = None,
) -> Path:
    # Harness is descriptive metadata, not part of a turn's durable identity.
    identity = json.dumps(
        [target.run_id, target.session_id, target.turn_index]
    ).encode()
    key = hashlib.sha256(identity).hexdigest()
    return state.plan_state_dir(plan_dir, state_dir) / "redirect-predecessors" / key


def write_evidence(
    directory: Path, worker: dict[str, Any], evidence: dict[str, Any]
) -> None:
    # Identity checks need no command lines, which may contain operator text.
    evidence = dict(evidence)
    if isinstance(evidence.get("entries"), list):
        evidence["entries"] = [
            {key: entry.get(key) for key in ("pid", "ppid", "pgid", "birth")}
            if isinstance(entry, dict)
            else entry
            for entry in evidence["entries"]
        ]
    durability.write_json_file(
        directory / "evidence.json",
        {"host": socket.gethostname(), "worker": worker, "evidence": evidence},
        durable=True,
    )


def mark_stopped(directory: Path) -> None:
    durability.write_json_file(
        directory / "stopped.json", {"stopped": True}, durable=True
    )


def predecessor_blocker(directory: Path) -> dict[str, Any] | None:
    """Revalidate the original group and each tracked escaped descendant."""
    try:
        stopped = directory / "stopped.json"
        if stopped.exists() and json.loads(stopped.read_text(encoding="utf-8")) == {
            "stopped": True
        }:
            return None
        payload = json.loads((directory / "evidence.json").read_text(encoding="utf-8"))
        worker = payload["worker"]
        evidence = payload["evidence"]
        if not isinstance(worker, dict) or not isinstance(evidence, dict):
            raise ValueError("invalid predecessor evidence")
        if evidence.get("collected") is not True or evidence.get("disabled"):
            raise ValueError("predecessor evidence was not collected")
        if payload["host"] != socket.gethostname():
            raise ValueError("predecessor belongs to another host")
        pgid = worker["pgid"]
        if (
            type(pgid) is not int
            or pgid <= 1
            or evidence.get("root_pid") != worker["pid"]
        ):
            raise ValueError("invalid predecessor identity")
        entries = evidence.get("entries")
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict)
            or type(entry.get("pid")) is not int
            or entry["pid"] <= 1
            or not isinstance(entry.get("birth"), str)
            or not entry["birth"]
            for entry in entries
        ):
            raise ValueError("descendant identity evidence is missing or malformed")
        if not process_tree.sweep_enabled():
            raise ValueError("process sweep is disabled")
        confirmation = process_tree.confirm_termination(pgid=pgid, evidence=evidence)
        if confirmation["complete"]:
            mark_stopped(directory)
            return None
        return {"evidence_path": str(directory), "confirmation": confirmation}
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        # RecursionError is not a ValueError: an over-deep file must block
        # like any other malformed evidence rather than crash the restart.
        return {"evidence_path": str(directory), "error": str(error)}


def _resolution_path(
    plan_dir: str | Path,
    request: redirect_store_models.RedirectRequest,
    state_dir: str | Path | None,
) -> Path:
    continuation = request.continuation or request.released_from
    identity = [
        request.operation_id,
        continuation.to_payload() if continuation else None,
    ]
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return (
        state.plan_state_dir(plan_dir, state_dir)
        / "redirect-resolutions"
        / f"{key}.json"
    )


def resolution_for(
    plan_dir: str | Path,
    request: redirect_store_models.RedirectRequest,
    state_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    try:
        payload = json.loads(
            _resolution_path(plan_dir, request, state_dir).read_text(encoding="utf-8")
        )
        continuation = request.continuation or request.released_from
        if (
            continuation is not None
            and payload["operation_id"] == request.operation_id
            and payload["continuation"] == continuation.to_payload()
            and payload["outcome"] in ("submitted", "not-submitted")
            and payload["reason"]
        ):
            return payload
    except OSError, ValueError, KeyError, TypeError, RecursionError:
        pass
    return None


def resolve_submission(
    plan_dir: str | Path,
    operation_id: str,
    *,
    outcome: str,
    reason: str,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Record an explicit operator decision while no runner owns the plan.

    Submitted permits continuation without claiming worker acknowledgment.
    Not-submitted releases only a carry whose journal has no delivery evidence.
    The decision is durable before release, so a crash between them is retryable.
    """
    if outcome not in ("submitted", "not-submitted") or not reason.strip():
        raise ValueError("an explicit outcome and reason are required")
    state_locks.acquire_lock(plan_dir, state_dir, owner="redirect-resolution")
    try:
        request = redirect_store_journal.read_journal(
            plan_dir, state_dir=state_dir
        ).request(operation_id)
        if request is None or request.acknowledged:
            raise ValueError("operation has no unresolved submission")
        prior = resolution_for(plan_dir, request, state_dir)
        if prior is not None and prior["outcome"] != outcome:
            raise ValueError("a conflicting decision is already recorded")
        if request.continuation is None:
            if prior is not None and outcome == "not-submitted" and request.released:
                return prior
            raise ValueError("operation has no unresolved submission")
        if outcome == "not-submitted" and request.delivered:
            raise ValueError("recorded delivery cannot be declared unsent")
        if not request.launch_ambiguous and not request.delivered:
            raise ValueError("operation has no uncertain submission")
        payload = prior or {
            "operation_id": operation_id,
            "continuation": request.to_payload()["continuation"],
            "outcome": outcome,
            "reason": reason,
        }
        durability.write_json_file(
            _resolution_path(plan_dir, request, state_dir), payload, durable=True
        )
        if outcome == "not-submitted":
            redirect_store_delivery.release_request(
                plan_dir,
                operation_id,
                continuation=request.continuation,
                state_dir=state_dir,
            )
        return payload
    finally:
        state_locks.release_lock(plan_dir, state_dir)


def restart_blocker(
    plan_dir: str | Path,
    *,
    pending_session_ids: tuple[str, ...],
    state_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    view = redirect_store_journal.read_journal(plan_dir, state_dir=state_dir)
    if view.torn_tail or view.rejected:
        return {
            "reason": "redirect-successor-blocked",
            "error": "redirect journal is incomplete",
        }
    for request in view.requests:
        if not request.accepted:
            continue
        blocked = predecessor_blocker(evidence_dir(plan_dir, request.target, state_dir))
        if blocked is not None:
            return {
                "reason": "redirect-successor-blocked",
                "operation_id": request.operation_id,
                **blocked,
            }
        if request.target.session_id not in pending_session_ids or request.acknowledged:
            continue
        if not request.launch_ambiguous:
            continue
        resolution = resolution_for(plan_dir, request, state_dir)
        if resolution is not None:
            if (
                resolution["outcome"] == "not-submitted"
                and request.continuation is not None
            ):
                redirect_store_delivery.release_request(
                    plan_dir,
                    request.operation_id,
                    continuation=request.continuation,
                    state_dir=state_dir,
                )
            continue
        return {
            "reason": "redirect-submission-unresolved",
            "operation_id": request.operation_id,
            "session_id": request.target.session_id,
            "resolution_command": shlex.join(
                [
                    "asl",
                    "control",
                    "resolve-redirect",
                    str(plan_dir),
                    "--operation-id",
                    request.operation_id,
                    "--outcome",
                    "OUTCOME",
                    "--reason",
                    "EXPLANATION",
                    *(["--state-dir", str(state_dir)] if state_dir is not None else []),
                ]
            ),
            "resolution_outcomes": ["submitted", "not-submitted"],
        }
    return None
