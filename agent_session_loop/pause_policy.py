"""Single source of truth for pause classification.

Every consumer that needs to know whether a paused run can be safely
relaunched, whether it needs a human, how many same-session retries it gets,
and how long to back off before each retry reads it from here. Payloads carry a
serialized :func:`classify_pause` block so downstream tooling (the ``asl``
supervisor helper, ``asl status``, ``asl questions``) stops hardcoding the
reason sets.

The reason sets compose from the atomic pause-reason constants in
:mod:`agent_session_loop.events`, which owns the raw vocabulary
(``PAUSE_REASON_*``) and the classifier's retryable-harness-failure set. This
module owns the higher-level policy tuples that used to be duplicated in the
supervisor helper.
"""

from __future__ import annotations

from typing import Any

from . import events

# Pause reasons the supervisor treats as transient and same-session retryable.
# ``verify-failed``, ``timeout``, and ``no-status-change`` are cases where
# relaunching the same session can succeed without any human answer, and
# ``transport-failed`` (``events.PAUSE_REASON_TRANSPORT_FAILED``) is the only
# harness-failure *category* the classifier marks retryable
# (``events.RETRYABLE_HARNESS_FAILURE_REASONS``). Unclassified ``nonzero-exit``
# is fail-closed (never retried), and the quota/auth/invalid-model categories
# are deliberately absent so quota and session-limit exhaustion are never
# relaunched. Every other pause needs a human: a reply when an answer clears it,
# otherwise an inspection (:data:`INSPECT_PAUSE_REASONS`).
RECOVERABLE_PAUSE_REASONS: tuple[str, ...] = (
    "verify-failed",
    events.PAUSE_REASON_TRANSPORT_FAILED,
    "timeout",
    "no-status-change",
)

# Non-retryable harness-failure categories that always require human
# intervention and carry no useful question/answer context: quota /
# session-limit exhaustion, authentication failures, and invalid-model
# rejections. Consumers surface these as ``user_required`` — carrying any
# parsed reset information — and deliberately skip ``asl questions``, since no
# reply can clear them.
USER_REQUIRED_HARNESS_FAILURE_REASONS: tuple[str, ...] = (
    events.PAUSE_REASON_QUOTA_EXHAUSTED,
    events.PAUSE_REASON_AUTH_FAILED,
    events.PAUSE_REASON_INVALID_MODEL,
)

# Pauses blocked on a workspace or environment precondition rather than on a
# missing answer. The dividing principle: a pause is ``reply`` iff an operator's
# mailbox answer can make the run proceed. None of these clear that way — a
# dirty worktree stays dirty however the operator answers — so waiting for a
# reply only burns the mailbox lease before escalating anyway.
#
# Membership is justified per reason by that reason's own clearing path, never
# by symmetry with a neighbour:
#
# - ``commit-preflight`` / ``commit-failed``: the working tree (or the commit
#   harness) has to change on disk before the same gate can pass.
# - ``repository-registry-required``: the plan must declare a ``## Repositories``
#   registry and re-derive its workspace binding — a plan edit plus
#   ``asl register-repositories``, not an answer.
# - ``workspace-repository-contention``: another live run already claims the
#   repository; one of the two processes has to stop.
# - ``not-loop-ready``: preflight has to reach ``loop-ready``, which means
#   repairing the plan or the environment its findings name.
# - ``required-checks-incomplete`` / ``review-not-clear``: the terminal
#   adjudicator refused a success the run had not earned. Relaunching with the
#   same arguments re-derives the same blocker from the same append-only
#   evidence, so these are never machine-retryable; the gates have to pass or
#   the review has to come back all-clear first.
ENVIRONMENT_BLOCKED_PAUSE_REASONS: tuple[str, ...] = (
    "commit-preflight",
    "commit-failed",
    events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
    events.PAUSE_REASON_WORKSPACE_REPOSITORY_CONTENTION,
    "not-loop-ready",
    events.PAUSE_REASON_REQUIRED_CHECKS_INCOMPLETE,
    events.PAUSE_REASON_REVIEW_NOT_CLEAR,
)

