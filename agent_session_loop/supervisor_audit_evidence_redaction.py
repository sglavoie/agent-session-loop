"""Secret, origin, argv and path redaction rules for the audit evidence."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, get_args

from . import assurance_models_vocab, external_write_evidence, harnesses, workspaces
from .supervisor_audit_evidence_constants import REDACTED
from .supervisor_audit_evidence_helpers import _sha256_text

#: Keys whose value is application output or a semantic diff rather than
#: control-plane evidence. Their digest and size are kept; their bytes are not.
_CONTENT_KEYS = frozenset(
    {
        "body",
        "content",
        "diff",
        "output",
        "patch",
        "source",
        "stderr",
        "stdout",
        "text",
    }
)


#: Keys naming a Git origin, which is canonicalized rather than copied so a
#: credential embedded in a remote URL cannot reach the bundle.
_ORIGIN_KEYS = frozenset({"origin", "origins", "remote", "remote_url", "url"})


#: The bounded enum a reply origin is drawn from, mirroring
#: ``run_prompts.REPLY_ORIGINS``. No Git remote can collide with these tokens.
_REPLY_ORIGIN_VALUES = frozenset({"argv", "stdin", "mailbox"})


#: The only values ``assurance.role.source`` may carry.  ``source`` is
#: otherwise application content and remains withheld; these bounded enums are
#: control-plane provenance validated by :class:`assurance_models_summary.AuditRole`.
_AUDIT_ROLE_SOURCE_VALUES = frozenset(assurance_models_vocab.AUDIT_ROLE_SOURCES)


#: Closed set of tokens a ``source`` may carry to name *where a runner control
#: signal was read*, as opposed to application output. Deliberately a value
#: allowlist rather than a parent-name exemption: a live run writes this
#: provenance under several different parents, while a ``source`` holding real
#: application text must stay withheld under every one of them.
#:
#: Mirrors, rather than imports, the values the run-loop boundaries above this
#: module own — this module is imported *by* them, so naming the values keeps
#: the layering one way round: ``get_args(harnesses.StreamName)``,
#: ``turn_execution_constants.TRUSTED_RUNNER_SIGNAL_SOURCES`` minus the streams,
#: ``turn_execution_constants.RUNNER_STATUS_SOURCE_ABSENT``, and the ``"pause"`` literal
#: ``run_termination`` stamps when the pause itself, not a stream, is the origin.
_CONTROL_PLANE_SOURCE_VALUES = frozenset(get_args(harnesses.StreamName)) | frozenset(
    {"last_message", "absent", "pause"}
)


#: Field parents under which ``origin`` names how an operator reply reached the
#: run rather than a repository. ``None`` covers the reply event, which carries
#: the enum at its payload root.
_REPLY_ORIGIN_PARENTS = frozenset({None, "reply_source", "source"})


#: Keys holding a process environment, whose values are withheld wholesale.
_ENVIRONMENT_KEYS = frozenset({"env", "environ", "environment"})


#: A key is secret-bearing when one of these words is the whole key or a whole
#: word inside it. The trailing boundary is what keeps ``auth`` from swallowing
#: ``authorized_aliases``; it admits a plural and an upper-case camelCase
#: continuation (``authToken``) so narrowing the match sheds no real secret.
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|[-_.])(?:api[-_]?key|access[-_]?key|auth|authorization|bearer|"
    r"cookie|credential|credentials|passphrase|passwd|password|private[-_]?key|"
    r"pwd|secret|session[-_]?key|signature|token)s?(?=$|[-_.]|(?-i:[A-Z]))"
)


_SECRET_VALUE_RE = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9]{20,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)


#: The closed vocabulary the external-write guard classifies a path into.
#: A value drawn from it is a verdict, never a secret, whatever its key.
_EXTERNAL_WRITE_CLASS_VALUES = frozenset(external_write_evidence.EXTERNAL_WRITE_CLASSES)


def _is_path_key(name: str) -> bool:
    """Whether a map key is an absolute path rather than a field name."""
    return name.startswith("/") and "\n" not in name


_CREDENTIAL_URL_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s@]+@")


#: Keys whose value is a command line. Argv is redacted positionally as well as
#: by shape, because a secret usually arrives as the argument after its flag.
_ARGV_KEYS = frozenset({"argv", "args", "command", "cmd"})


_SECRET_FLAG_RE = re.compile(
    r"(?i)^--?(?:api[-_]?key|auth|authorization|bearer|credential|credentials|"
    r"key|passphrase|passwd|password|pwd|secret|token)$"
)


_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)^(--?(?:api[-_]?key|auth|authorization|bearer|credential|credentials|"
    r"key|passphrase|passwd|password|pwd|secret|token)=)(.+)$"
)


def _redact_argv(items: Sequence[Any]) -> list[Any]:
    """Blank out argv values that a secret-bearing flag introduces or embeds."""
    redacted: list[Any] = []
    expect_secret = False
    for item in items:
        if not isinstance(item, str):
            redacted.append(item)
            expect_secret = False
            continue
        if expect_secret:
            redacted.append(REDACTED)
            expect_secret = False
            continue
        assignment = _SECRET_ASSIGNMENT_RE.match(item)
        if assignment is not None:
            redacted.append(f"{assignment.group(1)}{REDACTED}")
            continue
        redacted.append(item)
        expect_secret = _SECRET_FLAG_RE.match(item) is not None
    return redacted


def _is_reply_origin(value: str, *, key: str, parent: str | None) -> bool:
    """Whether an ``origin`` field names a reply channel rather than a remote.

    Canonicalizing one erases the only record of whether an operator reply
    arrived by argv, by stdin, or through the mailbox, and leaves an
    ``[unparsable-origin:…]`` hash in its place.
    """
    return (
        key == "origin"
        and parent in _REPLY_ORIGIN_PARENTS
        and value in _REPLY_ORIGIN_VALUES
    )


def _redact_origin(value: str) -> str:
    """Return a credential-free origin identity, or a stable stand-in."""
    try:
        return workspaces.canonicalize_origin(value)
    except workspaces.WorkspaceError:
        return f"[unparsable-origin:{_sha256_text(value)[:16]}]"


def _redact_secrets(text: str) -> str:
    """Strip URL userinfo and recognizable credential literals from ``text``."""
    cleaned = _CREDENTIAL_URL_RE.sub(rf"\1{REDACTED}@", text)
    return _SECRET_VALUE_RE.sub(REDACTED, cleaned)
