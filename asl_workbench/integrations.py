"""Owned host integrations: what to do, kept apart from where it is done.

Four resources on the deployed host belong to this application: a Tailscale
Serve route that publishes the private URL, an ntfy account that may write to
one topic, a Homepage card, and one Uptime Kuma monitor. Each is created on a
host that is full of resources belonging to somebody else, so every operation
here is written to the same four rules:

* **Preview mutates nothing.** ``preview`` reads the host, decides what it
  would do, and writes no file and no lock. An operator reviews the plan before
  anything is touched, and reviewing it must not be the thing that touches it.
* **A receipt precedes the mutation it describes.** :class:`ReceiptStore` holds
  one JSON file per component under the installation prefix, written *before*
  the host command runs. A receipt without the resource is recoverable -- the
  next ``remove`` finds nothing and says so -- while a resource without a
  receipt is an orphan nobody will ever clean up.
* **What was not ours is left exactly as it was.** The route algebra
  (:func:`route_without_owned`, :func:`desired_route`, :func:`own_route`)
  compares everything *except* the owned entry across a mutation, so an
  unrelated Serve route on another port survives an apply and a remove. Homepage
  is restored from the receipt's recorded bytes, not regenerated. The ntfy ACL
  is never reset, only the one owned user is removed.
* **Nothing is adopted.** An ntfy account, a Kuma monitor, or a Homepage block
  that exists without a receipt is somebody else's, and every operation refuses
  rather than take it over. The converse also holds: a resource that *does*
  have a receipt is recognised and reconciled rather than duplicated, which is
  what makes apply repeatable.

**Reusable logic, site-specific configuration.** Everything above is the same
on any host. The hostname, the port, the owned account name, the container
names and the visible card text are not, so they live in
:class:`IntegrationSite` -- data a caller supplies -- and :data:`PI_SITE` is the
one this deployment uses. That split is what lets a staging rehearsal run the
same transactions against its own names, and it is why the ntfy component
refuses outright when the site carries no production notification site: a
rehearsal that could mint a publisher credential would be a second publisher
for the production topic. See :mod:`asl_workbench.notifications` for the other
half of that guard.

**Ownership names never change; visible names do.** The ntfy user, the compose
marker and the Homepage group heading are how an already-installed resource is
recognised, so :data:`PI_SITE` keeps the identifiers the Companion installed --
``asl-companion``, ``# ASL companion integration v1`` -- while the text an
operator reads says Workbench. The one owned name that is also visible is the
Kuma monitor's, so the site carries both the current name and the legacy names:
the adapter matches on any of them and the apply path renames the monitor it
already owns, rather than creating a second one beside it.
"""

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


# -- Host commands ----------------------------------------------------------


class CommandRunner(Protocol):
    """How a host command is run. A seam, so a test never runs ``docker``."""

    def __call__(self, argv: Sequence[str], *, data: bytes | None = None) -> bytes: ...


