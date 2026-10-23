"""Lifecycle definitions."""

from __future__ import annotations

import plistlib
import shutil
from pathlib import Path
from typing import Any

from . import lifecycle_attachment as attachment
from . import lifecycle_common as common
from .installation import (
    CONNECTOR_TOKEN_PATH,
    SHARED_MODE_BITS,
)

# ---------------------------------------------------------------------------
# Native service definitions


def quote_unit(value: Any) -> str:
    """Quote one systemd command word, including its own escape characters."""
    return (
        '"'
        + str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        + '"'
    )


def connector_arguments(
    prefix: Path, settings: dict[str, Any], python: str
) -> list[str]:
    """What the launch agent runs: the installed ASL, told where to publish.

    The interpreter is named rather than left to the shebang. A launch agent
    starts with the PATH this module hands it, not the operator's, so
    ``/usr/bin/env python3`` there resolves to whatever the system ships --
    which on this Mac is old enough that the worker runtime is a SyntaxError.
    Naming the interpreter makes the one that was verified the one that runs.

    ``remote_actions`` is the one capability this file decides, because it is
    the one the operator decides: acting on a plan from a phone is opt-in, and
    a connector started without the flag announces itself observation-only and
    is refused a mutating frame by construction rather than by configuration.
    Absent, it stays off -- an installation that never said so gets the safer
    of the two -- and it is spelled in the prefix rather than in the launch
    agent so an operator changes their mind by editing configuration and
    reinstalling, not by hand-editing a generated definition.
    """
    arguments = [
        python,
        str(settings["asl"]),
        "workbench",
        "connect",
        "--target-id",
        str(settings["target_id"]),
        "--connector-id",
        str(settings["connector_id"]),
        "--origin",
        str(settings["settings"]["public_origin"]),
        "--token-file",
        str(prefix / CONNECTOR_TOKEN_PATH),
        "--state-dir",
        str(settings["asl_state"]),
    ]
    for root in settings["plan_roots"]:
        arguments += ["--plan-root", str(root)]
    if settings["settings"].get("remote_actions"):
        arguments.append("--allow-remote-actions")
    return arguments


def service_arguments(prefix: Path, python: str) -> list[str]:
    """What the unit runs: the packaged runtime, told which prefix it serves."""
    return [
        python,
        "-E",
        "-s",
        "-B",
        "-m",
        f"{common.PACKAGE_DIRECTORY}.runtime",
        "--prefix",
        str(prefix),
    ]


def definition(
    prefix: Path, release: Path, python: str, role: str
) -> tuple[str, bytes]:
    """The native definition for one role, as ``(filename, contents)``.

    ``AbandonProcessGroup`` and ``KillMode=control-group`` are the two lines
    that matter and they are deliberately different. The connector spawns runs
    that must outlive it, so its process group is abandoned; the service is
    threads inside one process, so stopping it takes the whole group.
    """
    if role == common.ROLE_CONNECTOR:
        settings = attachment.connector_settings(prefix)
        arguments = connector_arguments(prefix, settings, python)
        environment = common.service_environment(role, asl=str(settings["asl"]))
        return common.LABEL + ".plist", plistlib.dumps(
            {
                "Label": common.LABEL,
                "ProgramArguments": arguments,
                "WorkingDirectory": str(prefix),
                "EnvironmentVariables": environment,
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 10,
                "ExitTimeOut": common.CONNECTOR_EXIT_TIMEOUT_SECONDS,
                "Umask": SHARED_MODE_BITS,
                "AbandonProcessGroup": True,
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            },
            sort_keys=True,
        )
    arguments = service_arguments(prefix, python)
    environment = common.service_environment(role)
    text = "\n".join(
        [
            "[Unit]",
            "Description=ASL Workbench service",
            "StartLimitIntervalSec=60",
            "StartLimitBurst=5",
            "[Service]",
            "Type=simple",
            "ExecStart=" + " ".join(quote_unit(word) for word in arguments),
            "WorkingDirectory=" + str(release).replace("%", "%%"),
            *(
                "Environment=" + quote_unit(key + "=" + value)
                for key, value in environment.items()
            ),
            "Restart=always",
            "RestartSec=10",
            "TimeoutStopSec=30",
            "UMask=0077",
            "NoNewPrivileges=true",
            "KillMode=control-group",
            "StandardOutput=null",
            "StandardError=null",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    return common.UNIT, text.encode("utf-8")


def validate_native(path: Path, role: str) -> None:
    """Ask the native manager's own validator, before installing the file."""
    if role == common.ROLE_CONNECTOR:
        common.command(["/usr/bin/plutil", "-lint", str(path)])
        return
    validator = shutil.which("systemd-analyze")
    if not validator:
        raise common.LifecycleError("systemd-analyze is required on the target host")
    common.command([validator, "--user", "verify", str(path)])
