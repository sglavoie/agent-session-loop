"""Everything one turn is selected and switched by, before it is launched.

A turn is not just a prompt handed to a process. Before the run loop can spend
one it has to settle a series of independent questions: which model and effort
this particular turn asks its harness for, whether Spark routes this session or
the baseline does, whether the requested harness can even start, what the
ambient process environment says, whether approval is bypassed, how commits are
scoped, and whether a supervisor's mailbox is live enough to carry guidance
into the prompt. Each answer is small; together they were spread across three
hundred lines of the run loop, where a reader could not tell which of them were
policy and which were plumbing.

This module owns all of them, so the loop is left deciding *when* to select
rather than *how* selection stays honest. Nothing here launches anything,
polls anything, or waits: every function is a projection of the parsed
arguments, the environment, and the plan directory into a value the loop then
acts on.

One durable record is written here, and only one. Guidance carried into a turn
by its prompt is receipted through
:func:`_record_next_turn_guidance_delivery`, which appends to the mailbox's own
guidance-delivery ledger and then asks the caller to append the matching event.
It never reaches the run's serialized state: the event append arrives as a
:data:`GuidanceEventAppender` callback, so this module never sees the run's
persistence handle and never imports the runner that owns it.

Six invariants survive the move and are load-bearing:

* **Resolution is not forwarding.** :func:`_requested_model` returns the *raw*
  request, which is `None` when the CLI named no model, while
  :func:`_selected_model_effort_payload` resolves that same absence into the
  harness-native configured or default value for reporting. A model omitted on
  the command line must be represented in a durable payload and must not be
  forwarded to the harness subprocess, and keeping the two functions distinct
  is what stops a reporting default from becoming an argument.
* **A switched harness starts native.** A live implementation-harness switch
  deliberately does not inherit the declared implementation model: once the
  harness differs from ``args.harness`` and no override names a model, the
  request is `None` so the new harness starts at its own default.
* **Spark precedence is ordered and first-cause wins.** :func:`_spark_turn_route`
  evaluates its suppressions in a fixed order — review turn, disabled, live
  override, non-codex harness, generated review repair, run-scoped suppression,
  unavailable model, ineligible session — and routes to Spark only when every
  one of them passes. The route it returns always names the cause, so a
  baseline decision is never unexplained.
* **Eligibility fails to baseline.** :func:`_session_spark_suitability` reads a
  preparation marker that may be missing, stale, or unreadable, and every one
  of those cases returns ineligible with a reason rather than raising. Spark is
  an optimization; nothing about it may block a run.
* **Mailbox activation is never fatal.** :func:`_mailbox_activation_payload`
  and :func:`_pending_guidance_snapshot` degrade an unreadable lease or an
  unreadable mailbox into an inactive state and an empty snapshot, recording
  the reason. A supervisor's channel failing is an ordinary paused-run flow,
  not an exception out of turn setup.
* **Validation and projection are separate.** :func:`_commit_mode` raises
  :class:`~agent_session_loop.run_errors.RunError` on a mode it does not
  recognize, because a run launched under a mode nothing implements is a run
  that would commit wrongly; :func:`_commit_mode_payload` degrades the same
  input to `None`, because a durable payload describing a rejected run must
  still be writable.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    config,
    events,
    harnesses,
    mailbox,
    model_availability,
    plans,
    prepare,
    review_repair,
    state,
)
from .run_errors import RunError

#: The Codex model Spark routes eligible implementation turns to. Every other
#: model in a run is resolved from the CLI or the harness configuration; this
#: one is named here because Spark is a policy about a specific model rather
#: than about whatever the operator asked for.
SPARK_MODEL = "gpt-5.3-codex-spark"

#: Commit scoping. ``per-session`` commits each accepted session as it lands,
#: ``end`` defers to one commit after the last one, and a generated repair
#: child always owns its own commit under ``per-child`` regardless of the run's
#: mode -- which is why the repair constant is not a member of ``COMMIT_MODES``.
COMMIT_MODE_PER_SESSION = "per-session"
COMMIT_MODE_END = "end"
COMMIT_MODES = (COMMIT_MODE_PER_SESSION, COMMIT_MODE_END)
REPAIR_COMMIT_MODE_PER_CHILD = "per-child"

#: How a guidance-delivery receipt reaches the run's durable event log.
#:
#: Declared as a callback rather than as the run's persistence handle so this
#: module never depends on the type the run loop owns. The run serializes its
#: own event appends behind its lock; this module supplies the path, the event
#: name, and the payload, and nothing else.
GuidanceEventAppender = Callable[[Path, str, dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Model and effort selection
# ---------------------------------------------------------------------------


def _requested_model(
    args: argparse.Namespace,
    *,
    harness: str,
    is_review_turn: bool = False,
    implementation_overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> str | None:
    """Return the raw model request appropriate for one harness turn.

    Resolution for reporting is intentionally separate: a model omitted from
    the CLI is still represented by its harness-native configured/default
    value, but must not be forwarded to the harness subprocess. A live
    implementation-harness switch also deliberately starts at that switched
    harness's native default instead of inheriting the declared implementation
    model.
    """
    if is_review_turn:
        return getattr(args, "review_harness_model", None)
    override = (implementation_overrides or {}).get(harness, {}).get("model")
    if override is not None:
        return override
    if harness != args.harness:
        return None
    return getattr(args, "harness_model", None)


def _requested_effort(
    args: argparse.Namespace,
    *,
    harness: str,
    is_review_turn: bool = False,
    implementation_overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> str | None:
    """Return the raw effort request appropriate for one harness turn."""
    if not is_review_turn:
        override = (implementation_overrides or {}).get(harness, {}).get("effort")
        if override is not None:
            return override
    return getattr(args, "effort", None)


def _selected_model_effort_payload(
    args: argparse.Namespace,
    *,
    harness: str | None = None,
    is_review_turn: bool = False,
    implementation_overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, str | None]:
    selected_harness = args.harness if harness is None else harness
    selected = config.model_effort_payload(
        selected_harness,
        model=_requested_model(
            args,
            harness=selected_harness,
            is_review_turn=is_review_turn,
            implementation_overrides=implementation_overrides,
        ),
        effort=_requested_effort(
            args,
            harness=selected_harness,
            is_review_turn=is_review_turn,
            implementation_overrides=implementation_overrides,
        ),
    )
    if not is_review_turn:
        harness_overrides = (implementation_overrides or {}).get(selected_harness, {})
        if "model" in harness_overrides:
            selected["model_source"] = config.MODEL_SOURCE_CONTROL
        if "effort" in harness_overrides:
            selected["effort_source"] = config.EFFORT_SOURCE_CONTROL
    return selected


def _selected_future_harness(
    args: argparse.Namespace,
    control_payload: dict[str, Any],
) -> str:
    selected = control_payload.get("next_harness") or args.harness
    if selected not in harnesses.HARNESS_NAMES:
        raise RunError(f"unsupported selected harness: {selected}")
    return str(selected)


# ---------------------------------------------------------------------------
# Spark routing
# ---------------------------------------------------------------------------


def _spark_baseline_payload(args: argparse.Namespace) -> dict[str, str]:
    """Return the resolved Codex baseline used when Spark is enabled."""
    return config.selected_model_payload("codex", getattr(args, "harness_model", None))


def _initial_spark_payload(args: argparse.Namespace) -> dict[str, Any]:
    enabled = bool(getattr(args, "spark", False))
    baseline = _spark_baseline_payload(args)
    if not enabled:
        return {
            "enabled": False,
            "model": SPARK_MODEL,
            "spark_model": SPARK_MODEL,
            "baseline": baseline["model"],
            "baseline_model": baseline["model"],
            "baseline_source": baseline["model_source"],
            "available": False,
            "availability": False,
            "reason": "disabled",
            "retry_at": None,
            "warning": None,
            "active_route": None,
        }
    availability = model_availability.availability(
        state.state_root(getattr(args, "state_dir", None)), SPARK_MODEL
    )
    return {
        "enabled": True,
        "model": SPARK_MODEL,
        "spark_model": SPARK_MODEL,
        "baseline": baseline["model"],
        "baseline_model": baseline["model"],
        "baseline_source": baseline["model_source"],
        **availability.to_dict(),
        "active_route": None,
    }


def _safe_initial_spark_payload(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return _initial_spark_payload(args)
    except OSError as error:
        baseline = _spark_baseline_payload(args)
        return {
            "enabled": bool(getattr(args, "spark", False)),
            "model": SPARK_MODEL,
            "spark_model": SPARK_MODEL,
            "baseline": baseline["model"],
            "baseline_model": baseline["model"],
            "baseline_source": baseline["model_source"],
            "available": False,
            "availability": False,
            "reason": "ledger-unreadable",
            "retry_at": None,
            "warning": f"model availability ledger is unreadable: {error}",
            "active_route": None,
        }


def _session_spark_suitability(
    plan_dir: Path,
    session_id: str,
) -> tuple[bool, str]:
    """Load one conservative preparation marker without blocking a run."""
    try:
        validation = prepare.load_prepare_validation(plan_dir)
    except (OSError, plans.PlanError, ValueError) as error:
        return False, f"Spark eligibility metadata unreadable: {error}"
    if validation is None:
        return False, "Spark eligibility metadata missing; using baseline"
    session = validation.session(session_id)
    if session is None:
        return False, "Spark eligibility marker missing for session; using baseline"
    return session.spark.eligible, session.spark.reason


def _spark_turn_route(
    args: argparse.Namespace,
    *,
    plan_dir: Path,
    session_path: Path,
    session_id: str,
    harness: str,
    is_review_turn: bool,
    implementation_overrides: Mapping[str, Mapping[str, str]],
    spark_state: Mapping[str, Any],
    run_suppression: str | None,
    fallback_reason: str | None = None,
) -> tuple[str | None, dict[str, str | None], dict[str, Any]]:
    """Select one implementation model according to Spark policy precedence."""
    eligible, eligibility_reason = _session_spark_suitability(plan_dir, session_id)
    baseline_model = _requested_model(
        args,
        harness=harness,
        is_review_turn=is_review_turn,
        implementation_overrides=implementation_overrides,
    )
    selected = _selected_model_effort_payload(
        args,
        harness=harness,
        is_review_turn=is_review_turn,
        implementation_overrides=implementation_overrides,
    )
    suppression: str | None = None
    decision = "baseline"

    if is_review_turn:
        suppression = "review-or-audit"
    elif not bool(getattr(args, "spark", False)):
        suppression = "spark-disabled"
    elif implementation_overrides.get(harness):
        suppression = "live-set-model-override"
    elif harness != "codex":
        suppression = "non-codex-harness"
    elif review_repair.is_generated_review_repair_session_file(session_path):
        suppression = "generated-final-review-repair"
    elif run_suppression is not None:
        suppression = run_suppression
    elif not bool(spark_state.get("available")):
        suppression = str(spark_state.get("reason") or "spark-unavailable")
    elif not eligible:
        suppression = "session-ineligible"
    else:
        decision = "spark"
        baseline_model = SPARK_MODEL
        selected["model"] = SPARK_MODEL
        selected["model_source"] = config.MODEL_SOURCE_SPARK

    route = {
        "eligible": eligible,
        "reason": eligibility_reason,
        "eligibility_reason": eligibility_reason,
        "decision": decision,
        "selected": decision,
        "suppression_cause": suppression,
        "fallback_reason": fallback_reason,
    }
    return baseline_model, selected, route


# ---------------------------------------------------------------------------
# Harness configuration and preflight
# ---------------------------------------------------------------------------


def _safe_harness_config_payload() -> dict[str, Any]:
    try:
        return config.harness_config_payload()
    except config.ConfigError as error:
        config_path = config.default_config_path()
        return {
            "path": str(config_path),
            "present": config_path.exists(),
            "configured_harnesses": [],
            "error": str(error),
        }


def _harness_preflight_failure(
    requested_harness: str,
    *,
    session_id: str,
) -> tuple[str, dict[str, Any]] | None:
    config_path = config.default_config_path()
    details: dict[str, Any] = {
        **plans.session_identity_payload(session_id),
        "session_file": plans.session_id_to_filename(session_id),
        "requested_harness": requested_harness,
        "config_path": str(config_path),
    }

    try:
        adapter = harnesses.get_adapter(requested_harness)
    except (config.ConfigError, harnesses.HarnessError) as error:
        details.update(
            {
                "executable": None,
                "resolved_executable": None,
                "error": str(error),
            }
        )
        return str(error), details

    details["executable"] = adapter.executable
    try:
        executable_path = adapter.resolve_executable()
    except OSError as error:
        details.update(
            {
                "resolved_executable": None,
                "error": str(error),
            }
        )
        return str(error), details

    if executable_path is None:
        message = (
            f"{requested_harness} executable not found on PATH: {adapter.executable}"
        )
        details.update(
            {
                "resolved_executable": None,
                "error": message,
            }
        )
        return message, details

    details["resolved_executable"] = executable_path
    return None


# ---------------------------------------------------------------------------
# Environment knobs
# ---------------------------------------------------------------------------


def _positive_int_env(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def _kitty_window_id_from_env() -> int | None:
    return _positive_int_env("KITTY_WINDOW_ID")


# ---------------------------------------------------------------------------
# Run switches
# ---------------------------------------------------------------------------


def _unsafe_bypass(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "unsafe_bypass", True))


def _no_mailbox(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "no_mailbox", False))


def _approval_mode(args: argparse.Namespace) -> str:
    return "unsafe-bypass" if _unsafe_bypass(args) else "harness-default"


def _commit_mode(args: argparse.Namespace) -> str | None:
    value = getattr(args, "commit_mode", None)
    if value is None or value in COMMIT_MODES:
        return value
    raise RunError("commit_mode must be None, 'per-session', or 'end'")


def _commit_mode_payload(args: argparse.Namespace) -> str | None:
    value = getattr(args, "commit_mode", None)
    return value if value in COMMIT_MODES else None


# ---------------------------------------------------------------------------
# Mailbox activation
# ---------------------------------------------------------------------------


def _mailbox_activation_payload(
    args: argparse.Namespace,
    plan_dir: str | Path,
) -> dict[str, Any]:
    """Resolve mailbox activation without making mailbox failures fatal."""
    state_dir_arg = getattr(args, "state_dir", None)
    mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir_arg)
    lease_path = mailbox.lease_path_for_plan(plan_dir, state_dir_arg)
    disabled = _no_mailbox(args)
    mailbox_present = False if disabled else mailbox_path.exists()
    lease_fresh = False
    lease_reason: str | None = None
    error: str | None = None

    if not disabled:
        try:
            lease = mailbox.read_lease(plan_dir, state_dir=state_dir_arg)
        except (OSError, mailbox.MailboxError) as exc:
            lease_reason = (
                exc.reason
                if isinstance(exc, mailbox.MailboxError)
                else mailbox.ERROR_LEASE_READ
            )
            error = str(exc)
        else:
            lease_fresh = lease.fresh
            lease_reason = lease.reason

    if disabled:
        enabled = False
        reason = "disabled-by-flag"
    elif mailbox_present:
        enabled = True
        reason = "mailbox-present"
    elif lease_fresh:
        enabled = True
        reason = "fresh-lease"
    else:
        enabled = False
        reason = "inactive"

    return {
        "enabled": enabled,
        "reason": reason,
        "disabled_by_flag": disabled,
        "mailbox_path": str(mailbox_path),
        "mailbox_present": mailbox_present,
        "lease_path": str(lease_path),
        "lease_fresh": lease_fresh,
        "lease_reason": lease_reason,
        "error": error,
    }


def _pending_guidance_snapshot(
    *,
    enabled: bool,
    plan_dir: str | Path,
    state_dir_arg: str | Path | None,
) -> tuple[mailbox.MailboxMessage, ...]:
    """Return pending guidance for one prompt, degrading to an empty snapshot."""
    if not enabled:
        return ()
    try:
        return mailbox.pending_guidance(plan_dir, state_dir=state_dir_arg)
    except OSError, mailbox.MailboxError:
        return ()


def _record_next_turn_guidance_delivery(
    *,
    plan_dir: str | Path,
    state_dir_arg: str | Path | None,
    event_path: Path,
    append_event: GuidanceEventAppender,
    pending_guidance: Sequence[mailbox.MailboxMessage],
    harness: str,
    run_id: str,
    session_id: str,
    turn_index: int,
) -> None:
    """Record guidance carried into a turn whose prompt has just been accepted.

    Only for a harness with no same-turn channel at all. There, the prompt is
    not a fallback for a failed steer — it is the delivery mechanism, and the
    process starting with that prompt in hand is the strongest evidence such a
    harness can produce. Recording it is what turns an indefinitely queued
    record into a receipt naming the turn that received it.

    For every other harness this is never called: their ledger already carries
    the live attempt's own verdict, and adding a next-turn receipt beside it
    would claim two deliveries of one record.

    The run's own serialized event append arrives as ``append_event`` rather
    than as the persistence object that owns it, so recording a receipt needs
    nothing from the run loop but a path and a callable.

    Best effort throughout. A ledger that cannot take these records leaves the
    guidance deliverable, which the next turn's prompt will carry again.
    """
    for message in pending_guidance:
        try:
            record = mailbox.append_guidance_delivery(
                plan_dir,
                message.id,
                delivery_state=mailbox.GUIDANCE_STATE_DELIVERED_NEXT_TURN,
                delivery_reason=mailbox.GUIDANCE_REASON_DELIVERED,
                channel=mailbox.GUIDANCE_CHANNEL_NEXT_TURN,
                run_id=run_id,
                session_id=session_id,
                turn_index=turn_index,
                harness=harness,
                state_dir=state_dir_arg,
            )
        except OSError, mailbox.MailboxError:
            return
        append_event(
            event_path,
            events.MAILBOX_EVENT,
            {
                "action": events.MAILBOX_ACTION_GUIDANCE_DELIVERED,
                **mailbox.guidance_delivery_event_payload(record),
            },
        )
