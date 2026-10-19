"""Legacy Companion prefixes, credentials, databases, and journal fixtures."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

from asl_workbench import history, migration, store_migrations, store_types
from asl_workbench.credentials import ROLE_CONNECTOR, Credential, CredentialStore, issue
from asl_workbench.installation import (
    CONFIG_PATH,
    PRIVATE_FILE_MODE,
    RECEIPTS_DIRECTORY,
    SECRETS_PATH,
    STATE_DIRECTORY,
    document,
    write_json,
)
from asl_workbench.integrations import PI_SITE, provision
from asl_workbench.operations import (
    DETAIL_CONNECTOR_RESTARTED,
    DETAIL_DEADLINE_PASSED,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
    OPERATION_UNCONFIRMED,
)
from tests.test_workbench_lifecycle import ASL_EXECUTABLE, CONNECTOR_ID, OWNER

#: The connector whose observations the legacy database holds. Not the same
#: string as the connector this prefix would provision: the rows were uploaded
#: by a Mac that has been running for months, and an identity test that used
#: one name for both would pass for the wrong reason.
UPLOADER = "mac-companion"

PLAN_KEY = "plan-3f2a1c"

RUN_KEY = "run-9b8c7d"

ENTRY_ID = "entry-441"

INCIDENT_ID = "incident-88ec"

OPERATION_ID = "operation-0f21"

PROJECT = "Raspberry Pi"

#: What an operator saw the plan called, in the blob the Companion kept it in.
#: It is carried into ``catalog_entry`` by the schema migration, and it is here
#: because "the labels survived" is the difference between a migrated history
#: and a page of runs headed "Unknown project".
PLAN_LABEL = "pi-monitoring"

PLAN_TITLE = "Pi monitoring rollout"


def companion_database(path: Path) -> None:
    """One database at the schema the Companion stopped at, with rows in it.

    Built by replaying this package's migrations and stopping where the
    Companion's ledger stops, so the fixture is the legacy schema rather than a
    hand-copied description of it that could drift from the Pi's.
    """
    with closing(store_migrations.connect(path)) as connection:
        for version, statements in store_migrations.MIGRATIONS:
            if version > store_types.COMPANION_SCHEMA_VERSION:
                break
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version={int(version)}")
        connection.execute(
            "INSERT INTO service_meta (key, value) VALUES (?, ?)",
            (store_types.META_CREATED_AT, json.dumps(1000.0)),
        )
        connection.execute(
            "INSERT INTO service_meta (key, value) VALUES (?, ?)",
            (
                store_types.META_PROJECT_CATALOG_PREFIX + UPLOADER,
                json.dumps(
                    {
                        PLAN_KEY: {
                            "plan": PLAN_LABEL,
                            "title": PLAN_TITLE,
                            "projects": [PROJECT],
                        }
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO connector (connector_id, first_seen_at) VALUES (?, ?)",
            (UPLOADER, 1010.0),
        )
        connection.execute(
            "INSERT INTO run_summary (connector_id, run_key, project, plan_key, "
            "run_id, primary_state, observed_at, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (UPLOADER, RUN_KEY, PROJECT, PLAN_KEY, "run-1", "paused", 1100.0, 1101.0),
        )
        connection.execute(
            "INSERT INTO history_entry (connector_id, entry_id, source_key, "
            "plan_key, run_key, project, outcome, recorded_at, received_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                UPLOADER,
                ENTRY_ID,
                "source-a",
                PLAN_KEY,
                RUN_KEY,
                PROJECT,
                "complete",
                1200.0,
                1201.0,
            ),
        )
        connection.execute(
            "INSERT INTO incident (incident_id, connector_id, run_key, plan_key, "
            "project, kind, detected_at, evidence_at, next_attempt_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                INCIDENT_ID,
                UPLOADER,
                RUN_KEY,
                PLAN_KEY,
                PROJECT,
                "unanswered-question",
                1300.0,
                1290.0,
                1400.0,
            ),
        )
        connection.execute(
            "INSERT INTO operation (operation_id, connector_id, command_id, "
            "method, status, plan_key, run_key, requested_at, deliver_by, "
            "received_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                OPERATION_ID,
                UPLOADER,
                "command-7",
                "asl.reply",
                OPERATION_CONSUMED,
                PLAN_KEY,
                RUN_KEY,
                1250.0,
                1260.0,
                1251.0,
                1261.0,
            ),
        )
    # The Companion's own database is private, and a prefix carrying a
    # world-readable file is refused before any migration step runs.
    path.chmod(PRIVATE_FILE_MODE)


def companion_service_configuration() -> dict[str, Any]:
    """A service ``runtime.json`` as the Companion wrote it.

    ``observation_only`` is absent on purpose: the Companion's default was to
    permit mutations, so its file never had to say so. That absence is the one
    field whose meaning inverts across the upgrade.
    """
    return {
        "role": "service",
        "settings": {
            "owner_identity": OWNER,
            "history_window_days": 7,
            "fixture_mode": False,
        },
    }


def companion_connector_configuration(root: Path) -> dict[str, Any]:
    """A connector ``runtime.json`` as the Companion wrote it."""
    state = root / "asl-state"
    plans = root / "plans"
    for path in (state, plans):
        path.mkdir(mode=0o700, exist_ok=True)
    return {
        "role": "connector",
        "settings": {"public_origin": PI_SITE.public_origin},
        "connector_id": CONNECTOR_ID,
        "asl": ASL_EXECUTABLE,
        "harnesses": ["claude"],
        "asl_state": str(state),
        "plan_roots": [str(plans)],
    }


def companion_journal() -> dict[str, Any]:
    """A dispatch journal with one of every state a migration has to answer for.

    The two open records carry fields the journal has no column for. They are
    not there to be migrated; they are there so that "the reply text did not
    survive" is asserted against a record that had one.
    """
    return {
        "version": migration.JOURNAL_SCHEMA_VERSION,
        "operations": [
            {
                "operation_id": "op-pending",
                "command_id": "command-1",
                "method": "asl.reply",
                "status": OPERATION_PENDING,
                "plan_key": PLAN_KEY,
                "run_key": RUN_KEY,
                "requested_at": 100.0,
                "deliver_by": 400.0,
                "reply": "yes, proceed with the migration",
                "action_token": "token-abc",
            },
            {
                "operation_id": "op-dispatched",
                "command_id": "command-2",
                "method": "asl.reply",
                "status": OPERATION_DISPATCHED,
                "plan_key": PLAN_KEY,
                "run_key": RUN_KEY,
                "requested_at": 110.0,
                "deliver_by": 410.0,
                "dispatched_at": 115.0,
                "body": "the answer nobody may send twice",
            },
            {
                "operation_id": "op-accepted",
                "command_id": "command-3",
                "method": "asl.launch",
                "status": OPERATION_ACCEPTED,
                "plan_key": PLAN_KEY,
                "run_key": RUN_KEY,
                "requested_at": 120.0,
                "deliver_by": 420.0,
            },
            {
                "operation_id": "op-settled",
                "command_id": "command-4",
                "method": "asl.reply",
                "status": OPERATION_EXPIRED,
                "plan_key": PLAN_KEY,
                "run_key": RUN_KEY,
                "requested_at": 130.0,
                "deliver_by": 430.0,
                "settled_at": 431.0,
                "detail_code": DETAIL_DEADLINE_PASSED,
            },
            {
                "operation_id": "op-unconfirmed",
                "command_id": "command-5",
                "method": "asl.reply",
                "status": OPERATION_UNCONFIRMED,
                "plan_key": PLAN_KEY,
                "run_key": RUN_KEY,
                "requested_at": 140.0,
                "deliver_by": 440.0,
                "detail_code": DETAIL_CONNECTOR_RESTARTED,
            },
        ],
    }


def companion_checkpoints() -> dict[str, Any]:
    """A history checkpoint as the Companion wrote it: this one, minus a field.

    Derived from this package's own payload rather than typed out, so the
    fixture stays a *subset* of the current shape even if the current shape
    grows -- which is the property the carry depends on.
    """
    payload = history.SourceCheckpoint(
        source_key="plan-a",
        device=64,
        inode=4242,
        offset=8192,
        head_digest="c0ffee",
        head_length=4096,
        records=17,
        plan_dir="/home/pi/plans/plan-a",
        delivered=("run-0",),
    ).to_payload()
    del payload["open_questions"]
    return {"version": history.CHECKPOINT_SCHEMA_VERSION, "sources": [payload]}


def receipt(kind: str) -> dict[str, Any]:
    """One integration receipt, in the shape a receipt store reads."""
    return {"kind": kind, "owner": OWNER, "resource": f"/srv/{kind}", "claimed_at": 1.0}


def stored_credentials(path: Path) -> list[Credential]:
    """Every credential a service secrets file holds, disabled ones included."""
    store = CredentialStore.from_document(document(path)["credentials"])
    return list(store.credentials())


def write_credentials(path: Path, credentials: list[Credential]) -> None:
    """Put a credential list back into a service secrets file, in place."""
    store = CredentialStore(credentials)
    write_json(path, {**document(path), "credentials": store.as_document()})


def issue_extra(path: Path, *, subject: str = "second-mac") -> str:
    """Issue one more connector credential, and return its id.

    A second one, because the two a prefix is provisioned with cannot be
    disabled without making the installation unusable: withdrawing a pairing is
    something that happens to the third Mac somebody added, not to the service
    itself.
    """
    credential, _ = issue(ROLE_CONNECTOR, created_at=1.0, subject=subject)
    write_credentials(path, [*stored_credentials(path), credential])
    return credential.credential_id


def revoke(path: Path, credential_id: str) -> None:
    """Disable one credential, the way withdrawing a pairing would."""
    write_credentials(
        path,
        [
            replace(item, disabled=True)
            if item.credential_id == credential_id
            else item
            for item in stored_credentials(path)
        ],
    )


def revoke_everything(path: Path) -> None:
    """Disable every credential a file holds."""
    write_credentials(
        path, [replace(item, disabled=True) for item in stored_credentials(path)]
    )


def keys_in(database: Path) -> set[str]:
    """Every run key one database holds, read without migrating it."""
    with closing(sqlite3.connect(database)) as connection:
        return {
            str(row[0]) for row in connection.execute("SELECT run_key FROM run_summary")
        }


class LegacyPrefixTestCase(unittest.TestCase):
    """A prefix in the state the Companion left it in."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="workbench-migrate-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.prefix = self.root / "opt" / "companion"

    def service_prefix(self) -> Path:
        """A provisioned prefix, put back into the Companion's layout.

        Provisioned first because the credentials are the one thing a test
        cannot fabricate: the connector token exists only as a digest, and the
        point of several of these tests is that it survives untouched.
        """
        provision(self.prefix, OWNER, CONNECTOR_ID)
        write_json(self.prefix / CONFIG_PATH, companion_service_configuration())
        companion_database(self.prefix / migration.LEGACY_DATABASE_PATH)
        for name in ("route", "ntfy"):
            write_json(self.prefix / RECEIPTS_DIRECTORY / f"{name}.json", receipt(name))
        return self.prefix

    def connector_prefix(self) -> Path:
        """The Mac's half: a configuration, an issued token, and two files."""
        self.prefix.mkdir(mode=0o700, parents=True)
        for name in ("config", "credentials", STATE_DIRECTORY):
            (self.prefix / name).mkdir(mode=0o700)
        write_json(
            self.prefix / CONFIG_PATH, companion_connector_configuration(self.root)
        )
        write_json(self.prefix / SECRETS_PATH, {"connector_token": "a" * 43})
        write_json(self.prefix / migration.JOURNAL_PATH, companion_journal())
        write_json(self.prefix / migration.CHECKPOINT_PATH, companion_checkpoints())
        return self.prefix

    def migration(self, **options: Any) -> migration.Migration:
        return migration.Migration(self.prefix, **options)
