"""Agent mux contract manifest."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import agent_mux_contract_models as models

# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


def corpus_root(root: str | Path | None = None) -> Path:
    """Return the canonical corpus directory."""
    if root is None:
        return models.REPO_ROOT / models.CORPUS_RELATIVE_PATH
    return Path(root)


def _read_json(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise models.ContractError(f"cannot read {path}: {error}") from error
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise models.ContractError(f"{path} is not valid JSON: {error}") from error


def load_schema(component: str, *, root: str | Path | None = None) -> dict[str, Any]:
    """Load one component's canonical schema."""
    if component not in models.COMPONENTS:
        raise models.ContractError(f"unknown component `{component}`")
    path = (
        corpus_root(root) / models.SCHEMA_DIRNAME / models.SCHEMA_FILENAMES[component]
    )
    schema = _read_json(path)
    if not isinstance(schema, dict):
        raise models.ContractError(f"{path} must contain a JSON object")
    return schema


def corpus_files(root: str | Path | None = None) -> list[str]:
    """Return every canonical corpus file as a sorted repo-relative POSIX path.

    ``manifest.json`` is excluded: it carries the digest of everything else, so
    including it would make the identity depend on itself.
    """
    base = corpus_root(root)
    if not base.is_dir():
        raise models.ContractError(f"contract corpus directory is missing: {base}")
    return sorted(
        path.relative_to(base).as_posix()
        for path in base.rglob("*.json")
        if path.is_file() and path.name != models.MANIFEST_FILENAME
    )


def file_digests(root: str | Path | None = None) -> dict[str, str]:
    """Return ``{relative path: sha256}`` over every canonical corpus file."""
    base = corpus_root(root)
    digests: dict[str, str] = {}
    for name in corpus_files(base):
        try:
            digests[name] = models.sha256_hex((base / name).read_bytes())
        except OSError as error:
            raise models.ContractError(f"cannot read {base / name}: {error}") from error
    return digests


def corpus_digest(root: str | Path | None = None) -> str:
    """Digest the corpus bytes in stable path order.

    The pre-image is ``"<relative path>\\n<sha256 hex>\\n"`` per file in
    ascending path order, which is the same construction the protocol-1 bridge
    manifest uses. A renamed file moves the digest even when its bytes do not,
    because the path is part of the contract.
    """
    joined = "".join(
        f"{name}\n{digest}\n" for name, digest in sorted(file_digests(root).items())
    )
    return models.sha256_hex(joined.encode("utf-8"))


def contract_set_id(root: str | Path | None = None) -> str:
    """Return the contract-set ID derived from the corpus bytes."""
    digest = corpus_digest(root)[: models.CONTRACT_SET_ID_HEX_CHARS]
    return f"{models.CONTRACT_SET_PREFIX}.{digest}"


# ---------------------------------------------------------------------------
# Compatibility manifest
# ---------------------------------------------------------------------------


def _capabilities(root: str | Path | None = None) -> dict[str, Any]:
    """Read the supported capabilities out of the bridge schema itself.

    The manifest never restates a vocabulary: it points at the one the schema
    already closed, so the two cannot disagree.
    """
    schema = load_schema(models.COMPONENT_BRIDGE_PROTOCOL, root=root)
    definitions = schema["definitions"]
    hook = load_schema(models.COMPONENT_LIFECYCLE_HOOK, root=root)
    return {
        "bridge_methods": list(definitions["Method"]["enum"]),
        "bridge_mutation_methods": list(definitions["MutationMethod"]["enum"]),
        "bridge_events": list(definitions["EventName"]["enum"]),
        "bridge_error_codes": list(definitions["ErrorCode"]["enum"]),
        "lifecycle_events": list(hook["definitions"]["EventName"]["enum"]),
        "lifecycle_phases": list(hook["definitions"]["Phase"]["enum"]),
        "projection_phases": list(
            load_schema(models.COMPONENT_PROJECTION, root=root)["definitions"]["Phase"][
                "enum"
            ]
        ),
    }


