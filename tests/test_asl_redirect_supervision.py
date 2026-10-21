"""What supervision is allowed to decide on its own, and what it must hand back.

The autopilot helper exists to keep a plan moving without a person watching it.
Redirect and conversation recovery are the two places where that goal and the
plan's safety point in opposite directions: both look like an ordinary pause,
and both would be *resolved* by relaunching -- which is exactly the move that
loses an agent's saved reasoning or starts a second worker beside one still
running. So this module pins the boundary from both sides at once, because a
regression on either side reads as the same green suite:

* the ordinary retries still work. ``timeout`` and ``transport-failed`` stay
  eligible, keep the same two-attempt budget, and keep the same 5s/30s backoff,
  since a safety change that quietly stopped retrying transient failures would
  be a supervision loop that no longer supervises;
* the two recovery pauses stop, every time, and spend nothing. They are not in
  ``pause_policy.RECOVERABLE_PAUSE_REASONS``, so no retry budget is consumed
  and none can be exhausted -- a plan that hits one twice is in the same
  position the second time as the first;
* the stop is not silent. A recovery pause surfaces which of the two decisions
  is being handed over, the command that reads its evidence, and the exact
  one-use relaunch that authorizes a fresh conversation when one exists;
* ``--fresh-conversation`` cannot be supervised at all. Two independent layers
  refuse it: the command is rejected when it is parsed, and the flag is dropped
  when child argv is built. One authorization, used once, by the person who
  gave it;
* uncertain evidence stays uncertain. A correction delivered to a worker that
  never receipted it is never carried automatically, and a selection state that
  is not one of the two proceeding states requires a user;
* a stop stays stopped. ``stop-after-current`` and ``abort`` end supervision
  cleanly rather than being relaunched into the work the operator just stopped;
* and urgent redirect is documented for an explicitly authorized supervisor
  without becoming a standing fleet authority.
"""

from .asl_redirect_supervision_cases_recovery import (
    PreservedRetryPolicyTests,
    RecoveryDecisionSurfacedTests,
    RecoveryPausesSpendNothingTests,
)
from .asl_redirect_supervision_cases_stops import (
    ArgumentParsingHygieneTests,
    FreshConversationIsNotSupervisableTests,
    StopsStayStoppedTests,
    SupervisorPolicySurfaceTests,
    UncertainEvidenceRequiresAUserTests,
)

__all__ = [name for name in globals() if name.endswith("Tests")]
