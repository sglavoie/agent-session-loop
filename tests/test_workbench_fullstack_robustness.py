"""Storage failure, malformed input, on-disk privacy and browser storage."""

from __future__ import annotations

import json
import re
import sqlite3
import unittest
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    state,
)
from asl_workbench import connector_payloads as portable_payloads
from asl_workbench import http as workbench_http
from asl_workbench import (
    integrations,
    migration,
    operations,
    runtime,
    transport,
)
from asl_workbench.installation import (
    DATABASE_PATH,
    LOGS_DIRECTORY,
    PRIVATE_FILE_MODE,
)
from asl_workbench.store_types import StoreError
from asl_workbench.store_workbench import WorkbenchStore
from tests.support_workbench_fullstack import (
    CONNECTOR_ID,
    NOW,
    POLICY,
    TARGET_ID,
    Link,
)
from tests.support_workbench_fullstack_case import FullStackCase
from tests.workbench_support import (
    SHELL_OWNER_IDENTITY,
)

ASL_TEST_LANE = "integration"

# ---------------------------------------------------------------------------
# Storage can fail
# ---------------------------------------------------------------------------


class StorageFailureTests(FullStackCase):
    """What the operator is told when the Pi's own disk stops cooperating."""

    def readonly(self) -> None:
        """Make this connection's next write fail, the way a full disk does.

        ``PRAGMA query_only`` rather than a ``chmod``: the connection is already
        open with its write-ahead log already created, so taking the mode off the
        directory underneath it changes nothing SQLite needs to ask for, and the
        write would succeed while the test believed it had broken the disk. This
        makes the engine itself refuse, which is the error the Pi's service has
        to survive whatever the cause on the hardware was.
        """
        self.store.connection.execute("PRAGMA query_only = ON")
        self.addCleanup(self.store.connection.execute, "PRAGMA query_only = OFF")

    def heartbeat(self, at: float) -> Mapping[str, Any]:
        return transport.observation_envelope(
            payload=portable_payloads.heartbeat_payload(
                observed_at=at, bridge_alive=True, source_generation=2
            ),
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            sent_at=at,
        )

    def test_an_unwritable_database_refuses_ingestion_rather_than_lying(self) -> None:
        self.publish()
        self.readonly()
        with self.assertRaises((StoreError, sqlite3.Error)):
            self.store.ingest_heartbeat(
                self.heartbeat(self.clock.now), received_at=self.clock.now
            )

    def test_an_unwritable_database_still_serves_what_it_already_holds(self) -> None:
        # The retained observations are the point of retaining them: an operator
        # whose Pi cannot write must still be able to read what it last heard,
        # because that is the state they are deciding about.
        self.publish()
        rows = len(self.queue_rows())
        self.readonly()
        self.assertEqual(rows, len(self.queue_rows()))

    def test_a_service_over_a_failing_store_still_answers_its_probe(self) -> None:
        self.publish()
        with mock.patch.object(
            WorkbenchStore, "healthy", return_value=False, autospec=True
        ):
            status, body = self.request("/api/health")
        self.assertEqual(503, status)
        self.assertEqual("unavailable", body["status"])

    def test_a_read_that_raises_becomes_a_refusal_and_not_a_crash(self) -> None:
        self.publish()
        with mock.patch.object(
            WorkbenchStore,
            "catalog_states",
            side_effect=StoreError("the database is gone"),
            autospec=True,
        ):
            status, body = self.request("/api/summary")
        self.assertIn(status, {500, 502, 503})
        self.assertIsInstance(body, dict)
        self.assertIn("reason", body)
        # The service is still there. A failing read must not take the port.
        status, _ = self.request("/api/health")
        self.assertEqual(200, status)

    def test_writability_returning_lets_the_next_observation_land(self) -> None:
        self.publish()
        landed = len(self.link.landed)
        self.store.connection.execute("PRAGMA query_only = ON")
        with self.assertRaises((StoreError, sqlite3.Error)):
            self.store.ingest_heartbeat(
                self.heartbeat(self.clock.now), received_at=self.clock.now
            )
        self.store.connection.execute("PRAGMA query_only = OFF")
        self.idle(120.0)
        self.publish()
        # Nothing had to be told the disk came back: the next observation is a
        # whole observation, so recovery is the ordinary path running again.
        self.assertGreater(len(self.link.landed), landed)
        self.assertEqual(len(self.corpus.plan_dirs), len(self.queue_rows()))
        self.assertFalse(self.freshness()["stale"])


# ---------------------------------------------------------------------------
# Input can be malformed
# ---------------------------------------------------------------------------


