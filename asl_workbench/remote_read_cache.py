# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F401, F403, F405, F821, F822, I001
"""In-memory bounded body cache for remote detail reads."""

from __future__ import annotations

from . import remote_read_models as _models

globals().update(
    {name: value for name, value in vars(_models).items() if not name.startswith("__")}
)
del _models


# --------------------------------------------------------------------------
# The bodies this process is holding
# --------------------------------------------------------------------------


class BodyCache:
    """Answered bodies, in memory, bounded by count and by age.

    Nothing here reaches a store. A detail body is not a record of what
    happened, it is material somebody is reading right now, and material that
    outlives the moment is material sitting in a backup nobody audited.
    """

    def __init__(
        self,
        *,
        capacity: int = MAX_CACHED_BODIES,
        ttl_seconds: float = BODY_TTL_SECONDS,
    ) -> None:
        if capacity < 1:
            raise RemoteReadError("a body cache with no room holds nothing")
        self._capacity = int(capacity)
        self._ttl = float(ttl_seconds)
        self._lock = threading.RLock()
        # Insertion ordered, and relied on: the oldest arrival is the first
        # key. Eviction is by arrival rather than by least-recent-read, because
        # tracking reads would mean recording who looked at which plan.
        self._bodies: dict[ReadKey, dict[str, Any]] = {}

    # -- writing ---------------------------------------------------------
    def store(self, key: ReadKey, answer: ReadAnswer, *, now: float) -> None:
        """Hold one answered read's material.

        The body is held *beside* the answer rather than inside it, so
        :meth:`_forget` can blank the text this cache is carrying at exactly
        the moment it stops carrying the entry -- which a frozen answer handed
        out earlier would otherwise keep alive.
        """
        if not answer.answered:
            raise RemoteReadError("only an answered read has a body to hold")
        entry: dict[str, Any] = {
            "answer": replace(answer, content=None, entries=()),
            "content": answer.content,
            "entries": list(answer.entries),
            "received_at": float(now),
            # Measured now, so :meth:`held` can say how much is being carried
            # without reaching for the text to find out.
            "body_bytes": answer.body_bytes,
            "entry_count": len(answer.entries),
        }
        with self._lock:
            self._expire(now)
            # Removed before reinsertion so a refetched window counts as a new
            # arrival: a plan somebody is re-reading is a plan somebody is
            # working on.
            self._bodies.pop(key, None)
            self._bodies[key] = entry
            while len(self._bodies) > self._capacity:
                self._forget(next(iter(self._bodies)))

    def drop(self, plan_key: str) -> int:
        """Forget every body held for one plan. Returns how many."""
        with self._lock:
            doomed = [key for key in self._bodies if key[0] == plan_key]
            for key in doomed:
                self._forget(key)
            return len(doomed)

    def expire(self, now: float) -> int:
        """Drop everything the clock alone has decided. Returns how many."""
        with self._lock:
            return self._expire(now)

    def clear(self) -> None:
        """Forget everything, blanking each body on the way out."""
        with self._lock:
            for key in list(self._bodies):
                self._forget(key)

    # -- reading ---------------------------------------------------------
    def get(self, key: ReadKey, *, now: float) -> ReadAnswer | None:
        """The answer held for one window, body and all, or ``None``."""
        with self._lock:
            self._expire(now)
            entry = self._bodies.get(key)
            if entry is None:
                return None
            held: ReadAnswer = entry["answer"]
            return replace(
                held,
                content=entry["content"],
                entries=tuple(entry["entries"]),
            )

    def held(self, *, now: float) -> tuple[dict[str, Any], ...]:
        """Every body still held, oldest arrival first, without the text."""
        with self._lock:
            self._expire(now)
            return tuple(
                {
                    **entry["answer"].identity(),
                    "received_at": entry["received_at"],
                    "body_bytes": entry["body_bytes"],
                    "entry_count": entry["entry_count"],
                }
                for entry in self._bodies.values()
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._bodies)

    # -- internals -------------------------------------------------------
    def _expire(self, now: float) -> int:
        stale = [
            key
            for key, entry in self._bodies.items()
            if now - float(entry["received_at"]) >= self._ttl
        ]
        for key in stale:
            self._forget(key)
        return len(stale)

    def _forget(self, key: ReadKey) -> None:
        entry = self._bodies.pop(key, None)
        if entry is not None:
            entry["content"] = None
            entry["entries"] = []
