"""Heartbeat-driven classification of a silent harness turn as hung."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import (
    clock,
    harnesses,
    process_tree,
    turn_execution_constants,
    turn_execution_hang_settings,
)


@dataclass
class HangAssessment:
    """One heartbeat-tick verdict from :class:`HangSentinel`.

    ``suspected`` is the current (possibly refreshed) suspicion block or
    ``None``; ``transition`` is ``"suspected"``/``"cleared"`` only on the tick
    the state actually flips; ``retracted`` carries the old block on
    ``"cleared"`` so the event and notification can name what was withdrawn.
    ``generation`` is the sentinel activity generation the verdict sampled;
    ``confirm_assessment`` rejects the verdict at fold time when output has
    resumed since, so a stale suspicion can never be persisted after a newer
    clear.

    ``cpu_progress`` is the tick's raw CPU sampling result, published whenever
    a process tree was sampled — including ticks that raise no suspicion at
    all. A supervisor deciding on idle time alone has no ``suspected`` block to
    read, and a silent turn whose tree is burning CPU is working, not hung. Its
    ``cpu_total_seconds`` is accumulated by process identity rather than summed
    over the live tree, so consecutive ticks prove the delta and its
    non-negativity. ``None`` means no fresh sample this tick and must never be
    read as "not progressing".
    """

    suspected: dict[str, Any] | None = None
    transition: str | None = None
    retracted: dict[str, Any] | None = None
    generation: int = 0
    cpu_progress: dict[str, Any] | None = None
    input_wait_active: bool = False


class HangSentinel:
    """Classify a silent harness turn as suspected-hung from heartbeat ticks.

    Fed by the harness observation hooks (``note_harness_pid`` /
    ``note_final_result``) and by ``note_activity`` on resumed output;
    ``evaluate`` runs once per heartbeat tick. ``evaluate`` spawns ``ps``
    through ``sample_tree`` and must never be called under
    ``LiveTurn._lock``.
    """

    def __init__(
        self,
        *,
        sample_tree: Callable[
            [int], list[dict[str, Any]] | None
        ] = process_tree.sample_process_tree,
        monotonic: Callable[[], float] = time.monotonic,
        trigger_idle_seconds: float | None = None,
        confirm_samples: int | None = None,
        cpu_epsilon_seconds: float = turn_execution_constants.HANG_CPU_EPSILON_SECONDS,
        result_grace_seconds: float | None = None,
        root_shutdown_grace_seconds: float = (
            turn_execution_constants.HANG_ROOT_SHUTDOWN_GRACE_SECONDS
        ),
    ) -> None:
        self._sample_tree = sample_tree
        self._monotonic = monotonic
        self._trigger_idle_seconds = (
            turn_execution_hang_settings._hang_trigger_idle_seconds()
            if trigger_idle_seconds is None
            else trigger_idle_seconds
        )
        self._confirm_samples = (
            turn_execution_hang_settings._hang_confirm_samples()
            if confirm_samples is None
            else confirm_samples
        )
        self._cpu_epsilon_seconds = cpu_epsilon_seconds
        self._result_grace_seconds = (
            turn_execution_hang_settings._hang_result_grace_seconds()
            if result_grace_seconds is None
            else result_grace_seconds
        )
        self._lock = threading.Lock()
        self._harness_pid: int | None = None
        self._result_seen_at: datetime | None = None
        self._result_seen_monotonic: float | None = None
        self._last_cpu_seconds: float | None = None
        self._cpu_high_water: dict[process_tree.CpuIdentity, float] = {}
        self._zero_cpu_streak = 0
        self._suspected: dict[str, Any] | None = None
        self._recovery_attempted = False
        self._root_shutdown_grace_seconds = root_shutdown_grace_seconds
        self._root_shutdown_started: float | None = None
        self._root_shutdown_tree: tuple[dict[str, Any], ...] = ()
        self._root_shutdown_escalated = False
        # The harness's input channel, when it has one ASL may close. A
        # streaming harness can hold its stdin open past a terminal result
        # (background tasks it still tracks, a message it has not answered);
        # a root left alone and idle behind that hold is asked to finish by
        # closing stdin before any signal is sent (ASI-151).
        self._input_channel: harnesses.HarnessTransport | None = None
        self._input_release: dict[str, Any] | None = None
        self._input_released_at: float | None = None
        # Bumped on every note_activity; assessments carry the generation
        # they sampled so any verdict outlived by resumed output is discarded
        # (mid-sample by evaluate, post-return by confirm_assessment).
        self._activity_generation = 0
        # True once the current episode's suspected event was confirmed for
        # persistence; only announced episodes yield a retracted block (and
        # thus a cleared event), keeping suspected/cleared pairs matched.
        self._announced = False

    def note_harness_pid(self, pid: int) -> None:
        with self._lock:
            if pid == self._harness_pid:
                return
            # A different root makes the accumulated identities another tree's:
            # the dead root's frozen CPU would inflate the total forever and
            # mask a real hang. Re-notifying the same pid changes nothing, so a
            # repeat call cannot keep resetting the streak.
            self._harness_pid = pid
            self._activity_generation += 1
            self._root_shutdown_started = None
            self._root_shutdown_tree = ()
            self._root_shutdown_escalated = False
            self._input_release = None
            self._input_released_at = None
            self._cpu_high_water = {}
            self._last_cpu_seconds = None
            self._zero_cpu_streak = 0
            self._recovery_attempted = False

    def note_input_channel(self, channel: harnesses.HarnessTransport | None) -> None:
        with self._lock:
            self._input_channel = channel

    def input_hold_reason(self) -> str | None:
        """Why the harness's stdin is still open, or ``None`` when it is not."""
        with self._lock:
            channel = self._input_channel
        if channel is None:
            return None
        with contextlib.suppress(Exception):
            return channel.input_hold_reason()
        return None

    def note_final_result(self) -> None:
        with self._lock:
            if self._result_seen_monotonic is None:
                self._result_seen_at = clock.utc_now()
                self._result_seen_monotonic = self._monotonic()

    def note_activity(self, *, request_started: bool = False) -> dict[str, Any] | None:
        """Retract suspicion on resumed output; return the retracted block.

        Returns ``None`` for an unannounced episode (its suspected event was
        never confirmed for persistence) so no orphan cleared event is
        emitted for a suspicion nobody was ever told about. ``request_started``
        also invalidates a terminal result from the prior protocol request.
        """
        with self._lock:
            self._activity_generation += 1
            if request_started:
                self._result_seen_at = None
                self._result_seen_monotonic = None
            retracted = self._suspected if self._announced else None
            self._suspected = None
            self._announced = False
            self._zero_cpu_streak = 0
            self._last_cpu_seconds = None
            self._cpu_high_water = {}
            self._recovery_attempted = False
            self._root_shutdown_started = None
            self._root_shutdown_tree = ()
            self._root_shutdown_escalated = False
            self._input_release = None
            self._input_released_at = None
            return retracted

    def note_turn_exit(self) -> dict[str, Any] | None:
        """Retract an announced suspicion when the harness process exits.

        A terminal process exit is conclusive recovery evidence even when the
        SIGTERM-unblocked harness emits no additional output chunk. A turn that
        stays alive never reaches this hook and retains its unresolved block.
        """
        with self._lock:
            retracted = self._suspected if self._announced else None
            self._suspected = None
            self._announced = False
            return dict(retracted) if retracted is not None else None

    def recover_post_result(self, assessment: HangAssessment) -> HangAssessment:
        """Recover a terminal-result hang, including a root-only shutdown.

        Revalidation samples outside the sentinel lock. The generation is then
        checked again and signals are delivered while holding the lock, so
        output that resumed during the sample invalidates the assessment before
        any process can be touched. Generic idle suspicions never enter here.
        """
        block = assessment.suspected
        if (
            block is None
            or block.get("reason")
            != turn_execution_constants.HANG_REASON_RESULT_RECEIVED
        ):
            return assessment
        # Read before the lock: the channel answers under its own lock.
        hold_reason = self.input_hold_reason()
        with self._lock:
            if (
                assessment.generation != self._activity_generation
                or self._suspected is None
            ):
                return assessment
            pid = self._harness_pid
            prior_tree = tuple(block.get("process_tree") or ())
            root_only = (
                pid is not None
                and len(prior_tree) == 1
                and prior_tree[0].get("pid") == pid
            )
            force = False
            previous_recovery = block.get("recovery")
            if hold_reason is not None:
                # The harness still holds stdin open after its result. With
                # descendants alive the hold may be for them (a background
                # task still running), so nothing is reaped. With the root
                # alone and idle, closing stdin is the graceful ask to finish,
                # and it is made once before any signal (ASI-151).
                channel = self._input_channel
                if not root_only or channel is None or self._input_release:
                    return assessment
                self._input_release = {
                    "policy": turn_execution_constants.POST_RESULT_STDIN_RELEASE_POLICY,
                    "root_pid": pid,
                    "hold_reason": hold_reason,
                    "attempted_at": str(
                        block.get("last_checked_at") or clock.isoformat(clock.utc_now())
                    ),
                }
                release = self._input_release
            else:
                release = None
        if release is not None and channel is not None:
            released = False
            with contextlib.suppress(Exception):
                released = bool(channel.release_input())
            with self._lock:
                release["released"] = released
                if (
                    assessment.generation != self._activity_generation
                    or self._suspected is None
                ):
                    return HangAssessment(generation=assessment.generation)
                self._input_released_at = self._monotonic()
                recovered_block = dict(self._suspected)
                recovered_block["recovery"] = dict(release)
                self._suspected = recovered_block
                return HangAssessment(
                    suspected=dict(recovered_block),
                    transition=assessment.transition,
                    retracted=assessment.retracted,
                    generation=assessment.generation,
                    cpu_progress=assessment.cpu_progress,
                )
        with self._lock:
            if (
                assessment.generation != self._activity_generation
                or self._suspected is None
            ):
                return assessment
            if (
                self._input_released_at is not None
                and not self._root_shutdown_tree
                and self._monotonic() - self._input_released_at
                < self._root_shutdown_grace_seconds
            ):
                # stdin was just closed; give the CLI the same grace a
                # SIGTERM gets before falling back to the signal path.
                return assessment
            if self._root_shutdown_tree:
                if (
                    self._root_shutdown_started is None
                    or self._root_shutdown_escalated
                    or not root_only
                    or self._monotonic() - self._root_shutdown_started
                    < self._root_shutdown_grace_seconds
                ):
                    return assessment
                force = True
                self._root_shutdown_escalated = True
                prior_tree = self._root_shutdown_tree
            elif root_only:
                self._root_shutdown_tree = tuple(dict(row) for row in prior_tree)
            elif self._recovery_attempted:
                return assessment
            else:
                self._recovery_attempted = True
            attempted_at = str(
                block.get("last_checked_at") or clock.isoformat(clock.utc_now())
            )

        if pid is None:
            return assessment
        if root_only:
            recovery, candidates = process_tree.prepare_verified_root_recovery(
                root_pid=pid,
                prior_tree=prior_tree,
                attempted_at=attempted_at,
                force=force,
            )
        else:
            recovery, candidates = process_tree.prepare_verified_leaf_recovery(
                root_pid=pid,
                prior_tree=prior_tree,
                attempted_at=attempted_at,
            )

        with self._lock:
            if (
                assessment.generation != self._activity_generation
                or self._suspected is None
            ):
                # Output resumed while the process table was sampled. The
                # activity path already cleared the episode and re-armed a
                # future one, so this stale assessment publishes nothing.
                return HangAssessment(generation=assessment.generation)
            if root_only:
                recovery = process_tree.signal_verified_root_recovery(
                    recovery, candidates, force=force
                )
                if not force and recovery.get("signaled_pids") == [pid]:
                    self._root_shutdown_started = self._monotonic()
            else:
                recovery = process_tree.signal_verified_leaf_recovery(
                    recovery, candidates
                )
            if previous_recovery is not None:
                recovery["previous_attempt"] = previous_recovery
            recovered_block = dict(self._suspected)
            recovered_block["recovery"] = recovery
            self._suspected = recovered_block
            return HangAssessment(
                suspected=dict(recovered_block),
                transition=assessment.transition,
                retracted=assessment.retracted,
                generation=assessment.generation,
                cpu_progress=assessment.cpu_progress,
            )

    def confirm_assessment(self, assessment: HangAssessment) -> bool:
        """Atomically re-validate an assessment immediately before folding.

        Returns ``False`` when output resumed after the assessment sampled
        its activity generation — the verdict is stale and must be discarded
        wholesale. On success marks a live suspicion announced, so a
        subsequent ``note_activity`` retraction emits its paired cleared
        event strictly after the suspected event the caller is persisting
        (the caller holds the fold lock across confirm and persist).
        """
        with self._lock:
            if assessment.generation != self._activity_generation:
                return False
            if self._suspected is not None:
                self._announced = True
            return True

    def evaluate(
        self,
        *,
        idle_seconds: float,
        now: datetime,
        input_wait_active: bool = False,
    ) -> HangAssessment:
        with self._lock:
            pid = self._harness_pid
            result_seen_at = self._result_seen_at
            result_seen_monotonic = self._result_seen_monotonic
            previous = self._suspected
            generation = self._activity_generation

            if input_wait_active:
                retracted = self._suspected if self._announced else None
                self._suspected = None
                self._announced = False
                self._zero_cpu_streak = 0
                self._last_cpu_seconds = None
                self._cpu_high_water = {}
                self._recovery_attempted = False
                self._root_shutdown_started = None
                self._root_shutdown_tree = ()
                self._root_shutdown_escalated = False
                self._input_release = None
                self._input_released_at = None
                return HangAssessment(
                    transition="cleared" if retracted is not None else None,
                    retracted=(dict(retracted) if retracted is not None else None),
                    generation=generation,
                    input_wait_active=True,
                )

        result_grace_hit = (
            result_seen_monotonic is not None
            and self._monotonic() - result_seen_monotonic >= self._result_grace_seconds
        )
        should_sample = pid is not None and (
            result_grace_hit
            or idle_seconds >= self._trigger_idle_seconds
            or previous is not None
        )
        tree: list[dict[str, Any]] | None = None
        tree_error: str | None = None
        if should_sample:
            assert pid is not None
            # The ps spawn happens here, outside every lock.
            tree = self._sample_tree(pid)
            if tree is None:
                tree_error = "process tree sample failed"
            else:
                # Sampling only happens while the turn is silent or suspected
                # hung, which is exactly when the post-turn sweep needs a view
                # of descendants that may since have left the process group.
                process_tree.record_process_tree(pid, tree)

        with self._lock:
            if generation != self._activity_generation:
                # note_activity ran while ps sampled; the idle reading and
                # any verdict built from it are stale — discard this tick.
                return HangAssessment(generation=generation)

            cpu_delta: float | None = None
            cpu_positive = False
            cpu_total: float | None = None
            if tree is not None:
                # Accumulated per process identity, never summed over the live
                # tree: a descendant that exits between two samples must not
                # subtract the CPU it burned, which would read as stillness.
                cpu_total, self._cpu_high_water = process_tree.accumulate_cpu_seconds(
                    tree, self._cpu_high_water
                )
                if self._last_cpu_seconds is not None:
                    cpu_delta = cpu_total - self._last_cpu_seconds
                    if cpu_delta <= self._cpu_epsilon_seconds:
                        self._zero_cpu_streak += 1
                    else:
                        self._zero_cpu_streak = 0
                        cpu_positive = True
                self._last_cpu_seconds = cpu_total
            elif should_sample:
                # A failed sample never confirms: demand a fresh consecutive
                # run of successful zero-delta samples.
                self._zero_cpu_streak = 0
                self._last_cpu_seconds = None
                self._cpu_high_water = {}

            checked_at = clock.isoformat(now)
            cpu_progress: dict[str, Any] | None = None
            if tree is not None:
                cpu_progress = {
                    "harness_pid": pid,
                    # The first successful sample has no predecessor to compare
                    # against, so progress is unknown rather than absent.
                    "progressing": cpu_positive if cpu_delta is not None else None,
                    "cpu_delta_seconds": cpu_delta,
                    "cpu_total_seconds": cpu_total,
                    "tracked_processes": len(self._cpu_high_water),
                    "sampled_processes": len(tree),
                    "confirm_samples": self._zero_cpu_streak,
                    "last_checked_at": checked_at,
                }

            # Both classes demand the same proof: a confirmed run of zero-CPU
            # samples. A received harness result is sticky, so without this the
            # suspicion re-raises on every later tick and the CPU retraction
            # below is unreachable — a worker still burning CPU behind a
            # buffering pipe reads as hung.
            confirmed = self._zero_cpu_streak >= self._confirm_samples
            reason: str | None = None
            if result_grace_hit and confirmed:
                reason = turn_execution_constants.HANG_REASON_RESULT_RECEIVED
            elif (
                pid is not None
                and idle_seconds >= self._trigger_idle_seconds
                and confirmed
            ):
                reason = turn_execution_constants.HANG_REASON_ZERO_CPU

            if reason is not None:
                block: dict[str, Any] = {
                    "reason": reason,
                    "detected_at": (
                        previous["detected_at"] if previous is not None else checked_at
                    ),
                    "idle_seconds": idle_seconds,
                    "harness_pid": pid,
                    "result_seen_at": (
                        clock.isoformat(result_seen_at)
                        if result_seen_at is not None
                        else None
                    ),
                    "confirm_samples": self._zero_cpu_streak,
                    "cpu_delta_seconds": cpu_delta,
                    "process_tree": tree,
                    "last_checked_at": checked_at,
                }
                if tree_error is not None:
                    block["process_tree_error"] = tree_error
                if previous is not None and "recovery" in previous:
                    block["recovery"] = previous["recovery"]
                self._suspected = block
                return HangAssessment(
                    suspected=dict(block),
                    transition="suspected" if previous is None else None,
                    generation=generation,
                    cpu_progress=cpu_progress,
                )
            if previous is not None and cpu_positive:
                self._suspected = None
                self._announced = False
                # The episode is over, exactly as on resumed output, so a
                # later confirmed hang earns its own recovery attempt.
                self._recovery_attempted = False
                self._input_release = None
                self._input_released_at = None
                self._root_shutdown_started = None
                self._root_shutdown_tree = ()
                self._root_shutdown_escalated = False
                retracted = dict(previous)
                retracted["cpu_delta_seconds"] = cpu_delta
                retracted["last_checked_at"] = checked_at
                return HangAssessment(
                    transition="cleared",
                    retracted=retracted,
                    generation=generation,
                    cpu_progress=cpu_progress,
                )
            if previous is not None:
                # A ps failure (or a streak broken by one) refreshes rather
                # than retracts: absence of evidence is not activity.
                refreshed = dict(previous)
                refreshed["idle_seconds"] = idle_seconds
                refreshed["confirm_samples"] = self._zero_cpu_streak
                refreshed["cpu_delta_seconds"] = cpu_delta
                refreshed["process_tree"] = tree
                refreshed["last_checked_at"] = checked_at
                refreshed.pop("process_tree_error", None)
                if tree_error is not None:
                    refreshed["process_tree_error"] = tree_error
                self._suspected = refreshed
                return HangAssessment(
                    suspected=dict(refreshed),
                    generation=generation,
                    cpu_progress=cpu_progress,
                )
            return HangAssessment(generation=generation, cpu_progress=cpu_progress)
