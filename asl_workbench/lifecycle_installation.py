"""Lifecycle installation."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from . import (
    lifecycle_attachment as attachment,
)
from . import (
    lifecycle_common as common,
)
from . import (
    lifecycle_definitions as definitions,
)
from . import (
    lifecycle_state as state_module,
)
from .installation import (
    BACKUPS_DIRECTORY,
    DEFINITIONS_DIRECTORY,
    DEPLOYMENT_PATH,
    INSTALLED_DIRECTORIES,
    LIFECYCLE_LOCK,
    LOGS_DIRECTORY,
    PRIVATE_DIRECTORY_MODE,
    READY_FILE,
    RELEASES_DIRECTORY,
    RUNTIME_LOCK,
    RUNTIME_LOCK_FILE,
    STATE_DIRECTORY,
    TRANSACTION_PATH,
    atomic,
    lock,
    safe_prefix,
    write_json,
)

# ---------------------------------------------------------------------------
# The installation
from .lifecycle_installation_state import InstallationState
from .migration import (
    Migration,
    paired_backup,
    restore_state,
    retained_name,
)


class Installation(InstallationState):
    """One prefix, and every operation that may change what it is running."""

    # -- Reading -----------------------------------------------------------

    # -- Installing --------------------------------------------------------

    def install(
        self,
        *,
        python: str,
        artifact: Path | None = None,
        digest: str | None = None,
        activate: bool = False,
        upgrade: bool = False,
        dry_run: bool = False,
        restore: Path | None = None,
    ) -> dict[str, Any]:
        """Put one release, or one attachment, in place -- or change nothing.

        Everything that can refuse refuses before the first durable write, and
        everything after the transaction record is written is either completed
        or undone by :meth:`_restore`. There is no state in between that a
        later operation cannot name.

        ``restore`` names the state snapshot this release should run against
        instead of whatever is in the prefix now, and only :meth:`rollback`
        passes it: reinstalling an older application over data a newer one
        migrated is the one way an installation can end up with a pair that was
        never tested together.
        """
        safe_prefix(self.prefix)
        role = attachment.deployment_role(self.prefix)
        raw = self._candidate_bytes(role, artifact, digest)
        old = self.metadata()
        if (self.prefix / TRANSACTION_PATH).exists():
            raise common.LifecycleError(
                "interrupted transaction; run recover before continuing"
            )
        if upgrade and (not old or old.get("removed")):
            raise common.LifecycleError("upgrade requires an installed release")
        if restore is not None and (role != common.ROLE_SERVICE or dry_run):
            # A connector holds no state to pair with, and a rehearsal runs
            # against a copy -- restoring into one would prove nothing and
            # would leave the real prefix's post-upgrade data unretained.
            raise common.LifecycleError(
                "only a service install can restore paired state"
            )
        if old and old["role"] != role:
            raise common.LifecycleError("role cannot change during lifecycle")
        if (
            old
            and not upgrade
            and not old.get("removed")
            and old.get("artifact") != digest
        ):
            raise common.LifecycleError("use upgrade for a different artifact")
        payload = attachment.prerequisites(self.prefix, python, site=self.site)
        active = activate or bool(old and old["active"])
        manager = self.manager_factory(role)
        if active:
            manager.check_owner(self.prefix)
            manager.check_prerequisites()
        if dry_run:
            return self._rehearse(
                old, artifact=artifact, digest=digest, python=python, upgrade=upgrade
            )
        with lock(self.prefix / LIFECYCLE_LOCK):
            # Rechecked under the lock: two installers may have raced their
            # prerequisites and both decided they were upgrading the same thing.
            if self.metadata() != old:
                raise common.LifecycleError("installation changed concurrently")
            for name in INSTALLED_DIRECTORIES:
                (self.prefix / name).mkdir(exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
            release = self._materialize(raw, digest)
            name, content = definitions.definition(self.prefix, release, python, role)
            definition_path = self.prefix / DEFINITIONS_DIRECTORY / name
            with tempfile.TemporaryDirectory(dir=self.prefix) as temp:
                native = Path(temp) / name
                atomic(native, content)
                self.validator(native, role)
            candidate = self._candidate_record(
                old, role, python, digest, payload, active
            )
            write_json(
                self.prefix / TRANSACTION_PATH, {"old": old, "candidate": candidate}
            )
            try:
                if old and old["active"]:
                    manager.stop()
                retained = self._switch(
                    old,
                    candidate,
                    release,
                    python,
                    definition_path,
                    content,
                    restore=restore,
                )
                if active:
                    manager.start(definition_path)
                    self.wait_ready(candidate)
            except BaseException:
                self._restore(old, candidate, manager)
                raise
            (self.prefix / TRANSACTION_PATH).unlink()
            status = self.status()
            return {**status, "retained": retained.name} if retained else status

    def _switch(
        self,
        old: dict[str, Any] | None,
        candidate: dict[str, Any],
        release: Path,
        python: str,
        definition_path: Path,
        content: bytes,
        *,
        restore: Path | None = None,
    ) -> Path | None:
        """Back the state up, prove both releases can read it, then commit.

        Returns where the post-upgrade data was set aside, when a ``restore``
        displaced it. That snapshot is kept under a name
        :func:`~asl_workbench.migration.paired_backup` will never offer as the
        state a release was paired with: it is evidence from a version this
        installation is no longer running, and it is retained rather than
        discarded because a rollback is often the first half of diagnosing why
        the upgrade went wrong.

        The restore happens here, under the runtime lock and after the caller
        has stopped the service, because it is the only moment at which no
        release is reading the state directory.
        """
        state = self.prefix / STATE_DIRECTORY
        retained: Path | None = None
        with lock(self.prefix / RUNTIME_LOCK):
            if candidate["role"] == common.ROLE_SERVICE:
                stamp = time.time_ns()
                if restore is not None:
                    retained = (
                        self.prefix
                        / BACKUPS_DIRECTORY
                        / retained_name(stamp, str((old or {}).get("artifact") or ""))
                    )
                    state_module.snapshot(state, retained)
                    restore_state(state, restore)
                backup = (
                    self.prefix
                    / BACKUPS_DIRECTORY
                    / f"{stamp}-{str(candidate['artifact'])[:12]}"
                )
                state_module.snapshot(state, backup)
                with tempfile.TemporaryDirectory(dir=self.prefix) as temp:
                    trial = Path(temp) / STATE_DIRECTORY
                    state_module.snapshot(backup, trial)
                    state_module.release_check(self.prefix, release, python, trial)
                    if old and not old.get("removed"):
                        state_module.release_check(
                            self.prefix,
                            self.prefix / RELEASES_DIRECTORY / str(old["artifact"]),
                            str(old["python"]),
                            trial,
                        )
                    # Both directions, against the state the incoming release
                    # has already migrated. A downgrade that cannot read it is
                    # a rollback that would have failed after the switch.
                    state_module.release_check(self.prefix, release, python, trial)
                state_module.release_check(self.prefix, release, python, state)
            atomic(definition_path, content)
            (state / READY_FILE).unlink(missing_ok=True)
            write_json(self.prefix / DEPLOYMENT_PATH, candidate)
        return retained

    def _rehearse(
        self,
        old: dict[str, Any] | None,
        *,
        artifact: Path | None,
        digest: str | None,
        python: str,
        upgrade: bool,
    ) -> dict[str, Any]:
        """Run the whole install against a disposable copy of this prefix.

        Identical filesystem work, no service-manager mutation: the rehearsal's
        record says ``active: False``, so nothing it does can reach the running
        installation. What it answers is the question a dry run is actually
        asked -- would this artifact install here, against this state.
        """
        with tempfile.TemporaryDirectory(prefix="workbench-dry-run-") as temp:
            staging = Installation(
                Path(temp).resolve() / "install",
                manager_factory=self.manager_factory,
                validator=self.validator,
                site=self.site,
            )
            staging.prefix.mkdir(mode=PRIVATE_DIRECTORY_MODE)
            for name in ("config", "credentials", RELEASES_DIRECTORY):
                source = self.prefix / name
                if source.is_dir():
                    shutil.copytree(source, staging.prefix / name)
            state_module.snapshot(
                self.prefix / STATE_DIRECTORY, staging.prefix / STATE_DIRECTORY
            )
            if old:
                write_json(staging.prefix / DEPLOYMENT_PATH, {**old, "active": False})
            staging.install(
                artifact=artifact, digest=digest, python=python, upgrade=upgrade
            )
        return {"dry_run": True, "artifact": digest, "activate": False}

    # -- Recovering --------------------------------------------------------

    def recover(self) -> dict[str, Any]:
        """Finish undoing an interrupted transaction. Safe to repeat.

        An interrupted migration is undone first, and outside the lifecycle
        lock because :meth:`~asl_workbench.migration.Migration.recover` takes
        that lock itself. The order is the one the writes happened in: a
        migration that stopped halfway left a prefix no release can be
        installed over, so restoring an application before restoring the layout
        it reads would put a release back on top of a half-moved installation.
        """
        safe_prefix(self.prefix)
        Migration(self.prefix, site=self.site).recover()
        with lock(self.prefix / LIFECYCLE_LOCK):
            path = self.prefix / TRANSACTION_PATH
            if not path.exists():
                return self.status()
            transaction = json.loads(path.read_bytes())
            manager = self.manager_factory(transaction["candidate"]["role"])
            manager.check_owner(self.prefix)
            self._restore(transaction["old"], transaction["candidate"], manager)
        return self.status()

    def rollback(self, *, data: bool = True) -> dict[str, Any]:
        """Reinstall the previous release, against the state it ran on.

        A release and the data it ran against are one pair. Putting the
        application back on its own would hand an older release a database a
        newer one has migrated, which is the case the store is written to
        refuse -- so by default the snapshot taken just before the current
        artifact was installed goes back with it, and the data written since is
        retained under a separate name rather than discarded.

        ``data`` set aside is the escape hatch for the case where that trade is
        wrong: an operator who would rather keep the newer observations and
        risk the older application says so explicitly. It is also what happens
        on its own when no paired snapshot exists, because an installation that
        predates this is an installation nobody snapshotted.

        Credentials are not part of the pair and never move here. Revocation is
        a decision about who may connect *now*, and an application going back a
        version is not a reason to reinstate anybody.
        """
        safe_prefix(self.prefix)
        meta = self.metadata()
        if not meta or not meta.get("previous") or meta.get("removed"):
            raise common.LifecycleError("no previous release")
        previous = str(meta["previous"])
        source = paired_backup(self.prefix, str(meta["artifact"])) if data else None
        result = self.install(
            artifact=self.prefix / RELEASES_DIRECTORY / (previous + ".zip"),
            digest=previous,
            python=str(meta["previous_python"]),
            upgrade=True,
            restore=source,
        )
        return {**result, "restored": source.name if source else None}

    # -- Removing ----------------------------------------------------------

    def remove(
        self, *, reset: bool = False, confirm: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        """Stop and unlink the service; optionally empty what it accumulated.

        Removal keeps configuration and credentials: they were provisioned
        once, the connector token cannot be recovered from the digest that
        stores it, and reinstalling over a removed prefix is the normal way
        back. ``reset`` is the separate, typed decision to discard observations
        -- it requires a removed installation and the prefix spelled out.
        """
        safe_prefix(self.prefix)
        meta = self.metadata()
        if not meta:
            raise common.LifecycleError("no managed installation")
        if (self.prefix / TRANSACTION_PATH).exists():
            raise common.LifecycleError("recover interrupted transaction first")
        if reset and (not meta.get("removed") or confirm != str(self.prefix)):
            raise common.LifecycleError(
                "reset requires removal and --confirm matching the prefix"
            )
        if dry_run:
            return {"dry_run": True, "reset": reset, "credentials_preserved": True}
        with lock(self.prefix / LIFECYCLE_LOCK):
            if self.metadata() != meta:
                raise common.LifecycleError("installation changed concurrently")
            manager = self.manager_factory(meta["role"])
            if meta["active"]:
                manager.check_owner(self.prefix)
                manager.remove()
            with lock(self.prefix / RUNTIME_LOCK):
                if reset:
                    for path in (self.prefix / STATE_DIRECTORY).iterdir():
                        if path.name != RUNTIME_LOCK_FILE:
                            path.unlink()
                    for name in (BACKUPS_DIRECTORY, LOGS_DIRECTORY):
                        shutil.rmtree(self.prefix / name)
                        (self.prefix / name).mkdir(mode=PRIVATE_DIRECTORY_MODE)
                else:
                    for name in (RELEASES_DIRECTORY, DEFINITIONS_DIRECTORY):
                        shutil.rmtree(self.prefix / name)
                        (self.prefix / name).mkdir(mode=PRIVATE_DIRECTORY_MODE)
                meta.update(
                    active=False, removed=True, previous=None, previous_python=None
                )
                write_json(self.prefix / DEPLOYMENT_PATH, meta)
        return self.status()
