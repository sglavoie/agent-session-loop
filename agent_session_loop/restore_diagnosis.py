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

#: What a name looks like, as opposed to what a word looks like. The identifiers
#: providers mint are quoted, or announced as an "id", or carry a digit or a
#: separator: ``s-1``, ``c-3``, ``thread-77``, ``id=c-3``, ``"t-9"``. A bare
#: English word is none of those, and declining to read one as a name is what
#: keeps "session file", "thread pool" and "session storage" out of this
#: vocabulary: in each of them the word after the history noun is not the
#: history's name, it is the thing the sentence is actually about.
#:
#: The length bounds are not about identifiers being short. "Contains a digit or
#: a separator" is a claim about a whole token, so an unbounded form rescans the
#: token from every position inside it, and this runs over every line a CLI
#: harness prints -- one long line of base64 in a provider's diagnostics would
#: buy quadratic work. Sixty-four characters either side of the separator is
#: several times any identifier a provider mints, and a longer one falls to the
#: fail-closed default rather than to a stall.
_HISTORY_ID = r"(?:['\"`][^'\"`]+['\"`]|[\w.@+:-]{0,64}[\d_.@+:-][\w.@+:-]{0,64})"

#: The noun plus the name a provider may give it, so a verdict stays attached to
#: the subject it was passed about: ``session s-1 does not exist``, ``thread
#: "t-9" was deleted``, ``conversation id=c-3 no longer exists``. At most one
#: name -- room for the identifier of the history being judged, and no room for a
#: second subject to take the verb instead. The alternative, an unbounded gap,
#: reads "session/load failed: the settings file does not exist" as a disowned
#: history, and a gap of one *word* reads "session file not found" as one.
_NAMED_HISTORY = (
    rf"{_HISTORY_NOUN}\b"
    rf"(?:\s+(?:id|ids|identifier)\b(?:[\s:=]+{_HISTORY_ID})?"
    rf"|[\s:=]+{_HISTORY_ID})?"
)

#: Punctuation that closes the clause a history noun was the subject of, and the
#: predicates that continue one. Both exist for the two patterns whose verb comes
#: *before* the noun instead of after it, where nothing downstream of the noun
#: would otherwise have to agree with it: "no such session" is a whole sentence
#: about a session, while "no such session/load method" and "unknown session
#: storage backend" are sentences about a method and a backend in which the
#: history word is merely an adjective.
_CLAUSE_END = r"[.,;:!?()\[\]{}'\"`]"
_HISTORY_PREDICATE = (
    r"(?:found|exists?|existed|remains?|is|was|were|are|has|have|had"
    r"|with|for|in|on|to|by|of|from|anymore|yet|available|known|loaded"
    r"|active|open|named|matching|registered|selected)"
)

#: The noun, the name it may carry, and the evidence that the clause was about
#: it. Listing the predicates that may follow rather than the nouns that may not
#: is the fail-closed direction: there is no end to the infrastructure a provider
#: can put a history word in front of, so an unrecognized continuation leaves the
#: sentence unread rather than read as a retirement.
_HISTORY_AS_SUBJECT = (
    rf"{_NAMED_HISTORY}"
    rf"(?=[ \t]*(?:$|[\r\n]|{_CLAUSE_END}|{_HISTORY_PREDICATE}\b))"
)

#: What a provider says when the history named by the restore no longer exists.
#: Every alternative is a statement about that conversation's existence rather
#: than about this machine, this credential, or this moment, which is what makes
#: it safe to act on durably. "could not read", "permission denied" and the like
#: are deliberately absent: a history that cannot be reached right now is not a
#: history that is gone.
#:
#: Every alternative also names a history noun, and that is load-bearing rather
#: than incidental. A subject-free positive such as a bare "no such" is a
#: sentence about *something* being absent, and the things a provider reports
#: absent -- a host, a model, a file, a workspace -- are overwhelmingly not the
#: conversation. No vocabulary of transient words can rescue such a match
#: afterwards, because "no such host: api.example" says nothing transient; it
#: simply says nothing about history at all.
#:
#: Naming the noun is necessary and not sufficient, which is the second half of
#: the same rule. English builds compounds by putting one noun in front of
#: another, so "session file", "thread pool", "session/load method" and "session
#: storage backend" each contain a history word and each is about something else
#: entirely -- a configuration file, a worker pool, an RPC method, a storage
#: backend. Every alternative therefore requires the history noun to hold the
#: subject position: the verb either attaches to it directly, or the clause ends
#: where the noun and its identifier end.
MISSING_HISTORY_RE = re.compile(
    rf"(?:no\s+(?:such\s+)?{_HISTORY_AS_SUBJECT}"
    rf"|{_NAMED_HISTORY}\s+(?:was\s+)?not\s+found"
    rf"|{_NAMED_HISTORY}\s+(?:does\s+not\s+exist|no\s+longer\s+exists|was\s+deleted)"
    rf"|(?:unknown|invalid|expired)\s+{_HISTORY_AS_SUBJECT})",
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
    or access terms is read as the outage it is. The positive half insists on a
    conversation, session, or thread as the *subject* of the absence, so a line
    reporting some other missing thing -- including one whose name merely begins
    with a history word, like a session file or a thread pool -- is simply not an
    answer to this question.
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
        * the message says *the history* does not exist, in the shared
          vocabulary above -- naming a conversation, session, or thread as the
          subject of the absence rather than as an adjective in front of some
          piece of infrastructure, since a provider reports far more absent
          hosts, models, config files, and worker pools than absent
          conversations -- because the code alone never says that: both real
          providers answer a genuinely missing restore with the same
          invalid-params code they answer a dozen other things with;
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
