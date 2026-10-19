"""Owned-host integration API, composed from focused resource operations."""

from .integration_checks import *
from .integration_core import *
from .integration_kuma import _KumaDriveOperations
from .integration_ntfy import _NtfyOperations
from .integration_provision import *
from .integration_route_homepage import _RouteHomepageOperations
from .integration_routes import *


@dataclass
class Integrations(_RouteHomepageOperations, _NtfyOperations, _KumaDriveOperations):
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

    def receipt(self, name: str) -> dict[str, Any] | None:
        return self.receipts.read(name)

    def save(self, name: str, value: Mapping[str, Any]) -> None:
        self.receipts.save(name, value)

    def identity(self) -> str:
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
        return self.runner(
            ["docker", "exec", self.site.ntfy_container, "ntfy", "user", "list"]
        ).decode()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asl_workbench.integrations", description=__doc__.splitlines()[0]
    )
    parser.add_argument("mode", choices=("provision", *MODES))
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument(
        "--component", choices=(COMPONENT_ALL, *COMPONENTS), default=COMPONENT_ALL
    )
    parser.add_argument("--tailscale-sudo", action="store_true")
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