class MalformedInputTests(FullStackCase):
    """Every refusal here is followed by a good request, on purpose."""

    #: With no mutation lane the service declines a control POST before it reads
    #: the body, so a test about malformed bodies would be testing the lane's
    #: absence. The lane is here to make the refusal be about the input.
    mutations = True

    def test_a_frame_over_the_size_limit_is_refused_before_it_is_parsed(self) -> None:
        oversized = b"{" + b" " * (transport.MAX_FRAME_BYTES + 1) + b"}"
        with self.assertRaises(transport.TransportError):
            transport.validate_frame(
                oversized, direction=transport.DIRECTION_UP, target_id=TARGET_ID
            )

    def test_a_non_finite_number_never_becomes_a_timestamp(self) -> None:
        with self.assertRaises(transport.TransportError):
            transport.validate_frame(
                b'{"protocol":1,"kind":"observation","capability":"observation.read",'
                b'"connector_id":"mac-studio","target_id":"pi-1","sent_at":NaN,'
                b'"payload":{}}',
                direction=transport.DIRECTION_UP,
                target_id=TARGET_ID,
            )

    def test_a_body_that_is_not_json_is_refused_with_a_reason(self) -> None:
        status, body = self.request(
            "/api/control/abort", method="POST", raw=b"not json at all"
        )
        self.assertEqual(400, status)
        self.assertIn("reason", body)

    def test_an_unknown_route_is_a_refusal_rather_than_a_traceback(self) -> None:
        status, body = self.request("/api/invented")
        self.assertEqual(404, status)
        self.assertIn("reason", body)

    def test_a_service_that_refused_malformed_input_still_answers(self) -> None:
        self.publish()
        self.request("/api/control/abort", method="POST", raw=b"\x00\x01\x02")
        self.request("/api/queue?limit=-1")
        self.request("/api/plan")
        status, _ = self.request("/api/summary")
        self.assertEqual(200, status)
        self.assertEqual(len(self.corpus.plan_dirs), len(self.queue_rows()))


# ---------------------------------------------------------------------------
# Nothing private reaches the Pi's disk
# ---------------------------------------------------------------------------

#: Prose planted in the corpus before anything is published. Each string is
#: something only the Mac may hold: an operator's own words, a model's answer,
#: or the layout of a disk a remote service has no business learning.
SENTINELS: tuple[str, ...] = (
    "Should we drop the v1 column before the migration lands",
    "Here is the model's long answer about the v1 column",
    "operator private note about the kiln rollout",
)


class OnDiskPrivacyTests(FullStackCase):
    """Databases, journals, logs and backups, swept for what they may not hold."""

    reads = True

    def setUp(self) -> None:
        super().setUp()
        self.plant()

    def plant(self) -> None:
        """Put each sentinel where the stack will read it, not beside it.

        The manifest is what the catalog scans and the last message is what a
        correlated read carries, so both sentinels are on the path the material
        actually takes. A string dropped in an unread file would prove only that
        the sweep can pass.
        """
        alpha = self.corpus.plan_dir("alpha")
        (alpha / "manifest.md").write_text(
            f"# Alpha\n\n{SENTINELS[2]}\n", encoding="utf-8"
        )
        plan_state = state.plan_state_dir(alpha, self.corpus.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / "last-message.txt").write_text(
            f"{SENTINELS[0]}\n\n{SENTINELS[1]}\n", encoding="utf-8"
        )

    def prohibited(self) -> tuple[str, ...]:
        """Everything the Pi's disk may not contain, in one list.

        The paths are in here with the prose because they are the same kind of
        leak: a remote service may hold the whole catalog and must not learn
        the layout of the disk it describes.
        """
        return (
            *SENTINELS,
            str(self.corpus.plans_root),
            *(str(plan) for plan in self.corpus.plan_dirs),
        )

    def sweep(self, paths: Sequence[Path]) -> None:
        """Fail naming the file and the string, which is what a sweep is for."""
        prohibited = self.prohibited()
        searched = 0
        for path in paths:
            if not path.is_file():
                continue
            searched += 1
            raw = path.read_bytes()
            for needle in prohibited:
                self.assertNotIn(
                    needle.encode("utf-8"),
                    raw,
                    f"{path.name} holds {needle!r}",
                )
        self.assertGreater(searched, 0, "the sweep found nothing to search")

    def database_files(self) -> list[Path]:
        """The database and every sibling SQLite keeps beside it."""
        database = self.pi / "workbench.db"
        return [
            database,
            *(
                database.with_name(database.name + suffix)
                for suffix in ("-wal", "-shm", "-journal")
            ),
        ]

    def test_the_retained_database_holds_no_prose_and_no_path(self) -> None:
        self.publish()
        # An artifact read as well as a publish, because the read carries the
        # very prose the sentinels are: it must pass through and not be kept.
        self.request(f"/api/last-message?plan_key={self.corpus.plan_keys['alpha']}")
        self.request(f"/api/progress?plan_key={self.corpus.plan_keys['alpha']}")
        self.store.connection.execute("PRAGMA wal_checkpoint(FULL)")
        self.sweep(self.database_files())

    def test_a_runtime_log_of_the_whole_session_holds_neither(self) -> None:
        self.publish()
        prefix = self.pi / "installed"
        prefix.mkdir(mode=0o700)
        logger, handler = runtime.build_logger(prefix)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(handler.close)
        # Log what the service logs: the counts and the identities, never the
        # material. If that separation ever breaks, this is where it shows.
        for row in self.queue_rows():
            logger.info("row %s state=%s", row["plan_key"], row["primary_state"])
        logger.info("freshness %s", json.dumps(dict(self.freshness())))
        handler.flush()
        self.sweep(sorted((prefix / LOGS_DIRECTORY).glob("*")))

    def test_a_real_backup_of_the_prefix_carries_nothing_prohibited(self) -> None:
        self.publish()
        prefix = self.pi / "prefix"
        integrations.provision(prefix, SHELL_OWNER_IDENTITY, CONNECTOR_ID)
        target = prefix / DATABASE_PATH
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with WorkbenchStore.open(target, policy=POLICY, created_at=NOW) as installed:
            # Everything the link carried, replayed into a store that lives
            # where a provisioned prefix puts one, so the backup is taken over
            # the real layout rather than over a database in a scratch path.
            replay = Link(installed, clock=self.clock)
            for envelope in self.link.landed:
                if envelope["kind"] == transport.KIND_OBSERVATION:
                    replay.ingest(envelope)
        target.chmod(PRIVATE_FILE_MODE)
        migration.backup(prefix, self.pi / "backups" / "one")
        self.sweep(sorted((self.pi / "backups" / "one").rglob("*")))

    def test_the_journal_a_mutation_writes_names_keys_and_not_prose(self) -> None:
        # The Pi's operation journal is a database table, so this is the same
        # sweep aimed at the row a submission creates: a `reply_ref` is a
        # handle and the prose it redeems stays on the Mac.
        self.publish()
        self.store.record_operation(
            {
                "operation_id": "op-privacy-1",
                "method": "asl.reply",
                "plan_key": self.corpus.plan_keys["alpha"],
                "run_key": self.corpus.run_keys["alpha"],
                "status": operations.OPERATION_PENDING,
                "requested_at": NOW,
                "deliver_by": NOW + 120.0,
                "command_id": "command-1",
            },
            connector_id=CONNECTOR_ID,
            received_at=NOW,
        )
        self.store.connection.execute("PRAGMA wal_checkpoint(FULL)")
        self.sweep(self.database_files())


