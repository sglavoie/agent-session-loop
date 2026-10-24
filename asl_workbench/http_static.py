"""Http static."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from . import http_auth as auth
from . import http_messages as messages

# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------

STATIC_INDEX_FILE = "index.html"


STATIC_URL_PREFIX = "/static/"


#: The one copy of the front end. Both deployments serve *this* directory --
#: the loopback server on a laptop and the remote host over a tailnet -- because a
#: second copy is a second thing to keep in step, and the only way to be sure
#: the two modes run the same shell is for there to be nothing to keep in
#: step. The entry is a native ES module; `modules/` holds the rest.
STATIC_ROOT = Path(__file__).resolve().parent / "static"


STATIC_MIME_TYPES: Mapping[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".map": "application/json; charset=utf-8",
}


STATIC_DEFAULT_MIME = "application/octet-stream"


#: The shell stays unstored so a server upgrade is never masked by a cached
#: index; the bundle revalidates so an unchanged asset costs one 304 and a
#: changed one arrives immediately after a restart.
STATIC_INDEX_CACHE_CONTROL = "no-store"


STATIC_ASSET_CACHE_CONTROL = "no-cache, must-revalidate"


def compute_etag(stat_result: os.stat_result) -> str:
    """Build a strong validator from inode-style fields.

    Mixes mtime_ns and size so any in-place edit invalidates the cache. The
    token is quoted per RFC 7232 so clients echo it verbatim in
    ``If-None-Match``.
    """
    return f'"{stat_result.st_mtime_ns:x}-{stat_result.st_size:x}"'


def parse_etag_list(header_value: str) -> tuple[str, ...]:
    """Parse a comma-separated ``If-None-Match`` header into ETag tokens."""
    if not header_value:
        return ()
    tokens: list[str] = []
    for chunk in header_value.split(","):
        token = chunk.strip()
        if not token:
            continue
        if token.startswith("W/"):
            token = token[2:].strip()
        tokens.append(token)
    return tuple(tokens)


class StaticBundle:
    """The browser assets, served identically wherever they are served from.

    Both modes hand out the same shell and the same bundle, so the traversal
    refusal, the mime table, the cache policy, and the validator are decided
    once here rather than twice in two servers that would drift.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def index(self, request: messages.Request) -> messages.Response:
        return self._serve(
            request,
            self.root / STATIC_INDEX_FILE,
            STATIC_MIME_TYPES[".html"],
            cache_control=STATIC_INDEX_CACHE_CONTROL,
            use_etag=False,
            csp=messages.DOCUMENT_CONTENT_SECURITY_POLICY,
        )

    def asset(self, request: messages.Request, relative: str) -> messages.Response:
        if not relative or relative.startswith("/"):
            raise auth.HttpRefusal(404, "not-found", "missing static asset path")
        root = self.root.resolve()
        try:
            candidate = (root / relative).resolve()
        except (OSError, ValueError) as error:
            # ValueError is what a NUL byte in the path raises; it is the
            # client's malformed path, not this server's failure.
            raise auth.invalid_request(
                f"invalid static path: {error}", path=relative
            ) from error
        try:
            candidate.relative_to(root)
        except ValueError:
            raise auth.HttpRefusal(
                400,
                "invalid-request",
                "static path escapes the bundle",
                details={"path": relative},
            ) from None
        if not candidate.is_file():
            raise auth.HttpRefusal(
                404,
                "not-found",
                f"static asset not found: {relative}",
                details={"path": relative},
            )
        content_type = STATIC_MIME_TYPES.get(
            candidate.suffix.lower(), STATIC_DEFAULT_MIME
        )
        return self._serve(
            request,
            candidate,
            content_type,
            cache_control=STATIC_ASSET_CACHE_CONTROL,
            use_etag=True,
            csp=messages.API_CONTENT_SECURITY_POLICY,
        )

    def _serve(
        self,
        request: messages.Request,
        path: Path,
        content_type: str,
        *,
        cache_control: str,
        use_etag: bool,
        csp: str,
    ) -> messages.Response:
        headers = {
            **messages.SECURITY_HEADERS,
            "Cache-Control": cache_control,
            "Content-Security-Policy": csp,
        }
        etag: str | None = None
        if use_etag:
            stat_result = self._stat(path)
            etag = compute_etag(stat_result)
            presented = parse_etag_list(request.header("if-none-match") or "")
            if etag and etag in presented:
                return messages.Response(
                    status=304, headers={**headers, "ETag": etag}, content_type=None
                )
        body = self._read(path)
        if etag is not None:
            headers["ETag"] = etag
        return messages.Response(
            status=200, body=body, content_type=content_type, headers=headers
        )

    @staticmethod
    def _stat(path: Path) -> os.stat_result:
        try:
            return path.stat()
        except FileNotFoundError:
            raise auth.HttpRefusal(
                404,
                "not-found",
                f"static asset not found: {path.name}",
                details={"path": str(path)},
            ) from None
        except OSError as error:
            raise auth.HttpRefusal(
                500, "os-error", str(error), details={"path": str(path)}
            ) from error

    @staticmethod
    def _read(path: Path) -> bytes:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise auth.HttpRefusal(
                404,
                "not-found",
                f"static asset not found: {path.name}",
                details={"path": str(path)},
            ) from None
        except OSError as error:
            raise auth.HttpRefusal(
                500, "os-error", str(error), details={"path": str(path)}
            ) from error


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------


def shell_bundle() -> StaticBundle:
    """The front end, for whichever deployment is asking.

    There is one right answer and it is the same one in both modes, so it is
    given here instead of at each construction site. An application built
    without it serves no assets at all, which is the correct behavior for a
    caller that only wants the API.
    """
    return StaticBundle(STATIC_ROOT)
