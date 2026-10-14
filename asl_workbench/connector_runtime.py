"""The connector observation runtime: what the Mac publishes, and when.

This is the half of the Mac connector that has no opinion about where its
material comes from. It owns four things and nothing else:

* **One instance.** :class:`~asl_workbench.connector_support.SingleInstanceLock`
  is a pid file created with ``O_EXCL``, so two connectors racing for it resolve
  without either reading a half-written file, and a lock left behind by a dead
  owner is stolen rather than waited on forever.
* **Bounded, deterministic retry.** :class:`~asl_workbench.connector_support.Backoff`
  doubles to a ceiling and carries no jitter, because a connector whose
  reconnect schedule cannot be asserted is a connector whose reconnect behaviour
  cannot be tested.
* **Publication order and checkpoint timing.** A cycle's catalog pages and
  history batches are delivered before the source is told to commit, and a
  cycle that fails part way through is discarded uncommitted. The next pass
  re-reads from the same checkpoint and the receiver's duplicate suppression
  absorbs the overlap -- the only ordering under which "reconnect without
  duplication" and "no ending is ever lost" are both true.
* **Heartbeats that do not wait for observation.** The pass that walks the
  corpus runs somewhere else (a thread by default, injectable for tests), and
  :meth:`ObservationConnector.step` schedules the heartbeat from the clock
  rather than from the pass. A Mac whose disk has gone slow reports that it is
  alive and degraded, instead of going silent and being read as lost.
* **Answers to questions, on the same outward link.** A service with something
  to ask puts a ``request`` frame in the body of the acknowledgement of an
  upload; :class:`~asl_workbench.connector_protocols.Channel` returns it, and the
  connector answers it later with a correlated ``detail`` or a ``receipt`` saying
  why not. The read itself runs on a worker and the answer waits in a lane of its
  own, which is what keeps three failure modes apart: a slow disk cannot silence
  the heartbeat, a corpus being paged out cannot bury a read somebody is waiting
  on, and a service reading continuously cannot stop the corpus from ever being
  published. Every correlated body is dropped the moment it is sent, expires, or
  the link goes, and none of it is ever written down.

What it does *not* own is equally deliberate. It never reads a plan, never
spawns a subprocess, and never names a path: the corpus reaches it through
:class:`~asl_workbench.connector_protocols.ObservationSource`, whose only
ASL-aware implementation lives in ``agent_session_loop.workbench_connector``.
That is what lets the publication rules be tested against a stub source, and
what removed the Companion's bridge-subprocess dependency -- the new connector
calls the shared catalog, observation and history services in process.

Every frame leaves through :mod:`asl_workbench.transport`'s builders, so a
payload assembled wrong is refused here rather than at the far end; the payload
mappers live in :mod:`asl_workbench.connector_payloads`. The class itself is
assembled from lanes: state in :mod:`asl_workbench.connector_state`, mutations in
:mod:`asl_workbench.connector_mutations` and reads in
:mod:`asl_workbench.connector_reads`. What remains here is the schedule and the
delivery of one frame per step.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .connector_lanes import _ANSWER_KINDS, _Answer, _Pass, _Queue
from .connector_payloads import (
    catalog_page_payload,
    heartbeat_payload,
    history_batch_payload,
    snapshot_payload,
)
from .connector_protocols import ObservationCycle
from .connector_reads import ReadLane
from .connector_support import (
    STATE_BACKOFF,
    STATE_READY,
    STATE_STOPPED,
    WORKER_JOIN_TIMEOUT_SECONDS,
    ChannelError,
)
from .observation import MAX_LABEL_CHARS
from .operations import (
    DETAIL_BRIDGE_UNAVAILABLE,
    OPERATION_EXPIRED,
)
from .remote_actions import MutationResult, operation_payload
from .transport import (
    CAPABILITY_CATALOG,
    CAPABILITY_DETAIL,
    CAPABILITY_HISTORY,
    CAPABILITY_MUTATE,
    CAPABILITY_OBSERVE,
    KIND_DETAIL,
    KIND_OPERATION,
    KIND_RECEIPT,
    VIEW_CATALOG_PAGE,
    VIEW_HEARTBEAT,
    VIEW_HISTORY_BATCH,
    VIEW_SNAPSHOT,
    TransportError,
    detail_envelope,
    encode_frame,
    observation_envelope,
    operation_envelope,
    receipt_envelope,
)


class ObservationConnector(ReadLane):
    """Publishes one Mac's observations, on a schedule it can be held to.

    Driven by :meth:`step`, which does at most one thing: it sends a single
    frame, or it discovers it is in backoff, or it finds nothing to send. A
    caller loops it against a real clock; a test calls it with the moments it
    wants to assert about, and the behaviour is identical because the moment is
    always an argument and never a reading.
    """

    # -- the loop --------------------------------------------------------
    def step(self, now: float) -> str | None:
        """Service the schedules and deliver at most one frame.

        Returns the lane that was sent -- an observation view, ``detail`` or
        ``receipt`` -- or ``None`` when nothing was: an empty queue, a backoff
        that has not elapsed, or a delivery that failed. One frame per step is
        what keeps a recovered link from turning a built-up queue into an
        unbounded burst.

        Whatever the acknowledgement carried back is taken in here, which is the
        only way a question reaches this process. It is taken in *after* the
        delivery succeeded, so a request riding a response this connector never
        managed to read is simply never seen -- the far end's deadline covers
        it, and guessing would mean answering a question that may not have been
        asked.
        """
        if self._state == STATE_STOPPED:
            return None
        self._service(now)
        if self._state == STATE_BACKOFF and now < self._retry_at:
            return None
        selected = self._select()
        if selected is None:
            return None
        lane, payload, request_id, capability = selected
        try:
            frame = encode_frame(
                self._envelope(lane, payload, request_id, now, capability)
            )
        except TransportError as error:
            self._unpublishable(lane, error)
            return None
        try:
            carried = self._channel.send(frame)
        except ChannelError:
            self._fail(now)
            return None
        self._delivered(lane)
        self._receive(carried, now)
        return lane

    def _envelope(
        self,
        lane: str,
        payload: Mapping[str, Any],
        request_id: str | None,
        now: float,
        capability: str = CAPABILITY_DETAIL,
    ) -> dict[str, Any]:
        """The envelope for one selected frame, built by its own lane's rules."""
        if lane == KIND_DETAIL:
            assert request_id is not None
            return detail_envelope(
                payload=payload,
                connector_id=self.connector_id,
                target_id=self.target_id,
                sent_at=now,
                request_id=request_id,
            )
        if lane == KIND_OPERATION:
            return operation_envelope(
                payload=payload,
                connector_id=self.connector_id,
                target_id=self.target_id,
                sent_at=now,
                request_id=request_id,
            )
        if lane == KIND_RECEIPT:
            assert request_id is not None
            return receipt_envelope(
                payload=payload,
                capability=capability,
                connector_id=self.connector_id,
                target_id=self.target_id,
                sent_at=now,
                request_id=request_id,
            )
        return observation_envelope(
            payload=payload,
            connector_id=self.connector_id,
            target_id=self.target_id,
            sent_at=now,
        )

    def _unpublishable(self, lane: str, error: TransportError) -> None:
        """A frame this connector built is one its own receiver would refuse.

        An observation takes the cycle down with it: a refusal is a property of
        the payload, so leaving it queued would refuse it again on every step
        and the connector would never heartbeat again. Nothing is committed, so
        the next pass reads the same window.

        An answer takes only itself. The cycle is untouched and the heartbeat
        keeps claiming what is true, because one unencodable read says nothing
        about whether the corpus was walked -- and the caller's deadline is
        already waiting to report it.
        """
        self._release(lane)
        if lane in _ANSWER_KINDS:
            self._answer_fault = f"unsendable {lane}: {error}"[:MAX_LABEL_CHARS]
            return
        self._discard(f"unpublishable {lane}: {error}")

    def stop(self) -> None:
        """Stop publishing and let an in-flight observation pass go.

        Terminal. Nothing queued is delivered afterwards and nothing is
        committed, which leaves exactly the state a crash would have left: an
        uncommitted window is re-read next time and the receiver absorbs the
        overlap. The alternative -- committing on the way out -- would turn a
        shutdown during delivery into permanently lost history.
        """
        if self._state == STATE_STOPPED:
            return
        self._state = STATE_STOPPED
        # Bodies first, then the queue: a stopped connector holds no material,
        # and the process that replaces it starts with nothing to leak. Every
        # read this one had taken in is simply never answered, which is what the
        # far end's deadline is for.
        self._queue.clear_answers()
        self._forget_reads()
        # Every command still in hand goes with them, body references and
        # redemption tokens first. A stopped connector holds no authority to
        # act, so the only mutation that can still be running is the one already
        # handed to a worker -- and nothing restarts it.
        self._mutations.clear()
        self._settling_mutations.clear()
        self._queue = _Queue()
        self._cycle = None
        for thread in (self._thread, self._answer_thread, self._mutation_thread):
            if thread is not None and thread.is_alive():
                thread.join(WORKER_JOIN_TIMEOUT_SECONDS)
        self._thread = None
        self._answer_thread = None
        self._mutation_thread = None
        # After the join rather than in ``_forget_reads``: everywhere else that
        # abandons reads, the worker is still running and clearing this slot
        # would let a second read start beside it. Here there is no second read
        # and no next step, so the last thing a stopped connector is holding
        # goes too.
        with self._lock:
            self._answering = None
            self._answer_result = None
            self._mutating = None
            self._mutation_result = None

    # -- scheduling ------------------------------------------------------
    def _service(self, now: float) -> None:
        """Collect what the workers finished, start what is due, keep liveness."""
        self._collect(now)
        self._collect_answer()
        self._collect_mutation()
        self._expire_reads(now)
        self._expire_mutations(now)
        if now >= self._next_observation and not self.observing:
            self._start_observation(now)
        self._start_answer(now)
        self._start_mutation(now)
        if now >= self._next_heartbeat:
            self._queue.heartbeat = self._heartbeat(now)
            self._next_heartbeat = now + self._carrier_interval(now)

    def _start_observation(self, now: float) -> None:
        with self._lock:
            if self._observing:
                return
            self._observing = True
        # Scheduled before the pass runs rather than after it lands: a walk
        # that takes longer than the interval must not be re-entered the
        # instant it finishes.
        self._next_observation = now + self._observation_interval

        def attempt() -> None:
            outcome = _Pass()
            try:
                outcome.cycle = self._source.observe(now)
            except Exception as error:  # a source is arbitrary provider code
                outcome.error = f"{type(error).__name__}: {error}"
            with self._lock:
                self._result = outcome
                self._observing = False

        self._thread = self._run_observation(attempt)

    def _collect(self, now: float) -> None:
        """Take a finished pass, if one is waiting, and queue what it produced."""
        with self._lock:
            outcome = self._result
            self._result = None
        if outcome is None:
            return
        if outcome.cycle is None:
            self._degraded_reason = outcome.error or "observation failed"
            return
        if not self._queue.cycle_drained():
            # The previous cycle is still going out. Dropping the new one keeps
            # one generation's pages together; the next pass will read a corpus
            # at least as current as this one.
            return
        self._adopt(outcome.cycle, now)

    def _adopt(self, cycle: ObservationCycle, now: float) -> None:
        self._degraded_reason = cycle.degraded_reason
        self._last_cycle = cycle
        age = max(0.0, now - cycle.observed_at)
        self._queue.snapshot = snapshot_payload(
            cycle.runs,
            observed_at=cycle.observed_at,
            truncated=cycle.truncated,
            source_digest=cycle.source_digest,
            failed_projections=cycle.failed_projections,
            source_generation=self._generation,
            projection_age_seconds=age,
            warnings=cycle.warnings,
        )
        self._queue.pages = [
            catalog_page_payload(
                page,
                observed_at=cycle.observed_at,
                source_generation=self._generation,
                # The scan's warnings ride the page that closes the generation:
                # they describe the enumeration rather than any one slice of
                # it, and repeating them on every page would crowd out a page's
                # own.
                warnings=cycle.warnings if page.final else (),
            )
            for page in cycle.catalog_pages
        ]
        self._queue.batches = [
            history_batch_payload(batch) for batch in cycle.history_batches
        ]
        # Held only while it still has frames to deliver. A cycle with neither
        # pages nor batches has nothing to commit and nothing to wait for.
        self._cycle = None if self._queue.cycle_drained() else cycle

    def _heartbeat(self, now: float) -> dict[str, Any]:
        last = self._last_cycle
        return heartbeat_payload(
            observed_at=now,
            bridge_alive=last is not None and self._degraded_reason is None,
            asl_version=self.asl_version,
            degraded_reason=self._degraded_reason,
            source_generation=self._generation,
            projection_age_seconds=(
                max(0.0, now - last.observed_at) if last is not None else None
            ),
        )

    # -- delivery --------------------------------------------------------
    def _select(self) -> tuple[str, dict[str, Any], str | None, str] | None:
        """The next frame, in the order liveness outranks everything.

        The heartbeat goes first because a connector the receiver has written
        off cannot answer anything anyway. After that the two lanes take turns:
        a correlated read is not buried behind a corpus being paged out, and a
        service asking continuously cannot keep the corpus from ever going. A
        strict priority either way starves one of them, and both starvations
        are the kind nobody notices until a plan is missing from a cockpit.
        """
        if self._queue.heartbeat is not None:
            return VIEW_HEARTBEAT, self._queue.heartbeat, None, CAPABILITY_OBSERVE
        answer_ready = bool(self._queue.answers)
        if answer_ready and not (self._answered_last and self._queue.cycle_waiting()):
            answer = self._queue.answers[0]
            return answer.kind, answer.payload, answer.request_id, answer.capability
        if self._queue.snapshot is not None:
            return VIEW_SNAPSHOT, self._queue.snapshot, None, CAPABILITY_OBSERVE
        if self._queue.pages:
            return VIEW_CATALOG_PAGE, self._queue.pages[0], None, CAPABILITY_CATALOG
        if self._queue.batches:
            return VIEW_HISTORY_BATCH, self._queue.batches[0], None, CAPABILITY_HISTORY
        if answer_ready:
            answer = self._queue.answers[0]
            return answer.kind, answer.payload, answer.request_id, answer.capability
        return None

    def _release(self, view: str) -> None:
        """Take the frame :meth:`_select` returned off the queue."""
        if view in _ANSWER_KINDS:
            if self._queue.answers:
                # Forgotten on the way out, delivered or not: the frame has
                # already been encoded, so nothing here needs the body any more
                # and holding it would be holding a plan's text for no reason.
                self._queue.answers.pop(0).forget()
            return
        if view == VIEW_HEARTBEAT:
            self._queue.heartbeat = None
        elif view == VIEW_SNAPSHOT:
            self._queue.snapshot = None
        elif view == VIEW_CATALOG_PAGE:
            if self._queue.pages:
                self._queue.pages.pop(0)
        elif self._queue.batches:
            self._queue.batches.pop(0)

    def _delivered(self, view: str) -> None:
        self._release(view)
        if view in _ANSWER_KINDS:
            self._answered_last = True
        elif view != VIEW_HEARTBEAT:
            self._answered_last = False
        if self._state == STATE_BACKOFF:
            # A link that just came back is a new session as far as the
            # receiver is concerned, and it has to be able to tell one from a
            # connection that never dropped without being told.
            self._generation += 1
        self._state = STATE_READY
        self._backoff.reset()
        if self._cycle is not None and self._queue.cycle_drained():
            # Every frame the cycle produced has landed. This is the only place
            # a checkpoint moves, and it is after delivery rather than after
            # reading -- which is what makes an interrupted pass replay instead
            # of disappear.
            committed = self._cycle
            self._cycle = None
            self._source.commit(committed)

    def _fail(self, now: float) -> None:
        """A delivery failed. Drop the cycle and wait out the backoff.

        The reason is deliberately not recorded in ``degraded_reason``: that
        field travels on a heartbeat, and a heartbeat describing a link failure
        can only be sent once the link works again, by which point it would be
        describing something that is no longer true.

        The correlated half goes too, bodies and all. The far end abandons its
        waiters when the link drops, so every answer held here is an answer to a
        question nobody is waiting on any more -- and delivering one after the
        reconnect would hand a service material it had already reported as
        offline.

        Mutations are the exception, in both directions. A command that has not
        been attempted is dropped with its body reference and its tokens: its
        window is running out against a link that is down, and the far end will
        resubmit if it still wants the action. A *report* of one that was
        carried out is kept, because it is the only record this end has of what
        ASL did and the far end can still absorb it late.
        """
        self._queue.clear_cycle()
        self._queue.drop_correlated()
        self._forget_reads()
        for abandoned in self._mutations.abandon():
            self._enqueue(
                _Answer(
                    request_id=None,
                    kind=KIND_OPERATION,
                    payload=operation_payload(
                        abandoned,
                        MutationResult(
                            status=OPERATION_EXPIRED,
                            detail_code=DETAIL_BRIDGE_UNAVAILABLE,
                        ),
                        observed_at=now,
                    ),
                    capability=CAPABILITY_MUTATE,
                )
            )
        self._cycle = None
        self._state = STATE_BACKOFF
        self._retry_at = now + self._backoff.delay()

    def _discard(self, reason: str) -> None:
        """Drop the cycle over a payload this connector should not have built."""
        self._queue.clear_cycle()
        self._cycle = None
        self._degraded_reason = reason[:MAX_LABEL_CHARS]
