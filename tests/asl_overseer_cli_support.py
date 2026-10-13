"""Shared constants and injected boundaries for the overseer CLI test modules."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_session_loop import (
    clock,
    overseer_decisions,
    overseer_history,
    overseer_policy_client,
)

#: The fixed moment the fake clock starts from, deliberately ahead of any real
#: "now": see the module docstring on why a lease taken in the past would make
#: every dispatch fail for the wrong reason.
EPOCH = datetime(2099, 6, 19, 12, 0, 0, tzinfo=UTC)

WATCHER = "watcher-a"
FINGERPRINT = "fp-1"
DIGEST = "rd-1"
HARNESS = "claude"
SESSION = "session-7"
#: The ASL session the fixture's run is on. Deliberately not `SESSION`: a
#: harness conversation id and a plan session id are different vocabularies, and
#: `verify` reads the second one to find the contract it may run a check from.
PLAN_SESSION = "7"
#: The access category a mutation and its reconciliation are both asked under.
ACTION = overseer_policy_client.ACCESS_ACTION

#: Where a registered fixture repository claims to come from. A registry entry
#: names an origin and `workspaces.resolve_workspace` checks it against Git, so
#: both sides of that comparison are answered from this one template.
REGISTERED_ORIGIN = "https://github.com/example/{alias}.git"

#: A conversation class whose ``user`` turns are written by an orchestrating
#: agent rather than a person, and which AgentMux does not publish. The fixture
#: names one so `--generated-class` has something to mark that the default
#: classification would have refused for a different reason.
GENERATED_CLASS = "orchestrated"


class _Clock:
    """A clock that moves only when a test says so."""

    def __init__(self, start: datetime = EPOCH) -> None:
        self.now = start

    def utc_now(self) -> datetime:
        return self.now

    def utc_iso_now(self) -> str:
        return clock.isoformat(self.now)

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _Policy:
    """A stand-in for ``overseer_policy_client.authorize``.

    Records every question so a test can show a leaf asked again rather than
    reusing an answer, and can be told to refuse or to go unreachable -- which
    are different answers and have to stay different.

    ``on_ask`` runs once the answer is settled and before the caller sees it,
    which is the only place a test can change the world *between* two of the
    dispatcher's own guards rather than before or after all of them.
    """

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []
        self.refused: set[str] = set()
        self.unavailable: set[str] = set()
        self.fingerprint = FINGERPRINT
        self.digest = DIGEST
        self.on_ask: Callable[[str], None] | None = None

    def __call__(
        self,
        *,
        plan_dir: str,
        access: str,
        repositories: tuple[overseer_policy_client.Repository, ...],
        timeout: float,
    ) -> overseer_policy_client.Decision:
        self.asked.append({"plan_dir": plan_dir, "access": access})
        available = plan_dir not in self.unavailable
        allowed = available and plan_dir not in self.refused
        if self.on_ask is not None:
            self.on_ask(access)
        return overseer_policy_client.Decision(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            allowed=allowed,
            available=available,
            reason="opted_in" if allowed else "excluded",
            failure=None if available else "client_timeout",
            summary="1 repository" if available else "amx did not answer in time",
            config_fingerprint=self.fingerprint if available else "",
            request_digest=self.digest if available else "",
            contract_version=1 if available else None,
            exit_status=0 if allowed else 1,
            outcomes=(),
        )

    def asked_under(self, access: str) -> int:
        return sum(1 for item in self.asked if item["access"] == access)


class _Transcript:
    """A stand-in for ``overseer_history.read_history``.

    Hands back one conversation in the repository AgentMux authorized, so a
    ``user`` turn in it is the only thing in the whole fixture that can carry an
    authorization. The directory has to be the repository the run declares and
    not the workspace above it: a workspace is not a project, and a decision
    typed one level up reaches none of the projects beneath it.

    ``fail`` switches it to an answer that could not be obtained, which has to
    stay distinguishable from an answer that said nothing.

    ``on_read`` runs once an answer has been assembled, which is how a test puts
    a turn into the transcript *between* two readings of it. A person typing
    while an action is in flight is a real sequence, and reproducing it means
    changing the record at the moment the code under test is between its two
    looks at it.
    """

    def __init__(self, project: str) -> None:
        self.project = project
        self.calls: list[dict[str, Any]] = []
        self.failure: overseer_history.Failure | None = None
        self.session_class = overseer_decisions.SESSION_CLASS_MAIN
        self.on_read: Callable[[int], None] | None = None
        self.turns: list[tuple[str, str]] = [
            ("user", "answer routine tooling questions on my runs without asking me")
        ]
        self.elsewhere: list[tuple[str, str, list[tuple[str, str]]]] = []

    def __call__(self, **kwargs: Any) -> overseer_history.History:
        self.calls.append(kwargs)
        if self.failure is not None:
            return self._unavailable()
        answer = self._history(
            allowed=True,
            available=True,
            reason="opted_in",
            conversations=(
                self._conversation(SESSION, self.project, self.turns),
                *(
                    self._conversation(session, project, turns)
                    for session, project, turns in self.elsewhere
                ),
            ),
        )
        if self.on_read is not None:
            self.on_read(len(self.calls))
        return answer

    def also_in(self, project: str, text: str) -> str:
        """Add a conversation in another project, and name the turn it holds.

        A plan that spans two repositories can only be spoken for by someone
        speaking in each of them, and those are two different conversations in
        two different directories. Returns the statement id of the turn, which
        is what a claim cites.
        """
        session = f"{SESSION}-{len(self.elsewhere) + 2}"
        self.elsewhere.append((session, project, [("user", text)]))
        return f"{overseer_decisions.TRANSCRIPT_SPACE}:{HARNESS}:{session}#0"

    def statement_id(self, index: int = 0) -> str:
        return f"{overseer_decisions.TRANSCRIPT_SPACE}:{HARNESS}:{SESSION}#{index}"

    def _conversation(
        self, session: str, project: str, turns: list[tuple[str, str]]
    ) -> overseer_history.Conversation:
        excerpts = tuple(
            overseer_history.Excerpt(
                index=index,
                role=role,
                timestamp=clock.isoformat(EPOCH - timedelta(hours=index + 1)),
                text=text,
                truncated=False,
            )
            for index, (role, text) in enumerate(turns)
        )
        return overseer_history.Conversation(
            harness=HARNESS,
            session_id=session,
            profiles=(),
            directory=project,
            projects=(project,),
            path=f"{project}/.transcripts/{session}.jsonl",
            started_at=clock.isoformat(EPOCH - timedelta(days=1)),
            last_at=clock.isoformat(EPOCH - timedelta(hours=1)),
            session_class=self.session_class,
            message_count=len(excerpts),
            truncated=False,
            excerpts=excerpts,
        )

    def _unavailable(self) -> overseer_history.History:
        return self._history(
            allowed=False,
            available=False,
            reason="unreachable",
            conversations=(),
            failure=self.failure,
        )

    def _history(
        self,
        *,
        allowed: bool,
        available: bool,
        reason: str,
        conversations: tuple[overseer_history.Conversation, ...],
        failure: overseer_history.Failure | None = None,
    ) -> overseer_history.History:
        return overseer_history.History(
            plan_dir="",
            access=overseer_policy_client.ACCESS_CONTEXT,
            repositories=(),
            limits=overseer_history.Limits(),
            allowed=allowed,
            available=available,
            reason=reason,
            failure=failure,
            summary="1 conversation" if available else "amx did not answer",
            config_fingerprint=FINGERPRINT if available else "",
            request_digest=DIGEST if available else "",
            contract_version=1 if available else None,
            exit_status=0 if available else 1,
            outcomes=(),
            examined=len(conversations),
            truncated=False,
            conversations=conversations,
            withheld=(),
            warnings=(),
        )
