from __future__ import annotations

# ruff: noqa: F403, F405
from .workbench_transport_contract_support import *


class TheTransportStaysInsideThePortablePackage(unittest.TestCase):
    """Two new modules, and neither may reach for the Mac-only runtime."""

    MODULES = ("transport.py", "credentials.py")

    def test_the_new_modules_import_nothing_but_the_standard_library(self) -> None:
        for name in self.MODULES:
            with self.subTest(module=name):
                roots = _imported_roots(PACKAGE / name)
                self.assertEqual(set(), roots - set(sys.stdlib_module_names))

    def test_the_new_modules_never_name_the_worker_runtime(self) -> None:
        # Including in a deferred import inside a function body, which is how
        # this boundary would realistically be broken.
        for name in self.MODULES:
            with self.subTest(module=name):
                source = (PACKAGE / name).read_text(encoding="utf-8")
                self.assertNotIn("agent_session_loop", source)

    def test_the_package_exports_the_transport_surface(self) -> None:
        import asl_workbench

        for name in (
            "TransportError",
            "TransportCompatibility",
            "validate_envelope",
            "validate_frame",
            "encode_frame",
            "evaluate_hello",
            "authorize",
            "authorize_upload",
            "CredentialStore",
        ):
            with self.subTest(name=name):
                self.assertIn(name, asl_workbench.__all__)
                self.assertTrue(hasattr(asl_workbench, name))


class TheTransportIsSeparateFromTheAgentMuxContract(unittest.TestCase):
    """Two protocols on one wire, and two schemas that never share a version."""

    def test_the_transport_corpus_is_outside_the_digested_tree(self) -> None:
        """A fixture in the wrong directory would move the contract set id.

        The AgentMux identity is a digest over every ``.json`` under
        ``tests/fixtures/agent_mux_contract``. A transport frame placed there
        would mark every deployed connector incompatible with a build whose
        bridge had not changed at all.
        """
        digested = set(agent_mux_contract.file_digests())
        self.assertTrue(digested)
        for path in sorted(CORPUS.rglob("*.json")):
            with self.subTest(fixture=path.name):
                self.assertNotIn(path.name, {Path(n).name for n in digested})
        self.assertEqual(CONTRACT_SET_ID, agent_mux_contract.contract_set_id())

    def test_the_contract_set_id_travels_as_data_not_as_a_version(self) -> None:
        # The transport version gates the frame; the contract set id rides
        # inside it and is judged by the handshake. A frame from a build with
        # an unknown contract set is still a well-formed frame.
        envelope = fixture("observation-heartbeat")
        envelope["payload"]["contract_set_id"] = "asl-amx-1.deadbeefdeadbeef"
        checked = transport.validate_envelope(
            envelope, direction=transport.DIRECTION_UP, target_id=SERVICE_ID
        )
        self.assertEqual(
            "asl-amx-1.deadbeefdeadbeef", checked["payload"]["contract_set_id"]
        )
        self.assertIsInstance(transport.TRANSPORT_PROTOCOL_VERSION, int)
        self.assertEqual(
            (transport.TRANSPORT_PROTOCOL_VERSION,),
            transport.SUPPORTED_TRANSPORT_PROTOCOLS,
        )

    def test_a_transport_version_this_build_cannot_read_is_its_own_refusal(
        self,
    ) -> None:
        envelope = fixture("observation-heartbeat")
        envelope["protocol"] = transport.TRANSPORT_PROTOCOL_VERSION + 1
        with self.assertRaises(transport.TransportError) as caught:
            transport.validate_envelope(envelope, direction=transport.DIRECTION_UP)
        self.assertEqual(transport.ERROR_INCOMPATIBLE_PROTOCOL, caught.exception.code)
        self.assertNotIn("contract", caught.exception.detail)
