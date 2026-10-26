# ruff: noqa: F403, F405
from .migration_backup import *
from .migration_schema import *
from .migration_transforms import *


class Migration:
    """One prefix, moved from the Companion's layout onto this package's.

    Built around a plan that changes nothing, an apply that writes a record
    before its first durable write, and a recover that finishes undoing an
    apply that stopped halfway. The three exist as separate operations because
    a migration is the one lifecycle step whose input is a system somebody has
    been running for months: it has to be describable before it is run, and
    reversible while it is.
    """

    def __init__(
        self,
        prefix: Path,
        *,
        target_id: str | None = None,
        site: IntegrationSite = PI_SITE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.prefix = prefix
        self.target_id = target_id
        self.site = site
        self.clock = clock

    # -- Reading -----------------------------------------------------------

    def role(self) -> str:
        """Which half of the deployment this prefix is, legacy layout or not."""
        try:
            data = read_document(self.prefix / CONFIG_PATH)
        except OSError as error:
            raise MigrationError("prefix carries no configuration") from error
        if not isinstance(data, Mapping):
            raise MigrationError("prefix carries no configuration")
        role = data.get("role")
        if role not in (ROLE_CONNECTOR, ROLE_SERVICE):
            raise MigrationError("prefix is not a Companion or Workbench installation")
        return str(role)

    def plan(self) -> dict[str, Any]:
        """Name every step this migration would take, and take none of them.

        A step is listed when the prefix carries something the step would
        change. A plan with no steps is the normal answer for a prefix that has
        already been migrated, which is what makes running this twice safe.
        """
        safe_prefix(self.prefix)
        now = self.clock()
        role = self.role()
        configuration = read_document(self.prefix / CONFIG_PATH)
        if not isinstance(configuration, Mapping):
            raise MigrationError("prefix carries no configuration")
        steps: list[str] = []
        if (self.prefix / LEGACY_DATABASE_PATH).is_file():
            steps.append(STEP_DATABASE)
        if dict(configuration) != self._migrated_configuration(role, configuration):
            steps.append(STEP_CONFIGURATION)
        if role == ROLE_CONNECTOR and (self.prefix / SECRETS_PATH).is_file():
            steps.append(STEP_CREDENTIALS)
        if self._rewrites(self.prefix / CHECKPOINT_PATH, migrate_checkpoints):
            steps.append(STEP_CHECKPOINTS)
        if self._rewrites(
            self.prefix / JOURNAL_PATH, lambda data: migrate_journal(data, now=now)
        ):
            steps.append(STEP_JOURNAL)
        # Re-reading the receipts changes nothing; it is how a migration finds
        # out that the files it is about to vouch for are still readable. So it
        # accompanies work rather than being work, and a prefix with nothing
        # left to migrate is not given a step for it.
        if steps and members_of(self.prefix, receipts=True) != members_of(
            self.prefix, receipts=False
        ):
            steps.append(STEP_RECEIPTS)
        return {
            "version": MIGRATION_SCHEMA_VERSION,
            "role": role,
            "steps": [step for step in MIGRATION_STEPS if step in steps],
            "members": list(members_of(self.prefix)),
        }

    @staticmethod
    def _rewrites(
        path: Path, migrated: Callable[[Mapping[str, Any]], dict[str, Any]]
    ) -> bool:
        """Whether migrating one document would produce something else.

        Asked instead of "does the file exist", because both documents this is
        used for keep their names across the migration: it is their contents
        that change. Reconciling an already reconciled journal is a fixpoint,
        so the answer settles to no and the migration stops listing a step.
        """
        if not path.is_file():
            return False
        stored = read_document(path)
        if not isinstance(stored, Mapping):
            raise MigrationError(f"expected a JSON object: {path.name}")
        return dict(stored) != migrated(stored)

    def _migrated_configuration(
        self, role: str, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        if role == ROLE_CONNECTOR:
            return migrate_connector_configuration(
                data, target_id=self.target_id or str(data.get("target_id") or "")
            )
        return migrate_service_configuration(data, site=self.site)

    # -- Applying ----------------------------------------------------------

    def apply(self) -> dict[str, Any]:
        """Back the prefix up, then carry out the plan, or put it all back.

        Everything that can refuse refuses before the backup, and everything
        after the migration record is written is either finished or undone by
        :meth:`recover`. The locks are the ones an installation already uses:
        the lifecycle lock so no installer runs beside this, the runtime lock
        so the service is demonstrably not serving, and the integration lock so
        no receipt is written while the receipts are being read.
        """
        safe_prefix(self.prefix)
        if (self.prefix / MIGRATION_PATH).exists():
            raise MigrationError("interrupted migration; run recover before continuing")
        # A connector prefix has no receipts and so may have no directory to
        # take the integration lock in. Creating an empty one is idempotent and
        # is not a migration step: it is where the lock lives.
        (self.prefix / RECEIPTS_DIRECTORY).mkdir(
            exist_ok=True, mode=PRIVATE_DIRECTORY_MODE
        )
        with (
            lock(self.prefix / LIFECYCLE_LOCK),
            lock(self.prefix / RUNTIME_LOCK),
            lock(self.prefix / RECEIPTS_DIRECTORY / INTEGRATION_LOCK),
        ):
            plan = self.plan()
            if not plan["steps"]:
                return {"migrated": False, "steps": [], "backup": None, **plan}
            destination = self.prefix / BACKUPS_DIRECTORY / f"{time.time_ns()}-legacy"
            record = {
                **plan,
                "backup": destination.name,
                "started_at": self.clock(),
            }
            write_json(self.prefix / MIGRATION_PATH, record)
            try:
                backup(self.prefix, destination)
            except BaseException:
                # Nothing has been migrated yet, and a backup that never landed
                # is one neither the unwinding below nor `recover` could read:
                # leaving the record would refuse every later apply and every
                # recover, and would bury why the backup failed.
                (self.prefix / MIGRATION_PATH).unlink(missing_ok=True)
                raise
            try:
                for step in plan["steps"]:
                    getattr(self, f"_{step}")()
                self._verify(plan["role"])
            except BaseException:
                # The same unwinding a later `recover` would do, taken now
                # because this process is still here to do it. Anything else
                # would leave a prefix whose only route back is a command
                # nobody has been told to run.
                self._unwind(destination)
                (self.prefix / MIGRATION_PATH).unlink(missing_ok=True)
                raise
            (self.prefix / MIGRATION_PATH).unlink()
        return {"migrated": True, **record}

    def _database(self) -> None:
        """Move the observations onto the name every release opens.

        Through SQLite's backup API and then through the store's own
        migrations, so the rows -- and therefore every run key, plan key and
        incident id in them -- are the ones that were already there. The legacy
        file is unlinked only after the new one has been opened and checked:
        until then it is the only copy outside the backup.
        """
        source = self.prefix / LEGACY_DATABASE_PATH
        destination = self.prefix / DATABASE_PATH
        if destination.exists():
            raise MigrationError("prefix carries both databases; recover first")
        copy_database(source, destination)
        try:
            with WorkbenchStore.open(
                destination,
                policy=ObservationPolicy(),
                created_at=self.clock(),
            ) as store:
                if not store.healthy():
                    raise MigrationError("migrated store is unhealthy")
        except StoreError as error:
            raise MigrationError("migrated store refused this release") from error
        source.unlink()
        for suffix in ("-wal", "-shm"):
            (self.prefix / (LEGACY_DATABASE_PATH + suffix)).unlink(missing_ok=True)

    def _configuration(self) -> None:
        role = self.role()
        data = read_document(self.prefix / CONFIG_PATH)
        if not isinstance(data, Mapping):
            raise MigrationError("prefix carries no configuration")
        write_json(self.prefix / CONFIG_PATH, self._migrated_configuration(role, data))
        (self.prefix / CONFIG_PATH).chmod(PRIVATE_FILE_MODE)

    def _credentials(self) -> None:
        """Move the connector's issued token to the path this package reads.

        The Companion kept it in ``credentials/secrets.json``, which on a
        service prefix is the credential store. One name for two different
        documents is how a connector prefix ends up being read as a half
        provisioned service, so the connector's token moves and the old file
        goes.
        """
        source = self.prefix / SECRETS_PATH
        data = read_document(source)
        if not isinstance(data, Mapping) or set(data) != {"connector_token"}:
            raise MigrationError("connector secrets carry more than an issued token")
        token = data["connector_token"]
        if not isinstance(token, str) or not token:
            raise MigrationError("connector secrets carry no issued token")
        destination = self.prefix / CONNECTOR_TOKEN_PATH
        if destination.is_file() and read_document(destination) != dict(data):
            raise MigrationError("prefix already carries a different connector token")
        write_json(destination, {"connector_token": token})
        destination.chmod(PRIVATE_FILE_MODE)
        source.unlink()

    def _checkpoints(self) -> None:
        path = self.prefix / CHECKPOINT_PATH
        write_json(path, migrate_checkpoints(read_document(path)))
        path.chmod(PRIVATE_FILE_MODE)

    def _journal(self) -> None:
        path = self.prefix / JOURNAL_PATH
        write_json(path, migrate_journal(read_document(path), now=self.clock()))
        path.chmod(PRIVATE_FILE_MODE)

    def _receipts(self) -> None:
        """Read every receipt, and change none of them.

        The Companion wrote receipts this package's
        :class:`~asl_workbench.integrations.ReceiptStore` reads unchanged, so
        the migration for them is a proof rather than a rewrite: a receipt that
        cannot be read is found now, while the backup is one step away, instead
        of at the first integration run against a host it describes.
        """
        directory = self.prefix / RECEIPTS_DIRECTORY
        private(directory, directory=True)
        for name in members_of(self.prefix):
            if name.startswith(RECEIPTS_DIRECTORY + "/"):
                document(self.prefix / name)

    def _verify(self, role: str) -> None:
        """Read the migrated prefix back through the readers that will use it."""
        if role == ROLE_SERVICE:
            service_endpoint(self.prefix / CONFIG_PATH, self.site)
            return
        data = read_document(self.prefix / CONFIG_PATH)
        if not isinstance(data, Mapping) or set(data) != CONNECTOR_KEYS:
            raise MigrationError("migrated connector configuration is not readable")
        private(self.prefix / CONNECTOR_TOKEN_PATH)

    # -- Recovering --------------------------------------------------------

    def recover(self) -> dict[str, Any]:
        """Undo an interrupted migration from the backup it wrote first.

        Safe to repeat, and safe to run against a prefix with no interrupted
        migration: both answer "nothing to do". Members the migration created
        that the backup does not name are removed, because the point is to
        leave a prefix the Companion can still be started against.
        """
        safe_prefix(self.prefix)
        path = self.prefix / MIGRATION_PATH
        if not path.exists():
            return {"recovered": False}
        record = document(path)
        source = self.prefix / BACKUPS_DIRECTORY / str(record.get("backup", ""))
        if not source.is_dir():
            raise MigrationError("interrupted migration names no usable backup")
        with (
            lock(self.prefix / LIFECYCLE_LOCK),
            lock(self.prefix / RUNTIME_LOCK),
        ):
            report = self._unwind(source)
            path.unlink()
        return {"recovered": True, **report}

    def _unwind(self, source: Path) -> dict[str, list[str]]:
        """Put the backed-up members back, and remove the ones it does not name.

        The removal is the half that is easy to leave out and expensive to have
        left out: a half-finished migration's ``state/workbench.db`` sitting
        beside the restored legacy database is a prefix that neither
        application will start against, and every later attempt would refuse on
        the grounds that it cannot tell which database is the real one.
        """
        known = set(verify(source))
        report = restore(source, self.prefix, credentials=True)
        removed: list[str] = []
        for name in members_of(self.prefix):
            if name not in known:
                (self.prefix / name).unlink()
                for suffix in ("-wal", "-shm") if name in DATABASE_MEMBERS else ():
                    (self.prefix / (name + suffix)).unlink(missing_ok=True)
                removed.append(name)
        return {**report, "removed": removed}
