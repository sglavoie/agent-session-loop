# ruff: noqa: F401
"""Moving a deployed Companion installation onto this package, and back off it.

Two prefixes already exist on two hosts: a systemd user service on the Pi and a
launch agent on the Mac, both installed by the now-retired Companion lifecycle.
Neither can be re-provisioned. The connector token was handed out once and is stored as
a digest; the database holds months of run history whose ``run_key`` values are
already published in notification links; the incident rows are identities an
operator has seen. So the upgrade is a migration of what is there rather than a
fresh install beside it, and this module is that migration written down.

Five rules, and each one is a way the obvious version of this would lose
something:

* **Nothing is migrated implicitly.** :class:`Migration` is an operator action
  with a :meth:`~Migration.plan` that changes nothing and names every step it
  would take. An install that quietly rewrote the prefix it found would give an
  operator no moment at which to say no.
* **Identities are copied, never recomputed.** The database moves through
  SQLite's own online backup and then through
  :func:`asl_workbench.store_migrations.migrate`, so every ``run_key``, ``plan_key`` and
  ``incident_id`` is the one that was already there. A migration that rebuilt
  them from source data would break every link the Pi has published and split
  every open incident in two.
* **An uncertain action is closed, never repeated.** The connector's journal is
  reconciled by :func:`reconcile_journal` the way a restart reconciles it:
  ``pending`` never ran and becomes ``expired``; anything still open was
  attempted and becomes ``unconfirmed``. Neither carries a reply body or an
  action token, because the journal has no column for either -- which is what
  makes "the migration cannot re-send an answer" a property of the file rather
  than a promise about this code.
* **The whole prefix is backed up, not the database.** The retired snapshot
  covered three files. :data:`BACKUP_MEMBERS` plus the integration receipts is
  what an installation actually consists of, and a restore that recreated the
  database beside receipts describing host resources from another era would be
  a restore of half a system.
* **A restore never revives a revoked credential.** Credential *stores* are the
  one member a backup does not write back: revocation is a decision made after
  the backup was taken, and rolling an application back is not a reason to undo
  it. See :func:`restore`.

The rollback half lives next door in :mod:`asl_workbench.lifecycle`, which owns
the switch: this module supplies :func:`paired_backup` -- which state snapshot
goes with which release -- and :func:`restore_state`, and the lifecycle calls
both inside the same runtime lock that stops the service.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import Any

from .history import checkpoint_document, checkpoints_from_document
from .installation import (
    BACKUPS_DIRECTORY,
    CONFIG_PATH,
    CONNECTOR_TOKEN_PATH,
    DATABASE_FILE,
    DATABASE_PATH,
    INTEGRATION_LOCK,
    LIFECYCLE_LOCK,
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    READY_FILE,
    RECEIPTS_DIRECTORY,
    RUNTIME_LOCK,
    RUNTIME_LOCK_FILE,
    SECRETS_PATH,
    STATE_DIRECTORY,
    SUMMARY_TOKEN_PATH,
    InstallationError,
    atomic,
    document,
    lock,
    private,
    safe_prefix,
    write_json,
)
from .integrations import PI_SITE, IntegrationSite, service_endpoint
from .observation import ObservationPolicy
from .operations import (
    DETAIL_CONNECTOR_RESTARTED,
    OPERATION_ACCEPTED,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
    OPERATION_STATUSES,
    OPERATION_UNCONFIRMED,
    OperationError,
    advance_record,
    persist_operation,
)
from .store_types import StoreError
from .store_workbench import WorkbenchStore

#: The deployment roles, spelled here rather than imported from
#: :mod:`asl_workbench.lifecycle`: the lifecycle imports this module, and a
#: migration that imported it back would close the loop.
ROLE_CONNECTOR = "connector"
ROLE_SERVICE = "service"

# -- What the Companion left behind -----------------------------------------
#
# Paths an already-deployed prefix carries. They are facts about installed
# hosts rather than choices, and the only one that has to change is the
# database: `asl_workbench.installation.DATABASE_FILE` is what every release
# opens, and a release cannot be asked to look for two names.

LEGACY_DATABASE_FILE = "companion.sqlite3"
LEGACY_DATABASE_PATH = f"{STATE_DIRECTORY}/{LEGACY_DATABASE_FILE}"

#: The connector's dispatch journal and its history checkpoint, by the names
#: the Companion gave them. Both keep their names: a rename with no reader on
#: the other side would be churn that an interrupted migration could only make
#: harder to reason about.
JOURNAL_FILE = "operations.json"
JOURNAL_PATH = f"{STATE_DIRECTORY}/{JOURNAL_FILE}"
CHECKPOINT_FILE = "history.json"
CHECKPOINT_PATH = f"{STATE_DIRECTORY}/{CHECKPOINT_FILE}"

#: ``companion/commands.py``'s ``JOURNAL_SCHEMA_VERSION``. Mirrored rather than
#: imported -- this package may not import the Pi's -- and asserted against a
#: journal this package writes back out under the same number, because the
#: Companion is still the application that may have to read it after a
#: rollback.
JOURNAL_SCHEMA_VERSION = 1

#: The legacy connector configuration's fields, and this package's. The two
#: differences are the whole of the connector translation: ``harnesses`` named
#: a Mac-side selection the portable connector no longer reads, and
#: ``target_id`` is what the transport hello is judged against.
LEGACY_CONNECTOR_KEYS: frozenset[str] = frozenset(
    {"role", "settings", "connector_id", "asl", "harnesses", "asl_state", "plan_roots"}
)
CONNECTOR_KEYS: frozenset[str] = frozenset(
    {"role", "settings", "connector_id", "target_id", "asl", "asl_state", "plan_roots"}
)

#: The settings an installed service's configuration is reduced to. Every other
#: key the Companion's ``config.py`` carried described behaviour this package
#: decides elsewhere, and a configuration that kept them would invite an
#: operator to set one and watch nothing happen.
SERVICE_SETTINGS_KEYS: tuple[str, ...] = (
    "owner_identity",
    "observation_only",
    "bind_host",
    "bind_port",
    "public_origin",
)

# -- The migration record ---------------------------------------------------

#: Written before the first durable write and removed after the last one. While
#: it exists the prefix is mid-migration, and :meth:`Migration.recover` is the
#: only thing that should touch it.
MIGRATION_PATH = "migration.json"
MIGRATION_SCHEMA_VERSION = 1

STEP_CONFIGURATION = "configuration"
STEP_CREDENTIALS = "credentials"
STEP_DATABASE = "database"
STEP_CHECKPOINTS = "checkpoints"
STEP_JOURNAL = "journal"
STEP_RECEIPTS = "receipts"

#: In the order they are applied. The database goes first because it is the
#: step with the most to lose, and the receipts last because their step only
#: reads.
MIGRATION_STEPS: tuple[str, ...] = (
    STEP_DATABASE,
    STEP_CONFIGURATION,
    STEP_CREDENTIALS,
    STEP_CHECKPOINTS,
    STEP_JOURNAL,
    STEP_RECEIPTS,
)

# -- Backups ----------------------------------------------------------------

#: One backup directory holds the prefix's members at their prefix-relative
#: paths plus this manifest, which is what turns "a directory appeared" into "a
#: set of files whose bytes are known".
BACKUP_MANIFEST = "manifest.json"
BACKUP_SCHEMA_VERSION = 1

#: Everything a migration touches, plus what a restore of one would be
#: incomplete without. The Companion's own snapshot covered the first three of
#: these; the rest is the expansion this module exists to make -- the issued
#: tokens, the connector's journal and checkpoint, and (added separately,
#: because their names are not fixed) the integration receipts.
BACKUP_MEMBERS: tuple[str, ...] = (
    CONFIG_PATH,
    SECRETS_PATH,
    CONNECTOR_TOKEN_PATH,
    SUMMARY_TOKEN_PATH,
    DATABASE_PATH,
    LEGACY_DATABASE_PATH,
    JOURNAL_PATH,
    CHECKPOINT_PATH,
)

#: Members copied through SQLite rather than as bytes. A file copy of an open
#: database copies a moment that never existed.
DATABASE_MEMBERS: frozenset[str] = frozenset({DATABASE_PATH, LEGACY_DATABASE_PATH})

#: Members a restore leaves alone unless it is told otherwise, and the key that
#: makes one of them a credential store rather than an issued token.
CREDENTIAL_MEMBERS: frozenset[str] = frozenset(
    {SECRETS_PATH, CONNECTOR_TOKEN_PATH, SUMMARY_TOKEN_PATH}
)
CREDENTIAL_STORE_KEY = "credentials"

#: The middle segment that marks a snapshot as data kept *after* an upgrade,
#: set aside by a rollback rather than taken before a switch. It is what keeps
#: :func:`paired_backup` from ever offering post-upgrade data as the state a
#: previous release was paired with.
RETAINED_MARKER = "retained"

#: ``<nanoseconds>-<first twelve of the artifact digest>``, which is how
#: :meth:`asl_workbench.lifecycle.Installation._switch` names the snapshot it
#: takes before it puts a release in place. Matched in full rather than by
#: suffix: ``<nanoseconds>-retained-<digest>`` ends the same way and means the
#: opposite thing.
_ARTIFACT_SEGMENT = 12
_SNAPSHOT_NAME = re.compile(r"(?P<stamp>[0-9]+)-(?P<artifact>[0-9a-f]{1,12})\Z")

#: What one JSON document under the prefix may weigh. The journal and the
#: checkpoint are small by construction -- a bounded number of open operations,
#: one entry per plan directory -- so a large one is a file that is not what it
#: claims to be, and parsing it first is how that is discovered too late.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

#: How many files one backup may hold. The members above plus receipts for a
#: host's worth of integrations, with room to spare.
MAX_BACKUP_MEMBERS = 256


#: The statuses a migration answers for. ``unconfirmed`` is deliberately not
#: among them: it already says "nobody can account for this", and restamping it
#: would replace whatever detail code explained it with this one.
UNRECONCILED_STATUSES: frozenset[str] = frozenset(
    {OPERATION_PENDING, OPERATION_DISPATCHED, OPERATION_ACCEPTED}
)


class MigrationError(InstallationError):
    """A migration, a backup or a restore refused rather than acted."""


# ---------------------------------------------------------------------------
# Pure translations
