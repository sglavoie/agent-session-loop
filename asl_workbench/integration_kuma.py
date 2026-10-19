from .integration_checks import *
from .integration_core import *


class _KumaDriveOperations(_IntegrationOperations):
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
