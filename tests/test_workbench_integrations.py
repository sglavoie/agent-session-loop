"""Owned host resources, and the one credential allowed to publish.

Two halves of the same question. :mod:`asl_workbench.integrations` decides
which resources on a shared host belong to this application, and
:mod:`asl_workbench.notifications` decides who may write to the topic those
resources point at. Both are asserted here against a fake host rather than a
real one, because what is being proven is the decision, not ``docker``.

The fixture is deliberately a hostile neighbour: a Forgejo route on another
port, an unrelated Homepage entry, an ntfy server with somebody else's account
on it. Nearly every test below ends by checking that neighbour is untouched.
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from asl_workbench import incidents, notifications
from asl_workbench.http import MODE_LOCAL, MODE_REMOTE
from asl_workbench.installation import InstallationError, document, lock, write_json
from asl_workbench.integrations import (
    COMPONENTS,
    PI_SITE,
    IntegrationError,
    Integrations,
    check_publisher,
    desired_route,
    homepage_documents,
    own_route,
    provision,
    route_without_owned,
)
from asl_workbench.notifications import (
    DEPLOYMENT_LOCAL,
    DEPLOYMENT_PRODUCTION,
    DEPLOYMENT_STAGING,
    NOTIFICATION_TEMPLATES,
    PI_NOTIFICATION_SITE,
    NotificationError,
    NotificationSite,
    NtfyPublisher,
    notification,
    select_publisher,
)

OWNER = "owner@example.com"
TOKEN = "tk_fixtureprivate123"

#: The neighbour: somebody else's Serve route, on somebody else's port.
FORGEJO: dict[str, Any] = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {
        PI_SITE.host + ":443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}
    },
}
COMPOSE = (
    "services:\n  homepage:\n    image: homepage:pinned\n"
    "    container_name: homepage\n    restart: unless-stopped\n"
)
SERVICES = "- Services:\n    - Forgejo:\n        href: https://git.example.com\n"


class FakePi:
    """A host with neighbours: every command this module can run, and no more."""

    def __init__(self) -> None:
        self.route: dict[str, Any] = copy.deepcopy(FORGEJO)
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.owner = OWNER
        self.busy = False
        self.user = False
        self.permission = False
        self.broaden = False
        self.anonymous_grant = False
        self.tokens = 0
        self.fail_compose = False
        self.monitor: str | None = None

    def acl(self) -> bytes:
        text = (
            "user sglavoie (role: user, tier: none)\n- read-write access to topic *\n"
        )
        if self.user:
            text += f"user {PI_SITE.publisher_user} (role: user, tier: none)\n"
            if self.permission:
                text += f"- write-only access to topic {PI_SITE.publisher_topic}\n"
            else:
                text += (
                    "- no topic-specific permissions\n"
                    "- no access to any (other) topics (server config)\n"
                )
            if self.broaden:
                text += "- read-write access to topic *\n"
        anonymous = (
            f"- read-only access to topic {PI_SITE.publisher_topic}\n"
            if self.anonymous_grant
            else "- no topic-specific permissions\n"
            "- no access to any (other) topics (server config)\n"
        )
        return (text + "user * (role: anonymous, tier: none)\n" + anonymous).encode()

    def payloads(self) -> list[dict[str, Any]]:
        """Every Kuma payload, as the adapter would parse it off stdin."""
        return [
            json.loads(
                (data or b"")
                .decode()
                .split("\n", 1)[0]
                .removeprefix("global.PAYLOAD=")
                .removesuffix(";")
            )
            for argv, data in self.calls
            if PI_SITE.kuma_container in argv
        ]

    def __call__(self, argv: Sequence[str], *, data: bytes | None = None) -> bytes:
        argv = list(argv)
        self.calls.append((argv, data))
        if argv[:2] == ["sudo", "-n"]:
            argv = argv[2:]
        if argv == ["tailscale", "status", "--json"]:
            return json.dumps(
                {
                    "Self": {
                        "DNSName": PI_SITE.host + ".",
                        "UserID": 1,
                        "TailscaleIPs": ["100.64.0.1"],
                    },
                    "User": {"1": {"LoginName": self.owner}},
                }
            ).encode()
        if argv == ["tailscale", "serve", "status", "--json"]:
            return json.dumps(self.route).encode()
        if argv[:2] == ["ss", "-H"]:
            return b"LISTEN 0 128 0.0.0.0:9443 0.0.0.0:*" if self.busy else b""
        if argv == [
            "tailscale",
            "serve",
            "--bg",
            f"--https={PI_SITE.port}",
            PI_SITE.target,
        ]:
            self.route = desired_route(PI_SITE, self.route)
            return b""
        if argv == ["tailscale", "serve", f"--https={PI_SITE.port}", "off"]:
            self.route = route_without_owned(PI_SITE, self.route)
            return b""
        if argv[:2] == ["docker", "compose"]:
            if self.fail_compose:
                raise IntegrationError("fixture failure")
            return b""
        if argv[:4] == ["docker", "exec", "-i", PI_SITE.homepage_container]:
            return b"container checks passed"
        if argv[:4] == ["docker", "exec", PI_SITE.ntfy_container, "ntfy"]:
            return self._ntfy(argv[4:])
        if argv[:5] == ["docker", "exec", "-i", PI_SITE.ntfy_container, "sh"]:
            self.user = True
            return b""
        if argv[:5] == ["docker", "exec", "-i", PI_SITE.kuma_container, "node"]:
            return self._kuma(data)
        raise AssertionError(argv)

    def _ntfy(self, rest: list[str]) -> bytes:
        user = PI_SITE.publisher_user
        if rest == ["user", "list"]:
            return self.acl()
        if rest == ["user", "del", user]:
            self.user = False
            self.permission = False
            return b""
        if rest == ["access", user, PI_SITE.publisher_topic, "write-only"]:
            self.permission = True
            return b""
        if rest == ["token", "add", f"--label={user}", user]:
            self.tokens += 1
            return f"token {TOKEN} created".encode()
        raise AssertionError(rest)

    def _kuma(self, data: bytes | None) -> bytes:
        payload = json.loads(
            (data or b"")
            .decode()
            .split("\n", 1)[0]
            .removeprefix("global.PAYLOAD=")
            .removesuffix(";")
        )
        # The adapter's own ownership rule, in Python: a monitor under any known
        # name belongs to somebody, and it is only ours if a receipt says so.
        if self.monitor is not None and not payload["owned"]:
            raise IntegrationError("ownership conflict")
        if self.monitor is not None and self.monitor not in payload["names"]:
            raise IntegrationError("owned monitor not found")
        if payload["mode"] == "apply":
            self.monitor = payload["name"]
        if payload["mode"] == "remove":
            self.monitor = None
        return b"{}"


class IntegrationCase(unittest.TestCase):
    def setUp(self) -> None:
        old_umask = os.umask(0o077)
        self.addCleanup(os.umask, old_umask)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.prefix = self.root / "workbench"
        provision(self.prefix, OWNER, "mac-studio")
        self.home = self.root / "home"
        (self.home / "homepage/config").mkdir(parents=True)
        self.compose = self.home / PI_SITE.homepage_compose
        self.services = self.home / PI_SITE.homepage_services
        self.compose.write_text(COMPOSE)
        self.services.write_text(SERVICES)
        self.services.chmod(0o644)
        (self.home / "uptime-kuma/secrets").mkdir(parents=True)
        self.password = self.home / PI_SITE.kuma_password_file
        self.password.write_text("fixture-password")
        self.password.chmod(0o600)
        self.pi = FakePi()
        self.integration = Integrations(self.prefix, home=self.home, runner=self.pi)

    def files(self, *, locks: bool = True) -> dict[Path, bytes]:
        return {
            p: p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file() and (locks or p.suffix != ".lock")
        }

    def summary_token(self) -> str:
        return (self.prefix / "credentials/summary.token").read_text().strip()

    # -- provisioning -------------------------------------------------------

    def test_provision_repeats_without_reissuing_or_widening(self) -> None:
        before = self.files()
        provision(self.prefix, OWNER, "mac-studio")
        self.assertEqual(before, self.files())
        for path in self.prefix.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        token = document(self.prefix / "credentials/connector-issued.json")[
            "connector_token"
        ]
        store, _ = self.integration_secrets()
        found = store.authenticate(token)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual("mac-studio", found.subject)
        self.assertNotIn(token, (self.prefix / "credentials/secrets.json").read_text())

    def integration_secrets(self) -> tuple[Any, str | None]:
        from asl_workbench.integrations import service_secrets

        return service_secrets(self.integration.secret_path)

    def test_provision_refuses_a_different_owner_or_a_half_written_prefix(self) -> None:
        with self.assertRaises(IntegrationError):
            provision(self.prefix, "other@example.com", "mac-studio")
        (self.prefix / "credentials/summary.token").unlink()
        with self.assertRaises(IntegrationError):
            provision(self.prefix, OWNER, "mac-studio")

    def test_symlinked_prefix_refused(self) -> None:
        link = self.root / "link"
        link.symlink_to(self.prefix)
        with self.assertRaises(InstallationError):
            Integrations(link, home=self.home, runner=self.pi)

    # -- preview ------------------------------------------------------------

    def test_preview_touches_nothing_and_quotes_no_secret_in_an_argument(self) -> None:
        before = self.files()
        self.integration.execute("preview", "all")
        self.assertEqual(before, self.files())
        self.assertEqual(FORGEJO, self.pi.route)
        self.assertFalse(self.pi.user)
        self.assertIsNone(self.pi.monitor)
        for argv, _ in self.pi.calls:
            self.assertNotIn("fixture-password", str(argv))
            self.assertNotIn(self.summary_token(), str(argv))

    def test_preview_document_says_create_then_reconcile(self) -> None:
        value = self.integration.preview_document("all")
        self.assertEqual(list(COMPONENTS), [entry["component"] for entry in value])
        self.assertEqual({"create"}, {entry["action"] for entry in value})
        encoded = json.dumps(value)
        self.assertIn("write-only", encoded)
        self.assertIn(PI_SITE.port, encoded)
        self.assertNotIn(self.summary_token(), encoded)
        self.integration.execute("apply", "route")
        reconciled = self.integration.preview_document("route")
        self.assertEqual("reconcile-owned", reconciled[0]["action"])

    # -- the route ----------------------------------------------------------

    def test_route_applies_once_and_removal_leaves_the_neighbour(self) -> None:
        self.integration.execute("apply", "route")
        self.assertTrue(own_route(PI_SITE, self.pi.route))
        self.integration.execute("apply", "route")
        self.assertEqual(1, sum("--bg" in argv for argv, _ in self.pi.calls))
        self.pi.route["TCP"]["8443"] = {"TCPForward": "127.0.0.1:1234"}
        self.integration.execute("remove", "route")
        self.integration.execute("remove", "route")
        self.assertEqual(FORGEJO["Web"], self.pi.route["Web"])
        self.assertIn("8443", self.pi.route["TCP"])

    def test_sudo_is_limited_to_the_two_scoped_route_mutations(self) -> None:
        integration = Integrations(
            self.prefix, home=self.home, runner=self.pi, tailscale_sudo=True
        )
        integration.execute("preview", "all")
        self.assertFalse(any(argv[0] == "sudo" for argv, _ in self.pi.calls))
        integration.execute("apply", "route")
        integration.execute("remove", "route")
        self.assertEqual(
            [
                [
                    "sudo",
                    "-n",
                    "tailscale",
                    "serve",
                    "--bg",
                    f"--https={PI_SITE.port}",
                    PI_SITE.target,
                ],
                ["sudo", "-n", "tailscale", "serve", f"--https={PI_SITE.port}", "off"],
            ],
            [argv for argv, _ in self.pi.calls if argv[0] == "sudo"],
        )
        self.assertEqual(FORGEJO, self.pi.route)

    def test_route_refuses_an_occupied_port_and_rechecks_the_listeners(self) -> None:
        self.pi.busy = True
        with self.assertRaises(IntegrationError):
            self.integration.execute("apply", "route")
        self.assertIsNone(self.integration.receipt("route"))
        self.pi.busy = False
        self.pi.route = desired_route(PI_SITE, FORGEJO)
        with self.assertRaises(IntegrationError):
            self.integration.execute("apply", "route")

    def test_route_refuses_a_funnelled_port(self) -> None:
        self.pi.route["AllowFunnel"] = {PI_SITE.authority: True}
        with self.assertRaises(IntegrationError):
            self.integration.execute("apply", "route")

    def test_route_changed_since_application_is_not_removed(self) -> None:
        self.integration.execute("apply", "route")
        handlers = self.pi.route["Web"][PI_SITE.authority]["Handlers"]
        handlers["/"]["Proxy"] = "http://127.0.0.1:9999"
        before = copy.deepcopy(self.pi.route)
        with self.assertRaises(IntegrationError):
            self.integration.execute("remove", "route")
        self.assertEqual(before, self.pi.route)

    def test_a_different_tailnet_owner_is_refused(self) -> None:
        self.pi.owner = "other@example.com"
        with self.assertRaises(IntegrationError):
            self.integration.execute("apply", "route")
        self.assertEqual(FORGEJO, self.pi.route)

    # -- homepage -----------------------------------------------------------

    def test_homepage_is_private_while_installed_and_restored_byte_for_byte(
        self,
    ) -> None:
        self.integration.execute("apply", "homepage")
        after = self.services.read_bytes()
        self.assertIn(b"counts.running", after)
        self.assertIn(PI_SITE.summary_path.encode(), after)
        self.assertEqual(0o600, self.services.stat().st_mode & 0o777)
        self.integration.execute("apply", "homepage")
        self.assertEqual(after, self.services.read_bytes())
        self.integration.execute("verify", "homepage")
        self.integration.execute("remove", "homepage")
        self.integration.execute("remove", "homepage")
        self.assertEqual(SERVICES, self.services.read_text())
        self.assertEqual(COMPOSE, self.compose.read_text())
        self.assertEqual(0o644, self.services.stat().st_mode & 0o777)

    def test_homepage_restores_the_modes_the_operator_chose(self) -> None:
        paths = [self.compose, self.services]
        for path in paths:
            path.chmod(0o664)
        self.integration.execute("apply", "homepage")
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in paths))
        self.integration.execute("remove", "homepage")
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o664 for p in paths))

    def test_homepage_refuses_to_overwrite_an_operator_edit(self) -> None:
        self.integration.execute("apply", "homepage")
        self.services.write_text(self.services.read_text() + "# operator\n")
        before = self.services.read_bytes()
        with self.assertRaises(IntegrationError):
            self.integration.execute("remove", "homepage")
        self.assertEqual(before, self.services.read_bytes())

    def test_homepage_is_removable_after_a_failed_restart(self) -> None:
        self.pi.fail_compose = True
        with self.assertRaises(IntegrationError):
            self.integration.execute("apply", "homepage")
        self.assertIsNotNone(self.integration.receipt("homepage"))
        self.pi.fail_compose = False
        self.integration.execute("remove", "homepage")
        self.assertEqual(COMPOSE, self.compose.read_text())
        self.assertEqual(SERVICES, self.services.read_text())

    def test_homepage_refuses_a_credential_that_is_not_summary_only(self) -> None:
        token = document(self.prefix / "credentials/connector-issued.json")[
            "connector_token"
        ]
        (self.prefix / "credentials/summary.token").write_text(token)
        with self.assertRaises(IntegrationError):
            self.integration.execute("apply", "homepage")

    def test_homepage_refuses_an_ambiguous_or_foreign_document(self) -> None:
        for compose, services in (
            (COMPOSE + "    extra_hosts: []\n", SERVICES),
            (COMPOSE, SERVICES + PI_SITE.marker + "\n"),
            (COMPOSE, SERVICES + f"- {PI_SITE.group_heading}:\n"),
        ):
            with (
                self.subTest(services=services),
                self.assertRaises(IntegrationError),
            ):
                homepage_documents(PI_SITE, compose, services, "100.1.2.3", "token")

    def test_homepage_recognises_the_heading_the_companion_installed(self) -> None:
        # The visible name changed; a card installed under the old one is still
        # somebody's, so writing a second card beside it is refused rather than
        # done quietly.
        legacy = SERVICES + f"- {PI_SITE.legacy_group_headings[0]}:\n"
        with self.assertRaises(IntegrationError):
            homepage_documents(PI_SITE, COMPOSE, legacy, "100.1.2.3", "token")

    # -- ntfy ---------------------------------------------------------------

    def test_ntfy_grants_one_topic_once_and_revokes_cleanly(self) -> None:
        self.integration.execute("apply", "ntfy")
        self.integration.execute("apply", "ntfy")
        self.integration.execute("verify", "ntfy")
        self.assertEqual(1, self.pi.tokens)
        self.assertTrue(self.pi.permission)
        self.assertEqual(TOKEN, document(self.integration.secret_path)["ntfy_token"])
        self.integration.execute("remove", "ntfy")
        self.integration.execute("remove", "ntfy")
        self.assertFalse(self.pi.user)
        self.assertNotIn("ntfy_token", document(self.integration.secret_path))
        self.assertIn(b"read-write access to topic *", self.pi.acl())

    def test_ntfy_refuses_a_credential_with_no_ownership_receipt(self) -> None:
        saved = document(self.integration.secret_path)
        saved["ntfy_token"] = "existing-private-token"
        write_json(self.integration.secret_path, saved)
        with self.assertRaises(IntegrationError):
            self.integration.execute("apply", "ntfy")
        self.assertFalse(self.pi.user)

    def test_ntfy_refuses_a_server_where_anonymous_callers_hold_the_topic(self) -> None:
        self.pi.anonymous_grant = True
        with self.assertRaises(IntegrationError):
            self.integration.execute("apply", "ntfy")
        self.assertFalse(self.pi.user)

    def test_ntfy_never_adopts_an_existing_account(self) -> None:
        self.pi.user = True
        with self.assertRaises(IntegrationError):
            self.integration.execute("apply", "ntfy")
        self.assertIsNone(self.integration.receipt("ntfy"))

    def test_ntfy_refuses_a_broadened_account_even_to_remove_it(self) -> None:
        self.integration.execute("apply", "ntfy")
        self.pi.broaden = True
        with self.assertRaises(IntegrationError):
            self.integration.execute("verify", "ntfy")
        with self.assertRaises(IntegrationError):
            self.integration.execute("remove", "ntfy")
        self.assertTrue(self.pi.user)

    def test_ntfy_does_not_reissue_after_an_ambiguous_token_creation(self) -> None:
        self.pi.user = self.pi.permission = True
        self.integration.save(
            "ntfy",
            {"owned_user": PI_SITE.publisher_user, "phase": "token-intent"},
        )
        with self.assertRaises(IntegrationError):
            self.integration.execute("apply", "ntfy")
        self.assertEqual(0, self.pi.tokens)
        self.integration.execute("remove", "ntfy")
        self.assertFalse(self.pi.user)

    def test_a_staging_deployment_cannot_mint_the_production_publisher(self) -> None:
        staging = Integrations(
            self.prefix,
            site=replace(PI_SITE, deployment=DEPLOYMENT_STAGING),
            home=self.home,
            runner=self.pi,
        )
        with self.assertRaises(IntegrationError):
            staging.execute("apply", "ntfy")
        self.assertFalse(self.pi.user)
        self.assertEqual(0, self.pi.tokens)
        self.assertIsNone(staging.receipt("ntfy"))
        # The rest of the host is still installable from a rehearsal.
        staging.execute("apply", "route")
        self.assertTrue(own_route(PI_SITE, self.pi.route))

    def test_publisher_permissions_accept_only_the_dedicated_topic(self) -> None:
        widened = (
            self.pi.acl()
            .decode()
            .replace("user sglavoie", f"user {PI_SITE.publisher_user}", 1)
        )
        with self.assertRaises(IntegrationError):
            check_publisher(PI_SITE, widened)

    # -- kuma ---------------------------------------------------------------

    def test_kuma_applies_repeatably_and_removes(self) -> None:
        self.integration.execute("apply", "kuma")
        self.integration.execute("apply", "kuma")
        self.integration.execute("remove", "kuma")
        self.integration.execute("remove", "kuma")
        self.assertIsNone(self.pi.monitor)
        payloads = self.pi.payloads()
        self.assertTrue(payloads)
        for payload in payloads:
            self.assertEqual(PI_SITE.monitor_name, payload["name"])
            self.assertEqual(PI_SITE.target + PI_SITE.health_path, payload["url"])

    def test_kuma_renames_the_monitor_the_companion_installed(self) -> None:
        # The monitor's name is both its identity and what an operator reads, so
        # the legacy name travels in the payload: an owned monitor is found and
        # renamed rather than duplicated beside a second one.
        legacy = PI_SITE.legacy_monitor_names[0]
        self.pi.monitor = legacy
        self.integration.save("kuma", {"name": legacy})
        self.integration.execute("apply", "kuma")
        self.assertEqual(PI_SITE.monitor_name, self.pi.monitor)
        self.assertEqual(
            PI_SITE.monitor_name, (self.integration.receipt("kuma") or {})["name"]
        )
        self.assertIn(legacy, self.pi.payloads()[-1]["names"])

    def test_kuma_never_adopts_an_existing_monitor(self) -> None:
        self.pi.monitor = PI_SITE.monitor_name
        with self.assertRaises(IntegrationError):
            self.integration.execute("apply", "kuma")
        self.assertIsNone(self.integration.receipt("kuma"))

    def test_kuma_receives_its_password_only_over_stdin(self) -> None:
        self.integration.execute("apply", "kuma")
        for argv, _ in self.pi.calls:
            self.assertNotIn("fixture-password", str(argv))
        self.assertTrue(
            any(
                payload["password"] == "fixture-password"
                for payload in self.pi.payloads()
            )
        )

    # -- locking ------------------------------------------------------------

    def test_credential_changes_refuse_a_running_service(self) -> None:
        with (
            lock(self.prefix / "state/runtime.lock"),
            self.assertRaises(InstallationError),
        ):
            self.integration.execute("apply", "ntfy")
        self.assertFalse(self.pi.user)

    def test_monitoring_may_still_be_applied_while_the_service_runs(self) -> None:
        with lock(self.prefix / "state/runtime.lock"):
            self.integration.execute("apply", "kuma")
        self.assertEqual(PI_SITE.monitor_name, self.pi.monitor)

    # -- the whole host -----------------------------------------------------

    def test_every_component_applies_and_withdraws_leaving_the_host_as_found(
        self,
    ) -> None:
        # Lock files are excluded and only those: a mutation creates them, they
        # hold nothing, and they are the installation's rather than the host's.
        before = self.files(locks=False)
        self.integration.execute("apply", "all")
        self.assertTrue(own_route(PI_SITE, self.pi.route))
        self.assertTrue(self.pi.user)
        self.assertEqual(PI_SITE.monitor_name, self.pi.monitor)
        self.integration.execute("verify", "all")
        self.integration.execute("remove", "all")
        self.assertEqual(FORGEJO, self.pi.route)
        self.assertFalse(self.pi.user)
        self.assertIsNone(self.pi.monitor)
        self.assertEqual(SERVICES, self.services.read_text())
        self.assertEqual(COMPOSE, self.compose.read_text())
        self.assertIn(b"read-write access to topic *", self.pi.acl())
        self.assertEqual(before, self.files(locks=False))

    def test_removal_withdraws_the_url_last(self) -> None:
        # Reverse dependency order: the card that links to the private URL and
        # the monitor that watches the service are withdrawn while the service
        # is still reachable.
        self.integration.execute("apply", "all")
        self.pi.calls.clear()
        self.integration.execute("remove", "all")
        route = next(
            index
            for index, (argv, _) in enumerate(self.pi.calls)
            if argv == ["tailscale", "serve", f"--https={PI_SITE.port}", "off"]
        )
        for container in (PI_SITE.kuma_container, PI_SITE.homepage_container):
            with self.subTest(container=container):
                self.assertLess(
                    next(
                        index
                        for index, (argv, _) in enumerate(self.pi.calls)
                        if container in argv
                    ),
                    route,
                )


class NamingCase(unittest.TestCase):
    """What may be renamed, and what may never be."""

    def test_ownership_identifiers_are_the_ones_already_installed(self) -> None:
        self.assertEqual("asl-companion", PI_SITE.publisher_user)
        self.assertEqual("asl-alerts", PI_SITE.publisher_topic)
        self.assertEqual("# ASL companion integration v1", PI_SITE.marker)
        self.assertIn("ASL companion service health", PI_SITE.monitor_names)
        self.assertIn("ASL companion", PI_SITE.owned_group_headings)

    def test_visible_names_carry_the_unified_application(self) -> None:
        self.assertEqual("ASL Workbench", PI_SITE.group_heading)
        self.assertEqual("ASL Workbench service health", PI_SITE.monitor_name)
        self.assertIn("Workbench", notifications.NOTIFICATION_TITLE)

    def test_the_private_origin_and_link_shape_are_unchanged(self) -> None:
        # Every notification ever delivered carries this origin and this
        # fragment. They are not naming, they are addresses somebody has in
        # their notification history.
        self.assertEqual("https://pi.example.ts.net:9443", PI_SITE.public_origin)
        self.assertEqual(PI_SITE.public_origin, PI_NOTIFICATION_SITE.private_origin)
        self.assertEqual("ntfy.sglavoie.com", PI_NOTIFICATION_SITE.host)
        self.assertEqual(PI_SITE.publisher_topic, PI_NOTIFICATION_SITE.topic)
        self.assertEqual(
            "https://pi.example.ts.net:9443/?workbench=1#run=plan%2Frun-1",
            notifications.notification_link("plan/run-1", site=PI_NOTIFICATION_SITE),
        )

    def test_two_deployments_on_one_host_are_two_targets(self) -> None:
        """The target id has to separate a rehearsal from what it rehearses.

        A staging site keeps the host on purpose -- the rehearsal really does
        run on the Pi -- and moves the port aside. So the identity a connector
        addresses cannot be the host: derived that way, a staging connector's
        uploads would be addressed to the production service too, and the one
        check standing between the two is the one that stops carrying its
        weight exactly when it is needed.
        """
        staging = replace(PI_SITE, port="9444")
        self.assertEqual("pi.example.ts.net:9443", PI_SITE.target_id)
        self.assertNotEqual(PI_SITE.target_id, staging.target_id)
        self.assertEqual(PI_SITE.host, staging.host)


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, amt: int) -> bytes:
        return self._body[:amt]


class FakeConnection:
    """One connection, with each of its three failure points made reachable."""

    def __init__(
        self,
        *,
        response: FakeResponse | None = None,
        connect_error: Exception | None = None,
    ) -> None:
        self.response = response
        self.connect_error = connect_error
        self.requests: list[tuple[str, str, bytes, dict[str, str]]] = []
        self.closed = False

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    def request(self, method: str, url: str, body: bytes, headers: Any) -> None:
        self.requests.append((method, url, body, dict(headers)))

    def getresponse(self) -> FakeResponse:
        assert self.response is not None
        return self.response

    def close(self) -> None:
        self.closed = True


def receipt(identifier: str = "abcdefghij12") -> bytes:
    return json.dumps(
        {"id": identifier, "topic": PI_NOTIFICATION_SITE.topic, "event": "message"}
    ).encode()


class PublisherCase(unittest.TestCase):
    def publisher(self, connection: FakeConnection, **kwargs: Any) -> NtfyPublisher:
        return NtfyPublisher(TOKEN, connect=lambda: connection, **kwargs)

    def test_a_published_message_carries_the_link_and_the_receipt_identity(
        self,
    ) -> None:
        connection = FakeConnection(response=FakeResponse(200, receipt()))
        result = self.publisher(connection).publish(
            kind=incidents.INCIDENT_UNANSWERED_QUESTION,
            project="Example project",
            plan_key="abc123",
            run_key="plan/run-1",
            question_id="question/1",
            before_send=lambda: True,
        )
        self.assertEqual(incidents.DELIVERY_PUBLISHED, result.state)
        self.assertEqual("abcdefghij12", result.receipt_id)
        method, url, body, headers = connection.requests[0]
        self.assertEqual(("POST", "/asl-alerts"), (method, url))
        self.assertIn(
            b"https://pi.example.ts.net:9443/?workbench=2#plan?key=abc123&run=plan%2Frun-1&focus=reply&question=question%2F1",
            body,
        )
        self.assertEqual(
            "https://pi.example.ts.net:9443/?workbench=2#plan?key=abc123&run=plan%2Frun-1&focus=reply&question=question%2F1",
            headers["Click"],
        )
        self.assertEqual(notifications.NOTIFICATION_TITLE, headers["Title"])
        self.assertTrue(connection.closed)

    def test_a_release_change_forces_a_fresh_mobile_navigation(self) -> None:
        connection = FakeConnection(response=FakeResponse(200, receipt()))
        self.publisher(
            connection,
            navigation_version="bcd8c364435a0429",
        ).publish(
            kind=incidents.INCIDENT_UNANSWERED_QUESTION,
            project="Example project",
            plan_key="abc123",
            run_key="plan/run-1",
            question_id="question/1",
            before_send=lambda: True,
        )
        _method, _url, _body, headers = connection.requests[0]
        self.assertEqual(
            "https://pi.example.ts.net:9443/"
            "?workbench=2&release=bcd8c364435a0429"
            "#plan?key=abc123&run=plan%2Frun-1&focus=reply&question=question%2F1",
            headers["Click"],
        )

    def test_every_incident_kind_has_a_message(self) -> None:
        self.assertEqual(set(incidents.INCIDENT_KINDS), set(NOTIFICATION_TEMPLATES))
        for kind in incidents.INCIDENT_KINDS:
            with self.subTest(kind=kind):
                body, link = notification(
                    kind=kind,
                    project="",
                    run_key="r",
                    site=PI_NOTIFICATION_SITE,
                )
                self.assertTrue(body.startswith(b"ASL project: "))
                self.assertIn(link.encode(), body)

    def test_a_connection_failure_is_the_only_replayable_outcome(self) -> None:
        connection = FakeConnection(connect_error=OSError("refused"))
        result = self.publisher(connection).publish(
            kind=incidents.INCIDENT_TERMINAL_FAILURE,
            project="p",
            plan_key="abc123",
            run_key="r",
            before_send=lambda: True,
        )
        self.assertEqual(incidents.DELIVERY_RETRY, result.state)
        self.assertEqual([], connection.requests)

    def test_a_resolution_during_connect_sends_no_bytes(self) -> None:
        connection = FakeConnection(response=FakeResponse(200, receipt()))
        result = self.publisher(connection).publish(
            kind=incidents.INCIDENT_TERMINAL_FAILURE,
            project="p",
            plan_key="abc123",
            run_key="r",
            before_send=lambda: False,
        )
        self.assertEqual(incidents.DELIVERY_RETRY, result.state)
        self.assertEqual([], connection.requests)

    def test_an_unreadable_receipt_is_never_claimed_as_delivered(self) -> None:
        cases = (
            FakeResponse(500, receipt()),
            FakeResponse(200, b"not json"),
            FakeResponse(200, b"{}"),
            FakeResponse(
                200,
                json.dumps(
                    {"id": "x y", "topic": "asl-alerts", "event": "message"}
                ).encode(),
            ),
            FakeResponse(
                200,
                json.dumps(
                    {"id": "abcdefghij12", "topic": "other", "event": "message"}
                ).encode(),
            ),
            FakeResponse(200, b"x" * (notifications.MAX_RECEIPT_BYTES + 1)),
        )
        for response in cases:
            with self.subTest(status=response.status, body=response._body[:16]):
                result = self.publisher(FakeConnection(response=response)).publish(
                    kind=incidents.INCIDENT_LOST_OBSERVATION,
                    project="p",
                    plan_key="abc123",
                    run_key="r",
                    before_send=lambda: True,
                )
                self.assertEqual(incidents.DELIVERY_UNCONFIRMED, result.state)

    def test_the_publisher_satisfies_the_engine_protocol(self) -> None:
        # The engine calls exactly this, by keyword, so the names are the
        # contract rather than the class.
        self.assertEqual(
            inspect.signature(incidents.Publisher.publish),
            inspect.signature(NtfyPublisher.publish),
        )

    # -- who may hold it ----------------------------------------------------

    def test_only_a_remote_production_deployment_holding_a_token_publishes(
        self,
    ) -> None:
        connection = FakeConnection()
        chosen = select_publisher(
            mode=MODE_REMOTE,
            deployment=DEPLOYMENT_PRODUCTION,
            token=TOKEN,
            connect=lambda: connection,
        )
        self.assertIsInstance(chosen, NtfyPublisher)
        for mode, deployment, token in (
            (MODE_LOCAL, DEPLOYMENT_PRODUCTION, TOKEN),
            (MODE_LOCAL, DEPLOYMENT_LOCAL, TOKEN),
            (MODE_REMOTE, DEPLOYMENT_LOCAL, TOKEN),
            (MODE_REMOTE, DEPLOYMENT_PRODUCTION, None),
        ):
            with self.subTest(mode=mode, deployment=deployment, token=bool(token)):
                self.assertIsNone(
                    select_publisher(mode=mode, deployment=deployment, token=token)
                )

    def test_staging_holding_a_production_credential_is_a_misconfiguration(
        self,
    ) -> None:
        with self.assertRaises(NotificationError):
            select_publisher(
                mode=MODE_REMOTE, deployment=DEPLOYMENT_STAGING, token=TOKEN
            )
        with self.assertRaises(NotificationError):
            NtfyPublisher(TOKEN, deployment=DEPLOYMENT_STAGING)
        with self.assertRaises(NotificationError):
            select_publisher(mode=MODE_REMOTE, deployment="rehearsal", token=TOKEN)

    def test_a_staging_site_of_its_own_is_allowed(self) -> None:
        # The guard is about the production topic, not about publishing. A
        # rehearsal with its own topic is a rehearsal, not a second publisher.
        site = NotificationSite(
            host="ntfy.example.com",
            topic="asl-staging",
            private_origin="https://staging.example.com",
        )
        chosen = select_publisher(
            mode=MODE_REMOTE,
            deployment=DEPLOYMENT_STAGING,
            token=TOKEN,
            site=site,
            connect=lambda: FakeConnection(),
        )
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(site, chosen.site)

    def test_a_credential_that_is_not_one_is_refused(self) -> None:
        for token in ("", "tk with spaces", "tk_" + "x" * 600):
            with (
                self.subTest(token=token[:12]),
                self.assertRaises(NotificationError),
            ):
                NtfyPublisher(token)


if __name__ == "__main__":
    unittest.main()
