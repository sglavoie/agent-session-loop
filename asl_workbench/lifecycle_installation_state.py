"""Lifecycle installation state."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from . import (
    lifecycle_artifacts as artifacts,
)
from . import (
    lifecycle_common as common,
)
from . import (
    lifecycle_definitions as definitions,
)
from . import (
    lifecycle_native as native,
)
from . import (
    lifecycle_state as state,
)
from .installation import (
    DEFINITIONS_DIRECTORY,
    DEPLOYMENT_PATH,
    READY_FILE,
    RELEASES_DIRECTORY,
    RUNTIME_LOCK,
    STATE_DIRECTORY,
    TRANSACTION_PATH,
    InstallationError,
    atomic,
    lock,
    safe_prefix,
    write_json,
)
from .integrations import PI_SITE, IntegrationSite
from .migration import (
    MIGRATION_PATH,
)

# ---------------------------------------------------------------------------
# The installation


class InstallationState:
    """Release metadata, candidate files, readiness, and restoration."""

    def __init__(
        self,
        prefix: Path,
        *,
        manager_factory: Any = native.NativeManager,
        validator: Any = definitions.validate_native,
        site: IntegrationSite = PI_SITE,
    ) -> None:
        self.prefix = prefix
        self.manager_factory = manager_factory
        self.validator = validator
        self.site = site

    def metadata(self) -> dict[str, Any] | None:
        path = self.prefix / DEPLOYMENT_PATH
        return json.loads(path.read_bytes()) if path.exists() else None

    def status(self) -> dict[str, Any]:
        safe_prefix(self.prefix)
        pending = (self.prefix / TRANSACTION_PATH).exists()
        migrating = (self.prefix / MIGRATION_PATH).exists()
        meta = self.metadata()
        if not meta:
            return {
                "installed": False,
                "transaction_pending": pending,
                "migration_pending": migrating,
            }
        return {
            "installed": not meta.get("removed", False),
            "artifact": meta.get("artifact"),
            "attachment": meta.get("attachment"),
            "previous": meta.get("previous"),
            "role": meta["role"],
            "active": self.manager_factory(meta["role"]).status()
            if meta["active"]
            else False,
            "transaction_pending": pending,
            "migration_pending": migrating,
        }

    def _candidate_bytes(
        self, role: str, artifact: Path | None, digest: str | None
    ) -> bytes | None:
        """The artifact this install will unpack, if this role has one."""
        if role == common.ROLE_CONNECTOR:
            if artifact or digest:
                raise common.LifecycleError(
                    "the connector attaches to an installation rather than a package"
                )
            return None
        if not artifact or not digest:
            raise common.LifecycleError(
                "a service release requires an artifact and a SHA-256"
            )
        return artifacts.artifact_bytes(artifact, digest)

    def _materialize(self, raw: bytes | None, digest: str | None) -> Path:
        """Put the release directory in place, or prove the one there is right.

        A content address is a promise about bytes, so reusing one verifies
        them. Trusting the directory name would make an edited release
        indistinguishable from the artifact it was unpacked from.
        """
        if raw is None or digest is None:
            return self.prefix
        release = self.prefix / RELEASES_DIRECTORY / digest
        if not release.exists():
            with tempfile.TemporaryDirectory(
                dir=self.prefix / RELEASES_DIRECTORY
            ) as temp:
                staged = Path(temp) / "candidate"
                artifacts.unpack(raw, staged)
                os.replace(staged, release)
        else:
            with tempfile.TemporaryDirectory(dir=self.prefix) as temp:
                expected = Path(temp) / "release"
                artifacts.unpack(raw, expected)
                installed = {
                    path.relative_to(release)
                    for path in release.rglob("*")
                    if path.is_file()
                }
                candidate = {
                    path.relative_to(expected)
                    for path in expected.rglob("*")
                    if path.is_file()
                }
                if installed != candidate or any(
                    (release / path).read_bytes() != (expected / path).read_bytes()
                    for path in candidate
                ):
                    raise common.LifecycleError("installed release was modified")
        atomic(self.prefix / RELEASES_DIRECTORY / (digest + ".zip"), raw)
        return release

    def _candidate_record(
        self,
        old: dict[str, Any] | None,
        role: str,
        python: str,
        digest: str | None,
        payload: dict[str, Any],
        active: bool,
    ) -> dict[str, Any]:
        """What ``deployment.json`` will say if this install completes.

        ``previous`` is carried only for the service. A connector's previous
        state is a revision of a checkout that has already moved on, so
        recording it would describe a rollback this installation cannot
        perform.
        """
        changed = bool(old) and old is not None and old.get("artifact") != digest
        return {
            "artifact": digest,
            "python": python,
            "role": role,
            "active": active,
            "previous": (
                old.get("artifact")
                if role == common.ROLE_SERVICE and changed and old
                else (old or {}).get("previous")
                if role == common.ROLE_SERVICE
                else None
            ),
            "previous_python": (
                old.get("python")
                if role == common.ROLE_SERVICE and changed and old
                else (old or {}).get("previous_python")
                if role == common.ROLE_SERVICE
                else None
            ),
            "removed": False,
            **payload,
        }

    def wait_ready(self, meta: dict[str, Any], *, timeout: float = 10.0) -> None:
        """Wait for the started release to say it is serving, or refuse.

        Three facts, not one: the note names the release that was started, the
        process it names is alive, and the runtime lock is held -- which is
        what distinguishes a running service from a note left behind by one
        that died. A fixed sleep would report a crash loop as a success.
        """
        if meta["role"] != common.ROLE_SERVICE:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ready = json.loads(
                    (self.prefix / STATE_DIRECTORY / READY_FILE).read_bytes()
                )
                if ready["artifact"] == meta["artifact"]:
                    os.kill(ready["pid"], 0)
                    try:
                        with lock(self.prefix / RUNTIME_LOCK):
                            pass
                    except InstallationError:
                        return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.1)
        raise common.LifecycleError("runtime did not become ready")

    def _restore(
        self, old: dict[str, Any] | None, candidate: dict[str, Any], manager: Any
    ) -> None:
        """Put back whatever was running before this transaction started."""
        if candidate["active"]:
            manager.stop()
            if old and not old["active"]:
                manager.remove()
        if old and not old.get("removed"):
            role = old["role"]
            release = (
                self.prefix / RELEASES_DIRECTORY / str(old["artifact"])
                if role == common.ROLE_SERVICE
                else self.prefix
            )
            with lock(self.prefix / RUNTIME_LOCK):
                if role == common.ROLE_SERVICE:
                    state.release_check(
                        self.prefix,
                        release,
                        str(old["python"]),
                        self.prefix / STATE_DIRECTORY,
                    )
                name, content = definitions.definition(
                    self.prefix, release, str(old["python"]), role
                )
                atomic(self.prefix / DEFINITIONS_DIRECTORY / name, content)
                (self.prefix / STATE_DIRECTORY / READY_FILE).unlink(missing_ok=True)
                write_json(self.prefix / DEPLOYMENT_PATH, old)
            if old["active"]:
                manager.start(self.prefix / DEFINITIONS_DIRECTORY / name)
                self.wait_ready(old)
        else:
            if candidate["active"]:
                manager.remove()
            if old:
                write_json(self.prefix / DEPLOYMENT_PATH, old)
            else:
                (self.prefix / DEPLOYMENT_PATH).unlink(missing_ok=True)
        (self.prefix / TRANSACTION_PATH).unlink(missing_ok=True)