def run(argv: Sequence[str], *, data: bytes | None = None) -> bytes:
    """Run one host command, passing any secret over stdin rather than argv."""
    result = subprocess.run(
        list(argv),
        input=data,
        stdin=subprocess.DEVNULL if data is None else None,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode:
        # Child output may quote a credential it was handed; only the program
        # name is repeated back.
        raise IntegrationError("integration command failed: " + Path(argv[0]).name)
    return result.stdout


# -- Tailscale Serve --------------------------------------------------------

#: One Serve configuration document, as ``tailscale serve status --json``
#: renders it.
RouteTable = dict[str, Any]


def route_without_owned(site: IntegrationSite, value: RouteTable) -> RouteTable:
    """The Serve configuration with this application's entry taken out.

    Comparing two of these across a mutation is what proves nothing else moved.
    """
    result: RouteTable = copy.deepcopy(value)
    for section, key in (
        ("TCP", site.port),
        ("Web", site.authority),
        ("AllowFunnel", site.authority),
    ):
        if section in result:
            result[section].pop(key, None)
            if not result[section]:
                del result[section]
    return result


def desired_route(site: IntegrationSite, before: RouteTable) -> RouteTable:
    """What the Serve configuration should become, or a refusal.

    Both refusals are about somebody else's use of the port, and they are
    separate because they are different mistakes: an existing owner of exactly
    this port, and a configuration that reaches the port some other way --
    another authority on it, or a funnel that would publish it to the internet.
    """
    if site.port in before.get("TCP", {}) or site.authority in before.get("Web", {}):
        raise IntegrationError(f"port {site.port} already has a Serve owner")
    if any(
        key.endswith(":" + site.port) for key in before.get("Web", {})
    ) or before.get("AllowFunnel", {}).get(site.authority):
        raise IntegrationError(f"port {site.port} is already configured")
    value: RouteTable = copy.deepcopy(before)
    value.setdefault("TCP", {})[site.port] = {"HTTPS": True}
    value.setdefault("Web", {})[site.authority] = {
        "Handlers": {"/": {"Proxy": site.target}}
    }
    return value


def own_route(site: IntegrationSite, value: RouteTable) -> bool:
    """Whether the owned route is present, unmodified, and not funnelled."""
    return (
        value.get("TCP", {}).get(site.port) == {"HTTPS": True}
        and value.get("Web", {}).get(site.authority)
        == {"Handlers": {"/": {"Proxy": site.target}}}
        and not value.get("AllowFunnel", {}).get(site.authority)
    )


# -- Homepage ---------------------------------------------------------------


def homepage_documents(
    site: IntegrationSite, compose: str, services: str, ip: str, token: str
) -> tuple[str, str]:
    """The two Homepage documents with this application's card added.

    The compose edit is one ``extra_hosts`` entry mapping the tailnet name to
    the tailnet address, because the container resolves neither. It is refused
    unless there is exactly one Homepage service and it has no ``extra_hosts``
    of its own: merging into an existing mapping is a decision an operator
    makes, not one a tool makes silently. A site with a ``container_origin``
    gets no compose edit at all: the container never uses the tailnet name,
    and whatever routes it to that origin belongs to the operator.
    """
    if site.marker in compose or site.marker in services:
        raise IntegrationError("unowned Homepage configuration carries our marker")
    if any(heading + ":" in services for heading in site.owned_group_headings):
        raise IntegrationError("unowned Homepage group heading")
    if site.container_origin is None:
        anchor = f"    container_name: {site.homepage_container}\n"
        if compose.count(anchor) != 1 or "extra_hosts:" in compose:
            raise IntegrationError(
                "Homepage compose needs an explicitly reviewed host mapping"
            )
        compose = compose.replace(
            anchor,
            anchor
            + f'    {site.marker}\n    extra_hosts:\n      - "{site.host}:{ip}"\n',
        )
    # A JSON quoted scalar is also a YAML scalar. Only a summary token enters
    # Homepage; it cannot authorize browser reads or connector writes.
    services += f"""\n{site.marker}
- {site.group_heading}:
    - {site.entry_name}:
        icon: {site.entry_icon}
        href: {site.public_origin}
        description: {site.entry_description}
        widget:
          type: customapi
          url: {site.widget_origin}{site.summary_path}
          refreshInterval: 15000
          headers:
            Authorization: {json.dumps("Bearer " + token)}
          mappings:
            - field: reason
              label: Status
              format: text
            - field: freshness.total
              label: Observed
              format: number
            - field: counts.by_state.running
              label: Running
              format: number
"""
    return compose, services


def container_access_checks(
    site: IntegrationSite,
) -> tuple[tuple[str, str | None, int], ...]:
    """The four requests the container makes, and the answer each one must get.

    Named rather than written into the script below, because this is a contract
    between two modules and a contract embedded in a JavaScript string literal
    cannot be asserted against the application that has to honour it.
    ``tests/test_workbench_http.py`` drives every row of this table through
    :class:`~asl_workbench.http.Application`, so a change to the service's
    boundary fails there rather than inside a container at cutover.

    Each row is a different question. A summary credential reads the summary; an
    unaccepted one is told it was not accepted rather than shown a wall; the
    summary credential may not read owner data and is told *that* rather than
    that the route does not exist; and the health route a monitor polls needs no
    credential at all.
    """
    return (
        (site.summary_path, "token", 200),
        (site.summary_path, "invalid", 401),
        (site.owner_only_path, "token", 403),
        (site.health_path, None, 200),
    )


def container_access_script(site: IntegrationSite) -> str:
    """The check the Homepage container itself runs against the service.

    The four requests of :func:`container_access_checks`, made from inside the
    container that will be making them. Verifying from the host would prove
    something about the host's network, which is not the thing that has to work.
    """
    checks = json.dumps([list(row) for row in container_access_checks(site)])
    return (
        "const p=JSON.parse(require('fs').readFileSync(0,'utf8'));\n"
        f"const checks={checks};\n"
        "(async()=>{for(const [path,kind,status] of checks) {\n"
        "const token=kind==='token'?p.token:kind;\n"
        "const r=await fetch(p.origin+path,{headers:token?"
        "{Authorization:'Bearer '+token}:{},signal:AbortSignal.timeout(10000)});\n"
        "if(r.status!==status)throw Error('access verification failed');\n"
        f"if(path==={json.dumps(site.summary_path)}&&r.status===200)"
        "{const d=await r.json();"
        "if(!d.counts||!d.freshness||'runs' in d)"
        "throw Error('summary shape failed');}\n"
        "}console.log('Homepage container access verified');})()"
        ".catch(()=>{process.exitCode=1});"
    )


# -- ntfy access control ----------------------------------------------------


def acl_permissions(acl: str, user: str) -> list[str] | None:
    """The permission lines ``ntfy user list`` prints for one account."""
    blocks = re.split(r"(?m)^user ", acl)
    block = next((item for item in blocks if item.startswith(user + " (")), None)
    if block is None:
        return None
    if not block.startswith(user + " (role: user,"):
        raise IntegrationError("publisher role is unsafe")
    return [line.strip() for line in block.splitlines()[1:] if line.strip()]


def check_anonymous(site: IntegrationSite, acl: str) -> None:
    """Refuse a server where anonymous callers already hold any topic grant.

    The publisher is write-only on one topic, which is only meaningful if the
    topic is not readable by everyone. This is checked on the server as it is,
    and never changed: the ACL is shared with whatever else uses this ntfy.
    """
    anonymous = next(
        (
            block
            for block in re.split(r"(?m)^user ", acl)
            if block.startswith("* (role: anonymous,")
        ),
        "",
    )
    lines = [line.strip() for line in anonymous.splitlines()[1:] if line.strip()]
    if tuple(lines) != EMPTY_ACL_PERMISSIONS:
        raise IntegrationError(
            "ntfy anonymous access must already be default-deny without topic grants"
        )
    del site  # the anonymous rule is the server's, not this site's


def check_publisher(
    site: IntegrationSite,
    acl: str,
    *,
    allow_absent: bool = False,
    allow_empty: bool = False,
) -> None:
    """Refuse a publisher account that is broader than the one topic.

    ``allow_empty`` is for the window during an apply where the account exists
    and its grant has not been made yet; ``allow_absent`` is for a remove that
    finds the account already gone. Neither relaxes what a *broadened* account
    means, which is that somebody repurposed it and it is no longer ours to
    delete.
    """
    permissions = acl_permissions(acl, site.publisher_user)
    if permissions is None:
        if allow_absent:
            return
        raise IntegrationError("publisher account is absent")
    expected = [f"- write-only access to topic {site.publisher_topic}"]
    if permissions in (expected, expected + [EMPTY_ACL_PERMISSIONS[-1]]):
        return
    if allow_empty and tuple(permissions) == EMPTY_ACL_PERMISSIONS:
        return
    raise IntegrationError("publisher permissions differ from the dedicated topic")


# -- Uptime Kuma ------------------------------------------------------------

#: The adapter, run by ``node`` inside the Kuma container with its payload on
#: stdin. Everything host-specific -- the monitor name, the names an installed
#: monitor may still carry, the health URL, the login -- arrives in the payload,
#: so this text is the logic and nothing else. The password arrives the same
#: way and never appears in an argument list or a container's environment.
KUMA_SCRIPT = r"""
const {io}=require('/app/node_modules/socket.io-client');
const p=global.PAYLOAD;
const socket=io('http://127.0.0.1:3001',{transports:['websocket'],reconnection:false,timeout:10000});
const deadline=setTimeout(()=>{socket.disconnect();process.exitCode=1},45000);
let monitors={};
socket.on('monitorList',v=>{monitors=v});
const ready=new Promise(resolve=>socket.once('info',resolve));
const call=(event,...args)=>new Promise((resolve,reject)=>{
 socket.timeout(12000).emit(event,...args,(error,r)=>
  error||!r?.ok?reject(Error('Kuma refused: '+event)):resolve(r));
});
const definition={name:p.name,type:'json-query',
 url:p.url,method:'GET',jsonPath:'status',
 jsonPathOperator:'==',expectedValue:'ok',interval:60,retryInterval:60,
 resendInterval:0,maxretries:2,timeout:10,maxredirects:0,ignoreTls:false,
 notificationIDList:{},saveResponse:false,saveErrorResponse:false,
 accepted_statuscodes:['200'],authMethod:null,headers:'',body:null,
 upsideDown:false,expiryNotification:false,domainExpiryNotification:false,
 parent:null,retryOnlyOnStatusCodeFailure:false,responseMaxLength:1024,
 dns_resolve_type:'A',dns_resolve_server:'',proxyId:null,httpBodyEncoding:'json',
 kafkaProducerBrokers:[],kafkaProducerSaslOptions:{mechanism:'None'},
 rabbitmqNodes:[],conditions:[]};
(async()=>{
 await ready;
 await call('login',{username:p.username,password:p.password});
 await call('getMonitorList');
 // An owned monitor may still carry the name it was installed under, so it is
 // matched on any known name and renamed by the edit below. An unowned one is
 // never adopted, whichever name it carries.
 const matches=Object.values(monitors).filter(m=>p.names.includes(m.name));
 if(matches.length>1||matches.length&&!p.owned)
  throw Error('Monitor ownership conflict');
 const old=matches[0];
 let full=old?(await call('getMonitor',old.id)).monitor:null;
 if(full&&(full.type!==definition.type||full.url!==definition.url))
  throw Error('Monitor repurposed');
 if(p.mode==='remove'){
   if(old)await call('deleteMonitor',old.id);
 }else if(p.mode==='apply'){
   // No notification is attached here. Activation waits for local health, so
   // preparing integration before production cannot generate a false outage.
   let healthy=false;
   try{const r=await fetch(definition.url,{signal:AbortSignal.timeout(5000)});
     healthy=r.ok&&(await r.json()).status==='ok';}catch{}
   if(old)await call('editMonitor',{...full,...definition});
   else {const r=await call('add',{...definition,active:false});
     full={id:r.monitorID};}
   if(healthy)await call('resumeMonitor',full.id);
   else await call('pauseMonitor',full.id);
 }else if(p.mode==='verify'){
   if(!full||!full.active||Object.entries(definition).some(
     ([k,v])=>JSON.stringify(full[k])!==JSON.stringify(v)))
     throw Error('Monitor differs');
 }
 console.log(JSON.stringify({ok:true,monitor:definition.name,mode:p.mode}));
})().catch(error=>{console.error(
  error.message.startsWith('Kuma refused: ')?error.message
  :'Kuma integration refused');process.exitCode=1})
 .finally(()=>{clearTimeout(deadline);socket.disconnect()});
"""


# -- Receipts ---------------------------------------------------------------


class ReceiptStore:
    """One private JSON file per component, saying what this host owns."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        private(self.directory, directory=True)

    def path(self, name: str) -> Path:
        return self.directory / (name + ".json")

    def read(self, name: str) -> dict[str, Any] | None:
        path = self.path(name)
        return document(path) if path.exists() else None

    def save(self, name: str, value: Mapping[str, Any]) -> None:
        write_json(self.path(name), dict(value))

    def discard(self, name: str) -> None:
        self.path(name).unlink()


# -- Service configuration --------------------------------------------------


@dataclass(frozen=True)
class ServiceEndpoint:
    """The part of an installed service's configuration integrations read."""

    owner_identity: str
    bind_host: str
    bind_port: int
    public_origin: str
    #: Whether this deployment serves reads and accepts no mutation. Read here
    #: rather than by a second reader of the same file: the installed runtime
    #: needs it to build its policy, and a service whose configuration says
    #: "observe only" while its policy permits mutation is the one disagreement
    #: this file exists to prevent.
    observation_only: bool = True


def service_endpoint(path: Path, site: IntegrationSite) -> ServiceEndpoint:
    """Read an installed ``runtime.json``, refusing anything but this site.

    An integration that ran against a differently configured service would
    publish a URL to a port nothing is listening on, or hand a Homepage widget
    an origin that resolves somewhere else.
    """
    data = document(path)
    if data.get("role") != "service":
        raise IntegrationError("integration requires an installed service role")
    raw = data.get("settings")
    if not isinstance(raw, dict):
        raise IntegrationError("service configuration has no settings object")
    owner = raw.get("owner_identity")
    observation_only = raw.get("observation_only", True)
    if not isinstance(observation_only, bool):
        raise IntegrationError("observation_only must be a flag")
    endpoint = ServiceEndpoint(
        owner_identity=owner if isinstance(owner, str) else "",
        bind_host=raw.get("bind_host", site.bind_host),
        bind_port=raw.get("bind_port", site.bind_port),
        public_origin=raw.get("public_origin", site.public_origin),
        observation_only=observation_only,
    )
    if (
        not endpoint.owner_identity
        or endpoint.bind_host != site.bind_host
        or endpoint.bind_port != site.bind_port
        or endpoint.public_origin != site.public_origin
    ):
        raise IntegrationError("integration requires the fixed service endpoint")
    return endpoint


def service_secrets(path: Path) -> tuple[CredentialStore, str | None]:
    """Read the service secrets file: the credentials, and the publisher token.

    The two live in one file because they are revoked together, and the file is
    checked for the shape the service itself requires -- separate connector and
    summary credentials -- so an integration cannot be run against a half
    provisioned prefix.
    """
    data = document(path)
    if set(data) - {"credentials", NTFY_TOKEN_KEY} or "credentials" not in data:
        raise IntegrationError("invalid service secrets")
    try:
        store = CredentialStore.from_document(data["credentials"])
    except CredentialError as error:
        raise IntegrationError("invalid service secrets") from error
    roles = {item.role for item in store.credentials() if not item.disabled}
    if not {ROLE_CONNECTOR, ROLE_SUMMARY} <= roles:
        raise IntegrationError(
            "separate connector and summary credentials are required"
        )
    token = data.get(NTFY_TOKEN_KEY)
    if token is not None and (not isinstance(token, str) or not token.strip()):
        raise IntegrationError("invalid publisher credential")
    return store, token


def provision(
    prefix: Path,
    owner: str,
    connector_id: str,
    *,
    site: IntegrationSite = PI_SITE,
    clock: Callable[[], float] = time.time,
) -> None:
    """Create the installation prefix and its credentials, exactly once.

    Repeating it is not an error and is not a re-issue: an existing prefix is
    checked against the same owner and connector id and left alone, because the
    connector token was handed out once and cannot be recovered from the file
    that stores its digest. A *partially* provisioned prefix is refused
    outright -- filling in the missing half would mint a credential beside one
    that may already be in use.
    """
    safe_prefix(prefix)
    if not owner or "@" not in owner or any(ord(c) < 32 for c in owner):
        raise IntegrationError("verified tailnet login required")
    if not _CONNECTOR_ID.fullmatch(connector_id):
        raise IntegrationError("invalid connector id")
    prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock(prefix / PROVISION_LOCK):
        for folder in PREFIX_DIRECTORIES:
            (prefix / folder).mkdir(mode=0o700, exist_ok=True)
        paths = [
            prefix / CONFIG_PATH,
            prefix / SECRETS_PATH,
            prefix / CONNECTOR_TOKEN_PATH,
            prefix / SUMMARY_TOKEN_PATH,
        ]
        if any(path.exists() for path in paths):
            if not all(path.exists() for path in paths):
                raise IntegrationError(
                    "partial provisioning; preserve files and recover explicitly"
                )
            _check_existing_provisioning(paths, owner, connector_id, site)
            return
        connector, connector_token = issue(
            ROLE_CONNECTOR, created_at=clock(), subject=connector_id
        )
        summary, summary_token = issue(
            ROLE_SUMMARY, created_at=clock(), label="Homepage"
        )
        write_json(
            paths[0],
            {
                "role": "service",
                "settings": {"owner_identity": owner, "observation_only": True},
            },
        )
        write_json(
            paths[1],
            {"credentials": CredentialStore([connector, summary]).as_document()},
        )
        write_json(paths[2], {"connector_token": connector_token})
        atomic(paths[3], summary_token.encode("utf-8") + b"\n")


def _check_existing_provisioning(
    paths: Sequence[Path], owner: str, connector_id: str, site: IntegrationSite
) -> None:
    """Refuse an existing prefix that was provisioned for something else."""
    endpoint = service_endpoint(paths[0], site)
    store, _ = service_secrets(paths[1])
    private(paths[3])
    connector = store.authenticate(document(paths[2])["connector_token"])
    summary = store.authenticate(paths[3].read_text(encoding="utf-8").strip())
    if (
        endpoint.owner_identity != owner
        or connector is None
        or connector.role != ROLE_CONNECTOR
        or connector.subject != connector_id
        or summary is None
        or summary.role != ROLE_SUMMARY
    ):
        raise IntegrationError("existing provisioning differs")


# -- The transactions -------------------------------------------------------


@dataclass
class Integrations:
    """The four owned resources on one host, and the operations over them."""

    prefix: Path
    site: IntegrationSite = PI_SITE
    home: Path = field(default_factory=Path.home)
    runner: CommandRunner = run
    tailscale_sudo: bool = False

    def __post_init__(self) -> None:
        self.prefix = Path(self.prefix)
        self.home = Path(self.home)
        safe_prefix(self.prefix)
        self.receipts = ReceiptStore(self.prefix / RECEIPTS_DIRECTORY)
        self.endpoint = service_endpoint(self.prefix / CONFIG_PATH, self.site)
        self.secret_path = self.prefix / SECRETS_PATH
        service_secrets(self.secret_path)
        self.tailscale_command: list[str] = (
            ["sudo", "-n", "tailscale"] if self.tailscale_sudo else ["tailscale"]
        )

    # -- receipts -----------------------------------------------------------

    def receipt(self, name: str) -> dict[str, Any] | None:
        return self.receipts.read(name)

    def save(self, name: str, value: Mapping[str, Any]) -> None:
        self.receipts.save(name, value)

    # -- host reads ---------------------------------------------------------

    def identity(self) -> str:
        """The tailnet address of this host, having checked it is this host."""
        status = json.loads(self.runner(["tailscale", "status", "--json"]))
        own = status["Self"]
        user = status["User"].get(str(own["UserID"]), {})
        if (
            own.get("DNSName", "").rstrip(".") != self.site.host
            or user.get("LoginName") != self.endpoint.owner_identity
        ):
            raise IntegrationError("host identity does not match the configured owner")
        address = next((ip for ip in own["TailscaleIPs"] if ":" not in ip), None)
        if address is None:
            raise IntegrationError("host has no IPv4 tailnet address")
        return address

    def serve_status(self) -> RouteTable:
        value = json.loads(self.runner(["tailscale", "serve", "status", "--json"]))
        return value or {}

    def acl(self) -> str:
        container = self.site.ntfy_container
        return self.runner(
            ["docker", "exec", container, "ntfy", "user", "list"]
        ).decode()

    # -- route --------------------------------------------------------------

    def route(self, mode: str) -> None:
        """The published URL: one Serve route, and nothing else disturbed."""
        site = self.site
        self.identity()
        current = self.serve_status()
        receipt = self.receipt(COMPONENT_ROUTE)
        if mode == MODE_REMOVE:
            self._remove_route(current, receipt)
            return
        if receipt and own_route(site, current):
            return
        if mode == MODE_VERIFY:
            raise IntegrationError("owned Serve route absent")
        wanted = desired_route(site, current)
        # Recheck both listener families through ss on the host itself. A fresh
        # command runs immediately before mutation; Serve also refuses binding.
        listeners = self.runner(["ss", "-H", "-ltn"]).decode()
        if any(re.search(rf":{site.port}\s", line) for line in listeners.splitlines()):
            raise IntegrationError(f"port {site.port} is occupied")
        if mode == MODE_PREVIEW:
            return
        if receipt and receipt["before"] != current:
            raise IntegrationError("Serve changed during interrupted application")
        self.save(COMPONENT_ROUTE, {"before": current, "after": wanted})
        if self.serve_status() != current:
            raise IntegrationError("Serve changed before application")
        self.runner(
            [
                *self.tailscale_command,
                "serve",
                "--bg",
                f"--https={site.port}",
                site.target,
            ]
        )
        after = self.serve_status()
        if not own_route(site, after) or route_without_owned(
            site, after
        ) != route_without_owned(site, current):
            raise IntegrationError("Serve comparison failed; receipt retained")

    def _remove_route(
        self, current: RouteTable, receipt: dict[str, Any] | None
    ) -> None:
        site = self.site
        if not receipt:
            return
        if own_route(site, current):
            self.runner(
                [*self.tailscale_command, "serve", f"--https={site.port}", "off"]
            )
        elif current.get("TCP", {}).get(site.port) or any(
            key.endswith(":" + site.port) for key in current.get("Web", {})
        ):
            raise IntegrationError("Serve route changed since application")
        after = self.serve_status()
        if route_without_owned(site, after) != route_without_owned(
            site, current
        ) or own_route(site, after):
            raise IntegrationError("Serve removal comparison failed")
        self.receipts.discard(COMPONENT_ROUTE)

    # -- homepage -----------------------------------------------------------

    def homepage_paths(self) -> list[Path]:
        return [
            self.home / self.site.homepage_compose,
            self.home / self.site.homepage_services,
        ]

    def homepage(self, mode: str) -> None:
        """The host's own dashboard card, restored byte for byte on removal."""
        receipt = self.receipt(COMPONENT_HOMEPAGE)
        paths = self.homepage_paths()
        for path in paths:
            if (
                any(parent.is_symlink() for parent in (path, *path.parents))
                or path.stat().st_uid != os.getuid()
                or path.stat().st_mode & 0o002
            ):
                raise IntegrationError("unsafe Homepage configuration ownership")
        current = [path.read_bytes() for path in paths]
        if not receipt:
            if mode == MODE_REMOVE:
                return
            receipt = self._homepage_receipt(paths, current)
        before = [base64.b64decode(value) for value in receipt["before"]]
        after = [base64.b64decode(value) for value in receipt["after"]]
        if any(value not in (before[i], after[i]) for i, value in enumerate(current)):
            raise IntegrationError(
                "Homepage changed; refusing to overwrite operator edits"
            )
        if mode == MODE_PREVIEW:
            return
        if mode == MODE_VERIFY:
            if current != after:
                raise IntegrationError("Homepage integration incomplete")
            self.verify_container()
            return
        self.save(COMPONENT_HOMEPAGE, receipt)
        wanted = before if mode == MODE_REMOVE else after
        for index, path in enumerate(paths):
            if path.read_bytes() != current[index]:
                raise IntegrationError("Homepage changed before replacement")
            atomic(path, wanted[index])
            if mode == MODE_REMOVE:
                path.chmod(receipt["modes"][index])
        compose = str(paths[0])
        self.runner(["docker", "compose", "-f", compose, "config", "--quiet"])
        self.runner(
            [
                "docker",
                "compose",
                "-f",
                compose,
                "up",
                "-d",
                "--no-deps",
                self.site.homepage_container,
            ]
        )
        if mode == MODE_REMOVE:
            self.receipts.discard(COMPONENT_HOMEPAGE)

    def _homepage_receipt(
        self, paths: Sequence[Path], current: Sequence[bytes]
    ) -> dict[str, Any]:
        """What the two documents look like before and after, plus their modes.

        The recorded modes are what a removal restores. They are the operator's
        choice for their own files; ours is only that the version carrying a
        credential is private while it is in place.
        """
        token_path = self.prefix / SUMMARY_TOKEN_PATH
        private(token_path)
        token = token_path.read_text(encoding="utf-8").strip()
        store, _ = service_secrets(self.secret_path)
        found = store.authenticate(token)
        if found is None or found.role != ROLE_SUMMARY:
            raise IntegrationError("Homepage token is not summary-only")
        after = [
            text.encode("utf-8")
            for text in homepage_documents(
                self.site,
                current[0].decode("utf-8"),
                current[1].decode("utf-8"),
                self.identity(),
                token,
            )
        ]
        return {
            "before": [base64.b64encode(value).decode("ascii") for value in current],
            "after": [base64.b64encode(value).decode("ascii") for value in after],
            "modes": [path.stat().st_mode & 0o777 for path in paths],
        }

    def verify_container(self) -> None:
        """Prove the card's credential reaches the summary and nothing else."""
        token = (self.prefix / SUMMARY_TOKEN_PATH).read_text(encoding="utf-8").strip()
        # The script through argv is constant and contains no secret. Credential
        # data goes only over stdin, never a container's environment or argv.
        self.runner(
            [
                "docker",
                "exec",
                "-i",
                self.site.homepage_container,
                "node",
                "-e",
                container_access_script(self.site),
            ],
            data=json.dumps({"origin": self.site.widget_origin, "token": token}).encode(
                "utf-8"
            ),
        )

    # -- ntfy ---------------------------------------------------------------

    def ntfy(self, mode: str) -> None:
        """One write-only account on one topic, and its single credential."""
        site = self.site
        site.publishes()
        receipt = self.receipt(COMPONENT_NTFY)
        acl = self.acl()
        present = acl_permissions(acl, site.publisher_user) is not None
        if (
            not receipt
            and mode != MODE_REMOVE
            and document(self.secret_path).get(NTFY_TOKEN_KEY)
        ):
            raise IntegrationError(
                "existing ntfy credential has no integration ownership receipt"
            )
        if not receipt and present:
            raise IntegrationError("ntfy publisher already has an owner")
        check_anonymous(site, acl)
        if mode == MODE_REMOVE:
            self._remove_ntfy(acl, receipt, present)
            return
        if receipt and present:
            check_publisher(site, acl, allow_empty=mode == MODE_APPLY)
        if mode == MODE_PREVIEW:
            return
        if mode == MODE_VERIFY:
            check_publisher(site, acl)
            if not document(self.secret_path).get(NTFY_TOKEN_KEY):
                raise IntegrationError("ntfy token is missing")
            return
        self._apply_ntfy(receipt, present)

    def _apply_ntfy(self, receipt: dict[str, Any] | None, present: bool) -> None:
        site = self.site
        container = site.ntfy_container
        if not receipt:
            self.save(
                COMPONENT_NTFY,
                {"owned_user": site.publisher_user, "phase": PHASE_CREATING},
            )
        if not present:
            # read strips the newline and the generated password is discarded.
            # ntfy receives it in the process environment, never in an argument
            # list on the host or in the container.
            self.runner(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "sh",
                    "-c",
                    "read -r NTFY_PASSWORD; export NTFY_PASSWORD; "
                    f"exec ntfy user add --role=user {site.publisher_user}",
                ],
                data=(secrets.token_urlsafe(48) + "\n").encode("ascii"),
            )
        self.runner(
            [
                "docker",
                "exec",
                container,
                "ntfy",
                "access",
                site.publisher_user,
                site.publisher_topic,
                "write-only",
            ]
        )
        self._mint_token()
        self.save(
            COMPONENT_NTFY,
            {"owned_user": site.publisher_user, "phase": PHASE_COMPLETE},
        )
        check_publisher(site, self.acl())

    def _mint_token(self) -> None:
        """Create the publisher credential, at most once, ever.

        The intent is journalled before the token is created, so an interrupted
        attempt is recognised on the next run. It is not retried: ntfy would
        mint a second credential, and the first one -- which may already have
        been delivered to a running service -- would stay valid and unrecorded.
        """
        site = self.site
        secret = document(self.secret_path)
        if secret.get(NTFY_TOKEN_KEY):
            return
        previous = self.receipt(COMPONENT_NTFY) or {}
        if previous.get("phase") == PHASE_TOKEN_INTENT:
            raise IntegrationError(
                "ambiguous token creation; remove the scoped user before retry"
            )
        self.save(
            COMPONENT_NTFY,
            {"owned_user": site.publisher_user, "phase": PHASE_TOKEN_INTENT},
        )
        output = self.runner(
            [
                "docker",
                "exec",
                site.ntfy_container,
                "ntfy",
                "token",
                "add",
                f"--label={site.publisher_user}",
                site.publisher_user,
            ]
        ).decode()
        tokens = _TOKEN_IN_OUTPUT.findall(output)
        if len(set(tokens)) != 1:
            raise IntegrationError(
                "ntfy token receipt unavailable; remove scoped user before retry"
            )
        secret[NTFY_TOKEN_KEY] = tokens[0]
        write_json(self.secret_path, secret)

    def _remove_ntfy(
        self, acl: str, receipt: dict[str, Any] | None, present: bool
    ) -> None:
        site = self.site
        if not receipt:
            return
        # Refuse a broadened or repurposed account, and never reset shared ACLs.
        check_publisher(site, acl, allow_absent=True, allow_empty=True)
        if present:
            self.runner(
                [
                    "docker",
                    "exec",
                    site.ntfy_container,
                    "ntfy",
                    "user",
                    "del",
                    site.publisher_user,
                ]
            )
        secret = document(self.secret_path)
        secret.pop(NTFY_TOKEN_KEY, None)
        write_json(self.secret_path, secret)
        self.receipts.discard(COMPONENT_NTFY)

    # -- kuma ---------------------------------------------------------------

    def kuma(self, mode: str) -> bytes:
        """The one service-health monitor, driven over a stdin-only adapter."""
        site = self.site
        password_path = self.home / site.kuma_password_file
        private(password_path)
        receipt = self.receipt(COMPONENT_KUMA)
        payload: dict[str, Any] = {
            "mode": mode,
            "owned": bool(receipt),
            "name": site.monitor_name,
            "names": list(site.monitor_names),
            "url": site.target + site.health_path,
            "username": site.monitor_username,
            "password": password_path.read_text(encoding="utf-8").strip(),
        }
        # Validate ownership before journalling intent; a fresh same-name monitor
        # is never adopted. The adapter rechecks again when mutating.
        if mode == MODE_APPLY and not receipt:
            self._kuma_call({**payload, "mode": MODE_PREVIEW})
            self.save(COMPONENT_KUMA, {"name": site.monitor_name})
        result = self._kuma_call(payload)
        if mode == MODE_APPLY:
            # The monitor now carries the current name, whichever it had.
            self.save(COMPONENT_KUMA, {"name": site.monitor_name})
        if mode == MODE_REMOVE and receipt:
            self.receipts.discard(COMPONENT_KUMA)
        return result

    def _kuma_call(self, payload: Mapping[str, Any]) -> bytes:
        return self.runner(
            ["docker", "exec", "-i", self.site.kuma_container, "node", "-"],
            data=(
                "global.PAYLOAD=" + json.dumps(dict(payload)) + ";\n" + KUMA_SCRIPT
            ).encode("utf-8"),
        )

    # -- driving ------------------------------------------------------------

    def selected(self, component: str) -> list[str]:
        if component == COMPONENT_ALL:
            return list(COMPONENTS)
        if component not in COMPONENTS:
            raise IntegrationError(f"unknown component: {component!r}")
        return [component]

    def preview_document(self, component: str) -> list[dict[str, Any]]:
        """What each selected component would do, in terms an operator reviews.

        No credential is read to build this, and none appears in it. Each entry
        says whether it would create a resource or reconcile one this
        installation already owns, which is the distinction a reviewer is
        actually checking.
        """
        site = self.site
        descriptions: dict[str, dict[str, Any]] = {
            COMPONENT_ROUTE: {
                "endpoint": site.public_origin,
                "backend": site.target,
                "owner": self.endpoint.owner_identity,
                "preserve_other_ports": True,
            },
            COMPONENT_NTFY: {
                "user": site.publisher_user,
                "topic": site.publisher_topic,
                "permission": "write-only",
                "publishes_test_message": False,
            },
            COMPONENT_HOMEPAGE: {
                "card": site.group_heading,
                "widget_origin": site.widget_origin,
                "files": [site.homepage_compose, site.homepage_services],
                "token_role": ROLE_SUMMARY,
                "restart": site.homepage_container,
            },
            COMPONENT_KUMA: {
                "monitor": site.monitor_name,
                "renames": list(site.legacy_monitor_names),
                "endpoint": site.target + site.health_path,
                "notifications": "none",
                "per_run_monitors": False,
            },
        }
        return [
            {
                "component": name,
                "action": "reconcile-owned" if self.receipt(name) else "create",
                **descriptions[name],
            }
            for name in self.selected(component)
        ]

    def execute(self, mode: str, component: str) -> None:
        """Run one mode over the selected components, in dependency order."""
        if mode not in MODES:
            raise IntegrationError(f"unknown mode: {mode!r}")
        names = self.selected(component)
        if mode == MODE_REMOVE:
            names.reverse()
        if mode in READ_ONLY_MODES:
            # Preview and verify create no lock file: reviewing a host must not
            # be the thing that mutates it.
            for name in names:
                getattr(self, name)(mode)
            return
        # Mutations serialize through the installation and integration locks.
        # Credential changes additionally require a stopped runtime; Kuma
        # activation must remain possible after the service has started.
        runtime: AbstractContextManager[Any] = (
            lock(self.prefix / RUNTIME_LOCK)
            if COMPONENT_NTFY in names
            else nullcontext()
        )
        with (
            lock(self.prefix / LIFECYCLE_LOCK),
            lock(self.receipts.directory / INTEGRATION_LOCK),
            runtime,
        ):
            for name in names:
                getattr(self, name)(mode)


