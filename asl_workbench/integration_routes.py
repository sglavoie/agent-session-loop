from .integration_core import *


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
