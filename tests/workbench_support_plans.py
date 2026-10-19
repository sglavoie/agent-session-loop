"""Focused portion of tests/workbench_support.py."""

from .workbench_support_api import *


@dataclass(frozen=True)
class ShellCorpus:
    """One set of plans on disk, and the identities both modes derive from it.

    Both deployments are built over *this* object rather than over a directory
    each, because the thing under test is that they are two views of the same
    plans. Two corpora seeded the same way would produce two different sets of
    plan keys -- the key is a digest of the path -- and a ``#run=`` link would
    then resolve in whichever mode minted it, which is exactly the failure this
    fixture has to be able to see.
    """

    root: Path
    state_dir: Path
    registry_path: Path
    plans_root: Path
    plan_dirs: tuple[Path, ...]

    @property
    def plan_roots(self) -> tuple[str, ...]:
        """The search roots to hand a local reader, as it wants them."""
        return (str(self.plans_root),)

    @property
    def plan_keys(self) -> dict[str, str]:
        """Each plan's name mapped to the digest either mode addresses it by."""
        return {plan.name: catalog.plan_key(str(plan)) for plan in self.plan_dirs}

    @property
    def run_keys(self) -> dict[str, str]:
        """Each plan's name mapped to the run key a ``#run=`` link carries.

        Derived from the run the plan's state actually names -- ``None`` for a
        plan that has never run, which is a distinct component rather than the
        string. The remote catalog is published with these rather than with
        invented run ids, because a link that resolved in one mode and not the
        other would be two links wearing one format.
        """
        return {
            name: catalog.run_key(plan_key, SHELL_RUN_IDS.get(name))
            for name, plan_key in self.plan_keys.items()
        }

    def plan_dir(self, name: str) -> Path:
        """The canonical directory of a seeded plan, by name."""
        for plan in self.plan_dirs:
            if plan.name == name:
                return plan
        raise KeyError(name)


def shell_iso(epoch: float) -> str:
    """One moment in the spelling ASL writes into ``state.json``.

    The runner records ISO-8601 and the Pi records epoch seconds; both are read
    by the same window arithmetic. Writing the local side in ASL's own spelling
    is what makes that equivalence something the fixture proves rather than
    something it arranges around.
    """
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _seed_shell_runner_state(
    plan_dir: Path, state_dir: Path, name: str, run_id: str | None
) -> None:
    """Give a plan the runner state its published state claims it has.

    Written as a real ``state.json`` under the real per-plan state directory,
    so the local mode *derives* "paused for a reply" from the same file ASL
    would have written, rather than being told. A plan with no run id is left
    with no state at all, which is how a plan that has never run looks.

    ``charlie`` gets a *finished* state instead, because "this run ended and
    sessions remain" is a different row from "this run is waiting for you", and
    only the finished one can age out of the eight-hour window: a state with no
    ``run_finished_at`` and no live lock reads as stale, not as quiet.
    """
    if run_id is None:
        return
    plan_state = state.plan_state_dir(plan_dir, state_dir)
    plan_state.mkdir(parents=True, exist_ok=True)
    if name == "charlie":
        last = SHELL_HISTORY_RUNS[-1]
        started = time.time() - float(last["started_ago"])
        payload: dict[str, Any] = {
            "plan_dir": str(plan_dir),
            "run_id": run_id,
            "session_id": "1",
            "ok": True,
            "paused": False,
            "reason": str(last["reason"]),
            "run_started_at": shell_iso(started),
            "run_finished_at": shell_iso(started + float(last["duration"])),
            "updated_at": started + float(last["duration"]),
        }
    else:
        payload = {
            "plan_dir": str(plan_dir),
            "run_id": run_id,
            "session_id": "2",
            "paused": True,
            "updated_at": SHELL_FROZEN_NOW - 60.0,
        }
    (plan_state / events.STATE_FILE_NAME).write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    if name == "charlie":
        _seed_shell_history_events(plan_state, plan_dir)


def _seed_shell_history_events(plan_state: Path, plan_dir: Path) -> None:
    """Write the endings ``charlie``'s history pane reads back.

    Real ``events.jsonl`` records rather than pre-built entries, so the local
    provider *interprets* them with the same importer the connector publishes
    from. The remote store is then seeded from that interpretation rather than
    from a second hand-written copy, which is what makes the two panes unable to
    disagree about what a run did.

    Timestamps are relative to the real clock and not to
    :data:`SHELL_FROZEN_NOW`, because the local read applies a seven-day
    retention measured against ``time.time()``: entries pinned to a fixed epoch
    would silently fall out of the window as the fixture ages.
    """
    now = time.time()
    records: list[events.Event] = []
    for run in SHELL_HISTORY_RUNS:
        started = now - float(run["started_ago"])
        finished = started + float(run["duration"])
        records.append(
            events.Event(
                name="run_started",
                created_at=shell_iso(started),
                payload={
                    "run_id": run["run_id"],
                    "plan_dir": str(plan_dir),
                    "harness": "claude",
                    "model": "opus",
                    "run_started_at": shell_iso(started),
                },
            )
        )
        records.append(
            events.Event(
                name="run_finished",
                created_at=shell_iso(finished),
                payload={
                    "run_id": run["run_id"],
                    "plan_dir": str(plan_dir),
                    # Both flags, always. ``classify_outcome`` refuses to name an
                    # outcome unless ``ok`` and ``paused`` are both real
                    # booleans, so a record that merely omits ``paused`` imports
                    # as ``unknown`` -- which would make the pane's whole job
                    # untestable.
                    "ok": run["ok"],
                    "paused": False,
                    "reason": run["reason"],
                    "run_started_at": shell_iso(started),
                    "run_finished_at": shell_iso(finished),
                    "duration_seconds": run["duration"],
                    "completed_session_ids": list(run["completed_sessions"]),
                    "counts": {
                        "complete": len(run["completed_sessions"]),
                        "pending": run["pending"],
                    },
                },
            )
        )
    (plan_state / events.EVENTS_FILE_NAME).write_text(
        "".join(
            json.dumps(record.to_dict(), sort_keys=True) + "\n" for record in records
        ),
        encoding="utf-8",
    )


