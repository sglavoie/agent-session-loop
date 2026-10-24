"""Whole-state-root pairing survey for the AgentMux cockpit integration.

``amx config asl-sync`` has to answer six questions before it is allowed to
rewrite AgentMux's configuration, and five of them are ASL's to answer: which
executable is speaking, which contract set it implements, where its canonical
state root is, whether any *active* run still carries pre-projection state, and
whether the inactive remainder can be migrated. Every existing ASL surface that
knows those things is keyed by one plan directory (``contract-check``,
``migrate-state``, ``compact-state``), and pairing is a property of the whole
root rather than of any plan in it.

So this module is the plan-directory-free surface: it enumerates every plan
state directory under one root, classifies each one, and — only when asked —
upgrades the ones that can be upgraded without touching a live run.

The refusal rule is the reason this is a survey and not a migration loop. A run
whose lock is live holds its state payload in memory and rewrites the whole file
on its next transition, so upgrading underneath it is reverted at best and, at
worst, leaves a process emitting schema-1 lifecycle hooks at a consumer that
only ingests schema 2. Such a run is *named* and the whole pairing is refused;
it is never stopped, and never migrated anyway. Stopping someone else's run to
finish a configuration change is not a decision a pairing command gets to make.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import (
    __version__,
    agent_mux_projection,
    events,
    plans_files,
    responses,
    state,
    state_locks,
    state_plan_refs,
    state_schema_upgrade,
)

COMMAND_NAME = "agent-mux-pairing"

#: The bridge protocol this build serves. Restated here rather than imported
#: from :mod:`agent_session_loop.bridge` because pairing must not pull the
#: bridge server (and its projection machinery) into a survey that reads files.
BRIDGE_PROTOCOL_VERSION = 2

# Exit reasons. They are part of the command contract: AgentMux branches on
# them, so each one names a distinct operator action.
REASON_OK = "ok"
REASON_ACTIVE_INCOMPATIBLE_RUNS = "active-incompatible-runs"
REASON_MIGRATION_FAILED = "migration-failed"
REASON_STATE_ROOT_ERROR = "state-root-error"

# Per-plan classifications.
CLASS_COMPATIBLE = "compatible"
CLASS_MIGRATABLE = "migratable"
CLASS_ACTIVE_INCOMPATIBLE = "active-incompatible"
CLASS_UNPROJECTABLE = "unprojectable"
CLASS_UNREADABLE = "unreadable"
CLASS_EMPTY = "empty"

#: Classifications counted in the ``plans`` summary, in report order.
CLASSIFICATIONS = (
    CLASS_COMPATIBLE,
    CLASS_MIGRATABLE,
    CLASS_ACTIVE_INCOMPATIBLE,
    CLASS_UNPROJECTABLE,
    CLASS_UNREADABLE,
    CLASS_EMPTY,
)


def _nonempty_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def resolved_executable(argv0: str | None = None) -> str | None:
    """Return the absolute path of the ``asl`` entry point that is running.

    ``argv[0]`` is the only thing that names the entry point rather than the
    interpreter, and it is made absolute rather than reported verbatim: a
    relative ``argv[0]`` is meaningless to a daemon whose working directory is
    not the caller's, and the whole point of persisting this value is that
    nothing downstream has to consult an interactive shell's ``PATH``.

    Symlinks are deliberately *not* resolved. A launcher on ``PATH`` is usually
    the stable name — ``~/.local/bin/asl`` — pointing at a target that an
    upgrade replaces (a versioned virtualenv, a new build's wrapper). Following
    the link would persist the path that churns instead of the one that does
    not. Absolute is the requirement; canonical is not, and here it is worse.

    ``None`` means the entry point could not be named — an embedded or ``-c``
    invocation — which is a fact the caller must see rather than paper over
    with a bare ``asl`` that puts ``PATH`` back in the loop.
    """
    raw = argv0 if argv0 is not None else (sys.argv[0] if sys.argv else "")
    raw = raw.strip()
    if not raw:
        return None
    if os.sep in raw or (os.altsep and os.altsep in raw):
        candidate = os.path.abspath(raw)
    else:
        found = shutil.which(raw)
        if found is None:
            return None
        candidate = os.path.abspath(found)
    if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        return None
    return candidate


def _plan_dir_for_state_dir(state_dir: Path) -> str | None:
    """Recover the plan directory a state directory belongs to.

    ``state.json`` is authoritative; the lock is the fallback for a record that
    has not been written yet. Both are read defensively: this runs over every
    directory in the root, including ones written by a build that is not this
    one.
    """
    for name in (events.STATE_FILE_NAME, state.LOCK_FILE_NAME):
        obj = state_plan_refs._read_json_object_safe(state_dir / name)
        if obj is None:
            continue
        value = _nonempty_str(obj.get("plan_dir"))
        if value:
            return value
    return None


def _lock_for(state_dir: Path, plan_dir: str | None) -> state.LockInfo | None:
    """Read one plan's lock without reclaiming it.

    Reclaiming is a mutation, and a survey must be able to run against a root it
    is not going to touch. An unmatched ``plan_dir`` reads as stale, which is
    the safe direction here: it only ever moves a plan out of the refusing
    class, and a lock whose plan directory disagrees with its own state record
    cannot name a live owner of *this* record.
    """
    lock_path = state_dir / state.LOCK_FILE_NAME
    if not lock_path.exists():
        return None
    expected = plans_files.canonicalize_path(plan_dir) if plan_dir else state_dir
    return state_locks._read_lock_path(lock_path, expected, reclaim=False)


def _classify(state_dir: Path) -> dict[str, Any]:
    """Classify one plan state directory for pairing."""
    state_path = state_dir / events.STATE_FILE_NAME
    entry: dict[str, Any] = {
        "state_dir": str(state_dir),
        "plan_dir": None,
        "run_id": None,
        "schema_version": None,
        "classification": CLASS_EMPTY,
        "pid": None,
        "detail": "",
    }
    if not state_path.is_file():
        entry["detail"] = "no state record"
        return entry

    state_obj = state_plan_refs._read_json_object_safe(state_path)
    if state_obj is None:
        entry["classification"] = CLASS_UNREADABLE
        entry["detail"] = f"state file is not a JSON object: {state_path}"
        return entry

    plan_dir = _nonempty_str(state_obj.get("plan_dir")) or _plan_dir_for_state_dir(
        state_dir
    )
    entry["plan_dir"] = plan_dir
    entry["run_id"] = _nonempty_str(state_obj.get("run_id"))
    version = state_schema_upgrade.state_schema_version(state_obj)
    entry["schema_version"] = version

    current = agent_mux_projection.STATE_SCHEMA_VERSION
    lock = _lock_for(state_dir, plan_dir)
    live = lock is not None and lock.active
    if live and lock is not None:
        entry["pid"] = lock.pid

    if version == current:
        entry["classification"] = CLASS_COMPATIBLE
        entry["detail"] = f"state is at schema version {current}"
        return entry

    upgradable = agent_mux_projection.UPGRADABLE_STATE_SCHEMA_VERSIONS
    if version is not None and version not in upgradable:
        # A future or non-integer version, which `state_schema_version` reports
        # as -1. Not migratable and not a run this build may act on either way;
        # a live one still refuses pairing, because a process this build does
        # not understand is exactly the process that must not be surprised by a
        # rewritten configuration.
        entry["classification"] = (
            CLASS_ACTIVE_INCOMPATIBLE if live else CLASS_UNPROJECTABLE
        )
        entry["detail"] = (
            f"state records schema version {state_obj.get('version')!r}, which "
            f"this build cannot upgrade"
        )
        return entry

    if live:
        entry["classification"] = CLASS_ACTIVE_INCOMPATIBLE
        entry["detail"] = (
            f"plan is locked by pid {entry['pid']} and its state is at schema "
            f"version {'absent' if version is None else version}, not {current}"
        )
        return entry

    if not agent_mux_projection.projectable(state_obj):
        entry["classification"] = CLASS_UNPROJECTABLE
        entry["detail"] = (
            "state records no run identity (run_id, plan_dir and "
            "run_started_at are all required), so no projection can be built"
        )
        return entry

    entry["classification"] = CLASS_MIGRATABLE
    entry["detail"] = (
        f"schema version {'absent' if version is None else version} → {current}"
    )
    return entry


def _migrate_entry(entry: dict[str, Any], state_dir: str | None) -> dict[str, Any]:
    """Upgrade one migratable plan and return its migration record."""
    plan_dir = entry["plan_dir"]
    record: dict[str, Any] = {
        "plan_dir": plan_dir,
        "state_dir": entry["state_dir"],
        "schema_version_before": entry["schema_version"],
        "schema_version_after": None,
        "applied": False,
        "ok": False,
        "reason": None,
        "detail": "",
    }
    if plan_dir is None:
        record["reason"] = "unprojectable-state"
        record["detail"] = "state record names no plan directory"
        return record
    try:
        report = state_schema_upgrade.upgrade_plan_state_schema(
            plan_dir, state_dir=state_dir, apply=True
        )
    except state_schema_upgrade.MigrationError as error:
        record["reason"] = error.reason
        record["detail"] = str(error)
        return record
    except (OSError, state.StateError) as error:
        record["reason"] = "migration-error"
        record["detail"] = str(error)
        return record
    record["ok"] = True
    record["applied"] = report.applied
    record["schema_version_after"] = report.schema_version_after
    record["detail"] = report.schema_note or ""
    return record


def survey(
    *,
    state_dir: str | Path | None = None,
    migrate: bool = False,
    argv0: str | None = None,
) -> dict[str, Any]:
    """Return the pairing survey for one state root, optionally migrating it.

    ``migrate`` is refused outright — nothing is written — when any plan is
    classified ``active-incompatible``. The refusal is whole-root rather than
    per-plan because pairing is a whole-root promise: AgentMux is about to
    persist one executable, one state root, and one contract set for every run
    under it, and a live legacy run would keep publishing state that promise
    does not cover.
    """
    root = state.state_root(state_dir)
    payload: dict[str, Any] = {
        "asl_version": __version__,
        "executable": resolved_executable(argv0),
        "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
        "bridge_protocol": BRIDGE_PROTOCOL_VERSION,
        "state_schema_version": agent_mux_projection.STATE_SCHEMA_VERSION,
        "state_root": str(root),
        "state_root_present": root.is_dir(),
        "migrate_requested": migrate,
        "migrated": False,
        # Seeded so every outcome, including a root that cannot be scanned,
        # presents the same keys. A consumer branches on `reason`; it should
        # never also have to branch on which fields exist.
        "plans": {"total": 0, **dict.fromkeys(CLASSIFICATIONS, 0)},
        "active_incompatible_runs": [],
        "migrations": [],
        "issues": [],
    }

    entries: list[dict[str, Any]] = []
    try:
        for plan_state in state_plan_refs.iter_plan_state_dirs(root):
            entries.append(_classify(plan_state))
    except state.StateError as error:
        payload["reason"] = REASON_STATE_ROOT_ERROR
        payload["error"] = str(error)
        payload["ok"] = False
        return payload

    def _of_class(name: str) -> list[dict[str, Any]]:
        return [entry for entry in entries if entry["classification"] == name]

    active = _of_class(CLASS_ACTIVE_INCOMPATIBLE)
    migrations: list[dict[str, Any]] = []
    if migrate and not active:
        for entry in _of_class(CLASS_MIGRATABLE):
            migrations.append(_migrate_entry(entry, _as_str(state_dir)))
        payload["migrated"] = True

    payload["plans"] = {
        "total": len(entries),
        **{
            classification: sum(
                1 for entry in entries if entry["classification"] == classification
            )
            for classification in CLASSIFICATIONS
        },
    }
    payload["active_incompatible_runs"] = [
        {
            "plan_dir": entry["plan_dir"],
            "state_dir": entry["state_dir"],
            "run_id": entry["run_id"],
            "pid": entry["pid"],
            "schema_version": entry["schema_version"],
            "detail": entry["detail"],
        }
        for entry in active
    ]
    payload["migrations"] = migrations
    payload["issues"] = [
        {
            "classification": entry["classification"],
            "state_dir": entry["state_dir"],
            "plan_dir": entry["plan_dir"],
            "detail": entry["detail"],
        }
        for entry in entries
        if entry["classification"] in (CLASS_UNPROJECTABLE, CLASS_UNREADABLE)
    ]

    failed = [record for record in migrations if not record["ok"]]
    if active:
        payload["ok"] = False
        payload["reason"] = REASON_ACTIVE_INCOMPATIBLE_RUNS
        payload["error"] = (
            f"{len(active)} active run(s) still carry state this build cannot "
            f"pair with; stop them and re-run"
        )
    elif failed:
        payload["ok"] = False
        payload["reason"] = REASON_MIGRATION_FAILED
        payload["error"] = f"{len(failed)} of {len(migrations)} migration(s) failed"
    else:
        payload["ok"] = True
        payload["reason"] = REASON_OK
    return payload


def _as_str(value: str | Path | None) -> str | None:
    return None if value is None else os.fspath(value)


def _print_text(payload: dict[str, Any]) -> None:
    print(f"ASL version: {payload['asl_version']}")
    print(f"Executable: {payload['executable'] or 'unresolved'}")
    print(f"Contract set: {payload['contract_set_id']}")
    print(f"Bridge protocol: {payload['bridge_protocol']}")
    print(f"State root: {payload['state_root']}")
    print(f"State schema: {payload['state_schema_version']}")
    counts = payload.get("plans")
    if counts is not None:
        summary = " ".join(
            f"{classification}={counts[classification]}"
            for classification in CLASSIFICATIONS
        )
        print(f"Plans: {counts['total']} ({summary})")
    for run in payload.get("active_incompatible_runs", []):
        print(
            f"  [REFUSE] {run['plan_dir'] or run['state_dir']} "
            f"run {run['run_id'] or 'unknown'} pid {run['pid']}: {run['detail']}"
        )
    for record in payload.get("migrations", []):
        marker = "OK" if record["ok"] else "FAIL"
        print(
            f"  [{marker}] migrate {record['plan_dir'] or record['state_dir']}: "
            f"{record['detail'] or record['reason']}"
        )
    for issue in payload.get("issues", []):
        print(
            f"  [SKIP] {issue['classification']} "
            f"{issue['plan_dir'] or issue['state_dir']}: {issue['detail']}"
        )
    if not payload["ok"]:
        # Flushed first so the diagnostic follows the report it explains when
        # both streams land in one terminal or one captured log.
        sys.stdout.flush()
        print(f"{COMMAND_NAME}: {payload.get('error', '')}", file=sys.stderr)


def pairing_from_args(args: argparse.Namespace) -> int:
    """``asl agent-mux pairing`` entry point."""
    payload = survey(
        state_dir=getattr(args, "state_dir", None),
        migrate=bool(getattr(args, "migrate", False)),
    )
    reserved = {"ok", "error", "reason"}
    fields = {key: value for key, value in payload.items() if key not in reserved}
    if getattr(args, "json", False):
        if payload["ok"]:
            responses.print_json(
                responses.success_payload(
                    COMMAND_NAME, reason=payload["reason"], **fields
                )
            )
        else:
            responses.print_json(
                responses.error_payload(
                    COMMAND_NAME,
                    reason=payload["reason"],
                    error=payload.get("error", ""),
                    paused=False,
                    **fields,
                )
            )
    else:
        _print_text(payload)
    return 0 if payload["ok"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report the Agent Mux pairing survey for one ASL state root.",
    )
    parser.add_argument(
        "--state-dir", metavar="DIR", help="Override operational state directory"
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Upgrade every migratable inactive plan to the current state schema",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    return pairing_from_args(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
