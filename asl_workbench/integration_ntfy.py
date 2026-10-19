from .integration_checks import *
from .integration_core import *


class _NtfyOperations(_IntegrationOperations):
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
