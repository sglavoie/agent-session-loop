# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F401, F403, F405, F822, I001
"""Workbench naming and publisher integration tests."""

# ruff: noqa: F401, F403, F405
from tests.workbench_integrations_support import *


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
