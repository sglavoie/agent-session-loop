from __future__ import annotations

# ruff: noqa: F403, F405
from .workbench_transport_contract_support import *


class RolesSeparateWhatEachEndMayDo(unittest.TestCase):
    """The Companion's role separation, now bound to envelope kinds."""

    def setUp(self) -> None:
        self.store = credentials.CredentialStore()
        self.connector, self.connector_token = credentials.issue(
            credentials.ROLE_CONNECTOR, created_at=NOW, subject=CONNECTOR_ID
        )
        self.summary, self.summary_token = credentials.issue(
            credentials.ROLE_SUMMARY, created_at=NOW
        )
        self.store.add(self.connector)
        self.store.add(self.summary)
        self.envelope = fixture("observation-heartbeat")

    def bearer(self, token: str) -> str:
        return f"Bearer {token}"

    def test_a_connector_credential_uploads_under_its_own_subject(self) -> None:
        granted = credentials.authorize_upload(
            self.store, self.envelope, authorization=self.bearer(self.connector_token)
        )
        self.assertEqual(credentials.ROLE_CONNECTOR, granted.role)
        self.assertEqual(CONNECTOR_ID, granted.subject)
        self.assertEqual(self.connector.credential_id, granted.credential_id)

    def test_a_connector_credential_cannot_upload_under_another_name(self) -> None:
        """The binding a token alone cannot provide.

        The envelope names its own ``connector_id`` and the store keys
        everything by it, so without the subject check a leaked connector token
        would be a licence to file observations under any Mac's name.
        """
        stranger = dict(self.envelope, connector_id="mac-mini")
        with self.assertRaises(transport.TransportError) as caught:
            credentials.authorize_upload(
                self.store, stranger, authorization=self.bearer(self.connector_token)
            )
        self.assertEqual(transport.ERROR_UNAUTHORIZED, caught.exception.code)

    def test_a_summary_credential_cannot_upload_anything(self) -> None:
        with self.assertRaises(transport.TransportError) as caught:
            credentials.authorize_upload(
                self.store, self.envelope, authorization=self.bearer(self.summary_token)
            )
        self.assertEqual(transport.ERROR_UNAUTHORIZED, caught.exception.code)
        self.assertEqual(frozenset(), credentials.ROLE_CAPABILITIES["summary"])

    def test_no_credential_at_all_is_a_refusal_and_not_a_default(self) -> None:
        for header in (None, "", "Basic abc", "Bearer", "Bearer nonsense"):
            with self.subTest(header=header):
                with self.assertRaises(transport.TransportError) as caught:
                    credentials.authorize_upload(
                        self.store, self.envelope, authorization=header
                    )
                self.assertEqual(transport.ERROR_UNAUTHORIZED, caught.exception.code)

    def test_only_the_bearer_scheme_is_read(self) -> None:
        # A service that also accepted Basic would accept a credential a
        # browser can be talked into sending automatically.
        self.assertEqual(
            self.connector_token,
            credentials.parse_authorization(self.bearer(self.connector_token)),
        )
        self.assertIsNone(
            credentials.parse_authorization(f"Basic {self.connector_token}")
        )
        self.assertIsNone(credentials.parse_authorization(None))
        self.assertEqual(
            self.connector_token,
            credentials.parse_authorization(f"bearer {self.connector_token}"),
        )

    def test_a_connector_cannot_send_the_envelope_the_service_sends(self) -> None:
        request = fixture("request-catalog-publish")
        with self.assertRaises(transport.TransportError) as caught:
            credentials.authorize_upload(
                self.store, request, authorization=self.bearer(self.connector_token)
            )
        self.assertEqual(transport.ERROR_UNAUTHORIZED, caught.exception.code)

    def test_every_upload_kind_is_the_connectors_and_only_the_connectors(
        self,
    ) -> None:
        for kind in transport.KINDS_BY_DIRECTION[transport.DIRECTION_UP]:
            with self.subTest(kind=kind):
                self.assertEqual(
                    frozenset({credentials.ROLE_CONNECTOR}),
                    credentials.ROLES_BY_KIND[kind],
                )
        self.assertEqual(
            frozenset({credentials.ROLE_OWNER}),
            credentials.ROLES_BY_KIND[transport.KIND_REQUEST],
        )

    def test_a_credential_that_speaks_for_nobody_uploads_nothing(self) -> None:
        loose, token = credentials.issue(
            credentials.ROLE_CONNECTOR, created_at=NOW, label="no subject"
        )
        store_with_loose = credentials.CredentialStore([loose])
        with self.assertRaises(transport.TransportError) as caught:
            credentials.authorize_upload(
                store_with_loose, self.envelope, authorization=self.bearer(token)
            )
        self.assertEqual(transport.ERROR_UNAUTHORIZED, caught.exception.code)

    def test_a_disabled_credential_proves_nothing(self) -> None:
        disabled = credentials.Credential(
            credential_id=self.connector.credential_id,
            role=self.connector.role,
            salt=self.connector.salt,
            digest=self.connector.digest,
            created_at=self.connector.created_at,
            subject=self.connector.subject,
            disabled=True,
        )
        revoked = credentials.CredentialStore([disabled])
        self.assertIsNone(revoked.authenticate(self.connector_token))

    def test_a_wrong_secret_and_an_unknown_id_fail_the_same_way(self) -> None:
        # A caller that could tell them apart could enumerate the file's
        # contents through the front door.
        credential_id, _, secret = self.connector_token.partition(
            credentials.TOKEN_SEPARATOR
        )
        self.assertIsNone(self.store.authenticate(f"{credential_id}.wrong"))
        self.assertIsNone(self.store.authenticate(f"deadbeef.{secret}"))
        self.assertIsNone(self.store.authenticate("no-separator"))
        self.assertIsNone(self.store.authenticate(None))

    def test_the_owner_is_an_identity_rather_than_a_token(self) -> None:
        owner = credentials.resolve_role(
            self.store,
            identity="Owner@Example.COM",
            owner="owner@example.com",
        )
        self.assertEqual(credentials.ROLE_OWNER, owner.role)
        self.assertIsNone(owner.credential_id)
        self.assertTrue(owner.permits(transport.CAPABILITY_DETAIL))
        self.assertFalse(owner.permits(transport.CAPABILITY_MUTATE))

    def test_an_identity_outranks_a_token_the_same_caller_also_holds(self) -> None:
        # Otherwise a debugging header would quietly downgrade a browser
        # session to whatever token happened to be attached.
        resolved = credentials.resolve_role(
            self.store,
            authorization=self.bearer(self.summary_token),
            identity="owner@example.com",
            owner="owner@example.com",
        )
        self.assertEqual(credentials.ROLE_OWNER, resolved.role)

    def test_an_unconfigured_owner_is_never_matched(self) -> None:
        for configured, presented in ((None, "x@y"), ("", "x@y"), ("  ", "  ")):
            with self.subTest(configured=configured):
                self.assertFalse(credentials.owner_matches(configured, presented))

    def test_a_caller_proving_nothing_is_public_rather_than_refused(self) -> None:
        # The refusal belongs to what is being asked for, not to the act of
        # asking.
        resolved = credentials.resolve_role(self.store, authorization="Bearer nope")
        self.assertEqual(credentials.ROLE_PUBLIC, resolved.role)
        self.assertEqual(frozenset(), credentials.ROLE_CAPABILITIES["public"])
        for capability in transport.TRANSPORT_CAPABILITIES:
            self.assertFalse(resolved.permits(capability))

    def test_only_two_roles_may_be_issued(self) -> None:
        # An owner is an identity and the public is everybody; neither is
        # something a token can mint.
        for role in (credentials.ROLE_OWNER, credentials.ROLE_PUBLIC):
            with (
                self.subTest(role=role),
                self.assertRaises(credentials.CredentialError),
            ):
                credentials.issue(role, created_at=NOW)

    def test_a_duplicate_credential_id_is_refused(self) -> None:
        with self.assertRaises(credentials.CredentialError):
            self.store.add(self.connector)


