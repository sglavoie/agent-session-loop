"""Workbench server tests: registry pin and unpin endpoints.

Split from tests/test_asl_workbench_server.py."""

from __future__ import annotations

from agent_session_loop import (
    workbench,
)
from tests.asl_workbench_server_support import (
    WorkbenchServerTestCase,
)
from tests.support import (
    _http_request,
)


class WorkbenchServerRegistryTests(WorkbenchServerTestCase):
    """Registry pin and unpin endpoints."""

    def test_registry_get_returns_empty_when_unset(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/registry")
        self.assertEqual(200, status)
        self.assertEqual([], body["registry"]["pins"])
        self.assertEqual([], body["registry"]["plan_roots"])

    def test_registry_pin_persists_to_disk(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "label": "Alpha plan",
                "profile": {"harness": "codex", "timeout": 600},
            },
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        reloaded = workbench.load_registry(self.registry_path)
        self.assertEqual(1, len(reloaded.pins))
        self.assertEqual("Alpha plan", reloaded.pins[0].label)
        self.assertEqual("codex", reloaded.pins[0].profile.harness)
        self.assertEqual(600, reloaded.pins[0].profile.timeout)

    def test_registry_pin_rejects_negative_review_fix_cap_before_persisting(
        self,
    ) -> None:
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "profile": {"max_review_fix_cycles": -1},
            },
            headers=self._auth(server),
        )

        self.assertEqual(400, status)
        self.assertFalse(body["ok"])
        self.assertEqual("invalid-request", body["reason"])
        self.assertIn("max_review_fix_cycles", body["error"])
        reloaded = workbench.load_registry(self.registry_path)
        self.assertEqual((), reloaded.pins)

    def test_registry_pin_names_non_integer_review_fix_cap_in_error(self) -> None:
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "profile": {"max_review_fix_cycles": "two"},
            },
            headers=self._auth(server),
        )

        self.assertEqual(400, status)
        self.assertFalse(body["ok"])
        self.assertEqual("invalid-request", body["reason"])
        self.assertEqual(
            "profile max_review_fix_cycles must be an integer when set",
            body["error"],
        )

    def test_registry_unpin_removes_pin(self) -> None:
        seeded = workbench.upsert_pin(
            workbench.load_registry(self.registry_path),
            self.canonical_plan_dir,
            label="seed",
        )
        workbench.save_registry(seeded, self.registry_path)
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/unpin",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )

        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual([], body["registry"]["pins"])
        reloaded = workbench.load_registry(self.registry_path)
        self.assertEqual((), reloaded.pins)

    def test_registry_pin_round_trip_preserves_every_profile_field(
        self,
    ) -> None:
        # The frontend profile editor reads-modifies-writes pin payloads.
        # Confirm every dataclass field survives a POST + GET round trip
        # so the detail view does not silently drop operator settings.
        server = self._start_server()
        full_profile_body = {
            "cwd": "/tmp/cwd",
            "harness": "codex",
            "harness_model": "o3-mini",
            "effort": "medium",
            "timeout": 1234,
            "commit_mode": "per-session",
            "review_harness": "claude",
            "review_harness_model": "claude-sonnet",
            "max_review_fix_cycles": 3,
            "unsafe_bypass": True,
            "allow_not_loop_ready": True,
            "note": "scratch profile",
        }
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "label": "Round Trip",
                "profile": full_profile_body,
            },
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])

        status, body, _ = _http_request(f"{self._base_url(server)}/api/registry")
        self.assertEqual(200, status)
        pins = body["registry"]["pins"]
        self.assertEqual(1, len(pins))
        pin = pins[0]
        self.assertEqual("Round Trip", pin["label"])
        profile = pin["profile"]
        for key, expected in full_profile_body.items():
            self.assertEqual(
                expected,
                profile.get(key),
                msg=f"profile field {key!r} did not round-trip",
            )
        self.assertNotIn("model", profile)

    def test_registry_pin_refuses_an_unpermitted_commit_mode(self) -> None:
        # `commit_mode` is a two-value closed set, so an unrecognized value is
        # a malformed request rather than a preference the launch can resolve.
        # Refusing it here is what keeps a pin from storing a mode no launch
        # would ever honour.
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "profile": {"commit_mode": "sometimes"},
            },
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertIn("commit_mode", body["error"])

    def test_registry_pin_accepts_an_absent_commit_mode(self) -> None:
        # Absence is not a wrong value: a profile that never names a mode must
        # still round-trip as `null` rather than being refused with it.
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "profile": {"commit_mode": None, "note": "no preference"},
            },
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        status, body, _ = _http_request(f"{self._base_url(server)}/api/registry")
        self.assertEqual(200, status)
        self.assertIsNone(body["registry"]["pins"][0]["profile"]["commit_mode"])

    def test_registry_pin_round_trip_preserves_nullable_boolean_states(
        self,
    ) -> None:
        # Nullable launch booleans must survive a POST + GET round trip in
        # each of their three states (null / false / true). A blank launch
        # profile that records explicit ``None`` must not silently coerce
        # to ``False`` because the runner would then emit
        # ``--no-unsafe-bypass`` and override the harness default.
        server = self._start_server()
        cases = (
            ("explicit-null", None, None),
            ("explicit-false", False, False),
            ("explicit-true", True, True),
        )
        for label, unsafe_value, allow_value in cases:
            profile_body = {
                "unsafe_bypass": unsafe_value,
                "allow_not_loop_ready": allow_value,
            }
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/registry/pin",
                method="POST",
                body={
                    "plan_dir": str(self.canonical_plan_dir),
                    "label": label,
                    "profile": profile_body,
                },
                headers=self._auth(server),
            )
            self.assertEqual(200, status, msg=f"case={label}")
            self.assertTrue(body["ok"], msg=f"case={label}")

            status, body, _ = _http_request(f"{self._base_url(server)}/api/registry")
            self.assertEqual(200, status, msg=f"case={label}")
            pins = body["registry"]["pins"]
            self.assertEqual(1, len(pins), msg=f"case={label}")
            profile = pins[0]["profile"]
            # ``profile.get`` would conflate a missing key with ``None``;
            # assert the key is present so a future regression that drops
            # the field entirely cannot pass silently.
            self.assertIn("unsafe_bypass", profile, msg=f"case={label}")
            self.assertIn("allow_not_loop_ready", profile, msg=f"case={label}")
            self.assertIs(
                unsafe_value,
                profile["unsafe_bypass"],
                msg=f"case={label} unsafe_bypass",
            )
            self.assertIs(
                allow_value,
                profile["allow_not_loop_ready"],
                msg=f"case={label} allow_not_loop_ready",
            )
