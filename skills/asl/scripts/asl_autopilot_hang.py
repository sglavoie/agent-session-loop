"""Hard-hang escalation tracking and ``worker_hung`` payloads."""

from __future__ import annotations

from asl_autopilot_constants import (
    DEFAULT_HANG_ESCALATION_PROBES,
    DEFAULT_HANG_IDLE_CEILING,
    HANG_CLASS_IDLE_CEILING,
    HANG_CLASS_RESULT_RECEIVED,
    HANG_CLASS_ZERO_CPU,
    HANG_NEXT_STEPS,
)
from asl_autopilot_heartbeat import _snapshot_cpu_progressing, _snapshot_idle_seconds


class _HangEscalation:
    """Decide when a runner-suspected worker hang becomes a hard escalation.

    Folds one heartbeat snapshot per supervision tick. Two policies form hard
    bars above the soft ``WARN possibly stalled`` line, which stays unchanged
    below them:

    - **result-received** (:data:`HANG_CLASS_RESULT_RECEIVED`): the runner's
      ``hang_suspected`` block reports the harness result already arrived
      while the process stays alive *and* its process tree burned no CPU
      across the runner's confirmation samples. After ``probes`` consecutive
      ticks (default 2, ~60s at the 30s cadence) escalate — nothing is running
      and nothing more is coming, so waiting longer only blocks the
      supervising session. Left ungated on CPU evidence because the runner
      never publishes this class while the tree progresses.
    - **idle ceiling**: a tick whose idle seconds reach ``idle_ceiling``
      (default 900s) escalates regardless of hang class —
      :data:`HANG_CLASS_ZERO_CPU` when the runner's block confirms the
      zero-CPU class, else :data:`HANG_CLASS_IDLE_CEILING` — unless the
      runner's ``hang_cpu_progress`` sample says the tree is progressing.
      Silence is not idleness: a worker piping through a buffering consumer
      writes nothing for minutes while burning real CPU. Unknown (no fresh
      sample, e.g. ``ps`` unavailable) still escalates, because a runner that
      cannot sample can never confirm zero CPU either — this bar is that
      case's only backstop. Deliberately conservative for zero-cpu so a
      legitimate long sleep/network wait below the ceiling never churns; the
      runner-side block and AgentMux notification already surfaced it.

    Any tick without a result-received block — including an explicit
    ``turn_hang_cleared`` retraction or a heartbeat that dropped the block —
    resets the consecutive-probe count.

    Probe counting is bound to one suspicion episode, identified by the
    block's ``detected_at``: the runner keeps it stable across refreshes of
    the same episode and stamps a fresh value when suspicion re-arms after a
    clear. A result-received block from a different episode resets the count
    before being counted, so a clear plus re-arm folded between two polls —
    where only the final snapshot is visible — cannot combine two episodes
    into the required consecutive probes.
    """

    def __init__(
        self,
        *,
        probes: int = DEFAULT_HANG_ESCALATION_PROBES,
        idle_ceiling: float = DEFAULT_HANG_IDLE_CEILING,
    ) -> None:
        self._probes_required = probes
        self._idle_ceiling = idle_ceiling
        self._result_probes = 0
        self._result_episode: object = None

    def observe(self, snapshot: object) -> dict | None:
        """Fold one tick's snapshot; return the escalation verdict or None."""
        if not isinstance(snapshot, dict):
            snapshot = {}
        block = snapshot.get("hang_suspected")
        if not isinstance(block, dict):
            block = None
        reason = block.get("reason") if block is not None else None
        if reason == HANG_CLASS_RESULT_RECEIVED:
            episode = block.get("detected_at")
            if episode != self._result_episode:
                self._result_probes = 0
                self._result_episode = episode
            self._result_probes += 1
        else:
            self._result_probes = 0
            self._result_episode = None
        idle_seconds = _snapshot_idle_seconds(snapshot, block)
        progressing = _snapshot_cpu_progressing(snapshot)

        def verdict(hang_class: str) -> dict:
            return {
                "class": hang_class,
                "hang_suspected": block,
                "idle_seconds": idle_seconds,
                "probes": self._result_probes,
            }

        if self._result_probes >= self._probes_required:
            return verdict(HANG_CLASS_RESULT_RECEIVED)
        if (
            idle_seconds is not None
            and idle_seconds >= self._idle_ceiling
            and progressing is not True
        ):
            return verdict(
                HANG_CLASS_ZERO_CPU
                if reason == HANG_CLASS_ZERO_CPU
                else HANG_CLASS_IDLE_CEILING
            )
        return None


def _hang_payload(
    escalation: dict,
    parsed: dict,
    status: object,
    *,
    supervised_child_pid: int | None = None,
    detached: bool | None = None,
) -> dict:
    """Build the structured ``hang`` block of a ``worker_hung`` result.

    Carries everything the supervising session needs to inspect the hang
    without killing anything: the escalation class, the runner's verbatim
    ``hang_suspected`` block (or null), the observed idle seconds and probe
    count, the plan/state/events paths and the hung turn's artifact paths from
    the last status payload, and the fixed :data:`HANG_NEXT_STEPS` guidance.
    ``supervised_child_pid``/``detached`` are attached only for self-launched
    chunks, whose ``asl run`` child stays alive (lock held, re-attachable)
    after the helper returns.
    """
    status_dict = status if isinstance(status, dict) and status.get("ok") else {}
    active_turn = status_dict.get("active_turn")
    if not isinstance(active_turn, dict):
        runner_state = status_dict.get("runner_state")
        if isinstance(runner_state, dict):
            active_turn = runner_state.get("active_turn")
    if not isinstance(active_turn, dict):
        active_turn = {}
    payload = {
        "class": escalation.get("class"),
        "hang_suspected": escalation.get("hang_suspected"),
        "idle_seconds": escalation.get("idle_seconds"),
        "probes": escalation.get("probes"),
        "plan_dir": status_dict.get("plan_dir") or parsed.get("plan_dir"),
        "state_path": status_dict.get("state_path"),
        "events_path": status_dict.get("events_path"),
        "artifact_paths": {
            "stdout": active_turn.get("stdout_path"),
            "stderr": active_turn.get("stderr_path"),
            "last_message": active_turn.get("last_message_path"),
            "result": active_turn.get("result_path"),
        },
        "next_steps": list(HANG_NEXT_STEPS),
    }
    if supervised_child_pid is not None:
        payload["supervised_child_pid"] = supervised_child_pid
    if detached is not None:
        payload["detached"] = detached
    return payload
