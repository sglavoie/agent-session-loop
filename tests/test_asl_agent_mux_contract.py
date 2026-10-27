"""The ASL-owned compatibility contract for the AgentMux integration.

The corpus under ``tests/fixtures/agent_mux_contract`` is what AgentMux will
implement a strict consumer against, so its properties have to be checked rather
than assumed. This module checks them from both directions: that the corpus
describes itself consistently (schemas lint clean, the manifest matches the
bytes beside it, identity is derived from those bytes and only from those
bytes), and that the validator behind it actually refuses what the schemas
forbid — unknown fields, invalid enums, oversized values, leaked bodies, and
digest mismatches — rather than passing everything and reporting nothing.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import agent_mux_contract as contract

ASL_TEST_PORTABLE = True

CORPUS = contract.corpus_root()


def _reasons(violations: list[contract.Violation]) -> set[str]:
    return {violation.reason for violation in violations}


class CorpusIntegrityTests(unittest.TestCase):
    """The checked-in corpus is internally consistent."""

    def test_corpus_checks_clean(self) -> None:
        report = contract.check_corpus()
        self.assertEqual(
            report.schema_violations, dict.fromkeys(contract.COMPONENTS, [])
        )
        self.assertEqual(report.manifest_violations, [])
        self.assertEqual([fixture.to_dict() for fixture in report.fixture_failures], [])
        self.assertEqual(report.noncanonical, [])
        self.assertTrue(report.ok)

    def test_every_component_carries_both_fixture_polarities(self) -> None:
        for component in contract.COMPONENTS:
            with self.subTest(component=component):
                positive = contract.fixture_paths(component, valid=True)
                negative = contract.fixture_paths(component, valid=False)
                self.assertTrue(positive, "component has no accepting fixture")
                self.assertTrue(negative, "component has no rejecting fixture")

    def test_every_declared_root_carries_an_accepting_fixture(self) -> None:
        # A root is a frame a producer may put on the wire, so a root nobody
        # pinned is a frame the consumer's implementation is guessing at. The
        # per-component polarity check above cannot see this: one fixture makes
        # a whole component look covered however many roots it declares.
        for component in contract.COMPONENTS:
            roots = set(contract.load_schema(component).get("roots") or {})
            if not roots:
                continue
            pinned = {
                json.loads(path.read_text(encoding="utf-8"))["root"]
                for path in contract.fixture_paths(component, valid=True)
            }
            with self.subTest(component=component):
                self.assertEqual(roots - pinned, set())

    def test_negative_fixtures_cover_every_contract_property(self) -> None:
        # These are the refusals the session's completion contract names. A
        # corpus that stopped exercising one of them would still check clean,
        # which is exactly the regression this test exists to catch.
        required = {
            contract.REASON_UNKNOWN_FIELD,
            contract.REASON_INVALID_ENUM,
            contract.REASON_STRING_TOO_LONG,
            contract.REASON_BODY_NOT_ALLOWED,
            contract.REASON_CONST_MISMATCH,
            contract.REASON_MISSING_FIELD,
            contract.REASON_NULL_NOT_ALLOWED,
            contract.REASON_TYPE_MISMATCH,
            contract.REASON_INVALID_FORMAT,
            contract.REASON_PATTERN_MISMATCH,
            contract.REASON_ARRAY_TOO_LONG,
        }
        declared = set()
        for component in contract.COMPONENTS:
            for path in contract.fixture_paths(component, valid=False):
                declared.add(json.loads(path.read_text(encoding="utf-8"))["reason"])
        self.assertEqual(required - declared, set())

    def test_schemas_lint_clean(self) -> None:
        for component in contract.COMPONENTS:
            with self.subTest(component=component):
                schema = contract.load_schema(component)
                self.assertEqual(contract.lint_schema(schema), [])
                self.assertEqual(schema["component"], component)

    def test_every_corpus_file_is_canonical(self) -> None:
        self.assertEqual(contract.noncanonical_files(), [])
        for name in contract.corpus_files():
            with self.subTest(file=name):
                raw = (CORPUS / name).read_bytes()
                self.assertEqual(
                    raw, contract.canonical_json(json.loads(raw.decode("utf-8")))
                )

    def test_manifest_regeneration_is_byte_identical(self) -> None:
        stored = (CORPUS / contract.MANIFEST_FILENAME).read_bytes()
        rebuilt = contract.canonical_json(contract.build_manifest())
        self.assertEqual(stored, rebuilt)

    def test_contract_set_id_is_derived_from_corpus_bytes(self) -> None:
        identity = contract.contract_set_id()
        self.assertRegex(identity, contract.CONTRACT_SET_ID_PATTERN)
        self.assertTrue(
            identity.endswith(
                contract.corpus_digest()[: contract.CONTRACT_SET_ID_HEX_CHARS]
            )
        )
        manifest = contract.read_manifest()
        self.assertEqual(manifest["current_contract_set_id"], identity)
        entry = contract.current_contract_set(manifest)
        self.assertEqual(entry["corpus_digest"], contract.corpus_digest())
        self.assertEqual(entry["status"], contract.CONTRACT_SET_STATUS_CURRENT)

    def test_the_body_bearing_fixture_states_its_own_size(self) -> None:
        # ``body_bytes`` is the one field in the corpus whose correct value is a
        # function of another field, and no schema keyword can say so: JSON
        # Schema bounds it but cannot tie it to ``body``. The bridge computes it
        # from the encoded body, so a fixture that disagrees would teach a
        # consumer a relationship the live wire does not have. Nothing else
        # catches that, which is why it is asserted here rather than linted.
        paths = [
            path
            for path in contract.fixture_paths(
                contract.COMPONENT_BRIDGE_PROTOCOL, valid=True
            )
            if path.stem == "question-body"
        ]
        self.assertEqual(len(paths), 1)
        fixture = json.loads(paths[0].read_text(encoding="utf-8"))
        result = fixture["document"]["result"]
        self.assertEqual(result["body_bytes"], len(result["body"].encode("utf-8")))

    def test_manifest_excludes_itself_from_the_digest(self) -> None:
        self.assertNotIn(contract.MANIFEST_FILENAME, contract.corpus_files())
        self.assertNotIn(contract.MANIFEST_FILENAME, contract.file_digests())

    def test_manifest_reports_every_component_and_capability(self) -> None:
        entry = contract.current_contract_set(contract.read_manifest())
        self.assertEqual(sorted(entry["components"]), sorted(contract.COMPONENTS))
        for component, component_entry in entry["components"].items():
            with self.subTest(component=component):
                schema = contract.load_schema(component)
                self.assertEqual(component_entry["schema_version"], schema["version"])
                self.assertEqual(
                    component_entry["digest"],
                    contract.file_digests()[component_entry["schema_path"]],
                )
                self.assertEqual(component_entry["roots"], sorted(schema["roots"]))
        capabilities = entry["capabilities"]
        self.assertIn("session.handshake", capabilities["bridge_methods"])
        self.assertIn("contract-set-mismatch", capabilities["bridge_error_codes"])
        self.assertTrue(
            set(capabilities["bridge_mutation_methods"])
            <= set(capabilities["bridge_methods"])
        )


class DriftDetectionTests(unittest.TestCase):
    """A corpus edited on one side stops checking clean."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="asl-amx-contract-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.copy = self.tmp / "corpus"
        shutil.copytree(CORPUS, self.copy)

    def _rewrite(self, relative: str, mutate: Any) -> None:
        path = self.copy / relative
        document = json.loads(path.read_text(encoding="utf-8"))
        mutate(document)
        path.write_bytes(contract.canonical_json(document))

    def test_baseline_copy_checks_clean(self) -> None:
        self.assertTrue(contract.check_corpus(self.copy).ok)

    def test_editing_a_schema_moves_the_contract_set_id(self) -> None:
        before = contract.contract_set_id(self.copy)
        self._rewrite(
            "schemas/projection-v1.json",
            lambda schema: schema["definitions"]["Phase"]["enum"].append("resuming"),
        )
        after = contract.contract_set_id(self.copy)
        self.assertNotEqual(before, after)
        self.assertRegex(after, contract.CONTRACT_SET_ID_PATTERN)

    def test_a_stale_manifest_is_a_digest_mismatch(self) -> None:
        self._rewrite(
            "schemas/projection-v1.json",
            lambda schema: schema["definitions"]["Phase"]["enum"].append("resuming"),
        )
        report = contract.check_corpus(self.copy)
        self.assertFalse(report.ok)
        self.assertEqual(
            _reasons(report.manifest_violations), {contract.REASON_DIGEST_MISMATCH}
        )
        contract.write_manifest(self.copy)
        self.assertEqual(contract.manifest_violations(self.copy), [])

    def test_noncanonical_bytes_are_reported(self) -> None:
        path = self.copy / "schemas" / "projection-v1.json"
        path.write_text(
            json.dumps(json.loads(path.read_text(encoding="utf-8"))), encoding="utf-8"
        )
        self.assertIn(
            "schemas/projection-v1.json", contract.noncanonical_files(self.copy)
        )

    def test_a_fixture_that_stops_failing_is_reported(self) -> None:
        # Repair the leak the negative fixture exists to catch. The corpus must
        # notice that a rejection fixture is no longer rejected, rather than
        # quietly counting it as a pass.
        self._rewrite(
            "fixtures/projection-v1/invalid/question-body-leak.json",
            lambda fixture: fixture["document"].pop("question_body"),
        )
        contract.write_manifest(self.copy)
        report = contract.check_corpus(self.copy)
        self.assertFalse(report.ok)
        failures = [fixture.case for fixture in report.fixture_failures]
        self.assertEqual(failures, ["question-body-leak"])

    def test_a_missing_manifest_is_reported_not_raised(self) -> None:
        (self.copy / contract.MANIFEST_FILENAME).unlink()
        report = contract.check_corpus(self.copy)
        self.assertFalse(report.ok)
        self.assertEqual(
            _reasons(report.manifest_violations), {contract.REASON_DIGEST_MISMATCH}
        )
        payload = contract.report_payload(report, state_root="/state")
        self.assertIsNone(payload["manifest_version"])
        self.assertEqual(payload["state_root"], "/state")


