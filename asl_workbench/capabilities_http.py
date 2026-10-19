"""Capability rows for http."""

from __future__ import annotations

from . import capabilities_models as models

CAPABILITIES: tuple[models.Capability, ...] = (
    models._http(
        "GET /api/health",
        "Liveness probe; the one route that needs no bearer token.",
        coverage=(
            models._SERVER_HTTP_TESTS,
            models._FOUNDATION_TESTS,
            models._HTTP_TESTS,
        ),
    ),
    models._http(
        "GET /api/capabilities",
        "What this deployment can do: every operation with its role and, when "
        "the provider cannot serve it, the reason -- so the front end can "
        "explain an unavailable action instead of failing at the click.",
        coverage=(models._HTTP_TESTS, models._FOUNDATION_TESTS),
    ),
    models._http(
        "GET /api/summary",
        "Counts and health only, readable by a summary-scoped caller: the "
        "narrow read a status pane needs without owner-scoped plan detail.",
        coverage=(models._HTTP_TESTS, models._FOUNDATION_TESTS),
    ),
    models._http(
        "GET /api/queue",
        "Triage queue: one non-reclaiming catalog scan, per-plan summaries, "
        "urgency, warnings, state root, catalog generation. Runs no retention. "
        "Narrows by title, project, harness and state before it pages, and "
        "answers with the counts, facets and matched total of the whole "
        "filtered set rather than of the page -- so '12 of 340' is a fact the "
        "server knows and the browser could not compute.",
        coverage=(
            models._SERVER_QUEUE_TESTS,
            *models._SUMMARY_TESTS,
            models._FOUNDATION_TESTS,
            models._CATALOG_TESTS,
            *models._OVERVIEW_TESTS,
        ),
        changes=("observational-reads",),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_HTTP}:GET /api/history",
        surface=models.SURFACE_WORKBENCH_HTTP,
        summary="Runs that ended inside the retention window, newest first, "
        "narrowed by the same words and outcomes the queue is. The Companion's "
        "/v1/history answered from one machine's store; this answers from "
        "whichever provider is mounted, over the same interpretation of ASL's "
        "event journals.",
        # Companion-origin, unlike every other route on this surface: the
        # Workbench never had a history pane. Recording it as Workbench-origin
        # would lose the fact that there is a Pi implementation this one has to
        # stay compatible with.
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            models._HISTORY_TESTS,
            *models._OVERVIEW_TESTS,
            models._OVERVIEW_BROWSER_TESTS,
        ),
    ),
    models._http(
        "GET /api/plan",
        "One plan's triage summary, for detail and deep-link entry.",
        coverage=(
            models._SERVER_QUEUE_TESTS,
            *models._SUMMARY_TESTS,
            models._CATALOG_TESTS,
            models._DETAIL_TESTS,
        ),
        changes=("observational-reads",),
    ),
    models._http(
        "GET /api/recovery",
        "Recovery center: evidence rows, artifact references, action cards.",
        coverage=(
            *models._RECOVERY_TESTS,
            models._SERVER_RECOVERY_TESTS,
            models._DETAIL_TESTS,
        ),
        changes=("observational-reads",),
    ),
    models._http(
        "GET /api/registry",
        "Registry projection: pins, launch profiles, plan roots, retention.",
        coverage=(models._SERVER_REGISTRY_TESTS,),
    ),
    models._http(
        "GET /api/artifacts",
        "Artifact descriptors for a plan's turn outputs. The listing is also "
        "the request vocabulary: a named artifact is what a detail read asks "
        "for.",
        coverage=(models._SERVER_HTTP_TESTS, models._DETAIL_TESTS),
    ),
    models._http(
        "GET /api/state",
        "The plan's ASL state payload, including the session table.",
        coverage=(
            models._SERVER_RUN_STATE_TESTS,
            models._DOCS_TESTS,
            models._DETAIL_TESTS,
        ),
        changes=("observational-reads",),
    ),
    models._http(
        "GET /api/events",
        "Bounded window over the plan's append-only event journal.",
        coverage=(models._SERVER_RUN_STATE_TESTS, models._DETAIL_TESTS),
    ),
    models._http(
        "GET /api/logs",
        "Bounded stdout/stderr window, transparently reading gzip archives. "
        "Reaches only artifacts the selected plan published, or a path that "
        "plan contains.",
        coverage=(models._SERVER_RUN_STATE_TESTS, models._DETAIL_TESTS),
    ),
    models._http(
        "GET /api/last-message",
        "Truncated preview of the harness's last message.",
        coverage=(models._SERVER_RUN_RESULTS_TESTS, models._DETAIL_TESTS),
    ),
    models._http(
        "GET /api/result",
        "The turn result artifact for the plan's latest run.",
        coverage=(models._SERVER_RUN_RESULTS_TESTS, models._DETAIL_TESTS),
    ),
    models._http(
        "GET /api/verification",
        "Verification artifact and the active check's position.",
        coverage=(models._SERVER_RUN_RESULTS_TESTS, models._DETAIL_TESTS),
        changes=("observational-reads",),
    ),
    models._http(
        "GET /api/commits",
        "Commit rows recorded for the plan's sessions.",
        coverage=(models._SERVER_RUN_RESULTS_TESTS, models._DETAIL_TESTS),
    ),
    models._http(
        "GET /api/progress",
        "Progress-journal entries parsed from the plan directory.",
        coverage=(models._SERVER_RUN_RESULTS_TESTS, models._DETAIL_TESTS),
    ),
    models._http(
        "GET /api/final-review",
        "final-review.md and final-review.json for a completed plan.",
        coverage=(models._SERVER_RUN_RESULTS_TESTS, models._DETAIL_TESTS),
    ),
    models._http(
        "GET /api/questions",
        "Open ASL questions awaiting an operator reply.",
        coverage=(
            models._SERVER_QUESTIONS_TESTS,
            "scripts:tests/test_asl_reply_flows_e2e_live_resolution.py",
            models._DETAIL_TESTS,
        ),
        changes=("observational-reads",),
    ),
    models._http(
        "GET /api/redirect-status",
        "Redirect journal projection: accepted, pending, stranded corrections.",
        coverage=(models._REDIRECT_TESTS, *models._CONTROL_TESTS),
    ),
    models._http(
        "GET /api/conversation-recovery",
        "Conversation recovery card: retained facts and the commands to run.",
        coverage=(
            models._SERVER_RECOVERY_TESTS,
            models._DOCS_TESTS,
            *models._CONTROL_TESTS,
        ),
    ),
    models._http(
        "GET /api/archive",
        "Archive listing, retention setting, and which entries retention has "
        "marked as cleanup candidates. Removes nothing.",
        coverage=(
            models._ARCHIVE_TESTS,
            models._ARCHIVE_SERVICE_TESTS,
            models._SERVER_ARCHIVE_TESTS,
            models._FOUNDATION_TESTS,
        ),
        # Both changes landed here: the read stopped mutating, and what
        # retention does when an entry expires stopped being a deletion.
        changes=("observational-reads", "explicit-archive-cleanup"),
    ),
    models._http(
        "POST /api/registry/pin",
        "Pin a plan with an optional label and launch profile. Serialized "
        "against every other registry writer by the registry lock.",
        coverage=(
            models._ARCHIVE_SERVICE_TESTS,
            models._SERVER_REGISTRY_TESTS,
            models._CLI_TESTS,
        ),
    ),
    models._http(
        "POST /api/registry/unpin",
        "Remove a registry pin without touching the plan.",
        coverage=(
            models._ARCHIVE_SERVICE_TESTS,
            models._SERVER_REGISTRY_TESTS,
            models._CLI_TESTS,
        ),
    ),
    models._http(
        "POST /api/plan/archive",
        "Archive one plan; requires confirm_plan_dir to match after canonicalization.",
        coverage=(
            models._ARCHIVE_TESTS,
            models._ARCHIVE_SERVICE_TESTS,
            models._SERVER_ARCHIVE_TESTS,
        ),
    ),
    models._http(
        "POST /api/plans/archive",
        "Bulk archive from the cleanup review bar, reporting per-plan results.",
        coverage=(
            models._ARCHIVE_TESTS,
            models._ARCHIVE_SERVICE_TESTS,
            models._SERVER_ARCHIVE_TESTS,
        ),
    ),
    models._http(
        "POST /api/plan/delete",
        "Historic delete route, kept as an archive alias with its original "
        "confirmation contract.",
        coverage=(
            models._ARCHIVE_TESTS,
            models._ARCHIVE_SERVICE_TESTS,
            models._SERVER_ARCHIVE_TESTS,
        ),
        adapters=("route alias onto POST /api/plan/archive",),
    ),
    models._http(
        "POST /api/archive/restore",
        "Restore an archived plan and its state back into place; refuses "
        "rather than overwriting an occupied destination.",
        coverage=(
            models._ARCHIVE_TESTS,
            models._ARCHIVE_SERVICE_TESTS,
            models._SERVER_ARCHIVE_TESTS,
        ),
    ),
    models._http(
        "POST /api/archive/delete",
        "Permanently empty archive entries; requires confirm_count to match "
        "the archive_ids supplied. The only route that destroys one.",
        coverage=(
            models._ARCHIVE_TESTS,
            models._ARCHIVE_SERVICE_TESTS,
            models._SERVER_ARCHIVE_TESTS,
            models._FOUNDATION_TESTS,
        ),
        changes=("explicit-archive-cleanup",),
    ),
    models._http(
        "POST /api/archive/settings",
        "Set the archive retention window: the age at which an entry "
        "becomes a cleanup candidate, not the age at which it is removed.",
        coverage=(
            models._ARCHIVE_TESTS,
            models._ARCHIVE_SERVICE_TESTS,
            models._SERVER_ARCHIVE_TESTS,
        ),
        changes=("explicit-archive-cleanup",),
    ),
    models._http(
        "POST /api/launch",
        "Start a run for a plan under a launch profile, keyed for idempotency.",
        coverage=(
            models._LAUNCH_TESTS,
            models._SERVER_LAUNCH_REPLY_TESTS,
            models._DOCS_TESTS,
            *models._OPERATION_TESTS,
            models._ACTION_TESTS,
        ),
    ),
    models._http(
        "POST /api/reply",
        "Answer an open ASL question with reply_text, streamed to the child.",
        coverage=(
            models._SERVER_LAUNCH_REPLY_TESTS,
            "scripts:tests/test_asl_reply_flows_e2e_reply_resume.py",
            *models._OPERATION_TESTS,
            models._ACTION_TESTS,
        ),
    ),
    models._http(
        "POST /api/question-reply",
        "Answer the exact live mailbox question without restarting its run; "
        "the remote provider stages prose outside the durable operation row.",
        coverage=(
            models._ACTION_TESTS,
            models._HTTP_TESTS,
            models._INTERACTION_BROWSER_TESTS,
        ),
    ),
    models._http(
        "POST /api/control/stop-after-current",
        "Queue a stop that lands after the in-flight turn settles.",
        coverage=(
            models._SERVER_CONTROL_TESTS,
            models._CONTROL_CLI_TESTS,
            *models._CONTROL_TESTS,
        ),
    ),
    models._http(
        "POST /api/control/abort",
        "Abort the in-flight turn under ASL's default grace.",
        coverage=(
            models._SERVER_CONTROL_TESTS,
            models._CONTROL_CLI_TESTS,
            *models._CONTROL_TESTS,
        ),
    ),
    models._http(
        "POST /api/control/select-harness",
        "Choose the harness a paused run resumes under.",
        coverage=(
            models._SERVER_CONTROL_TESTS,
            models._CONTROL_CLI_TESTS,
            *models._CONTROL_TESTS,
        ),
    ),
    models._http(
        "POST /api/control/redirect",
        "Submit a mid-run correction and wait a bounded time for its verdict.",
        coverage=(models._REDIRECT_TESTS, models._DOCS_TESTS, *models._CONTROL_TESTS),
    ),
    models._http(
        "POST /api/unlock",
        "Release a plan lock the operator has proven dead. The one place "
        "reclaim is still deliberate, now that no read does it.",
        coverage=(
            models._ARCHIVE_SERVICE_TESTS,
            models._SERVER_CONTROL_TESTS,
            *models._RECOVERY_TESTS,
        ),
    ),
)
