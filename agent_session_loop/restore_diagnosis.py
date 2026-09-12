"""What a provider's refusal says about whether a conversation still exists.

A turn that asks to continue a native conversation can be told no in a hundred
ways, and exactly one of them is durable: the provider stating that the history
itself is gone. Everything else -- an internal fault, a service that is down for
the afternoon, an expired credential, a workspace the caller may no longer read
-- is a statement about *now*, and acting on it as though it were permanent
retires a record the provider is still holding and steers the operator toward
discarding an agent's accumulated context for nothing.

The distinction cannot live in the adapters, because there are three of them and
a rule split three ways is a rule that drifts. So it lives here, as one
vocabulary and one decision, and this module deliberately imports nothing from
the package: the app-server transport, the ACP transport, and the claude CLI
reader all sit below :mod:`~agent_session_loop.harnesses`, and a classifier they
can all reach has to sit below them in turn.

Two shapes of evidence arrive, and both funnel into the same vocabulary:

* a JSON-RPC error response, which codex and copilot get from ``thread/resume``
  and ``session/load`` -- parsed into :class:`ProviderRejection` so the code and
  the message are judged separately rather than as one rendered string;
* a line of CLI output, which is all claude ever offers, since ``--resume`` goes
  on argv and there is no handshake to refuse.

The decision is fail-closed in the direction that costs the least. An
unrecognized refusal is *not* a missing history: the turn still fails, the run
still pauses, and the operator still sees the provider's own words -- but the
ledger entry survives, so repairing the outage and re-running continues the
exact saved conversation with no authorization to spend. The opposite default
throws away history on the strength of a sentence nobody taught this module to
read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

#: The nouns a provider uses for the thing being restored. Kept as one group so
#: the patterns below stay statements about *a conversation* rather than about
#: whatever else the sentence happened to mention.
_HISTORY_NOUN = r"(?:conversation|session|thread)"

#: What a provider says when the history named by the restore no longer exists.
#: Every alternative is a statement about that conversation's existence rather
#: than about this machine, this credential, or this moment, which is what makes
#: it safe to act on durably. "could not read", "permission denied" and the like
#: are deliberately absent: a history that cannot be reached right now is not a
#: history that is gone.
MISSING_HISTORY_RE = re.compile(
    r"(?:no (?:such|conversation|session|thread)\b"
    rf"|{_HISTORY_NOUN}\s+(?:was\s+)?not\s+found"
    rf"|{_HISTORY_NOUN}\b[^\n]*\b(?:does not exist|no longer exists|was deleted)"
    rf"|(?:unknown|invalid|expired)\s+{_HISTORY_NOUN})",
    re.IGNORECASE,
)

#: What a provider says when the refusal is about anything other than the
#: history: a fault, an outage, a credential, or a permission. It vetoes rather
#: than merely failing to match, because a message can contain both vocabularies
#: -- "no such session (authentication expired; retry later)" is an outage
#: wearing a missing-history sentence, and reading it the other way round is the
#: expensive mistake.
TRANSIENT_OR_ACCESS_RE = re.compile(
    r"(?:temporar"
    r"|try again|retry|unavailable|timed out|time out|timeout"
    r"|overload|capacity|rate[ _-]?limit|quota|throttl"
    r"|unauthori[sz]ed|unauthenticated|authentication|credential|api key"
    r"|log ?in|sign ?in|token expired"
    r"|permission|forbidden|access denied|not permitted|not allowed"
    r"|internal (?:server )?error"
    r"|parameter|argument|required field|malformed|invalid request)",
    re.IGNORECASE,
)

#: JSON-RPC codes whose meaning is fixed by the protocol and is never a
#: statement about a conversation's history. ``-32603`` matters most of the
#: three: it is the code a server wraps *any* unhandled failure in, so a
#: transient backend outage surfaces under it constantly. ``-32602`` is
#: deliberately absent -- a request naming a conversation the provider does not
#: have is an invalid parameter, and it is what both real adapters answer a
#: genuinely missing restore with.
NON_DIAGNOSTIC_ERROR_CODES: frozenset[int] = frozenset({-32700, -32600, -32601, -32603})


def text_diagnoses_missing_history(text: str) -> bool:
    """Whether one line of provider prose states that the history is gone.

    The whole of the decision for claude, and the message half of it for the two
    protocol transports. Both halves of the vocabulary are consulted, so a
    sentence that says the conversation is missing *and* says why in transient
    or access terms is read as the outage it is.
    """
    if not text:
        return False
    if TRANSIENT_OR_ACCESS_RE.search(text):
        return False
    return bool(MISSING_HISTORY_RE.search(text))


@dataclass(frozen=True)
class ProviderRejection:
    """One JSON-RPC error response, kept structured instead of rendered.

    The adapters used to fold an error straight into a string and carry that,
    which meant every later reader had one sentence and no way to ask which part
    of it was the code. Keeping the three fields apart is what lets
    :meth:`diagnoses_missing_history` weigh a protocol-fixed code against the
    server's own words; :attr:`rendered` still produces the sentence, for the
    exception message and the operator-facing detail.
    """

    #: Whatever the provider put in ``code``, rendered as it arrived. Narrowed
    #: to an integer by :attr:`numeric_code` before any decision reads it, so a
    #: provider that answers with a string code still shows it to an operator
    #: without that string being weighed as a protocol code.
    code: Any = None
    message: str = ""
    data: Any = None

    @classmethod
    def from_payload(cls, error: object) -> ProviderRejection:
        """Read an ``error`` member, however little of it the provider filled in.

        A non-mapping error is not a protocol violation worth refusing over --
        some agents answer with a bare string -- so it becomes the message and
        the classification falls to the vocabulary alone.
        """
        if not isinstance(error, dict):
            return cls(message=str(error))
        return cls(
            code=error.get("code"),
            message=str(error.get("message", "")),
            data=error.get("data"),
        )

    @property
    def numeric_code(self) -> int | None:
        """The protocol code, or ``None`` when the provider sent no usable one.

        ``bool`` is an ``int`` in Python, so a forged ``"code": true`` would
        otherwise read as ``1`` -- a code no vocabulary knows, which quietly
        hands the whole decision to the message.
        """
        if isinstance(self.code, bool) or not isinstance(self.code, int):
            return None
        return self.code

    @property
    def rendered(self) -> str:
        """The one-line form carried into exceptions, logs, and pause details."""
        if self.code is not None:
            rendered = f"[{self.code}] {self.message}"
        else:
            rendered = self.message
        if self.data is not None:
            rendered = f"{rendered} {_render_data(self.data)}"
        return rendered

    def __str__(self) -> str:
        return self.rendered

    def diagnoses_missing_history(self) -> bool:
        """Whether this refusal is evidence the conversation itself is gone.

        Three gates, and all three have to agree:

        * the code is not one the protocol already spends on something else, so
          the internal-error code a server wraps every unhandled failure in can
          never retire a conversation on its own;
        * the message says the history does not exist, in the shared vocabulary
          above, because the code alone never says that -- both real providers
          answer a genuinely missing restore with the same invalid-params code
          they answer a dozen other things with;
        * neither the message nor the ``data`` blob says the refusal is about an
          outage, a credential, or a permission. ``data`` is consulted only for
          this veto and never as positive evidence: it is free-form, and a
          provider that echoes the request into it would otherwise be quoting
          the very identifier that is in question back as proof.
        """
        if self.numeric_code in NON_DIAGNOSTIC_ERROR_CODES:
            return False
        if self.data is not None and TRANSIENT_OR_ACCESS_RE.search(
            _render_data(self.data)
        ):
            return False
        return text_diagnoses_missing_history(self.message)


def _render_data(data: Any) -> str:
    try:
        return json.dumps(data, separators=(",", ":"))
    except TypeError, ValueError:
        # An error's ``data`` is whatever the provider put there. Something
        # unserializable is still worth reading, and losing the whole rendering
        # to it would lose the refusal's detail as well.
        return str(data)
