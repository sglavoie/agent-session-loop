"""Stable, opaque identifiers for plans and runs.

The operations journal names its targets with these keys rather than with
paths, so a journal row never carries a filesystem layout. Journals already on
disk were written with these exact digests: changing the encoding or the key
length would orphan every recorded operation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

#: Hex characters kept from each SHA-256 digest.
KEY_CHARS = 16


def digest_of(material: Sequence[object]) -> str:
    """Digest a list of components without inventing a delimiter.

    JSON encoding is the delimiter, so a component containing a comma, a quote,
    or a path separator cannot shift the boundary between components.
    """
    encoded = json.dumps(list(material), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:KEY_CHARS]


def plan_key(plan_dir: object) -> str:
    """The stable, opaque identity of one plan directory."""
    if not isinstance(plan_dir, str) or not plan_dir.strip():
        raise ValueError("a plan key needs a plan directory")
    return digest_of([plan_dir])


def run_key(plan: str, run_id: object) -> str:
    """The stable, opaque identity of one run of one plan.

    ``plan`` is a plan key rather than a plan directory. A plan with no run yet
    still gets a key: ``None`` is a distinct component from the string
    ``"None"``.
    """
    if not isinstance(plan, str) or not plan:
        raise ValueError("a run key needs a plan key")
    identifier = run_id if isinstance(run_id, str) and run_id else None
    return digest_of([plan, identifier])
