"""Decode one line of a harness's JSON stream into text ASL can write."""

from __future__ import annotations

import json
import re
from typing import Any

#: Any ``\uD800``-``\uDFFF`` escape, paired or not. A line without one cannot
#: decode to a surrogate, so most lines skip the walk entirely.
_SURROGATE_ESCAPE_RE = re.compile(r"\\u[dD][89a-fA-F]")
_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def decode_harness_line(line: str) -> Any:
    """``json.loads`` for a harness line, with every lone surrogate replaced.

    A JS CLI that slices a string mid-emoji emits a lone ``\\ud83d`` escape,
    which ``json.loads`` keeps and every strict UTF-8 write or print of that
    text refuses, so each one becomes U+FFFD here, as an undecodable pipe byte
    already does. Pathological nesting is reported as the ``ValueError`` a
    malformed line raises, so a caller's plain-text fallback covers both.
    """
    try:
        decoded = json.loads(line)
    except RecursionError as error:
        raise ValueError("harness line is nested too deeply to decode") from error
    if _SURROGATE_ESCAPE_RE.search(line) is None:
        return decoded
    return _replace_lone_surrogates(decoded)


def _replace_lone_surrogates(value: Any) -> Any:
    # Iterative, because a document json.loads accepted can still be nested
    # deeper than the interpreter's recursion limit.
    def clean(item: Any) -> Any:
        if isinstance(item, str):
            return _LONE_SURROGATE_RE.sub("\ufffd", item)
        return item

    root = clean(value)
    pending: list[Any] = [root]
    while pending:
        container = pending.pop()
        if isinstance(container, dict):
            items = list(container.items())
            container.clear()
            for key, item in items:
                cleaned = clean(item)
                container[clean(key)] = cleaned
                pending.append(cleaned)
        elif isinstance(container, list):
            for index, item in enumerate(container):
                cleaned = clean(item)
                container[index] = cleaned
                pending.append(cleaned)
    return root
