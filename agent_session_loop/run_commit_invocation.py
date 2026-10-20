"""What a run says it ran: the invocation payload and the model projection.

The *invocation payload* is the durable statement of what a turn was launched
as — its harness, its argv or protocol transport and turn plan, the model and
effort it asked for, and the workspace it was allowed to touch. The per-role
*model projection* folds each turn's observation into what the run can
honestly claim it ran: a requested model is the run's own answer, and an
observation may settle only an unresolved request, and only when every turn of
the role agreed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import config, harnesses, run_prompts, verification_models


def _build_invocation_payload(
    invocation: harnesses.HarnessInvocation,
    *,
    model_effort: dict[str, str | None] | None = None,
    verification_context: verification_models.VerificationContext | None = None,
) -> dict[str, Any]:
    if model_effort is None:
        model_effort = config.model_effort_payload(
            invocation.name,
            model=invocation.model,
            effort=invocation.effort,
        )
    workspace_fields = (
        {}
        if verification_context is None
        else run_prompts._workspace_payload_fields(verification_context)
    )
    return {
        "name": invocation.name,
        "argv": invocation.argv,
        # Under a protocol transport ``argv`` names only the binary, so it is
        # no longer a full account of the turn on its own. ``transport`` says
        # which of the two this is, and ``turn_plan`` carries what argv used to:
        # the access profile, the writable roots, the model, and the effort.
        "transport": invocation.transport,
        "turn_plan": (
            invocation.turn_plan.to_payload()
            if invocation.turn_plan is not None
            else None
        ),
        "cwd": str(invocation.cwd),
        "last_message_path": (
            str(invocation.last_message_path)
            if invocation.last_message_path is not None
            else None
        ),
        **model_effort,
        **workspace_fields,
    }


def _resolved_model_record(record: dict[str, Any]) -> dict[str, Any]:
    """Decide one role's model from what it requested and what it observed."""

    observed = record["observed_models"]
    # A requested model is the run's own answer; an observation cannot outrank
    # it. Only an unresolved request is open for an observation to settle, and
    # only when every turn of the role agreed on the same model — disagreement
    # means the role has no single answer, not that the last one wins.
    resolved = (
        record["requested_model_source"] == config.MODEL_SOURCE_UNRESOLVED
        and len(observed) == 1
    )
    return {
        **record,
        "model": observed[0] if resolved else record["requested_model"],
        "model_source": (
            config.MODEL_SOURCE_HARNESS_REPORTED
            if resolved
            else record["requested_model_source"]
        ),
    }


def _merged_model_projection(
    projection: Any, turn: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Fold one turn into the run's durable per-role model projection.

    `model` records what the run asked for, which stays true of the request
    forever and says nothing about what ran; a run that requested nothing keeps
    the harness default under unresolved provenance. This projection is where
    the observation is allowed to settle that question.

    Roles are kept apart by session kind and harness because a review harness
    need not be the implementation harness, and one projection over both would
    attribute one harness's model to the other. The projection accumulates
    rather than being recomputed from the turn list, so trimming old turns
    cannot erase what the run already observed.
    """

    records: dict[tuple[str, str], dict[str, Any]] = {}
    if isinstance(projection, list):
        for entry in projection:
            if not isinstance(entry, Mapping):
                continue
            session_kind = entry.get("session_kind")
            harness = entry.get("harness")
            if not isinstance(session_kind, str) or not isinstance(harness, str):
                continue
            observed = entry.get("observed_models")
            records[(session_kind, harness)] = {
                "session_kind": session_kind,
                "harness": harness,
                "requested_model": entry.get("requested_model"),
                "requested_model_source": entry.get("requested_model_source"),
                "observed_models": (
                    [item for item in observed if isinstance(item, str)]
                    if isinstance(observed, list)
                    else []
                ),
                "turns": turns if isinstance(turns := entry.get("turns"), int) else 0,
            }

    session_kind = turn.get("session_kind")
    harness = turn.get("harness")
    if isinstance(session_kind, str) and isinstance(harness, str):
        record = records.setdefault(
            (session_kind, harness),
            {
                "session_kind": session_kind,
                "harness": harness,
                "requested_model": turn.get("model"),
                "requested_model_source": turn.get("model_source"),
                "observed_models": [],
                "turns": 0,
            },
        )
        record["turns"] += 1
        observed_model = turn.get("observed_model")
        if (
            isinstance(observed_model, str)
            and observed_model
            and observed_model not in record["observed_models"]
        ):
            record["observed_models"] = sorted(
                [*record["observed_models"], observed_model]
            )

    return [_resolved_model_record(record) for record in records.values()]