# -- Operator entry point ---------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asl_workbench.integrations", description=__doc__.splitlines()[0]
    )
    parser.add_argument("mode", choices=("provision", *MODES))
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument(
        "--component", choices=(COMPONENT_ALL, *COMPONENTS), default=COMPONENT_ALL
    )
    parser.add_argument(
        "--tailscale-sudo",
        action="store_true",
        help="Use already authorized noninteractive sudo only for the two "
        "scoped Serve mutations",
    )
    parser.add_argument("--owner")
    parser.add_argument("--connector-id", default="mac-studio")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.mode == "provision":
            provision(args.prefix, args.owner, args.connector_id)
        else:
            integration = Integrations(args.prefix, tailscale_sudo=args.tailscale_sudo)
            integration.execute(args.mode, args.component)
            if args.mode == MODE_PREVIEW:
                print(
                    json.dumps(
                        {"changes": integration.preview_document(args.component)},
                        sort_keys=True,
                    )
                )
        print(json.dumps({"ok": True, "mode": args.mode, "component": args.component}))
    except (
        ValueError,
        RuntimeError,
        OSError,
        KeyError,
        StopIteration,
        InstallationError,
        subprocess.SubprocessError,
    ):
        # Raw exception text may quote a credential read from a parser or a
        # child process, so none of it is printed.
        parser.exit(
            1,
            "Workbench integration refused; inspect the scoped configuration "
            "and the private receipts.\n",
        )
    return 0


__all__ = [
    "COMPONENTS",
    "COMPONENT_ALL",
    "COMPONENT_HOMEPAGE",
    "COMPONENT_KUMA",
    "COMPONENT_NTFY",
    "COMPONENT_ROUTE",
    "EMPTY_ACL_PERMISSIONS",
    "KUMA_SCRIPT",
    "MODES",
    "MODE_APPLY",
    "MODE_PREVIEW",
    "MODE_REMOVE",
    "MODE_VERIFY",
    "PHASE_COMPLETE",
    "PHASE_CREATING",
    "PHASE_TOKEN_INTENT",
    "PI_SITE",
    "READ_ONLY_MODES",
    "CommandRunner",
    "IntegrationError",
    "IntegrationSite",
    "Integrations",
    "ReceiptStore",
    "RouteTable",
    "ServiceEndpoint",
    "acl_permissions",
    "build_parser",
    "check_anonymous",
    "check_publisher",
    "container_access_script",
    "desired_route",
    "homepage_documents",
    "main",
    "own_route",
    "provision",
    "route_without_owned",
    "run",
    "service_endpoint",
    "service_secrets",
]


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
