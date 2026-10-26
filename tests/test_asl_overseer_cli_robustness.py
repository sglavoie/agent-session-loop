"""Overseer leaves meeting hostile records, stores and inputs fail closed."""

from __future__ import annotations

import io
import shutil
import unittest
from typing import Any
from unittest import mock

from agent_session_loop import (
    overseer_cli_claims,
    overseer_cli_common,
    overseer_discovery,
    state,
)
from tests.asl_overseer_cli_fleet import _OverseerCase

ASL_TEST_PORTABLE = True


class PlanResolutionTests(_OverseerCase):
    """A plan resolves to the record that proves it is that plan's."""

    def _copy_state_aside(self, name: str) -> None:
        own = state.plan_state_dir(self.plan, self.fleet.state_root)
        shutil.copytree(own, own.parent / name)

    def test_a_stray_copy_sorting_first_does_not_shadow_the_real_record(
        self,
    ) -> None:
        self._copy_state_aside("0" * 64)

        self.watch()
        payload = self.inspect()

        self.assertEqual(f"{self.plan.name}-run-1", payload["run_id"])

    def test_two_records_proving_one_plan_are_refused_as_ambiguous(self) -> None:
        own = state.plan_state_dir(self.plan, self.fleet.state_root)
        inventory = overseer_discovery.build_inventory(state_dir=self.fleet.state_root)
        (candidate,) = inventory.candidates
        twin = overseer_discovery.Inventory(
            state_root=inventory.state_root,
            candidates=(candidate, candidate),
            warnings=(),
            complete=True,
        )

        with self.assertRaises(overseer_cli_common.OverseerCliError) as caught:
            overseer_cli_claims._resolve_candidate(twin, str(self.plan))

        self.assertEqual("plan_identity_ambiguous", caught.exception.reason)
        self.assertIn(str(own), caught.exception.message)


class ClaimsInputTests(_OverseerCase):
    """Claims of the wrong shape or encoding are a usage error, not a crash."""

    def _decide(self, *extra: str) -> tuple[int, dict[str, Any]]:
        return self.fleet.invoke(
            "decide", str(self.plan), "--operation", "guidance", *extra
        )

    def _refused(self, status: int, payload: dict[str, Any]) -> None:
        self.assertEqual(2, status, payload)
        self.assertEqual("claims_invalid", payload["reason"])

    def test_a_non_utf8_claims_file_is_refused(self) -> None:
        path = self.fleet.root / "claims-bad.json"
        path.write_bytes(b'[{"statement_id": "\xff"}]')

        self._refused(*self._decide("--claims-file", str(path)))

    def test_non_utf8_claims_on_stdin_are_refused(self) -> None:
        stdin = io.TextIOWrapper(io.BytesIO(b"[\xff]"), encoding="utf-8")
        with mock.patch("sys.stdin", stdin):
            self._refused(*self._decide("--claims-stdin"))

    def test_claims_nested_too_deeply_are_refused(self) -> None:
        path = self.fleet.root / "claims-deep.json"
        path.write_text("[" * 1_000_000, encoding="utf-8")

        self._refused(*self._decide("--claims-file", str(path)))

    def test_operations_and_corroborating_must_be_lists_of_strings(self) -> None:
        for key, value in (
            ("operations", 7),
            ("operations", "guidance"),
            ("operations", [1]),
            ("corroborating", 7),
            ("corroborating", "abc"),
        ):
            with self.subTest(key=key, value=value):
                claim = {
                    "statement_id": "s1",
                    "topic": "operations",
                    "stance": "allow",
                    key: value,
                }
                path = self.fleet.claims_file(self, [claim])

                status, payload = self._decide("--claims-file", path)

                self._refused(status, payload)
                self.assertIn(key, payload["error"])


if __name__ == "__main__":
    unittest.main()
