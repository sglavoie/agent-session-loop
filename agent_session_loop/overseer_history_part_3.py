# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import overseer_history_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .overseer_history_shared import *
from .overseer_history_part_1 import *
from .overseer_history_part_2 import *


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
