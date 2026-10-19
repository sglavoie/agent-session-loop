from .integration_core import *


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
