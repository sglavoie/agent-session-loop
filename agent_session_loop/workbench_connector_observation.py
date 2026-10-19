# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F401, F403, F405, F821, F822, I001
"""ASL corpus projection and observation source for the Workbench connector."""

from __future__ import annotations

from . import workbench_connector_projection as _projection

globals().update(
    {
        name: value
        for name, value in vars(_projection).items()
        if not name.startswith("__")
    }
)
del _projection


# --------------------------------------------------------------------------
# Run rows
# --------------------------------------------------------------------------


def project_run(
    row: Mapping[str, Any],
    *,
    entry: CatalogEntry,
) -> dict[str, Any] | None:
    """Translate one bridge snapshot row into the transport's run shape.

    Returns ``None`` rather than a partial row when a required field has no
    value, which is the Companion's ``observation.project_row`` rule: a row
    missing its run id or its activity time would either be refused on arrival
    or, worse, accepted with a fabricated timestamp. The caller counts the
    refusal in ``failed_projections``, and the snapshot stops claiming to be
    whole.

    ``entry`` supplies the three identity fields the row has no way to know:
    the plan key, the run key, and the project name. They come from the catalog
    record built in the same scan, so a run and its catalog entry can never
    disagree about which plan they describe.

    Four of the bridge row's fields are dropped and not translated:
    ``plan_dir`` and ``workspace_root`` are this machine's layout, ``label`` is
    an operator-typed pin name, and ``question`` carries a body. The remote
    service gets keys and vocabulary words, and nothing it could not have
    inferred from them.
    """
    run_id = row.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    activity = portable_history.epoch(row.get("last_activity_at"))
    if activity is None:
        activity = portable_history.epoch(row.get("run_started_at"))
    if activity is None:
        return None
    question = row.get("question")
    question = question if isinstance(question, Mapping) else None
    published: dict[str, Any] = {
        "run_key": entry.run_key,
        "plan_key": entry.plan_key,
        "project": entry.project_group,
        "run_id": run_id,
        "primary_state": row.get("primary_state"),
        "phase": row.get("phase"),
        "urgency_group": row.get("urgency_group"),
        "actions": _remote_actions(row.get("actions")),
        "last_activity_at": activity,
        "action_token": row.get("action_token"),
        "session_id": portable_payloads.session_id_word(row.get("session_id")),
        "harness": row.get("harness"),
        "run_started_at": portable_history.epoch(row.get("run_started_at")),
        "run_finished_at": portable_history.epoch(row.get("run_finished_at")),
        "pause_reason": portable_payloads.pause_reason_word(row.get("pause_reason")),
        "warnings": list(row.get("warnings") or ()),
    }
    if question is not None:
        published["question_id"] = question.get("question_id")
        published["question_asked_at"] = portable_history.epoch(
            question.get("asked_at")
        )
        published["unanswered_count"] = question.get("unanswered_count")
    return published


def _remote_actions(actions: object) -> list[str]:
    """The published subset of a row's actions, in the transport's vocabulary."""
    if not isinstance(actions, Sequence) or isinstance(actions, str):
        return []
    return [
        REMOTE_ACTIONS[action]
        for action in actions
        if isinstance(action, str) and action in REMOTE_ACTIONS
    ]


def _selection_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Reserve bounded space for unanswered questions, then live work.

    The bridge's ``_snapshot_selection_key`` in intent, restated here because
    it decides which rows survive a cap and that decision has to be visible in
    the module that applies the cap. A paused question must outrank ordinary
    running rows: omitting it leaves the remote incident engine with no input,
    so the notification and reply path can never exist on a busy corpus.
    """
    activity = portable_history.epoch(
        row.get("last_activity_at")
    ) or portable_history.epoch(row.get("run_started_at"))
    waiting_for_reply = bool(row.get("question_id") and row.get("unanswered_count"))
    return (
        not waiting_for_reply,
        row.get("primary_state") not in _LIVE_PRIMARY_STATES,
        activity is None,
        -(activity or 0.0),
        row.get("urgency_rank") or 0,
        str(row.get("plan_key") or ""),
    )


def _display_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Order the surviving rows the way the Workbench queue orders its view."""
    activity = portable_history.epoch(row.get("last_activity_at"))
    return (
        row.get("urgency_rank") or 0,
        activity is None,
        -(activity or 0.0),
        str(row.get("plan_key") or ""),
    )


# --------------------------------------------------------------------------
# The source
# --------------------------------------------------------------------------


