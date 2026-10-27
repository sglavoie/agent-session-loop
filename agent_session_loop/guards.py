# mypy: ignore-errors
# ruff: noqa
"""Public facade for run-scoped command and protected-path guards."""

from . import guards_controller as _controller
from . import guards_models as _models
from . import guards_policy as _policy
from . import guards_rules as _rules
from .guards_controller import GuardController, legacy_workspace_aliases
from .guards_models import *  # noqa: F401, F403
from .guards_policy import *  # noqa: F401, F403
from .guards_rules import *  # noqa: F401, F403

for _module in (_models, _rules, _policy, _controller):
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
    )
