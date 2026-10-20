"""Surface parity: service, HTTP routes and CLI land in the same place.

**One service, several spellings.** :class:`SurfaceParityTests` drives the
shared service, the HTTP routes, and the ``asl workbench`` CLI over the same
registry and asserts they land in the same place: the same serialized write, the
same refusal reason, and -- for the historic ``/api/plan/delete`` route -- the
same archive the modern route produces.
"""

from __future__ import annotations

import argparse
import io
import os
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agent_session_loop import (
    workbench_archive,
    workbench_registry,
    workbench_server,
)
from asl_workbench import archive as portable_archive

from .support import (
    _http_request,
    _write_lock_file,
    patched_env,
)
from .workbench_archive_support import ArchiveFixture


class SurfaceParityTests(ArchiveFixture):
    """The HTTP routes and the CLI are renderings of the one service."""

    def setUp(self) -> None:
        super().setUp()
        self.servers: list[workbench_server.WorkbenchHTTPServer] = []

    def start_server(self) -> workbench_server.WorkbenchHTTPServer:
        server = workbench_server.create_server(
            host="127.0.0.1",
            port=0,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            plan_roots=(),
            include_default_dirs=False,
            token=None,
        )
        thread = threading.Thread(
            target=server.serve_forever, name="workbench-archive-test", daemon=True
        )
        thread.start()

        def _shutdown() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_shutdown)
        self.servers.append(server)
        return server

    def url(self, server: workbench_server.WorkbenchHTTPServer, path: str) -> str:
        return f"http://{server.bound_host}:{server.bound_port}{path}"

    def auth(self, server: workbench_server.WorkbenchHTTPServer) -> dict[str, str]:
        return {"Authorization": f"Bearer {server.token}"}

    def test_the_historic_delete_route_archives_exactly_as_the_modern_one_does(
        self,
    ) -> None:
        # `/api/plan/delete` predates the archive. Keeping it mapped here is
        # what stops an old bookmark or an old script from meaning "destroy".
        second = self.make_plan("beta")
        server = self.start_server()

        status_legacy, legacy, _ = _http_request(
            self.url(server, "/api/plan/delete"),
            method="POST",
            body={
                "plan_dir": str(self.canonical),
                "confirm_plan_dir": str(self.canonical),
            },
            headers=self.auth(server),
        )
        status_modern, modern, _ = _http_request(
            self.url(server, "/api/plan/archive"),
            method="POST",
            body={"plan_dir": str(second), "confirm_plan_dir": str(second)},
            headers=self.auth(server),
        )

        self.assertEqual(200, status_legacy, legacy)
        self.assertEqual(200, status_modern, modern)
        # Both produced an archive entry rather than a removal: the plans are
        # gone from their origins and restorable from the archive.
        self.assertEqual(sorted(legacy.keys()), sorted(modern.keys()))
        self.assertTrue(legacy["plan_archived"])
        self.assertEqual(
            2, len(workbench_archive.list_archive_entries(self.state_dir).entries)
        )
        workbench_archive.restore_entry(
            legacy["archive_id"],
            state_dir=self.state_dir,
            registry_path=self.registry_path,
        )
        self.assertTrue(self.canonical.exists())

    def test_the_legacy_route_keeps_its_confirmation_contract(self) -> None:
        server = self.start_server()

        status, body, _ = _http_request(
            self.url(server, "/api/plan/delete"),
            method="POST",
            body={
                "plan_dir": str(self.canonical),
                "confirm_plan_dir": str(self.tmp_path),
            },
            headers=self.auth(server),
        )

        self.assertEqual(400, status)
        self.assertEqual("confirmation-mismatch", body["reason"])
        self.assertTrue(self.canonical.exists())

    def test_a_refusal_carries_the_same_reason_and_the_status_its_scope_earns(
        self,
    ) -> None:
        _write_lock_file(self.canonical, self.state_dir, pid=os.getpid())
        server = self.start_server()

        status, body, _ = _http_request(
            self.url(server, "/api/plan/archive"),
            method="POST",
            body={
                "plan_dir": str(self.canonical),
                "confirm_plan_dir": str(self.canonical),
            },
            headers=self.auth(server),
        )
        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.archive_one(
                str(self.canonical),
                str(self.canonical),
                state_dir=self.state_dir,
                registry_path=self.registry_path,
                include_default_dirs=False,
            )

        self.assertEqual(ctx.exception.reason, body["reason"])
        self.assertEqual(ctx.exception.status, status)
        self.assertEqual(portable_archive.status_for_scope(ctx.exception.scope), status)

    def test_the_route_and_the_cli_pin_into_the_same_registry(self) -> None:
        # The two surfaces reach the registry by different routes -- the server
        # holds an explicit path, the CLI resolves the default -- so the test
        # points them both at one file and checks the second pin did not land
        # on top of the first. That is the serialization claim in the small.
        config_home = self.tmp_path / "config"
        with patched_env(XDG_CONFIG_HOME=str(config_home)):
            registry_path = workbench_registry.default_registry_path()
            self.registry_path = registry_path
            server = self.start_server()
            second = self.make_plan("beta")

            status, body, _ = _http_request(
                self.url(server, "/api/registry/pin"),
                method="POST",
                body={"plan_dir": str(self.canonical), "label": "Alpha"},
                headers=self.auth(server),
            )
            self.assertEqual(200, status, body)

            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                rc = workbench_archive.workbench_pin_from_args(
                    argparse.Namespace(plan_dir=str(second), label="Beta", cwd=None)
                )
            self.assertEqual(0, rc)
            self.assertIn(str(registry_path), stdout.getvalue())

            reloaded = workbench_registry.load_registry(registry_path)

        self.assertEqual(
            [str(self.canonical), str(second)],
            [pin.plan_dir for pin in reloaded.pins],
        )
        self.assertEqual(["Alpha", "Beta"], [pin.label for pin in reloaded.pins])

    def test_the_route_and_the_cli_refuse_a_bad_plan_for_the_same_reason(
        self,
    ) -> None:
        server = self.start_server()

        status, body, _ = _http_request(
            self.url(server, "/api/registry/pin"),
            method="POST",
            body={"plan_dir": "   "},
            headers=self.auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-plan", body["reason"])

        # Same refusal, rendered as an exit code and a stderr line rather than
        # a status and a body. The service raised it once for both.
        with patched_env(XDG_CONFIG_HOME=str(self.tmp_path / "config")):
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                rc = workbench_archive.workbench_pin_from_args(
                    argparse.Namespace(plan_dir="   ", label=None, cwd=None)
                )
        self.assertEqual(1, rc)
        self.assertIn("workbench pin", stderr.getvalue())

    def test_the_archive_route_reports_the_review_it_did_not_act_on(self) -> None:
        archive_id = self.archive_one()
        self.backdate(archive_id, days=400)
        before = self.archive_tree()
        server = self.start_server()

        status, body, _ = _http_request(
            self.url(server, "/api/archive"), headers=self.auth(server)
        )

        self.assertEqual(200, status)
        self.assertEqual([archive_id], body["archive_cleanup"]["candidate_ids"])
        self.assertEqual(1, body["archive_cleanup"]["candidate_count"])
        self.assertEqual(30, body["archive_retention_days"])
        self.assertEqual(before, self.archive_tree())

    def test_the_settings_route_moves_the_threshold_and_empties_nothing(
        self,
    ) -> None:
        archive_id = self.archive_one()
        self.backdate(archive_id, days=5)
        server = self.start_server()

        status, settings, _ = _http_request(
            self.url(server, "/api/archive/settings"),
            method="POST",
            body={"archive_retention_days": 1},
            headers=self.auth(server),
        )
        self.assertEqual(200, status, settings)
        self.assertEqual(1, settings["archive_retention_days"])

        _, listing, _ = _http_request(
            self.url(server, "/api/archive"), headers=self.auth(server)
        )

        self.assertEqual([archive_id], listing["archive_cleanup"]["candidate_ids"])
        self.assertEqual(
            [archive_id], [entry["archive_id"] for entry in listing["entries"]]
        )
        self.assertTrue(
            workbench_archive.archive_entry_dir(archive_id, self.state_dir).exists()
        )

    def test_the_purge_route_keeps_its_typed_count(self) -> None:
        archive_id = self.archive_one()
        server = self.start_server()

        status, body, _ = _http_request(
            self.url(server, "/api/archive/delete"),
            method="POST",
            body={"archive_ids": [archive_id], "confirm_count": 2},
            headers=self.auth(server),
        )

        self.assertEqual(400, status)
        self.assertEqual("confirmation-mismatch", body["reason"])
        self.assertTrue(
            workbench_archive.archive_entry_dir(archive_id, self.state_dir).exists()
        )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