# Every pause an operator must inspect rather than answer, from either source:
# a harness failure carrying no question context, or a blocked precondition.
INSPECT_PAUSE_REASONS: tuple[str, ...] = (
    *USER_REQUIRED_HARNESS_FAILURE_REASONS,
    *ENVIRONMENT_BLOCKED_PAUSE_REASONS,
)

# What a human is supposed to do about a pause. Each pause carries exactly one:
# ``reply`` when an operator answer can clear it, ``inspect`` when no answer can
# and the environment has to change instead, ``None`` when the pause is
# recoverable and no human is needed at all.
USER_ACTION_REPLY = "reply"
USER_ACTION_INSPECT = "inspect"

# Bumped whenever the block's key set or the meaning of a key changes, so a
# consumer diffing a persisted block against a fresh classification can tell a
# schema difference from a real contradiction. v1 blocks carry no version and
# classify ``requires_user`` as the harness-failure set alone. v2 classified
# every non-recoverable pause outside the harness-failure set as ``reply``; v3
# reserves ``reply`` for pauses an answer can actually clear and routes
# :data:`ENVIRONMENT_BLOCKED_PAUSE_REASONS` to ``inspect``, so the meaning of
# ``user_action`` changed for reasons that already ship.
POLICY_VERSION = 3

# How many times a supervisor relaunches the same session for a given
# recoverable pause reason before giving up and surfacing the pause.
DEFAULT_MAX_RETRIES = 2

# Bounded backoff schedule (seconds) applied before each relaunch of a
# transient ``transport-failed`` pause. The classifier marks only
# ``transport-failed`` retryable, so the same session is relaunched at most
# ``len(TRANSPORT_RETRY_DELAYS_SECONDS)`` times, sleeping the scheduled delay
# first (5s, then 30s). The other recoverable pauses keep their immediate,
# zero-delay retry.
TRANSPORT_RETRY_DELAYS_SECONDS: tuple[float, ...] = (5.0, 30.0)


def classify_pause(reason: str | None) -> dict[str, Any]:
    """Classify a pause ``reason`` into a serializable policy block.

    Returns a bounded dict describing how the pause should be handled:

    - ``reason``: the pause reason, echoed back for consumers that persist the
      block next to a payload keyed by something else;
    - ``policy_version``: :data:`POLICY_VERSION`, so a consumer diffing a
      persisted block can tell a schema difference from a contradiction;
    - ``recoverable``: whether the same session may be relaunched without a
      human answer (``reason`` is in :data:`RECOVERABLE_PAUSE_REASONS`);
    - ``requires_user``: whether a human has to act at all. A pause ASL cannot
      prove a machine can retry needs one, so this is the negation of
      ``recoverable`` rather than a set membership;
    - ``user_action``: what that human does — :data:`USER_ACTION_REPLY` when an
      operator answer can clear the pause, :data:`USER_ACTION_INSPECT` when none
      can and the environment has to change instead
      (:data:`INSPECT_PAUSE_REASONS`: the non-retryable harness failures that
      carry no useful question context, plus the pauses blocked on a workspace
      precondition), ``None`` when recoverable;
    - ``max_retries``: the same-session retry budget (``DEFAULT_MAX_RETRIES``
      when recoverable, else ``0``);
    - ``backoff_seconds``: the per-retry backoff schedule — the
      ``transport-failed`` schedule (5s, 30s) for that reason, else an empty
      list (the other recoverable pauses relaunch immediately).
    """
    recoverable = reason in RECOVERABLE_PAUSE_REASONS
    if recoverable:
        user_action: str | None = None
    elif reason in INSPECT_PAUSE_REASONS:
        user_action = USER_ACTION_INSPECT
    else:
        user_action = USER_ACTION_REPLY
    if reason == events.PAUSE_REASON_TRANSPORT_FAILED:
        backoff_seconds = list(TRANSPORT_RETRY_DELAYS_SECONDS)
    else:
        backoff_seconds = []
    return {
        "policy_version": POLICY_VERSION,
        "reason": reason,
        "recoverable": recoverable,
        "requires_user": not recoverable,
        "user_action": user_action,
        "max_retries": DEFAULT_MAX_RETRIES if recoverable else 0,
        "backoff_seconds": backoff_seconds,
    }