class SchemaLintTests(unittest.TestCase):
    """The linter refuses the schema shapes the contract rules out."""

    def _schema(self, **overrides: Any) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "$schema": contract.SCHEMA_MARKER,
            "$id": "asl-amx/example",
            "title": "example",
            "component": contract.COMPONENT_PROJECTION,
            "version": 1,
            "max_document_bytes": 1024,
            "forbidden_property_names": ["body"],
            "body_bearing_pointers": [],
            "roots": {"example": "Example"},
            "definitions": {
                "Example": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"name": {"type": "string", "maxLength": 8}},
                    "required": ["name"],
                }
            },
        }
        schema.update(overrides)
        return schema

    def test_the_example_schema_lints_clean(self) -> None:
        self.assertEqual(contract.lint_schema(self._schema()), [])

    def test_an_open_object_is_refused(self) -> None:
        schema = self._schema()
        del schema["definitions"]["Example"]["additionalProperties"]
        self.assertEqual(
            _reasons(contract.lint_schema(schema)), {contract.REASON_SCHEMA_INVALID}
        )

    def test_an_unbounded_string_is_refused(self) -> None:
        schema = self._schema()
        schema["definitions"]["Example"]["properties"]["name"] = {"type": "string"}
        self.assertEqual(
            _reasons(contract.lint_schema(schema)), {contract.REASON_SCHEMA_INVALID}
        )

    def test_an_unbounded_array_is_refused(self) -> None:
        schema = self._schema()
        schema["definitions"]["Example"]["properties"]["name"] = {
            "type": "array",
            "items": {"type": "string", "maxLength": 4},
        }
        self.assertEqual(
            _reasons(contract.lint_schema(schema)), {contract.REASON_SCHEMA_INVALID}
        )

    def test_an_unknown_keyword_is_refused(self) -> None:
        schema = self._schema()
        schema["definitions"]["Example"]["properties"]["name"]["oneOf"] = []
        self.assertEqual(
            _reasons(contract.lint_schema(schema)), {contract.REASON_SCHEMA_INVALID}
        )

    def test_an_unreachable_definition_is_refused(self) -> None:
        schema = self._schema()
        schema["definitions"]["Orphan"] = {"type": "string", "maxLength": 4}
        self.assertEqual(
            _reasons(contract.lint_schema(schema)), {contract.REASON_SCHEMA_INVALID}
        )

    def test_an_undeclared_body_bearing_property_is_refused(self) -> None:
        schema = self._schema()
        schema["definitions"]["Example"]["properties"]["body"] = {
            "type": "string",
            "maxLength": 8,
        }
        schema["definitions"]["Example"]["required"] = ["body", "name"]
        problems = contract.lint_schema(schema)
        self.assertEqual(_reasons(problems), {contract.REASON_SCHEMA_INVALID})
        self.assertEqual(
            {problem.pointer for problem in problems}, {"/body_bearing_pointers"}
        )

    def test_a_declared_body_bearing_property_lints_clean(self) -> None:
        schema = self._schema(body_bearing_pointers=["/body"])
        schema["definitions"]["Example"]["properties"]["body"] = {
            "type": "string",
            "maxLength": 8,
        }
        schema["definitions"]["Example"]["required"] = ["body", "name"]
        self.assertEqual(contract.lint_schema(schema), [])

    def test_a_stale_exemption_is_refused(self) -> None:
        # The failure mode that matters: an exemption outliving the field it
        # was written for keeps permitting prose nobody declares any more.
        schema = self._schema(body_bearing_pointers=["/result/body"])
        self.assertEqual(
            _reasons(contract.lint_schema(schema)), {contract.REASON_SCHEMA_INVALID}
        )


