"""One-shot, portable process sampling for turn-hang diagnostics and sweeps.

A single headerless ``ps`` invocation (macOS and Linux compatible) is parsed
into a ppid->children map and walked breadth-first from the harness root pid.
Any failure — spawn error, timeout, nonzero exit, unparsable output, or an
absent root — yields ``None`` so callers stay fail-open.

:func:`prepare_verified_leaf_recovery` re-samples a suspected-live harness tree
and selects only identity-verified leaves for a one-shot SIGTERM recovery.
:func:`sweep_survivors` separately reuses sampling to SIGKILL processes that
outlived the harness turn or verification check they belonged to. Both paths
are best-effort cleanup, so nothing in this module raises: every failure
degrades to a report rather than destabilizing the run.

:func:`accumulate_cpu_seconds` folds successive samples into an identity-keyed
CPU high-water map, so turn-hang accounting survives tree churn: a descendant
that exits between two samples no longer takes the CPU it burned with it.

:func:`collect_descendant_evidence`, :func:`force_stop_process_group`,
:func:`terminate_escaped_descendants` and :func:`confirm_termination` are the
urgent counterparts, split so that *none* of the sampling is on the deadline
path: evidence is taken while the worker is still alive, the kill spawns
nothing at all, and every identity check runs after the group is already dead.

The public entry points remain here. Implementations live in
``process_sampling``, ``process_recovery``, ``process_sweep``, and
``process_termination``. Sampling patches belong on ``process_sampling``.
"""

import time

from .process_recovery import (
    _signal_verified_recovery,
    _verified_leaf_candidates,
    prepare_verified_leaf_recovery,
    prepare_verified_root_recovery,
    signal_verified_leaf_recovery,
    signal_verified_root_recovery,
)
from .process_sampling import (
    _BIRTH_PATTERN,
    _BIRTH_RE,
    _CPU_TIME_RE,
    _LAST_TREE_LOCK,
    _LSTART_RE,
    _SWEEP_DISABLED_VALUES,
    COMMAND_MAX_CHARS,
    POST_RESULT_RECOVERY_POLICY,
    POST_RESULT_RECOVERY_SIGNAL,
    POST_RESULT_ROOT_RECOVERY_POLICY,
    PS_BIRTH_COMMAND,
    PS_COMMAND,
    PS_TABLE_COMMAND,
    SAMPLE_TIMEOUT_SECONDS,
    SWEEP_DISABLED_ENV_VAR,
    SWEEP_SETTLE_SECONDS,
    CpuIdentity,
    _parse_cpu_time,
    _split_birth,
    accumulate_cpu_seconds,
    clear_process_tree,
    descendant_pids,
    group_member_pids,
    last_process_tree,
    os,
    process_birth,
    record_process_tree,
    sample_process_table,
    sample_process_tree,
    sweep_enabled,
)
from .process_sweep import (
    sweep_survivors,
)
from .process_termination import (
    GROUP_NOT_PROBED,
    GROUP_OWNERSHIP_UNKNOWN,
    GROUP_RUNNING,
    GROUP_STOPPED,
    INCOMPLETE_DESCENDANTS_RUNNING,
    INCOMPLETE_EVIDENCE_UNAVAILABLE,
    INCOMPLETE_GROUP_NOT_STOPPED,
    INCOMPLETE_OWNERSHIP_UNKNOWN,
    INCOMPLETE_SWEEP_OPT_OUT,
    REFUSE_INVALID_PGID,
    REFUSE_OWN_PROCESS_GROUP,
    URGENT_ESCAPEE_POLICY,
    URGENT_ESCAPEE_SIGNAL,
    URGENT_SAMPLE_TIMEOUT_SECONDS,
    GroupLiveness,
    IdentityVerdict,
    _probe_group,
    _verify_identity,
    collect_descendant_evidence,
    confirm_termination,
    force_stop_process_group,
    terminate_escaped_descendants,
)

__all__ = [
    "_signal_verified_recovery",
    "_verified_leaf_candidates",
    "prepare_verified_leaf_recovery",
    "prepare_verified_root_recovery",
    "signal_verified_leaf_recovery",
    "signal_verified_root_recovery",
    "sweep_survivors",
    "GROUP_NOT_PROBED",
    "GROUP_OWNERSHIP_UNKNOWN",
    "GROUP_RUNNING",
    "GROUP_STOPPED",
    "INCOMPLETE_DESCENDANTS_RUNNING",
    "INCOMPLETE_EVIDENCE_UNAVAILABLE",
    "INCOMPLETE_GROUP_NOT_STOPPED",
    "INCOMPLETE_OWNERSHIP_UNKNOWN",
    "INCOMPLETE_SWEEP_OPT_OUT",
    "REFUSE_INVALID_PGID",
    "REFUSE_OWN_PROCESS_GROUP",
    "URGENT_ESCAPEE_POLICY",
    "URGENT_ESCAPEE_SIGNAL",
    "URGENT_SAMPLE_TIMEOUT_SECONDS",
    "GroupLiveness",
    "IdentityVerdict",
    "_probe_group",
    "_verify_identity",
    "collect_descendant_evidence",
    "confirm_termination",
    "force_stop_process_group",
    "terminate_escaped_descendants",
    "PS_COMMAND",
    "PS_TABLE_COMMAND",
    "PS_BIRTH_COMMAND",
    "COMMAND_MAX_CHARS",
    "SAMPLE_TIMEOUT_SECONDS",
    "SWEEP_DISABLED_ENV_VAR",
    "_SWEEP_DISABLED_VALUES",
    "SWEEP_SETTLE_SECONDS",
    "POST_RESULT_RECOVERY_POLICY",
    "POST_RESULT_RECOVERY_SIGNAL",
    "POST_RESULT_ROOT_RECOVERY_POLICY",
    "_CPU_TIME_RE",
    "_BIRTH_PATTERN",
    "_BIRTH_RE",
    "_LSTART_RE",
    "_split_birth",
    "_parse_cpu_time",
    "descendant_pids",
    "sample_process_tree",
    "process_birth",
    "CpuIdentity",
    "accumulate_cpu_seconds",
    "_LAST_TREE_LOCK",
    "record_process_tree",
    "clear_process_tree",
    "last_process_tree",
    "sample_process_table",
    "group_member_pids",
    "sweep_enabled",
    "os",
    "time",
]
