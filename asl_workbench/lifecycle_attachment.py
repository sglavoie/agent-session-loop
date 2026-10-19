"""Lifecycle attachment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import lifecycle_common as common
from .installation import (
    CONFIG_PATH,
    CONNECTOR_TOKEN_PATH,
    SECRETS_PATH,
    document,
    private,
)
from .integrations import IntegrationSite, service_endpoint, service_secrets
from .observation import ObservationPolicy
from .transport import TransportCompatibility, evaluate_hello

# ---------------------------------------------------------------------------
# Deployment records


def deployment_role(prefix: Path) -> str:
    """Which half of the deployment a provisioned prefix is.

    A directory with no configuration in it is the same answer as one holding a
    configuration for something else: not an installation. Both arrive here as
    a refusal rather than as a missing file, because the operator who typed the
    wrong ``--prefix`` is owed the same sentence either way.
    """
    try:
        role = document(prefix / CONFIG_PATH).get("role")
    except OSError as error:
        raise common.LifecycleError(
            "prefix is not a provisioned Workbench installation"
        ) from error
    if role not in common.DEPLOYMENT_ROLES:
        raise common.LifecycleError(
            "prefix is not a provisioned Workbench installation"
        )
    return str(role)


def connector_settings(prefix: Path) -> dict[str, Any]:
    """Read a connector prefix's configuration, refusing anything unusable.

    Closed, like every other document the installation reads: an unexpected
    field is a configuration written for something else, and acting on the part
    that parsed would attach the Mac to a target nobody named.
    """
    data = document(prefix / CONFIG_PATH)
    expected = {
        "role",
        "settings",
        "connector_id",
        "target_id",
        "asl",
        "asl_state",
        "plan_roots",
    }
    if set(data) != expected:
        raise common.LifecycleError("connector configuration fields differ")
    for name in ("connector_id", "target_id"):
        value = data[name]
        if not isinstance(value, str) or not value or len(value) > 128:
            raise common.LifecycleError(f"invalid {name}")
    if not isinstance(data["plan_roots"], list):
        raise common.LifecycleError("plan_roots must be an array")
    for entry in [data["asl_state"], *data["plan_roots"]]:
        root = Path(str(entry))
        if not root.is_absolute() or not root.is_dir():
            raise common.LifecycleError(
                "ASL state and plan roots must be absolute directories"
            )
    common.executable(str(data["asl"]))
    settings = data["settings"]
    if not isinstance(settings, dict) or not isinstance(
        settings.get("public_origin"), str
    ):
        raise common.LifecycleError("connector configuration needs a public origin")
    if "remote_actions" in settings and not isinstance(
        settings["remote_actions"], bool
    ):
        # Checked rather than coerced: every non-empty string is true, so a
        # configuration reading ``"false"`` would turn acting on somebody's
        # plans on. A permission is a boolean or it is a mistake.
        raise common.LifecycleError("remote_actions must be true or false")
    return data


def attached_root(asl: str) -> Path:
    """The checkout behind an ASL executable.

    Derived the way the executable derives it itself -- the wrapper inserts its
    fourth parent on ``sys.path`` -- and then verified by looking for the
    portable package there, so a path that merely has four parents is not
    mistaken for an installation.
    """
    root = Path(asl).resolve().parents[3]
    if not (root / common.PACKAGE_DIRECTORY / "__init__.py").is_file():
        raise common.LifecycleError(
            "cannot locate the installation behind this executable"
        )
    return root


def attachment(
    settings: dict[str, Any], python: str, *, policy: ObservationPolicy | None = None
) -> dict[str, Any]:
    """What the Mac connector is, recorded as three facts.

    The executable, the revision of the checkout behind it, and what the two
    ends may do with each other. The third is not asserted here: the attached
    installation is asked what it would announce, and
    :func:`asl_workbench.transport.evaluate_hello` -- the same function the
    service applies to a live pairing -- judges it. An attachment recorded as
    compatible by a second implementation of that judgement would be a record
    of nothing.
    """
    asl = str(settings["asl"])
    common.executable(asl)
    root = attached_root(asl)
    environment = common.service_environment(common.ROLE_CONNECTOR, asl=asl)
    revision = (
        common.command(["git", "rev-parse", "HEAD"], cwd=root, env=environment)
        .stdout.decode()
        .strip()
    )
    if not common._REVISION.fullmatch(revision):
        raise common.LifecycleError(
            "the attached installation is not at a known revision"
        )
    dirty = bool(
        common.command(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            env=environment,
        ).stdout.strip()
    )
    # Availability, under the named interpreter. This is where "the Mac runs
    # 3.14" is actually enforced: the wrapper imports the worker runtime, whose
    # grammar an older Python cannot parse, so a mismatch fails here instead of
    # under a launch agent that then restarts forever.
    help_text = common.command(
        [python, asl, "workbench", "connect", "--help"], env=environment
    ).stdout
    if any(flag.encode() not in help_text for flag in common._CONNECT_FLAGS):
        raise common.LifecycleError(
            "executable does not provide the Workbench connector"
        )
    hello = json.loads(
        common.command(
            [
                python,
                "-s",
                "-B",
                "-c",
                common._HELLO_PROBE,
                str(settings["connector_id"]),
                str(settings["target_id"]),
            ],
            cwd=root,
            env=environment,
        ).stdout
    )
    compatibility = evaluate_hello(
        hello,
        policy=policy or ObservationPolicy(),
        target_id=str(settings["target_id"]),
    )
    if not compatibility.compatible:
        raise common.LifecycleError(
            "attached installation is not compatible: "
            + ", ".join(compatibility.reasons)
        )
    return {
        "executable": asl,
        "python": python,
        "revision": revision,
        "dirty": dirty,
        "transport": transport_record(compatibility),
    }


def transport_record(compatibility: TransportCompatibility) -> dict[str, Any]:
    """One verdict, as data a deployment record can carry."""
    return {
        "protocol": compatibility.protocol,
        "contract_set_id": compatibility.contract_set_id,
        "connector_id": compatibility.connector_id,
        "capabilities": sorted(compatibility.granted),
        "reads_allowed": compatibility.reads_allowed,
        "mutations_allowed": compatibility.mutations_allowed,
        "mutation_refusal": compatibility.mutation_refusal,
    }


def prerequisites(
    prefix: Path, python: str, *, site: IntegrationSite
) -> dict[str, Any]:
    """Everything that must be true of the host before anything is written.

    Returns the payload the deployment record will carry for this role: an
    attachment for the connector, and nothing for the service, whose payload is
    the artifact digest the caller already named.
    """
    common.python_check(python)
    role = deployment_role(prefix)
    if role == common.ROLE_SERVICE:
        service_endpoint(prefix / CONFIG_PATH, site)
        service_secrets(prefix / SECRETS_PATH)
        return {}
    settings = connector_settings(prefix)
    private(prefix / CONNECTOR_TOKEN_PATH)
    return {"attachment": attachment(settings, python)}
