"""The ASL side of AgentMux's authorized transcript-evidence contract.

``overseer_policy_client`` asks whether the overseer may act. This module is
what acting on a ``context`` grant looks like: it runs
``amx overseer history --json``, reads the schema-v1 envelope, and turns the
answer into a :class:`History` the overseer can quote from. Like its sibling it
holds no state between calls, keeps nothing on disk, and has no configuration of
its own -- everything it knows comes back over stdout from the process it just
started.

## Why this is a separate question and not a bigger answer

Reading a conversation is ``context`` access, and the access category is not a
parameter here: it is fixed at :data:`~overseer_policy_client.ACCESS_CONTEXT`
and verified in the reply. A client that could ask for evidence under an
``action`` grant would be a client that could launder one permission into
another, and the request digest binds the category precisely so that cannot
happen quietly.

AgentMux authorizes twice on its side -- once for the plan before it discovers
anything, and once per conversation against the working directory that
conversation's identity metadata names -- and it does both before a message body
is opened. This client cannot verify that ordering from the outside. What it can
verify is that the answer it holds is an answer to the question it asked, and
that the answer respects the bounds it was asked under. Both are checked below,
because a caller that accepts whatever came back is a caller that can be handed
somebody else's transcripts.

## The bounds are part of the question

An answer taken under a 10-message bound is not evidence for a request that
asked for 200, and AgentMux binds the bounds into the request digest for that
reason. This client re-derives the same conclusion locally: an answer carrying
more conversations, more messages, or longer excerpts than were asked for is
:data:`FAILURE_ANSWER_MISMATCH`. That is not defensive duplication of AgentMux's
clamping. It is the difference between "the bounds were applied" and "the bounds
were reported", and only the first is a property of the evidence.

## Refusals must not describe what they withheld

A withheld conversation comes back as a harness, a session id, and a reason.
The path, the working directory, and the project are exactly what an exclusion
is about, so an answer that named them under the heading of explaining a refusal
would be handing back the withheld thing. This client refuses such an answer
outright rather than reading past the extra fields: a contract that leaks on one
side is not one this side should normalize.

## Unavailable is never permitted, and never cached

Every way the question can fail to be answered produces a :class:`History` with
``allowed`` false, ``available`` false, and an empty conversation tuple, under a
reason from :data:`FAILURE_REASONS`. There is no fallback and no cache, so a
failure cannot be quietly served from evidence gathered under configuration that
has since changed. ``available`` and ``allowed`` stay separate for the same
reason they do in the policy client: "AgentMux said no" is an operator decision
to respect and "AgentMux could not be asked" is a fault to report.

Durable records this boundary writes: **none**. It starts one subprocess and
returns what it said.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from . import overseer_policy_client

__all__ = [
    "DEFAULT_CONVERSATIONS",
    "DEFAULT_EXCERPT_CHARS",
    "DEFAULT_MESSAGES",
    "DEFAULT_TIMEOUT_SECONDS",
    "FAILURE_REASONS",
    "HISTORY_CONTRACT_NAME",
    "HISTORY_CONTRACT_VERSION",
    "HISTORY_SCHEMA_VERSION",
    "MAX_CONVERSATIONS",
    "MAX_EXCERPT_CHARS",
    "MAX_MESSAGES",
    "WITHHELD_FORBIDDEN_FIELDS",
    "Conversation",
    "Excerpt",
    "History",
    "Limits",
    "Withheld",
    "history_argv",
    "read_history",
]

#: The contract ``amx overseer history`` publishes inside the shared schema-v1
#: envelope. It versions independently of that envelope and of
#: ``overseer_policy``, so this pair is checked on its own.
HISTORY_CONTRACT_NAME = "overseer_history"
HISTORY_CONTRACT_VERSION = 1

#: The envelope version the command wraps its payload in.
HISTORY_SCHEMA_VERSION = 1

#: The bounds AgentMux applies when a request names none, and the ceilings it
#: clamps an over-large request to. They are mirrored here so a request can be
#: rejected before it is sent rather than silently clamped on arrival: a caller
#: that asked for 500 conversations and received 100 should be told it asked for
#: something this contract does not offer.
DEFAULT_CONVERSATIONS = 10
DEFAULT_MESSAGES = 20
DEFAULT_EXCERPT_CHARS = 2000
MAX_CONVERSATIONS = 100
MAX_MESSAGES = 200
MAX_EXCERPT_CHARS = 20000

#: Fields a withheld entry must never carry. Each one locates the conversation
#: that was withheld, which is the thing an exclusion exists to withhold.
WITHHELD_FORBIDDEN_FIELDS: tuple[str, ...] = (
    "path",
    "directory",
    "cwd",
    "projects",
    "project",
    "excerpts",
    "text",
)

#: How long one evidence request may take. Longer than an authorization
#: question, because this one walks a transcript corpus; still bounded, because
#: a hung read must become a refusal rather than a stalled overseer.
DEFAULT_TIMEOUT_SECONDS = 30.0

Failure = Literal[
    "request_unsendable",
    "client_missing",
    "client_timeout",
    "client_unusable",
    "response_unreadable",
    "response_malformed",
    "unsupported_envelope",
    "unsupported_contract",
    "request_rejected",
    "answer_mismatch",
]

#: The one refusal that is not a failure of transport or shape: a well-formed
#: answer to a question other than the one asked, or one that contradicts its
#: own bounds.
FAILURE_ANSWER_MISMATCH: Failure = "answer_mismatch"

#: Every way this client can fail to obtain evidence. It is the same vocabulary
#: the policy client uses, deliberately: an operator surface that already knows
#: how to read ``client_timeout`` from one overseer boundary should not have to
#: learn a second spelling for the other.
FAILURE_REASONS: tuple[Failure, ...] = (
    "request_unsendable",
    "client_missing",
    "client_timeout",
    "client_unusable",
    "response_unreadable",
    "response_malformed",
    "unsupported_envelope",
    "unsupported_contract",
    "request_rejected",
    "answer_mismatch",
)


@dataclass(frozen=True)
class Limits:
    """The bounds one evidence request was taken under.

    A request states them and the answer echoes them back after AgentMux has
    applied its defaults and ceilings, so the pair is comparable: the answer's
    limits may be smaller than the request's, never larger.
    """

    conversations: int = DEFAULT_CONVERSATIONS
    messages: int = DEFAULT_MESSAGES
    excerpt_chars: int = DEFAULT_EXCERPT_CHARS

    def exceeds_ceilings(self) -> str | None:
        """Why these bounds cannot be asked for, or ``None`` if they can."""
        for name, value, ceiling in (
            ("conversations", self.conversations, MAX_CONVERSATIONS),
            ("messages", self.messages, MAX_MESSAGES),
            ("excerpt_chars", self.excerpt_chars, MAX_EXCERPT_CHARS),
        ):
            if value < 1:
                return f"{name} bound {value} is not a positive count"
            if value > ceiling:
                return f"{name} bound {value} is above the contract ceiling {ceiling}"
        return None


@dataclass(frozen=True)
class Excerpt:
    """One bounded message from an authorized conversation.

    ``index`` is the message's position in the conversation's handoff-safe
    message sequence. It is what makes an excerpt quotable as evidence: it says
    where in the conversation the text came from rather than only that the
    conversation contained it.
    """

    index: int
    role: str
    timestamp: str
    text: str
    truncated: bool


@dataclass(frozen=True)
class Conversation:
    """One authorized conversation and the evidence taken from it."""

    harness: str
    session_id: str
    profiles: tuple[str, ...]
    directory: str
    projects: tuple[str, ...]
    path: str
    started_at: str
    last_at: str
    session_class: str
    message_count: int
    truncated: bool
    excerpts: tuple[Excerpt, ...]


@dataclass(frozen=True)
class Withheld:
    """One conversation that was discovered and not read.

    It names the conversation and the reason and nothing that locates it. That
    is not an omission to work around: an entry carrying more than this is
    rejected by :func:`read_history` rather than parsed.
    """

    harness: str
    session_id: str
    reason: str


@dataclass(frozen=True)
class History:
    """One evidence answer, verified against the question that was asked.

    ``allowed`` is the plan-level decision and ``available`` says whether there
    was an answer at all. Every failure sets both false and leaves
    ``conversations`` empty, so a caller that only branches on ``allowed`` is
    already fail closed.
    """

    plan_dir: str
    access: str
    repositories: tuple[overseer_policy_client.Repository, ...]
    limits: Limits
    allowed: bool
    available: bool
    reason: str
    failure: Failure | None
    summary: str
    config_fingerprint: str
    request_digest: str
    contract_version: int | None
    exit_status: int | None
    outcomes: tuple[overseer_policy_client.RepositoryOutcome, ...]
    examined: int
    truncated: bool
    conversations: tuple[Conversation, ...]
    withheld: tuple[Withheld, ...]
    warnings: tuple[str, ...]

    def grant(self) -> overseer_policy_client.Grant | None:
        """The permission this answer was taken under, or ``None``.

        It is the same :class:`~overseer_policy_client.Grant` an authorization
        answer confers, so evidence and the permission it was gathered under are
        recorded in one vocabulary. Like the policy client's, it requires an
        available, allowed answer carrying both identities: the fingerprint is
        what the ledger gates stored evidence on, and the digest is what proves
        which question -- and under which bounds -- was asked.
        """
        if not self.available or not self.allowed:
            return None
        if not self.request_digest or not self.config_fingerprint:
            return None
        return overseer_policy_client.Grant(
            plan_dir=self.plan_dir,
            access=self.access,
            request_digest=self.request_digest,
            config_fingerprint=self.config_fingerprint,
        )

    def withheld_reasons(self) -> tuple[str, ...]:
        """Every distinct reason a conversation was not returned, in order.

        An operator asking "is that all of it?" gets a real answer from this: a
        corpus that returned two conversations and withheld thirty for
        ``out_of_scope`` is a different situation from one that returned two and
        withheld nothing.
        """
        seen: list[str] = []
        for entry in self.withheld:
            if entry.reason and entry.reason not in seen:
                seen.append(entry.reason)
        return tuple(seen)


def history_argv(
    *,
    amx_path: str,
    plan_dir: str,
    repositories: Sequence[overseer_policy_client.Repository],
    limits: Limits,
    harnesses: Sequence[str] = (),
    since: str = "",
) -> list[str]:
    """Build the exact command one evidence request is asked with.

    ``--contract-version`` is sent explicitly for the same reason the policy
    client sends it: the command's default is whatever the installed ``amx``
    believes, which is the one value that cannot detect a version disagreement.

    The access category is not on the command line because the command does not
    take one -- reading a conversation is ``context`` access by construction --
    and this client verifies that the answer says so.
    """
    argv = [
        amx_path,
        "overseer",
        "history",
        "--json",
        "--contract-version",
        str(HISTORY_CONTRACT_VERSION),
        "--plan",
        plan_dir,
        "--limit",
        str(limits.conversations),
        "--messages",
        str(limits.messages),
        "--max-chars",
        str(limits.excerpt_chars),
    ]
    for repository in repositories:
        argv.extend(["--repository", repository.spec()])
    for harness in harnesses:
        argv.extend(["--harness", harness])
    if since:
        argv.extend(["--since", since])
    return argv


def read_history(
    *,
    plan_dir: str,
    repositories: Sequence[overseer_policy_client.Repository],
    limits: Limits | None = None,
    harnesses: Sequence[str] = (),
    since: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    resolve_binary: Callable[[str], str | None] = shutil.which,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> History:
    """Ask AgentMux for bounded transcript evidence and verify the answer.

    It is a question each time, never a lease. AgentMux reads configuration on
    every invocation and re-authorizes every conversation on every request, so
    asking again is the only way to see a withdrawn opt-in -- and this client
    keeps nothing that would let a caller skip the asking.
    """
    repositories = tuple(repositories)
    harnesses = tuple(harnesses)
    bounds = limits if limits is not None else Limits()

    problem = _unsendable(
        plan_dir=plan_dir,
        repositories=repositories,
        limits=bounds,
        since=since,
    )
    if problem is not None:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=bounds,
            failure="request_unsendable",
            summary=problem,
            exit_status=None,
        )

    amx_path = resolve_binary(overseer_policy_client.AMX_BINARY)
    if not amx_path:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=bounds,
            failure="client_missing",
            summary=(
                f"no {overseer_policy_client.AMX_BINARY} executable is available, "
                "so transcript evidence could not be requested"
            ),
            exit_status=None,
        )

    argv = history_argv(
        amx_path=amx_path,
        plan_dir=plan_dir,
        repositories=repositories,
        limits=bounds,
        harnesses=harnesses,
        since=since,
    )
    try:
        completed = run_command(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=bounds,
            failure="client_timeout",
            summary=(
                f"{overseer_policy_client.AMX_BINARY} overseer history did not "
                f"answer in {timeout}s"
            ),
            exit_status=None,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=bounds,
            failure="client_unusable",
            summary=(
                f"{overseer_policy_client.AMX_BINARY} overseer history could not "
                f"be run: {error}"
            ),
            exit_status=None,
        )

    return _read_answer(
        plan_dir=plan_dir,
        repositories=repositories,
        limits=bounds,
        stdout=completed.stdout or "",
        exit_status=_exit_status(completed.returncode),
    )


def _unsendable(
    *,
    plan_dir: str,
    repositories: Sequence[overseer_policy_client.Repository],
    limits: Limits,
    since: str,
) -> str | None:
    """Why this request must not be sent, or ``None`` if it is well formed.

    The repository rules are the policy client's, unchanged: the two boundaries
    name a plan's repositories the same way or one of them is asking about
    something the other never authorized.
    """
    if not plan_dir:
        return "no plan directory was named, so there is nothing to read evidence for"
    if not repositories:
        return (
            "no repositories were named; a plan participating in nothing has no "
            "conversations to read"
        )
    seen: set[str] = set()
    for repository in repositories:
        if not repository.alias or not repository.path:
            return (
                "every repository must carry both an alias and a resolved path; "
                f"{repository!r} does not"
            )
        if not repository.path.startswith(("/", "~")):
            return (
                f"repository path {repository.path!r} is neither absolute nor "
                "rooted at ~, and would be resolved against the wrong directory"
            )
        if repository.alias in seen:
            return f"repository alias {repository.alias!r} was named twice"
        seen.add(repository.alias)
    bound_problem = limits.exceeds_ceilings()
    if bound_problem is not None:
        return bound_problem
    if since and not _looks_like_timestamp(since):
        return f"since {since!r} is not an RFC 3339 timestamp"
    return None


def _looks_like_timestamp(value: str) -> bool:
    """A cheap shape check, not a parse.

    The command owns the real parse and rejects a malformed value with a
    structured error. This exists only so an obviously wrong value is refused
    before a process is started for it.
    """
    return len(value) >= len("2006-01-02T15:04:05Z") and value[4] == value[7] == "-"


def _read_answer(
    *,
    plan_dir: str,
    repositories: tuple[overseer_policy_client.Repository, ...],
    limits: Limits,
    stdout: str,
    exit_status: int | None,
) -> History:
    """Parse and verify one history answer, refusing anything unrecognizable."""
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as error:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="response_unreadable",
            summary=f"history output was not JSON: {error}",
            exit_status=exit_status,
        )
    if not isinstance(parsed, dict):
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="response_malformed",
            summary="history output was not a JSON object",
            exit_status=exit_status,
        )

    schema_version = parsed.get("schema_version")
    if schema_version != HISTORY_SCHEMA_VERSION:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="unsupported_envelope",
            summary=(
                f"history envelope schema {schema_version!r} is not the schema "
                f"{HISTORY_SCHEMA_VERSION} this client speaks"
            ),
            exit_status=exit_status,
        )

    error_payload = parsed.get("error")
    if isinstance(error_payload, dict):
        code = _text(error_payload.get("code")) or "unknown"
        message = _text(error_payload.get("message")) or "no message"
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="request_rejected",
            summary=f"history request was rejected ({code}): {message}",
            exit_status=exit_status,
        )

    data = parsed.get("data")
    if not isinstance(data, dict):
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="response_malformed",
            summary="history envelope carried no data object",
            exit_status=exit_status,
        )

    contract = data.get("contract")
    if not isinstance(contract, dict):
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="unsupported_contract",
            summary="history payload named no contract",
            exit_status=exit_status,
        )
    contract_version = _version(contract.get("version"))
    if (
        _text(contract.get("name")) != HISTORY_CONTRACT_NAME
        or contract_version != HISTORY_CONTRACT_VERSION
    ):
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="unsupported_contract",
            summary=(
                f"history payload is {_text(contract.get('name'))!r} version "
                f"{contract_version!r}, not {HISTORY_CONTRACT_NAME} version "
                f"{HISTORY_CONTRACT_VERSION}"
            ),
            exit_status=exit_status,
            contract_version=contract_version,
        )

    allowed = data.get("allowed")
    fingerprint = _text(data.get("config_fingerprint"))
    digest = _text(data.get("request_digest"))
    if not isinstance(allowed, bool) or not fingerprint or not digest:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="response_malformed",
            summary=(
                "history payload is missing the decision, the configuration "
                "fingerprint, or the request digest"
            ),
            exit_status=exit_status,
            contract_version=contract_version,
        )

    applied = _read_limits(data.get("limits"))
    if applied is None:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="response_malformed",
            summary="history payload carried no readable limits block",
            exit_status=exit_status,
            contract_version=contract_version,
        )

    outcomes, outcomes_malformed = _read_outcomes(data.get("repositories"))
    if outcomes_malformed:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="response_malformed",
            summary="history payload carried an unreadable repository outcome",
            exit_status=exit_status,
            contract_version=contract_version,
        )

    conversations = _read_conversations(data.get("conversations"))
    if conversations is None:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="response_malformed",
            summary="history payload carried an unreadable conversation",
            exit_status=exit_status,
            contract_version=contract_version,
        )

    withheld, leak = _read_withheld(data.get("withheld"))
    if leak is not None:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure=FAILURE_ANSWER_MISMATCH,
            summary=leak,
            exit_status=exit_status,
            contract_version=contract_version,
        )
    if withheld is None:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="response_malformed",
            summary="history payload carried an unreadable withheld entry",
            exit_status=exit_status,
            contract_version=contract_version,
        )

    mismatch = _mismatch(
        data=data,
        plan_dir=plan_dir,
        repositories=repositories,
        requested=limits,
        applied=applied,
        allowed=allowed,
        outcomes=outcomes,
        conversations=conversations,
        exit_status=exit_status,
    )
    if mismatch is not None:
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure=FAILURE_ANSWER_MISMATCH,
            summary=mismatch,
            exit_status=exit_status,
            contract_version=contract_version,
        )

    return History(
        plan_dir=plan_dir,
        access=overseer_policy_client.ACCESS_CONTEXT,
        repositories=repositories,
        limits=applied,
        allowed=allowed,
        available=True,
        reason=_text(data.get("reason")) or "",
        failure=None,
        summary=_text(data.get("summary")) or "",
        config_fingerprint=fingerprint,
        request_digest=digest,
        contract_version=contract_version,
        exit_status=exit_status,
        outcomes=outcomes,
        examined=_count(data.get("examined")),
        truncated=bool(data.get("truncated")),
        conversations=conversations,
        withheld=withheld,
        warnings=_read_strings(data.get("warnings")),
    )


def _mismatch(
    *,
    data: dict[str, Any],
    plan_dir: str,
    repositories: tuple[overseer_policy_client.Repository, ...],
    requested: Limits,
    applied: Limits,
    allowed: bool,
    outcomes: tuple[overseer_policy_client.RepositoryOutcome, ...],
    conversations: tuple[Conversation, ...],
    exit_status: int | None,
) -> str | None:
    """Why this answer is not an answer to the question that was asked.

    The plan, access, and repository clauses are the policy client's and catch
    the same three ways an answer could be carried somewhere it does not belong.
    The bound clauses are this contract's own: they check that the evidence
    respects the limits rather than merely reporting them, which is the
    difference between a bound that was applied and one that was announced. The
    last two catch an answer that disagrees with itself.
    """
    if _text(data.get("plan")) != plan_dir:
        return (
            f"history answered about plan {_text(data.get('plan'))!r}, not {plan_dir!r}"
        )
    if _text(data.get("access")) != overseer_policy_client.ACCESS_CONTEXT:
        return (
            f"history answered under access {_text(data.get('access'))!r}, not "
            f"{overseer_policy_client.ACCESS_CONTEXT!r}; reading a conversation "
            "is context access"
        )
    answered = tuple((outcome.alias, outcome.path) for outcome in outcomes)
    asked = tuple((repository.alias, repository.path) for repository in repositories)
    if answered != asked:
        return (
            "history answered about a different repository set: asked about "
            f"{list(asked)}, answered about {list(answered)}"
        )
    for name, applied_value, asked_value in (
        ("conversations", applied.conversations, requested.conversations),
        ("messages", applied.messages, requested.messages),
        ("excerpt_chars", applied.excerpt_chars, requested.excerpt_chars),
    ):
        if applied_value > asked_value:
            return (
                f"history applied a {name} bound of {applied_value} for a request "
                f"that asked for {asked_value}"
            )
    if len(conversations) > applied.conversations:
        return (
            f"history returned {len(conversations)} conversations under a bound "
            f"of {applied.conversations}"
        )
    for conversation in conversations:
        if len(conversation.excerpts) > applied.messages:
            return (
                f"conversation {conversation.session_id!r} carried "
                f"{len(conversation.excerpts)} excerpts under a bound of "
                f"{applied.messages}"
            )
        for excerpt in conversation.excerpts:
            if len(excerpt.text) > applied.excerpt_chars:
                return (
                    f"conversation {conversation.session_id!r} excerpt "
                    f"{excerpt.index} is {len(excerpt.text)} characters under a "
                    f"bound of {applied.excerpt_chars}"
                )
        if not conversation.directory:
            return (
                f"conversation {conversation.session_id!r} names no working "
                "directory, so nothing about it was authorized"
            )
    if allowed and any(not outcome.allowed for outcome in outcomes):
        return "history allowed the plan while refusing one of its repositories"
    if allowed and exit_status not in (0, None):
        return (
            f"history allowed the plan and exited {exit_status}; a permitted "
            "answer exits zero"
        )
    if not allowed and conversations:
        return (
            f"history refused the plan and still returned {len(conversations)} "
            "conversations"
        )
    return None


def _read_limits(value: Any) -> Limits | None:
    if not isinstance(value, dict):
        return None
    conversations = _count(value.get("conversations"))
    messages = _count(value.get("messages"))
    excerpt_chars = _count(value.get("excerpt_chars"))
    if conversations < 1 or messages < 1 or excerpt_chars < 1:
        return None
    return Limits(
        conversations=conversations,
        messages=messages,
        excerpt_chars=excerpt_chars,
    )


def _read_conversations(value: Any) -> tuple[Conversation, ...] | None:
    """Parse the conversations, refusing the whole answer on any unusable entry.

    A malformed entry is not dropped. Dropping one would turn a broken answer
    into a smaller-looking good one, and "fewer conversations than there are" is
    indistinguishable from a legitimate bound.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        return None
    conversations: list[Conversation] = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        harness = _text(entry.get("harness"))
        session_id = _text(entry.get("id"))
        if not harness or not session_id:
            return None
        excerpts = _read_excerpts(entry.get("excerpts"))
        if excerpts is None:
            return None
        conversations.append(
            Conversation(
                harness=harness,
                session_id=session_id,
                profiles=_read_strings(entry.get("profiles")),
                directory=_text(entry.get("directory")) or "",
                projects=_read_strings(entry.get("projects")),
                path=_text(entry.get("path")) or "",
                started_at=_text(entry.get("started_at")) or "",
                last_at=_text(entry.get("last_at")) or "",
                session_class=_text(entry.get("session_class")) or "",
                message_count=_count(entry.get("message_count")),
                truncated=bool(entry.get("truncated")),
                excerpts=excerpts,
            )
        )
    return tuple(conversations)


