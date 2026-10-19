# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F401, F403, F405, F821, F822, I001
"""HTTPS publication and body-redemption transport for the Workbench connector."""

from __future__ import annotations

from . import workbench_connector_projection as _projection

globals().update(
    {
        name: value
        for name, value in vars(_projection).items()
        if not name.startswith("__")
    }
)
del _projection


# --------------------------------------------------------------------------
# The channel
# --------------------------------------------------------------------------


def read_connector_token(path: str | Path) -> str:
    """The connector's bearer token, from a file only this user may read.

    The same three refusals the Companion's ``runtime.private`` makes, for the
    same reason: a token readable by the group is a token the operator did not
    choose to share, a symlink is a way to make this read something else, and a
    file owned by somebody else is not this connector's secret to use.
    """
    target = Path(path)
    try:
        info = target.lstat()
    except OSError as error:
        raise ConnectorSetupError(
            f"could not read the connector token: {error}"
        ) from error
    if not stat_bits.S_ISREG(info.st_mode):
        raise ConnectorSetupError("the connector token file is not a regular file")
    if info.st_uid != os.getuid():
        raise ConnectorSetupError("the connector token file belongs to another user")
    if info.st_mode & 0o077:
        raise ConnectorSetupError(
            "the connector token file is readable beyond its owner"
        )
    try:
        document = json.loads(target.read_bytes())
    except OSError as error:
        raise ConnectorSetupError(
            f"could not read the connector token: {error}"
        ) from error
    except ValueError as error:
        raise ConnectorSetupError(
            f"the connector token file is not JSON: {error}"
        ) from error
    if not isinstance(document, dict):
        raise ConnectorSetupError("the connector token file is not an object")
    token = document.get("connector_token")
    if not isinstance(token, str) or not token.strip():
        raise ConnectorSetupError("the connector token file carries no connector_token")
    if not token.isprintable() or not token.isascii():
        raise ConnectorSetupError("the connector token is not printable ASCII")
    return token


class HttpsChannel:
    """One bounded POST per frame, and nothing clever.

    Byte-for-byte the Companion's ``PiClient`` in the ways that matter: no
    redirects, no proxy environment, no response spooled to disk, and no
    automatic retry. Retry is the connector's decision, not the transport's --
    a POST replayed by a layer that cannot see the queue is how one frame
    arrives twice.

    Every failure becomes :class:`asl_workbench.connector_support.ChannelError`, which
    is the only exception the runtime knows how to classify as "the link, not
    the code".
    """

    def __init__(
        self,
        *,
        origin: str,
        token: str,
        path: str = INGEST_PATH,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        connect: Callable[[], http.client.HTTPConnection] | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ConnectorSetupError(
                "the connector origin must be an https URL naming a host"
            )
        self.origin = origin
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.path = path
        self.timeout = float(timeout)
        self._token = token
        self._connect = connect or self._default_connect

    def _default_connect(self) -> http.client.HTTPConnection:
        return http.client.HTTPSConnection(
            self.host,
            self.port,
            timeout=self.timeout,
            context=ssl.create_default_context(),
        )

    def send(self, frame: bytes) -> bytes | None:
        """Deliver one encoded envelope and return any frame carried back.

        The response body is the downward half of the link. A service with a
        correlated read to ask puts a ``request`` frame in the acknowledgement
        of this upload; anything else -- an empty body, a status line, a JSON
        ``{"ok": true}`` -- comes back as itself and the connector refuses it as
        undecodable without treating the delivery as failed. The frame landed;
        that is what this method's success means.
        """
        try:
            connection = self._connect()
        except (OSError, ValueError) as error:
            raise ChannelError(f"could not reach {self.origin}: {error}") from error
        try:
            connection.request(
                "POST",
                self.path,
                body=frame,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(frame)),
                },
            )
            response = connection.getresponse()
            body = response.read(MAX_FRAME_BYTES + 1)
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise ChannelError(f"delivery to {self.origin} failed: {error}") from error
        finally:
            with contextlib.suppress(OSError, http.client.HTTPException):
                connection.close()
        if response.status not in ACCEPTED_STATUSES:
            raise ChannelError(
                f"{self.origin} refused the frame with status {response.status}"
            )
        if len(body) > MAX_FRAME_BYTES:
            raise ChannelError(f"{self.origin} answered with an oversized body")
        return body or None


