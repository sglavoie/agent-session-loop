# ruff: noqa: F401, F403, F405, I001
"""The harness the deployed staging rehearsal is driven and recorded through.

Session 22 stages the candidate release on the two machines that already run
the Companion -- this Mac and the Pi -- and then has to answer the question
:mod:`tests.workbench_acceptance` asks: *did that happen, to this release, on
those machines?* This module is the other half of that pair. The acceptance
reader refuses a report; this one is what a report may be built out of, and it
refuses to build one out of anything that was not observed.

It is deliberately not a test of the application, for the same reason the
acceptance reader is not: what it does is run against the real hosts. It is
also deliberately self-contained -- standard library plus
:mod:`asl_workbench` -- because half of it runs on the Pi, which has no
checkout of this repository and receives this one file beside the unpacked
release. Its rules are proved by ``tests/test_workbench_staging.py``.

Three jobs, and each exists because of a way a staging rehearsal lies:

**Isolation, as a refusal rather than as care.** A rehearsal that reuses one
production identifier is a production change with a rehearsal's name on it.
:func:`staging_site` builds the site the rehearsal runs against and
:func:`check_isolated` refuses it if it shares *any* owned identifier with
:data:`~asl_workbench.integrations.PI_SITE` -- the Serve port, the loopback
port, the published origin, the publisher, the compose marker, the monitor.
The site carries ``deployment="staging"`` and no notification site at all, so
:func:`~asl_workbench.notifications.select_publisher` raises rather than mints
a second publisher for the topic a real phone is subscribed to.

**Native names, restaged rather than reused.** The release names one launch
agent and one unit per user (:data:`~asl_workbench.lifecycle.LABEL`,
:data:`~asl_workbench.lifecycle.UNIT`), because in production there is one
deployment per user per host. A rehearsal is the second one, so
:class:`StagingNativeManager` installs the definition the release produced
under staging names, having first rewritten exactly the two things that carry a
production identity: the launch agent's ``Label`` and the unit's ``ExecStart``.
The rewrite is verified against what the release itself would have written --
:func:`restage_plist` and :func:`restage_unit` refuse content they did not
recognise -- so a definition that changed shape upstream fails here instead of
being silently re-emitted from memory.

The ``ExecStart`` rewrite is the first of the two substantive deviations this
rehearsal makes, and it is recorded rather than hidden: ``asl_workbench.runtime``'s
``main`` binds :data:`~asl_workbench.integrations.PI_SITE`'s fixed
``127.0.0.1:8766``, which the production service is already holding, so the
staging unit starts the same ``runtime.run`` with the staging site passed as
the keyword argument it already accepts. Everything the started process then
does -- the prefix it reads, the release it reports, the lock it takes, the
store it opens, the application it serves, the readiness note the installer
waits on -- is the candidate release's own code.

The second is the staged connector's environment. The release fixes it -- no
inherited variables at all, which is right, because a launch agent starts from
a session nobody chose -- and two facts about a *rehearsal* cannot be spelled
anywhere the release would read them: which certificate terminates a staging
origin that Serve is not allowed to publish, and where the default plan roots
are, given that the real ones hold the operator's actual plans and a rehearsal
that published one of them would not have a fixture corpus.
:func:`staging_environment` names the three variables and
:func:`restage_plist` adds them, refusing any the release already set and any
whose value is not a path under the rehearsal's own state. Nothing about what
the connector does with a frame changes.

**A report that cannot be written by hand.** :class:`Journal` is append-only
and timestamped, and :meth:`Journal.render` refuses to produce a document
unless every check the stage owes was recorded as passed, inside the window
between the release commit and the report, on a named host, with a soak that
actually ran for its full hour in all three phases. The rendered document is
then whatever the acceptance reader will accept, or nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from asl_workbench import lifecycle
from asl_workbench.installation import atomic
from asl_workbench.integrations import PI_SITE, IntegrationSite
from asl_workbench.notifications import DEPLOYMENT_STAGING

# ---------------------------------------------------------------------------
# What the rehearsal is allowed to be

#: The Serve port, the loopback port and the origin the rehearsal publishes on.
#: Chosen beside the production pair rather than over it: 9443 and 8766 belong
#: to the deployed Companion for the whole of this session.
STAGING_PORT = "9444"
STAGING_BIND_PORT = 8767

#: The prefixes, by what each one rehearses. Three on the Pi and two on the
#: Mac, because "install a release" and "migrate the installation that is
#: already there" cannot be rehearsed in the same directory: the second needs a
#: copy of a Companion prefix as its starting state.
STAGING_PREFIX_NAMES: Mapping[str, str] = {
    "service": "asl-workbench-staging",
    "service_rollback": "asl-workbench-staging-rollback",
    "service_migration": "asl-workbench-staging-migration",
    "connector": "asl-workbench-staging",
    "connector_migration": "asl-workbench-staging-migration",
}

#: The prefix of every native name this rehearsal installs under.
STAGING_LABEL_DOMAIN = "com.sglavoie."

#: The prefixes this rehearsal may not touch, by bare name. Both hosts install
#: the Companion under the same one.
PRODUCTION_PREFIX_NAME = "asl-companion"

#: Where the fixture corpus and the rehearsal's own state live on the Mac.
#: Outside every prefix, because they are the rehearsal's rather than any
#: installation's, and removing an installation must not remove the evidence.
STAGING_FIXTURE_NAME = "asl-workbench-staging-fixtures"

#: The environment variable that names a trust store to the connector's
#: ``ssl`` module. The rehearsal's staging origin is terminated by a
#: certificate no public CA signed, so this is how the staged connector is
#: told to trust exactly it -- and, because it replaces the default store
#: rather than adding to it, exactly nothing else.
TRUST_VARIABLE = "SSL_CERT_FILE"

#: The checks session 22 owes, restated here so the Pi half can be driven
#: without this repository. ``tests/test_workbench_staging.py`` pins this to
#: :data:`tests.workbench_acceptance.STAGE_CHECKS`, so a divergence is a test
#: failure rather than a report the acceptance reader then rejects.
STAGING_CHECKS: tuple[str, ...] = (
    "deployed-versions-inspected",
    "isolated-prefixes",
    "isolated-fixtures",
    "fresh-install",
    "migration-from-copied-state",
    "native-restart",
    "rollback",
    "recovery",
    "two-host-detail-reads",
    "two-host-controls",
    "production-routing-unchanged",
    "production-publishing-unchanged",
    "unrelated-service-health",
    "staging-cleanup",
)

#: The soak, and the three phases it is made of.
SOAK_MINIMUM_SECONDS = 3600.0
SOAK_PHASES: tuple[str, ...] = ("active", "idle", "reconnect")

#: The two hosts, and the operating system each one must report.
HOST_ROLES: Mapping[str, str] = {"mac": "Darwin", "pi": "Linux"}

#: Where a check may say it was observed.
CHECK_HOSTS: tuple[str, ...] = (*HOST_ROLES, "both")

SCHEMA_VERSION = 1
STAGE = "staging"
OUTCOME_PASSED = "passed"
PROVENANCE_ACTUAL_HOST = "actual-host"

#: How far a recorded moment may sit outside the window before it is read as
#: wrong rather than as skew between two machines' clocks.
MAX_CLOCK_SKEW_SECONDS = 300.0


class StagingError(AssertionError):
    """A rehearsal step, or a report built out of one, was refused."""


# ---------------------------------------------------------------------------
# The site the rehearsal runs against


def staging_site(site: IntegrationSite = PI_SITE) -> IntegrationSite:
    """The production site with every owned identifier moved aside.

    Everything a resource is *recognised* by changes -- the ports, the origin,
    the publisher, the compose marker, the monitor names -- so an integration
    run against this site cannot find, adopt, rename or remove anything the
    Companion installed. The host does not change, because
    :meth:`~asl_workbench.integrations.Integrations.identity` checks it against
    the machine the command is running on: a staging rehearsal on the Pi is
    still on the Pi.

    ``legacy_monitor_names`` and ``legacy_group_headings`` are emptied rather
    than renamed. They exist so an apply can adopt a resource this application
    installed under an older name, which is exactly the adoption a rehearsal
    must not be able to perform.
    """
    return replace(
        site,
        port=STAGING_PORT,
        bind_port=STAGING_BIND_PORT,
        public_origin=f"https://{site.host}:{STAGING_PORT}",
        publisher_user="asl-workbench-staging",
        publisher_topic="asl-workbench-staging",
        marker="# ASL workbench staging rehearsal v1",
        group_heading="ASL Workbench staging",
        entry_name="ASL staging",
        monitor_name="ASL Workbench staging service health",
        legacy_monitor_names=(),
        legacy_group_headings=(),
        notifications=None,
        deployment=DEPLOYMENT_STAGING,
        # Production's bridge forwards to production's bind port only.
        container_origin=None,
    )


#: The fields of a site that name a resource rather than describe one. A
#: rehearsal that shares one of these with production shares the resource.
OWNED_SITE_FIELDS: tuple[str, ...] = (
    "port",
    "bind_port",
    "public_origin",
    "publisher_user",
    "publisher_topic",
    "marker",
    "group_heading",
    "monitor_name",
)
