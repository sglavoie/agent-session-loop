"""Focused tests for the overseer's read isolation and contention primitives.

Two properties are proved here, and both are proved against the filesystem
rather than against a return value.

*Isolation.* Checking a plan the overseer may act on must leave every other
plan in the state root byte-identical, including the excluded plan whose lock
the ordinary contention scan would have reclaimed. Every observation test wraps
itself in :class:`_CorpusSnapshot`, which records content and mtime for every
file under the state root before and after and fails on any difference.

*The existing contract.* The same tests assert the ordinary path still
reclaims, and that the observation returns exactly the conflicts the ordinary
refusal returns over the same corpus -- so the isolation is a narrower write,
never a different answer.
"""

from .asl_overseer_isolation_cases_launch import (
    ExtractionContractTests,
    LaunchPropagationTests,
)
from .asl_overseer_isolation_cases_observation import (
    ObservationIsolationTests,
    UnresolvedOwnershipTests,
)
from .asl_overseer_isolation_cases_recovery import RecoveryAuthorizationTests

__all__ = [name for name in globals() if name.endswith("Tests")]
