# ruff: noqa: F401, F403, F405, I001
"""Compatibility facade for overseer verification operations."""

from __future__ import annotations

from .overseer_verify_adapter import VerifyAdapter, adapters
from .overseer_verify_context import (
    _authorized_participants,
    _context,
    _declared_participants,
    _decision_id,
    _key,
    _locator,
    _observed_participants,
    _observed_registry,
    _observed_run,
    _phrase,
    _receipt,
    _registry_bound,
    _registry_kept,
    _render,
    _resolved_bound,
    _resolved_participants,
    _same_decision,
)
from .overseer_verify_selection import select, session_under_verification
from .overseer_verify_shared import (
    ESCALATING_REFUSALS,
    REFUSALS,
    VERIFY_OPERATIONS,
    Prepared,
    Refusal,
    Selection,
)

VerifyAdapter.__module__ = __name__

__all__ = [
    "ESCALATING_REFUSALS",
    "REFUSALS",
    "VERIFY_OPERATIONS",
    "Prepared",
    "Refusal",
    "Selection",
    "VerifyAdapter",
    "adapters",
    "select",
    "session_under_verification",
]
