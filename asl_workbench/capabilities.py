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
The Companion's rows are checked the same way against
:mod:`asl_workbench.companion_inventory` whenever a Pi checkout is reachable.
Browser workflows, integrations, and persistence boundaries are enumerated
rather than derived -- there is nothing mechanical to read them off -- and
their completeness is this session's judgement, recorded so a later session
amends it deliberately.

``python3 -m asl_workbench`` renders the matrix as Markdown; ``--json`` prints
the counts and declared gaps as data instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .compatibility import PACKAGE_NAME

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
SURFACE_COMPANION_HTTP = "companion-http"
SURFACE_COMPANION_MODULE = "companion-module"
SURFACE_COMPANION_BROWSER = "companion-browser"
SURFACE_INTEGRATION = "integration"
SURFACE_PERSISTENCE = "persistence"

SURFACES: tuple[str, ...] = (
    SURFACE_WORKBENCH_HTTP,
    SURFACE_WORKBENCH_STATIC,
    SURFACE_WORKBENCH_CLI,
    SURFACE_WORKBENCH_BROWSER,
    SURFACE_BRIDGE,
    SURFACE_COMPANION_HTTP,
    SURFACE_COMPANION_MODULE,
    SURFACE_COMPANION_BROWSER,
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
COVERAGE_ALIASES: tuple[str, ...] = ("scripts", "sglavoie_raspberry_pi")

#: Coverage references that resolve inside this repository are checked against
#: the filesystem; the prefix below is how they are recognised.
LOCAL_COVERAGE_ALIAS = "scripts"

#: Browser rows the Playwright shell suite does not reach. It drives the
#: queue, the responsive shell, the detail panes and both deep-link formats
#: against both deployments; everything else on this surface is still gated by
#: the documented smoke checklist that ``tests/test_asl_docs.py`` keeps in step
#: with the payloads. A row carrying this reason is saying which of the two it
#: is, rather than claiming coverage it lacks.
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


def _companion_module(
    name: str,
    summary: str,
    *,
    owner: str,
    coverage: tuple[str, ...],
    authority: str = AUTHORITY_PI_SERVICE,
    adapters: tuple[str, ...] = (),
    gap: str | None = None,
) -> Capability:
    return Capability(
        key=f"{SURFACE_COMPANION_MODULE}:{name}",
        surface=SURFACE_COMPANION_MODULE,
        summary=summary,
        origin=ORIGIN_COMPANION,
        owner=owner,
        authority=authority,
        compatibility_adapters=adapters,
        regression_coverage=coverage,
        coverage_gap=gap,
    )


def _companion_http(
    key: str,
    summary: str,
    *,
    coverage: tuple[str, ...],
    owner: str = OWNER_PORTABLE,
) -> Capability:
    return Capability(
        key=f"{SURFACE_COMPANION_HTTP}:{key}",
        surface=SURFACE_COMPANION_HTTP,
        summary=summary,
        origin=ORIGIN_COMPANION,
        owner=owner,
        authority=AUTHORITY_PI_SERVICE,
        regression_coverage=coverage,
    )


# Coverage references used often enough to name once.
_SERVER_TESTS = "scripts:tests/test_asl_workbench_server.py"
_ARCHIVE_TESTS = "scripts:tests/test_asl_workbench_archive.py"
_ARCHIVE_SERVICE_TESTS = "scripts:tests/test_workbench_archive.py"
_SUMMARY_TESTS = "scripts:tests/test_asl_workbench_summary.py"
_RECOVERY_TESTS = "scripts:tests/test_asl_workbench_recovery.py"
_REDIRECT_TESTS = "scripts:tests/test_asl_workbench_redirect.py"
_LAUNCH_TESTS = "scripts:tests/test_asl_workbench_launch.py"
_STATIC_TESTS = "scripts:tests/test_asl_workbench_static.py"
_FOUNDATION_TESTS = "scripts:tests/test_workbench_foundations.py"
_CATALOG_TESTS = "scripts:tests/test_workbench_catalog.py"
_DETAIL_TESTS = "scripts:tests/test_workbench_details.py"
_HISTORY_TESTS = "scripts:tests/test_workbench_history.py"
_OPERATION_TESTS = "scripts:tests/test_workbench_operations.py"
_ACTION_TESTS = "scripts:tests/test_workbench_launch_reply.py"
_BRIDGE_TESTS = "scripts:tests/test_asl_bridge_server.py"
_DOCS_TESTS = "scripts:tests/test_asl_docs.py"
_CLI_TESTS = "scripts:tests/test_asl_cli.py"
_CONTROL_TESTS = "scripts:tests/test_workbench_controls.py"
_STORE_TESTS = "scripts:tests/test_workbench_store.py"
_TRANSPORT_TESTS = "scripts:tests/test_workbench_transport_contract.py"
_CONNECTOR_TESTS = "scripts:tests/test_workbench_connector.py"
_REMOTE_READ_TESTS = "scripts:tests/test_workbench_remote_reads.py"
_REMOTE_ACTION_TESTS = "scripts:tests/test_workbench_remote_actions.py"
_HTTP_TESTS = "scripts:tests/test_workbench_http.py"
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
_OVERVIEW_TESTS = "scripts:tests/test_workbench_overview.py"
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
_PI = "sglavoie_raspberry_pi:services/pi-monitoring"


# -- The matrix -------------------------------------------------------------

#: Every capability of the combined application. Ordered by surface so the
#: rendered matrix reads as an inventory rather than a changelog.
CAPABILITIES: tuple[Capability, ...] = (
    # -- Workbench local API ------------------------------------------------
    _http(
        "GET /api/health",
        "Liveness probe; the one route that needs no bearer token.",
        coverage=(_SERVER_TESTS, _FOUNDATION_TESTS, _HTTP_TESTS),
    ),
    _http(
        "GET /api/capabilities",
        "What this deployment can do: every operation with its role and, when "
        "the provider cannot serve it, the reason -- so the front end can "
        "explain an unavailable action instead of failing at the click.",
        coverage=(_HTTP_TESTS, _FOUNDATION_TESTS),
    ),
    _http(
        "GET /api/summary",
        "Counts and health only, readable by a summary-scoped caller: the "
        "narrow read a status pane needs without owner-scoped plan detail.",
        coverage=(_HTTP_TESTS, _FOUNDATION_TESTS),
    ),
    _http(
        "GET /api/queue",
        "Triage queue: one non-reclaiming catalog scan, per-plan summaries, "
        "urgency, warnings, state root, catalog generation. Runs no retention. "
        "Narrows by title, project, harness and state before it pages, and "
        "answers with the counts, facets and matched total of the whole "
        "filtered set rather than of the page -- so '12 of 340' is a fact the "
        "server knows and the browser could not compute.",
        coverage=(
            _SERVER_TESTS,
            _SUMMARY_TESTS,
            _FOUNDATION_TESTS,
            _CATALOG_TESTS,
            _OVERVIEW_TESTS,
        ),
        changes=("observational-reads",),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_HTTP}:GET /api/history",
        surface=SURFACE_WORKBENCH_HTTP,
        summary="Runs that ended inside the retention window, newest first, "
        "narrowed by the same words and outcomes the queue is. The Companion's "
        "/v1/history answered from one machine's store; this answers from "
        "whichever provider is mounted, over the same interpretation of ASL's "
        "event journals.",
        # Companion-origin, unlike every other route on this surface: the
        # Workbench never had a history pane. Recording it as Workbench-origin
        # would lose the fact that there is a Pi implementation this one has to
        # stay compatible with.
        origin=ORIGIN_COMPANION,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            _HISTORY_TESTS,
            _OVERVIEW_TESTS,
            _OVERVIEW_BROWSER_TESTS,
        ),
    ),
    _http(
        "GET /api/plan",
        "One plan's triage summary, for detail and deep-link entry.",
        coverage=(_SERVER_TESTS, _SUMMARY_TESTS, _CATALOG_TESTS, _DETAIL_TESTS),
        changes=("observational-reads",),
    ),
    _http(
        "GET /api/recovery",
        "Recovery center: evidence rows, artifact references, action cards.",
        coverage=(_RECOVERY_TESTS, _SERVER_TESTS, _DETAIL_TESTS),
        changes=("observational-reads",),
    ),
    _http(
        "GET /api/registry",
        "Registry projection: pins, launch profiles, plan roots, retention.",
        coverage=(_SERVER_TESTS,),
    ),
    _http(
        "GET /api/artifacts",
        "Artifact descriptors for a plan's turn outputs. The listing is also "
        "the request vocabulary: a named artifact is what a detail read asks "
        "for.",
        coverage=(_SERVER_TESTS, _DETAIL_TESTS),
    ),
    _http(
        "GET /api/state",
        "The plan's ASL state payload, including the session table.",
        coverage=(_SERVER_TESTS, _DOCS_TESTS, _DETAIL_TESTS),
        changes=("observational-reads",),
    ),
    _http(
        "GET /api/events",
        "Bounded window over the plan's append-only event journal.",
        coverage=(_SERVER_TESTS, _DETAIL_TESTS),
    ),
    _http(
        "GET /api/logs",
        "Bounded stdout/stderr window, transparently reading gzip archives. "
        "Reaches only artifacts the selected plan published, or a path that "
        "plan contains.",
        coverage=(_SERVER_TESTS, _DETAIL_TESTS),
    ),
    _http(
        "GET /api/last-message",
        "Truncated preview of the harness's last message.",
        coverage=(_SERVER_TESTS, _DETAIL_TESTS),
    ),
    _http(
        "GET /api/result",
        "The turn result artifact for the plan's latest run.",
        coverage=(_SERVER_TESTS, _DETAIL_TESTS),
    ),
    _http(
        "GET /api/verification",
        "Verification artifact and the active check's position.",
        coverage=(_SERVER_TESTS, _DETAIL_TESTS),
        changes=("observational-reads",),
    ),
    _http(
        "GET /api/commits",
        "Commit rows recorded for the plan's sessions.",
        coverage=(_SERVER_TESTS, _DETAIL_TESTS),
    ),
    _http(
        "GET /api/progress",
        "Progress-journal entries parsed from the plan directory.",
        coverage=(_SERVER_TESTS, _DETAIL_TESTS),
    ),
    _http(
        "GET /api/final-review",
        "final-review.md and final-review.json for a completed plan.",
        coverage=(_SERVER_TESTS, _DETAIL_TESTS),
    ),
    _http(
        "GET /api/questions",
        "Open ASL questions awaiting an operator reply.",
        coverage=(
            _SERVER_TESTS,
            "scripts:tests/test_asl_reply_flows.py",
            _DETAIL_TESTS,
        ),
        changes=("observational-reads",),
    ),
    _http(
        "GET /api/redirect-status",
        "Redirect journal projection: accepted, pending, stranded corrections.",
        coverage=(_REDIRECT_TESTS, _CONTROL_TESTS),
    ),
    _http(
        "GET /api/conversation-recovery",
        "Conversation recovery card: retained facts and the commands to run.",
        coverage=(_SERVER_TESTS, _DOCS_TESTS, _CONTROL_TESTS),
    ),
    _http(
        "GET /api/archive",
        "Archive listing, retention setting, and which entries retention has "
        "marked as cleanup candidates. Removes nothing.",
        coverage=(
            _ARCHIVE_TESTS,
            _ARCHIVE_SERVICE_TESTS,
            _SERVER_TESTS,
            _FOUNDATION_TESTS,
        ),
        # Both changes landed here: the read stopped mutating, and what
        # retention does when an entry expires stopped being a deletion.
        changes=("observational-reads", "explicit-archive-cleanup"),
    ),
    _http(
        "POST /api/registry/pin",
        "Pin a plan with an optional label and launch profile. Serialized "
        "against every other registry writer by the registry lock.",
        coverage=(_ARCHIVE_SERVICE_TESTS, _SERVER_TESTS, _CLI_TESTS),
    ),
    _http(
        "POST /api/registry/unpin",
        "Remove a registry pin without touching the plan.",
        coverage=(_ARCHIVE_SERVICE_TESTS, _SERVER_TESTS, _CLI_TESTS),
    ),
    _http(
        "POST /api/plan/archive",
        "Archive one plan; requires confirm_plan_dir to match after canonicalization.",
        coverage=(_ARCHIVE_TESTS, _ARCHIVE_SERVICE_TESTS, _SERVER_TESTS),
    ),
    _http(
        "POST /api/plans/archive",
        "Bulk archive from the cleanup review bar, reporting per-plan results.",
        coverage=(_ARCHIVE_TESTS, _ARCHIVE_SERVICE_TESTS, _SERVER_TESTS),
    ),
    _http(
        "POST /api/plan/delete",
        "Historic delete route, kept as an archive alias with its original "
        "confirmation contract.",
        coverage=(_ARCHIVE_TESTS, _ARCHIVE_SERVICE_TESTS, _SERVER_TESTS),
        adapters=("route alias onto POST /api/plan/archive",),
    ),
    _http(
        "POST /api/archive/restore",
        "Restore an archived plan and its state back into place; refuses "
        "rather than overwriting an occupied destination.",
        coverage=(_ARCHIVE_TESTS, _ARCHIVE_SERVICE_TESTS, _SERVER_TESTS),
    ),
    _http(
        "POST /api/archive/delete",
        "Permanently empty archive entries; requires confirm_count to match "
        "the archive_ids supplied. The only route that destroys one.",
        coverage=(
            _ARCHIVE_TESTS,
            _ARCHIVE_SERVICE_TESTS,
            _SERVER_TESTS,
            _FOUNDATION_TESTS,
        ),
        changes=("explicit-archive-cleanup",),
    ),
    _http(
        "POST /api/archive/settings",
        "Set the archive retention window: the age at which an entry "
        "becomes a cleanup candidate, not the age at which it is removed.",
        coverage=(_ARCHIVE_TESTS, _ARCHIVE_SERVICE_TESTS, _SERVER_TESTS),
        changes=("explicit-archive-cleanup",),
    ),
    _http(
        "POST /api/launch",
        "Start a run for a plan under a launch profile, keyed for idempotency.",
        coverage=(
            _LAUNCH_TESTS,
            _SERVER_TESTS,
            _DOCS_TESTS,
            _OPERATION_TESTS,
            _ACTION_TESTS,
        ),
    ),
    _http(
        "POST /api/reply",
        "Answer an open ASL question with reply_text, streamed to the child.",
        coverage=(
            _SERVER_TESTS,
            "scripts:tests/test_asl_reply_flows.py",
            _OPERATION_TESTS,
            _ACTION_TESTS,
        ),
    ),
    _http(
        "POST /api/control/stop-after-current",
        "Queue a stop that lands after the in-flight turn settles.",
        coverage=(_SERVER_TESTS, "scripts:tests/test_asl_control.py", _CONTROL_TESTS),
    ),
    _http(
        "POST /api/control/abort",
        "Abort the in-flight turn under ASL's default grace.",
        coverage=(_SERVER_TESTS, "scripts:tests/test_asl_control.py", _CONTROL_TESTS),
    ),
    _http(
        "POST /api/control/select-harness",
        "Choose the harness a paused run resumes under.",
        coverage=(_SERVER_TESTS, "scripts:tests/test_asl_control.py", _CONTROL_TESTS),
    ),
    _http(
        "POST /api/control/redirect",
        "Submit a mid-run correction and wait a bounded time for its verdict.",
        coverage=(_REDIRECT_TESTS, _DOCS_TESTS, _CONTROL_TESTS),
    ),
    _http(
        "POST /api/unlock",
        "Release a plan lock the operator has proven dead. The one place "
        "reclaim is still deliberate, now that no read does it.",
        coverage=(_ARCHIVE_SERVICE_TESTS, _SERVER_TESTS, _RECOVERY_TESTS),
    ),
    # -- Workbench static assets -------------------------------------------
    Capability(
        key=f"{SURFACE_WORKBENCH_STATIC}:GET /",
        surface=SURFACE_WORKBENCH_STATIC,
        summary="The single-page shell, served uncached so a reload gets the "
        "current app.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        regression_coverage=(_STATIC_TESTS,),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_STATIC}:GET /static/",
        surface=SURFACE_WORKBENCH_STATIC,
        summary="app.js and styles.css with ETag revalidation and a closed MIME map.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        regression_coverage=(_STATIC_TESTS,),
    ),
    # -- Workbench CLI ------------------------------------------------------
    Capability(
        key=f"{SURFACE_WORKBENCH_CLI}:asl workbench",
        surface=SURFACE_WORKBENCH_CLI,
        summary="The workbench command group; bare invocation serves, so the "
        "parent form keeps working.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        compatibility_adapters=(
            "cli._normalize_workbench_serve_args folds parent-form flags onto "
            "the serve subcommand",
        ),
        regression_coverage=(_CLI_TESTS, _DOCS_TESTS),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_CLI}:asl workbench serve",
        surface=SURFACE_WORKBENCH_CLI,
        summary="Serve the shared application locally on loopback with a "
        "bearer token; --open launches a browser.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        compatibility_adapters=(
            "the local provider wires asl_workbench to the Mac services",
        ),
        regression_coverage=(_CLI_TESTS, _SERVER_TESTS, _DOCS_TESTS),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_CLI}:asl workbench connect",
        surface=SURFACE_WORKBENCH_CLI,
        summary="Publish heartbeats, the complete catalog, active runs, and "
        "incremental history to a remote service, answer that service's "
        "correlated detail reads, and -- only under --allow-remote-actions -- "
        "perform its mutating commands, all on the same outward link. Calls "
        "the shared catalog, observation, history, detail, action, control, "
        "and archive services in process; no bridge subprocess and no disk "
        "spooling. Capabilities are derived from what was wired, so the "
        "default invocation announces itself observation-only.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
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
            _CONNECTOR_TESTS,
            _REMOTE_READ_TESTS,
            _REMOTE_ACTION_TESTS,
            _CLI_TESTS,
            _DOCS_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_CLI}:asl workbench pin",
        surface=SURFACE_WORKBENCH_CLI,
        summary="Pin a plan into the registry with a label and launch "
        "profile. Same service as POST /api/registry/pin, so the two refuse "
        "the same inputs for the same reason.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(_ARCHIVE_SERVICE_TESTS, _CLI_TESTS, _DOCS_TESTS),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_CLI}:asl workbench unpin",
        surface=SURFACE_WORKBENCH_CLI,
        summary="Drop a registry pin.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(_ARCHIVE_SERVICE_TESTS, _CLI_TESTS, _DOCS_TESTS),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_CLI}:asl workbench registry",
        surface=SURFACE_WORKBENCH_CLI,
        summary="Print the registry, its warnings, and the discovered plan roots.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(_CLI_TESTS, _DOCS_TESTS),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_CLI}:asl plans",
        surface=SURFACE_WORKBENCH_CLI,
        summary="Terminal plan listing with stale/never-run filters, sharing "
        "the Workbench summary builder.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(_CLI_TESTS, _SUMMARY_TESTS),
    ),
    # -- Workbench browser workflows ---------------------------------------
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:triage-inbox",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="Default inbox: urgency grouping, activity labels, filters, "
        "search, and completed-plan suppression.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        # The inbox is now the overview, and the overview suite drives it: the
        # sections it groups into, the chips and facets that narrow it, the
        # search, and the request that hides completed work -- against both
        # deployments, so "the inbox" is one behaviour rather than two.
        regression_coverage=(
            _DOCS_TESTS,
            _SUMMARY_TESTS,
            _DETAIL_TESTS,
            _OVERVIEW_TESTS,
            _OVERVIEW_BROWSER_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:detailed-search",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="Free-text search over a plan's private detail -- its path, "
        "the operator's pin label, pause prose, last-message preview. An "
        "online read the Mac answers; none of it enters a published entry.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        regression_coverage=(_DETAIL_TESTS,),
        # The service behind it is tested; the browser that calls it is not,
        # like every other row on this surface.
        coverage_gap=GAP_WORKBENCH_BROWSER,
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:two-pane-shell",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="Responsive two-pane shell with the preview pane and its "
        "phone-width collapse.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        # Both shapes are asserted at both widths against both deployments, so
        # the collapse is a tested behaviour rather than a CSS rule nobody has
        # opened a phone to check.
        regression_coverage=(_DOCS_TESTS, _SHELL_BROWSER_TESTS),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:row-action-focus",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="Row actions move focus to the pane they act on, so keyboard "
        "use survives a re-render.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        regression_coverage=(_DOCS_TESTS, _SHELL_BROWSER_TESTS),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:plan-detail",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="Detail view: state, sessions, events, logs, artifacts, "
        "verification, progress, workspace.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        # Every pane is asserted to render either its material or its own empty
        # sentence, for a plan that has been run and one that never has, in
        # both deployments -- a pane that failed silently is the failure mode
        # this replaces.
        regression_coverage=(
            _DOCS_TESTS,
            _SERVER_TESTS,
            _SHELL_TESTS,
            _SHELL_BROWSER_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:controls-bar",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="Launch, reply, stop-after-current, abort, harness select, "
        "redirect, unlock, pin, archive from one bar.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_ASL,
        # Every control on the bar is now driven through one guarded path and
        # asserted from both ends: what each mode will carry out, that the
        # sentence under a shut control is the refusal it would have given,
        # that a run that has moved on is named as the reason rather than the
        # mode, and that a recorded-but-unconfirmed submission is never drawn
        # as done. Launch is asserted through its disabled state rather than by
        # pressing it -- a browser suite that started a real run would leave
        # one behind.
        regression_coverage=(
            _DOCS_TESTS,
            _SERVER_TESTS,
            _INTERACTION_TESTS,
            _INTERACTION_BROWSER_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:recovery-center",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="Recovery cards with copyable commands and the security "
        "boundaries on what they may offer.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_ASL,
        # One of the boundaries is now asserted: a recovery card that acts is
        # the same action as the controls bar, so it is shut by the same
        # capability pass rather than refused after the press. The cards
        # themselves are still only reached by the smoke checklist -- the shell
        # fixture's plans diagnose to a card that navigates, so no browser test
        # here can put a mutating card on screen.
        regression_coverage=(_DOCS_TESTS, _RECOVERY_TESTS, _INTERACTION_TESTS),
        coverage_gap=GAP_WORKBENCH_BROWSER,
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:archive-browser",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="Archive listing with bulk select, restore, permanent empty, "
        "the retention control, and per-entry cleanup candidacy. Opening it "
        "deletes nothing.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_ASL,
        # Browsing-deletes-nothing and the two confirmations are asserted
        # against a live archive, and the browser half drives the listing, the
        # bulk bar's two distinct reasons for being shut, and the expired-entry
        # review that now stands in front of the typed count. The purge itself
        # is driven up to that gate and no further: a spec that got past it
        # would be emptying the archive the other specs read.
        regression_coverage=(
            _DOCS_TESTS,
            _ARCHIVE_TESTS,
            _STATIC_TESTS,
            _INTERACTION_TESTS,
            _INTERACTION_BROWSER_TESTS,
        ),
        intentional_changes=("explicit-archive-cleanup",),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:cleanup-review-bar",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="Bulk cleanup selection by age threshold, reviewed before it "
        "archives anything.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_ASL,
        # The review is the capability, so the browser half drives the bar to
        # the edge of the archive and stops: the button starts shut, selecting
        # is a read that arms nothing, and on the Pi the mode's reason replaces
        # the selection's rather than the selection unlocking it.
        regression_coverage=(
            _DOCS_TESTS,
            _ARCHIVE_TESTS,
            _INTERACTION_BROWSER_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:conversation-recovery-card",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="Conversation recovery card: what was retained and what to run next.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_ASL,
        regression_coverage=(_DOCS_TESTS, _SERVER_TESTS),
        coverage_gap=GAP_WORKBENCH_BROWSER,
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:session-preferences",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="Filter, sort, and pane preferences held in session storage "
        "and mirrored into the URL, so a view can be sent as a link.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        # Asserted in both directions, which is the whole claim: a choice made
        # in the controls lands in the address bar, and a link carrying one
        # opens the view it describes rather than the default.
        regression_coverage=(_DOCS_TESTS, _OVERVIEW_BROWSER_TESTS),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:empty-state-actions",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="Empty states offer the action that would end them: clear "
        "search, reset filter and its facets, show completed.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        # Driven through the case that matters now that the narrowing happens
        # on the server: a facet that matches nothing arrives as zero rows, and
        # the page has to say which filter emptied it rather than report an
        # empty machine with nothing to undo.
        regression_coverage=(_DOCS_TESTS, _OVERVIEW_BROWSER_TESTS),
    ),
    Capability(
        key=f"{SURFACE_WORKBENCH_BROWSER}:fragment-deep-link-routing",
        surface=SURFACE_WORKBENCH_BROWSER,
        summary="One router over both link formats history left behind: the "
        "Workbench's '#plan?dir=|key=&focus=' and the Companion's '#run=', "
        "which is resolved against the catalog and rewritten to the canonical "
        "form.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        regression_coverage=(
            _BRIDGE_TESTS,
            _FOUNDATION_TESTS,
            _SHELL_BROWSER_TESTS,
        ),
    ),
    # -- AgentMux bridge ----------------------------------------------------
    Capability(
        key=f"{SURFACE_BRIDGE}:workbench.ensure",
        surface=SURFACE_BRIDGE,
        summary="Start-or-reuse one bridge-owned Workbench and hand back a "
        "tokenized deep link for a plan.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        compatibility_adapters=(
            "the existing bridge protocol is unchanged; the new Workbench "
            "transport is versioned separately",
        ),
        regression_coverage=(_BRIDGE_TESTS, _FOUNDATION_TESTS),
    ),
    Capability(
        key=f"{SURFACE_BRIDGE}:runs.snapshot",
        surface=SURFACE_BRIDGE,
        summary="The bounded intervention projection AgentMux and the "
        "Companion connector both read.",
        origin=ORIGIN_BOTH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        compatibility_adapters=(
            "the Companion connector consumes this capped projection and "
            "cannot supply full Workbench discovery from it",
        ),
        regression_coverage=(
            "scripts:tests/test_asl_bridge_projection.py",
            _BRIDGE_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_BRIDGE}:run.reply",
        surface=SURFACE_BRIDGE,
        summary="Answer one open question over the bridge, with an operation "
        "identity that survives a reconnect.",
        origin=ORIGIN_BOTH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            _BRIDGE_TESTS,
            _ACTION_TESTS,
            f"{_PI}/tests/test_companion_live_reply.py",
        ),
    ),
    Capability(
        key=f"{SURFACE_BRIDGE}:run.resume",
        surface=SURFACE_BRIDGE,
        summary="Spawn a resume for a paused plan with lock authorization "
        "narrowed to that plan.",
        origin=ORIGIN_BOTH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            _BRIDGE_TESTS,
            _ACTION_TESTS,
            f"{_PI}/tests/test_companion_resume.py",
        ),
    ),
    Capability(
        key=f"{SURFACE_BRIDGE}:run.control",
        surface=SURFACE_BRIDGE,
        summary="Stop-after-current and abort over the bridge, sharing ASL's "
        "control queue and grace.",
        origin=ORIGIN_BOTH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            _BRIDGE_TESTS,
            "scripts:tests/test_asl_control.py",
            _CONTROL_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_BRIDGE}:redirect",
        surface=SURFACE_BRIDGE,
        summary="Submit, acknowledge, and recover mid-run corrections through "
        "the durable redirect journal.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            "scripts:tests/test_asl_bridge_redirect.py",
            _REDIRECT_TESTS,
            _CONTROL_TESTS,
        ),
    ),
    # -- Companion Pi service routes ---------------------------------------
    _companion_http(
        "GET /",
        "Dashboard page on the bare host, behind the owner check.",
        coverage=(f"{_PI}/tests/test_companion_dashboard.py",),
    ),
    _companion_http(
        "GET /dashboard",
        "Dashboard page on its explicit path, for bookmarks and alert links.",
        coverage=(f"{_PI}/tests/test_companion_dashboard.py",),
    ),
    _companion_http(
        "GET /assets/dashboard.css",
        "Dashboard stylesheet, owner-gated like the data it renders.",
        coverage=(f"{_PI}/tests/test_companion_dashboard.py",),
    ),
    _companion_http(
        "GET /assets/dashboard.js",
        "Dashboard script, owner-gated like the data it renders.",
        coverage=(f"{_PI}/tests/test_companion_dashboard.py",),
    ),
    _companion_http(
        "GET /v1/health",
        "The one public route: service liveness, and nothing about any run.",
        coverage=(f"{_PI}/tests/test_companion_api.py",),
    ),
    _companion_http(
        "GET /v1/summary",
        "Shape-only run summary for the restricted summary credential.",
        coverage=(f"{_PI}/tests/test_companion_api.py",),
    ),
    _companion_http(
        "GET /v1/status",
        "Owner status: transport liveness, projection age, completeness.",
        coverage=(
            f"{_PI}/tests/test_companion_api.py",
            f"{_PI}/tests/test_companion_observation.py",
        ),
    ),
    _companion_http(
        "GET /v1/runs",
        "Owner run rows from retained observations, with freshness.",
        coverage=(f"{_PI}/tests/test_companion_api.py",),
    ),
    _companion_http(
        "GET /v1/history",
        "Seven-day terminal-run history imported from ASL's event journals, "
        "interpreted by the shared history service.",
        coverage=(f"{_PI}/tests/test_companion_history.py", _HISTORY_TESTS),
    ),
    _companion_http(
        "GET /v1/questions",
        "The question bodies the Pi holds, owner-only and never in /v1/runs.",
        coverage=(f"{_PI}/tests/test_companion_api.py",),
    ),
    _companion_http(
        "GET /v1/projects",
        "Project and plan labels, with filesystem paths withheld.",
        coverage=(f"{_PI}/tests/test_companion_projects.py",),
    ),
    _companion_http(
        "POST /v1/ingest",
        "The single connector upload path: observations, results, operation updates.",
        coverage=(
            f"{_PI}/tests/test_companion_transport.py",
            f"{_PI}/tests/test_companion_protocol.py",
        ),
    ),
    _companion_http(
        "POST /v1/commands/poll",
        "Connector collects the commands the Pi is holding.",
        coverage=(f"{_PI}/tests/test_companion_transport.py",),
    ),
    _companion_http(
        "POST /v1/commands/redeem",
        "Connector redeems a reply body exactly once.",
        coverage=(
            f"{_PI}/tests/test_companion_transport.py",
            f"{_PI}/tests/test_companion_live_reply.py",
        ),
    ),
    _companion_http(
        "GET /v1/operations",
        "Owner view of submitted mutations and their reconciled outcomes.",
        coverage=(f"{_PI}/tests/test_companion_transport.py",),
    ),
    _companion_http(
        "POST /v1/operations",
        "Owner submits one bounded mutation with a closed field set.",
        coverage=(f"{_PI}/tests/test_companion_transport.py",),
    ),
    _companion_http(
        "POST /v1/operations/cancel",
        "Owner cancels a submitted mutation that has not been dispatched.",
        coverage=(f"{_PI}/tests/test_companion_transport.py",),
    ),
    # -- Companion modules --------------------------------------------------
    _companion_module(
        "companion",
        "Package contract: two processes, one protocol, read-mostly cockpit.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_protocol.py",),
    ),
    _companion_module(
        "companion.auth",
        "Three caller roles with three different proofs: connector, summary, "
        "owner. Migrated to asl_workbench.credentials, which binds each role "
        "to the envelope kinds and transport capabilities it may exercise; the "
        "Pi module is the frozen baseline until the Companion imports the "
        "portable package.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_api.py", _TRANSPORT_TESTS),
    ),
    _companion_module(
        "companion.commands",
        "The Pi's command queue and the Mac's durable operation journal.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_transport.py",),
    ),
    _companion_module(
        "companion.config",
        "Strict typed configuration: unknown keys are refused, not ignored.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_api.py",),
    ),
    _companion_module(
        "companion.connector",
        "Owns the ASL bridge subprocess on the Mac and turns frames into "
        "observations. Superseded by asl_workbench.connector and "
        "'asl workbench connect', which read the shared services in process; "
        "the Pi module is the frozen baseline for the single-instance lock and "
        "the bounded backoff, which were copied rather than reinvented.",
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        coverage=(f"{_PI}/tests/test_companion_observation.py", _CONNECTOR_TESTS),
    ),
    _companion_module(
        "companion.dashboard",
        "Three static files and the policy that serves them; nothing renders "
        "on the Pi.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_dashboard.py",),
    ),
    _companion_module(
        "companion.framing",
        "Bounded NDJSON framing for the bridge subprocess.",
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        coverage=(f"{_PI}/tests/test_companion_protocol.py",),
    ),
    _companion_module(
        "companion.freshness",
        "Three independent staleness questions: liveness, age, completeness. "
        "Migrated to asl_workbench.freshness; the Pi module is the frozen "
        "baseline until the Companion imports the portable package.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_observation.py", _STORE_TESTS),
    ),
    _companion_module(
        "companion.history",
        "Incremental import of ASL terminal runs into seven-day history. The "
        "interpretation now lives in asl_workbench.history, beside ASL's own "
        "vocabulary; the reading lives in "
        "agent_session_loop.workbench_history.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_history.py", _HISTORY_TESTS),
    ),
    _companion_module(
        "companion.identity",
        "Stable plan, run, and session identity, and labels safe to leave the Mac.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_observation.py",),
    ),
    _companion_module(
        "companion.incidents",
        "Durable incident policy with persist-before-publish delivery. "
        "Migrated to asl_workbench.incidents; the Pi module is the frozen "
        "baseline until the Companion imports the portable package.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_incidents.py", _STORE_TESTS),
    ),
    _companion_module(
        "companion.integrations",
        "Scoped Pi integrations: read-only preview, receipt before each mutation.",
        owner=OWNER_PI_REPO,
        authority=AUTHORITY_HOST,
        coverage=(f"{_PI}/tests/test_companion_integrations.py",),
    ),
    _companion_module(
        "companion.integrations_kuma",
        "Private stdin-only Kuma adapter for the one service-health monitor.",
        owner=OWNER_PI_REPO,
        authority=AUTHORITY_HOST,
        coverage=(f"{_PI}/tests/test_companion_integrations.py",),
    ),
    _companion_module(
        "companion.lifecycle",
        "Content-addressed per-user installation and the fixed native unit "
        "identifiers.",
        owner=OWNER_PI_REPO,
        authority=AUTHORITY_HOST,
        coverage=(f"{_PI}/tests/test_companion_lifecycle.py",),
    ),
    _companion_module(
        "companion.live",
        "The Mac's live half: read one ASL question and answer it.",
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        coverage=(f"{_PI}/tests/test_companion_live_reply.py",),
    ),
    _companion_module(
        "companion.ntfy",
        "Receipt-aware ntfy publisher over the standard library only.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_incidents.py",),
    ),
    _companion_module(
        "companion.observation",
        "The one place ASL's shapes become the Companion's shapes. Timestamp "
        "and project/run enrichment are shared with the local view through "
        "asl_workbench.history.",
        owner=OWNER_PORTABLE,
        coverage=(
            f"{_PI}/tests/test_companion_observation.py",
            _HISTORY_TESTS,
            _STORE_TESTS,
        ),
    ),
    _companion_module(
        "companion.projects",
        "Project and plan labels read locally; paths never leave the Mac.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_projects.py",),
    ),
    _companion_module(
        "companion.protocol",
        "Protocol v1: envelope validation, operation states, allowlists. The "
        "Workbench's own five envelopes are versioned separately in "
        "asl_workbench.transport, which reuses this module's bounded frames "
        "and closed shapes without inheriting its version number; the Pi "
        "module is the frozen baseline until the Companion imports the "
        "portable package.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_protocol.py", _TRANSPORT_TESTS),
    ),
    _companion_module(
        "companion.questions",
        "The question bodies the Pi holds, and nothing else.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_api.py",),
    ),
    _companion_module(
        "companion.recovery",
        "Companion-only consistent snapshots and offline restore staging.",
        owner=OWNER_PI_REPO,
        authority=AUTHORITY_HOST,
        coverage=(f"{_PI}/tests/test_companion_rollout.py",),
    ),
    _companion_module(
        "companion.resume",
        "What a resume launch proves and what it does not. The interpretation "
        "is ASL's own now, in asl_workbench.actions; this module is the "
        "Companion's adapter onto it.",
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        coverage=(_ACTION_TESTS, f"{_PI}/tests/test_companion_resume.py"),
    ),
    _companion_module(
        "companion.runtime",
        "Installed entry points; secrets and reply bodies stay out of diagnostics.",
        owner=OWNER_PI_REPO,
        authority=AUTHORITY_HOST,
        coverage=(f"{_PI}/tests/test_companion_lifecycle.py",),
    ),
    _companion_module(
        "companion.service",
        "The Pi's HTTP service: one ingest path, three read audiences, one "
        "health check.",
        owner=OWNER_PORTABLE,
        coverage=(f"{_PI}/tests/test_companion_api.py",),
    ),
    _companion_module(
        "companion.store",
        "The Pi's SQLite store: migrations, ordered ingestion, retention, "
        "reads. Migrated to asl_workbench.store, which owns catalog storage "
        "the Pi module never had; the Pi module is the frozen baseline until "
        "the Companion imports the portable package.",
        owner=OWNER_PORTABLE,
        coverage=(
            f"{_PI}/tests/test_companion_api.py",
            f"{_PI}/tests/test_companion_history.py",
            _STORE_TESTS,
        ),
    ),
    # -- Companion browser workflows ---------------------------------------
    Capability(
        key=f"{SURFACE_COMPANION_BROWSER}:overview-sections",
        surface=SURFACE_COMPANION_BROWSER,
        summary="Needs-attention, running, unconfirmed, recent, and linked-"
        "older sections as the phone overview.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        # The Pi's spec is kept because it is what the behaviour was proven by;
        # the two new references are what proves it survived the move, section
        # by section and in both deployments rather than only on the phone.
        regression_coverage=(
            f"{_PI}/tests/browser/dashboard.spec.js",
            _OVERVIEW_TESTS,
            _OVERVIEW_BROWSER_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_COMPANION_BROWSER}:filters-and-sort",
        surface=SURFACE_COMPANION_BROWSER,
        summary="Project, harness, search, sort order, age-window note, and "
        "clear-filters.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        # Folded into the Workbench's sort modes and URL persistence rather
        # than kept as a second filter bar, so the references cover both
        # halves: the narrowing is applied before the page and counted over the
        # whole match, and every mode the select offers is one a server takes.
        regression_coverage=(
            f"{_PI}/tests/browser/dashboard.spec.js",
            _OVERVIEW_TESTS,
            _OVERVIEW_BROWSER_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_COMPANION_BROWSER}:freshness-chip",
        surface=SURFACE_COMPANION_BROWSER,
        summary="Observed-at and freshness chip: what the page is showing and "
        "how old it is.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        regression_coverage=(
            f"{_PI}/tests/browser/dashboard.spec.js",
            f"{_PI}/tests/test_companion_observation.py",
            # Including the case the chip exists for: a partial generation
            # leaves rows the newest observation never confirmed, and the page
            # has to say so instead of drawing them as live.
            _OVERVIEW_TESTS,
            _OVERVIEW_BROWSER_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_COMPANION_BROWSER}:linked-run-deep-link",
        surface=SURFACE_COMPANION_BROWSER,
        summary="Notification deep link opens one run, with a dismissable "
        "older-run warning.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        regression_coverage=(f"{_PI}/tests/browser/dashboard.spec.js",),
    ),
    Capability(
        key=f"{SURFACE_COMPANION_BROWSER}:reply-panel",
        surface=SURFACE_COMPANION_BROWSER,
        summary="Phone reply flow: question body, draft, availability, send, "
        "and its refusal messages.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            f"{_PI}/tests/browser/reply_ui.spec.js",
            f"{_PI}/tests/test_companion_reply_ui.py",
        ),
    ),
    Capability(
        key=f"{SURFACE_COMPANION_BROWSER}:resume-reply",
        surface=SURFACE_COMPANION_BROWSER,
        summary="Resume a paused run from the phone, with the explanation of "
        "what a launch proves.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            f"{_PI}/tests/browser/reply_ui.spec.js",
            f"{_PI}/tests/test_companion_resume.py",
        ),
    ),
    Capability(
        key=f"{SURFACE_COMPANION_BROWSER}:failure-banners",
        surface=SURFACE_COMPANION_BROWSER,
        summary="Banners for transport, staleness, and incident conditions the "
        "page must not hide.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_NONE,
        regression_coverage=(
            f"{_PI}/tests/browser/failure_matrix.spec.js",
            f"{_PI}/tests/test_companion_failure_matrix.py",
        ),
    ),
    # -- Integrations -------------------------------------------------------
    Capability(
        key=f"{SURFACE_INTEGRATION}:ntfy-notifications",
        surface=SURFACE_INTEGRATION,
        summary="Incident notifications with deep links back to the private "
        "dashboard URL.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_PI_SERVICE,
        compatibility_adapters=("existing notification links must keep resolving",),
        regression_coverage=(f"{_PI}/tests/test_companion_incidents.py",),
    ),
    Capability(
        key=f"{SURFACE_INTEGRATION}:uptime-kuma-monitor",
        surface=SURFACE_INTEGRATION,
        summary="The single fixed service-health monitor, provisioned over a "
        "stdin-only adapter.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PI_REPO,
        authority=AUTHORITY_HOST,
        regression_coverage=(f"{_PI}/tests/test_companion_integrations.py",),
    ),
    Capability(
        key=f"{SURFACE_INTEGRATION}:tailscale-serve-route",
        surface=SURFACE_INTEGRATION,
        summary="The private URL: one owned route on port 9443 pointing at the "
        "companion service.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PI_REPO,
        authority=AUTHORITY_HOST,
        compatibility_adapters=("the existing private URL must not change",),
        regression_coverage=(f"{_PI}/tests/test_companion_integrations.py",),
    ),
    Capability(
        key=f"{SURFACE_INTEGRATION}:homepage-documents",
        surface=SURFACE_INTEGRATION,
        summary="Homepage service entries describing the companion for the "
        "host's own dashboard.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PI_REPO,
        authority=AUTHORITY_HOST,
        regression_coverage=(f"{_PI}/tests/test_companion_integrations.py",),
    ),
    Capability(
        key=f"{SURFACE_INTEGRATION}:agentmux-bridge",
        surface=SURFACE_INTEGRATION,
        summary="AgentMux's existing bridge protocol, which the new Workbench "
        "transport must not be folded into.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        compatibility_adapters=(
            "the bridge contract stays as it is; new operations get their own "
            "versioned transport",
        ),
        regression_coverage=(
            "scripts:tests/test_asl_agent_mux_contract.py",
            "scripts:tests/test_asl_agent_mux_projection.py",
            _TRANSPORT_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_INTEGRATION}:native-service-units",
        surface=SURFACE_INTEGRATION,
        summary="The fixed launchd label and systemd unit names the installation owns.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PI_REPO,
        authority=AUTHORITY_HOST,
        compatibility_adapters=(
            "existing installation prefixes and unit identifiers are preserved",
        ),
        regression_coverage=(f"{_PI}/tests/test_companion_lifecycle.py",),
    ),
    # -- Persistence boundaries --------------------------------------------
    Capability(
        key=f"{SURFACE_PERSISTENCE}:workbench-registry",
        surface=SURFACE_PERSISTENCE,
        summary="workbench.json: schema v2 pins, launch profiles, plan roots, "
        "archive retention, under a lock.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(_SERVER_TESTS, _ARCHIVE_TESTS),
    ),
    Capability(
        key=f"{SURFACE_PERSISTENCE}:asl-plan-state",
        surface=SURFACE_PERSISTENCE,
        summary="Per-plan state.json under the ASL state root: the run's "
        "authoritative record.",
        origin=ORIGIN_BOTH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            "scripts:tests/test_asl_state_migration.py",
            _SUMMARY_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_PERSISTENCE}:asl-event-journal",
        surface=SURFACE_PERSISTENCE,
        summary="Append-only events.jsonl per plan, read by both the Workbench "
        "and Companion history import through one forward reader that resumes "
        "only where a checkpoint proves the file is unchanged.",
        origin=ORIGIN_BOTH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            _SERVER_TESTS,
            f"{_PI}/tests/test_companion_history.py",
            _HISTORY_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_PERSISTENCE}:asl-plan-lock",
        surface=SURFACE_PERSISTENCE,
        summary="Per-plan lock files; catalog and queue reads leave them "
        "alone, and reclaim stays on recovery and unlock.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            _SUMMARY_TESTS,
            _RECOVERY_TESTS,
            _FOUNDATION_TESTS,
            _CATALOG_TESTS,
        ),
        intentional_changes=("observational-reads",),
    ),
    Capability(
        key=f"{SURFACE_PERSISTENCE}:workbench-archive",
        surface=SURFACE_PERSISTENCE,
        summary="archive/<id>/ holding archive.json plus the plan and state "
        "copies. The retention window marks entries for cleanup; only a "
        "confirmed purge removes one.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            _ARCHIVE_TESTS,
            _ARCHIVE_SERVICE_TESTS,
            _FOUNDATION_TESTS,
        ),
        intentional_changes=("explicit-archive-cleanup",),
    ),
    Capability(
        key=f"{SURFACE_PERSISTENCE}:redirect-journal",
        surface=SURFACE_PERSISTENCE,
        summary="The durable redirect journal: what a correction's fate was, "
        "across a restart of whatever submitted it.",
        origin=ORIGIN_WORKBENCH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            "scripts:tests/test_asl_redirect_store.py",
            _REDIRECT_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_PERSISTENCE}:plan-operation-journal",
        surface=SURFACE_PERSISTENCE,
        summary="operations.jsonl beside the redirect journal: which mutation "
        "was admitted, that it was dispatched before anything ran, and how it "
        "was accounted for afterwards. Identifiers only -- no reply text and "
        "no action token.",
        origin=ORIGIN_BOTH,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_ASL,
        regression_coverage=(_OPERATION_TESTS, _SERVER_TESTS, _ACTION_TESTS),
    ),
    Capability(
        key=f"{SURFACE_PERSISTENCE}:plan-artifacts",
        surface=SURFACE_PERSISTENCE,
        summary="Turn artifacts and final-review.md/json in the plan "
        "directory; bodies are fetched on demand and never retained on the Pi.",
        origin=ORIGIN_BOTH,
        owner=OWNER_MAC,
        authority=AUTHORITY_ASL,
        regression_coverage=(_SERVER_TESTS,),
    ),
    Capability(
        key=f"{SURFACE_PERSISTENCE}:companion-sqlite-store",
        surface=SURFACE_PERSISTENCE,
        summary="state/companion.sqlite3: cached observations, history, "
        "incidents, commands, operations, and their retention.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_PI_SERVICE,
        regression_coverage=(
            f"{_PI}/tests/test_companion_api.py",
            f"{_PI}/tests/test_companion_transport.py",
            _STORE_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_PERSISTENCE}:companion-credentials",
        surface=SURFACE_PERSISTENCE,
        summary="credentials/secrets.json: the connector, summary, and owner "
        "credentials that must survive the migration unchanged.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PI_REPO,
        authority=AUTHORITY_HOST,
        compatibility_adapters=("existing credentials are preserved, not reissued",),
        regression_coverage=(
            f"{_PI}/tests/test_companion_lifecycle.py",
            _TRANSPORT_TESTS,
        ),
    ),
    Capability(
        key=f"{SURFACE_PERSISTENCE}:companion-runtime-document",
        surface=SURFACE_PERSISTENCE,
        summary="config/runtime.json: the installed service's configuration and role.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PI_REPO,
        authority=AUTHORITY_HOST,
        regression_coverage=(f"{_PI}/tests/test_companion_lifecycle.py",),
    ),
    Capability(
        key=f"{SURFACE_PERSISTENCE}:connector-operation-journal",
        surface=SURFACE_PERSISTENCE,
        summary="The Mac connector's operation journal: dispatch evidence "
        "written before the mutation is attempted.",
        origin=ORIGIN_COMPANION,
        owner=OWNER_PORTABLE,
        authority=AUTHORITY_ASL,
        regression_coverage=(
            f"{_PI}/tests/test_companion_transport.py",
            _OPERATION_TESTS,
            _STORE_TESTS,
        ),
    ),
)


