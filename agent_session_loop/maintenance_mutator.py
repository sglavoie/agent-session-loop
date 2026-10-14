"""The mutating half of a pass: run removal, body compression, budgeted removal."""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any

from . import retention
from .maintenance_bodies import (
    _Body,
    _BodyOutcome,
    _digest_file,
    _discard_staged,
    _iter_bodies,
    _over_budget_selection,
    _read_ledger,
    _stage_compressed,
    _write_ledger,
)
from .maintenance_models import MaintenanceFailure
from .maintenance_plan import (
    _Blocker,
    _Compaction,
    _drop,
    _Fence,
    _Removal,
)
from .maintenance_policy import (
    BODY_LEDGER_SCHEMA_VERSION,
    FAILURE_BODY_COMPRESS,
    FAILURE_BODY_REMOVE,
    FAILURE_RUN_REMOVAL,
    LANE_COMPACTION,
    LANE_RECLAIM,
    REASON_LOG_BODY_BUDGET,
    STAGE_MUTATION,
    _file_size,
    _tree_bytes,
)


class _Deadline:
    """The wall-clock bound on one pass, checked between units of work."""

    def __init__(self, seconds: float) -> None:
        self._seconds = max(0.0, seconds)
        self._started = time.monotonic()

    def expired(self) -> bool:
        return (time.monotonic() - self._started) >= self._seconds

    def elapsed(self) -> float:
        return round(time.monotonic() - self._started, 3)


