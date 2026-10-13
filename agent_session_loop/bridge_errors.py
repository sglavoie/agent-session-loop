"""Error mapping and request-parameter validation for the bridge.

Service refusals are re-expressed in the protocol's own error vocabulary here,
and request parameters are bounded before a handler sees them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from asl_workbench import controls as control_rules

from . import intervention, workbench_controls
from .bridge_protocol import (
    ERROR_CODES,
    ERROR_INTERNAL,
    ERROR_INVALID_REQUEST,
    ERROR_MUTATION_FAILED,
    MAX_ID_CHARS,
    MAX_REDIRECT_OPERATIONS,
    BridgeError,
    _bounded_path,
    _bounded_text,
)


def _bridge_error(error: intervention.InterventionError) -> BridgeError:
    """Re-raise a service refusal in the protocol's own error vocabulary."""
    return BridgeError(
        error.reason,
        error.message,
        retryable=error.retryable,
        details=error.details,
    )


def _control_details(details: Mapping[str, Any]) -> dict[str, Any]:
    """Bound a refusal's details to values a versioned frame can carry.

    Numbers travel as text because a client renders these rather than computing
    with them, and because the shared service states a bound once for every
    caller: a ceiling reads the same here as it does in a browser, where a raw
    float and a string of one would not.
    """
    bounded: dict[str, Any] = {}
    for key, value in details.items():
        if value is None or isinstance(value, bool):
            bounded[key] = value
        else:
            text = value if isinstance(value, str) else str(value)
            bounded[key] = _bounded_text(text)
    return bounded


def _control_error(error: workbench_controls.ControlRefused) -> BridgeError:
    """Re-raise a shared control refusal in the protocol's own vocabulary.

    The service refuses in terms of what a client should *do* -- rewrite the
    request, retry it, or reconcile an id it already holds -- and this is where
    that becomes one of the protocol's codes. The mapping is deliberately
    lossless in one direction only: a durable reason the closed enum has no name
    for travels in ``details.reason``, so a client keeps the specific answer
    without this surface widening a versioned contract to carry it.
    """
    if isinstance(error, workbench_controls.ControlQueueRefused):
        return BridgeError(
            ERROR_MUTATION_FAILED,
            f"control command could not be queued: {error.queue_error}",
            retryable=True,
        )
    if isinstance(error, workbench_controls.RedirectSubmitFailed):
        # Split on which layer refused rather than on whether the body was at
        # fault: "submitted" and "persisted" are this protocol's two sentences
        # for a correction that did not land, and admission is the only layer
        # that refuses one before the journal is ever opened.
        if error.source == control_rules.SOURCE_ADMISSION:
            return BridgeError(
                ERROR_INVALID_REQUEST,
                error.submitted_message,
                details={"reason": error.reason},
            )
        return BridgeError(
            ERROR_MUTATION_FAILED,
            error.persisted_message,
            retryable=True,
            details=(
                {"reason": error.reason}
                if error.source == control_rules.SOURCE_JOURNAL
                else {}
            ),
        )
    if error.scope == control_rules.SCOPE_UNAVAILABLE:
        # A fact about this machine rather than about the request: an unreadable
        # plan or an unreadable journal. Retryable, and never answered as though
        # the plan simply held nothing.
        return BridgeError(
            ERROR_INTERNAL,
            error.message,
            retryable=True,
            details=_control_details(error.details),
        )
    code = error.reason if error.reason in ERROR_CODES else ERROR_INVALID_REQUEST
    details = dict(error.details)
    if code != error.reason:
        details = {"reason": error.reason, **details}
    if error.plan_dir is not None:
        details = {"plan_dir": _bounded_path(error.plan_dir), **details}
    return BridgeError(
        code,
        error.message,
        retryable=error.retryable,
        details=_control_details(details),
    )


def _require_str(params: Mapping[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise BridgeError(
            ERROR_INVALID_REQUEST, f"params.{key} must be a nonempty string"
        )
    return value


def _optional_str(params: Mapping[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BridgeError(
            ERROR_INVALID_REQUEST, f"params.{key} must be a nonempty string when set"
        )
    return value


def _optional_id_list(params: Mapping[str, Any], key: str) -> frozenset[str]:
    """Read an optional filter of operation ids, bounded like every other list.

    An empty result means *no filter*, not *nothing matched*, which is why an
    explicitly empty array is refused rather than silently read as "all": a
    client that filtered its way down to zero ids and got the whole journal back
    would be told the opposite of what it asked.
    """
    value = params.get(key)
    if value is None:
        return frozenset()
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
        or len(value) > MAX_REDIRECT_OPERATIONS
    ):
        raise BridgeError(
            ERROR_INVALID_REQUEST,
            f"params.{key} must be a nonempty array of at most "
            f"{MAX_REDIRECT_OPERATIONS} strings when set",
        )
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_ID_CHARS:
            raise BridgeError(
                ERROR_INVALID_REQUEST,
                f"params.{key} entries must be nonempty strings of at most "
                f"{MAX_ID_CHARS} characters",
            )
    return frozenset(value)
