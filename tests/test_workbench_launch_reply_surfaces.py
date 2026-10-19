# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F401, F403, F405, F822, I001
"""Launch/reply parity tests across Workbench surfaces."""

# ruff: noqa: F401, F403, F405
from tests.workbench_launch_reply_support import *


class SurfaceParityTests(WorkbenchAPIFixture, unittest.TestCase):
    """The local route and the shared action are one gate, not two."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.install_workbench_api(Path(self._tmp.name))
        self.plan_dir = self.plans_root / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical = plans_files.canonicalize_path(self.plan_dir)
        self.server = self.start_workbench_server()

    def _post(self, route: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, payload, _ = _http_request(
            f"{self.base_url(self.server)}{route}",
            method="POST",
            body=body,
            headers=self.auth_headers(self.server),
        )
        return status, payload

    def _result(self) -> workbench_launch.LaunchResult:
        return workbench_launch.LaunchResult(
            pid=os.getpid(),
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical,
            profile_payload=workbench_registry_models.LaunchProfile().to_payload(),
            reply_provided=False,
            reply_length=None,
            reply_origin=None,
            state_dir=self.state_dir,
        )

    def test_a_retry_that_arrives_at_the_other_surface_does_not_spawn_again(
        self,
    ) -> None:
        """The failure this session is about: a lost response, then a retry.

        The connector submits, the answer never arrives, and the operator
        clicks the button in the browser instead. Two surfaces, one plan, one
        idempotency key -- and one process, because both surfaces admit through
        the same file.
        """
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=self._result()
        ) as patched:
            first = workbench_actions.launch_run(
                self.canonical, operation_id="shared-1", state_dir=self.state_dir
            )
            status, body = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "shared-1"},
            )
        patched.assert_called_once()
        self.assertFalse(first.replayed)
        self.assertEqual(200, status)
        self.assertTrue(body["replayed"])
        # The HTTP surface answered from the journal rather than inventing a
        # launch payload for a process it never started.
        self.assertIsNone(body["launch"])
        self.assertEqual("shared-1", body["operation"]["operation_id"])

    def test_a_route_launch_records_the_same_row_the_action_would(self) -> None:
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=self._result()
        ):
            status, body = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "route-1"},
            )
        self.assertEqual(200, status)
        self.assertEqual(operations.OPERATION_CONSUMED, body["operation"]["status"])
        self.assertEqual(
            operations.METHOD_LAUNCH,
            workbench_operations.read_operation(
                self.canonical, "route-1", state_dir=self.state_dir
            )["method"],
        )

    def test_the_route_still_refuses_a_plan_a_run_already_holds(self) -> None:
        """Admission is a gate in front of the ownership check, not instead."""
        _write_state_payload(
            self.canonical,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.canonical),
                "paused": False,
                "harness": "claude",
                "run_id": RUN_ID,
            },
        )
        _write_lock_file(
            self.canonical,
            self.state_dir,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        with mock.patch.object(workbench_launch, "launch_plan") as patched:
            status, body = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "route-1"},
            )
        self.assertEqual(409, status)
        self.assertEqual("run-already-active", body["reason"])
        patched.assert_not_called()
        record = workbench_operations.read_operation(
            self.canonical, "route-1", state_dir=self.state_dir
        )
        assert record is not None
        self.assertEqual(operations.OPERATION_CANCELLED, record["status"])