# ---------------------------------------------------------------------------
# The front end stores only what it declared
# ---------------------------------------------------------------------------

#: Every browser-storage write in the shipped bundle, as it is written.
_STORAGE_WRITE = re.compile(
    r"(?:sessionStorage|localStorage)\.(?:setItem|removeItem)"
    r"\(\s*([A-Za-z0-9_.\[\]\"' ]+?)\s*[,)]"
)
#: The declared key constants, which are the only names a write may use.
_STORAGE_CONSTANT = re.compile(r"export const (\w*STORAGE_KEY\w*) = \"([\w.]+)\";")
#: A name a write may use that is not itself a constant: a parameter a helper
#: was handed one through. ``persistFacet(key, value)`` is the shape.
_THREADED_NAME = re.compile(r"[a-z][A-Za-z0-9]*")


class BrowserStorageTests(unittest.TestCase):
    """What the shell may leave in a browser, and under which names."""

    static: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.static = Path(workbench_http.__file__).resolve().parent / "static"

    def modules(self) -> Iterator[tuple[Path, str]]:
        for path in sorted(self.static.rglob("*.js")):
            yield path, path.read_text(encoding="utf-8")

    def declared(self) -> dict[str, str]:
        source = (self.static / "modules" / "constants.js").read_text(encoding="utf-8")
        return dict(_STORAGE_CONSTANT.findall(source))

    def test_every_storage_key_is_a_declared_constant(self) -> None:
        declared = self.declared()
        self.assertTrue(declared, "constants.js declares no storage keys")
        for path, source in self.modules():
            for name in _STORAGE_WRITE.findall(source):
                expression = name.strip()
                if expression.startswith(("'", '"')):
                    self.fail(f"{path.name} writes a literal storage key: {name}")
                root = expression.split("[")[0].split(".")[0]
                if _THREADED_NAME.fullmatch(root):
                    # A helper writing the key it was passed. The constant is at
                    # the call site, and the literal sweep below is what keeps
                    # that call site from having invented one.
                    continue
                self.assertIn(
                    root,
                    declared,
                    f"{path.name} writes storage key {expression}, "
                    "which constants.js does not declare",
                )

    def test_only_constants_js_spells_a_storage_key_out(self) -> None:
        # The one-place rule the indirection above depends on. A module that
        # spelled `"wb.something"` itself would be a key nothing declared, which
        # is a key nobody can find when an operator asks what the shell keeps.
        for path, source in self.modules():
            if path.name == "constants.js":
                continue
            self.assertNotIn('"wb.', source, f"{path.name} spells out a storage key")

    def test_every_declared_key_lives_under_one_namespace(self) -> None:
        # One prefix, so an operator clearing the shell's storage clears all of
        # it and a key added later cannot collide with another app's.
        for name, value in self.declared().items():
            self.assertTrue(value.startswith("wb."), f"{name} = {value!r}")

    def test_no_module_reaches_for_a_cookie_or_a_database(self) -> None:
        for path, source in self.modules():
            for forbidden in ("document.cookie", "indexedDB", "openDatabase"):
                self.assertNotIn(forbidden, source, f"{path.name} uses {forbidden}")
