# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F401, F403, F405, F822, I001
"""Workbench deployment integration tests."""

# ruff: noqa: F401, F403, F405
from tests.workbench_integrations_support import *


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
        self.assertIn(b"field: reason", after)
        self.assertIn(b"field: freshness.total", after)
        self.assertIn(b"field: counts.by_state.running", after)
        self.assertNotIn(b"field: freshness\n", after)
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
        tailnet = replace(PI_SITE, container_origin=None)
        for compose, services in (
            (COMPOSE + "    extra_hosts: []\n", SERVICES),
            (COMPOSE, SERVICES + PI_SITE.marker + "\n"),
            (COMPOSE, SERVICES + f"- {PI_SITE.group_heading}:\n"),
        ):
            with (
                self.subTest(services=services),
                self.assertRaises(IntegrationError),
            ):
                homepage_documents(tailnet, compose, services, "100.1.2.3", "token")

    def test_homepage_widget_uses_the_container_origin(self) -> None:
        # Since Tailscale 1.104 a container is refused at the tailnet address,
        # so the widget and the container check go through the host's bridge,
        # and the compose file the bridge lives in is left to its operator.
        compose = (
            COMPOSE + "    extra_hosts:\n      - host.docker.internal:host-gateway\n"
        )
        after_compose, after_services = homepage_documents(
            PI_SITE, compose, SERVICES, "100.1.2.3", "token"
        )
        self.assertEqual(compose, after_compose)
        self.assertIn(
            f"url: {PI_SITE.container_origin}{PI_SITE.summary_path}\n", after_services
        )
        self.assertIn(f"href: {PI_SITE.public_origin}\n", after_services)
        self.integration.execute("apply", "homepage")
        self.integration.execute("verify", "homepage")
        checks = [
            data
            for argv, data in self.pi.calls
            if argv[:4] == ["docker", "exec", "-i", PI_SITE.homepage_container]
        ]
        payload = json.loads(checks[-1] or b"")
        self.assertEqual(PI_SITE.container_origin, payload["origin"])

    def test_homepage_without_a_container_origin_maps_the_tailnet_name(self) -> None:
        tailnet = replace(PI_SITE, container_origin=None)
        compose, services = homepage_documents(
            tailnet, COMPOSE, SERVICES, "100.1.2.3", "token"
        )
        self.assertIn(f'- "{PI_SITE.host}:100.1.2.3"\n', compose)
        self.assertIn(f"url: {PI_SITE.public_origin}{PI_SITE.summary_path}\n", services)

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
