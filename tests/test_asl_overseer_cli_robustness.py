"""Overseer leaves meeting hostile records, stores and inputs fail closed."""

from __future__ import annotations

import dataclasses
import io
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest import mock

from agent_session_loop import (
    overseer_cli_claims,
    overseer_cli_common,
    overseer_discovery,
    overseer_store_ledger,
    overseer_store_models,
    state,
)
from tests.asl_overseer_cli_fleet import _OverseerCase
from tests.asl_overseer_cli_support import WATCHER

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


class DomainRefusalTests(_OverseerCase):
    """A context or ledger refusal is an exit-2 envelope, never a traceback."""

    def _inspect(self, plan: str, *extra: str) -> tuple[int, dict[str, Any]]:
        return self.fleet.invoke(
            "inspect", plan, "--kind", "question_pending", "--summary", "s", *extra
        )

    def test_an_unknown_incident_is_refused_incident_unknown(self) -> None:
        self.watch()

        status, payload = self._inspect(str(self.plan), "--incident-id", "999")

        self.assertEqual(2, status, payload)
        self.assertEqual("incident_unknown", payload["reason"])

    def test_an_ineligible_plan_is_refused_plan_ineligible(self) -> None:
        dry = self.fleet.write_run("plan-dry", dry_run=True)

        status, payload = self._inspect(str(dry))

        self.assertEqual(2, status, payload)
        self.assertEqual("plan_ineligible", payload["reason"])

    def test_a_ledger_error_raised_anywhere_in_a_leaf_is_an_envelope(self) -> None:
        def refusing(**_: Any) -> overseer_store_ledger.Ledger:
            raise overseer_store_models.LedgerError("no ledger today")

        seams = dataclasses.replace(self.fleet.seams(), open_ledger=refusing)
        with mock.patch.object(self.fleet, "seams", return_value=seams):
            for argv in (
                ("watch", "--watcher-id", WATCHER),
                ("watch", "--watcher-id", WATCHER, "--release"),
                ("inspect", str(self.plan)),
                ("reconcile", str(self.plan)),
            ):
                with self.subTest(argv=argv):
                    status, payload = self.fleet.invoke(*argv)

                    self.assertEqual(2, status, payload)
                    self.assertEqual("ledger_error", payload["reason"])

    def test_a_blank_watcher_id_is_refused_by_the_parser(self) -> None:
        for value in ("", "   "):
            with self.subTest(value=value):
                with (
                    redirect_stderr(io.StringIO()),
                    redirect_stdout(io.StringIO()),
                    self.assertRaises(SystemExit) as caught,
                ):
                    self.fleet.parse("watch", "--watcher-id", value)

                self.assertEqual(2, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