def _read_excerpts(value: Any) -> tuple[Excerpt, ...] | None:
    if value is None:
        return ()
    if not isinstance(value, list):
        return None
    excerpts: list[Excerpt] = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        role = _text(entry.get("role"))
        text = entry.get("text")
        index = entry.get("index")
        if not role or not isinstance(text, str):
            return None
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return None
        excerpts.append(
            Excerpt(
                index=index,
                role=role,
                timestamp=_text(entry.get("timestamp")) or "",
                text=text,
                truncated=bool(entry.get("truncated")),
            )
        )
    return tuple(excerpts)


def _read_withheld(value: Any) -> tuple[tuple[Withheld, ...] | None, str | None]:
    """Parse the withheld entries, and refuse any that describes what it hid.

    The second return value is the leak, reported separately from a shape
    problem because they mean different things: an unreadable entry is a broken
    answer, and an entry carrying a path is a contract this side must not read
    past.
    """
    if value is None:
        return (), None
    if not isinstance(value, list):
        return None, None
    entries: list[Withheld] = []
    for entry in value:
        if not isinstance(entry, dict):
            return None, None
        leaked = [field for field in WITHHELD_FORBIDDEN_FIELDS if field in entry]
        if leaked:
            return None, (
                "history described a withheld conversation with "
                f"{', '.join(sorted(leaked))}; a refusal must not name what it "
                "withheld"
            )
        harness = _text(entry.get("harness"))
        session_id = _text(entry.get("id"))
        reason = _text(entry.get("reason"))
        if not harness or not session_id or not reason:
            return None, None
        entries.append(Withheld(harness=harness, session_id=session_id, reason=reason))
    return tuple(entries), None


