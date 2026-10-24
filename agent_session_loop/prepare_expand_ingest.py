"""Atomic ``expand-ingest`` batch application and its CLI wrapper."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import plans_metadata, repository_intent, responses
from .authoring_transaction import PlanTransaction
from .cli_defaults import program_name
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .prepare_corrections import CorrectionOutcome, _apply_one_correction
from .prepare_expansion import insert_expansion_reference, render_expansion
from .prepare_models import (
    CORRECTION_ALREADY_APPLIED,
    CORRECTION_APPLIED,
    CORRECTION_BLOCKED,
    PREPARE_VALIDATION_FILENAME,
    PrepareValidation,
    RepositoryFootprintError,
    RepositoryRegistryRequiredError,
)
from .prepare_parsing import parse_validator_output
from .prepare_validation_store import (
    _validate_validation_authority,
    load_prepare_validation,
    merge_validations,
    serialize_prepare_validation,
)


@dataclass(frozen=True)
class ExpandIngestResult:
    """The outcome of one atomic ``expand-ingest`` batch application."""

    aggregate: PrepareValidation
    batch_session_ids: tuple[str, ...]
    files: tuple[Path, ...]
    outcomes: tuple[CorrectionOutcome, ...]


def ingest_expand_batch(
    plan_dir: str | Path,
    payload: object,
    *,
    expanded: str | None = None,
    replace_aggregate: bool = False,
) -> ExpandIngestResult:
    """Ingest one validator batch as a single all-or-nothing transaction.

    Merges the batch into the stored ``prepare-validation.json`` aggregate
    (or starts a fresh aggregate when ``replace_aggregate`` is true), renders
    the batch's ``session-<id>.expand.md`` documents and reference lines, and
    applies its exact factual corrections — staging **every** write (the
    aggregate, each expansion document, each session reference insertion, and
    each applied correction) into one
    :class:`~agent_session_loop.authoring_transaction.PlanTransaction` that commits
    atomically.

    All parsing happens before the transaction is staged, and every late
    rendering or correction failure (for example a session with no
    ``> **Before starting**:`` anchor) raises :class:`PlanError` *before*
    :meth:`PlanTransaction.commit`, so a failed batch leaves every pre-command
    file byte-identical and creates no new artifact — the aggregate is never
    persisted ahead of a rendering/correction failure.

    Session corrections are applied against the same in-memory text that
    already carries the freshly inserted expansion reference line, matching the
    historical expansion-then-correction ordering.
    """
    canonical_dir = canonicalize_path(plan_dir)
    update = parse_validator_output(payload)
    metadata_text = _validate_validation_authority(canonical_dir, update)
    aggregate = merge_validations(
        None if replace_aggregate else load_prepare_validation(plan_dir),
        update,
    )

    transaction = PlanTransaction(canonical_dir)
    if metadata_text is not None:
        transaction.stage(plans_metadata.PLAN_METADATA_FILE_NAME, metadata_text)
    transaction.stage(
        PREPARE_VALIDATION_FILENAME, serialize_prepare_validation(aggregate)
    )

    outcomes: list[CorrectionOutcome] = []
    for session in update.sessions:
        session_path = canonical_dir / session.session_id.filename
        try:
            session_text = session_path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise PlanError(f"missing session file: {session_path}") from error
        except OSError as error:
            raise PlanError(f"cannot read {session_path}: {error}") from error

        original_text = session_text
        transaction.stage(
            session.session_id.expansion_filename,
            render_expansion(
                session,
                codebase_root=update.codebase_root,
                expanded=expanded,
                validation_version=update.version,
            ),
        )
        session_text, _changed = insert_expansion_reference(
            session_text, session.session_id
        )
        for correction in session.corrections:
            session_text, outcome = _apply_one_correction(
                session_text, session.session_id.value, correction
            )
            outcomes.append(outcome)
        if session_text != original_text:
            transaction.stage(session.session_id.filename, session_text)

    files = transaction.commit()
    return ExpandIngestResult(
        aggregate=aggregate,
        batch_session_ids=tuple(update.session_ids),
        files=files,
        outcomes=tuple(outcomes),
    )


EXPAND_INGEST_REASON_OK = "ok"
#: Any fail-closed parse/merge/render/correction :class:`PlanError`. The batch
#: file is verbatim validator output, so a malformed payload is a schema issue.
EXPAND_INGEST_REASON_VALIDATOR_SCHEMA = "validator-schema"
#: The batch file itself could not be read (not a validator schema problem).
EXPAND_INGEST_REASON_INPUT_ERROR = "input-error"
#: The plan reaches more than one repository while still in legacy mode. The
#: payload is well-formed; the plan is the thing that must change.
EXPAND_INGEST_REASON_REPOSITORY_FOOTPRINT = "repository-footprint"
#: The plan intends to reach more than one repository while still in legacy
#: mode, including locations no session has created yet.
EXPAND_INGEST_REASON_REGISTRY_REQUIRED = (
    repository_intent.REASON_REPOSITORY_REGISTRY_REQUIRED
)


def expand_ingest_from_args(args: Any) -> int:
    """Run the ``expand-ingest`` subcommand from CLI arguments.

    Delegates to :func:`ingest_expand_batch`, which merges the payload into the
    stored ``prepare-validation.json`` aggregate, (re)renders the batch's
    ``session-<id>.expand.md`` files, and applies every proposed factual
    correction — all through one atomic
    :class:`~agent_session_loop.authoring_transaction.PlanTransaction`.
    A malformed payload
    (or any late rendering/correction failure) fails closed with
    ``reason: "validator-schema"`` and the :class:`PlanError` message verbatim,
    leaving every pre-command file byte-identical and creating no new artifact.
    """
    want_json = bool(args.json)
    plan_dir = str(args.plan_dir)
    batch_file = str(args.batch_file)
    common_fields = {"plan_dir": plan_dir, "batch_file": batch_file}

    def _emit_error(reason: str, error: str, **extra: Any) -> int:
        if want_json:
            responses.print_json(
                responses.error_payload(
                    "expand-ingest",
                    reason=reason,
                    error=error,
                    paused=False,
                    **{**common_fields, **extra},
                )
            )
        else:
            print(f"{program_name()} expand-ingest: {error}", file=sys.stderr)
        return 1

    try:
        text = Path(os.path.expanduser(batch_file)).read_text(encoding="utf-8")
    except OSError as error:
        return _emit_error(
            EXPAND_INGEST_REASON_INPUT_ERROR,
            f"cannot read batch file {batch_file}: {error}",
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        return _emit_error(
            EXPAND_INGEST_REASON_VALIDATOR_SCHEMA,
            f"batch file is not valid JSON: {error.msg} at line {error.lineno}",
        )

    try:
        # Merge the aggregate, render the batch's expansions, and apply its
        # corrections as one all-or-nothing transaction. Expansions and
        # corrections operate on exactly the batch, not the whole aggregate;
        # a late failure leaves prepare-validation.json unwritten.
        result = ingest_expand_batch(plan_dir, payload)
    except RepositoryFootprintError as error:
        return _emit_error(
            EXPAND_INGEST_REASON_REPOSITORY_FOOTPRINT,
            str(error),
            repository_footprint=error.payload,
        )
    except RepositoryRegistryRequiredError as error:
        return _emit_error(
            EXPAND_INGEST_REASON_REGISTRY_REQUIRED,
            str(error),
            repository_intent=error.payload,
        )
    except PlanError as error:
        return _emit_error(EXPAND_INGEST_REASON_VALIDATOR_SCHEMA, str(error))
    batch_session_ids = result.batch_session_ids
    files = result.files
    outcomes = result.outcomes

    applied = sum(1 for o in outcomes if o.status == CORRECTION_APPLIED)
    already = sum(1 for o in outcomes if o.status == CORRECTION_ALREADY_APPLIED)
    blocked = sum(1 for o in outcomes if o.status == CORRECTION_BLOCKED)
    corrections = [outcome.to_dict() for outcome in outcomes]
    spark_missing = sorted(
        session_id
        for session_id in batch_session_ids
        if (session := result.aggregate.session(session_id)) is not None
        and not session.spark.reported
    )
    payload_fields = {
        "sessions": list(batch_session_ids),
        "files": [str(path) for path in files],
        "corrections": corrections,
        "spark_missing": spark_missing,
        "correction_summary": {
            "applied": applied,
            "already_applied": already,
            "blocked": blocked,
        },
    }

    if want_json:
        responses.print_json(
            responses.success_payload(
                "expand-ingest",
                reason=EXPAND_INGEST_REASON_OK,
                **{**common_fields, **payload_fields},
            )
        )
    else:
        print(f"Plan directory: {plan_dir}")
        print(f"Ingested sessions: {', '.join(batch_session_ids)}")
        for path in files:
            print(f"  {path}")
        print(
            f"Corrections: {applied} applied, {already} already applied, "
            f"{blocked} blocked"
        )
        if spark_missing:
            print(
                "Spark eligibility marker missing for sessions: "
                f"{', '.join(spark_missing)}"
            )
        for outcome in outcomes:
            if outcome.status == CORRECTION_BLOCKED:
                print(
                    f"  blocked [{outcome.blocker}] session "
                    f"{outcome.session_id}: {outcome.detail}",
                    file=sys.stderr,
                )
    return 0
