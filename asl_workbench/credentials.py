"""Who may send a Workbench envelope: four roles, and four different proofs.

The portable half of the Companion's ``auth`` module, carried across unchanged
where it was already right and extended where the Workbench transport needs it
to reach further. The roles and the stored file format are byte-compatible with
the Companion's, so an installed ``credentials.json`` loads here without being
rewritten; what is new is that a role is also a statement about
:mod:`asl_workbench.transport` -- which envelope kinds it may send and which
capabilities it may exercise.

The four callers are not variations of one another, so they do not share one
credential:

* **The connector** uploads everything the service knows. Its credential is the
  only one that may write, and it is bound to a connector id through
  :attr:`Credential.subject`, so a stolen connector token cannot be used to
  speak for a different Mac. :func:`authorize_upload` checks that binding
  against the envelope's own ``connector_id`` rather than trusting either one
  alone.
* **A monitor** reads counts and freshness and nothing else -- no project name,
  no run identity, no question. A summary credential that could read the
  dashboard would make "put the widget on the home page" a decision about
  disclosure rather than about layout.
* **The owner** reads the dashboard from a browser and proves identity rather
  than possession: Tailscale Serve stamps the verified tailnet login on every
  forwarded request and strips any copy the client supplied, so the value is
  the tailnet's statement. A bearer token in a browser would be a token in a
  bookmark, a history file and a screenshot.
* **The public** presents nothing, and may ask only whether the service is
  alive. A monitor that has to authenticate to discover the service is down is
  a monitor that reports a credential problem as an outage.

Secrets are stored as a keyed digest, so a readable credentials file is not a
usable one. The digest is a single HMAC pass rather than a password KDF, and
deliberately: the secret is a 256-bit value this module generated, never a
passphrase a person chose, so there is no dictionary to stretch against. What
the digest buys is that a file disclosure is not immediately a replayable
token.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .observation import MAX_LABEL_CHARS
from .transport import (
    CAPABILITY_CATALOG,
    CAPABILITY_DETAIL,
    CAPABILITY_HISTORY,
    CAPABILITY_MUTATE,
    CAPABILITY_OBSERVE,
    DIRECTION_UP,
    ENVELOPE_KINDS,
    ERROR_UNAUTHORIZED,
    KINDS_BY_DIRECTION,
    TransportError,
)

# -- Roles ------------------------------------------------------------------

#: The two roles that carry a token. ``owner`` is deliberately absent: an owner
#: is recognised, not authenticated by possession, and a role that could be
#: either would end up being both.
ROLE_CONNECTOR = "connector"
ROLE_SUMMARY = "summary"
CREDENTIAL_ROLES: tuple[str, ...] = (ROLE_CONNECTOR, ROLE_SUMMARY)

ROLE_OWNER = "owner"
#: No credential at all.
ROLE_PUBLIC = "public"

ROLES: tuple[str, ...] = (ROLE_CONNECTOR, ROLE_SUMMARY, ROLE_OWNER, ROLE_PUBLIC)

#: The header Tailscale Serve stamps on a forwarded request, carrying the login
#: it verified.
IDENTITY_HEADER = "Tailscale-User-Login"

#: Which transport capabilities each role may exercise. The connector holds
#: every one because it is the end that produces them all; the owner holds the
#: reading capabilities because a browser asks for details and catalogs but
#: uploads nothing; a monitor and the public hold none, because every
#: capability here names material about a specific plan.
ROLE_CAPABILITIES: Mapping[str, frozenset[str]] = {
    ROLE_CONNECTOR: frozenset(
        {
            CAPABILITY_OBSERVE,
            CAPABILITY_CATALOG,
            CAPABILITY_HISTORY,
            CAPABILITY_DETAIL,
            CAPABILITY_MUTATE,
        }
    ),
    ROLE_OWNER: frozenset(
        {
            CAPABILITY_OBSERVE,
            CAPABILITY_CATALOG,
            CAPABILITY_HISTORY,
            CAPABILITY_DETAIL,
        }
    ),
    ROLE_SUMMARY: frozenset(),
    ROLE_PUBLIC: frozenset(),
}

#: Which roles may *send* each envelope kind. Everything a connector uploads
#: is connector-only: an owner who could post an observation could write a run
#: into the store that no Mac ever saw, and the store's whole claim is that its
#: rows came from somewhere.
ROLES_BY_KIND: Mapping[str, frozenset[str]] = {
    kind: frozenset({ROLE_CONNECTOR}) for kind in KINDS_BY_DIRECTION[DIRECTION_UP]
} | {
    # A request travels the other way: the service composes it on the owner's
    # behalf, and the connector collects it with its own credential.
    kind: frozenset({ROLE_OWNER})
    for kind in ENVELOPE_KINDS
    if kind not in KINDS_BY_DIRECTION[DIRECTION_UP]
}

CREDENTIALS_SCHEMA_VERSION = 1

#: ``credential_id.secret``. The id is public and exists so a lookup does not
#: have to scan; the secret is the whole of the proof.
TOKEN_SEPARATOR = "."
ID_BYTES = 8
SECRET_BYTES = 32

_CREDENTIAL_KEYS = frozenset(
    {
        "credential_id",
        "role",
        "salt",
        "digest",
        "created_at",
        "subject",
        "label",
        "disabled",
    }
)
_FILE_KEYS = frozenset({"version", "credentials"})


class CredentialError(ValueError):
    """The credential file, or a credential in it, was refused."""


@dataclass(frozen=True)
class Credential:
    """One issued credential, as it is stored: never the secret itself."""

    credential_id: str
    role: str
    salt: str
    digest: str
    created_at: float
    #: The connector id this credential may speak for, when it speaks for one.
    #: A connector credential without a subject could upload under any
    #: connector id, and the service keys everything it stores by that id.
    subject: str | None = None
    label: str | None = None
    disabled: bool = False

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "credential_id": self.credential_id,
            "role": self.role,
            "salt": self.salt,
            "digest": self.digest,
            "created_at": self.created_at,
        }
        if self.subject is not None:
            document["subject"] = self.subject
        if self.label is not None:
            document["label"] = self.label
        if self.disabled:
            document["disabled"] = True
        return document


@dataclass(frozen=True)
class Authorization:
    """The decision one presented proof produced.

    ``credential_id`` and ``subject`` are absent for the roles that carry no
    token, which is exactly the difference between being recognised and being
    authenticated.
    """

    role: str
    credential_id: str | None = None
    subject: str | None = None

    def permits(self, capability: str) -> bool:
        return capability in ROLE_CAPABILITIES.get(self.role, frozenset())


def _hex(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CredentialError(f"credential {name} must be a hex string")
    try:
        bytes.fromhex(value)
    except ValueError as error:
        raise CredentialError(f"credential {name} is not hex") from error
    return value


def _bounded(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CredentialError(f"credential {name} must be a non-empty string")
    if len(value) > MAX_LABEL_CHARS or any(character < " " for character in value):
        raise CredentialError(f"credential {name} exceeds the label bound")
    return value


def credential_from_document(document: Any) -> Credential:
    """Read one credential record, refusing anything it does not declare."""
    if not isinstance(document, dict):
        raise CredentialError("a credential must be an object")
    unknown = sorted(set(document) - _CREDENTIAL_KEYS)
    if unknown:
        raise CredentialError(f"credential carries unknown keys: {', '.join(unknown)}")
    role = document.get("role")
    if role not in CREDENTIAL_ROLES:
        # ``owner`` lands here too, which is the point: an owner credential
        # cannot be written into the file by mistake and quietly become a
        # password.
        raise CredentialError(f"credential role is not issuable: {role!r}")
    created_at = document.get("created_at")
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
        raise CredentialError("credential created_at must be a number")
    subject = document.get("subject")
    if subject is not None:
        subject = _bounded(subject, "subject")
    label = document.get("label")
    if label is not None:
        label = _bounded(label, "label")
    disabled = document.get("disabled", False)
    if not isinstance(disabled, bool):
        raise CredentialError("credential disabled must be a boolean")
    return Credential(
        credential_id=_bounded(document.get("credential_id"), "credential_id"),
        role=role,
        salt=_hex(document.get("salt"), "salt"),
        digest=_hex(document.get("digest"), "digest"),
        created_at=float(created_at),
        subject=subject,
        label=label,
        disabled=disabled,
    )


def _digest(salt: str, secret: str) -> str:
    return hmac.new(bytes.fromhex(salt), secret.encode("utf-8"), sha256).hexdigest()


def issue(
    role: str,
    *,
    created_at: float,
    subject: str | None = None,
    label: str | None = None,
) -> tuple[Credential, str]:
    """Mint one credential and return it with the token to hand out once.

    The token is returned rather than stored. Nothing in this package can
    recover it afterwards, which is the property that makes a readable
    credentials file survivable.
    """
    if role not in CREDENTIAL_ROLES:
        raise CredentialError(f"credential role is not issuable: {role!r}")
    credential_id = secrets.token_hex(ID_BYTES)
    secret = secrets.token_urlsafe(SECRET_BYTES)
    salt = secrets.token_hex(16)
    credential = Credential(
        credential_id=credential_id,
        role=role,
        salt=salt,
        digest=_digest(salt, secret),
        created_at=float(created_at),
        subject=subject,
        label=label,
    )
    return credential, f"{credential_id}{TOKEN_SEPARATOR}{secret}"


def parse_authorization(header: Any) -> str | None:
    """Extract the presented token from an ``Authorization`` header.

    Only the ``Bearer`` scheme is read. A service that also accepted ``Basic``
    would accept a credential a browser can be talked into sending
    automatically.
    """
    if not isinstance(header, str):
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


class CredentialStore:
    """Every issued credential, and the one question the service asks of them."""

    def __init__(self, credentials: list[Credential] | None = None) -> None:
        self._by_id: dict[str, Credential] = {}
        for credential in credentials or []:
            self.add(credential)
        # A miss still costs one HMAC over a salt this store invented, so an
        # unknown credential id and a wrong secret take the same shape of work.
        self._decoy_salt = secrets.token_hex(16)
        self._decoy_digest = _digest(
            self._decoy_salt, secrets.token_urlsafe(SECRET_BYTES)
        )

    def __len__(self) -> int:
        return len(self._by_id)

    def add(self, credential: Credential) -> None:
        if credential.credential_id in self._by_id:
            raise CredentialError(f"duplicate credential {credential.credential_id}")
        self._by_id[credential.credential_id] = credential

    def get(self, credential_id: str) -> Credential | None:
        return self._by_id.get(credential_id)

    def credentials(self) -> tuple[Credential, ...]:
        return tuple(self._by_id.values())

    def authenticate(self, token: Any) -> Credential | None:
        """Return the credential a token proves, or ``None``.

        One refusal for every way of failing. A caller that could tell "no such
        credential" from "wrong secret" could enumerate the file's contents
        through the front door.
        """
        if not isinstance(token, str) or TOKEN_SEPARATOR not in token:
            return None
        credential_id, _, secret = token.partition(TOKEN_SEPARATOR)
        credential = self._by_id.get(credential_id)
        if credential is None:
            hmac.compare_digest(_digest(self._decoy_salt, secret), self._decoy_digest)
            return None
        if not hmac.compare_digest(_digest(credential.salt, secret), credential.digest):
            return None
        if credential.disabled:
            return None
        return credential

    # -- persistence --------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> CredentialStore:
        """Read a credentials file, refusing one that anyone else can read."""
        location = Path(path)
        try:
            mode = location.stat().st_mode
        except OSError as error:
            raise CredentialError(
                f"cannot read the credentials file: {error}"
            ) from error
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise CredentialError(
                "the credentials file is readable beyond its owner; fix its mode"
            )
        try:
            document = json.loads(location.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise CredentialError(f"credentials file is unreadable: {error}") from error
        return cls.from_document(document)

    @classmethod
    def from_document(cls, document: Any) -> CredentialStore:
        if not isinstance(document, dict):
            raise CredentialError("the credentials file must be an object")
        unknown = sorted(set(document) - _FILE_KEYS)
        if unknown:
            raise CredentialError(
                f"credentials file has unknown keys: {', '.join(unknown)}"
            )
        version = document.get("version")
        if version != CREDENTIALS_SCHEMA_VERSION:
            raise CredentialError(f"unsupported credentials schema: {version!r}")
        entries = document.get("credentials")
        if not isinstance(entries, list):
            raise CredentialError("credentials must be an array")
        return cls([credential_from_document(entry) for entry in entries])

    def as_document(self) -> dict[str, Any]:
        return {
            "version": CREDENTIALS_SCHEMA_VERSION,
            "credentials": [
                credential.as_document() for credential in self.credentials()
            ],
        }

    def save(self, path: str | os.PathLike[str]) -> None:
        """Write the file owner-readable only, and atomically.

        Created through ``O_EXCL`` at mode 0600 and renamed into place, so the
        file never exists briefly with a permissive mode -- the window in which
        a file like this is worth stealing is exactly that brief.
        """
        location = Path(path)
        temporary = location.with_name(f"{location.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.as_document(), handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, location)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def owner_matches(configured: Any, presented: Any) -> bool:
    """Whether a forwarded identity is the configured owner.

    Compared case-insensitively because a login is an address and addresses are
    not case-sensitive in practice, and compared in constant time because the
    comparison happens before anything else is decided about the request.
    """
    if not isinstance(configured, str) or not configured.strip():
        return False
    if not isinstance(presented, str) or not presented.strip():
        return False
    return hmac.compare_digest(
        configured.strip().casefold().encode("utf-8"),
        presented.strip().casefold().encode("utf-8"),
    )


def resolve_role(
    store: CredentialStore,
    *,
    authorization: Any = None,
    identity: Any = None,
    owner: Any = None,
) -> Authorization:
    """Decide which role a caller presented, in the order the proofs rank.

    Identity is read first: an owner who also happens to hold a summary token
    is still the owner, and deciding otherwise would make a debugging header
    quietly downgrade a browser session. A token that proves nothing leaves the
    caller public rather than refused -- the refusal belongs to whatever is
    being asked for, not to the act of asking.
    """
    if owner_matches(owner, identity):
        return Authorization(role=ROLE_OWNER)
    credential = store.authenticate(parse_authorization(authorization))
    if credential is None:
        return Authorization(role=ROLE_PUBLIC)
    return Authorization(
        role=credential.role,
        credential_id=credential.credential_id,
        subject=credential.subject,
    )


def authorize_upload(
    store: CredentialStore,
    envelope: Mapping[str, Any],
    *,
    authorization: Any = None,
) -> Authorization:
    """Refuse an upload the presented credential does not support.

    Three separate questions, and all three are asked: did a credential prove
    anything, may that role send this kind of envelope, and does the credential
    speak for the connector the envelope claims to come from. The third is the
    one a token alone cannot answer -- the envelope names its own
    ``connector_id``, and without the ``subject`` binding a leaked connector
    token would be a licence to file observations under any Mac's name.
    """
    kind = envelope.get("kind")
    capability = envelope.get("capability")
    connector_id = envelope.get("connector_id")
    if not isinstance(kind, str) or kind not in ROLES_BY_KIND:
        raise TransportError(ERROR_UNAUTHORIZED, f"unknown envelope kind: {kind!r}")

    credential = store.authenticate(parse_authorization(authorization))
    if credential is None:
        raise TransportError(ERROR_UNAUTHORIZED, "no credential was proved")
    if credential.role not in ROLES_BY_KIND[kind]:
        raise TransportError(
            ERROR_UNAUTHORIZED, f"a {credential.role} credential cannot send a {kind}"
        )
    if not isinstance(capability, str) or capability not in ROLE_CAPABILITIES.get(
        credential.role, frozenset()
    ):
        raise TransportError(
            ERROR_UNAUTHORIZED,
            f"a {credential.role} credential cannot exercise {capability!r}",
        )
    if credential.subject is None:
        raise TransportError(
            ERROR_UNAUTHORIZED, "the credential speaks for no connector"
        )
    if credential.subject != connector_id:
        raise TransportError(
            ERROR_UNAUTHORIZED,
            f"the credential speaks for {credential.subject}, not {connector_id!r}",
        )
    return Authorization(
        role=credential.role,
        credential_id=credential.credential_id,
        subject=credential.subject,
    )


__all__ = [
    "CREDENTIALS_SCHEMA_VERSION",
    "CREDENTIAL_ROLES",
    "ID_BYTES",
    "IDENTITY_HEADER",
    "ROLES",
    "ROLES_BY_KIND",
    "ROLE_CAPABILITIES",
    "ROLE_CONNECTOR",
    "ROLE_OWNER",
    "ROLE_PUBLIC",
    "ROLE_SUMMARY",
    "SECRET_BYTES",
    "TOKEN_SEPARATOR",
    "Authorization",
    "Credential",
    "CredentialError",
    "CredentialStore",
    "authorize_upload",
    "credential_from_document",
    "issue",
    "owner_matches",
    "parse_authorization",
    "resolve_role",
]
