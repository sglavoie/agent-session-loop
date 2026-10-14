"""The overseer's durable ledger: what it saw, decided, attempted, and proved.

Discovery answers *which runs exist* and the policy client answers *may we act*.
Both are momentary. This is the boundary that remembers -- across a restart, a
crash between two writes, and a schema that has moved since the file was
created -- and it is a SQLite database rather than another JSONL file because
what it holds is relational and has to change atomically: an incident and the
evidence it rests on, an attempted dispatch and the receipt that later confirms
it, a policy change and every evidence row it invalidates.

The file is ``overseer-ledger.sqlite3``, a sibling of the per-plan state
directories under ASL's XDG state root. A *file* rather than a directory
deliberately: :func:`agent_session_loop.state.iter_plan_state_dirs` yields every
directory under that root as a plan, so a directory here would appear in the
fleet inventory as an unreadable run.

The implementation is split by concern and composed here: ``overseer_store_models``
(records and vocabularies), ``overseer_store_schema`` (migrations),
``overseer_store_rows`` (row mappers), and the ``overseer_store_core`` /
``_watch`` / ``_incidents`` / ``_actions`` / ``_groups`` mixins that
:class:`Ledger` inherits.

## Nothing stored here is permission

The ledger records authorization decisions. It never issues them. A stored
decision comes back as :class:`DecisionRecord`, which has no conversion to a
grant, no ``permits`` method, and no way to become one -- only
``overseer_policy_client.Decision.grant``, built from a live verified answer,
produces a :class:`~agent_session_loop.overseer_policy_client.Grant`. That
asymmetry is the whole design: "AgentMux was unavailable, but it said yes an
hour ago" has to be unrepresentable rather than merely discouraged, so the
substitution the shared requirements forbid is a thing this module cannot
express.

## Policy changes gate stored evidence

Evidence rows carry references to project content that was read under one
configuration. When :meth:`Ledger.note_policy` sees a configuration fingerprint
that differs from the one in force, it bumps the epoch and **seals** every
evidence row gathered under the old one, in the same transaction. A sealed row
is withheld from :meth:`Ledger.read_evidence` no matter who asks; it is
released only by :meth:`Ledger.release_evidence`, which requires a fresh
``context`` grant for that plan issued under the fingerprint now in force. So a
project the operator has just excluded never produces a grant, never gets its
evidence released, and never yields a row again -- while a policy edit that
left the project enrolled costs one re-authorization rather than the evidence
itself.

Incidents, decisions, actions, and receipts are ASL's own operational record
and are not gated: they say what the overseer did, not what the project
contains. The gate is on the project's content, which is what the exclusion is
about.

## Attempted is not executed

An action is written ``attempted`` before it is dispatched and reaches
``executed`` only through :meth:`Ledger.confirm_action`, which writes a receipt
in the same transaction as the status change. There is no path from
``attempted`` to ``executed`` that does not produce a receipt, and a crash
between the dispatch and the confirmation leaves the action ``attempted``
forever -- reported by :meth:`Ledger.in_doubt_actions` and never resolved by
this module in either direction. That matches the shared rule that
already-dispatched actions are not automatically undone: an overseer that
guessed "probably landed" or "probably did not" would be inventing the one fact
it does not have.

## Two watchers, one row

Ownership between concurrent watcher instances is a lease per plan, taken and
renewed through :meth:`Ledger.claim_watch` inside the same ``BEGIN IMMEDIATE``
that reads it. A lease changes hands unwillingly only by running out of time,
never by anyone deciding its holder has died: a watcher is a process that may be
on another host, and a probe that fails says the same thing about a busy watcher
as about a gone one.

Durable records this boundary writes: **one SQLite database**, always through
:meth:`Ledger.transaction`, so every multi-row write is all-or-nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from . import clock, state
from .overseer_store_actions import ActionMixin
from .overseer_store_groups import CheckpointGroupMixin
from .overseer_store_incidents import IncidentMixin
from .overseer_store_models import LEDGER_FILE_NAME, LedgerError
from .overseer_store_schema import apply_pragmas, migrate
from .overseer_store_watch import WatchMixin


class Ledger(
    WatchMixin,
    IncidentMixin,
    ActionMixin,
    CheckpointGroupMixin,
):
    """The overseer's durable memory, open on one SQLite database.

    Constructed by :func:`open_ledger`. Every write goes through
    :meth:`transaction`, so a caller cannot leave half of a multi-row change
    behind by returning early or raising in the middle of one.
    """


def ledger_path(state_dir: str | Path | None = None) -> Path:
    """Where the overseer ledger lives under ``state_dir`` or the XDG default."""
    return state.state_root(state_dir) / LEDGER_FILE_NAME


def open_ledger(
    *,
    state_dir: str | Path | None = None,
    now: Callable[[], str] = clock.utc_iso_now,
) -> Ledger:
    """Open the ledger, creating and migrating it as needed.

    Every open runs the pending migrations, so an ASL that starts against a
    database an older build created reaches the current schema before it reads a
    row. A database written by a *newer* build is refused instead: replaying
    nothing onto an unknown schema and hoping the columns line up is how an
    incident ledger starts recording into the wrong shape.
    """
    path = ledger_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(str(path), isolation_level=None)
    except sqlite3.Error as error:
        raise LedgerError(f"could not open overseer ledger {path}: {error}") from error
    connection.row_factory = sqlite3.Row
    try:
        apply_pragmas(connection)
        migrate(connection, path)
    except BaseException:
        connection.close()
        raise
    return Ledger(connection=connection, path=path, now=now)
