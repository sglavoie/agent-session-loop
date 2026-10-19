"""The ASL-owned compatibility contract for the AgentMux integration.

ASL is the sole run authority, so ASL — not AgentMux — owns the schemas the two
components exchange, the fixtures that pin them, and the identity a consumer
checks before it trusts either. This module is that ownership expressed as code:
it loads the canonical corpus under ``tests/fixtures/agent_mux_contract``,
validates documents against it, derives the contract-set ID from the corpus
bytes, and builds the compatibility manifest that maps that ID to component
schema versions and supported protocol capabilities.

Three components are covered:

- **projection v1** — the ``agent_mux`` block ASL writes into ``state.json``.
- **lifecycle hook v2** — the payload ASL pipes to ``amx hook-indicator``.
- **bridge protocol v2** — the NDJSON frames ``asl bridge serve`` writes.

Four properties are structural rather than incidental:

- **Every field is closed.** Objects declare ``additionalProperties: false``,
  every string carries an enum, a const, a format, or a ``maxLength``, and every
  array carries a ``maxItems``. A field nobody declared is a rejection, not a
  value to ignore, so a producer cannot widen the wire without moving the
  contract-set ID.
- **The corpus is body-free by construction.** Question and reply prose is
  refused by name anywhere in a document, at every depth, before any
  schema-guided walk runs. The single body-bearing frame declares its exemption
  as an explicit JSON pointer in its own schema, so the exception is data rather
  than a special case in the validator.
- **Identity is derived, never declared.** The contract-set ID is a digest over
  the canonical schema and fixture bytes in sorted path order. Editing any byte
  of the corpus moves it; nothing else can.
- **Generation is byte-identical.** Every corpus file is stored in one canonical
  JSON rendering, and the manifest is rebuilt from the corpus rather than
  maintained by hand, so regeneration is a no-op or a diff.

This module does not activate v2 anywhere. It publishes the contract; the
runtime components adopt it in later work.


Implementation is split into focused sibling modules; exports here preserve
the existing import surface. Patch collaborators in their owning modules.
"""

from __future__ import annotations