class DocumentValidationTests(unittest.TestCase):
    """The validator enforces what the real schemas declare."""

    def setUp(self) -> None:
        self.projection = contract.load_schema(contract.COMPONENT_PROJECTION)
        self.hook = contract.load_schema(contract.COMPONENT_LIFECYCLE_HOOK)
        self.bridge = contract.load_schema(contract.COMPONENT_BRIDGE_PROTOCOL)

    def _fixture(self, component: str, case: str) -> Any:
        for path in contract.fixture_paths(component, valid=True):
            if path.stem == case:
                return json.loads(path.read_text(encoding="utf-8"))["document"]
        raise AssertionError(f"no valid fixture named {case}")

    def _live_turn(self) -> dict[str, Any]:
        return dict(self._fixture(contract.COMPONENT_PROJECTION, "live-turn"))

    def test_a_valid_projection_is_accepted(self) -> None:
        self.assertEqual(
            contract.validate_document(self.projection, self._live_turn()), []
        )

    def test_an_unknown_field_is_rejected(self) -> None:
        document = self._live_turn()
        document["active_session"] = "3"
        problems = contract.validate_document(self.projection, document)
        self.assertEqual(_reasons(problems), {contract.REASON_UNKNOWN_FIELD})
        self.assertEqual([problem.pointer for problem in problems], ["/active_session"])

    def test_an_invalid_enum_is_rejected(self) -> None:
        document = self._live_turn()
        document["phase"] = "sleeping"
        self.assertEqual(
            _reasons(contract.validate_document(self.projection, document)),
            {contract.REASON_INVALID_ENUM},
        )

    def test_a_boolean_is_not_the_number_a_const_names(self) -> None:
        """Python says ``True == 1``; JSON Schema says a boolean is not a number."""
        document = self._live_turn()
        document["schema_version"] = True
        self.assertEqual(
            _reasons(contract.validate_document(self.projection, document)),
            {contract.REASON_CONST_MISMATCH},
        )

    def test_const_and_enum_compare_json_types_not_python_equality(self) -> None:
        schema = {
            "roots": {"doc": "Doc"},
            "forbidden_property_names": [],
            "body_bearing_pointers": [],
            "max_document_bytes": 10000,
            "definitions": {
                "Doc": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["flag", "kind"],
                    "properties": {"flag": {"const": False}, "kind": {"enum": [0, 1]}},
                }
            },
        }
        self.assertEqual(
            contract.validate_document(schema, {"flag": False, "kind": 1.0}), []
        )
        self.assertEqual(
            _reasons(contract.validate_document(schema, {"flag": 0, "kind": True})),
            {contract.REASON_CONST_MISMATCH, contract.REASON_INVALID_ENUM},
        )

    def test_an_oversized_string_is_rejected(self) -> None:
        document = self._live_turn()
        document["question_id"] = "q" * 129
        self.assertEqual(
            _reasons(contract.validate_document(self.projection, document)),
            {contract.REASON_STRING_TOO_LONG},
        )

    def test_an_oversized_document_is_rejected(self) -> None:
        document = self._live_turn()
        document["run_id"] = "r" * 120
        ceiling = self.projection["max_document_bytes"]
        tight = dict(self.projection, max_document_bytes=64)
        self.assertGreater(ceiling, 64)
        self.assertIn(
            contract.REASON_DOCUMENT_TOO_LARGE,
            _reasons(contract.validate_document(tight, document)),
        )

    def test_null_is_refused_where_the_schema_forbids_it(self) -> None:
        document = self._live_turn()
        document["run_id"] = None
        self.assertEqual(
            _reasons(contract.validate_document(self.projection, document)),
            {contract.REASON_NULL_NOT_ALLOWED},
        )

    def test_null_is_accepted_where_the_schema_allows_it(self) -> None:
        document = self._live_turn()
        document["workspace_root"] = None
        document["harness"] = None
        self.assertEqual(contract.validate_document(self.projection, document), [])

    def test_a_missing_field_is_rejected(self) -> None:
        document = self._live_turn()
        del document["updated_at"]
        self.assertEqual(
            _reasons(contract.validate_document(self.projection, document)),
            {contract.REASON_MISSING_FIELD},
        )

    def test_only_one_utc_timestamp_spelling_is_accepted(self) -> None:
        for spelling in (
            "2026-06-19T14:00:00Z",
            "2026-06-19T14:00:00",
            "2026-06-19T14:00:00+02:00",
            "2026-06-19 14:00:00+00:00",
            "1782230400",
        ):
            with self.subTest(timestamp=spelling):
                document = self._live_turn()
                document["started_at"] = spelling
                self.assertEqual(
                    _reasons(contract.validate_document(self.projection, document)),
                    {contract.REASON_INVALID_FORMAT},
                )

    def test_a_noncanonical_path_is_rejected(self) -> None:
        for path in (
            "plans/plan-alpha",
            "/plans/../plans/plan-alpha",
            "/plans/./plan-alpha",
            "/plans//plan-alpha",
            "/plans/plan-alpha/",
        ):
            with self.subTest(path=path):
                document = self._live_turn()
                document["plan_dir"] = path
                self.assertEqual(
                    _reasons(contract.validate_document(self.projection, document)),
                    {contract.REASON_INVALID_FORMAT},
                )

    def test_a_body_leak_is_reported_as_a_body_leak(self) -> None:
        # Not as `unknown-field`: the privacy sweep runs before the
        # schema-guided walk precisely so the diagnosis names the real problem.
        document = self._live_turn()
        document["question_body"] = "Should we drop the v1 column?"
        problems = contract.validate_document(self.projection, document)
        self.assertIn(contract.REASON_BODY_NOT_ALLOWED, _reasons(problems))
        self.assertIn("/question_body", [problem.pointer for problem in problems])

    def test_a_nested_body_leak_is_found(self) -> None:
        document = json.loads(
            json.dumps(
                self._fixture(contract.COMPONENT_BRIDGE_PROTOCOL, "runs-snapshot")
            )
        )
        document["result"]["rows"][1]["question"] = {"body": "leaked"}
        problems = contract.validate_document(
            self.bridge, document, root="runs_snapshot_response"
        )
        self.assertIn(contract.REASON_BODY_NOT_ALLOWED, _reasons(problems))
        self.assertIn(
            "/result/rows/1/question/body", [problem.pointer for problem in problems]
        )

    def test_the_declared_body_pointer_is_the_only_exemption(self) -> None:
        document = json.loads(
            json.dumps(
                self._fixture(contract.COMPONENT_BRIDGE_PROTOCOL, "question-body")
            )
        )
        self.assertEqual(
            contract.validate_document(
                self.bridge, document, root="question_body_response"
            ),
            [],
        )
        moved = json.loads(json.dumps(document))
        moved["result"]["body"] = None
        moved["error_body"] = {"body": "elsewhere"}
        self.assertIn(
            contract.REASON_BODY_NOT_ALLOWED,
            _reasons(
                contract.validate_document(
                    self.bridge, moved, root="question_body_response"
                )
            ),
        )

    def test_an_array_ceiling_is_enforced(self) -> None:
        document = json.loads(
            json.dumps(
                self._fixture(contract.COMPONENT_BRIDGE_PROTOCOL, "runs-snapshot")
            )
        )
        document["result"]["warnings"] = [f"w{index}" for index in range(9)]
        self.assertEqual(
            _reasons(
                contract.validate_document(
                    self.bridge, document, root="runs_snapshot_response"
                )
            ),
            {contract.REASON_ARRAY_TOO_LONG},
        )

    def test_a_duplicate_array_item_is_rejected(self) -> None:
        document = json.loads(
            json.dumps(self._fixture(contract.COMPONENT_BRIDGE_PROTOCOL, "hello"))
        )
        document["capabilities"] = [*document["capabilities"], "runs.snapshot"]
        self.assertEqual(
            _reasons(contract.validate_document(self.bridge, document, root="hello")),
            {contract.REASON_DUPLICATE_ITEM},
        )

    def test_a_naming_a_root_is_required_when_a_schema_has_several(self) -> None:
        document = self._fixture(contract.COMPONENT_BRIDGE_PROTOCOL, "hello")
        self.assertEqual(
            _reasons(contract.validate_document(self.bridge, document)),
            {contract.REASON_UNKNOWN_ROOT},
        )
        self.assertEqual(
            _reasons(
                contract.validate_document(self.bridge, document, root="not-a-root")
            ),
            {contract.REASON_UNKNOWN_ROOT},
        )

    def test_the_sequence_is_a_string_and_is_not_coerced(self) -> None:
        document = dict(
            self._fixture(contract.COMPONENT_LIFECYCLE_HOOK, "turn-started")
        )
        document["source_sequence"] = 41
        self.assertEqual(
            _reasons(contract.validate_document(self.hook, document)),
            {contract.REASON_TYPE_MISMATCH},
        )

    def test_an_integer_satisfies_a_number(self) -> None:
        document = json.loads(
            json.dumps(
                self._fixture(contract.COMPONENT_BRIDGE_PROTOCOL, "runs-snapshot")
            )
        )
        document["result"]["projection"]["age_seconds"] = 0
        self.assertEqual(
            contract.validate_document(
                self.bridge, document, root="runs_snapshot_response"
            ),
            [],
        )

    def test_a_boolean_does_not_satisfy_an_integer(self) -> None:
        document = self._live_turn()
        document["projection_generation"] = True
        self.assertEqual(
            _reasons(contract.validate_document(self.projection, document)),
            {contract.REASON_TYPE_MISMATCH},
        )

    def test_the_contract_set_id_format_is_enforced(self) -> None:
        for identity in ("", "asl-amx-1.", "asl-amx-1.NOTHEX0000000000", "deadbeef"):
            with self.subTest(contract_set_id=identity):
                document = self._live_turn()
                document["contract_set_id"] = identity
                self.assertEqual(
                    _reasons(contract.validate_document(self.projection, document)),
                    {contract.REASON_INVALID_FORMAT},
                )


