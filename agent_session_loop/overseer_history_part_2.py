# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import overseer_history_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .overseer_history_shared import *
from .overseer_history_part_1 import *


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
    except RecursionError:
        # Not a ValueError: pathologically nested JSON raises it from the
        # decoder instead of a decode error.
        return _failed(
            plan_dir=plan_dir,
            repositories=repositories,
            limits=limits,
            failure="response_unreadable",
            summary="history output was nested too deeply to decode",
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
