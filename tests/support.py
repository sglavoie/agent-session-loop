"""Compatibility facade for the focused shared-test-fixture modules."""

from . import (
    support_base as _support_base,
)
from . import (
    support_git as _support_git,
)
from . import (
    support_plans as _support_plans,
)
from . import (
    support_prepare as _support_prepare,
)
from . import (
    support_processes as _support_processes,
)
from . import (
    support_review as _support_review,
)
from . import (
    support_workbench as _support_workbench,
)

_COMPONENTS = (
    _support_base,
    _support_plans,
    _support_git,
    _support_processes,
    _support_review,
    _support_workbench,
    _support_prepare,
)
for _component in _COMPONENTS:
    globals().update(
        {
            name: value
            for name, value in vars(_component).items()
            if not name.startswith("__")
        }
    )

__all__ = tuple(name for name in globals() if not name.startswith("__"))