def _seed_shell_archive(state_dir: Path, plans_root: Path) -> None:
    """Write the archive entries the archive browser reads back.

    Real entry directories under the real archive root, each with the
    ``archive.json`` the Workbench writes, because the listing is produced by
    enumerating that root: a hand-built listing would test the fixture's idea
    of an archive rather than the reader's.

    ``expires_at`` is stored, but it is *not* what decides the verdict -- the
    retention window in the registry is, applied to ``archived_at`` at read
    time. Both are written here so an entry that disagrees with itself would be
    a fixture bug rather than a silent pass.
    """
    now = time.time()
    for entry in SHELL_ARCHIVE_ENTRIES:
        archived_at = now - float(entry["archived_ago"])
        expires_at = archived_at + float(entry["retention_days"]) * 24 * 60 * 60
        entry_dir = workbench_archive.archive_root(state_dir) / str(entry["archive_id"])
        (entry_dir / workbench_archive.ARCHIVE_PLAN_DIR_NAME).mkdir(
            parents=True, exist_ok=True
        )
        (
            entry_dir / workbench_archive.ARCHIVE_PLAN_DIR_NAME / "manifest.md"
        ).write_text(
            f"# {str(entry['plan_name']).title()}\n\nOne archived plan.\n",
            encoding="utf-8",
        )
        (entry_dir / workbench_archive.ARCHIVE_METADATA_FILE_NAME).write_text(
            json.dumps(
                {
                    "archive_id": entry["archive_id"],
                    "original_plan_dir": str(plans_root / str(entry["plan_name"])),
                    "archived_at": shell_iso(archived_at),
                    "expires_at": shell_iso(expires_at),
                    "summary": entry["summary"],
                    "pin": None,
                    "plan_archived": True,
                    "state_archived": False,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


def seed_shell_plans(root: Path) -> ShellCorpus:
    """Write the plan corpus both modes read, under ``root``.

    Each plan is a real plan tree with real sessions, because the local
    provider enumerates the filesystem and a stub directory would be
    enumerated as a malformed plan rather than as a plan.
    """
    root = Path(root).resolve()
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    registry_path = root / workbench_registry_models.REGISTRY_FILE_NAME
    plans_root = root / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, name in enumerate(SHELL_PLAN_NAMES):
        plan_dir = plans_root / name
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text(
            f"# {name.title()}\n\nOne plan, for the shell fixture.\n", encoding="utf-8"
        )
        # ``alpha`` and ``charlie`` are part-done; ``bravo`` has not started.
        # A plan with no pending session would be ``complete``, which the
        # default queue hides -- and a hidden row proves nothing about the
        # section it was meant to land in.
        statuses = ("pending", "pending") if index == 1 else ("complete", "pending")
        for number, status_value in enumerate(statuses, start=1):
            (plan_dir / f"session-{number}.md").write_text(
                "\n".join(
                    (
                        f"# Session {number}: {name.title()} step {number}",
                        "",
                        "Instructions.",
                        "",
                        "## Handoff",
                        "",
                        f"- **Status**: {status_value}",
                        "- **Files modified**: (none)",
                        "- **Key decisions**: (none)",
                        "- **Blockers for next session**: (none)",
                        "- **Notes**:",
                        "",
                    )
                ),
                encoding="utf-8",
            )
        canonical = plans_files.canonicalize_path(plan_dir)
        _seed_shell_runner_state(canonical, state_dir, name, SHELL_RUN_IDS.get(name))
        written.append(canonical)
    _seed_shell_archive(state_dir, plans_files.canonicalize_path(plans_root))
    return ShellCorpus(
        root=root,
        state_dir=state_dir,
        registry_path=registry_path,
        plans_root=plans_files.canonicalize_path(plans_root),
        plan_dirs=tuple(written),
    )


__all__ = ["argparse", "http", "json", "threading", "time", "Callable", "Mapping", "dataclass", "field", "UTC", "datetime", "sha256", "Path", "Any", "cli", "events", "plans_files", "state", "workbench_archive", "workbench_history", "workbench_registry_models", "workbench_remote_reads", "workbench_server", "catalog", "observation", "workbench_http", "IDENTITY_HEADER", "CredentialStore", "ObservationPolicy", "ReadAnswer", "RemoteProvider", "WorkbenchStore", "STATIC_ROUTE_KEYS", "CLI_WORKBENCH_COMMAND", "CLI_PLANS_COMMAND", "SurfaceReadError", "_subparser_choices", "workbench_cli_entry_points", "workbench_api_routes", "RecordingWorkbenchServer", "recording_server_factory", "WorkbenchAPIFixture", "SHELL_OWNER_IDENTITY", "SHELL_CONNECTOR_ID", "SHELL_FROZEN_NOW", "SHELL_CONTRACT_SET_ID", "SHELL_PLAN_NAMES", "SHELL_RUN_IDS", "SHELL_QUIET_SECONDS", "SHELL_HISTORY_RUNS", "SHELL_ARCHIVE_ENTRIES", "SHELL_ACTION_TOKEN", "SHELL_PERMITTED_ACTIONS", "ShellCorpus", "shell_iso", "_seed_shell_runner_state", "_seed_shell_history_events", "_seed_shell_archive", "seed_shell_plans"]  # fmt: skip
