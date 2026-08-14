"""Shared parsing for provider reset and retry hints.

Harnesses report reset times in several human-oriented forms.  Keeping the
normalizer here makes the runner, the Spark availability ledger, and the ASL
supervisor agree on the instant a hint names.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_WHITESPACE_RE = re.compile(r"\s+")
_RETRY_AT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"retry-after:\s*(\d+)", re.IGNORECASE),
    re.compile(r"resets?\s+(?:at|on|in)\s+([^\n.]{1,48})", re.IGNORECASE),
    re.compile(r"reset\s+(?:at|on|in)\s+([^\n.]{1,48})", re.IGNORECASE),
    re.compile(r"try again\s+(?:at|after|in|on)\s+([^\n.]{1,48})", re.IGNORECASE),
    re.compile(r"will reset (?:at|on|in)\s+([^\n.]{1,48})", re.IGNORECASE),
)
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_DURATION_RE = re.compile(r"^(\d+)\s*(hours?|minutes?)$", re.IGNORECASE)
_CLOCK_RE = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?"
    r"(?:\s*\(([A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+)\))?$",
    re.IGNORECASE,
)


def extract_retry_hint(text: str) -> str | None:
    """Extract a bounded provider retry/reset hint from harness text."""
    for pattern in _RETRY_AT_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        value = _WHITESPACE_RE.sub(" ", match.group(1)).strip(" .,;:")
        if value:
            return value[:60]
    return None


def _shift(base: datetime, seconds: float) -> datetime | None:
    try:
        return base + timedelta(seconds=seconds)
    except OverflowError:
        return None


def resolve_retry_at(value: object, *, now: datetime | None = None) -> datetime | None:
    """Resolve a supported hint to a timezone-aware absolute instant."""
    if not isinstance(value, str) or not value.strip():
        return None
    hint = value.strip()
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)

    if hint.isdigit():
        return _shift(current, int(hint))

    duration = _DURATION_RE.fullmatch(hint)
    if duration is not None:
        seconds = 3600 if duration.group(2).lower().startswith("hour") else 60
        return _shift(current, int(duration.group(1)) * seconds)

    if _ISO_RE.match(hint):
        text = hint[:-1] + "+00:00" if hint[-1] in "Zz" else hint
        try:
            parsed = datetime.fromisoformat(text)
            return parsed.astimezone() if parsed.tzinfo is None else parsed
        except ValueError, OverflowError, OSError:
            return None

    match = _CLOCK_RE.fullmatch(hint)
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "").lower()
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif hour > 23:
        return None
    zone_name = match.group(4)
    try:
        local_now = (
            current.astimezone(ZoneInfo(zone_name))
            if zone_name
            else current.astimezone()
        )
        target = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= local_now:
            target += timedelta(days=1)
        return target
    except KeyError, ValueError, OverflowError, OSError:
        return None


def normalized_retry_at(value: object, *, now: datetime | None = None) -> str | None:
    """Return a canonical UTC ISO-8601 timestamp for a supported hint."""
    resolved = resolve_retry_at(value, now=now)
    if resolved is None:
        return None
    return resolved.astimezone(UTC).isoformat().replace("+00:00", "Z")