class HttpsBodySource:
    """Redeems one command body over the same link, at the moment of use.

    The transport's envelope set is pinned, so a reply's prose has no field to
    ride in: the service announces a ``reply_ref`` and the body is fetched out
    of band here, exchanging the operation's single-use ``action_token`` for
    the text. That is why this is a separate route rather than a bigger frame
    -- a wire shape change would break the contract set every participant is
    pinned to.

    Redeeming *late* is the point. The body is asked for after the window has
    been checked and immediately before ASL is handed it, so a command that
    waits in the queue and then expires never had its prose on this machine at
    all, and a restart has nothing to carry forward.

    Every failure is :class:`~asl_workbench.remote_actions.BodyUnavailable`,
    which settles the operation without attempting it. A refused redemption is
    ``action-token-refused`` and an unreachable service is
    ``bridge-unavailable``: the first is an answer, the second is a link.
    """

    def __init__(
        self,
        *,
        origin: str,
        token: str,
        path: str = REDEEM_PATH,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        connect: Callable[[], http.client.HTTPConnection] | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ConnectorSetupError(
                "the connector origin must be an https URL naming a host"
            )
        self.origin = origin
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.path = path
        self.timeout = float(timeout)
        self._token = token
        self._connect = connect or self._default_connect

    def _default_connect(self) -> http.client.HTTPConnection:
        return http.client.HTTPSConnection(
            self.host,
            self.port,
            timeout=self.timeout,
            context=ssl.create_default_context(),
        )

    def fetch(self, request: MutationRequest, *, now: float) -> str:
        """Exchange this operation's action token for the prose it names."""
        if not request.reply_ref or not request.action_token:
            raise BodyUnavailable(
                "the operation names no redeemable body",
                detail_code=DETAIL_ACTION_TOKEN_REFUSED,
            )
        body = json.dumps(
            {
                "operation_id": request.operation_id,
                "action_token": request.action_token,
                "reply_ref": request.reply_ref,
            }
        ).encode("utf-8")
        try:
            connection = self._connect()
        except (OSError, ValueError) as error:
            raise BodyUnavailable(f"could not reach {self.origin}: {error}") from error
        try:
            connection.request(
                "POST",
                self.path,
                body=body,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_FRAME_BYTES + 1)
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise BodyUnavailable(f"redemption failed: {error}") from error
        finally:
            with contextlib.suppress(OSError, http.client.HTTPException):
                connection.close()
        if response.status not in ACCEPTED_STATUSES:
            # A status, not a link failure: the service considered the token
            # and declined it. Reported as the token's refusal so the operator
            # can tell a spent token from a Pi that is down.
            raise BodyUnavailable(
                f"{self.origin} refused the redemption with status {response.status}",
                detail_code=DETAIL_ACTION_TOKEN_REFUSED,
            )
        if len(raw) > MAX_FRAME_BYTES:
            raise BodyUnavailable(f"{self.origin} answered with an oversized body")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise BodyUnavailable(f"the redemption was undecodable: {error}") from error
        if not isinstance(document, dict):
            raise BodyUnavailable("the redemption was not an object")
        text = document.get("reply_text")
        if not isinstance(text, str) or not text.strip():
            raise BodyUnavailable(
                "the redemption carried no reply_text",
                detail_code=DETAIL_ACTION_TOKEN_REFUSED,
            )
        if len(text) > MAX_BODY_CHARS:
            raise BodyUnavailable(f"{self.origin} answered with an oversized body")
        return text
