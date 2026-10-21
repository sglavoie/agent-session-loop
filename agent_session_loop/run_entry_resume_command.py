"""Best-effort ``asl run`` command rebuilt from a state document's top-level fields.

When ``asl run --resume`` finds no usable ``run_config``, the refusal carries a
command the operator can run instead of only a reason. This module owns that
reconstruction: :func:`_reconstruct_resume_command` re-emits the flags
recoverable from the fields ``run_payloads._initial_payload`` has always written
at the top level, and :func:`_reconstructed_audit_flags` recovers the audit
flags from the persisted assurance object. It reads a mapping and returns a
string; it touches no file and imports nothing from the rest of ``run_entry``.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any

from . import supervisor_audit


def _reconstruct_resume_command(
    plan_dir: str,
    state_dir_override: str | None,
    payload: dict[str, Any],
) -> str:
    """Best-effort ``asl run`` command from top-level persisted state fields.

    Used only when ``run_config`` is absent (legacy or missing state): it re-emits
    the flags recoverable from the fields ``run_payloads._initial_payload`` has
    always written at the top level, so the operator can resume manually with
    explicit flags.
    """
    argv = ["asl", "run", str(plan_dir)]
    if state_dir_override:
        argv += ["--state-dir", str(state_dir_override)]
    cwd = payload.get("cwd")
    if cwd:
        argv += ["--cwd", str(cwd)]
    harness = payload.get("harness")
    if harness:
        argv += ["--harness", str(harness)]
    review_harness = payload.get("review_harness")
    if review_harness:
        argv += ["--review-harness", str(review_harness)]
    model = payload.get("model")
    if model:
        argv += ["--harness-model", str(model)]
    effort = payload.get("effort")
    if effort:
        argv += ["--effort", str(effort)]
    commit_mode = payload.get("commit_mode")
    if commit_mode == "per-session":
        argv.append("--commit-per-session")
    elif commit_mode == "end":
        argv.append("--commit-at-end")
    max_pending = payload.get("max_pending_sessions")
    if isinstance(max_pending, int):
        argv += ["--max-pending-sessions", str(max_pending)]
    note = payload.get("note")
    if note:
        argv += ["--note", str(note)]
    if payload.get("allow_not_loop_ready") is True:
        argv.append("--allow-not-loop-ready")
    argv += _reconstructed_audit_flags(payload.get("assurance"))
    return shlex.join(argv)


def _reconstructed_audit_flags(assurance: Any) -> list[str]:
    """Recover the audit flags from the assurance object state persisted.

    An inherited role needs no ``--audit-harness``: rebuilding ``--review-harness``
    reproduces it. Its resolved model is re-stated explicitly, which yields the
    same effective role because an explicit audit model wins over an inherited one.
    """
    if not isinstance(assurance, Mapping):
        return []
    argv: list[str] = []
    if assurance.get("state") == "skipped" and (
        assurance.get("reason") == supervisor_audit.REASON_OPTED_OUT
    ):
        return ["--no-supervisor-audit"]
    role = assurance.get("role")
    if not isinstance(role, Mapping):
        return argv
    if role.get("source") == "explicit" and role.get("harness"):
        argv += ["--audit-harness", str(role["harness"])]
    if role.get("model"):
        argv += ["--audit-harness-model", str(role["model"])]
    if role.get("effort"):
        argv += ["--audit-effort", str(role["effort"])]
    timeout = role.get("timeout_seconds")
    if (
        isinstance(timeout, (int, float))
        and int(timeout) != supervisor_audit.DEFAULT_AUDIT_TIMEOUT_SECONDS
    ):
        argv += ["--audit-timeout", str(int(timeout))]
    return argv