class _Mutator:
    """Applies one plan under the claim, accounting only for bytes truly freed.

    Every method here that destroys something asks ``fence`` first, so the plan
    is a list of *candidates* rather than a list of permissions: what the plan
    said was safe is re-established against current state at the instant each
    action is taken, and a candidate the fence refuses is dropped and reported
    instead of deleted.

    "The instant each action is taken" means the ``unlink``, not the call that
    leads to one. Compression and budgeted removal both read a whole body before
    they destroy anything, so they carry the fence inside and re-take it at that
    boundary; a body they were told to stop touching keeps its bytes, and any
    metadata they had already published about it is corrected rather than left
    describing a removal that did not happen.
    """

    def __init__(self, *, deadline: _Deadline, now: datetime, fence: _Fence) -> None:
        self.deadline = deadline
        self.now = now
        self.fence = fence
        self.removed_run_ids: list[str] = []
        self.reclaimed_bytes = 0
        self.compressed_body_count = 0
        self.removed_body_count = 0
        self.compaction_reclaimed_bytes = 0
        self.failures: list[MaintenanceFailure] = []
        self.drops: list[dict[str, str]] = []
        self.deadline_hit = False

    def out_of_time(self) -> bool:
        if self.deadline.expired():
            self.deadline_hit = True
            return True
        return False

    def _fenced_out(
        self, *, lane: str, plan_dir: str, plan_state_dir: str, run_id: str
    ) -> bool:
        """True when current state forbids touching this run, recording why."""
        reason = self.fence(plan_dir, plan_state_dir, run_id)
        if not reason:
            return False
        self.drops.append(
            _drop(
                lane=lane,
                stage=STAGE_MUTATION,
                plan_dir=plan_dir,
                run_id=run_id,
                reason=reason,
            )
        )
        return True

    def remove_run(self, target: _Removal) -> None:
        """Delete one run directory, counting only the bytes that actually went."""
        if self._fenced_out(
            lane=LANE_RECLAIM,
            plan_dir=target.plan_dir,
            plan_state_dir=target.plan_state_dir,
            run_id=target.run_id,
        ):
            return
        candidate = target.candidate
        error = retention.remove_run_directory(candidate.path)
        if not error:
            self.removed_run_ids.append(candidate.run_id)
            self.reclaimed_bytes += candidate.size_bytes
            return
        # A partial rmtree leaves an unknown remainder, so measure it rather
        # than assume either extreme: the difference is what was really freed.
        still_allocated = _tree_bytes(candidate.path) if candidate.path.exists() else 0
        self.reclaimed_bytes += max(0, candidate.size_bytes - still_allocated)
        self.failures.append(
            MaintenanceFailure(
                kind=FAILURE_RUN_REMOVAL,
                target=str(candidate.path),
                error=error,
                bytes_still_allocated=still_allocated,
            )
        )

    def compress_body(self, body: _Body, *, blocker: _Blocker) -> _BodyOutcome:
        """Replace one raw body with its gzip, recording the original first.

        The fence in front of this call proves only that the body was
        unprotected when compression *started*, and compressing a body is the
        slowest thing maintenance does. ``blocker`` is therefore re-read twice
        more: once with the gzip merely staged, where a refusal costs a
        temporary file and changes nothing else, and once immediately before the
        original is unlinked, which is the only destructive instruction here.
        """
        target = body.source.with_name(f"{body.source.name}.gz")

        def failed(error: str) -> _BodyOutcome:
            self.failures.append(
                MaintenanceFailure(
                    kind=FAILURE_BODY_COMPRESS,
                    target=str(body.source),
                    error=error,
                    bytes_still_allocated=_file_size(body.source),
                )
            )
            return _BodyOutcome(body=None, blocked="")

        try:
            staged, digest, byte_count = _stage_compressed(body.source, target)
        except OSError as error:
            return failed(str(error))
        reason = blocker()
        if reason:
            # Nothing has been published, so withdrawing is exact: the staged
            # copy goes and the corpus is byte-for-byte what it was before.
            _discard_staged(staged)
            return _BodyOutcome(body=None, blocked=reason)
        try:
            os.replace(staged, target)
        except OSError as error:
            _discard_staged(staged)
            return failed(str(error))
        stored_bytes = _file_size(target)
        ledger = {
            "schema_version": BODY_LEDGER_SCHEMA_VERSION,
            "path": str(body.source),
            "byte_count": byte_count,
            "sha256": digest,
            "compressed_path": str(target),
            "compressed_bytes": stored_bytes,
            "compressed_at": self.now.isoformat(),
            "removed": False,
            "removed_at": None,
            "reason": "",
        }
        try:
            _write_ledger(body.source, ledger)
        except OSError as error:
            return failed(f"could not record the body ledger: {error}")
        reason = blocker()
        if reason:
            # The gzip and its ledger are published, and both are true: the
            # ledger says the body is not removed, and it is not. This is the
            # same recoverable shape an interrupted pass leaves — original plus
            # gzip plus sidecar — which the next unprotected pass converges.
            return _BodyOutcome(body=None, blocked=reason)
        try:
            body.source.unlink()
        except OSError as error:
            return failed(str(error))
        self.compressed_body_count += 1
        self.compaction_reclaimed_bytes += max(0, byte_count - stored_bytes)
        return _BodyOutcome(
            body=_Body(
                source=body.source,
                stored=target,
                compressed=True,
                stored_bytes=stored_bytes,
            ),
            blocked="",
        )

    def remove_body(self, body: _Body, *, blocker: _Blocker) -> str:
        """Remove one over-budget body, leaving its identity behind on disk.

        Returns the reason protection forbade the removal, or ``""``. Digesting
        a body that has no ledger yet reads every byte of it, so ``blocker`` is
        re-read after that preparation and again immediately before the unlink;
        a tombstone published in between is withdrawn rather than left standing
        over a body that is still there.
        """
        prepared = _read_ledger(body.source)
        if not prepared.get("sha256"):
            try:
                digest, byte_count = _digest_file(body.stored)
            except OSError as error:
                self.failures.append(
                    MaintenanceFailure(
                        kind=FAILURE_BODY_REMOVE,
                        target=str(body.stored),
                        error=str(error),
                        bytes_still_allocated=body.stored_bytes,
                    )
                )
                return ""
            prepared = {
                "schema_version": BODY_LEDGER_SCHEMA_VERSION,
                "path": str(body.source),
                "byte_count": byte_count,
                "sha256": digest,
                "compressed_path": str(body.stored) if body.compressed else None,
                "compressed_bytes": body.stored_bytes if body.compressed else None,
                "compressed_at": None,
                "removed": False,
                "removed_at": None,
                "reason": "",
            }
        reason = blocker()
        if reason:
            # Caught before the tombstone was published, so nothing on disk has
            # to be walked back at all.
            return reason
        ledger = {
            **prepared,
            "removed": True,
            "removed_at": self.now.isoformat(),
            "reason": REASON_LOG_BODY_BUDGET,
            "removed_path": str(body.stored),
            "removed_bytes": body.stored_bytes,
        }
        try:
            _write_ledger(body.source, ledger)
        except OSError as error:
            self.failures.append(
                MaintenanceFailure(
                    kind=FAILURE_BODY_REMOVE,
                    target=str(body.stored),
                    error=f"could not record the body ledger: {error}",
                    bytes_still_allocated=body.stored_bytes,
                )
            )
            return ""
        reason = blocker()
        if reason:
            self._withdraw_tombstone(body, prepared)
            return reason
        try:
            body.stored.unlink()
        except OSError as error:
            self.failures.append(
                MaintenanceFailure(
                    kind=FAILURE_BODY_REMOVE,
                    target=str(body.stored),
                    error=str(error),
                    bytes_still_allocated=_file_size(body.stored),
                )
            )
            return ""
        self.removed_body_count += 1
        self.compaction_reclaimed_bytes += body.stored_bytes
        return ""

    def _withdraw_tombstone(self, body: _Body, prepared: dict[str, Any]) -> None:
        """Undo a published tombstone for a body protection saved from removal.

        A ledger saying ``removed`` over a body that is still on disk is a lie
        an operator would act on, so it is rewritten to the pre-removal record —
        which keeps the digest the preparation just paid for. A rewrite that
        fails is a real partial failure: the bytes are safe, but the metadata
        beside them is wrong until a human or a later pass fixes it.
        """
        retracted = {
            key: value
            for key, value in prepared.items()
            if key not in ("removed_path", "removed_bytes")
        }
        retracted.update(removed=False, removed_at=None, reason="")
        try:
            _write_ledger(body.source, retracted)
        except OSError as error:
            self.failures.append(
                MaintenanceFailure(
                    kind=FAILURE_BODY_REMOVE,
                    target=str(body.stored),
                    error=f"could not withdraw the body ledger: {error}",
                    bytes_still_allocated=body.stored_bytes,
                )
            )

    def compact_run(self, target: _Compaction, *, log_body_budget_bytes: int) -> bool:
        """Compress then budget one run's bodies. False when the deadline hit.

        Compression is destructive too — the original is unlinked once the gzip
        and its ledger are durable — so the fence guards it as well as removal,
        and a run that acquires protection midway keeps every body it still has.
        The fence is handed down into both operations rather than only checked
        in front of them, because each one spends real time preparing before it
        destroys anything, and a reading that old is a memory, not a fence.
        """

        def blocker() -> str:
            return self.fence(target.plan_dir, target.plan_state_dir, target.run_id)

        def dropped(reason: str) -> None:
            self.drops.append(
                _drop(
                    lane=LANE_COMPACTION,
                    stage=STAGE_MUTATION,
                    plan_dir=target.plan_dir,
                    run_id=target.run_id,
                    reason=reason,
                )
            )

        bodies: list[_Body] = []
        for body in _iter_bodies(target.run_path):
            if self.out_of_time():
                return False
            if body.compressed:
                bodies.append(body)
                continue
            reason = blocker()
            if reason:
                dropped(reason)
                return True
            outcome = self.compress_body(body, blocker=blocker)
            if outcome.blocked:
                dropped(outcome.blocked)
                return True
            bodies.append(outcome.body if outcome.body is not None else body)
        selected, _removable = _over_budget_selection(
            bodies, budget_bytes=log_body_budget_bytes
        )
        for body in selected:
            if self.out_of_time():
                return False
            reason = blocker()
            if reason:
                dropped(reason)
                return True
            reason = self.remove_body(body, blocker=blocker)
            if reason:
                dropped(reason)
                return True
        return True
