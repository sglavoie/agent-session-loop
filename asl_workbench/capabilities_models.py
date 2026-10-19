"""The capability/ownership matrix for the unified Workbench.

The consolidation's primary acceptance criterion is that one implementation
survives it. A plan that says so is not enough: two applications that both
work are the failure mode, and they fail by *nobody having written down* which
one owns a behavior. So every capability either half has today gets a row
here, and every row names four things:

* **owner** -- the single place the behavior lives once the consolidation is
  done. Exactly one, from :data:`OWNERS`.
* **authority** -- who remains the source of truth behind it. ASL stays the
  authority on whether a mutation is permitted and what actually happened, so
  a row owned by the portable application usually still answers
  ``agent_session_loop`` here. Ownership and authority are different
  questions, and collapsing them is how an adapter grows into a second
  implementation.
* **compatibility_adapters** -- what must keep translating an old request into
  the owner's interface. An adapter may translate; it may not reimplement.
* **regression_coverage** -- the tests that hold the behavior today, as
  ``alias:path`` references into the registered repositories, plus an explicit
  ``coverage_gap`` where the only thing holding it is a documented manual
  check. An unnamed gap is the one this program cannot afford.

Rows for the Workbench's HTTP routes and CLI entry points are *derived*
against the live code by ``tests/test_workbench_foundations.py``: the route
table in ``agent_session_loop/workbench_server.py`` and the argument parser in
``agent_session_loop/cli.py`` are read, and a surface without a row fails.
Browser workflows, integrations, and persistence boundaries are enumerated
rather than derived -- there is nothing mechanical to read them off. Their
historical origin remains recorded, but the retired Companion modules, routes,
dashboard, fixtures, and tests are deliberately absent from this final matrix.

``python3 -m asl_workbench`` renders the matrix as Markdown; ``--json`` prints
the counts and declared gaps as data instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .operations import OPERATION_METHODS

REPO_ROOT = Path(__file__).resolve().parent.parent

# -- Vocabularies -----------------------------------------------------------

#: The surfaces a capability can live on. The split is by *what a caller
#: touches*, because that is what a compatibility promise is made about: a URL,
#: a command line, a screen, a frame on the bridge, a file on disk.
SURFACE_WORKBENCH_HTTP = "workbench-http"
SURFACE_WORKBENCH_STATIC = "workbench-static"
SURFACE_WORKBENCH_CLI = "workbench-cli"
SURFACE_WORKBENCH_BROWSER = "workbench-browser"
SURFACE_BRIDGE = "bridge"
SURFACE_INTEGRATION = "integration"
SURFACE_PERSISTENCE = "persistence"

SURFACES: tuple[str, ...] = (
    SURFACE_WORKBENCH_HTTP,
    SURFACE_WORKBENCH_STATIC,
    SURFACE_WORKBENCH_CLI,
    SURFACE_WORKBENCH_BROWSER,
    SURFACE_BRIDGE,
    SURFACE_INTEGRATION,
    SURFACE_PERSISTENCE,
)

#: Where the capability exists today.
ORIGIN_WORKBENCH = "workbench"
ORIGIN_COMPANION = "companion"
ORIGIN_BOTH = "both"

ORIGINS: tuple[str, ...] = (ORIGIN_WORKBENCH, ORIGIN_COMPANION, ORIGIN_BOTH)

#: The sole behavior owner after consolidation. There is no "undecided": a row
#: that cannot name one of these is a row this session has not finished.
OWNER_PORTABLE = "asl_workbench"
OWNER_MAC = "agent_session_loop"
OWNER_PI_REPO = "pi-deployment"

OWNERS: tuple[str, ...] = (OWNER_PORTABLE, OWNER_MAC, OWNER_PI_REPO)

#: Who stays the source of truth behind the owner.
AUTHORITY_ASL = "agent_session_loop"
AUTHORITY_PI_SERVICE = "pi-service"
AUTHORITY_HOST = "deployed-host"
AUTHORITY_NONE = "none"

AUTHORITIES: tuple[str, ...] = (
    AUTHORITY_ASL,
    AUTHORITY_PI_SERVICE,
    AUTHORITY_HOST,
    AUTHORITY_NONE,
)

#: The repository aliases a coverage reference may name, as registered by the
#: plan. A reference is ``alias:path`` so it reads the same in a handoff.
COVERAGE_ALIASES: tuple[str, ...] = ("scripts",)

#: The final behavior owner for every mutation method. ASL remains the
#: authority on permission and outcome, while the portable operation layer is
#: the single owner of admission, identity, serialization, and reconciliation.
OPERATION_BEHAVIOR_OWNERS: Mapping[str, str] = {
    method: OWNER_PORTABLE for method in OPERATION_METHODS
}

#: Coverage references that resolve inside this repository are checked against
#: the filesystem; the prefix below is how they are recognised.
LOCAL_COVERAGE_ALIAS = "scripts"

#: Browser rows the Playwright shell suite does not reach. It drives the
#: queue, the responsive shell, the detail panes and both deep-link formats
#: against both deployments; everything else on this surface is still gated by
#: the documented smoke checklist that the ``tests/test_asl_docs_*.py`` modules
#: keep in step with the payloads. A row carrying this reason is saying which
#: of the two it is, rather than claiming coverage it lacks.
GAP_WORKBENCH_BROWSER = (
    "not covered by the Playwright shell suite; only the documented smoke checklist"
)
GAP_DEPLOYED_HOST = "verified on the deployed host only; no automated gate"


class MatrixError(Exception):
    """Raised when the matrix is asked about a capability it does not carry."""


@dataclass(frozen=True)
class Capability:
    """One capability of the combined application, and who owns it."""

    key: str
    surface: str
    summary: str
    origin: str
    owner: str
    authority: str
    compatibility_adapters: tuple[str, ...] = ()
    regression_coverage: tuple[str, ...] = ()
    coverage_gap: str | None = None
    # A row can sit under more than one deliberate change: the archive listing
    # both mutates on read and empties entries by age, which are two separate
    # decisions with two separate target behaviors.
    intentional_changes: tuple[str, ...] = ()

    @property
    def identifier(self) -> str:
        """The part of the key after the surface prefix."""
        _, _, rest = self.key.partition(":")
        return rest


def _http(
    key: str,
    summary: str,
    *,
    coverage: tuple[str, ...],
    adapters: tuple[str, ...] = (),
    changes: tuple[str, ...] = (),
) -> Capability:
    """A Workbench local-API route: portable shape, ASL authority."""
    return Capability(
        key=f"{SURFACE_WORKBENCH_HTTP}:{key}",
        surface=SURFACE_WORKBENCH_HTTP,
        summary=summary,
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_ASL,
        compatibility_adapters=adapters,
        regression_coverage=coverage,
        intentional_changes=changes,
    )


# Coverage references used often enough to name once.
_SERVER_ARCHIVE_TESTS = "scripts:tests/test_asl_workbench_server_archive.py"
_SERVER_CONTROL_TESTS = "scripts:tests/test_asl_workbench_server_control.py"
_SERVER_HTTP_TESTS = "scripts:tests/test_asl_workbench_server_http.py"
_SERVER_LAUNCH_REPLY_TESTS = "scripts:tests/test_asl_workbench_server_launch_reply.py"
_SERVER_QUESTIONS_TESTS = "scripts:tests/test_asl_workbench_server_questions.py"
_SERVER_QUEUE_TESTS = "scripts:tests/test_asl_workbench_server_queue.py"
_SERVER_RECOVERY_TESTS = "scripts:tests/test_asl_workbench_server_recovery.py"
_SERVER_REGISTRY_TESTS = "scripts:tests/test_asl_workbench_server_registry.py"
_SERVER_RUN_RESULTS_TESTS = "scripts:tests/test_asl_workbench_server_run_results.py"
_SERVER_RUN_STATE_TESTS = "scripts:tests/test_asl_workbench_server_run_state.py"
_ARCHIVE_TESTS = "scripts:tests/test_asl_workbench_archive.py"
_ARCHIVE_SERVICE_TESTS = "scripts:tests/test_workbench_archive.py"
_SUMMARY_TESTS = (
    "scripts:tests/test_asl_workbench_summary.py",
    "scripts:tests/test_asl_workbench_summary_review.py",
    "scripts:tests/test_asl_workbench_summary_completion.py",
    "scripts:tests/test_asl_workbench_summary_active.py",
    "scripts:tests/test_asl_workbench_summary_state.py",
    "scripts:tests/test_asl_workbench_summary_triage.py",
)
_RECOVERY_TESTS = (
    "scripts:tests/test_asl_workbench_recovery.py",
    "scripts:tests/test_asl_workbench_recovery_kinds.py",
    "scripts:tests/test_asl_workbench_recovery_pause.py",
    "scripts:tests/test_asl_workbench_recovery_state.py",
    "scripts:tests/test_asl_workbench_recovery_verification.py",
    "scripts:tests/test_asl_workbench_recovery_failures.py",
)
_REDIRECT_TESTS = "scripts:tests/test_asl_workbench_redirect.py"
_LAUNCH_TESTS = "scripts:tests/test_asl_workbench_launch.py"
_STATIC_SHELL_TESTS = "scripts:tests/test_asl_workbench_static_shell.py"
_STATIC_ASSET_TESTS = "scripts:tests/test_asl_workbench_static_assets.py"
_STATIC_CLEANUP_TESTS = "scripts:tests/test_asl_workbench_hardening_cleanup.py"
_FOUNDATION_TESTS = "scripts:tests/test_workbench_foundations.py"
_CATALOG_TESTS = (
    "scripts:tests/test_workbench_catalog.py",
    "scripts:tests/test_workbench_catalog_observation.py",
    "scripts:tests/test_workbench_catalog_generations.py",
)
_DETAIL_TESTS = "scripts:tests/test_workbench_details.py"
_HISTORY_TESTS = "scripts:tests/test_workbench_history.py"
_OPERATION_TESTS = (
    "scripts:tests/test_workbench_operations.py",
    "scripts:tests/test_workbench_operations_journal.py",
    "scripts:tests/test_workbench_operations_processes.py",
    "scripts:tests/test_workbench_operations_routes.py",
)
_ACTION_TESTS = "scripts:tests/test_workbench_launch_reply.py"
_BRIDGE_TESTS = "scripts:tests/test_asl_bridge_server_capabilities_rows.py"
_DOCS_TESTS = "scripts:tests/test_asl_docs_authoritative_lanes.py"
_CLI_TESTS = "scripts:tests/test_asl_cli_parser_review_guard_and_resume_overlay.py"
_CONTROL_TESTS = (
    "scripts:tests/test_workbench_controls.py",
    "scripts:tests/test_workbench_controls_corrections.py",
    "scripts:tests/test_workbench_controls_parity.py",
)
_CONTROL_CLI_TESTS = "scripts:tests/test_asl_control_cli_1.py"
_STORE_TESTS = "scripts:tests/test_workbench_store_ingestion.py"
_TRANSPORT_TESTS = "scripts:tests/test_workbench_transport_contract.py"
_CONNECTOR_TESTS = "scripts:tests/test_workbench_connector_delivery.py"
#: The service's own half of the link: what an upload does to the store, what
#: rides back down in its acknowledgement, and how a read or a control settles
#: when nothing carries it. Named separately from the connector module because
#: the two are the two ends, and a test of one proves nothing about the other.
_UPLINK_TESTS = "scripts:tests/test_workbench_uplink.py"
#: The two ends over an actual socket, which no other module covers: the
#: full-stack module supplies the network itself and says so. Named on the
#: uplink route because that is the surface where "every lane is covered and
#: nothing carries a frame" was true until a deployed rehearsal found it.
_WIRE_TESTS = "scripts:tests/test_workbench_wire.py"
_REMOTE_READ_TESTS = (
    "scripts:tests/test_workbench_remote_reads.py",
    "scripts:tests/test_workbench_remote_reads_lifetime.py",
    "scripts:tests/test_workbench_remote_reads_lanes.py",
    "scripts:tests/test_workbench_remote_reads_responder.py",
    "scripts:tests/test_workbench_remote_reads_paging.py",
)
_REMOTE_ACTION_TESTS = "scripts:tests/test_workbench_remote_actions_performer.py"
_HTTP_TESTS = "scripts:tests/test_workbench_http.py"
#: Packaging and the native lifecycle, split by what each half can prove. The
#: Mac modules drive install, upgrade, rollback, recovery and removal against a
#: release that is genuinely started; the portable module runs under the Pi's
#: own interpreter and holds the artifact to portable code and static assets.
#: Both are named wherever a row is about how the application is deployed.
_LIFECYCLE_TESTS = (
    "scripts:tests/test_workbench_lifecycle.py",
    "scripts:tests/test_workbench_lifecycle_service.py",
    "scripts:tests/test_workbench_lifecycle_connector.py",
)
_PORTABLE_TESTS = "scripts:tests/test_workbench_portable.py"
#: The one-way trip onto this package, and the one way back. Named separately
#: from the lifecycle module because it is the only coverage that starts from
#: an installation this package did not create: what it holds is that existing
#: state arrives intact, that identities somebody has already been shown do not
#: change, and that neither a half-finished migration nor a rollback repeats an
#: action or reinstates a credential.
_MIGRATION_TESTS = "scripts:tests/test_workbench_migration.py"
#: The owned host resources and the publisher that writes to one topic. One
#: module covers both because they are one question: which resources on a
#: shared host belong to this application, and what may they be used for.
_INTEGRATION_TESTS = "scripts:tests/test_workbench_integrations.py"
#: The one front end, asserted from Python and from a real browser. The Python
#: half proves both deployments serve the same assets over one plan corpus; the
#: Playwright half drives that shell at desktop and phone widths against both
#: of them, which is the only place the responsive layout and the keyboard are
#: actually exercised.
_SHELL_TESTS = "scripts:tests/test_workbench_shell.py"
_SHELL_BROWSER_TESTS = "scripts:tests/workbench_browser/shell.spec.js"
#: The overview -- sections, the inactive-work window, grouping, the facets and
#: the history pane -- asserted the same two ways, and for the same reason the
#: shell is: the Python half proves both providers answer one set of questions
#: identically, and the Playwright half proves one page draws both answers.
#: Rows that used to point only at the Pi's dashboard spec name these as well,
#: because that spec now describes an implementation this one replaced.
_OVERVIEW_TESTS = (
    "scripts:tests/test_workbench_overview.py",
    "scripts:tests/test_workbench_overview_selection.py",
    "scripts:tests/test_workbench_overview_parity.py",
    "scripts:tests/test_workbench_overview_catalog.py",
)
_OVERVIEW_BROWSER_TESTS = "scripts:tests/workbench_browser/overview.spec.js"
#: The mutating half of the matrix, asserted the same two ways again. The
#: Python half asks both providers what they will carry out and holds their
#: advance warning against their refusal word for word; the Playwright half is
#: where the sentence reaching the operator, the keyboard surviving a confirm
#: dialog, a recorded-but-unrun operation surviving a reload, and the
#: expired-archive review are actually exercised -- none of which is visible
#: from a request.
_INTERACTION_TESTS = "scripts:tests/test_workbench_interactions.py"
_INTERACTION_BROWSER_TESTS = "scripts:tests/workbench_browser/interactions.spec.js"
#: The whole application at once: one plan corpus, the real observation source,
#: a connector over a link that can be made to fail, the Pi's store, and the
#: remote provider behind a loopback socket, with launch children replaced by an
#: inert interpreter. Named on the rows whose behaviour is only observable with
#: all of it running -- a settlement that has to cross the link twice, a
#: database nobody may find prose in, a ceiling a dead link fills.
_FULLSTACK_TESTS = "scripts:tests/test_workbench_fullstack_corpus_link.py"