def build_manifest(root: str | Path | None = None) -> dict[str, Any]:
    """Build the compatibility manifest from the corpus bytes.

    Everything here is derived. Nothing is transcribed, so the manifest cannot
    describe a corpus other than the one beside it.
    """
    base = corpus_root(root)
    digests = file_digests(base)
    identity = contract_set_id(base)
    components: dict[str, Any] = {}
    for component in models.COMPONENTS:
        relative = f"{models.SCHEMA_DIRNAME}/{models.SCHEMA_FILENAMES[component]}"
        schema = load_schema(component, root=base)
        fixture_prefix = (
            f"{models.FIXTURE_DIRNAME}/{models.FIXTURE_DIRNAMES[component]}/"
        )
        components[component] = {
            "schema_path": relative,
            "schema_version": schema["version"],
            "digest": digests[relative],
            "roots": sorted(schema["roots"]),
            "max_document_bytes": schema["max_document_bytes"],
            "fixture_count": sum(
                1 for name in digests if name.startswith(fixture_prefix)
            ),
        }
    return {
        "manifest_version": models.MANIFEST_VERSION,
        "generated_by": "python3 -m agent_session_loop.agent_mux_contract --write",
        "current_contract_set_id": identity,
        "contract_sets": [
            {
                "contract_set_id": identity,
                "status": models.CONTRACT_SET_STATUS_CURRENT,
                "corpus_digest": corpus_digest(base),
                "components": components,
                "capabilities": _capabilities(base),
            }
        ],
        "files": digests,
    }


def read_manifest(root: str | Path | None = None) -> dict[str, Any]:
    """Load the checked-in compatibility manifest."""
    path = corpus_root(root) / models.MANIFEST_FILENAME
    manifest = _read_json(path)
    if not isinstance(manifest, dict):
        raise models.ContractError(f"{path} must contain a JSON object")
    return manifest


def current_contract_set(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return the manifest entry the current contract-set ID names."""
    identity = manifest.get("current_contract_set_id")
    for entry in manifest.get("contract_sets", []):
        if entry.get("contract_set_id") == identity:
            return dict(entry)
    raise models.ContractError(
        f"manifest names current contract set `{identity}` but does not define it"
    )


def manifest_violations(
    root: str | Path | None = None,
) -> list[models.Violation]:
    """Return every way the checked-in manifest disagrees with the corpus."""
    base = corpus_root(root)
    expected = build_manifest(base)
    try:
        actual = read_manifest(base)
    except models.ContractError as error:
        return [models.Violation(models.REASON_DIGEST_MISMATCH, "", str(error))]

    problems: list[models.Violation] = []
    expected_id = expected["current_contract_set_id"]
    actual_id = actual.get("current_contract_set_id")
    if actual_id != expected_id:
        problems.append(
            models.Violation(
                models.REASON_DIGEST_MISMATCH,
                "/current_contract_set_id",
                f"manifest claims `{actual_id}`; corpus derives `{expected_id}`",
            )
        )
    actual_files = actual.get("files")
    if actual_files != expected["files"]:
        expected_names = set(expected["files"])
        actual_names = set(actual_files or {})
        for name in sorted(expected_names - actual_names):
            problems.append(
                models.Violation(
                    models.REASON_DIGEST_MISMATCH,
                    models._pointer(["files", name]),
                    "corpus file is absent from the manifest",
                )
            )
        for name in sorted(actual_names - expected_names):
            problems.append(
                models.Violation(
                    models.REASON_DIGEST_MISMATCH,
                    models._pointer(["files", name]),
                    "manifest names a file the corpus does not carry",
                )
            )
        for name in sorted(expected_names & actual_names):
            if expected["files"][name] != (actual_files or {})[name]:
                problems.append(
                    models.Violation(
                        models.REASON_DIGEST_MISMATCH,
                        models._pointer(["files", name]),
                        "manifest digest does not match the file beside it",
                    )
                )
    if not problems and models.canonical_json(actual) != models.canonical_json(
        expected
    ):
        problems.append(
            models.Violation(
                models.REASON_DIGEST_MISMATCH,
                "",
                "manifest content differs from the corpus it describes",
            )
        )
    return problems


def write_manifest(root: str | Path | None = None) -> Path:
    """Rewrite ``manifest.json`` from the corpus; return the path written."""
    base = corpus_root(root)
    path = base / models.MANIFEST_FILENAME
    path.write_bytes(models.canonical_json(build_manifest(base)))
    return path
