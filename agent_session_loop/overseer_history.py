# ruff: noqa: E402, F401, F403, F405, I001
"""Compatibility facade for the overseer history implementation."""

from __future__ import annotations

from .overseer_history_part_1 import (
    __all__,
    HISTORY_CONTRACT_NAME,
    HISTORY_CONTRACT_VERSION,
    HISTORY_SCHEMA_VERSION,
    DEFAULT_CONVERSATIONS,
    DEFAULT_MESSAGES,
    DEFAULT_EXCERPT_CHARS,
    MAX_CONVERSATIONS,
    MAX_MESSAGES,
    MAX_EXCERPT_CHARS,
    WITHHELD_FORBIDDEN_FIELDS,
    DEFAULT_TIMEOUT_SECONDS,
    Failure,
    FAILURE_ANSWER_MISMATCH,
    FAILURE_REASONS,
    Limits,
    Excerpt,
    Conversation,
    Withheld,
    History,
    history_argv,
    read_history,
)
from .overseer_history_part_2 import (
    _unsendable,
    _looks_like_timestamp,
    _read_answer,
    _mismatch,
    _read_limits,
    _read_conversations,
)
from .overseer_history_part_3 import (
    _read_excerpts,
    _read_withheld,
    _read_outcomes,
    _read_strings,
    _failed,
    _exit_status,
    _count,
    _version,
    _text,
)

from . import overseer_history_shared as _shared
from . import overseer_history_part_1 as _overseer_history_part_1
from . import overseer_history_part_2 as _overseer_history_part_2
from . import overseer_history_part_3 as _overseer_history_part_3

_PARTS = (
    _overseer_history_part_1,
    _overseer_history_part_2,
    _overseer_history_part_3,
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