class TheCredentialFileIsTheCompanionsFile(unittest.TestCase):
    """Ported, not reinvented: an installed file has to keep working."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "credentials.json"

    def test_a_saved_file_round_trips_and_is_owner_readable_only(self) -> None:
        credential, token = credentials.issue(
            credentials.ROLE_CONNECTOR, created_at=NOW, subject=CONNECTOR_ID
        )
        original = credentials.CredentialStore([credential])
        original.save(self.path)
        mode = self.path.stat().st_mode
        self.assertEqual(0, mode & (stat.S_IRWXG | stat.S_IRWXO))

        reloaded = credentials.CredentialStore.load(self.path)
        self.assertEqual(original.as_document(), reloaded.as_document())
        self.assertIsNotNone(reloaded.authenticate(token))

    def test_a_file_anyone_can_read_is_refused_rather_than_used(self) -> None:
        credential, _ = credentials.issue(
            credentials.ROLE_SUMMARY, created_at=NOW, label="kuma"
        )
        credentials.CredentialStore([credential]).save(self.path)
        os.chmod(self.path, 0o644)
        with self.assertRaises(credentials.CredentialError):
            credentials.CredentialStore.load(self.path)

    def test_a_file_written_by_the_companion_loads_unchanged(self) -> None:
        """The interop claim, written out by hand rather than by this module.

        The digest is an HMAC-SHA256 over the secret keyed by the credential's
        own salt, exactly as ``companion/auth.py`` computes it. Recomputing it
        here proves the algorithm rather than asserting the two files look
        alike.
        """
        salt = "5f0d2c1b9a8877665544332211aabbcc"
        secret = "a-secret-the-companion-handed-out-once"
        digest = hmac_new(
            bytes.fromhex(salt), secret.encode("utf-8"), sha256
        ).hexdigest()
        document = {
            "version": credentials.CREDENTIALS_SCHEMA_VERSION,
            "credentials": [
                {
                    "credential_id": "0011223344556677",
                    "role": "connector",
                    "salt": salt,
                    "digest": digest,
                    "created_at": NOW,
                    "subject": CONNECTOR_ID,
                    "label": "mac studio",
                }
            ],
        }
        loaded = credentials.CredentialStore.from_document(document)
        self.assertEqual(1, len(loaded))
        credential = loaded.authenticate(f"0011223344556677.{secret}")
        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(CONNECTOR_ID, credential.subject)
        self.assertEqual(document, loaded.as_document())

    def test_an_unreadable_or_unknown_file_is_refused_by_name(self) -> None:
        for document, why in (
            ([], "not an object"),
            ({"version": 99, "credentials": []}, "unsupported schema"),
            ({"version": 1, "credentials": {}}, "credentials not an array"),
            ({"version": 1, "credentials": [], "extra": 1}, "unknown key"),
        ):
            with (
                self.subTest(case=why),
                self.assertRaises(credentials.CredentialError),
            ):
                credentials.CredentialStore.from_document(document)

    def test_a_missing_file_is_reported_rather_than_treated_as_empty(self) -> None:
        with self.assertRaises(credentials.CredentialError):
            credentials.CredentialStore.load(self.path)
