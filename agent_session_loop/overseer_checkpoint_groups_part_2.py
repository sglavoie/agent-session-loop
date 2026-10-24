# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import overseer_checkpoint_groups_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .overseer_checkpoint_groups_shared import *
from . import overseer_checkpoint_groups_part_1 as _part_1

globals().update(
    {name: value for name, value in vars(_part_1).items() if not name.startswith("__")}
)

from .overseer_checkpoint_groups_adapter_prepare import _GroupAdapterPreparation
from .overseer_checkpoint_groups_adapter_operations import _GroupAdapterOperations


@dataclass(frozen=True)
class GroupAdapter(_GroupAdapterPreparation, _GroupAdapterOperations):
    """The ``commit`` operation over a registry, behind the dispatcher's guards.

    ``authority`` and ``authorize`` are separate seams because they answer
    separate questions: the ruling says an operator decided this checkpoint
    should happen, and the policy client says AgentMux still permits writing to
    these repositories. Both are asked again before every commit rather than
    once for the group, so a revocation that arrives mid-group stops the next
    repository instead of being noticed after the last one.

    No preparation is memoised in the process. The durable group in the ledger
    *is* the handoff from :meth:`prepare` to :meth:`execute`, which is what lets
    the two run in different processes with a crash in between.
    """

    authority: Authority
    ledger: overseer_store_ledger.Ledger
    state_dir: Path | None = None
    repositories: Repositories = overseer_checkpoint.declared_repositories
    authorize: Authorize = field(default=overseer_policy_client.authorize, repr=False)
    timeout: float = 5.0
