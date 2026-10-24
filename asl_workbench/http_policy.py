"""Http policy."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from . import http_constants as constants

# ---------------------------------------------------------------------------
# Request policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestPolicy:
    """The size, framing, content-type, origin, and diagnostic rules of a mode.

    One enforcement implementation, two configurations. Each field that
    differs between the modes says why in its default below rather than in a
    branch: the point of naming them is that a reader can see the whole
    difference between the loopback server and the public one at once.
    """

    #: The largest body this mode reads, before reading any of it.
    max_body_bytes: int = constants.MAX_REQUEST_BODY
    #: Whether a mutation must declare its length up front. A chunked body has
    #: no length to bound, so refusing it is how the cap above stays a cap.
    require_content_length: bool = True
    #: Whether a request that carries a body must declare it as JSON. Checked
    #: only when a body is present, so a POST with nothing in it is not refused
    #: for failing to describe the nothing.
    require_json_content_type: bool = True
    #: Whether a mutation must prove it came from this application's own page.
    require_origin: bool = False
    #: Origins accepted when :attr:`require_origin` holds, in addition to the
    #: loopback form built from the request's own ``Host``.
    allowed_origins: tuple[str, ...] = ()
    #: A header no cross-origin form can set, required on mutations when
    #: :attr:`require_origin` holds.
    request_header: str | None = None
    #: ``Sec-Fetch-Site`` values refused outright on a mutation.
    refused_fetch_sites: frozenset[str] = frozenset({"cross-site", "same-site"})
    #: Whether an unexpected exception's text may reach the client. True only
    #: on loopback, where the client is the operator who owns the process and
    #: the text is the fastest thing they have; false anywhere reachable, where
    #: it is an oracle.
    disclose_internal_errors: bool = False
    #: Whether a route the caller's role cannot reach answers ``not-found``
    #: instead of ``forbidden``. On a public surface the existence of an
    #: owner-only route is itself worth not confirming.
    conceal_forbidden: bool = False
    #: Whether the request's ``Host`` must name this machine. The loopback
    #: reads are token-free because nothing off the machine can reach the
    #: socket, and DNS rebinding is how a page off the machine reaches it
    #: anyway: the attacker's own hostname, re-resolved to 127.0.0.1, makes
    #: their page same-origin with this server. A rebound request still
    #: carries that hostname in ``Host``, which is what this refuses. The
    #: private network is reached by its own published name, so it does not.
    require_local_host: bool = False

    def origins(self, host: str) -> frozenset[str]:
        """Every Origin this mode accepts for a mutation, given the request Host."""
        accepted = set(self.allowed_origins)
        if host:
            accepted.add(f"http://{host}")
        return frozenset(accepted)


def is_local_host(host: str) -> bool:
    """Whether a ``Host`` header could only have been written for this machine.

    ``localhost`` (and its subdomains, which browsers resolve to loopback
    themselves) and any IP literal pass: no page can rebind an address, only a
    name. An absent header passes too, because every browser sends one and the
    callers that omit it are not pages.
    """
    name = host.strip()
    if name.startswith("["):
        name = name[1:].partition("]")[0]
    elif name.count(":") == 1:
        name = name.partition(":")[0]
    name = name.rstrip(".").lower()
    if not name or name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


#: Loopback. The bearer token minted at startup is the proof, so there is no
#: origin to check against and no identity header to trust; and the client is
#: the operator, so an unexpected failure says what it was.
LOCAL_POLICY = RequestPolicy(
    require_content_length=False,
    require_json_content_type=True,
    require_origin=False,
    disclose_internal_errors=True,
    conceal_forbidden=False,
    require_local_host=True,
)


#: Reachable over the private network. Everything the Companion enforced, for
#: the reasons it enforced them: a length before a read, a declared JSON body,
#: an origin and a header no cross-site form can forge, and a refusal that
#: says nothing a prober could use.
REMOTE_POLICY = RequestPolicy(
    require_content_length=True,
    require_json_content_type=True,
    require_origin=True,
    request_header="X-Workbench-Request",
    disclose_internal_errors=False,
    conceal_forbidden=True,
)