class ReportPayloadTests(unittest.TestCase):
    """The block ``asl contract-check --agent-mux --json`` emits."""

    def test_payload_reports_the_whole_contract_identity(self) -> None:
        payload = contract.report_payload(
            contract.check_corpus(), state_root="/state/agent-session-loop"
        )
        self.assertEqual(payload["contract_set_id"], contract.contract_set_id())
        self.assertEqual(payload["corpus_digest"], contract.corpus_digest())
        self.assertEqual(payload["corpus_root"], str(CORPUS))
        self.assertEqual(payload["state_root"], "/state/agent-session-loop")
        self.assertEqual(payload["manifest_version"], contract.MANIFEST_VERSION)
        self.assertEqual(payload["issues"], [])
        self.assertEqual(
            [component["component"] for component in payload["components"]],
            list(contract.COMPONENTS),
        )
        for component in payload["components"]:
            self.assertEqual(component["schema_status"], "ok")
            self.assertIn("digest", component)
            self.assertIn("schema_version", component)
        fixtures = payload["fixtures"]
        self.assertEqual(fixtures["total"], fixtures["accepted"] + fixtures["rejected"])
        self.assertEqual(fixtures["failures"], [])

    def test_payload_is_json_serializable(self) -> None:
        payload = contract.report_payload(contract.check_corpus())
        self.assertEqual(json.loads(json.dumps(payload)), payload)


if __name__ == "__main__":
    unittest.main()
