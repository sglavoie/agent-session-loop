"""The concurrent validation stage and its findings."""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from . import harnesses, plans_registry
from .plans_errors import PlanError
from .prepare_corrections import CorrectionOutcome
from .prepare_expand_ingest import ingest_expand_batch
from .prepare_models import (
    LEGACY_PREPARE_VALIDATION_VERSION,
    PREPARE_VALIDATION_VERSION,
    REPOSITORY_AMBIGUITY_ISSUE,
    ContractProblem,
    PrepareValidation,
    PrepareValidationCache,
    ValidationIssue,
)
from .prepare_packet import (
    ValidationTaskPacket,
    batch_sessions,
    build_validation_packet,
)
from .prepare_parsing import _fail, parse_validator_output
from .prepare_validation_cache import (
    load_prepare_validation_cache,
    save_prepare_validation_cache,
)
from .prepare_validation_store import (
    _validate_validation_authority,
    load_prepare_validation,
)
from .prepare_validation_support import (
    _cache_entry_for_validation,
    _cache_hit_validation,
    _merge_batch_validation_results,
    _positive_validator_control,
    _single_session_validation,
    _validation_cache_identities,
    _validation_progress,
    _validation_session_hashes,
)
from .prepare_validator import ValidatorRunner


def _run_validation_stage(
    plan_dir: Path,
    session_ids: tuple[str, ...],
    *,
    codebase_root: str,
    validator: ValidatorRunner,
    batch_size: int,
    validator_harness: str,
    validator_model: str | None,
    validator_effort: str | None,
    validator_jobs: int,
    validator_heartbeat: int,
) -> tuple[PrepareValidation, int, tuple[CorrectionOutcome, ...]]:
    """Validate stale sessions concurrently, cache, then ingest in order.

    The aggregate remains a rendered plan artifact, not the cache authority:
    aggregates created before the cache existed deliberately cause one refresh.
    Every worker packet is constructed before the first worker starts and no
    expansion/correction mutation occurs until all launched workers have either
    yielded schema-valid output or completed with an error. A successful output
    is cached immediately, so a later failed worker leaves a resumable wave.
    """
    started_at = time.monotonic()
    batch_size = _positive_validator_control(batch_size, name="validation batch size")
    validator_jobs = _positive_validator_control(validator_jobs, name="validator jobs")
    validator_heartbeat = _positive_validator_control(
        validator_heartbeat, name="validator heartbeat"
    )

    # Parse an existing aggregate fail-closed, but do not make its historical
    # authority assertions gate a refresh. A changed session scope is exactly
    # the kind of key mismatch that must be allowed to revalidate that session.
    # The ordered ingest below validates every result against current authority
    # before it can affect the aggregate or plan files.
    load_prepare_validation(plan_dir)

    all_batches = batch_sessions(session_ids, batch_size)
    if not all_batches:
        raise _fail("no validator output was produced for this plan")
    expected_schema_version = (
        PREPARE_VALIDATION_VERSION
        if plans_registry.read_repository_registry(plan_dir) is not None
        else LEGACY_PREPARE_VALIDATION_VERSION
    )
    identities, reusable = _validation_cache_identities(
        plan_dir,
        session_ids,
        codebase_root=codebase_root,
        validator_harness=validator_harness,
        validator_model=validator_model,
        validator_effort=validator_effort,
        validator_schema_version=expected_schema_version,
    )
    cache = load_prepare_validation_cache(plan_dir)
    results: dict[str, PrepareValidation] = {}
    worker_batches: list[tuple[int, tuple[str, ...], ValidationTaskPacket]] = []
    completed_batches = 0

    for ordinal, batch in enumerate(all_batches, start=1):
        stale: list[str] = []
        for session_id in batch:
            cached = _cache_hit_validation(
                cache,
                session_id,
                identity=identities[session_id],
                reusable=reusable,
                codebase_root=codebase_root,
                validator_schema_version=expected_schema_version,
            )
            if cached is None:
                stale.append(session_id)
            else:
                results[session_id] = cached
        if not stale:
            completed_batches += 1
            _validation_progress(
                f"batch {ordinal}/{len(all_batches)} cache hit",
                completed=completed_batches,
                total=len(all_batches),
                in_flight=0,
                started_at=started_at,
            )
            continue
        packet = build_validation_packet(
            plan_dir, tuple(stale), codebase_root=codebase_root
        )
        if packet.version != expected_schema_version:
            raise _fail("validator packet schema changed while validation was starting")
        worker_batches.append((ordinal, tuple(stale), packet))

    newly_validated = sum(len(batch) for _ordinal, batch, _packet in worker_batches)
    failures: dict[int, Exception] = {}
    if worker_batches:
        next_batch = 0
        in_flight: dict[Future[object], tuple[int, tuple[str, ...]]] = {}
        with ThreadPoolExecutor(max_workers=validator_jobs) as executor:

            def launch_one() -> None:
                nonlocal next_batch
                ordinal, batch, packet = worker_batches[next_batch]
                next_batch += 1
                future = executor.submit(validator, packet)
                in_flight[future] = (ordinal, batch)
                _validation_progress(
                    f"batch {ordinal}/{len(all_batches)} started: {', '.join(batch)}",
                    completed=completed_batches,
                    total=len(all_batches),
                    in_flight=len(in_flight),
                    started_at=started_at,
                )

            while next_batch < len(worker_batches) and len(in_flight) < validator_jobs:
                launch_one()

            next_heartbeat = time.monotonic() + validator_heartbeat
            while in_flight:
                timeout = max(0.0, next_heartbeat - time.monotonic())
                done, _pending = wait(
                    in_flight,
                    timeout=timeout,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    _validation_progress(
                        "heartbeat",
                        completed=completed_batches,
                        total=len(all_batches),
                        in_flight=len(in_flight),
                        started_at=started_at,
                    )
                    while next_heartbeat <= time.monotonic():
                        next_heartbeat += validator_heartbeat
                    continue

                for future in sorted(done, key=lambda item: in_flight[item][0]):
                    ordinal, batch = in_flight.pop(future)
                    try:
                        payload = future.result()
                        update = parse_validator_output(payload)
                        if update.codebase_root != codebase_root:
                            root_field = (
                                "workspace_root"
                                if update.version == PREPARE_VALIDATION_VERSION
                                else "codebase_root"
                            )
                            raise _fail(
                                f"{root_field} {update.codebase_root!r} does not match "
                                f"the "
                                f"preparation root {codebase_root!r}"
                            )
                        _validate_validation_authority(
                            plan_dir, update, expected_session_ids=batch
                        )
                        for session in update.sessions:
                            session_validation = _single_session_validation(
                                update, session
                            )
                            results[session.session_id.value] = session_validation
                            cache = cache.with_entry(
                                _cache_entry_for_validation(
                                    session_validation,
                                    identity=identities[session.session_id.value],
                                )
                            )
                        # Persist every successful worker result before starting
                        # another batch. This is the recovery boundary for a
                        # later failure in the same concurrent wave.
                        save_prepare_validation_cache(plan_dir, cache)
                    except Exception as error:  # noqa: BLE001 - worker boundary
                        failures[ordinal] = error
                        detail = str(error)
                        _validation_progress(
                            (
                                f"batch {ordinal}/{len(all_batches)} failed"
                                + (f": {detail}" if detail else "")
                            ),
                            completed=completed_batches,
                            total=len(all_batches),
                            in_flight=len(in_flight),
                            started_at=started_at,
                        )
                    else:
                        completed_batches += 1
                        _validation_progress(
                            f"batch {ordinal}/{len(all_batches)} completed: "
                            f"{', '.join(batch)}",
                            completed=completed_batches,
                            total=len(all_batches),
                            in_flight=len(in_flight),
                            started_at=started_at,
                        )

                # Once any result failed, finish only work that was already in
                # flight. Its valid outputs still reach the cache, but no new
                # workers launch into a known-failed wave.
                if not failures:
                    while (
                        next_batch < len(worker_batches)
                        and len(in_flight) < validator_jobs
                    ):
                        launch_one()

        if failures:
            first_ordinal = min(failures)
            failure = failures[first_ordinal]
            if isinstance(failure, (PlanError, harnesses.HarnessError)):
                raise failure
            raise PlanError(
                f"validator batch {first_ordinal} failed: {failure}"
            ) from failure

    # Only now are plan mutations allowed. Reassemble each original packet in
    # batch order so corrections cannot change the files a later worker saw.
    # Recheck every assembled payload here, including cache hits, before the
    # first transaction replaces a historical aggregate.
    batch_validations = tuple(
        (batch, _merge_batch_validation_results(batch, results))
        for batch in all_batches
    )
    for batch, batch_validation in batch_validations:
        _validate_validation_authority(
            plan_dir,
            batch_validation,
            expected_session_ids=batch,
        )

    aggregate: PrepareValidation | None = None
    outcomes: list[CorrectionOutcome] = []
    for batch_index, (_batch, batch_validation) in enumerate(batch_validations):
        ingested = ingest_expand_batch(
            plan_dir,
            batch_validation.to_dict(),
            replace_aggregate=batch_index == 0,
        )
        aggregate = ingested.aggregate
        outcomes.extend(ingested.outcomes)

    if aggregate is None:
        raise _fail("no validator output was produced for this plan")
    missing = [sid for sid in session_ids if aggregate.session(sid) is None]
    if missing:
        raise _fail(f"stored aggregate is missing sessions: {', '.join(missing)}")

    # Ingesting a result inserts expansion references and can apply an exact
    # factual correction. Re-key only the resulting session text. Everything
    # shared by the worker wave remains frozen from its initial snapshot: in
    # particular, a repository change that happens while workers run must not
    # make old research look reusable under a new fingerprint.
    post_ingest_session_hashes = _validation_session_hashes(plan_dir, session_ids)
    refreshed_cache = PrepareValidationCache()
    for session_id in session_ids:
        stored_session = aggregate.session(session_id)
        if stored_session is None:
            raise _fail(f"stored aggregate is missing session {session_id}")
        refreshed_identity = dict(identities[session_id])
        refreshed_identity["session_sha256"] = post_ingest_session_hashes[session_id]
        refreshed_cache = refreshed_cache.with_entry(
            _cache_entry_for_validation(
                _single_session_validation(aggregate, stored_session),
                identity=refreshed_identity,
            )
        )
    # Store the outputs even when repository evidence was temporarily
    # unavailable. The next lookup simply refuses to reuse them until it can
    # establish a trustworthy fingerprint again.
    save_prepare_validation_cache(plan_dir, refreshed_cache)
    return aggregate, newly_validated, tuple(outcomes)


def _repository_ambiguity_findings(
    validation: PrepareValidation,
) -> tuple[tuple[str, ValidationIssue], ...]:
    if validation.version != PREPARE_VALIDATION_VERSION:
        return ()
    return tuple(
        (session.session_id.value, issue)
        for session in validation.sessions
        for issue in session.issues
        if issue.issue_type == REPOSITORY_AMBIGUITY_ISSUE
    )


def _contract_problem_findings(
    validation: PrepareValidation,
) -> tuple[tuple[str, ContractProblem], ...]:
    """Return every validator-reported contract defect in plan order."""
    return tuple(
        (session.session_id.value, problem)
        for session in validation.sessions
        for problem in session.contract_problems
    )
