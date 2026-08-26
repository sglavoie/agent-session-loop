"""The shared state-machine mechanism, tested without a domain.

:mod:`agent_session_loop.transitions` knows nothing about verification or
review; what it owes its callers is that a declared table is well formed, that
an undeclared step raises instead of being silently allowed, and that a cursor
keeps the route it actually took. Those three properties are what the two lane
modules build their model tests on, so they are established here first.
"""

from __future__ import annotations

import unittest

from agent_session_loop.transitions import (
    IllegalTransitionError,
    MachineCursor,
    StateMachine,
)

TRAFFIC = StateMachine(
    name="traffic",
    initial="red",
    table={
        "red": {"go": "green"},
        "green": {"warn": "amber", "fault": "dark"},
        "amber": {"stop": "red"},
        "dark": {},
    },
)


class DeclarationTests(unittest.TestCase):
    def test_an_undeclared_initial_state_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as raised:
            StateMachine(name="m", initial="nope", table={"a": {}})
        self.assertIn("initial state", str(raised.exception))

    def test_a_transition_to_an_undeclared_state_is_rejected(self) -> None:
        # Without this, a typo in a target would produce a machine that raises
        # only on the path that happens to reach it.
        with self.assertRaises(ValueError) as raised:
            StateMachine(name="m", initial="a", table={"a": {"go": "b"}})
        self.assertIn("undeclared state", str(raised.exception))

    def test_states_and_events_are_reported_in_declaration_order(self) -> None:
        self.assertEqual(TRAFFIC.states, ("red", "green", "amber", "dark"))
        self.assertEqual(TRAFFIC.events, ("go", "warn", "fault", "stop"))

    def test_a_state_with_no_moves_is_terminal(self) -> None:
        self.assertEqual(TRAFFIC.terminal_states, frozenset({"dark"}))
        self.assertTrue(TRAFFIC.is_terminal("dark"))
        self.assertFalse(TRAFFIC.is_terminal("red"))


class EnumerationTests(unittest.TestCase):
    def test_legal_transitions_yields_every_declared_triple(self) -> None:
        self.assertEqual(
            sorted(TRAFFIC.legal_transitions()),
            sorted(
                [
                    ("red", "go", "green"),
                    ("green", "warn", "amber"),
                    ("green", "fault", "dark"),
                    ("amber", "stop", "red"),
                ]
            ),
        )

    def test_the_two_enumerations_partition_the_whole_grid(self) -> None:
        legal = {(state, event) for state, event, _ in TRAFFIC.legal_transitions()}
        illegal = set(TRAFFIC.illegal_transitions())
        self.assertEqual(legal & illegal, set())
        self.assertEqual(
            len(legal) + len(illegal), len(TRAFFIC.states) * len(TRAFFIC.events)
        )

    def test_every_event_of_a_terminal_state_is_illegal(self) -> None:
        self.assertEqual(
            {
                event
                for state, event in TRAFFIC.illegal_transitions()
                if state == "dark"
            },
            set(TRAFFIC.events),
        )


class AdvanceTests(unittest.TestCase):
    def test_a_declared_step_returns_its_target(self) -> None:
        self.assertEqual(TRAFFIC.advance("red", "go"), "green")

    def test_an_undeclared_event_raises_with_the_offending_step(self) -> None:
        with self.assertRaises(IllegalTransitionError) as raised:
            TRAFFIC.advance("red", "stop")
        error = raised.exception
        self.assertEqual(
            (error.machine, error.state, error.event), ("traffic", "red", "stop")
        )
        self.assertIn("no transition from state 'red' on event 'stop'", str(error))

    def test_an_undeclared_state_raises_rather_than_keyerror(self) -> None:
        with self.assertRaises(IllegalTransitionError):
            TRAFFIC.advance("purple", "go")


class CursorTests(unittest.TestCase):
    def test_a_new_cursor_starts_at_the_initial_state(self) -> None:
        cursor = TRAFFIC.start()
        self.assertIsInstance(cursor, MachineCursor)
        self.assertIs(cursor.machine, TRAFFIC)
        self.assertEqual(cursor.state, "red")
        self.assertEqual(cursor.history, ("red",))
        self.assertFalse(cursor.settled)

    def test_the_history_is_the_route_taken_not_the_route_declared(self) -> None:
        cursor = TRAFFIC.start()
        cursor.advance("go")
        cursor.advance("warn")
        cursor.advance("stop")
        self.assertEqual(cursor.history, ("red", "green", "amber", "red"))

    def test_a_refused_step_leaves_the_cursor_where_it_was(self) -> None:
        cursor = TRAFFIC.start()
        with self.assertRaises(IllegalTransitionError):
            cursor.advance("warn")
        self.assertEqual(cursor.state, "red")
        self.assertEqual(cursor.history, ("red",))

    def test_two_cursors_over_one_machine_do_not_share_position(self) -> None:
        first, second = TRAFFIC.start(), TRAFFIC.start()
        first.advance("go")
        self.assertEqual((first.state, second.state), ("green", "red"))

    def test_a_settled_cursor_reports_itself_settled(self) -> None:
        cursor = TRAFFIC.start()
        cursor.advance("go")
        cursor.advance("fault")
        self.assertTrue(cursor.settled)


if __name__ == "__main__":
    unittest.main()
