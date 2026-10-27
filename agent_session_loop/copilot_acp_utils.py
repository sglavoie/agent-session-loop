# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F405
"""Small ACP message-shape helpers."""

from .copilot_acp_types import *  # noqa: F403


def _params(message: Mapping[str, Any]) -> Mapping[str, Any]:
    params = message.get("params")
    return params if isinstance(params, Mapping) else {}


def _select_option(options: Sequence[Any], wanted: str) -> str | None:
    """Return the id of the first option of ``wanted`` kind, or ``None``.

    Kinds this client refuses outright are skipped before anything else, so an
    agent cannot get a standing grant selected by offering it under a name that
    happens to sort first.
    """
    for option in options:
        if not isinstance(option, Mapping):
            continue
        kind = option.get("kind")
        # Type first: a list or object here is unhashable, and the set lookup
        # would raise on it instead of passing over an option it cannot use.
        if not isinstance(kind, str) or kind in _STANDING_KINDS or kind != wanted:
            continue
        option_id = option.get("optionId")
        if isinstance(option_id, str) and option_id:
            return option_id
    return None


def _tool_label(tool_call: object) -> str:
    """A short, agent-authored description of what permission was asked for."""
    if not isinstance(tool_call, Mapping):
        return "unnamed tool call"
    for key in ("title", "toolName", "name", "kind", "toolCallId"):
        label = _text_or_empty(tool_call.get(key))
        if label:
            return label
    return "unnamed tool call"


def _content_text(content: object) -> str:
    """The text of one ACP content block, or ``""`` for any other kind."""
    if not isinstance(content, Mapping):
        return ""
    if content.get("type") != "text":
        return ""
    return _text_or_empty(content.get("text"))


def _text_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""
