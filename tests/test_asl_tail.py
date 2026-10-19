"""Tail follower tests, split by rendering and following behavior."""

from .asl_tail_cases_events import TailFollowerEventsTests
from .asl_tail_cases_follow import TailFollowerFollowTests
from .asl_tail_cases_mailbox import TailFollowerMailboxTests
from .asl_tail_cases_recovery import TailFollowerRecoveryTests
from .asl_tail_follow_events_cases import FollowEventsTests

__all__ = [name for name in globals() if name.endswith("Tests")]
