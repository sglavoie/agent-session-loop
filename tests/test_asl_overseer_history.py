"""Focused tests for the ASL side of AgentMux's transcript-evidence contract.

``amx`` is never started here. Every test drives the client through its injected
``resolve_binary``/``run_command`` seams, which is the only way to reach the
cases that matter: the command missing, hanging, crashing, or answering
something other than what was asked.

Three properties are proved, and none of them is the happy path.

*Unavailable is never permitted.* A parametric sweep drives every transport and
protocol failure the module names and asserts the same five things of each: not
allowed, not available, no grant, no identities, and no conversations. The sweep
is driven from :data:`overseer_history.FAILURE_REASONS` itself and asserts it
covered every entry, so a failure mode added later without a test fails this
file rather than passing silently.

*An answer is evidence for one question, taken under stated bounds.* A
well-formed, internally consistent, ``allowed`` payload is rewritten one field
at a time -- the plan, the access category, a repository, the exit status, and
each of the three bounds -- and every mutation must turn a permitted answer into
a refused one. The bound cases are the ones with teeth: an answer carrying more
conversations, more messages, or longer excerpts than were asked for is refused
even though every other field agrees, because a bound that was reported is not a
bound that was applied.

*A refusal does not describe what it withheld.* A withheld entry that names the
path, directory, or project of the conversation it hid is rejected outright
rather than parsed past, and the answer carrying it yields nothing.
"""

from .asl_overseer_history_cases_mismatch import (
    HistoryMalformedAnswerTests,
    HistoryMismatchTests,
)
from .asl_overseer_history_cases_request import (
    HistoryAnswerTests,
    HistoryRequestTests,
    HistoryUnavailableTests,
)

ASL_TEST_PORTABLE = True

if __name__ == "__main__":
    import unittest

    unittest.main()
