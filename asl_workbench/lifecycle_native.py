"""Lifecycle native."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

from . import lifecycle_common as common
from . import lifecycle_definitions as definitions
from .installation import (
    CONNECTOR_TOKEN_PATH,
    atomic,
)


class NativeManager:
    """One fixed per-user service, through the manager that owns it."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.domain = f"gui/{os.getuid()}"
        self.destination = Path.home() / (
            "Library/LaunchAgents/" + common.LABEL + ".plist"
            if role == common.ROLE_CONNECTOR
            else ".config/systemd/user/" + common.UNIT
        )

    def check_owner(self, prefix: Path) -> None:
        """Refuse to touch a definition that belongs to another installation.

        A launch agent or a unit under this name that points somewhere else is
        somebody else's service. Stopping it would be an outage nobody asked
        for, and replacing it would be an adoption.
        """
        if not self.destination.exists() and not self.destination.is_symlink():
            return
        if (
            self.destination.is_symlink()
            or self.destination.stat().st_uid != os.getuid()
        ):
            raise common.LifecycleError("native definition is not owned by this user")
        raw = self.destination.read_bytes()
        try:
            if self.role == common.ROLE_CONNECTOR:
                parsed = plistlib.loads(raw)
                arguments = parsed.get("ProgramArguments", [])
                owned = parsed.get("Label") == common.LABEL and str(
                    prefix / CONNECTOR_TOKEN_PATH
                ) in list(arguments)
            else:
                owned = any(
                    line.startswith("ExecStart=")
                    and line.endswith('"--prefix" ' + definitions.quote_unit(prefix))
                    for line in raw.decode("utf-8").splitlines()
                )
        except (ValueError, plistlib.InvalidFileException):
            owned = False
        if not owned:
            raise common.LifecycleError(
                "native definition belongs to another installation"
            )

    def status(self) -> bool:
        if self.role == common.ROLE_CONNECTOR:
            return (
                common.command(
                    [
                        "/bin/launchctl",
                        "print",
                        self.domain + "/" + common.LABEL,
                    ],
                    check=False,
                ).returncode
                == 0
            )
        return (
            common.command(
                ["systemctl", "--user", "is-active", "--quiet", common.UNIT],
                check=False,
            ).returncode
            == 0
        )

    def check_prerequisites(self) -> None:
        if self.role == common.ROLE_CONNECTOR:
            common.command(["/bin/launchctl", "print", self.domain])
            return
        linger = common.command(
            ["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"]
        ).stdout.strip()
        if linger != b"yes":
            raise common.LifecycleError(
                "systemd user service requires linger for boot recovery"
            )

    def start(self, definition_path: Path) -> None:
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        atomic(self.destination, definition_path.read_bytes())
        if self.role == common.ROLE_CONNECTOR:
            common.command(
                ["/bin/launchctl", "bootstrap", self.domain, str(self.destination)]
            )
        else:
            common.command(["systemctl", "--user", "daemon-reload"])
            common.command(["systemctl", "--user", "enable", "--now", common.UNIT])
        if not self.status():
            raise common.LifecycleError("native service did not become active")

    def stop(self) -> None:
        if self.role == common.ROLE_CONNECTOR:
            if self.status():
                common.command(
                    [
                        "/bin/launchctl",
                        "bootout",
                        self.domain + "/" + common.LABEL,
                    ]
                )
                common.await_unloaded(self.status)
            return
        common.command(["systemctl", "--user", "stop", common.UNIT])

    def remove(self) -> None:
        self.stop()
        if self.role == common.ROLE_SERVICE and self.destination.exists():
            common.command(["systemctl", "--user", "disable", common.UNIT])
        self.destination.unlink(missing_ok=True)
        if self.role == common.ROLE_SERVICE:
            common.command(["systemctl", "--user", "daemon-reload"])
