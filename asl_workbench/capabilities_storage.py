"""Capability rows for storage."""

from __future__ import annotations

from . import capabilities_models as models

CAPABILITIES: tuple[models.Capability, ...] = (
    models.Capability(
        key=f"{models.SURFACE_INTEGRATION}:ntfy-notifications",
        surface=models.SURFACE_INTEGRATION,
        summary="Incident notifications with deep links back to the private "
        "dashboard URL.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_PI_SERVICE,
        compatibility_adapters=(
            "existing notification links must keep resolving",
            "only a production deployment may hold the production publisher",
        ),
        regression_coverage=(models._INTEGRATION_TESTS,),
    ),
    models.Capability(
        key=f"{models.SURFACE_INTEGRATION}:uptime-kuma-monitor",
        surface=models.SURFACE_INTEGRATION,
        summary="The single fixed service-health monitor, provisioned over a "
        "stdin-only adapter.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_HOST,
        compatibility_adapters=(
            "a monitor installed under the Companion's name is renamed in "
            "place rather than duplicated",
        ),
        regression_coverage=(models._INTEGRATION_TESTS,),
    ),
    models.Capability(
        key=f"{models.SURFACE_INTEGRATION}:tailscale-serve-route",
        surface=models.SURFACE_INTEGRATION,
        summary="The private URL: one owned route on port 9443 pointing at the "
        "unified service.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_HOST,
        compatibility_adapters=("the existing private URL must not change",),
        regression_coverage=(models._INTEGRATION_TESTS,),
    ),
    models.Capability(
        key=f"{models.SURFACE_INTEGRATION}:homepage-documents",
        surface=models.SURFACE_INTEGRATION,
        summary="Homepage service entries describing the Workbench for the "
        "host's own dashboard, carrying a summary-only credential.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_HOST,
        compatibility_adapters=(
            "an installed card is recognised by its receipt and its former "
            "group heading, so the visible name can change without orphaning it",
        ),
        regression_coverage=(models._INTEGRATION_TESTS,),
    ),
    models.Capability(
        key=f"{models.SURFACE_INTEGRATION}:agentmux-bridge",
        surface=models.SURFACE_INTEGRATION,
        summary="AgentMux's existing bridge protocol, which the new Workbench "
        "transport must not be folded into.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        compatibility_adapters=(
            "the bridge contract stays as it is; new operations get their own "
            "versioned transport",
        ),
        regression_coverage=(
            "scripts:tests/test_asl_agent_mux_contract.py",
            "scripts:tests/test_asl_agent_mux_projection.py",
            "scripts:tests/test_asl_agent_mux_projection_state.py",
            "scripts:tests/test_asl_agent_mux_projection_hooks.py",
            models._TRANSPORT_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_INTEGRATION}:native-service-units",
        surface=models.SURFACE_INTEGRATION,
        summary="The fixed launchd label and systemd unit names the "
        "installation owns, and the two lines that keep the worker "
        "independent: AbandonProcessGroup on the launch agent, so stopping the "
        "connector never kills the runs an operator launched through it, and "
        "KillMode=control-group on the unit, so stopping the service takes its "
        "own threads with it.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_HOST,
        compatibility_adapters=(
            "existing installation prefixes and unit identifiers are preserved",
        ),
        regression_coverage=(*models._LIFECYCLE_TESTS,),
    ),
    models.Capability(
        key=f"{models.SURFACE_PERSISTENCE}:workbench-registry",
        surface=models.SURFACE_PERSISTENCE,
        summary="workbench.json: schema v2 pins, launch profiles, plan roots, "
        "archive retention, under a lock.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(models._SERVER_REGISTRY_TESTS, models._ARCHIVE_TESTS),
    ),
    models.Capability(
        key=f"{models.SURFACE_PERSISTENCE}:asl-plan-state",
        surface=models.SURFACE_PERSISTENCE,
        summary="Per-plan state.json under the ASL state root: the run's "
        "authoritative record.",
        origin=models.ORIGIN_BOTH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            "scripts:tests/test_asl_state_migration.py",
            *models._SUMMARY_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_PERSISTENCE}:asl-event-journal",
        surface=models.SURFACE_PERSISTENCE,
        summary="Append-only events.jsonl per plan, read by both the Workbench "
        "and Companion history import through one forward reader that resumes "
        "only where a checkpoint proves the file is unchanged.",
        origin=models.ORIGIN_BOTH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            models._SERVER_RUN_STATE_TESTS,
            models._HISTORY_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_PERSISTENCE}:asl-plan-lock",
        surface=models.SURFACE_PERSISTENCE,
        summary="Per-plan lock files; catalog and queue reads leave them "
        "alone, and reclaim stays on recovery and unlock.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            *models._SUMMARY_TESTS,
            *models._RECOVERY_TESTS,
            models._FOUNDATION_TESTS,
            *models._CATALOG_TESTS,
        ),
        intentional_changes=("observational-reads",),
    ),
    models.Capability(
        key=f"{models.SURFACE_PERSISTENCE}:workbench-archive",
        surface=models.SURFACE_PERSISTENCE,
        summary="archive/<id>/ holding archive.json plus the plan and state "
        "copies. The retention window marks entries for cleanup; only a "
        "confirmed purge removes one.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            models._ARCHIVE_TESTS,
            models._ARCHIVE_SERVICE_TESTS,
            models._FOUNDATION_TESTS,
        ),
        intentional_changes=("explicit-archive-cleanup",),
    ),
    models.Capability(
        key=f"{models.SURFACE_PERSISTENCE}:redirect-journal",
        surface=models.SURFACE_PERSISTENCE,
        summary="The durable redirect journal: what a correction's fate was, "
        "across a restart of whatever submitted it.",
        origin=models.ORIGIN_WORKBENCH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            "scripts:tests/test_asl_redirect_store.py",
            models._REDIRECT_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_PERSISTENCE}:plan-operation-journal",
        surface=models.SURFACE_PERSISTENCE,
        summary="operations.jsonl beside the redirect journal: which mutation "
        "was admitted, that it was dispatched before anything ran, and how it "
        "was accounted for afterwards. Identifiers only -- no reply text and "
        "no action token.",
        origin=models.ORIGIN_BOTH,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(
            *models._OPERATION_TESTS,
            models._SERVER_ARCHIVE_TESTS,
            models._ACTION_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_PERSISTENCE}:plan-artifacts",
        surface=models.SURFACE_PERSISTENCE,
        summary="Turn artifacts and final-review.md/json in the plan "
        "directory; bodies are fetched on demand and never retained on the Pi.",
        origin=models.ORIGIN_BOTH,
        owner=models.OWNER_MAC,
        authority=models.AUTHORITY_ASL,
        regression_coverage=(models._SERVER_RUN_RESULTS_TESTS,),
    ),
    models.Capability(
        key=f"{models.SURFACE_PERSISTENCE}:companion-sqlite-store",
        surface=models.SURFACE_PERSISTENCE,
        summary="state/companion.sqlite3: cached observations, history, "
        "incidents, commands, operations, and their retention.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_PI_SERVICE,
        # The full-stack module is named here for the two things only a real
        # file can answer: that a database this application actually published
        # into -- and a backup taken of it, and the runtime log beside it --
        # holds no prose and no path, and that republishing the same corpus ten
        # times neither duplicates a row nor grows the file without bound.
        regression_coverage=(
            models._STORE_TESTS,
            models._FULLSTACK_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_PERSISTENCE}:companion-credentials",
        surface=models.SURFACE_PERSISTENCE,
        summary="credentials/secrets.json: the connector, summary, and owner "
        "credentials that must survive the migration unchanged. Minted once by "
        "provisioning and never by packaging; removal keeps them, and the "
        "separate reset that discards observations keeps them too.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_HOST,
        compatibility_adapters=("existing credentials are preserved, not reissued",),
        regression_coverage=(
            models._TRANSPORT_TESTS,
            *models._LIFECYCLE_TESTS,
        ),
    ),
    models.Capability(
        key=f"{models.SURFACE_PERSISTENCE}:companion-runtime-document",
        surface=models.SURFACE_PERSISTENCE,
        summary="config/runtime.json: the installed service's configuration "
        "and role. The role is what decides which half of the deployment a "
        "prefix is, so the lifecycle reads it closed -- an unexpected field is "
        "a configuration written for something else.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_HOST,
        regression_coverage=(*models._LIFECYCLE_TESTS,),
    ),
    models.Capability(
        key=f"{models.SURFACE_PERSISTENCE}:connector-operation-journal",
        surface=models.SURFACE_PERSISTENCE,
        summary="The Mac connector's operation journal: dispatch evidence "
        "written before the mutation is attempted.",
        origin=models.ORIGIN_COMPANION,
        owner=models.OWNER_PORTABLE,
        authority=models.AUTHORITY_ASL,
        # "Before" is the claim, and it is only checkable against a link that
        # can be cut at the moment of dispatch: the full-stack module journals a
        # submission, drops the report on the way back, and asserts the row is
        # left unsettled rather than guessed at -- then restarts the connector
        # and has it close the row out instead of running the mutation twice.
        regression_coverage=(
            *models._OPERATION_TESTS,
            models._STORE_TESTS,
            models._FULLSTACK_TESTS,
        ),
    ),
)
