# mypy: ignore-errors
# ruff: noqa: I001
"""Turn-level conversation continuity: what a turn resumes, and what it refuses.

``tests/test_asl_conversations.py`` covers the durable ledger in isolation --
what a record is, how it is appended, what a lookup may answer. These tests
cover the layer above it: the policy that turns a pending session plus a harness
into exactly one of continue / open / stop, and the wiring that carries that
decision into the adapter and back out into the ledger.

Two properties are load-bearing enough to be tested from more than one angle.
The first is that the intended conversation is restored -- keyed by session and
harness, so distinct plan sessions and generated repair children never reach
each other's. The second is that ``--fresh-conversation`` authorizes exactly one
fresh start: not the next turn, not the next session, not a supervisor retry,
and not a ``--resume`` that rehydrates flags from disk. The leak surfaces have
one test each, because each covers a replay the others cannot.
"""

from ._test_asl_conversation_resume_planning import *  # noqa: F403
from ._test_asl_conversation_resume_fresh_start import *  # noqa: F403
from ._test_asl_conversation_resume_turn_outcomes import *  # noqa: F403
from ._test_asl_conversation_resume_support import *  # noqa: F403


ASL_TEST_PORTABLE = True


if __name__ == "__main__":
    import unittest

    unittest.main()
