import hashlib
import json

from .workbench_local_core import *


def _catalog_revision(scan: workbench_catalog.CatalogScan) -> str:
    """Stable identity for the plans one scan found, across paged reads.

    The remote provider names its catalog by connector generation; the Mac has
    no upload to name, so it digests what a selection depends on: which plans
    exist, which run each is on, what state each is in, and whether the scan
    was complete. Activity timestamps are left out on purpose: a running plan
    moves every few seconds, and a revision that moved with it would discard
    a cleanup selection on every poll for a change that selection never
    covered (running plans are not archivable).
    """
    material = [
        [
            record.entry.plan_key,
            record.entry.run_key,
            record.entry.primary_state,
            record.entry.completed_at,
        ]
        for record in scan.catalog.records
    ]
    encoded = json.dumps(
        [scan.catalog.complete, material], separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


class _ReadOperations(_LocalProviderOperations):
    def _registry(self) -> workbench_registry_models.WorkbenchRegistry:
        try:
            return workbench_registry.load_registry(self.config.registry_path)
        except workbench_registry_models.WorkbenchError as error:
            raise HttpRefusal(500, "registry-unreadable", str(error)) from error

    def _open_plan(self, call: Call) -> workbench_details.PlanReader:
        """Open the requested plan for one detail read, reclaiming nothing.

        The single door every detail route goes through. Classification happens
        once per request and produces the scope that decides which files that
        request may reach, so no handler below resolves a path of its own.
        """
        return workbench_details.open_plan(
            call.require_query("plan_dir"), state_dir=self.config.state_dir
        )

    def _handle_health(self, call: Call) -> Response:
        del call
        return Response.success(reason="ok", status="ok")

    def _scan(
        self, registry: workbench_registry_models.WorkbenchRegistry
    ) -> workbench_catalog.CatalogScan:
        # One non-reclaiming catalog scan answers the queue. Polling the queue
        # no longer empties expired archive entries or reclaims plan locks: a
        # read that repairs makes the answer a function of who looked and when.
        # Retention now runs only where an operator asked for it, on the
        # archive route; `observational-reads` in `asl_workbench.changes`
        # records the decision.
        return workbench_catalog.scan(
            state_dir=self.config.state_dir,
            registry=registry,
            plan_roots=self.config.plan_roots,
            include_default_dirs=self.config.include_default_dirs,
        )

    def _handle_summary(self, call: Call) -> Response:
        """Counts and freshness, and deliberately nothing else.

        The summary role exists so a caller who may not read a plan can still
        see that there are plans and whether the view is current. Every field
        below is a number or a flag; adding a title here would make the role a
        weaker owner rather than a different one.
        """
        del call
        scan = self._scan(self._registry())
        by_state: dict[str, int] = {}
        for summary in scan.summaries:
            by_state[summary.primary_state] = by_state.get(summary.primary_state, 0) + 1
        return Response.success(
            reason="ok",
            counts={"plans": len(scan.summaries), "by_state": by_state},
            freshness={
                "generation": scan.catalog.generation,
                "complete": scan.catalog.complete,
                "total": scan.catalog.total,
                # The Mac reads the filesystem it is asked about, so the answer
                # is as of now and there is no observation to be stale.
                "stale": False,
                "observed_at": None,
            },
        )

    def _handle_queue(self, call: Call) -> Response:
        """Every visible plan, narrowed and counted before it is paged.

        The Mac used to answer with the whole corpus and let the browser hide
        what did not match. That reads correctly right up until the catalog
        outgrows one page, at which point the browser is filtering a page and
        counting a page while describing a corpus. So the narrowing happens
        here now, in the same order the Pi's SQL does it --
        :func:`~asl_workbench.overview_selection.select` is that order, shared --
        and the page carries the size of the set it was cut from.
        """
        registry = self._registry()
        state_root = state.state_root(self.config.state_dir)
        scan = self._scan(registry)
        filters = overview_filters.parse_filters(call)
        # The moment this answer describes, published because the eight-hour
        # window is measured against it. A browser reading its own clock would
        # measure a phone's idea of now against a laptop's idea of when a run
        # last moved, and the two are not the same number.
        observed_at = time.time()
        try:
            page = overview_selection.select(_queue_rows(scan), filters)
        except overview_vocabulary.OverviewError as error:
            raise invalid_request(str(error)) from error
        return Response.success(
            reason="ok",
            plans=list(page.rows),
            observed_at=observed_at,
            registry_warnings=list(registry.warnings),
            discovery_warnings=list(scan.discovery.warnings),
            catalog_warnings=list(scan.projection_warnings),
            catalog_generation=scan.catalog.generation,
            # The identity a multi-page read checks every page against, in the
            # remote provider's shape: without it a cleanup selection could not
            # tell page two of this catalog from page two of a later one.
            catalog_revision=_catalog_revision(scan),
            catalog_complete=scan.catalog.complete,
            # The same fact in the shared vocabulary. A scan that did not cover
            # every root covered part of the catalog; the Mac is never in the
            # third case, because a filesystem it can read is a filesystem that
            # answers how much of itself it just showed.
            catalog_completeness=(
                observation.COMPLETENESS_COMPLETE
                if scan.catalog.complete
                else observation.COMPLETENESS_PARTIAL
            ),
            filters=filters.as_document(),
            # The Mac is standing on the plans it is describing, so every row
            # it publishes is current by construction. Saying so explicitly is
            # what lets one front end mark a *retained* row as retained
            # without having to know which provider answered.
            observation_status=overview_vocabulary.OBSERVATION_CURRENT,
            freshness=freshness.FRESHNESS_LIVE,
            # No connector, no incident engine: these two are the Pi's record
            # of what it could not confirm, and the honest local answer is
            # "there is no such thing here" rather than a missing key.
            connectors=[],
            incidents=[],
            state_root=str(state_root),
            state_root_exists=state_root.is_dir(),
            archive_retention_days=registry.archive_retention_days,
            **page.as_document(),
        )

    def _handle_history(self, call: Call) -> Response:
        """Runs that ended inside the retention window, newest first.

        Read from the event files themselves rather than from the publication
        the connector uploads, and read without advancing the connector's
        checkpoint: a pane that consumed endings would be a pane that stopped
        the Pi from ever seeing them.
        """
        filters = overview_filters.parse_filters(call)
        outcome = call.optional_query("outcome")
        view = workbench_history.read_window(
            now=time.time(), state_dir=self.config.state_dir
        )
        labels = _plan_labels(self._scan(self._registry()))
        entries: list[_JSONDict] = []
        # ``view.entries`` arrives in the order the event files were read, so
        # the newest-first promise is this provider's to keep. Sorted before
        # the limit, or the limit would keep the oldest tail of the window.
        for entry in _history_newest_first(view.entries):
            record = _history_record(entry, labels)
            if outcome is not None and record.get("outcome") != outcome:
                continue
            if not overview_filters.matches(record, filters, states=False):
                continue
            entries.append(observation.export_history_entry(record))
            if len(entries) >= filters.limit:
                break
        return Response.success(
            reason="ok",
            entries=entries,
            observed_at=view.window_end,
            complete=view.complete,
            incidents=[],
            warnings=list(view.warnings),
        )

    def _handle_plan(self, call: Call) -> Response:
        plan_dir = call.require_query("plan_dir")
        registry = self._registry()
        return Response.success(
            reason="ok",
            **workbench_details.read_plan(
                plan_dir, registry=registry, state_dir=self.config.state_dir
            ),
        )

    def _handle_recovery(self, call: Call) -> Response:
        return Response.success(
            reason="ok",
            **workbench_details.read_recovery(
                call.require_query("plan_dir"), state_dir=self.config.state_dir
            ),
        )

    def _handle_registry(self, call: Call) -> Response:
        del call
        return Response.success(reason="ok", registry=self._registry().to_payload())

    def _handle_artifacts(self, call: Call) -> Response:
        return Response.success(
            reason="ok", **workbench_details.read_artifacts(self._open_plan(call))
        )

    def _handle_state(self, call: Call) -> Response:
        return Response.success(
            reason="ok", **workbench_details.read_state(self._open_plan(call))
        )

    def _handle_events(self, call: Call) -> Response:
        reader = self._open_plan(call)
        return Response.success(
            reason="ok",
            **workbench_details.read_events(
                reader,
                offset=call.non_negative_int("offset", default=0),
                limit=call.bounded_int(
                    "limit",
                    default=detail_rules.DEFAULT_EVENT_LIMIT,
                    maximum=detail_rules.MAX_EVENT_LIMIT,
                ),
                run_id=call.optional_query("run_id"),
            ),
        )

    def _handle_logs(self, call: Call) -> Response:
        reader = self._open_plan(call)
        # `artifact` is the named form; `stream` is its two-value alias and
        # `path` the compatibility form. Which of the three is acceptable, and
        # what each resolves to, is decided by the scope rather than here --
        # every one of them lands inside the selected plan or is refused.
        return Response.success(
            reason="ok",
            **workbench_details.read_logs(
                reader,
                stream=call.optional_query("stream"),
                artifact=call.optional_query("artifact"),
                path=call.optional_query("path"),
                window=Window(
                    offset=call.optional_int_query("offset"),
                    tail_lines=call.optional_int_query("tail_lines"),
                    max_bytes=call.bounded_int(
                        "max_bytes",
                        default=detail_rules.DEFAULT_LOG_BYTES,
                        maximum=detail_rules.MAX_READ_BYTES,
                    ),
                ),
            ),
        )

    def _handle_last_message(self, call: Call) -> Response:
        reader = self._open_plan(call)
        return Response.success(
            reason="ok",
            **workbench_details.read_last_message(
                reader,
                max_bytes=call.bounded_int(
                    "max_bytes",
                    default=detail_rules.DEFAULT_LAST_MESSAGE_BYTES,
                    maximum=detail_rules.MAX_READ_BYTES,
                ),
            ),
        )

    def _handle_result(self, call: Call) -> Response:
        return Response.success(
            reason="ok", **workbench_details.read_result(self._open_plan(call))
        )

    def _handle_verification(self, call: Call) -> Response:
        return Response.success(
            reason="ok", **workbench_details.read_verification(self._open_plan(call))
        )

    def _handle_commits(self, call: Call) -> Response:
        reader = self._open_plan(call)
        return Response.success(
            reason="ok",
            **workbench_details.read_commits(
                reader, run_id=call.optional_query("run_id")
            ),
        )

    def _handle_progress(self, call: Call) -> Response:
        return Response.success(
            reason="ok", **workbench_details.read_progress(self._open_plan(call))
        )

    def _handle_final_review(self, call: Call) -> Response:
        reader = self._open_plan(call)
        return Response.success(
            reason="ok",
            **workbench_details.read_final_review(
                reader,
                max_bytes=call.bounded_int(
                    "max_bytes",
                    default=detail_rules.DEFAULT_FINAL_REVIEW_BYTES,
                    maximum=detail_rules.MAX_READ_BYTES,
                ),
            ),
        )

    def _handle_questions(self, call: Call) -> Response:
        return Response.success(
            reason="ok",
            **workbench_details.read_questions(
                call.require_query("plan_dir"),
                question_id=call.optional_query("question_id"),
                state_dir=self.config.state_dir,
            ),
        )

    def _handle_archive_list(self, call: Call) -> Response:
        """Report the archive and what retention makes of it. Delete nothing.

        This route used to run a maintenance pass first, so opening the archive
        tab was the act that permanently emptied expired entries -- an operator
        who went looking for something old destroyed it by looking. Now the
        retention window only marks candidates, and emptying them is
        ``POST /api/archive/delete`` with a typed count.
        """
        del call
        view = workbench_archive.review_archive(
            state_dir=self.config.state_dir, registry_path=self.config.registry_path
        )
        return Response.success(reason="ok", **view.to_payload())

    # -- Registry and archive mutations -------------------------------------

    def _handle_registry_pin(self, call: Call) -> Response:
        update = workbench_archive.pin_plan(
            call.require_string("plan_dir"),
            registry_path=self.config.registry_path,
            label=call.optional_string("label"),
            profile=coerce_profile_payload(call.body.get("profile")),
        )
        return Response.success(reason="ok", **update.to_payload())

    def _handle_registry_unpin(self, call: Call) -> Response:
        update = workbench_archive.unpin_plan(
            call.require_string("plan_dir"), registry_path=self.config.registry_path
        )
        return Response.success(reason="ok", **update.to_payload())

    def _handle_plan_archive(self, call: Call) -> Response:
        result = workbench_archive.archive_one(
            call.require_string("plan_dir"),
            call.require_string("confirm_plan_dir"),
            state_dir=self.config.state_dir,
            registry_path=self.config.registry_path,
            plan_roots=self.config.plan_roots,
            include_default_dirs=self.config.include_default_dirs,
        )
        return Response.success(reason="ok", **result.to_payload())

    def _handle_plans_archive(self, call: Call) -> Response:
        bulk = workbench_archive.archive_many(
            call.body.get("plan_dirs"),
            state_dir=self.config.state_dir,
            registry_path=self.config.registry_path,
            plan_roots=self.config.plan_roots,
            include_default_dirs=self.config.include_default_dirs,
        )
        return Response.success(reason="ok", **bulk.to_payload())

    def _handle_archive_restore(self, call: Call) -> Response:
        result = workbench_archive.restore_entry(
            call.require_string("archive_id"),
            state_dir=self.config.state_dir,
            registry_path=self.config.registry_path,
        )
        return Response.success(reason="ok", **result.to_payload())

    def _handle_archive_delete(self, call: Call) -> Response:
        """The one route that destroys, and the only one that ever did."""
        result = workbench_archive.purge_entries(
            call.body.get("archive_ids"),
            call.body.get("confirm_count"),
            state_dir=self.config.state_dir,
        )
        return Response.success(reason="ok", **result.to_payload())

    def _handle_archive_settings(self, call: Call) -> Response:
        update = workbench_archive.set_retention(
            call.body.get("archive_retention_days"),
            registry_path=self.config.registry_path,
        )
        return Response.success(
            reason="ok",
            **update.to_payload(),
            archive_retention_days=update.registry.archive_retention_days,
        )

    def _handle_unlock(self, call: Call) -> Response:
        plan_dir = call.require_string("plan_dir")
        result = workbench_archive.unlock(
            plan_dir,
            state_dir=self.config.state_dir,
            force=bool(call.body.get("force", False)),
        )
        return Response.success(
            reason="ok", **workbench_archive.unlock_payload(plan_dir, result)
        )
