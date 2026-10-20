"""Attempt identity and the refusals a worker's receipt can raise.

A correction is receipted against one *attempt*: the turn the run is executing
when the worker reads it. The identity of that attempt is one string, and the
two facts the receipt has to pin are both comparisons of it.

:func:`attempt_token` renders ``run_id/session_id/turn_index`` and
:func:`token_identifies` compares by equality, never by parsing -- the harness
is excluded exactly as it is from
:class:`redirect_store_models.RedirectTarget`, so pinning the retry to its
predecessor's harness cannot invalidate a receipt the operator is waiting on.

:class:`RedirectAckError` carries the stable reasons this subsystem raises on
its own behalf, which are about the *worker's* claim rather than the journal's.
"""

from __future__ import annotations

from typing import Any

from . import redirect_store_models

#: Separator inside an attempt token. Run ids and session ids cannot contain it
#: (the journal's own validation bounds them, and both are path-safe
#: identifiers), so a token has exactly three parts by construction.
ATTEMPT_TOKEN_SEPARATOR = "/"


def attempt_token(target: redirect_store_models.RedirectTarget) -> str:
    """Return the opaque identity a worker echoes back to receipt a correction.

    Built from the turn's identity triple and deliberately *not* from the
    harness: the run loop may re-select a harness between the interruption and
    the continuation, and that changes what runs the attempt rather than which
    attempt it is.

    The token is compared with string equality and never parsed back into a
    target. A comparison that took the token apart would have to decide what a
    malformed part means, and every answer to that question is a way for a
    worker to be credited with a receipt it did not earn.
    """
    return ATTEMPT_TOKEN_SEPARATOR.join(
        (
            target.run_id,
            target.session_id,
            str(target.turn_index),
        )
    )


def token_identifies(
    token: str,
    target: redirect_store_models.RedirectTarget,
) -> bool:
    """Whether ``token`` is exactly the token ``target`` would mint."""
    return isinstance(token, str) and token == attempt_token(target)


#: Failure reasons this module raises on its own behalf. Journal refusals keep
#: `redirect_store_vocab`'s reasons; these two are about the *worker's* claim.
ERROR_ATTEMPT_MISMATCH = "redirect-attempt-mismatch"
ERROR_NO_LIVE_ATTEMPT = "redirect-no-live-attempt"


class RedirectAckError(Exception):
    """A refused acknowledgment, carrying a stable surface-agnostic reason."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "message": self.message,
            "details": dict(self.details),
        }