from .agent_mux_contract_cli import (
    _print_violations as _print_violations,
)
from .agent_mux_contract_cli import (
    main as main,
)
from .agent_mux_contract_corpus import (
    _FIXTURE_KEYS as _FIXTURE_KEYS,
)
from .agent_mux_contract_corpus import (
    _INVALID_FIXTURE_KEYS as _INVALID_FIXTURE_KEYS,
)
from .agent_mux_contract_corpus import (
    CorpusReport as CorpusReport,
)
from .agent_mux_contract_corpus import (
    FixtureResult as FixtureResult,
)
from .agent_mux_contract_corpus import (
    _check_fixture as _check_fixture,
)
from .agent_mux_contract_corpus import (
    check_corpus as check_corpus,
)
from .agent_mux_contract_corpus import (
    fixture_paths as fixture_paths,
)
from .agent_mux_contract_corpus import (
    noncanonical_files as noncanonical_files,
)
from .agent_mux_contract_corpus import (
    report_payload as report_payload,
)
from .agent_mux_contract_formats import (
    _EPOCH_SECONDS_RE as _EPOCH_SECONDS_RE,
)
from .agent_mux_contract_formats import (
    _FORMAT_CHECKS as _FORMAT_CHECKS,
)
from .agent_mux_contract_formats import (
    _JSON_TYPES as _JSON_TYPES,
)
from .agent_mux_contract_formats import (
    _MAX_PATH_CHARS as _MAX_PATH_CHARS,
)
from .agent_mux_contract_formats import (
    _NODE_KEYS as _NODE_KEYS,
)
from .agent_mux_contract_formats import (
    _QUALIFIED_PATH_RE as _QUALIFIED_PATH_RE,
)
from .agent_mux_contract_formats import (
    _SCHEMA_TOP_LEVEL_KEYS as _SCHEMA_TOP_LEVEL_KEYS,
)
from .agent_mux_contract_formats import (
    _SCHEMA_TOP_LEVEL_REQUIRED as _SCHEMA_TOP_LEVEL_REQUIRED,
)
from .agent_mux_contract_formats import (
    _SEQUENCE_RE as _SEQUENCE_RE,
)
from .agent_mux_contract_formats import (
    _SHA256_RE as _SHA256_RE,
)
from .agent_mux_contract_formats import (
    _TIMESTAMP_RE as _TIMESTAMP_RE,
)
from .agent_mux_contract_formats import (
    FORMAT_CANONICAL_PATH as FORMAT_CANONICAL_PATH,
)
from .agent_mux_contract_formats import (
    FORMAT_CONTRACT_SET_ID as FORMAT_CONTRACT_SET_ID,
)
from .agent_mux_contract_formats import (
    FORMAT_EPOCH_SECONDS as FORMAT_EPOCH_SECONDS,
)
from .agent_mux_contract_formats import (
    FORMAT_QUALIFIED_PATH as FORMAT_QUALIFIED_PATH,
)
from .agent_mux_contract_formats import (
    FORMAT_SEQUENCE as FORMAT_SEQUENCE,
)
from .agent_mux_contract_formats import (
    FORMAT_SHA256 as FORMAT_SHA256,
)
from .agent_mux_contract_formats import (
    FORMAT_TIMESTAMP as FORMAT_TIMESTAMP,
)
from .agent_mux_contract_formats import (
    FORMATS as FORMATS,
)
from .agent_mux_contract_formats import (
    SCHEMA_MARKER as SCHEMA_MARKER,
)
from .agent_mux_contract_formats import (
    _canonical_path_ok as _canonical_path_ok,
)
from .agent_mux_contract_formats import (
    _timestamp_ok as _timestamp_ok,
)
from .agent_mux_contract_manifest import (
    _capabilities as _capabilities,
)
from .agent_mux_contract_manifest import (
    _read_json as _read_json,
)
from .agent_mux_contract_manifest import (
    build_manifest as build_manifest,
)
from .agent_mux_contract_manifest import (
    contract_set_id as contract_set_id,
)
from .agent_mux_contract_manifest import (
    corpus_digest as corpus_digest,
)
from .agent_mux_contract_manifest import (
    corpus_files as corpus_files,
)
from .agent_mux_contract_manifest import (
    corpus_root as corpus_root,
)
from .agent_mux_contract_manifest import (
    current_contract_set as current_contract_set,
)
from .agent_mux_contract_manifest import (
    file_digests as file_digests,
)
from .agent_mux_contract_manifest import (
    load_schema as load_schema,
)
from .agent_mux_contract_manifest import (
    manifest_violations as manifest_violations,
)
from .agent_mux_contract_manifest import (
    read_manifest as read_manifest,
)
from .agent_mux_contract_manifest import (
    write_manifest as write_manifest,
)
from .agent_mux_contract_models import (
    COMPONENT_BRIDGE_PROTOCOL as COMPONENT_BRIDGE_PROTOCOL,
)
from .agent_mux_contract_models import (
    COMPONENT_LIFECYCLE_HOOK as COMPONENT_LIFECYCLE_HOOK,
)
from .agent_mux_contract_models import (
    COMPONENT_PROJECTION as COMPONENT_PROJECTION,
)
from .agent_mux_contract_models import (
    COMPONENTS as COMPONENTS,
)
from .agent_mux_contract_models import (
    CONTRACT_SET_ID_HEX_CHARS as CONTRACT_SET_ID_HEX_CHARS,
)
from .agent_mux_contract_models import (
    CONTRACT_SET_ID_PATTERN as CONTRACT_SET_ID_PATTERN,
)
from .agent_mux_contract_models import (
    CONTRACT_SET_PREFIX as CONTRACT_SET_PREFIX,
)
from .agent_mux_contract_models import (
    CONTRACT_SET_STATUS_CURRENT as CONTRACT_SET_STATUS_CURRENT,
)
from .agent_mux_contract_models import (
    CONTRACT_SET_STATUS_RETIRED as CONTRACT_SET_STATUS_RETIRED,
)
from .agent_mux_contract_models import (
    CONTRACT_SET_STATUSES as CONTRACT_SET_STATUSES,
)
from .agent_mux_contract_models import (
    CORPUS_RELATIVE_PATH as CORPUS_RELATIVE_PATH,
)
from .agent_mux_contract_models import (
    FIXTURE_DIRNAME as FIXTURE_DIRNAME,
)
from .agent_mux_contract_models import (
    FIXTURE_DIRNAMES as FIXTURE_DIRNAMES,
)
from .agent_mux_contract_models import (
    INVALID_DIRNAME as INVALID_DIRNAME,
)
from .agent_mux_contract_models import (
    MANIFEST_FILENAME as MANIFEST_FILENAME,
)
from .agent_mux_contract_models import (
    MANIFEST_VERSION as MANIFEST_VERSION,
)
from .agent_mux_contract_models import (
    REASON_ARRAY_TOO_LONG as REASON_ARRAY_TOO_LONG,
)
from .agent_mux_contract_models import (
    REASON_ARRAY_TOO_SHORT as REASON_ARRAY_TOO_SHORT,
)
from .agent_mux_contract_models import (
    REASON_BODY_NOT_ALLOWED as REASON_BODY_NOT_ALLOWED,
)
from .agent_mux_contract_models import (
    REASON_CONST_MISMATCH as REASON_CONST_MISMATCH,
)
from .agent_mux_contract_models import (
    REASON_DIGEST_MISMATCH as REASON_DIGEST_MISMATCH,
)
from .agent_mux_contract_models import (
    REASON_DOCUMENT_TOO_LARGE as REASON_DOCUMENT_TOO_LARGE,
)
from .agent_mux_contract_models import (
    REASON_DUPLICATE_ITEM as REASON_DUPLICATE_ITEM,
)
from .agent_mux_contract_models import (
    REASON_INVALID_ENUM as REASON_INVALID_ENUM,
)
from .agent_mux_contract_models import (
    REASON_INVALID_FORMAT as REASON_INVALID_FORMAT,
)
from .agent_mux_contract_models import (
    REASON_MISSING_FIELD as REASON_MISSING_FIELD,
)
from .agent_mux_contract_models import (
    REASON_NULL_NOT_ALLOWED as REASON_NULL_NOT_ALLOWED,
)
from .agent_mux_contract_models import (
    REASON_NUMBER_OUT_OF_RANGE as REASON_NUMBER_OUT_OF_RANGE,
)
from .agent_mux_contract_models import (
    REASON_PATTERN_MISMATCH as REASON_PATTERN_MISMATCH,
)
from .agent_mux_contract_models import (
    REASON_SCHEMA_INVALID as REASON_SCHEMA_INVALID,
)
from .agent_mux_contract_models import (
    REASON_STRING_TOO_LONG as REASON_STRING_TOO_LONG,
)
from .agent_mux_contract_models import (
    REASON_STRING_TOO_SHORT as REASON_STRING_TOO_SHORT,
)
from .agent_mux_contract_models import (
    REASON_TYPE_MISMATCH as REASON_TYPE_MISMATCH,
)
from .agent_mux_contract_models import (
    REASON_UNKNOWN_FIELD as REASON_UNKNOWN_FIELD,
)
from .agent_mux_contract_models import (
    REASON_UNKNOWN_ROOT as REASON_UNKNOWN_ROOT,
)
from .agent_mux_contract_models import (
    REPO_ROOT as REPO_ROOT,
)
from .agent_mux_contract_models import (
    SCHEMA_DIRNAME as SCHEMA_DIRNAME,
)
from .agent_mux_contract_models import (
    SCHEMA_FILENAMES as SCHEMA_FILENAMES,
)
from .agent_mux_contract_models import (
    VALID_DIRNAME as VALID_DIRNAME,
)
from .agent_mux_contract_models import (
    VIOLATION_REASONS as VIOLATION_REASONS,
)
from .agent_mux_contract_models import (
    ContractError as ContractError,
)
from .agent_mux_contract_models import (
    Violation as Violation,
)
from .agent_mux_contract_models import (
    _pointer as _pointer,
)
from .agent_mux_contract_models import (
    canonical_json as canonical_json,
)
from .agent_mux_contract_models import (
    sha256_hex as sha256_hex,
)
from .agent_mux_contract_schema import (
    _body_bearing_declarations as _body_bearing_declarations,
)
from .agent_mux_contract_schema import (
    _collect_reachable as _collect_reachable,
)
from .agent_mux_contract_schema import (
    _lint_node as _lint_node,
)
from .agent_mux_contract_schema import (
    _lint_object_node as _lint_object_node,
)
from .agent_mux_contract_schema import (
    _node_types as _node_types,
)
from .agent_mux_contract_schema import (
    _referenced_definitions as _referenced_definitions,
)
from .agent_mux_contract_schema import (
    lint_schema as lint_schema,
)
from .agent_mux_contract_validation import (
    _json_type as _json_type,
)
from .agent_mux_contract_validation import (
    _privacy_violations as _privacy_violations,
)
from .agent_mux_contract_validation import (
    _validate_array as _validate_array,
)
from .agent_mux_contract_validation import (
    _validate_node as _validate_node,
)
from .agent_mux_contract_validation import (
    _validate_number as _validate_number,
)
from .agent_mux_contract_validation import (
    _validate_object as _validate_object,
)
from .agent_mux_contract_validation import (
    _validate_string as _validate_string,
)
from .agent_mux_contract_validation import (
    validate_document as validate_document,
)

if __name__ == "__main__":
    raise SystemExit(main())
