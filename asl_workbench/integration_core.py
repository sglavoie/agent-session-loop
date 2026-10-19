from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .credentials import (
    ROLE_CONNECTOR,
    ROLE_SUMMARY,
    CredentialError,
    CredentialStore,
    issue,
)
from .installation import (
    CONFIG_PATH,
    CONNECTOR_TOKEN_PATH,
    INTEGRATION_LOCK,
    LIFECYCLE_LOCK,
    PREFIX_DIRECTORIES,
    PROVISION_LOCK,
    RECEIPTS_DIRECTORY,
    RUNTIME_LOCK,
    SECRETS_PATH,
    SUMMARY_TOKEN_PATH,
    InstallationError,
    atomic,
    document,
    lock,
    private,
    safe_prefix,
    write_json,
)
from .notifications import (
    DEPLOYMENT_PRODUCTION,
    PI_NOTIFICATION_SITE,
    NotificationSite,
)

# -- Vocabulary -------------------------------------------------------------

#: The four resources, in the order they are applied: the published URL first,
#: then the things that depend on it. Removal reverses the list, so the card
#: that links to the URL and the monitor that watches the service are withdrawn
#: while the service is still reachable, and the route goes last.
COMPONENT_ROUTE = "route"
COMPONENT_NTFY = "ntfy"
COMPONENT_HOMEPAGE = "homepage"
COMPONENT_KUMA = "kuma"
COMPONENTS: tuple[str, ...] = (
    COMPONENT_ROUTE,
    COMPONENT_NTFY,
    COMPONENT_HOMEPAGE,
    COMPONENT_KUMA,
)
COMPONENT_ALL = "all"

MODE_PREVIEW = "preview"
MODE_APPLY = "apply"
MODE_REMOVE = "remove"
MODE_VERIFY = "verify"
MODES: tuple[str, ...] = (MODE_PREVIEW, MODE_APPLY, MODE_REMOVE, MODE_VERIFY)

#: The modes that only read. Neither takes a lock, and neither writes a file.
READ_ONLY_MODES: tuple[str, ...] = (MODE_PREVIEW, MODE_VERIFY)

#: How long any one host command may take.
COMMAND_TIMEOUT_SECONDS = 60

#: Phases an ntfy receipt passes through. ``token-intent`` is the one that
#: matters: it is journalled before the token is minted, so a crash between the
#: two is detected as ambiguous rather than retried into a second credential.
PHASE_CREATING = "creating"
PHASE_TOKEN_INTENT = "token-intent"
PHASE_COMPLETE = "complete"

#: The key the publisher credential is stored under inside the secrets file.
NTFY_TOKEN_KEY = "ntfy_token"

#: What an ntfy ACL listing says about an account with no topic grants. Both
#: lines, in this order, are what "default deny" looks like in ``ntfy user
#: list`` output.
EMPTY_ACL_PERMISSIONS: tuple[str, ...] = (
    "- no topic-specific permissions",
    "- no access to any (other) topics (server config)",
)

_CONNECTOR_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_TOKEN_IN_OUTPUT = re.compile(r"\btk_[a-zA-Z0-9]+\b")


class IntegrationError(ValueError):
    """An integration refused to act on what it found on the host."""


# -- Site configuration -----------------------------------------------------


@dataclass(frozen=True)
class IntegrationSite:
    """One deployed host, as data. Everything else in this module is logic.

    The fields split three ways, and the split is the point:

    * **Endpoint** -- ``host``, ``port``, ``bind_host``, ``bind_port``,
      ``public_origin``, ``container_origin`` and the three paths. These
      describe a service that already exists and are checked against its
      configuration before anything is applied.
    * **Owned identity** -- ``publisher_user``, ``publisher_topic``, ``marker``,
      ``owned_group_headings`` and ``legacy_monitor_names``. These are how an
      already-installed resource is recognised. Changing one orphans a resource
      this application created.
    * **Visible naming** -- ``group_heading``, ``entry_name``,
      ``entry_description``, ``entry_icon`` and ``monitor_name``. These are read
      by a person and may be updated; the legacy fields above are what keeps
      that safe.
    """

    host: str
    port: str
    bind_host: str
    bind_port: int
    public_origin: str
    summary_path: str
    health_path: str
    owner_only_path: str
    publisher_user: str
    publisher_topic: str
    marker: str
    group_heading: str
    entry_name: str
    entry_description: str
    entry_icon: str
    monitor_name: str
    monitor_username: str
    homepage_container: str
    ntfy_container: str
    kuma_container: str
    homepage_compose: str
    homepage_services: str
    kuma_password_file: str
    legacy_monitor_names: tuple[str, ...] = ()
    legacy_group_headings: tuple[str, ...] = ()
    #: The notification site this host's publisher writes to, or ``None`` for a
    #: deployment that owns no publisher at all.
    notifications: NotificationSite | None = None
    #: Which deployment this is, in :mod:`asl_workbench.notifications`' terms.
    deployment: str = DEPLOYMENT_PRODUCTION
    #: Where the Homepage container reaches the service, when that is not
    #: ``public_origin``. Since Tailscale 1.104 a Serve listener accepts only
    #: connections arriving on ``tailscale0``, so a container on a Docker
    #: bridge is refused at the tailnet address and goes through a bridge the
    #: host's operator owns instead. ``None`` means the container uses
    #: ``public_origin`` through the ``extra_hosts`` mapping written below.
    container_origin: str | None = None

    @property
    def widget_origin(self) -> str:
        """The origin the Homepage container's requests are made to."""
        return self.container_origin or self.public_origin

    @property
    def authority(self) -> str:
        """``host:port``, the key Serve uses for a web handler."""
        return f"{self.host}:{self.port}"

    @property
    def target(self) -> str:
        """The loopback address the published route proxies to."""
        return f"http://{self.bind_host}:{self.bind_port}"

    @property
    def owned_group_headings(self) -> tuple[str, ...]:
        """Every Homepage heading that would mean "already ours, or theirs"."""
        return (self.group_heading, *self.legacy_group_headings)

    @property
    def monitor_names(self) -> tuple[str, ...]:
        """The monitor name now, plus the names an installed one may carry."""
        return (self.monitor_name, *self.legacy_monitor_names)

    @property
    def target_id(self) -> str:
        """The transport identity a connector addresses this deployment by.

        Derived from what this deployment publishes on rather than configured
        beside it. A target id is how a connector proves it meant *this*
        deployment, and a second field that could disagree with the published
        identity would make "is this frame addressed to me?" a question with
        two answers.

        It is the authority and not the host because a rehearsal runs on the
        same machine as the deployment it rehearses -- only the port moves
        aside -- so a host-only identity would make a staging connector's
        uploads addressed to production as well. Two deployments on one host
        are two targets.
        """
        return self.authority

    def publishes(self) -> NotificationSite:
        """The notification site, or a refusal if this host owns no publisher."""
        if self.notifications is None or self.deployment != DEPLOYMENT_PRODUCTION:
            raise IntegrationError(
                "this deployment does not own the production publisher"
            )
        return self.notifications


