from .integration_checks import *
from .integration_core import *
from .integration_provision import service_secrets
from .integration_routes import *


class _RouteHomepageOperations(_IntegrationOperations):
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
