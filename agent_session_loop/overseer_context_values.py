"""Overseer context values."""

from __future__ import annotations

import json
from typing import Any

from . import (
    overseer_context_models as models,
)
from . import (
    overseer_context_scope as scope_module,
)
from . import (
    overseer_policy_client,
)


def _withheld(
    *,
    plan_dir: str,
    run_id: str,
    incident_id: int,
    kind: str,
    summary: str,
    grant: overseer_policy_client.Grant | None,
    scope: str,
    captured_at: str,
    refusal: models.Refusal,
    withheld: int,
) -> models.Packet:
    """A packet that describes nothing, and says so."""
    return models.Packet(
        plan_dir=plan_dir,
        run_id=run_id,
        incident_id=incident_id,
        kind=kind,
        summary=summary,
        questions=(),
        request_digest=scope_module._request_digest(grant),
        config_fingerprint=(grant.config_fingerprint if grant is not None else ""),
        scope=scope,
        sources=(),
        gaps=(),
        captured_at=captured_at,
        withheld=withheld,
        refusal=refusal,
    )


def _unavailable(
    *,
    kind: models.SourceKind,
    locator: str,
    refusal: models.Refusal,
) -> models.Excerpt:
    """An excerpt that carries no text, and says why."""
    return models.Excerpt(
        kind=kind,
        space=models.STATE_SPACE,
        locator=locator,
        text="",
        digest="",
        recorded_digest="",
        size=0,
        truncated=False,
        refusal=refusal,
    )


def _named(value: Any) -> list[str]:
    """Name the entries of a list of check records, however they are shaped."""
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for index, entry in enumerate(value):
        if isinstance(entry, dict):
            label = entry.get("name") or entry.get("label") or entry.get("command")
            names.append(str(label) if label else f"check[{index}]")
        else:
            names.append(_clip(str(entry), 80))
    return names


def _lines(fields: tuple[tuple[str, Any], ...] | list[tuple[str, Any]]) -> str:
    """One ``key: value`` line per field, omitting the ones with nothing in them."""
    rendered = []
    for key, value in fields:
        if value is None or value == [] or value == {}:
            continue
        rendered.append(f"{key}: {_value(value)}")
    return "\n".join(rendered)


def _value(value: Any, *, limit: int = 240) -> str:
    """Render one field compactly, whatever JSON put there."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        rendered = value
    elif isinstance(value, int | float):
        rendered = str(value)
    else:
        try:
            rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
        except TypeError, ValueError:
            rendered = repr(value)
    return _clip(rendered, limit)


def _clip(text: str, limit: int) -> str:
    """One line, no longer than ``limit``, with the truncation visible."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(limit - 3, 0)].rstrip() + "..."


def _first(*values: Any) -> str:
    """The first value that is a non-empty string, or ``""``."""
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""