#: The deployed Pi. Its identifiers are the Companion's, unchanged, because
#: they name resources that already exist on that host; its visible text is the
#: unified application's.
PI_SITE = IntegrationSite(
    host="pi.example.ts.net",
    port="9443",
    bind_host="127.0.0.1",
    bind_port=8766,
    public_origin="https://pi.example.ts.net:9443",
    summary_path="/v1/summary",
    health_path="/v1/health",
    owner_only_path="/v1/runs",
    publisher_user="asl-companion",
    publisher_topic="asl-alerts",
    marker="# ASL companion integration v1",
    group_heading="ASL Workbench",
    entry_name="ASL",
    entry_description="ASL counts and observation freshness",
    entry_icon="mdi-console",
    monitor_name="ASL Workbench service health",
    monitor_username="sglavoie",
    homepage_container="homepage",
    ntfy_container="ntfy",
    kuma_container="uptime-kuma",
    homepage_compose="homepage/compose.yaml",
    homepage_services="homepage/config/services.yaml",
    kuma_password_file="uptime-kuma/secrets/kuma-admin-password",
    legacy_monitor_names=("ASL companion service health",),
    legacy_group_headings=("ASL companion",),
    notifications=PI_NOTIFICATION_SITE,
    deployment=DEPLOYMENT_PRODUCTION,
    # The asl-companion-bridge sidecar in the Pi's homepage/compose.yaml
    # (sglavoie_raspberry_pi), on the docker0 gateway, to 127.0.0.1:8766.
    container_origin="http://host.docker.internal:8768",
)


class _IntegrationOperations:
    """Attributes and hooks supplied by :class:`Integrations` to its mixins."""

    prefix: Path
    site: IntegrationSite
    home: Path
    runner: Callable[..., bytes]
    tailscale_command: list[str]
    receipts: Any
    endpoint: Any
    secret_path: Path

    def receipt(self, name: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def save(self, name: str, value: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def identity(self) -> str:
        raise NotImplementedError

    def serve_status(self) -> dict[str, Any]:
        raise NotImplementedError

    def acl(self) -> str:
        raise NotImplementedError


# Focused operation modules share this vocabulary.  Its static export list
# keeps their runtime and type-checking imports aligned.
# fmt: off
__all__ = ("argparse", "base64", "copy", "json", "os", "re", "secrets", "subprocess", "time", "Callable", "Mapping", "Sequence", "AbstractContextManager", "nullcontext", "dataclass", "field", "Path", "Any", "Protocol", "ROLE_CONNECTOR", "ROLE_SUMMARY", "CredentialError", "CredentialStore", "issue", "CONFIG_PATH", "CONNECTOR_TOKEN_PATH", "INTEGRATION_LOCK", "LIFECYCLE_LOCK", "PREFIX_DIRECTORIES", "PROVISION_LOCK", "RECEIPTS_DIRECTORY", "RUNTIME_LOCK", "SECRETS_PATH", "SUMMARY_TOKEN_PATH", "InstallationError", "atomic", "document", "lock", "private", "safe_prefix", "write_json", "DEPLOYMENT_PRODUCTION", "PI_NOTIFICATION_SITE", "NotificationSite", "COMPONENT_ROUTE", "COMPONENT_NTFY", "COMPONENT_HOMEPAGE", "COMPONENT_KUMA", "COMPONENTS", "COMPONENT_ALL", "MODE_PREVIEW", "MODE_APPLY", "MODE_REMOVE", "MODE_VERIFY", "MODES", "READ_ONLY_MODES", "COMMAND_TIMEOUT_SECONDS", "PHASE_CREATING", "PHASE_TOKEN_INTENT", "PHASE_COMPLETE", "NTFY_TOKEN_KEY", "EMPTY_ACL_PERMISSIONS", "_CONNECTOR_ID", "_TOKEN_IN_OUTPUT", "IntegrationError", "IntegrationSite", "PI_SITE", "_IntegrationOperations")
# fmt: on


# -- Host commands ----------------------------------------------------------
