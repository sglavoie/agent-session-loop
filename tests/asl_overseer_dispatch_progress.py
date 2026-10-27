# ruff: noqa: F403, F405
from .asl_overseer_dispatch_support import *


class ProgressTokenTests(unittest.TestCase):
    """What the token notices, stated as equalities rather than as prose."""

    def test_timestamps_are_not_in_the_token(self) -> None:
        self.assertEqual(
            overseer_dispatch.progress_token(_facts()),
            overseer_dispatch.progress_token(
                _facts(last_activity_at="2027-06-06T06:06:06+00:00")
            ),
        )

    def test_question_identity_is_not_in_the_token(self) -> None:
        self.assertEqual(
            overseer_dispatch.progress_token(_facts()),
            overseer_dispatch.progress_token(
                _facts(question_id="q-99", unanswered=("q-99",))
            ),
        )

    def test_a_new_turn_is_in_the_token(self) -> None:
        self.assertNotEqual(
            overseer_dispatch.progress_token(_facts()),
            overseer_dispatch.progress_token(_facts(turn_index=4)),
        )

    def test_a_new_session_is_in_the_token(self) -> None:
        self.assertNotEqual(
            overseer_dispatch.progress_token(_facts()),
            overseer_dispatch.progress_token(_facts(session_id="session-8")),
        )

    def test_a_paused_run_on_a_later_turn_is_in_the_token(self) -> None:
        """A real pause clears ``active_turn``; its last turn still moved."""

        def paused_on(turn_index: int) -> intervention.RunFacts:
            facts = _facts()
            return replace(
                facts,
                paused=True,
                run_active=False,
                turn_active=False,
                runner_state={
                    **facts.runner_state,
                    "paused": True,
                    "active_turn": None,
                    "last_turn": {"session_id": "session-7", "turn_index": turn_index},
                },
            )

        self.assertNotEqual(
            overseer_dispatch.progress_token(paused_on(3)),
            overseer_dispatch.progress_token(paused_on(4)),
        )

    def test_a_question_appearing_is_in_the_token(self) -> None:
        self.assertNotEqual(
            overseer_dispatch.progress_token(_facts()),
            overseer_dispatch.progress_token(_facts(unanswered=(QUESTION, "q-2"))),
        )

    def test_a_pause_is_in_the_token(self) -> None:
        paused = replace(_facts(), paused=True, run_active=False)
        self.assertNotEqual(
            overseer_dispatch.progress_token(_facts()),
            overseer_dispatch.progress_token(paused),
        )


if __name__ == "__main__":
    unittest.main()


__all__ = tuple(name for name in globals() if not name.startswith("__"))
