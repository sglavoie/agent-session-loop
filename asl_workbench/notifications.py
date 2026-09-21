"""The one thing that carries a notification off the host, and who may hold it.

:mod:`asl_workbench.incidents` decides *whether* an incident is worth sending
and records what happened to the attempt; this module is the attempt. The split
matters because the two halves have opposite failure rules: the engine may
retry anything it knows was not sent, and this module may claim that about
almost nothing.

**What a publish attempt establishes.** Once ``request()`` has started, an
error proves nothing: a proxy can fail after its upstream accepted the message.
So only a failure to *establish the connection* answers
:data:`~asl_workbench.incidents.DELIVERY_RETRY`; every other failure -- a
timeout, a 500, an unreadable body, a receipt that names another topic --
answers :data:`~asl_workbench.incidents.DELIVERY_UNCONFIRMED`, which the engine
stores and never replays. A duplicate alert at 3am is cheap; a second run
launched because an alert looked undelivered is not. This is the Companion's
rule carried across unchanged, and :class:`~asl_workbench.incidents.
PublishResult` is imported rather than restated, so there is exactly one
vocabulary for the outcome.

**What a notification may contain.** Identity and a closed reason, never
caller-supplied prose: :data:`NOTIFICATION_TEMPLATES` is keyed by incident
kind, and the only variable parts are a bounded project label and the run key
inside the link. The link itself is the deployment's existing private origin
and the ``#run=`` fragment the shell has always understood, so a notification
sent a year ago still opens the run it names. That is why
:class:`NotificationSite` carries the origin as data and why nothing here
builds one from a request.

**Who may publish.** A production topic has exactly one publisher. The Mac's
local fallback runs the same application against the same incidents, and a
staging rehearsal runs it against a copy of the production database -- either
one holding a real credential would double every alert and would do it with the
production deployment's own receipt. :func:`select_publisher` is therefore the
only supported way to obtain one: it refuses a production site to anything that
is not the production remote deployment, and returns ``None`` -- record the
incident, publish nothing -- rather than quietly downgrading to a second
publisher. :class:`NtfyPublisher` enforces the same rule in its constructor, so
the guard cannot be sidestepped by building one directly.
"""

from __future__ import annotations

import http.client
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlsplit

from .http import MODE_REMOTE
from .incidents import (
    DELIVERY_PUBLISHED,
    DELIVERY_RETRY,
    DELIVERY_UNCONFIRMED,
    INCIDENT_LOST_OBSERVATION,
    INCIDENT_TERMINAL_FAILURE,
    INCIDENT_UNANSWERED_QUESTION,
    PublishResult,
)

# -- Message vocabulary -----------------------------------------------------

#: One closed sentence per incident kind. A notification says which of the
#: three things happened and nothing else: the phone is a doorbell, and the
#: application behind the link is where the detail lives.
NOTIFICATION_TEMPLATES: Mapping[str, str] = {
    INCIDENT_UNANSWERED_QUESTION: "A question is waiting for your answer.",
    INCIDENT_TERMINAL_FAILURE: "An ASL run ended unsuccessfully.",
    INCIDENT_LOST_OBSERVATION: "Observation of active ASL work was lost.",
}

#: What a notification is titled on the phone. Visible naming, so it says
#: Workbench; nothing recognises a resource by it.
NOTIFICATION_TITLE = "ASL Workbench needs attention"

#: The label used when a project name sanitises away to nothing.
FALLBACK_PROJECT_LABEL = "ASL project"

#: How much of a project label a notification carries. Deliberately the
#: Companion's own bound rather than :data:`~asl_workbench.observation.
#: MAX_LABEL_CHARS`: the body below is the text that was already delivered to a
#: phone, and its shape is preserved rather than improved.
MAX_NOTIFICATION_LABEL_CHARS = 160

#: The route a new notification uses before the run fragment.  The query is
#: deliberately stable and ignored by the shell: changing the URL before the
#: fragment forces mobile browsers to navigate when an old Companion tab is
#: still open at this origin.  A hash-only change would reuse that tab and its
#: retired JavaScript, while old ``/#run=`` links remain valid in the router.
RUN_FRAGMENT = "/?workbench=1#run="

#: How long a publish attempt may take end to end.
PUBLISH_TIMEOUT_SECONDS = 10.0

#: The largest receipt body that is read. A larger one is not truncated and
#: parsed, it is treated as an outcome this publisher cannot confirm.
MAX_RECEIPT_BYTES = 4096

_RECEIPT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,512}")
_TOPIC = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")


class NotificationError(ValueError):
    """A site, a credential, or a publisher holder that was refused."""


# -- Deployments ------------------------------------------------------------

