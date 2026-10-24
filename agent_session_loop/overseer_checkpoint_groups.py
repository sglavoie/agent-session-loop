# ruff: noqa: F401, I001
"""Compatibility facade for the overseer checkpoint groups implementation."""

from __future__ import annotations

from .overseer_checkpoint_groups_part_1 import (
    __all__,
    Operation,
    GROUP_OPERATIONS,
    GROUP_SCOPE,
    GROUP_PHASES,
    Refusal,
    REFUSALS,
    _ID_NAMESPACE,
    Authority,
    Repositories,
    Authorize,
    group_key,
    repository_identity,
    Permission,
    summarize,
)
from .overseer_checkpoint_groups_part_2 import (
    GroupAdapter,
)
from .overseer_checkpoint_groups_part_3 import (
    adapters,
    _phrase,
    _decline,
    _failure,
    _drift,
    _decision_identity,
    _decision_id,
    _refusal_name,
    _rows,
    _target_rows,
    _group_roots,
    _verification_state,
    _verbatim_message,
    _text,
)

from . import overseer_checkpoint_groups_shared as _shared
from . import overseer_checkpoint_groups_part_1 as _overseer_checkpoint_groups_part_1
from . import overseer_checkpoint_groups_adapter_prepare as _adapter_prepare
from . import overseer_checkpoint_groups_adapter_operations as _adapter_operations
from . import overseer_checkpoint_groups_part_2 as _overseer_checkpoint_groups_part_2
from . import overseer_checkpoint_groups_part_3 as _overseer_checkpoint_groups_part_3

_PARTS = (
    _overseer_checkpoint_groups_part_1,
    _adapter_prepare,
    _adapter_operations,
    _overseer_checkpoint_groups_part_2,
    _overseer_checkpoint_groups_part_3,
)
for _part in _PARTS:
    globals().update(
        {
            name: value
            for name, value in vars(_part).items()
            if not name.startswith("__")
        }
    )
for _part in _PARTS:
    _part.__dict__.update(
        {name: value for name, value in globals().items() if not name.startswith("__")}
    )
del _part
