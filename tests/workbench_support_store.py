"""Focused portion of tests/workbench_support.py."""

from .workbench_support_plans import *


def start_local_shell_server(
    corpus: ShellCorpus,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
) -> tuple[Any, threading.Thread]:
    """The laptop's deployment: the real local server over a seeded corpus."""
    server = workbench_server.create_server(
        host=host,
        port=port,
        state_dir=corpus.state_dir,
        registry_path=corpus.registry_path,
        plan_roots=corpus.plan_roots,
        include_default_dirs=False,
        token=token,
    )
    thread = threading.Thread(
        target=server.serve_forever, name="workbench-shell-local", daemon=True
    )
    thread.start()
    return server, thread


# -- The remote half --------------------------------------------------------


def _shell_envelope(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": 1,
        "kind": kind,
        "connector_id": SHELL_CONNECTOR_ID,
        "sent_at": payload.get("observed_at", SHELL_FROZEN_NOW),
        "payload": payload,
    }


def shell_catalog_entries(corpus: ShellCorpus) -> list[dict[str, Any]]:
    """One published catalog entry per plan, keyed the way the Pi addresses it.

    The identities come from the corpus, which derives them with the catalog's
    own functions from the same plan directories the local mode enumerates. So
    the remote fixture publishes the *same* ``plan_key`` and ``run_key`` the
    laptop computes, and one ``#run=`` link resolves in either deployment
    rather than in whichever one minted it.
    """
    plan_keys = corpus.plan_keys
    run_keys = corpus.run_keys
    entries = []
    for index, name in enumerate(SHELL_PLAN_NAMES):
        plan_key = plan_keys[name]
        quiet = name == "charlie"
        paused = SHELL_RUN_IDS.get(name) is not None and not quiet
        if quiet:
            state_name, action, group = "recent", "run", "ready"
            activity = "Run finished, sessions remain"
        elif paused:
            state_name, action, group = "paused-for-reply", "reply", "needs-action"
            activity = "Reply needed"
        else:
            state_name, action, group = "ready", "run", "ready"
            activity = "Ready to run"
        entry: dict[str, Any] = {
            "plan_key": plan_key,
            "run_key": run_keys[name],
            # The words the Mac derived, not words invented for the Pi. These
            # plans register no repositories, so the catalog names their project
            # ``Unknown project`` and titles them from the manifest's ``# H1``;
            # publishing anything else here would put two different names for
            # one plan on the two deployments and make a cross-mode assertion
            # impossible to write honestly.
            "title": name.title(),
            "display_label": name.title(),
            # What the Mac derived from this plan's state, repeated. The
            # local mode derives the same words from the same file, so the
            # two queues describe one plan rather than agreeing by luck.
            "primary_state": state_name,
            "recommended_action": action,
            "urgency_rank": index,
            "urgency_group": group,
            "activity_label": activity,
            "projects": [catalog.UNKNOWN_PROJECT],
            "project_group": catalog.UNKNOWN_PROJECT,
            "counts": {"sessions": 2},
            "sources": ["manifest"],
            "exists": True,
            "pinned": False,
            "repository_count": 0,
            "warning_count": 0,
            "search_text": name.casefold(),
            "registry_digest": "9d2e59249fe21a3c",
        }
        if quiet:
            # Epoch seconds, which is how the Pi publishes a moment; the local
            # mode publishes ISO-8601 for the same plan. Both are read by one
            # window, and seeding them differently is what proves that.
            entry["last_activity_at"] = SHELL_FROZEN_NOW - SHELL_QUIET_SECONDS
            entry["last_activity_source"] = "run-finished"
        entries.append(entry)
    return entries


def seed_shell_history(store: WorkbenchStore, corpus: ShellCorpus) -> None:
    """Publish the endings the Mac imported, exactly as the connector would.

    The entries are not written by hand: they are the objects
    :func:`agent_session_loop.workbench_history.read_window` builds from the
    corpus's own ``events.jsonl``, which is the same projection the local
    history route answers with. Seeding the store from that projection is what
    makes "both modes present consistent history" a property of the code rather
    than of two hand-maintained copies agreeing.

    The window is read against the real clock for the same reason it is written
    against it -- a seven-day retention measured from ``time.time()`` would drop
    entries pinned to :data:`SHELL_FROZEN_NOW` as the fixture ages -- while the
    batch is *received* at the frozen moment, because that is when the Pi is
    pretending to have heard from the Mac.
    """
    view = workbench_history.read_window(now=time.time(), state_dir=corpus.state_dir)
    store.ingest_history(
        _shell_envelope(
            "history_batch",
            {
                "observed_at": SHELL_FROZEN_NOW,
                "batch_id": "shell-fixture-history-1",
                "generated_at": SHELL_FROZEN_NOW,
                "window_start": view.window_start,
                "window_end": view.window_end,
                "complete": view.complete,
                "more": False,
                "entries": [entry.to_payload() for entry in view.entries],
                "questions": [],
            },
        ),
        received_at=SHELL_FROZEN_NOW,
    )