# -- Queries ----------------------------------------------------------------


def capability(key: str) -> Capability:
    """The row for ``key``, or :class:`MatrixError` if the matrix lacks one."""
    for row in CAPABILITIES:
        if row.key == key:
            return row
    raise MatrixError(f"no capability named {key!r}")


def by_surface(surface: str) -> tuple[Capability, ...]:
    """Every row on one surface, in matrix order."""
    if surface not in SURFACES:
        raise MatrixError(f"no surface named {surface!r}")
    return tuple(row for row in CAPABILITIES if row.surface == surface)


def surface_keys(surface: str) -> frozenset[str]:
    """The identifiers covered on one surface, with the prefix stripped.

    This is what a completeness check compares against: a route table or an
    argument parser yields identifiers, not matrix keys.
    """
    return frozenset(row.identifier for row in by_surface(surface))


def owned_by(owner: str) -> tuple[Capability, ...]:
    if owner not in OWNERS:
        raise MatrixError(f"no owner named {owner!r}")
    return tuple(row for row in CAPABILITIES if row.owner == owner)


def coverage_gaps() -> tuple[Capability, ...]:
    """Every row whose coverage is a documented manual check, not a test."""
    return tuple(row for row in CAPABILITIES if row.coverage_gap is not None)


def coverage_paths(alias: str) -> tuple[str, ...]:
    """Distinct repository-relative coverage paths named under one alias.

    Split out by alias because the two repositories are checked differently:
    ``scripts`` paths always resolve, while ``sglavoie_raspberry_pi`` paths only
    resolve when a Pi checkout is reachable.
    """
    if alias not in COVERAGE_ALIASES:
        raise MatrixError(f"no coverage alias named {alias!r}")
    prefix = f"{alias}:"
    paths = {
        reference.removeprefix(prefix)
        for row in CAPABILITIES
        for reference in row.regression_coverage
        if reference.startswith(prefix)
    }
    return tuple(sorted(paths))