#: The deployment whose publisher the production topic belongs to.
DEPLOYMENT_PRODUCTION = "production"
#: A rehearsal against copied state. It may hold a database and a service; it
#: may not hold the production publisher.
DEPLOYMENT_STAGING = "staging"
#: A developer's own machine, including the Mac's local fallback.
DEPLOYMENT_LOCAL = "local"

DEPLOYMENTS: tuple[str, ...] = (
    DEPLOYMENT_PRODUCTION,
    DEPLOYMENT_STAGING,
    DEPLOYMENT_LOCAL,
)


@dataclass(frozen=True)
class NotificationSite:
    """Where notifications go, as data rather than as a constant in the code.

    ``production`` is not decoration. It marks the topic that a real phone is
    subscribed to, and it is what :func:`authorize_publisher` checks against the
    deployment asking to publish.
    """

    host: str
    topic: str
    private_origin: str
    title: str = NOTIFICATION_TITLE
    production: bool = False

    def __post_init__(self) -> None:
        if not _HOST.fullmatch(self.host):
            raise NotificationError("invalid notification host")
        if not _TOPIC.fullmatch(self.topic):
            raise NotificationError("invalid notification topic")
        origin = urlsplit(self.private_origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise NotificationError("private_origin must be a bare HTTPS origin")
        if not self.title.strip() or not self.title.isprintable():
            raise NotificationError("invalid notification title")

    @property
    def path(self) -> str:
        """The request target one message is posted to."""
        return "/" + self.topic


#: The deployed Pi's notification site: the existing host, the existing topic,
#: and the existing private origin. None of the three may change without
#: breaking either the phone's subscription or every link already delivered.
PI_NOTIFICATION_SITE = NotificationSite(
    host="ntfy.sglavoie.com",
    topic="asl-alerts",
    private_origin="https://pi.example.ts.net:9443",
    production=True,
)


def authorize_publisher(deployment: str, site: NotificationSite) -> None:
    """Refuse a production site to anything but the production deployment."""
    if deployment not in DEPLOYMENTS:
        raise NotificationError(f"unknown deployment: {deployment!r}")
    if site.production and deployment != DEPLOYMENT_PRODUCTION:
        raise NotificationError(
            "only the production deployment may publish to the production topic"
        )


# -- Messages ---------------------------------------------------------------


def project_label(project: str) -> str:
    """Flatten a project name to one printable line, bounded."""
    printable = "".join(
        character if character.isprintable() else " " for character in project
    )
    return " ".join(printable.split())[:MAX_NOTIFICATION_LABEL_CHARS]


def notification_link(
    run_key: str, *, site: NotificationSite, focus_reply: bool = False
) -> str:
    """The deep link one notification carries back to the run it is about.

    Historic callers keep the bare ``#run=`` form.  A new unanswered-question
    alert names the reply focus as well, so resolving the run scrolls to the
    live question rather than leaving the correction composer as the first
    actionable control on a phone screen.
    """
    link = site.private_origin + RUN_FRAGMENT + quote(run_key, safe="")
    if focus_reply:
        link += "&focus=reply"
    return link


def notification(
    *, kind: str, project: str, run_key: str, site: NotificationSite
) -> tuple[bytes, str]:
    """The body and the link for one incident, from identity and a kind alone."""
    if kind not in NOTIFICATION_TEMPLATES:
        raise NotificationError(f"no notification template for kind: {kind!r}")
    reason = NOTIFICATION_TEMPLATES[kind]
    label = project_label(project) or FALLBACK_PROJECT_LABEL
    link = notification_link(
        run_key,
        site=site,
        focus_reply=kind == INCIDENT_UNANSWERED_QUESTION,
    )
    return f"{label}: {reason}\n{link}".encode(), link


# -- Transport --------------------------------------------------------------


class PublishResponse(Protocol):
    """The part of an HTTP response this module reads."""

    @property
    def status(self) -> int: ...

    def read(self, amt: int) -> bytes: ...


class PublishConnection(Protocol):
    """The part of a connection this module drives.

    A protocol rather than :class:`http.client.HTTPSConnection` so a test can
    supply one without patching a module attribute, and so the four outcomes
    below can be provoked exactly rather than approximately.
    """

    def connect(self) -> None: ...

    def request(
        self, method: str, url: str, body: bytes, headers: Mapping[str, str]
    ) -> None: ...

    def getresponse(self) -> PublishResponse: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[], PublishConnection]


def https_connection_factory(site: NotificationSite) -> ConnectionFactory:
    """A factory for real HTTPS connections to ``site``.

    ``http.client`` is used directly and on purpose: it does not follow
    redirects, does not consult proxy environment variables, and does not retry
    a POST. Each of those would turn one attempt into an unknown number of them.
    """

    def factory() -> PublishConnection:
        return http.client.HTTPSConnection(site.host, timeout=PUBLISH_TIMEOUT_SECONDS)

    return factory