def seed_shell_store(
    store: WorkbenchStore,
    entries: list[dict[str, Any]],
    *,
    corpus: ShellCorpus | None = None,
) -> None:
    """Give the Pi something to have observed: a heartbeat, a catalog, history."""
    store.ingest_heartbeat(
        _shell_envelope(
            "heartbeat",
            {
                "observed_at": SHELL_FROZEN_NOW,
                "bridge_alive": True,
                "bridge_protocol": 2,
                "asl_version": "0.2.0",
                "contract_set_id": SHELL_CONTRACT_SET_ID,
                "source_generation": 1,
            },
        ),
        received_at=SHELL_FROZEN_NOW,
    )
    runs = []
    for name, entry in zip(SHELL_PLAN_NAMES, entries, strict=True):
        run_id = SHELL_RUN_IDS.get(name)
        live = run_id is not None and name != "charlie"
        run: dict[str, Any] = {
            "run_key": entry["run_key"],
            "project": catalog.UNKNOWN_PROJECT,
            "plan_key": entry["plan_key"],
            # ``None`` where no run has started, which is the component the run
            # key above digests. Naming an id here would contradict it.
            "run_id": run_id,
            "primary_state": entry["primary_state"],
            "phase": entry["primary_state"],
            "urgency_group": entry["urgency_group"],
            # Only a live run permits anything, and only the Mac decides what.
            # The Pi repeats the list and the token that redeems it; a
            # submission naming an action not in this list is refused before it
            # is carried anywhere. A finished run permits nothing, which is why
            # ``charlie`` carries an id and an empty list rather than both.
            "actions": list(SHELL_PERMITTED_ACTIONS) if live else [],
            "last_activity_at": entry.get("last_activity_at", SHELL_FROZEN_NOW - 30.0),
        }
        if live:
            run["action_token"] = SHELL_ACTION_TOKEN
            run["session_id"] = "2"
        runs.append(run)
    material = json.dumps([SHELL_FROZEN_NOW, runs], sort_keys=True).encode()
    store.ingest_snapshot(
        _shell_envelope(
            "snapshot",
            {
                "observed_at": SHELL_FROZEN_NOW,
                "digest": sha256(material).hexdigest(),
                "contract_set_id": SHELL_CONTRACT_SET_ID,
                "staleness_bound_seconds": 20.0,
                "source_generation": 1,
                "row_count": len(runs),
                "truncated": False,
                "runs": runs,
                "warnings": [],
            },
        ),
        received_at=SHELL_FROZEN_NOW,
    )
    store.ingest_catalog(
        _shell_envelope(
            "catalog_page",
            {
                "observed_at": SHELL_FROZEN_NOW,
                "generation": "shell-fixture-1",
                "source_generation": 1,
                "complete": True,
                "final": True,
                "offset": 0,
                "total": len(entries),
                "entries": entries,
            },
        ),
        received_at=SHELL_FROZEN_NOW,
    )
    if corpus is not None:
        seed_shell_history(store, corpus)


__all__ = ["argparse", "http", "json", "threading", "time", "Callable", "Mapping", "dataclass", "field", "UTC", "datetime", "sha256", "Path", "Any", "cli", "events", "plans_files", "state", "workbench_archive", "workbench_history", "workbench_registry_models", "workbench_remote_reads", "workbench_server", "catalog", "observation", "workbench_http", "IDENTITY_HEADER", "CredentialStore", "ObservationPolicy", "ReadAnswer", "RemoteProvider", "WorkbenchStore", "STATIC_ROUTE_KEYS", "CLI_WORKBENCH_COMMAND", "CLI_PLANS_COMMAND", "SurfaceReadError", "_subparser_choices", "workbench_cli_entry_points", "workbench_api_routes", "RecordingWorkbenchServer", "recording_server_factory", "WorkbenchAPIFixture", "SHELL_OWNER_IDENTITY", "SHELL_CONNECTOR_ID", "SHELL_FROZEN_NOW", "SHELL_CONTRACT_SET_ID", "SHELL_PLAN_NAMES", "SHELL_RUN_IDS", "SHELL_QUIET_SECONDS", "SHELL_HISTORY_RUNS", "SHELL_ARCHIVE_ENTRIES", "SHELL_ACTION_TOKEN", "SHELL_PERMITTED_ACTIONS", "ShellCorpus", "shell_iso", "_seed_shell_runner_state", "_seed_shell_history_events", "_seed_shell_archive", "seed_shell_plans", "start_local_shell_server", "_shell_envelope", "shell_catalog_entries", "seed_shell_history", "seed_shell_store"]  # fmt: skip
