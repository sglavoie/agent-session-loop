"""Capability rows for link."""

from __future__ import annotations

from . import capabilities_models as models

CAPABILITIES: tuple[models.Capability, ...] = (
    models.Capability(
        key=f"{models.SURFACE_BRIDGE}:workbench.ensure",
        surface=models.SURFACE_BRIDGE,
        summary="Start-or-reuse one bridge-owned Workbench and hand back a "
        "tokenized deep link for a plan.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        compatibility_adapters=(
            "the existing bridge protocol is unchanged; the new Workbench "
            "transport is versioned separately",
        ),
        regression_coverage=(models._BRIDGE_TESTS, models._FOUNDATION_TESTS),
    ),
    models.Capability(
        key=f"{models.SURFACE_BRIDGE}:runs.snapshot",
        surface=models.SURFACE_BRIDGE,
        summary="The bounded intervention projection AgentMux and the "
        "Companion connector both read.",
        origin=models.ORIGIN_BOTH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        compatibility_adapters=(
            "the Companion connector consumes this capped projection and "
            "cannot supply full Workbench discovery from it",
        ),
        regression_coverage=(
            "scripts:tests/test_asl_bridge_projection.py",
            models._BRIDGE_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_BRIDGE}:run.reply",
        surface=models.SURFACE_BRIDGE,
        summary="Answer one open question over the bridge, with an operation "
        "identity that survives a reconnect.",
        origin=models.ORIGIN_BOTH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            models._BRIDGE_TESTS,
            models._ACTION_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_BRIDGE}:run.resume",
        surface=models.SURFACE_BRIDGE,
        summary="Spawn a resume for a paused plan with lock authorization "
        "narrowed to that plan.",
        origin=models.ORIGIN_BOTH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            models._BRIDGE_TESTS,
            models._ACTION_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_BRIDGE}:run.control",
        surface=models.SURFACE_BRIDGE,
        summary="Stop-after-current and abort over the bridge, sharing ASL's "
        "control queue and grace.",
        origin=models.ORIGIN_BOTH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            models._BRIDGE_TESTS,
            models._CONTROL_CLI_TESTS,
            models._CONTROL_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_BRIDGE}:redirect",
        surface=models.SURFACE_BRIDGE,
        summary="Submit, acknowledge, and recover mid-run corrections through "
        "the durable redirect journal.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            "scripts:tests/test_asl_bridge_redirect.py",
            models._REDIRECT_TESTS,
            models._CONTROL_TESTS,
        ),
    ),
    models._http(
        "POST /v1/ingest",
        "The paired connector's one upload path, and the only route a "
        "credential rather than an owner identity reaches. Its response body "
        "is the downward half of the link: an upload is acknowledged with "
        "whatever request the service is holding, so a detail read and a "
        "control leave on the next frame the Mac sends without a poll route "
        "or a socket held open.",
        coverage=(
            models._UPLINK_TESTS,
            models._WIRE_TESTS,
            models._HTTP_TESTS,
            models._FULLSTACK_TESTS,
            models._TRANSPORT_TESTS,
        ),
    ),
    models._http(
        "POST /v1/commands/redeem",
        "Exchange a dispatched command token for its bounded in-memory body. "
        "This connector-only compatibility path never persists the prose.",
        coverage=(models._HTTP_TESTS, models._FULLSTACK_TESTS, models._WIRE_TESTS),
    ),
    models._http(
        "GET /v1/health",
        "Liveness at the path the installed monitor already polls; an alias "
        "of GET /api/health, same operation and same public role.",
        coverage=(
            models._HTTP_TESTS,
            models._FOUNDATION_TESTS,
            models._INTEGRATION_TESTS,
        ),
    ),
    models._http(
        "GET /v1/summary",
        "Counts and health at the path the Homepage widget already reads; an "
        "alias of GET /api/summary, same operation and same summary role.",
        coverage=(
            models._HTTP_TESTS,
            models._FOUNDATION_TESTS,
            models._INTEGRATION_TESTS,
        ),
    ),
    models._http(
        "GET /v1/runs",
        "Owner run rows at the path the published site record already names; "
        "an alias of GET /api/queue, same operation and same owner role. The "
        "three aliases exist so cutting production over to this application "
        "is one change to one service rather than four changes to the widget, "
        "the monitor, the site record and the service at once.",
        coverage=(
            models._HTTP_TESTS,
            models._FOUNDATION_TESTS,
            models._INTEGRATION_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:overview-sections",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Needs-attention, running, unconfirmed, recent, and linked-"
        "older sections as the phone overview.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        # The surviving references prove the transferred behaviour section by
        # section and in both deployments rather than only on the phone.
        regression_coverage=(
            models._OVERVIEW_TESTS,
            models._OVERVIEW_BROWSER_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:filters-and-sort",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Project, harness, search, sort order, age-window note, and "
        "clear-filters.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        # Folded into the Workbench's sort modes and URL persistence rather
        # than kept as a second filter bar, so the references cover both
        # halves: the narrowing is applied before the page and counted over the
        # whole match, and every mode the select offers is one a server takes.
        regression_coverage=(
            models._OVERVIEW_TESTS,
            models._OVERVIEW_BROWSER_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:freshness-chip",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Observed-at and freshness chip: what the page is showing and "
        "how old it is.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        regression_coverage=(
            # Including the case the chip exists for: a partial generation
            # leaves rows the newest observation never confirmed, and the page
            # has to say so instead of drawing them as live.
            models._OVERVIEW_TESTS,
            models._OVERVIEW_BROWSER_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:linked-run-deep-link",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Notification deep link opens one run, with a dismissable "
        "older-run warning.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        # Same correction as the banners above: the surviving implementation is
        # the unified shell's router, and it is driven in a browser here -- a
        # '#run=' link resolved against a catalog page and rewritten to the
        # canonical form while still carrying which run it named, which is what
        # lets a run that has gone quiet get its own heading instead of a row
        # among today's work.
        regression_coverage=(
            models._SHELL_BROWSER_TESTS,
            models._OVERVIEW_BROWSER_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:reply-panel",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Phone reply flow: question body, draft, availability, send, "
        "and its refusal messages.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            models._ACTION_TESTS,
            models._INTERACTION_TESTS,
            models._INTERACTION_BROWSER_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:resume-reply",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Resume a paused run from the phone, with the explanation of "
        "what a launch proves.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            models._ACTION_TESTS,
            models._INTERACTION_TESTS,
            models._INTERACTION_BROWSER_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_WORKBENCH_BROWSER}:failure-banners",
        surface=models.SURFACE_WORKBENCH_BROWSER,
        summary="Banners for transport, staleness, and incident conditions the "
        "page must not hide.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_NONE,
        # The surviving banner region is gated from three sides: the freshness
        # and completeness a read reports, the full stack actually losing its
        # link and saying so about itself rather than going quiet, and the
        # browser drawing the one incident state that means "nobody can
        # establish whether this alert landed" -- and drawing nothing for the
        # five that would turn every raised alert into a delivery failure.
        regression_coverage=(
            models._OVERVIEW_TESTS,
            models._FULLSTACK_TESTS,
            models._OVERVIEW_BROWSER_TESTS,
        ),
    ),
)