def _read_outcomes(
    value: Any,
) -> tuple[tuple[overseer_policy_client.RepositoryOutcome, ...], bool]:
    """Parse the per-repository outcomes, reporting whether any is unusable.

    The type is the policy client's, because it is the same answer about the
    same repositories: an evidence request reports its repository decisions in
    the vocabulary an authorization request established, so an operator surface
    reads one shape from both boundaries. A malformed entry raises the flag
    rather than being dropped, since a shrunken answered set is exactly what the
    comparison against the asked set exists to catch.
    """
    if not isinstance(value, list):
        return (), True
    outcomes: list[overseer_policy_client.RepositoryOutcome] = []
    for entry in value:
        if not isinstance(entry, dict):
            return (), True
        allowed = entry.get("allowed")
        path = _text(entry.get("path"))
        if not isinstance(allowed, bool) or not path:
            return (), True
        outcomes.append(
            overseer_policy_client.RepositoryOutcome(
                alias=_text(entry.get("alias")) or "",
                path=path,
                resolved_path=_text(entry.get("resolved_path")) or "",
                allowed=allowed,
                reason=_text(entry.get("reason")) or "",
                detail=_text(entry.get("detail")) or "",
            )
        )
    return tuple(outcomes), False


def _read_strings(value: Any) -> tuple[str, ...]:
    """Read an optional list of names, tolerating its absence.

    These lists -- profiles, projects, warnings -- are descriptive rather than
    load bearing: nothing is authorized on them, so a missing or oddly typed one
    reads as empty instead of failing the whole answer.
    """
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _failed(
    *,
    plan_dir: str,
    repositories: Sequence[overseer_policy_client.Repository],
    limits: Limits,
    failure: Failure,
    summary: str,
    exit_status: int | None,
    contract_version: int | None = None,
) -> History:
    """The answer every unanswered request produces.

    One constructor for all of them, so no failure path can accidentally build
    an answer carrying conversations, a grant identity, or a permitted decision.
    """
    return History(
        plan_dir=plan_dir,
        access=overseer_policy_client.ACCESS_CONTEXT,
        repositories=tuple(repositories),
        limits=limits,
        allowed=False,
        available=False,
        reason="",
        failure=failure,
        summary=summary,
        config_fingerprint="",
        request_digest="",
        contract_version=contract_version,
        exit_status=exit_status,
        outcomes=(),
        examined=0,
        truncated=False,
        conversations=(),
        withheld=(),
        warnings=(),
    )


def _exit_status(value: Any) -> int | None:
    """The process status, or ``None`` when the runner did not report one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _version(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
