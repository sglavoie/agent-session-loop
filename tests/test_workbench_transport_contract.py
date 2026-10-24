"""Focused transport-contract tests."""

from .workbench_transport_contract_cases_boundary import (
    TheTransportIsSeparateFromTheAgentMuxContract,
    TheTransportStaysInsideThePortablePackage,
)
from .workbench_transport_contract_cases_capabilities import (
    IncompatibleDeploymentsCannotMutate,
    ProseNeverTravelsExceptWhereItIsDeclaredTo,
    VersionsAndCapabilitiesAreDifferentFailures,
)
from .workbench_transport_contract_cases_roles import (
    RolesSeparateWhatEachEndMayDo,
    TheCredentialFileIsTheCompanionsFile,
)
from .workbench_transport_contract_cases_roundtrip import (
    InvalidMessagesFailPredictably,
    ObservationsReachTheStoreUntranslated,
    ValidExchangesRoundTrip,
)

ASL_TEST_PORTABLE = True

__all__ = [
    "ValidExchangesRoundTrip",
    "ObservationsReachTheStoreUntranslated",
    "InvalidMessagesFailPredictably",
    "ProseNeverTravelsExceptWhereItIsDeclaredTo",
    "VersionsAndCapabilitiesAreDifferentFailures",
    "IncompatibleDeploymentsCannotMutate",
    "RolesSeparateWhatEachEndMayDo",
    "TheCredentialFileIsTheCompanionsFile",
    "TheTransportStaysInsideThePortablePackage",
    "TheTransportIsSeparateFromTheAgentMuxContract",
]