class NtfyPublisher:
    """A publish-only credential pointed at one topic on one host.

    The endpoint, the topic and the message templates cannot be supplied per
    call: a publisher that accepted a URL and a body would be a way to send
    arbitrary text from the service's credential. No credential, response body,
    or remote error text is returned, logged, or stored -- the outcome is one of
    three words, and the words are what the incident row records.
    """

    def __init__(
        self,
        token: str,
        *,
        site: NotificationSite = PI_NOTIFICATION_SITE,
        deployment: str = DEPLOYMENT_PRODUCTION,
        connect: ConnectionFactory | None = None,
    ) -> None:
        authorize_publisher(deployment, site)
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise NotificationError("invalid ntfy credential")
        self._token = token
        self.site = site
        self.deployment = deployment
        self._connect = (
            connect if connect is not None else https_connection_factory(site)
        )

    def publish(
        self,
        *,
        kind: str,
        project: str,
        run_key: str,
        before_send: Callable[[], bool],
    ) -> PublishResult:
        """Send one notification and report what the attempt established."""
        body, link = notification(
            kind=kind, project=project, run_key=run_key, site=self.site
        )
        connection = self._connect()
        try:
            try:
                connection.connect()
            except (OSError, http.client.HTTPException):
                # Nothing was sent. This is the one outcome that may retry.
                return PublishResult(DELIVERY_RETRY)
            if not before_send():
                # Connecting may have taken seconds. The incident is re-checked
                # against fresh evidence here, while no request has been sent.
                return PublishResult(DELIVERY_RETRY)
            try:
                connection.request(
                    "POST",
                    self.site.path,
                    body,
                    {
                        "Authorization": "Bearer " + self._token,
                        "Title": self.site.title,
                        "Click": link,
                        "Content-Type": "text/plain; charset=utf-8",
                    },
                )
                return self._receipt(connection.getresponse())
            except (OSError, http.client.HTTPException, ValueError, UnicodeError):
                return PublishResult(DELIVERY_UNCONFIRMED)
        finally:
            connection.close()

    def _receipt(self, response: PublishResponse) -> PublishResult:
        """Read the receipt, or decline to claim the message was delivered."""
        data = response.read(MAX_RECEIPT_BYTES + 1)
        if not 200 <= response.status < 300 or len(data) > MAX_RECEIPT_BYTES:
            return PublishResult(DELIVERY_UNCONFIRMED)
        receipt = json.loads(data)
        if (
            not isinstance(receipt, dict)
            or receipt.get("topic") != self.site.topic
            or receipt.get("event") != "message"
        ):
            return PublishResult(DELIVERY_UNCONFIRMED)
        identifier = receipt.get("id")
        if not isinstance(identifier, str) or not _RECEIPT_ID.fullmatch(identifier):
            return PublishResult(DELIVERY_UNCONFIRMED)
        return PublishResult(DELIVERY_PUBLISHED, identifier)


def select_publisher(
    *,
    mode: str,
    deployment: str,
    token: str | None,
    site: NotificationSite = PI_NOTIFICATION_SITE,
    connect: ConnectionFactory | None = None,
) -> NtfyPublisher | None:
    """The only supported way to obtain a publisher, and usually there is none.

    ``None`` means "record the incident, send nothing", which is what the
    engine already does when no publisher is configured. It is returned rather
    than raised for the two ordinary cases -- a local application, a deployment
    holding no credential -- because neither is a misconfiguration. A staging
    deployment that *does* hold a production credential is one, and raises.
    """
    if deployment not in DEPLOYMENTS:
        raise NotificationError(f"unknown deployment: {deployment!r}")
    if mode != MODE_REMOTE or deployment == DEPLOYMENT_LOCAL:
        # The Mac reads the same incidents through the local provider. It is
        # not a second publisher, however it is deployed.
        return None
    if token is None:
        return None
    authorize_publisher(deployment, site)
    return NtfyPublisher(token, site=site, deployment=deployment, connect=connect)


__all__ = [
    "DEPLOYMENTS",
    "DEPLOYMENT_LOCAL",
    "DEPLOYMENT_PRODUCTION",
    "DEPLOYMENT_STAGING",
    "FALLBACK_PROJECT_LABEL",
    "MAX_NOTIFICATION_LABEL_CHARS",
    "MAX_RECEIPT_BYTES",
    "NOTIFICATION_TEMPLATES",
    "NOTIFICATION_TITLE",
    "PI_NOTIFICATION_SITE",
    "PUBLISH_TIMEOUT_SECONDS",
    "RUN_FRAGMENT",
    "ConnectionFactory",
    "NotificationError",
    "NotificationSite",
    "NtfyPublisher",
    "PublishConnection",
    "PublishResponse",
    "authorize_publisher",
    "https_connection_factory",
    "notification",
    "notification_link",
    "project_label",
    "select_publisher",
]