class AslObservationSource:
    """One observation pass over this Mac's ASL corpus.

    The whole point of the class is that :meth:`observe` is *one* pass.
    :func:`agent_session_loop.workbench_catalog.scan` returns the portable
    catalog and the Mac-side summaries it was built from, in the same order, so
    the catalog generation and the run rows are cut from a single discovery.
    Scheduling them separately is how a dashboard ends up showing a run the
    catalog says does not exist.

    :meth:`commit` writes the history checkpoint and nothing else, and it is
    the connector -- not this class -- that decides when it has earned the
    right to be called.
    """

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        plan_roots: Iterable[str | Path] = (),
        include_default_dirs: bool = True,
        checkpoints: workbench_history.CheckpointStore | None = None,
        retention_seconds: float = workbench_history.DEFAULT_RETENTION_SECONDS,
        page_limit: int = CATALOG_PAGE_LIMIT,
    ) -> None:
        self.state_dir = state_dir
        self.plan_roots = tuple(plan_roots)
        self.include_default_dirs = include_default_dirs
        self.retention_seconds = float(retention_seconds)
        self.page_limit = max(1, int(page_limit))
        self._checkpoints = (
            checkpoints
            if checkpoints is not None
            else workbench_history.default_store(state_dir)
        )

    @property
    def checkpoints(self) -> workbench_history.CheckpointStore:
        """Where this source's history progress is recorded."""
        return self._checkpoints

    def observe(self, now: float) -> ObservationCycle:
        """Read the corpus once and assemble everything the connector publishes."""
        scan = workbench_catalog.scan(
            state_dir=self.state_dir,
            plan_roots=self.plan_roots,
            include_default_dirs=self.include_default_dirs,
        )
        rows, failed, truncated = self._runs(scan)
        projection = workbench_history.project_history(
            now=now,
            store=self._checkpoints,
            state_dir=self.state_dir,
            retention_seconds=self.retention_seconds,
        )
        return ObservationCycle(
            observed_at=now,
            runs=tuple(rows),
            catalog_pages=scan.catalog.pages(limit=self.page_limit),
            history_batches=projection.batches,
            history=projection.result,
            # The snapshot names the catalog generation its rows were cut from,
            # which is what lets a receiver line the two views up without
            # trusting that they arrived from one read.
            source_digest=scan.catalog.generation,
            truncated=truncated,
            failed_projections=failed,
            warnings=tuple(scan.catalog.warnings),
            degraded_reason=self._degraded(scan, projection),
        )

    def commit(self, cycle: ObservationCycle) -> None:
        """Persist the history checkpoint a fully delivered cycle earned."""
        result = cycle.history
        if isinstance(result, portable_history.ImportResult):
            workbench_history.commit(self._checkpoints, result)

    @staticmethod
    def _degraded(
        scan: workbench_catalog.CatalogScan,
        projection: workbench_history.HistoryProjection,
    ) -> str | None:
        """Why this pass saw less than the whole corpus, if it did.

        Reported on the heartbeat, so an operator watching a Mac that is up but
        half-blind sees the difference between "nothing is happening" and
        "nothing can be read". The two frames already carry the machine-
        readable form -- ``complete`` on the page and on the batch -- and this
        is the summary a human reads.
        """
        faults: list[str] = []
        if not scan.complete:
            faults.append(f"catalog incomplete ({len(scan.projection_warnings)} plans)")
        if not projection.result.complete:
            faults.append("history window incomplete")
        return "; ".join(faults) if faults else None

    def _runs(
        self, scan: workbench_catalog.CatalogScan
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """The publishable run rows, how many were lost, and whether capped."""
        projected: list[dict[str, Any]] = []
        failed = 0
        for summary, record in zip(scan.summaries, scan.catalog.records, strict=True):
            if summary.primary_state not in ACTIVE_PRIMARY_STATES:
                continue
            try:
                row = bridge_projection.project_row(summary, state_dir=self.state_dir)
            except OSError, ValueError:
                # The plan was classified a moment ago and is unreadable now.
                # One lost row must not cost the operator the other rows, and
                # the count is what stops the snapshot claiming to be whole.
                failed += 1
                continue
            published = project_run(row, entry=record.entry)
            if published is None:
                failed += 1
                continue
            published["urgency_rank"] = row.get("urgency_rank")
            projected.append(published)
        projected.sort(key=_selection_key)
        truncated = len(projected) > MAX_PUBLISHED_RUNS
        kept = projected[:MAX_PUBLISHED_RUNS]
        kept.sort(key=_display_key)
        # ``urgency_rank`` exists to order this list and is not a field the run
        # shape declares, so it comes off before the rows are handed on.
        for row in kept:
            row.pop("urgency_rank", None)
        return kept, failed, truncated
