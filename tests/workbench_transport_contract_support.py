"""The Workbench's own transport: five envelopes, bounded, and separately versioned.

Session 10 adds a wire of the Workbench's own between the Mac connector and the
Pi service. Everything here holds one of the three properties that wire is for.

**Valid exchanges round-trip.** The fixture corpus under
``tests/fixtures/workbench_transport/valid`` is composed through the builders,
encoded, decoded and validated again, and the result has to equal the fixture
byte for byte in content. The round trip does not stop at the envelope: an
observation that validates is handed straight to :class:`WorkbenchStore` and
ingested with no translation step, because the payload bodies *are* what the
store already reads. A transport that needed a shim between "valid" and
"storable" would have two schemas and one name for them.

**Invalid or oversized messages fail predictably.** Every frame under
``invalid/`` names the single error code it must raise, and the code is
asserted rather than the mere fact of failure -- a refusal that reports the
wrong reason is how an oversized frame gets diagnosed as a version mismatch and
a perfectly good connector gets torn out. Size is measured before the bytes are
parsed, so a frame too large to accept is never a frame this process decodes.

**Incompatible deployments cannot execute mutations.** This is the distinction
the session exists to draw, and it is drawn twice: an unusable protocol
*version* is fatal in both directions, while an unsupported individual
*capability* -- or a contract set this build has never seen -- narrows what is
permitted and leaves reads working. :func:`transport.authorize` is where that
verdict stops being a dataclass and becomes a refusal on the path a mutation
takes.

Two boundaries are held alongside them. The transport schemas are separate from
AgentMux's contract: the corpus lives outside the digested tree, and the
contract set id travels through these envelopes as *data* rather than as this
transport's own version number. And the credential roles are the Companion's,
ported rather than reinvented, with each role now bound to the envelope kinds
and capabilities it may exercise.
"""

from __future__ import annotations

import ast
import json
import os
import stat
import sys
import unittest
from hashlib import sha256
from hmac import new as hmac_new
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_session_loop import agent_mux_contract
from asl_workbench import (
    credentials,
    observation,
    store_types,
    store_workbench,
    transport,
)
from asl_workbench.operations import FORBIDDEN_PERSISTED_KEYS, OPERATION_PERSIST_FIELDS

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "asl_workbench"
CORPUS = REPO_ROOT / "tests" / "fixtures" / "workbench_transport"

NOW = 1770000000.0
CONNECTOR_ID = "mac-studio"
SERVICE_ID = "pi-companion"
PLAN_KEY = "0123456789abcdef"
CONTRACT_SET_ID = observation.SUPPORTED_CONTRACT_SET_IDS[0]

POLICY = observation.ObservationPolicy()

#: The four codes the corpus cannot carry, and where each is proved instead.
#: ``frame-too-large`` needs a frame no fixture file should contain; the other
#: three are decided by :func:`transport.evaluate_hello` and
#: :func:`credentials.authorize_upload` about a *pairing*, not about a frame.
CODES_PROVED_OUTSIDE_THE_CORPUS = frozenset(
    {
        transport.ERROR_FRAME_TOO_LARGE,
        transport.ERROR_INCOMPATIBLE_CONTRACT,
        transport.ERROR_OBSERVATION_ONLY,
        transport.ERROR_UNAUTHORIZED,
    }
)


# --------------------------------------------------------------------------
# Corpus helpers
# --------------------------------------------------------------------------


def canonical(document: Any) -> str:
    """The one rendering every fixture in this corpus is written in."""
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def load_corpus(kind: str) -> list[tuple[str, dict[str, Any]]]:
    paths = sorted((CORPUS / kind).glob("*.json"))
    return [(path.stem, json.loads(path.read_text(encoding="utf-8"))) for path in paths]


def fixture(name: str) -> dict[str, Any]:
    """One valid envelope, freshly decoded so a test may mutate it."""
    path = CORPUS / "valid" / f"{name}.json"
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    envelope: dict[str, Any] = document["envelope"]
    return envelope


def rebuild(envelope: dict[str, Any]) -> dict[str, Any]:
    """Compose the same envelope through the builder for its kind."""
    kind = envelope["kind"]
    common: dict[str, Any] = {
        "payload": envelope["payload"],
        "connector_id": envelope["connector_id"],
        "target_id": envelope["target_id"],
        "sent_at": envelope["sent_at"],
    }
    if kind == transport.KIND_REQUEST:
        return transport.request_envelope(
            **common,
            request_id=envelope["request_id"],
            deadline=envelope["deadline"],
        )
    if kind == transport.KIND_OBSERVATION:
        return transport.observation_envelope(**common)
    if kind == transport.KIND_DETAIL:
        return transport.detail_envelope(**common, request_id=envelope["request_id"])
    if kind == transport.KIND_OPERATION:
        return transport.operation_envelope(
            **common, request_id=envelope.get("request_id")
        )
    return transport.receipt_envelope(
        **common,
        capability=envelope["capability"],
        request_id=envelope["request_id"],
    )


def hello(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "protocol": transport.TRANSPORT_PROTOCOL_VERSION,
        "connector_id": CONNECTOR_ID,
        "target_id": SERVICE_ID,
        "capabilities": list(transport.TRANSPORT_CAPABILITIES),
        "contract_set_id": CONTRACT_SET_ID,
    }
    document.update(overrides)
    return document


def detail_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "detail": "final-review",
        "target": {"plan_key": PLAN_KEY},
        "observed_at": NOW,
        "complete": "partial",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _literal(node: ast.expr) -> Any:
    """Fold the two non-literal spellings a constant here is written in."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and node.args
    ):
        return frozenset(ast.literal_eval(node.args[0]))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        # ``1024 * 1024`` is a size written the way a reader wants to see it.
        return _literal(node.left) * _literal(node.right)
    return ast.literal_eval(node)


def _source_constant(tree: ast.Module, name: str) -> Any:
    """Read one constant out of a parsed module, without importing it.

    The Pi is a read-only dependency here and importing it would leave bytecode
    behind in somebody else's repository, so the cross-check is a parse.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if name not in names or node.value is None:
            continue
        return _literal(node.value)
    raise AssertionError(f"{name} is not a literal constant in the checkout")


__all__ = [
    "ast",
    "json",
    "os",
    "stat",
    "sys",
    "unittest",
    "sha256",
    "hmac_new",
    "Path",
    "TemporaryDirectory",
    "Any",
    "agent_mux_contract",
    "credentials",
    "observation",
    "store_types",
    "store_workbench",
    "transport",
    "FORBIDDEN_PERSISTED_KEYS",
    "OPERATION_PERSIST_FIELDS",
    "REPO_ROOT",
    "PACKAGE",
    "CORPUS",
    "NOW",
    "CONNECTOR_ID",
    "SERVICE_ID",
    "PLAN_KEY",
    "CONTRACT_SET_ID",
    "POLICY",
    "CODES_PROVED_OUTSIDE_THE_CORPUS",
    "canonical",
    "load_corpus",
    "fixture",
    "rebuild",
    "hello",
    "detail_payload",
    "_imported_roots",
    "_literal",
    "_source_constant",
]
