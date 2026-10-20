"""Capability rows for shell."""

from __future__ import annotations

from . import capabilities_models as models

CAPABILITIES: tuple[models.Capability, ...] = (
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_STATIC}:GET /",
        surface=models.SURFACE_WORKBENCH_STATIC,
        summary="The single-page shell, served uncached so a reload gets the "
        "current app.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        regression_coverage=(models._STATIC_SHELL_TESTS,),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_STATIC}:GET /static/",
        surface=models.SURFACE_WORKBENCH_STATIC,
        summary="app.js and styles.css with ETag revalidation and a closed MIME map.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        regression_coverage=(models._STATIC_ASSET_TESTS,),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_CLI}:asl workbench",
        surface=models.SURFACE_WORKBENCH_CLI,
        summary="The workbench command group; bare invocation serves, so the "
        "parent form keeps working.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        compatibility_adapters=(
            "cli._normalize_workbench_serve_args folds parent-form flags onto "
            "the serve subcommand",
        ),
        regression_coverage=(models._CLI_TESTS, models._DOCS_TESTS),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_CLI}:asl workbench serve",
        surface=models.SURFACE_WORKBENCH_CLI,
        summary="Serve the shared application locally on loopback with a "
        "bearer token; --open launches a browser.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        compatibility_adapters=(
            "the local provider wires asl_workbench to the Mac services",
        ),
        regression_coverage=(
            models._CLI_TESTS,
            models._SERVER_HTTP_TESTS,
            models._DOCS_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_CLI}:asl workbench connect",
        surface=models.SURFACE_WORKBENCH_CLI,
        summary="Publish heartbeats, the complete catalog, active runs, and "
        "incremental history to a remote service, answer that service's "
        "correlated detail reads, and -- only under --allow-remote-actions -- "
        "perform its mutating commands, all on the same outward link. Calls "
        "the shared catalog, observation, history, detail, action, control, "
        "and archive services in process; no bridge subprocess and no disk "
        "spooling. Capabilities are derived from what was wired, so the "
        "default invocation announces itself observation-only.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        compatibility_adapters=(
            "workbench_connector.project_run narrows a bridge snapshot row "
            "onto the transport's closed run shape",
            "workbench_connector.REMOTE_ACTIONS maps intervention's local "
            "action names onto operations' mutation methods",
            "connector.pause_reason_word folds an unknown pause slug onto "
            "'other', as the Companion's own row projection does",
            "workbench_remote_reads.AslDetailResponder demotes a reader's "
            "undeclared refusal word onto 'not-found' and carries the "
            "original as a warning, so an unknown reason is never silence",
            "remote_actions.reportable_status folds a queue-only status onto "
            "the three the transport lets a connector report, so 'cancelled' "
            "reaches the service as 'rejected' rather than as a refused frame",
            "workbench_remote_actions.bulk_archive_result reassembles the "
            "per-plan outcomes of one-operation-per-plan remote archives into "
            "the same bulk result shape the local route returns",
            "workbench_connector.HttpsBodySource redeems a reply's prose over "
            "a second route because the contract set is pinned and no "
            "envelope may carry it",
        ),
        regression_coverage=(
            models._CONNECTOR_TESTS,
            *models._REMOTE_READ_TESTS,
            models._REMOTE_ACTION_TESTS,
            models._CLI_TESTS,
            models._DOCS_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_CLI}:asl workbench pin",
        surface=models.SURFACE_WORKBENCH_CLI,
        summary="Pin a plan into the registry with a label and launch "
        "profile. Same service as POST /api/registry/pin, so the two refuse "
        "the same inputs for the same reason.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            models._ARCHIVE_SERVICE_TESTS,
            models._CLI_TESTS,
            models._DOCS_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_CLI}:asl workbench unpin",
        surface=models.SURFACE_WORKBENCH_CLI,
        summary="Drop a registry pin.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            models._ARCHIVE_SERVICE_TESTS,
            models._CLI_TESTS,
            models._DOCS_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_CLI}:asl workbench registry",
        surface=models.SURFACE_WORKBENCH_CLI,
        summary="Print the registry, its warnings, and the discovered plan roots.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(models._CLI_TESTS, models._DOCS_TESTS),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_CLI}:asl plans",
        surface=models.SURFACE_WORKBENCH_CLI,
        summary="Terminal plan listing with stale/never-run filters, sharing "
        "the Workbench summary builder.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(models._CLI_TESTS, *models._SUMMARY_TESTS),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:triage-inbox",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Default inbox: urgency grouping, activity labels, filters, "
        "search, and completed-plan suppression.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        # The inbox is now the overview, and the overview suite drives it: the
        # sections it groups into, the chips and facets that narrow it, the
        # search, and the request that hides completed work -- against both
        # deployments, so "the inbox" is one behaviour rather than two.
        regression_coverage=(
            models._DOCS_TESTS,
            *models._SUMMARY_TESTS,
            *models._DETAIL_TESTS,
            *models._OVERVIEW_TESTS,
            models._OVERVIEW_BROWSER_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:detailed-search",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Free-text search over a plan's private detail -- its path, "
        "the operator's pin label, pause prose, last-message preview. An "
        "online read the Mac answers; none of it enters a published entry.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        regression_coverage=(*models._DETAIL_TESTS,),
        # The service behind it is tested; the browser that calls it is not,
        # like every other row on this surface.
        coverage_gap=models.GAP_WORKBENCH_BROWSER,
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:two-pane-shell",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Responsive two-pane shell with the preview pane and its "
        "phone-width collapse.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        # Both shapes are asserted at both widths against both deployments, so
        # the collapse is a tested behaviour rather than a CSS rule nobody has
        # opened a phone to check.
        regression_coverage=(models._DOCS_TESTS, models._SHELL_BROWSER_TESTS),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:row-action-focus",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Row actions move focus to the pane they act on, so keyboard "
        "use survives a re-render.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        regression_coverage=(models._DOCS_TESTS, models._SHELL_BROWSER_TESTS),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:viewport-preservation",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="The five-second poll puts the operator's place back only when "
        "the redraw displaced it, so reading is never interrupted and a tap is "
        "never eaten.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        # A row of its own rather than a clause of the shell's, because the
        # conditional is the capability and it is only observable in a browser.
        # An unconditional restore looks identical in a screenshot and is silent
        # in a log, and on a phone it is a defect: WebKit reads a document
        # scroll between `touchstart` and `touchend` as a pan and throws the tap
        # away, so restoring a scroll to where it already was -- every five
        # seconds, forever -- eats the press of an operator reaching for a
        # control, with no request and no feedback. Both halves are driven at
        # all three widths: a poll that changed nothing performs no scroll at
        # all, and a poll that shortened the page still puts the operator back.
        regression_coverage=(models._STATIC_SHELL_TESTS, models._SHELL_BROWSER_TESTS),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:plan-detail",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Detail view: state, sessions, events, logs, artifacts, "
        "verification, progress, workspace.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        # Every pane is asserted to render either its material or its own empty
        # sentence, for a plan that has been run and one that never has, in
        # both deployments -- a pane that failed silently is the failure mode
        # this replaces.
        regression_coverage=(
            models._DOCS_TESTS,
            models._SERVER_RUN_STATE_TESTS,
            models._SHELL_TESTS,
            models._SHELL_BROWSER_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:controls-bar",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Launch, reply, stop-after-current, abort, harness select, "
        "redirect, unlock, pin, archive from one bar.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_ASL,
        # Every control on the bar is now driven through one guarded path and
        # asserted from both ends: what each mode will carry out, that the
        # sentence under a shut control is the refusal it would have given,
        # that a run that has moved on is named as the reason rather than the
        # mode, and that a recorded-but-unconfirmed submission is never drawn
        # as done. Launch is asserted through its disabled state rather than by
        # pressing it -- a browser suite that started a real run would leave
        # one behind.
        regression_coverage=(
            models._DOCS_TESTS,
            models._SERVER_CONTROL_TESTS,
            models._INTERACTION_TESTS,
            models._INTERACTION_BROWSER_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:recovery-center",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Recovery cards with copyable commands and the security "
        "boundaries on what they may offer.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_ASL,
        # One of the boundaries is now asserted: a recovery card that acts is
        # the same action as the controls bar, so it is shut by the same
        # capability pass rather than refused after the press. The cards
        # themselves are still only reached by the smoke checklist -- the shell
        # fixture's plans diagnose to a card that navigates, so no browser test
        # here can put a mutating card on screen.
        regression_coverage=(
            models._DOCS_TESTS,
            *models._RECOVERY_TESTS,
            models._INTERACTION_TESTS,
        ),
        coverage_gap=models.GAP_WORKBENCH_BROWSER,
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:archive-browser",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Archive listing with bulk select, restore, permanent empty, "
        "the retention control, and per-entry cleanup candidacy. Opening it "
        "deletes nothing.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_ASL,
        # Browsing-deletes-nothing and the two confirmations are asserted
        # against a live archive, and the browser half drives the listing, the
        # bulk bar's two distinct reasons for being shut, and the expired-entry
        # review that now stands in front of the typed count. The purge itself
        # is driven up to that gate and no further: a spec that got past it
        # would be emptying the archive the other specs read.
        regression_coverage=(
            models._DOCS_TESTS,
            models._ARCHIVE_TESTS,
            models._STATIC_CLEANUP_TESTS,
            models._INTERACTION_TESTS,
            models._INTERACTION_BROWSER_TESTS,
        ),
        intentional_changes=("explicit-archive-cleanup",),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:cleanup-review-bar",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Bulk cleanup selection by age threshold, reviewed before it "
        "archives anything.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_ASL,
        # The review is the capability, so the browser half drives the bar to
        # the edge of the archive and stops: the button starts shut, selecting
        # is a read that arms nothing, and on the Pi the mode's reason replaces
        # the selection's rather than the selection unlocking it.
        regression_coverage=(
            models._DOCS_TESTS,
            models._ARCHIVE_TESTS,
            models._INTERACTION_BROWSER_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:conversation-recovery-card",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Conversation recovery card: what was retained and what to run next.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(models._DOCS_TESTS, models._SERVER_RECOVERY_TESTS),
        coverage_gap=models.GAP_WORKBENCH_BROWSER,
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:session-preferences",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Filter, sort, and pane preferences held in session storage "
        "and mirrored into the URL, so a view can be sent as a link.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        # Asserted in both directions, which is the whole claim: a choice made
        # in the controls lands in the address bar, and a link carrying one
        # opens the view it describes rather than the default. The full-stack
        # module holds the privacy half: every key the front end writes is a
        # declared constant under one namespace, spelled out in exactly one
        # module, and no module reaches for a cookie or a database instead.
        regression_coverage=(
            models._DOCS_TESTS,
            models._FULLSTACK_TESTS,
            models._OVERVIEW_BROWSER_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:empty-state-actions",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Empty states offer the action that would end them: clear "
        "search, reset filter and its facets, show completed.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        # Driven through the case that matters now that the narrowing happens
        # on the server: a facet that matches nothing arrives as zero rows, and
        # the page has to say which filter emptied it rather than report an
        # empty machine with nothing to undo.
        regression_coverage=(models._DOCS_TESTS, models._OVERVIEW_BROWSER_TESTS),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:fragment-deep-link-routing",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="One router over both link formats history left behind: the "
        "Workbench's '#plan?dir=|key=&focus=' and the Companion's '#run=', "
        "which is resolved against the catalog and rewritten to the canonical "
        "form.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        regression_coverage=(
            models._BRIDGE_TESTS,
            models._FOUNDATION_TESTS,
            models._SHELL_BROWSER_TESTS,
        ),
    ),
)
