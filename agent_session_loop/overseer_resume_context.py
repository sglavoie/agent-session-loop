"""Recover a paused run's launch configuration without starting it."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import events, state
from .overseer_resume_models import (
    LIMIT_FIELDS,
    PROFILE_FIELDS,
    REQUIRED_RUN_CONFIG_KEYS,
    LaunchContext,
)


def recover_launch_context(
    *,
    plan_dir: str,
    repositories: Sequence[str] = (),
    state_dir: str | Path | None = None,
) -> LaunchContext:
    """Rebuild the launch this run was started under, or say why it cannot be.

    Read from the run's own ``state.json`` rather than from anything this
    process was configured with. A resume that fell back to local defaults would
    restart the run under a different harness, model, effort or workspace and
    report it as recovery, which is worse than refusing: the run would appear to
    have been resumed and would in fact have been replaced.

    ``repositories`` is the alias set the action was authorized against. A
    launch context describing a different set is refused, because rehydrating a
    workspace binding nobody authorized is how a permission for one repository
    becomes work in another.
    """
    resolved_state_dir = None if state_dir is None else str(state_dir)
    payload = _read_state(plan_dir, state_dir)
    if payload is None:
        return _unrecoverable(
            plan_dir,
            resolved_state_dir,
            {},
            "this plan has no readable runner state, so the launch it ran under "
            "cannot be restored",
        )
    run_config = payload.get("run_config")
    if not isinstance(run_config, Mapping):
        return _unrecoverable(
            plan_dir,
            resolved_state_dir,
            {},
            "this run's state predates run_config persistence, so its harness, "
            "model, effort and workspace binding were never recorded",
        )

    missing = [
        key
        for key in REQUIRED_RUN_CONFIG_KEYS
        if run_config.get(key) in (None, "", [], {})
    ]
    if missing:
        return _unrecoverable(
            plan_dir,
            resolved_state_dir,
            run_config,
            f"this run's launch metadata is missing {', '.join(missing)}",
        )

    override = run_config.get("state_dir_override")
    override = override if isinstance(override, str) and override else None
    if resolved_state_dir is None:
        resolved_state_dir = override
    elif override is not None and not _same_path(override, resolved_state_dir):
        return _unrecoverable(
            plan_dir,
            resolved_state_dir,
            run_config,
            f"the run recorded state directory {override!r} and this overseer "
            f"reads {resolved_state_dir!r}",
        )

    declared = _aliases(run_config.get("repositories"))
    wanted = tuple(repositories)
    if wanted and declared != wanted:
        return _unrecoverable(
            plan_dir,
            resolved_state_dir,
            run_config,
            f"the run's launch names repositories {declared or ('(none)',)} and "
            f"this action was authorized for {wanted}",
        )

    return LaunchContext(
        plan_dir=plan_dir,
        state_dir=resolved_state_dir,
        profile={key: run_config[key] for key in PROFILE_FIELDS if key in run_config},
        limits={key: run_config[key] for key in LIMIT_FIELDS if key in run_config},
        workspace_root=_text(run_config.get("workspace_root")),
        registry_digest=_text(run_config.get("registry_digest")),
        repositories=declared,
        manual_command=_manual_command(plan_dir, resolved_state_dir, run_config),
    )


#: The ``run_config`` keys a hand-written ``asl run`` restates, and the flags
#: that carry them. Not the whole config: the point is a command an operator can
#: read and finish, not a serialization of everything the run knew.
_MANUAL_FLAGS: tuple[tuple[str, str], ...] = (
    ("--cwd", "cwd"),
    ("--harness", "harness"),
    ("--harness-model", "harness_model"),
    ("--review-harness", "review_harness"),
    ("--review-harness-model", "review_harness_model"),
    ("--effort", "effort"),
    ("--timeout", "timeout"),
)

_COMMIT_MODE_FLAGS: Mapping[str, str] = {
    "per-session": "--commit-per-session",
    "end": "--commit-at-end",
}


def _read_state(
    plan_dir: str, state_dir: str | Path | None
) -> Mapping[str, Any] | None:
    try:
        path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _unrecoverable(
    plan_dir: str,
    state_dir: str | None,
    run_config: Mapping[str, Any],
    detail: str,
) -> LaunchContext:
    """The context for a launch that cannot be rebuilt, and how to do it by hand."""
    return LaunchContext(
        plan_dir=plan_dir,
        state_dir=state_dir,
        profile={},
        limits={},
        workspace_root=_text(run_config.get("workspace_root")),
        registry_digest=_text(run_config.get("registry_digest")),
        repositories=_aliases(run_config.get("repositories")),
        manual_command=_manual_command(plan_dir, state_dir, run_config),
        refusal="environment_unrecoverable",
        detail=detail,
    )


def _manual_command(
    plan_dir: str, state_dir: str | None, run_config: Mapping[str, Any]
) -> str:
    """A best-effort ``asl run`` built from whatever the run did persist.

    Deliberately not ``asl run --resume``: this command is produced only when
    the metadata ``--resume`` reads is the thing that is missing, so echoing the
    flag that failed would send the operator back into the same refusal.
    """
    argv = ["asl", "run", plan_dir]
    for flag, key in _MANUAL_FLAGS:
        value = run_config.get(key)
        if value not in (None, ""):
            argv += [flag, str(value)]
    commit_flag = _COMMIT_MODE_FLAGS.get(_text(run_config.get("commit_mode")))
    if commit_flag is not None:
        argv.append(commit_flag)
    if state_dir:
        argv += ["--state-dir", state_dir]
    return shlex.join(argv)


def _launch_summary(context: LaunchContext) -> str:
    harness = _text(context.profile.get("harness")) or "(no harness recorded)"
    model = _text(context.profile.get("harness_model"))
    effort = _text(context.profile.get("effort"))
    named = [harness, *(part for part in (model, effort) if part)]
    return f"{'/'.join(named)} in {_text(context.profile.get('cwd'))}"


def _aliases(value: object) -> tuple[str, ...]:
    """The repository aliases a persisted workspace projection names, in order."""
    if not isinstance(value, list):
        return ()
    named: list[str] = []
    for row in value:
        alias = row.get("alias") if isinstance(row, Mapping) else None
        if isinstance(alias, str) and alias:
            named.append(alias)
    return tuple(named)


def _same_path(first: str, second: str) -> bool:
    try:
        return Path(first).expanduser() == Path(second).expanduser()
    except OSError, ValueError, RuntimeError:
        return first == second


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
