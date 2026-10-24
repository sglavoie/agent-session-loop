"""Overseer cli common."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    clock,
    intervention,
    mailbox,
    mailbox_snapshot,
    overseer_context,
    overseer_decisions,
    overseer_discovery,
    overseer_history,
    overseer_policy_client,
    overseer_store_ledger,
    overseer_watch,
    responses,
)
from .cli_defaults import program_name

#: Exit status for a leaf that answered and found nothing wrong.
EXIT_OK = 0


#: Exit status for a substantive negative answer: a refusal, a withheld packet,
#: a partial sweep, an unsettled action. The command worked; the answer is no.
EXIT_REFUSED = 1


#: Exit status for a malformed invocation or unreadable input. Distinct from
#: ``EXIT_REFUSED`` so a script can tell "the overseer said no" from "you asked
#: the overseer the wrong question".
EXIT_USAGE = 2


#: The operations an action leaf will build a payload for. Narrower than
#: nothing: it is exactly :data:`overseer_decisions.OPERATIONS`, restated here
#: only so the parser can offer it as ``choices``.
OPERATIONS: tuple[str, ...] = overseer_decisions.OPERATIONS


#: Sources ``inspect --expand`` will widen. The vocabulary belongs to
#: :mod:`overseer_context`; naming it here keeps argparse honest about it.
SOURCE_KINDS: tuple[str, ...] = overseer_context.SOURCE_KINDS


type OpenLedger = Callable[..., overseer_store_ledger.Ledger]


type BuildInventory = Callable[..., overseer_discovery.Inventory]


type Authorize = Callable[..., overseer_policy_client.Decision]


type InspectRun = Callable[..., intervention.RunFacts]


type ReadMailbox = Callable[..., mailbox_snapshot.MailboxView]


type ReadHistory = Callable[..., overseer_history.History]


type UtcNow = Callable[[], datetime]


type UtcIsoNow = Callable[[], str]


type Sleep = Callable[[float], None]


@dataclass(frozen=True)
class Seams:
    """Everything this module reaches the world through.

    A frozen record rather than module globals so a test can substitute a fake
    AgentMux, a fake clock and a temporary state root without patching. What is
    deliberately *absent* is as load-bearing as what is present:
    :func:`overseer_dispatch.dispatch` is not a seam. It is called by name, so a
    test can prove that the only way this module performs anything is through
    the guarded dispatcher, and no substitution can quietly make that untrue.
    """

    open_ledger: OpenLedger = overseer_store_ledger.open_ledger
    build_inventory: BuildInventory = overseer_discovery.build_inventory
    authorize: Authorize = overseer_policy_client.authorize
    inspect_run: InspectRun = intervention.inspect_run
    read_mailbox: ReadMailbox = mailbox.read_mailbox
    read_history: ReadHistory = overseer_history.read_history
    utc_now: UtcNow = clock.utc_now
    utc_iso_now: UtcIsoNow = clock.utc_iso_now
    sleep: Sleep = time.sleep


#: The seams every leaf uses unless a caller passes its own.
DEFAULT_SEAMS = Seams()


@dataclass(frozen=True)
class Escalation:
    """A question only a human can answer, and what happens meanwhile.

    There is no ``default`` field, no ``timeout`` field and no ``deadline``
    field, and that is the entire design. A timed default is how an escalation
    becomes a rubber stamp: the overseer asks, waits, hears nothing, and treats
    silence as consent. Silence is not consent, so there is nowhere to put one.

    ``blocked`` names the work that stays stopped until the question is
    answered. ``independent`` names the plans the same invocation may keep
    working on in the meantime, because a host that can interleave should not
    idle a fleet on one plan's open question -- and a host that cannot should
    still know what it is giving up by waiting.
    """

    reason: str
    question: str
    blocked: tuple[str, ...] = ()
    independent: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        """The escalation as a JSON object."""
        return {
            "reason": self.reason,
            "question": self.question,
            "blocked": list(self.blocked),
            "independent": list(self.independent),
        }


class OverseerCliError(Exception):
    """A malformed invocation. Carries the reason slug the envelope reports."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def canonical_plan(plan_dir: str | None) -> str:
    """The one spelling of a plan directory every leaf compares against.

    Discovery reports a plan directory as optional because a state record can
    be truncated or half-written, and a record with no plan directory is a
    real thing to have found. It canonicalises to the empty string, which
    matches no plan and so quietly falls out of every comparison here.
    """
    if not plan_dir:
        return ""
    return str(Path(plan_dir).expanduser().resolve())


def plan_dir_of(candidate: overseer_discovery.Candidate) -> str:
    """A candidate's plan directory as a plain string, empty when it has none."""
    return candidate.plan_dir or ""


def _selection(args: argparse.Namespace) -> overseer_watch.Selection:
    plans = tuple(getattr(args, "plans", None) or ())
    projects = tuple(getattr(args, "projects", None) or ())
    if not plans and not projects:
        return overseer_watch.WHOLE_FLEET
    return overseer_watch.Selection(plans=plans, projects=projects)


def _pause_window_seconds(args: argparse.Namespace) -> float:
    hours = getattr(args, "pause_window_hours", None)
    if hours is None:
        return float(overseer_discovery.DEFAULT_PAUSE_WINDOW_SECONDS)
    return float(hours) * 3600.0


def _state_dir(args: argparse.Namespace) -> str | None:
    value = getattr(args, "state_dir", None)
    return str(value) if value else None


def _emit(payload: dict[str, Any], lines: Sequence[str], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return
    for line in lines:
        print(line)


def _failure(command: str, error: OverseerCliError, *, as_json: bool) -> int:
    payload = responses.error_payload(command, reason=error.reason, error=error.message)
    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        # The JSON ``command`` stays the hyphenated identifier; the text prefix
        # names the leaf the way the user typed it (``asl overseer inspect:``).
        leaf = command.replace("-", " ", 1)
        print(f"{program_name()} {leaf}: {error.message}", file=sys.stderr)
    return EXIT_USAGE


def _scope_payload(scope: overseer_decisions.Scope) -> dict[str, Any]:
    # `project` is the single-project spelling and is empty for a plan spanning
    # several, so `projects` is the field a caller reads to see them all.
    return {
        "project": scope.project,
        "projects": list(scope.named_projects),
        "plan_dir": scope.plan_dir,
        "run_id": scope.run_id,
        "incident": scope.incident,
    }
