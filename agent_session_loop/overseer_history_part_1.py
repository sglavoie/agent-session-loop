# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405
from . import overseer_history_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .overseer_history_shared import *

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
