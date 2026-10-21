"""``asl install-verify`` Agent Mux checks."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from agent_session_loop import (
    agent_mux_projection,
    install_verify,
    install_verify_agent_mux,
    install_verify_common,
)


class InstallVerifyAgentMuxTests(unittest.TestCase):
    """Paired-cockpit health is reported, and never required."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def _checks(self, report: Any, *, active_asl: str = "/usr/bin/asl") -> list[dict]:
        def _fake_doctor(path_env: Any, home: Any) -> tuple[str, Any]:
            return "/usr/bin/amx", report

        original = install_verify_agent_mux._amx_doctor
        install_verify_agent_mux._amx_doctor = _fake_doctor
        self.addCleanup(setattr, install_verify_agent_mux, "_amx_doctor", original)
        return install_verify_agent_mux._check_agent_mux(
            path_env=None, home=self.tmp_path, active_asl=active_asl
        )

    def test_missing_amx_is_a_passing_advisory(self) -> None:
        """ASL must install and verify cleanly with no cockpit present."""
        checks = install_verify_agent_mux._check_agent_mux(
            path_env=str(self.tmp_path), home=self.tmp_path, active_asl=None
        )
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0]["ok"])
        self.assertTrue(checks[0]["advisory"])

    def test_healthy_pairing_passes(self) -> None:
        checks = self._checks(
            {
                "schema_version": 2,
                "healthy": True,
                "daemon": {
                    "asl": {
                        "mode": "required",
                        "available": True,
                        "mutations_enabled": True,
                        "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                    }
                },
            }
        )
        self.assertTrue(all(check["ok"] for check in checks), checks)
        self.assertTrue(all(check["advisory"] for check in checks))

    def test_unreachable_bridge_is_reported_with_its_reason(self) -> None:
        checks = self._checks(
            {
                "schema_version": 2,
                "healthy": False,
                "daemon": {
                    "asl": {
                        "mode": "auto",
                        "available": False,
                        "reason": 'exec: "asl": executable file not found in $PATH',
                    }
                },
            }
        )
        bridge = next(c for c in checks if c["name"] == "agent-mux-bridge")
        self.assertFalse(bridge["ok"])
        self.assertIn("executable file not found", bridge["detail"])

    def test_disabled_integration_is_not_a_complaint(self) -> None:
        checks = self._checks(
            {
                "schema_version": 2,
                "healthy": True,
                "daemon": {"asl": {"mode": "disabled", "available": False}},
            }
        )
        bridge = next(c for c in checks if c["name"] == "agent-mux-bridge")
        self.assertTrue(bridge["ok"])

    def test_contract_mismatch_is_named(self) -> None:
        checks = self._checks(
            {
                "schema_version": 2,
                "healthy": True,
                "daemon": {
                    "asl": {
                        "mode": "required",
                        "available": True,
                        "contract_set_id": "asl-amx-1.deadbeefdeadbeef",
                    }
                },
            }
        )
        mismatch = next(c for c in checks if c["name"] == "agent-mux-contract")
        self.assertFalse(mismatch["ok"])
        self.assertIn("asl-amx-1.deadbeefdeadbeef", mismatch["detail"])

    def test_daemon_resolving_a_different_asl_is_named(self) -> None:
        """Neither side can see this failure alone, which is why it is checked."""
        checks = self._checks(
            {
                "schema_version": 2,
                "healthy": True,
                "daemon": {
                    "asl": {
                        "mode": "required",
                        "available": True,
                        "executable": "/opt/other/bin/asl",
                        "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                    }
                },
            },
            active_asl="/usr/bin/asl",
        )
        drift = next(c for c in checks if c["name"] == "agent-mux-executable")
        self.assertFalse(drift["ok"])
        self.assertIn("/opt/other/bin/asl", drift["detail"])
        self.assertIn("asl-sync", drift["detail"])

    def test_absent_asl_block_asks_for_pairing(self) -> None:
        checks = self._checks({"schema_version": 2, "healthy": True, "daemon": {}})
        bridge = next(c for c in checks if c["name"] == "agent-mux-bridge")
        self.assertFalse(bridge["ok"])
        self.assertIn("asl-sync", bridge["detail"])

    def test_unreadable_doctor_report_is_advisory_not_fatal(self) -> None:
        checks = self._checks(OSError("boom"))
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0]["ok"])
        self.assertTrue(checks[0]["advisory"])


class InstallVerifyPromotionTests(unittest.TestCase):
    """``--require-agent-mux`` is the only way an advisory fails the run."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self._stub_checks()

    def _stub_checks(self) -> None:
        """Reduce verification to one failing advisory and one passing check."""
        originals = {
            name: getattr(install_verify, name)
            for name in (
                "_check_asl_path",
                "_check_skill_links",
                "_check_session_run_content",
                "_check_startup_context",
                "_check_asl_skill",
                "_check_status_smoke",
                "_check_agent_mux",
            )
        }
        for name, original in originals.items():
            self.addCleanup(setattr, install_verify, name, original)
        install_verify._check_asl_path = lambda *_: (
            "/usr/bin/asl",
            install_verify_common.success("asl-path", "ok"),
        )
        for name in (
            "_check_skill_links",
            "_check_session_run_content",
            "_check_startup_context",
            "_check_asl_skill",
        ):
            setattr(install_verify, name, lambda *_a, **_k: [])
        install_verify._check_status_smoke = lambda **_: install_verify_common.success(
            "status-smoke", "ok"
        )
        install_verify._check_agent_mux = lambda **_: [
            install_verify_common.advisory("agent-mux-bridge", "no bridge")
        ]

    def test_advisory_failure_does_not_fail_verification_by_default(self) -> None:
        payload = install_verify.verify_install(home=self.home)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["failures"], [])
        self.assertEqual(len(payload["advisories"]), 1)
        self.assertFalse(payload["require_agent_mux"])

    def test_require_flag_promotes_the_advisory_to_a_failure(self) -> None:
        payload = install_verify.verify_install(home=self.home, require_agent_mux=True)
        self.assertFalse(payload["ok"])
        self.assertEqual(len(payload["failures"]), 1)
        self.assertEqual(payload["advisories"], [])
        self.assertTrue(payload["require_agent_mux"])

    def test_text_output_distinguishes_warn_from_fail(self) -> None:
        payload = install_verify.verify_install(home=self.home)
        out = io.StringIO()
        with redirect_stdout(out):
            install_verify._print_text(payload)
        text = out.getvalue()
        self.assertIn("warn agent-mux-bridge", text)
        self.assertNotIn("FAIL agent-mux-bridge", text)

    def test_require_flag_is_exposed_on_the_parser(self) -> None:
        args = install_verify.build_parser().parse_args(["--require-agent-mux"])
        self.assertTrue(args.require_agent_mux)
        self.assertFalse(install_verify.build_parser().parse_args([]).require_agent_mux)


if __name__ == "__main__":
    unittest.main()
