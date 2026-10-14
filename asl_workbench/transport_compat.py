"""Hello evaluation, protocol negotiation and mutation authorization.

Part of :mod:`asl_workbench.transport`, which re-exports everything here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .observation import (
    SUPPORTED_CONTRACT_SET_IDS,
    ObservationPolicy,
    require_policy,
)
from .transport_checks import (
    _fail,
    _flag,
    _identifier,
    _mapping,
    _optional,
    _sequence,
    check_shape,
)
from .transport_shapes import (
    HELLO_SHAPE,
)
from .transport_vocabulary import (
    CAPABILITY_MUTATE,
    ERROR_INCOMPATIBLE_CONTRACT,
    ERROR_INCOMPATIBLE_PROTOCOL,
    ERROR_INVALID_FIELD,
    ERROR_OBSERVATION_ONLY,
    ERROR_UNSUPPORTED_CAPABILITY,
    ERROR_WRONG_TARGET,
    MAX_CAPABILITIES,
    MAX_PROTOCOLS,
    MUTATING_CAPABILITIES,
    REQUIRED_CAPABILITIES,
    SUPPORTED_TRANSPORT_PROTOCOLS,
    TRANSPORT_CAPABILITIES,
)

# Compatibility
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportCompatibility:
    """What two deployments may do with each other, and why.

    ``compatible`` is the summary; the two permissions are what the code acts
    on. They come apart on purpose: a peer on an understood transport version
    whose contract set this build has never seen still produces observations
    worth recording, so ``reads_allowed`` stays true while
    ``mutations_allowed`` does not. ``mutation_refusal`` carries the single
    code a receipt should cite, chosen by a fixed precedence so two ends of the
    same mismatch report it identically.
    """

    compatible: bool
    reads_allowed: bool
    mutations_allowed: bool
    protocol: int | None
    connector_id: str | None
    contract_set_id: str | None
    asl_version: str | None
    granted: frozenset[str] = frozenset()
    unknown: frozenset[str] = frozenset()
    missing: frozenset[str] = frozenset()
    reasons: tuple[str, ...] = ()
    mutation_refusal: str | None = None
    notes: tuple[str, ...] = ()

    def permits(self, capability: str) -> bool:
        """Whether an envelope claiming ``capability`` may be acted on."""
        if not self.reads_allowed or capability not in self.granted:
            return False
        if capability in MUTATING_CAPABILITIES:
            return self.mutations_allowed
        return True


#: The order a mutation refusal is reported in. An incompatible version
#: explains everything downstream of it, so it is named first; "observation
#: only" is last because it is a deliberate setting rather than a mismatch, and
#: reporting it over a real incompatibility would hide the incompatibility.
_MUTATION_REFUSAL_PRECEDENCE: tuple[str, ...] = (
    ERROR_INCOMPATIBLE_PROTOCOL,
    ERROR_WRONG_TARGET,
    ERROR_INCOMPATIBLE_CONTRACT,
    ERROR_UNSUPPORTED_CAPABILITY,
    ERROR_OBSERVATION_ONLY,
)


def negotiate_protocol(offered: Iterable[Any]) -> int | None:
    """The highest transport version both ends can speak, or ``None``."""
    shared = {
        version
        for version in offered
        if isinstance(version, int)
        and not isinstance(version, bool)
        and version in SUPPORTED_TRANSPORT_PROTOCOLS
    }
    return max(shared) if shared else None


def validate_hello(message: Any) -> dict[str, Any]:
    """Check one announcement. Closed, like every other document here."""
    hello = _mapping(message, "hello")
    check_shape(hello, HELLO_SHAPE, "hello")
    protocol = hello["protocol"]
    if isinstance(protocol, bool) or not isinstance(protocol, int):
        raise _fail(ERROR_INVALID_FIELD, "hello.protocol", "expected an integer")
    _identifier(hello["connector_id"], "hello.connector_id")
    _identifier(hello["target_id"], "hello.target_id")
    capabilities = _sequence(
        hello["capabilities"], "hello.capabilities", limit=MAX_CAPABILITIES
    )
    for index, capability in enumerate(capabilities):
        _identifier(capability, f"hello.capabilities[{index}]")
    protocols = hello.get("protocols")
    if protocols is not None:
        for index, version in enumerate(
            _sequence(protocols, "hello.protocols", limit=MAX_PROTOCOLS)
        ):
            if isinstance(version, bool) or not isinstance(version, int):
                raise _fail(
                    ERROR_INVALID_FIELD,
                    f"hello.protocols[{index}]",
                    "expected an integer",
                )
    _optional(hello, "contract_set_id", "hello", _identifier)
    _optional(hello, "asl_version", "hello", _identifier)
    _optional(hello, "observation_only", "hello", _flag)
    return dict(hello)


def evaluate_hello(
    message: Any,
    *,
    policy: ObservationPolicy,
    target_id: str | None = None,
    supported_contract_set_ids: Sequence[str] | None = None,
) -> TransportCompatibility:
    """Decide what a peer may do, keeping versions and capabilities apart.

    An unusable *version* is fatal: without a shared transport number neither
    end can read the other's frames, so nothing is permitted and the verdict
    says so once. An unusable *capability* -- one this build does not know, one
    the peer never offered, or a contract set outside
    ``supported_contract_set_ids`` -- narrows what is permitted and leaves the
    rest working.
    """
    hello = validate_hello(message)
    policy = require_policy(policy)
    supported = tuple(
        supported_contract_set_ids
        if supported_contract_set_ids is not None
        else SUPPORTED_CONTRACT_SET_IDS
    )

    offered_protocols = hello.get("protocols") or [hello["protocol"]]
    protocol = negotiate_protocol(offered_protocols)
    offered = frozenset(hello["capabilities"])
    granted = offered & frozenset(TRANSPORT_CAPABILITIES)
    unknown = offered - frozenset(TRANSPORT_CAPABILITIES)
    missing = REQUIRED_CAPABILITIES - offered
    contract_set_id = hello.get("contract_set_id")

    reasons: list[str] = []
    notes: list[str] = []

    if protocol is None:
        reasons.append(ERROR_INCOMPATIBLE_PROTOCOL)
    if target_id is not None and hello["target_id"] != target_id:
        reasons.append(ERROR_WRONG_TARGET)
    if contract_set_id not in supported:
        reasons.append(ERROR_INCOMPATIBLE_CONTRACT)
    # What the peer cannot do, as opposed to what this deployment declines to
    # ask for. A connector that never offered ``mutate`` is the first; a
    # deployment set to observe only is the second, and they are reported
    # separately because only the second is somebody's decision to reverse.
    if missing or CAPABILITY_MUTATE not in granted:
        reasons.append(ERROR_UNSUPPORTED_CAPABILITY)
    if not policy.replies_permitted or bool(hello.get("observation_only")):
        reasons.append(ERROR_OBSERVATION_ONLY)
    if unknown:
        # Named, never fatal: a newer connector offering something this build
        # has not learned yet is still a connector whose observations count.
        notes.append(f"unknown capabilities: {', '.join(sorted(unknown))}")

    fatal = protocol is None or ERROR_WRONG_TARGET in reasons
    reads_allowed = not fatal
    compatible = reads_allowed and not missing and contract_set_id in supported
    mutation_refusal = next(
        (code for code in _MUTATION_REFUSAL_PRECEDENCE if code in reasons), None
    )
    mutations_allowed = compatible and mutation_refusal is None

    return TransportCompatibility(
        compatible=compatible,
        reads_allowed=reads_allowed,
        mutations_allowed=mutations_allowed,
        protocol=protocol,
        connector_id=hello["connector_id"],
        contract_set_id=contract_set_id,
        asl_version=hello.get("asl_version"),
        granted=frozenset(granted),
        unknown=frozenset(unknown),
        missing=frozenset(missing),
        reasons=tuple(reasons),
        mutation_refusal=mutation_refusal,
        notes=tuple(notes),
    )


def authorize(
    document: Mapping[str, Any], compatibility: TransportCompatibility
) -> Mapping[str, Any]:
    """Refuse an envelope the negotiated compatibility does not permit.

    This is where "an incompatible deployment cannot execute mutations" stops
    being a property somebody remembers and becomes a refusal on the path a
    mutation takes. A validated envelope is not yet an authorized one: shape
    says the frame is readable, and this says the pairing may act on it.
    """
    capability = document.get("capability")
    if not isinstance(capability, str) or capability not in TRANSPORT_CAPABILITIES:
        raise _fail(ERROR_INVALID_FIELD, "envelope.capability", str(capability))

    if not compatibility.reads_allowed:
        code = (
            ERROR_WRONG_TARGET
            if ERROR_WRONG_TARGET in compatibility.reasons
            else ERROR_INCOMPATIBLE_PROTOCOL
        )
        raise _fail(code, "envelope", "the deployments are not compatible")

    connector_id = document.get("connector_id")
    if (
        compatibility.connector_id is not None
        and connector_id != compatibility.connector_id
    ):
        raise _fail(ERROR_WRONG_TARGET, "envelope.connector_id", str(connector_id))

    if capability not in compatibility.granted:
        raise _fail(ERROR_UNSUPPORTED_CAPABILITY, "envelope.capability", capability)

    if capability in MUTATING_CAPABILITIES and not compatibility.mutations_allowed:
        raise _fail(
            compatibility.mutation_refusal or ERROR_UNSUPPORTED_CAPABILITY,
            "envelope.capability",
            capability,
        )

    return document


__all__ = [
    "TransportCompatibility",
    "authorize",
    "evaluate_hello",
    "negotiate_protocol",
    "validate_hello",
]
