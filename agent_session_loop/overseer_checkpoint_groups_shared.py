# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: F401
"""The ``commit`` operation across a plan's whole repository registry.

:mod:`agent_session_loop.overseer_checkpoint` checkpoints one repository, and
refuses outright when a plan declares more than one. This module is the
multi-repository form of the same operation: it authorizes every participating
repository before it stages anything, records what it established in a durable
group in the overseer ledger, and then commits the repositories one at a time,
rewriting that group at every boundary.

The two properties everything here is arranged around are that a partial group
is *reported* rather than repaired, and that a retry *reconciles* rather than
repeats. A commit that landed is never rolled back and never made twice: the
per-repository :data:`~agent_session_loop.overseer_checkpoint.CHECKPOINT_TRAILER`
identity is derived from the action, so a process that crashed between the
commit and the record it owed can be told what it did by the process that
follows it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import (
    commit_groups,
    commit_message,
    overseer_checkpoint,
    overseer_decisions,
    overseer_dispatch,
    overseer_policy_client,
    overseer_store_ledger,
    overseer_store_models,
)