def local_coverage_paths() -> tuple[str, ...]:
    """Distinct ``scripts``-relative coverage paths named anywhere in the matrix."""
    return coverage_paths(LOCAL_COVERAGE_ALIAS)


# -- Validation -------------------------------------------------------------


def _coverage_problems(row: Capability) -> list[str]:
    problems: list[str] = []
    if not row.regression_coverage:
        problems.append(f"{row.key}: names no regression coverage")
    for reference in row.regression_coverage:
        alias, separator, path = reference.partition(":")
        if not separator or alias not in COVERAGE_ALIASES:
            problems.append(
                f"{row.key}: coverage {reference!r} is not an "
                f"'alias:path' reference into {list(COVERAGE_ALIASES)}"
            )
            continue
        if not path or path.startswith("/"):
            problems.append(
                f"{row.key}: coverage {reference!r} must be repository-relative"
            )
    return problems


def validate_matrix(
    *,
    repo_root: Path | None = None,
    known_changes: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Every structural problem with the matrix, as lines naming the row.

    Returns an empty tuple when the matrix holds. ``known_changes`` is the
    intentional-change register's keys; passing it checks that no row points at
    a change nobody wrote down. ``repo_root`` resolves ``scripts:`` coverage
    references against the filesystem, so a renamed test module is a failure
    here instead of a reference that reads fine and means nothing.
    """
    root = REPO_ROOT if repo_root is None else Path(repo_root)
    changes = set(known_changes) if known_changes is not None else None
    problems: list[str] = []
    seen: set[str] = set()
    for row in CAPABILITIES:
        if row.key in seen:
            problems.append(f"{row.key}: duplicate key")
        seen.add(row.key)
        if row.surface not in SURFACES:
            problems.append(f"{row.key}: unknown surface {row.surface!r}")
        elif not row.key.startswith(f"{row.surface}:") or not row.identifier:
            problems.append(f"{row.key}: key must be '<surface>:<identifier>'")
        if row.origin not in ORIGINS:
            problems.append(f"{row.key}: unknown origin {row.origin!r}")
        if row.owner not in OWNERS:
            problems.append(f"{row.key}: unknown owner {row.owner!r}")
        if row.authority not in AUTHORITIES:
            problems.append(f"{row.key}: unknown authority {row.authority!r}")
        if not row.summary.strip():
            problems.append(f"{row.key}: empty summary")
        problems.extend(_coverage_problems(row))
        if len(set(row.intentional_changes)) != len(row.intentional_changes):
            problems.append(f"{row.key}: lists an intentional change twice")
        if changes is not None:
            for change_key in row.intentional_changes:
                if change_key not in changes:
                    problems.append(
                        f"{row.key}: names unregistered intentional change "
                        f"{change_key!r}"
                    )
    for path in local_coverage_paths():
        if not (root / path).exists():
            problems.append(
                f"coverage path does not exist: {LOCAL_COVERAGE_ALIAS}:{path}"
            )
    return tuple(problems)


# -- Rendering --------------------------------------------------------------


def render_matrix(rows: Sequence[Capability] | None = None) -> str:
    """The matrix as Markdown tables, one per surface.

    A rendered artifact rather than a checked-in document: the data is the
    source of truth, so there is no generated file to go stale.
    """
    selected = CAPABILITIES if rows is None else tuple(rows)
    grouped: dict[str, list[Capability]] = {surface: [] for surface in SURFACES}
    for row in selected:
        grouped.setdefault(row.surface, []).append(row)
    lines = ["# Capability and ownership matrix", ""]
    for surface in SURFACES:
        present = grouped.get(surface) or []
        if not present:
            continue
        lines.append(f"## {surface}")
        lines.append("")
        lines.append(
            "| Capability | Origin | Owner | Authority | Adapters | Coverage "
            "| Intentional change |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for row in present:
            adapters = "; ".join(row.compatibility_adapters) or "-"
            coverage = "; ".join(row.regression_coverage) or "-"
            if row.coverage_gap is not None:
                coverage = f"{coverage} (gap: {row.coverage_gap})"
            changed = "; ".join(row.intentional_changes) or "-"
            lines.append(
                f"| `{row.identifier}` | {row.origin} | {row.owner} | "
                f"{row.authority} | {adapters} | {coverage} | {changed} |"
            )
        lines.append("")
    return "\n".join(lines)


def summary_counts() -> dict[str, int]:
    """Row counts per surface, for a one-line inventory check."""
    return {surface: len(by_surface(surface)) for surface in SURFACES}


def main(argv: Sequence[str] | None = None) -> int:
    """Render the matrix, or report its problems and refuse.

    Validation runs before either output: a matrix that does not satisfy
    :func:`validate_matrix` is not worth printing, and printing it anyway is
    how an inventory starts being trusted after it stopped being true.
    """
    parser = argparse.ArgumentParser(
        prog=f"python3 -m {PACKAGE_NAME}",
        description="Render the capability and ownership matrix.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the summary counts as JSON instead of Markdown tables",
    )
    options = parser.parse_args(list(argv) if argv is not None else None)
    problems = validate_matrix()
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    if options.json:
        print(json.dumps(payload(), indent=2, sort_keys=True))
    else:
        print(render_matrix())
    return 0


def payload() -> dict[str, object]:
    """The matrix as plain data, for a caller that wants to render it itself."""
    counts: Mapping[str, int] = summary_counts()
    return {
        "total": len(CAPABILITIES),
        "by_surface": dict(counts),
        "coverage_gaps": [row.key for row in coverage_gaps()],
    }
