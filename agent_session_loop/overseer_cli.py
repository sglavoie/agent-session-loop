"""Operator surface for the overseer helper: scan, watch, inspect, decide, act.

These six leaves are the whole seam between a host agent and the overseer
machinery. Each one is deterministic: it reads state, applies the rules the
guarded modules already encode, and prints what it found. None of them decides
anything. Judgement enters in exactly one place -- the claims a host writes for
``asl overseer decide`` -- and it enters as data the register can reject, not as
a branch in this module.

The split matters because the host is a language model. A model that can be
argued into an action is a model that must never be the thing holding the
guard, so the guards live in :mod:`overseer_dispatch` and the adapters behind
it, and this module's only route to a mutation is
:func:`overseer_dispatch.dispatch`. There is no second path: no adapter's
``execute`` or ``prepare`` is called here, and the adapters are built only to be
handed to the dispatcher.

The host loop these leaves compose is:

1. **observe** -- ``scan`` lists what discovery can see, ``watch`` enrols what
   AgentMux authorizes and holds a lease on it.
2. **authorize** -- every leaf that reads content or prepares a mutation asks
   AgentMux again, immediately before it acts. A grant is never cached across
   a leaf boundary; the CLI's process lifetime is the cache lifetime.
3. **collect context** -- ``inspect`` opens an incident and captures a bounded
   packet, and ``--expand`` widens one source at a time under a byte and record
   ceiling rather than handing over a run's whole state.
4. **reason** -- ``decide`` turns recorded human statements into a ruling. What
   a human already settled is reused; what no human has settled is refused.
5. **dispatch** -- ``act`` builds an :class:`overseer_dispatch.Action` from the
   token and scope ``inspect`` captured earlier and hands it to the guarded
   dispatcher. Capturing them earlier is the point: a token recomputed at
   dispatch time would agree with itself and ``stale_action`` would never fire.
6. **reconcile** -- ``reconcile`` settles rows a crashed dispatch left in doubt,
   which is the only way an in-doubt row ever becomes an answer.
7. **report** -- every leaf prints a JSON envelope under ``--json`` and exits
   non-zero on any refusal, so a shell ``&&`` chain stops on the first one.

Escalation is structural. :class:`Escalation` has no default, no timeout and no
deadline, so a caller cannot express "proceed if nobody answers in N seconds".
The only way past a genuine question is a human answering it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from . import (
    clock,
    intervention,
    mailbox,
    overseer_checkpoint_groups,
    overseer_context,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_history,
    overseer_mailbox,
    overseer_policy_client,
    overseer_resume,
    overseer_store,
    overseer_watch,
    responses,
)

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

type OpenLedger = Callable[..., overseer_store.Ledger]
type BuildInventory = Callable[..., overseer_discovery.Inventory]
type Authorize = Callable[..., overseer_policy_client.Decision]
type InspectRun = Callable[..., intervention.RunFacts]
type ReadMailbox = Callable[..., mailbox.MailboxView]
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

    open_ledger: OpenLedger = overseer_store.open_ledger
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
        print(f"{command}: {error.message}", file=sys.stderr)
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


def _candidate_payload(
    candidate: overseer_discovery.Candidate, *, window_seconds: float
) -> dict[str, Any]:
    return {
        "plan_dir": candidate.plan_dir,
        "run_id": candidate.run_id,
        "workspace_root": candidate.workspace_root,
        "registry_digest": candidate.registry_digest,
        "lifecycle": candidate.lifecycle,
        "paused": candidate.paused,
        "pause_reason": candidate.pause_reason,
        "eligible": candidate.eligible,
        "in_window": candidate.paused_within(window_seconds),
        "observed": candidate.record_observed,
        "owned": not candidate.ownership.unowned(),
        "scope": overseer_context.scope_digest(candidate),
        "repositories": [
            {"alias": declaration.alias, "root": declaration.resolved_root}
            for declaration in candidate.repositories
        ],
        "ineligible_reasons": list(candidate.ineligible_reasons),
    }


def _sweep_payload(sweep: overseer_watch.Sweep) -> dict[str, Any]:
    return {
        "started_at": clock.isoformat(sweep.started_at),
        "finished_at": clock.isoformat(sweep.finished_at),
        "considered": sweep.considered,
        "serviced": sweep.serviced,
        "complete": sweep.complete,
        "inventory_complete": sweep.inventory_complete,
        "warnings": list(sweep.warnings),
        "enrolled": [
            {
                "plan_dir": item.plan_dir,
                "run_id": item.run_id,
                "lifecycle": item.lifecycle,
                "named": item.named,
                "took_over": item.took_over,
                "recoverable": item.recoverable,
                "lease_expires_at": item.lease_expires_at,
            }
            for item in sweep.enrolled
        ],
        "excluded": [
            {
                "plan_dir": item.plan_dir,
                "run_id": item.run_id,
                "reason": item.reason,
                "detail": item.detail,
            }
            for item in sweep.excluded
        ],
    }


def _packet_payload(packet: overseer_context.Packet) -> dict[str, Any]:
    return {
        "plan_dir": packet.plan_dir,
        "run_id": packet.run_id,
        "incident_id": packet.incident_id,
        "kind": packet.kind,
        "summary": packet.summary,
        "questions": list(packet.questions),
        "scope": packet.scope,
        "captured_at": packet.captured_at,
        "withheld": packet.withheld,
        "refusal": packet.refusal,
        "sources": [
            {
                "kind": source.kind,
                "space": source.space,
                "locator": source.locator,
                "summary": source.summary,
                "size": source.size,
                "truncated": source.truncated,
            }
            for source in packet.sources
        ],
        "gaps": [
            {"kind": gap.kind, "reason": gap.reason, "detail": gap.detail}
            for gap in packet.gaps
        ],
    }


def _excerpt_payload(excerpt: overseer_context.Excerpt) -> dict[str, Any]:
    return {
        "kind": excerpt.kind,
        "space": excerpt.space,
        "locator": excerpt.locator,
        "size": excerpt.size,
        "truncated": excerpt.truncated,
        "refusal": excerpt.refusal,
        "text": excerpt.text,
    }


def _statement_payload(
    statement: overseer_decisions.Statement,
) -> dict[str, Any]:
    return {
        "statement_id": statement.statement_id,
        "voice": statement.voice,
        "space": statement.space,
        "locator": statement.locator,
        "at": statement.at,
        "marker": statement.marker,
        "truncated": statement.truncated,
        "text": statement.text,
        "scope": _scope_payload(statement.scope),
    }


def _history_payload(
    history: overseer_history.History | None,
) -> dict[str, Any] | None:
    """What the transcript boundary answered, including that it did not."""
    if history is None:
        return None
    return {
        "allowed": history.allowed,
        "available": history.available,
        "reason": history.reason,
        "failure": history.failure,
        "summary": history.summary,
        "examined": history.examined,
        "truncated": history.truncated,
        "conversations": len(history.conversations),
        "withheld": len(history.withheld),
        "warnings": list(history.warnings),
    }


def _decision_payload(
    decision: overseer_decisions.Decision,
) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "statement_id": decision.statement_id,
        "topic": decision.topic,
        "stance": decision.stance,
        "operations": list(decision.operations),
        "at": decision.at,
        "reading": decision.reading,
        "scope": _scope_payload(decision.scope),
    }


def _ruling_payload(ruling: overseer_decisions.Ruling) -> dict[str, Any]:
    return {
        "operation": ruling.operation,
        "authorized": ruling.authorized,
        "refusal": ruling.refusal,
        "summary": ruling.summary,
        "scope": _scope_payload(ruling.scope),
        "decision": (
            None if ruling.decision is None else _decision_payload(ruling.decision)
        ),
        "superseded": [_decision_payload(item) for item in ruling.superseded],
        "precedents": [_decision_payload(item) for item in ruling.precedents],
        "settlements": [
            {
                "topic": settlement.topic,
                "decision": (
                    None
                    if settlement.decision is None
                    else _decision_payload(settlement.decision)
                ),
                "superseded": [
                    _decision_payload(item) for item in settlement.superseded
                ],
            }
            for settlement in ruling.settlements
        ],
    }


def _record_payload(
    record: overseer_store.ActionRecord | None,
) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "action_id": record.action_id,
        "plan_dir": record.plan_dir,
        "incident_id": record.incident_id,
        "operation": record.operation,
        "target": record.target,
        "status": record.status,
        "attempted_at": record.attempted_at,
        "settled_at": record.settled_at,
    }


def _receipt_payload(
    receipt: overseer_dispatch.Receipt | None,
) -> dict[str, Any] | None:
    if receipt is None:
        return None
    return {
        "outcome": receipt.outcome,
        "detail": receipt.detail,
        "locator": receipt.locator,
    }


def _dispatch_payload(dispatch: overseer_dispatch.Dispatch) -> dict[str, Any]:
    return {
        "operation": dispatch.action.payload.operation,
        "plan_dir": dispatch.action.plan_dir,
        "run_id": dispatch.action.run_id,
        "incident_id": dispatch.action.incident_id,
        "performed": dispatch.performed,
        "refusal": dispatch.refusal,
        "detail": dispatch.detail,
        "escalated": dispatch.escalated,
        "recoveries": dispatch.recoveries,
        "recovery_limit": overseer_dispatch.RECOVERY_LIMIT,
        "record": _record_payload(dispatch.record),
        "receipt": _receipt_payload(dispatch.receipt),
    }


def _resolve_candidate(
    inventory: overseer_discovery.Inventory, plan_dir: str
) -> overseer_discovery.Candidate:
    wanted = canonical_plan(plan_dir)
    for candidate in inventory.candidates:
        if canonical_plan(candidate.plan_dir) == wanted:
            return candidate
    raise OverseerCliError(
        "plan_unobserved",
        f"discovery has no record of {wanted}; run `asl overseer scan` first",
    )


def _independent_plans(
    inventory: overseer_discovery.Inventory, *, excluding: str
) -> tuple[str, ...]:
    """Eligible plans other than this one: work a blocked host may still do."""
    blocked = canonical_plan(excluding)
    return tuple(
        plan_dir_of(candidate)
        for candidate in inventory.eligible()
        if canonical_plan(candidate.plan_dir) != blocked
    )


def _grant_for(
    candidate: overseer_discovery.Candidate,
    *,
    access: str,
    seams: Seams,
    timeout: float,
) -> tuple[overseer_policy_client.Decision, overseer_policy_client.Grant | None]:
    """Ask AgentMux now. Never reuse an answer from an earlier leaf.

    The plan directory goes on the wire in :func:`plan_dir_of`'s spelling, which
    is the one every other comparison in this module uses. A grant naming a
    different spelling than the caller passes on -- ``None`` against ``""``, for
    the truncated record discovery still reports -- is a grant the dispatcher
    would refuse to match, and the refusal would read as a policy problem.
    """
    decision = seams.authorize(
        plan_dir=plan_dir_of(candidate),
        access=access,
        repositories=overseer_policy_client.repositories_for(candidate.repositories),
        timeout=timeout,
    )
    return decision, decision.grant()


def project_identities(
    candidate: overseer_discovery.Candidate,
) -> tuple[str, ...]:
    """Every project a plan participates in, in the registry's own spelling.

    A project is a repository root, because that is the identity AgentMux
    authorizes a conversation under. The workspace directory those repositories
    sit under is not one of them: no conversation is ever admitted under it, so
    a request naming it could be matched by no evidence at all and a repository
    approval would be rejected ``scope_widened`` for naming somewhere else.

    The roots are passed through exactly as discovery resolved them, since the
    packet digests, the policy question and the dispatcher all compare that same
    spelling; a second normalization here would be a second answer to what one
    repository is.
    """
    return tuple(
        dict.fromkeys(
            declaration.resolved_root
            for declaration in candidate.repositories
            if declaration.resolved_root
        )
    )


def _scope_for(
    candidate: overseer_discovery.Candidate, *, incident_id: int | None
) -> overseer_decisions.Scope:
    """The scope one request is about: this plan, in its own repositories.

    A plan that declares no repository names no project, which refuses
    ``target_unscoped`` rather than falling back to the workspace -- an
    unmatched identity would read as a permission problem, and an unscoped
    request reads as what it is.
    """
    return overseer_decisions.Scope(
        projects=project_identities(candidate),
        plan_dir=plan_dir_of(candidate),
        run_id=candidate.run_id or "",
        incident="" if incident_id is None else str(incident_id),
    )


def _claim_projects(
    named: dict[str, Any], position: int, *, scope: overseer_decisions.Scope
) -> tuple[str, ...]:
    """The projects one claim's scope names, defaulting to the request's.

    A claim scope that mentions neither ``project`` nor ``projects`` inherits
    every project the request is about, which is the narrowest reading for a
    plan in one repository and the only honest one for a plan in several. A
    claim that names either of them is taken at its word and checked against
    the evidence, which is where naming a project nobody spoke in is refused.
    """
    if "projects" not in named:
        return () if "project" in named else scope.named_projects
    listed = named["projects"]
    if not isinstance(listed, list):
        raise OverseerCliError(
            "claims_invalid", f"claim {position} has a non-list scope.projects"
        )
    return tuple(str(item) for item in listed)


def parse_claims(
    text: str, *, scope: overseer_decisions.Scope
) -> tuple[overseer_decisions.Claim, ...]:
    """Read host-composed claims, defaulting each one's scope to the request's.

    The host writes claims because reading what a human meant is judgement, and
    judgement is the host's job. Everything after this point is mechanical: the
    register decides which claims survive, and it can reject every one of them.

    An omitted ``scope`` means "exactly the scope being asked about", the
    narrowest reading available. A claim may still name a wider one and be
    rejected for it -- that rejection is reported, not silently dropped.

    Topics, stances and operations are passed through unchecked rather than
    validated here. The register owns those vocabularies and already reports
    an unrecognised one as an ``unknown_topic`` or ``unknown_operation``
    rejection against the claim that carried it. Rejecting them at the parser
    instead would turn a reportable finding into a usage error and lose which
    claim was at fault.
    """
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as error:
        raise OverseerCliError(
            "claims_invalid", f"claims are not JSON: {error}"
        ) from error
    if not isinstance(loaded, list):
        raise OverseerCliError(
            "claims_invalid", "claims must be a JSON array of claim objects"
        )
    claims: list[overseer_decisions.Claim] = []
    for position, entry in enumerate(loaded):
        if not isinstance(entry, dict):
            raise OverseerCliError(
                "claims_invalid", f"claim {position} is not an object"
            )
        required = ("statement_id", "topic", "stance")
        missing = [key for key in required if key not in entry]
        if missing:
            raise OverseerCliError(
                "claims_invalid",
                f"claim {position} omits {', '.join(missing)}",
            )
        named = entry.get("scope")
        if named is None:
            claim_scope = scope
        elif isinstance(named, dict):
            claim_scope = overseer_decisions.Scope(
                project=str(named.get("project", "")),
                projects=_claim_projects(named, position, scope=scope),
                plan_dir=str(named.get("plan_dir", scope.plan_dir)),
                run_id=str(named.get("run_id", scope.run_id)),
                incident=str(named.get("incident", scope.incident)),
            )
        else:
            raise OverseerCliError(
                "claims_invalid", f"claim {position} has a non-object scope"
            )
        claims.append(
            overseer_decisions.Claim(
                statement_id=str(entry["statement_id"]),
                topic=cast(overseer_decisions.Topic, str(entry["topic"])),
                stance=cast(overseer_decisions.Stance, str(entry["stance"])),
                scope=claim_scope,
                operations=cast(
                    tuple[overseer_decisions.Operation, ...],
                    tuple(str(item) for item in entry.get("operations", ())),
                ),
                corroborating=tuple(
                    str(item) for item in entry.get("corroborating", ())
                ),
                reading=str(entry.get("reading", "")),
            )
        )
    return tuple(claims)


def _claims_text(args: argparse.Namespace) -> str:
    path = getattr(args, "claims_file", None)
    if getattr(args, "claims_stdin", False):
        if path:
            raise OverseerCliError(
                "claims_invalid", "--claims-file and --claims-stdin are exclusive"
            )
        return sys.stdin.read()
    if not path:
        return "[]"
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise OverseerCliError(
            "claims_invalid", f"cannot read {path}: {error}"
        ) from error


@dataclass(frozen=True)
class Evidence:
    """Everything a ruling was derived from, and how much of it was readable."""

    scope: overseer_decisions.Scope
    statements: tuple[overseer_decisions.Statement, ...]
    history: overseer_history.History | None


def gather(
    *,
    candidate: overseer_discovery.Candidate,
    grant: overseer_policy_client.Grant | None,
    incident_id: int | None,
    seams: Seams,
    state_dir: str | None,
    history: bool,
    generated_classes: Sequence[str],
    timeout: float,
) -> Evidence:
    """Collect the statements a claim may cite, from both evidence boundaries.

    The mailbox is one boundary and the harness transcript is the other, and
    they are not interchangeable. A mailbox record shows that an instruction
    *arrived*, never that a person composed it, so everything it yields is a
    relayed instruction -- readable, citable as corroboration, and never on its
    own an authorization. Only a transcript carries a turn a human actually
    typed. A caller that skips the transcript is therefore choosing a state
    where nothing can be authorized, which is a legitimate thing to want and a
    silent surprise if it happened by default.

    A transcript that cannot be read is not an error here. It comes back as a
    history carrying its own failure, contributes no statements, and is
    reported, so "no authorization was found" and "the evidence could not be
    reached" stay distinguishable.
    """
    scope = _scope_for(candidate, incident_id=incident_id)
    view = seams.read_mailbox(candidate.plan_dir, state_dir=state_dir)
    statements = list(
        overseer_decisions.statements_from_supervision(
            view.messages, grant=grant, scope=scope
        )
    )
    transcript: overseer_history.History | None = None
    if history:
        transcript = seams.read_history(
            plan_dir=plan_dir_of(candidate),
            repositories=overseer_policy_client.repositories_for(
                candidate.repositories
            ),
            timeout=timeout,
        )
        statements.extend(
            overseer_decisions.statements_from_history(
                transcript,
                generated_classes=tuple(generated_classes),
                repositories=project_identities(candidate),
            )
        )
    return Evidence(scope=scope, statements=tuple(statements), history=transcript)


def _rule(
    *,
    evidence: Evidence,
    claims: Sequence[overseer_decisions.Claim],
    operation: str,
    reviewed: str = "",
) -> tuple[overseer_decisions.Ruling, overseer_decisions.Register]:
    """Recompute a ruling from live statements. Never from a cached one.

    ``reviewed`` is the evidence digest the claims were read from, when the
    caller is binding them to one. Re-deriving the ruling proves the claims are
    still in force; the digest is what makes the re-derivation notice a
    statement nobody claimed because nobody had seen it yet.
    """
    register = overseer_decisions.register(
        statements=evidence.statements, claims=claims
    )
    ruling = overseer_decisions.resolve(
        register=register,
        request=overseer_decisions.Request(
            operation=operation, scope=evidence.scope, reviewed=reviewed
        ),
    )
    return ruling, register


def _adapters(
    *,
    authority: Callable[[overseer_dispatch.Action], overseer_decisions.Ruling | None],
    ledger: overseer_store.Ledger,
    state_dir: str | None,
    seams: Seams,
    timeout: float,
) -> overseer_dispatch.Registry:
    """The registry the dispatcher is handed. Built here, called only there.

    ``commit`` comes from :mod:`overseer_checkpoint_groups` rather than
    :mod:`overseer_checkpoint` because a run may declare more than one writable
    repository, and a crash between the first repository and the second is the
    case the durable group exists for. The group form handles a single
    repository as the degenerate case, so there is no reason to register the
    ungrouped one and every reason not to.

    ``verify`` is absent on purpose. No adapter implements it, so
    :func:`overseer_dispatch.dispatch` refuses it ``operation_unsupported``
    before it touches the ledger. That refusal is the escalation path working:
    an operation outside the helper's reach becomes a question for a human, not
    an improvised command.
    """
    root = None if state_dir is None else Path(state_dir)
    sources = (
        overseer_mailbox.adapters(authority=authority, state_dir=root),
        overseer_checkpoint_groups.adapters(
            authority=authority,
            ledger=ledger,
            state_dir=root,
            authorize=seams.authorize,
            timeout=timeout,
        ),
        overseer_resume.adapters(
            authority=authority,
            state_dir=root,
            build_inventory=seams.build_inventory,
            inspect_run=seams.inspect_run,
        ),
    )
    merged: dict[str, overseer_dispatch.Adapter] = {}
    for source in sources:
        merged.update({str(name): adapter for name, adapter in source.adapters.items()})
    return overseer_dispatch.registry(merged)


def _payload_for(args: argparse.Namespace, operation: str) -> overseer_dispatch.Payload:
    if operation == "reply":
        if not args.question_id or not args.text:
            raise OverseerCliError(
                "payload_incomplete", "reply needs --question-id and --text"
            )
        return overseer_dispatch.Reply(question_id=args.question_id, text=args.text)
    if operation == "guidance":
        if not args.text:
            raise OverseerCliError("payload_incomplete", "guidance needs --text")
        return overseer_dispatch.Guidance(text=args.text)
    if operation == "verify":
        if not args.check:
            raise OverseerCliError("payload_incomplete", "verify needs --check")
        return overseer_dispatch.Verify(check=args.check)
    if operation == "commit":
        if not args.message:
            raise OverseerCliError("payload_incomplete", "commit needs --message")
        return overseer_dispatch.Commit(message=args.message)
    return overseer_dispatch.Resume(session_id=args.session_id or "")


def scan_from_args(args: argparse.Namespace, *, seams: Seams = DEFAULT_SEAMS) -> int:
    """List every run discovery can see, with why each one is or is not in play.

    Discovery only. Nothing here reads a run's content, so nothing here asks
    AgentMux: the eligibility a candidate reports is ASL's own, and a plan that
    looks eligible may still be refused the moment ``watch`` asks about it.
    """
    as_json = bool(getattr(args, "json", False))
    try:
        state_dir = _state_dir(args)
        window = _pause_window_seconds(args)
        selection = _selection(args)
        inventory = seams.build_inventory(state_dir=state_dir)
    except OverseerCliError as error:
        return _failure("overseer-scan", error, as_json=as_json)
    matching = [
        candidate for candidate in inventory.candidates if selection.selects(candidate)
    ]
    payload = responses.success_payload(
        "overseer-scan",
        state_root=str(inventory.state_root),
        complete=inventory.complete,
        warnings=list(inventory.warnings),
        considered=len(matching),
        eligible=sum(1 for candidate in matching if candidate.eligible),
        candidates=[
            _candidate_payload(candidate, window_seconds=window)
            for candidate in matching
        ],
    )
    lines = [
        f"state root: {inventory.state_root}",
        f"candidates: {len(matching)} "
        f"({sum(1 for c in matching if c.eligible)} eligible)"
        f"{'' if inventory.complete else ' [partial view]'}",
    ]
    for candidate in matching:
        mark = "+" if candidate.eligible else "-"
        detail = candidate.lifecycle
        if candidate.ineligible_reasons:
            detail = f"{detail}: {', '.join(candidate.ineligible_reasons)}"
        lines.append(f"  {mark} {candidate.plan_dir} [{detail}]")
    for warning in inventory.warnings:
        lines.append(f"  ! {warning}")
    _emit(payload, lines, as_json=as_json)
    return EXIT_OK if inventory.complete else EXIT_REFUSED


def watch_from_args(args: argparse.Namespace, *, seams: Seams = DEFAULT_SEAMS) -> int:
    """Enrol the runs AgentMux authorizes and hold a lease on each of them.

    One sweep by default. ``--for-seconds`` keeps sweeping until the deadline,
    which is what "watch while invoked" means: newly started runs and eligible
    pauses join on a later sweep without the operator re-invoking anything.
    ``--release`` gives the leases back, which is what ending an invocation
    means -- an abandoned lease expires on its own, but only after it has kept
    another watcher out for the rest of its term.
    """
    as_json = bool(getattr(args, "json", False))
    state_dir = _state_dir(args)
    ledger = seams.open_ledger(state_dir=state_dir)
    watcher = overseer_watch.Watcher.here(args.watcher_id)
    if getattr(args, "release", False):
        released = overseer_watch.release(watcher=watcher, ledger=ledger)
        payload = responses.success_payload(
            "overseer-watch",
            watcher_id=watcher.watcher_id,
            released=released,
            sweeps=[],
        )
        _emit(
            payload,
            [f"released {released} lease(s) held by {watcher.watcher_id}"],
            as_json=as_json,
        )
        return EXIT_OK
    common: dict[str, Any] = {
        "watcher": watcher,
        "ledger": ledger,
        "selection": _selection(args),
        "state_dir": state_dir,
        "now": seams.utc_now,
        "pause_window_seconds": _pause_window_seconds(args),
        "lease_seconds": float(args.lease_seconds),
        "authorize": seams.authorize,
        "authorize_timeout": float(args.timeout),
        "build_inventory": seams.build_inventory,
    }
    if args.for_seconds:
        until = seams.utc_now() + timedelta(seconds=float(args.for_seconds))
        invocation = overseer_watch.watch(
            until=until,
            sleep=seams.sleep,
            sweep_seconds=float(args.sweep_seconds),
            interval_seconds=float(args.interval_seconds),
            **common,
        )
        sweeps = list(invocation.sweeps)
    else:
        sweeps = [overseer_watch.sweep(**common)]
    complete = all(item.complete for item in sweeps) and bool(sweeps)
    payload = responses.success_payload(
        "overseer-watch",
        watcher_id=watcher.watcher_id,
        released=0,
        complete=complete,
        sweeps=[_sweep_payload(item) for item in sweeps],
    )
    lines = [f"watcher {watcher.watcher_id}: {len(sweeps)} sweep(s)"]
    for item in sweeps:
        lines.append(
            f"  enrolled {len(item.enrolled)}, excluded {len(item.excluded)}, "
            f"considered {item.considered}"
            f"{'' if item.complete else ' [partial]'}"
        )
        for enrollment in item.enrolled:
            lines.append(f"    + {enrollment.plan_dir} ({enrollment.lifecycle})")
        for excluded in item.excluded:
            lines.append(f"    - {excluded.plan_dir} [{excluded.reason}]")
    _emit(payload, lines, as_json=as_json)
    return EXIT_OK if complete else EXIT_REFUSED


def inspect_from_args(args: argparse.Namespace, *, seams: Seams = DEFAULT_SEAMS) -> int:
    """Open an incident and capture a bounded packet of a run's context.

    The packet is a summary, not a transcript. ``--expand`` widens exactly one
    source at a time under a byte and record ceiling, so context grows because
    the host asked for a named thing and not because a run happened to be
    verbose. Every expansion re-checks the grant, so a revocation between the
    packet and the expansion stops the expansion.

    The ``scope`` and ``token`` this prints are what ``asl overseer act`` must
    be given later. They are captured here, at the moment the context was read,
    precisely so a run that moves between reading and acting produces a
    ``stale_action`` refusal instead of an action aimed at a state that is gone.
    """
    as_json = bool(getattr(args, "json", False))
    state_dir = _state_dir(args)
    try:
        ledger = seams.open_ledger(state_dir=state_dir)
        inventory = seams.build_inventory(state_dir=state_dir)
        candidate = _resolve_candidate(inventory, args.plan_dir)
        decision, grant = _grant_for(
            candidate,
            access=overseer_policy_client.ACCESS_CONTEXT,
            seams=seams,
            timeout=float(args.timeout),
        )
        if args.incident_id is not None:
            packet = overseer_context.load_packet(
                grant=grant,
                ledger=ledger,
                incident_id=int(args.incident_id),
                state_dir=state_dir,
                now=seams.utc_iso_now,
                read_mailbox=seams.read_mailbox,
                build_inventory=seams.build_inventory,
            )
        else:
            packet = overseer_context.build_packet(
                grant=grant,
                ledger=ledger,
                candidate=candidate,
                kind=args.kind,
                summary=args.summary,
                state_dir=state_dir,
                now=seams.utc_iso_now,
                read_mailbox=seams.read_mailbox,
            )
        excerpts = [
            overseer_context.expand(
                grant=grant,
                ledger=ledger,
                incident_id=packet.incident_id,
                kind=kind,
                state_dir=state_dir,
                read_mailbox=seams.read_mailbox,
                limit=int(args.limit),
            )
            for kind in (args.expand or ())
        ]
    except OverseerCliError as error:
        return _failure("overseer-inspect", error, as_json=as_json)
    revalidation = overseer_context.revalidate(packet=packet, candidate=candidate)
    facts = seams.inspect_run(candidate.plan_dir, state_dir=state_dir)
    fresh = revalidation.fresh and packet.refusal is None
    payload = responses.success_payload(
        "overseer-inspect",
        authorized=decision.allowed,
        authorization=decision.summary,
        packet=_packet_payload(packet),
        excerpts=[_excerpt_payload(item) for item in excerpts],
        fresh=fresh,
        revalidation={
            "fresh": revalidation.fresh,
            "reason": revalidation.reason,
            "detail": revalidation.detail,
        },
        token=intervention.action_token(facts),
        scope=overseer_context.scope_digest(candidate),
        run_id=candidate.run_id,
        expansion_limits={
            "chars": overseer_context.MAX_EXCERPT_CHARS,
            "records": overseer_context.MAX_EXCERPT_RECORDS,
        },
    )
    lines = [
        f"incident {packet.incident_id} on {packet.plan_dir} ({packet.kind})",
        f"  summary: {packet.summary}",
        f"  sources: {len(packet.sources)}, gaps: {len(packet.gaps)}, "
        f"withheld: {packet.withheld}",
        f"  scope: {payload['scope']}",
        f"  token: {payload['token']}",
    ]
    if packet.refusal:
        lines.append(f"  refused: {packet.refusal}")
    if not revalidation.fresh:
        lines.append(f"  stale: {revalidation.reason} {revalidation.detail}".rstrip())
    for excerpt in excerpts:
        lines.append(
            f"  expanded {excerpt.kind}: {excerpt.size} chars"
            f"{' (truncated)' if excerpt.truncated else ''}"
            f"{'' if excerpt.refusal is None else f' refused {excerpt.refusal}'}"
        )
    _emit(payload, lines, as_json=as_json)
    return EXIT_OK if fresh else EXIT_REFUSED


def decide_from_args(args: argparse.Namespace, *, seams: Seams = DEFAULT_SEAMS) -> int:
    """Resolve an operation against what humans already decided on this plan.

    ``--list-statements`` prints the supervision record the register will read,
    with the stable statement ids a claim has to name. The host reads those,
    writes claims, and passes them back: that round trip is where judgement
    lives, and the ids being derived rather than invented is what keeps a claim
    from citing a statement nobody made.

    ``evidence_digest`` names the record those claims were read from. It is
    printed rather than stored, because storing it would be the cached
    permission this boundary refuses to keep; the host carries it to
    ``asl overseer act --reviewed-evidence`` and the action is refused
    ``evidence_changed`` if human evidence in scope moved in the meantime.

    A refusal is not a failure to try harder. ``held`` and ``human_decision``
    both mean a person already settled this, or has not, and the honest next
    move is the escalation this prints rather than an action.
    """
    as_json = bool(getattr(args, "json", False))
    state_dir = _state_dir(args)
    try:
        inventory = seams.build_inventory(state_dir=state_dir)
        candidate = _resolve_candidate(inventory, args.plan_dir)
        incident_id = None if args.incident_id is None else int(args.incident_id)
        scope = _scope_for(candidate, incident_id=incident_id)
        claims = parse_claims(_claims_text(args), scope=scope)
        _decision, grant = _grant_for(
            candidate,
            access=overseer_policy_client.ACCESS_CONTEXT,
            seams=seams,
            timeout=float(args.timeout),
        )
    except OverseerCliError as error:
        return _failure("overseer-decide", error, as_json=as_json)
    evidence = gather(
        candidate=candidate,
        grant=grant,
        incident_id=incident_id,
        seams=seams,
        state_dir=state_dir,
        history=bool(args.history),
        generated_classes=tuple(args.generated_classes or ()),
        timeout=float(args.timeout),
    )
    reviewed = overseer_decisions.evidence_digest(evidence.statements, scope=scope)
    if args.list_statements:
        payload = responses.success_payload(
            "overseer-decide",
            listed=True,
            scope=_scope_payload(scope),
            evidence_digest=reviewed,
            history=_history_payload(evidence.history),
            statements=[_statement_payload(item) for item in evidence.statements],
        )
        lines = [f"{len(evidence.statements)} statement(s) in scope"]
        lines.extend(
            f"  {item.statement_id} [{item.voice}] {item.text[:72]}"
            for item in evidence.statements
        )
        lines.append(f"  evidence digest: {reviewed}")
        _emit(payload, lines, as_json=as_json)
        return EXIT_OK
    ruling, register = _rule(evidence=evidence, claims=claims, operation=args.operation)
    settlements = [
        overseer_decisions.settle(register=register, topic=topic, scope=scope)
        for topic in overseer_decisions.HUMAN_TOPICS
    ]
    escalation: Escalation | None = None
    if not ruling.authorized:
        escalation = Escalation(
            reason=ruling.refusal or "no_decision",
            question=(
                f"May the overseer {args.operation} on {candidate.plan_dir}? "
                f"{ruling.summary}".strip()
            ),
            blocked=(f"{candidate.plan_dir}::{args.operation}",),
            independent=_independent_plans(inventory, excluding=plan_dir_of(candidate)),
        )
    payload = responses.success_payload(
        "overseer-decide",
        listed=False,
        authorized=ruling.authorized,
        ruling=_ruling_payload(ruling),
        evidence_digest=reviewed,
        history=_history_payload(evidence.history),
        human_settlements=[
            {
                "topic": settlement.topic,
                "settled": settlement.decision is not None,
                "stance": (
                    None if settlement.decision is None else settlement.decision.stance
                ),
            }
            for settlement in settlements
        ],
        rejected_claims=[
            {
                "statement_id": rejection.claim.statement_id,
                "reason": rejection.reason,
                "detail": rejection.detail,
            }
            for rejection in register.rejections
        ],
        escalation=None if escalation is None else escalation.payload(),
    )
    lines = [
        f"{args.operation} on {candidate.plan_dir}: "
        f"{'authorized' if ruling.authorized else f'refused ({ruling.refusal})'}",
        f"  {ruling.summary}",
        f"  evidence digest: {reviewed}",
    ]
    for rejection in register.rejections:
        lines.append(f"  rejected {rejection.claim.statement_id}: {rejection.reason}")
    if escalation is not None:
        lines.append(f"  ask a human: {escalation.question}")
        lines.append(f"  blocked: {', '.join(escalation.blocked)}")
        lines.append(
            "  meanwhile: "
            f"{', '.join(escalation.independent) or 'nothing else in scope'}"
        )
    _emit(payload, lines, as_json=as_json)
    return EXIT_OK if ruling.authorized else EXIT_REFUSED


def act_from_args(args: argparse.Namespace, *, seams: Seams = DEFAULT_SEAMS) -> int:
    """Hand one action to the guarded dispatcher. The only mutating leaf.

    ``--token`` and ``--scope`` are required and are not recomputed here. They
    come from the ``asl overseer inspect`` that read the context this action is
    a response to; recomputing them would make them agree with themselves and
    turn ``stale_action`` into a guard that can never fire.

    The authority callback re-derives the ruling from live supervision each time
    the dispatcher asks, which it does twice: once before the adapter prepares
    and once immediately before it performs. Re-deriving catches a revocation,
    because a revoked grant is a missing grant. It does not on its own catch a
    *new* statement: the claims cite what the host read, and a hold nobody
    claimed never becomes a decision, so the second reading of the same claims
    agrees with the first however the record moved.

    ``--reviewed-evidence`` is what closes that. It carries the digest
    ``asl overseer decide`` printed for the evidence the host actually read, and
    a mismatch is refused ``evidence_changed`` before any decision is weighed.
    When it is absent the first authority check pins the digest it observes, so
    a hold arriving *during* preparation is still caught -- what is not caught
    without the flag is a hold that arrived between ``decide`` and ``act``,
    which is the window the host closes by passing what it reviewed.
    """
    as_json = bool(getattr(args, "json", False))
    state_dir = _state_dir(args)
    try:
        ledger = seams.open_ledger(state_dir=state_dir)
        inventory = seams.build_inventory(state_dir=state_dir)
        candidate = _resolve_candidate(inventory, args.plan_dir)
        payload_for_action = _payload_for(args, args.operation)
        incident_id = int(args.incident_id)
        claims = parse_claims(
            _claims_text(args), scope=_scope_for(candidate, incident_id=incident_id)
        )
    except OverseerCliError as error:
        return _failure("overseer-act", error, as_json=as_json)
    timeout = float(args.timeout)
    binding = {
        "reviewed": str(getattr(args, "reviewed_evidence", "") or ""),
        "observed": "",
    }

    def authority(
        action: overseer_dispatch.Action,
    ) -> overseer_decisions.Ruling | None:
        _decision, grant = _grant_for(
            candidate,
            access=overseer_policy_client.ACCESS_CONTEXT,
            seams=seams,
            timeout=timeout,
        )
        if grant is None:
            return None
        evidence = gather(
            candidate=candidate,
            grant=grant,
            incident_id=action.incident_id,
            seams=seams,
            state_dir=state_dir,
            history=bool(args.history),
            generated_classes=tuple(args.generated_classes or ()),
            timeout=timeout,
        )
        binding["observed"] = overseer_decisions.evidence_digest(
            evidence.statements, scope=evidence.scope
        )
        if not binding["reviewed"]:
            binding["reviewed"] = binding["observed"]
        ruling, _register = _rule(
            evidence=evidence,
            claims=claims,
            operation=action.payload.operation,
            reviewed=binding["reviewed"],
        )
        return ruling

    action = overseer_dispatch.Action(
        plan_dir=plan_dir_of(candidate),
        run_id=args.run_id,
        incident_id=incident_id,
        payload=payload_for_action,
        token=args.token,
        scope=args.scope,
        repositories=overseer_policy_client.repositories_for(candidate.repositories),
    )
    dispatch = overseer_dispatch.dispatch(
        action=action,
        watcher_id=args.watcher_id,
        ledger=ledger,
        adapters=_adapters(
            authority=authority,
            ledger=ledger,
            state_dir=state_dir,
            seams=seams,
            timeout=timeout,
        ),
        authorize=seams.authorize,
        build_inventory=seams.build_inventory,
        inspect_run=seams.inspect_run,
        state_dir=state_dir,
        timeout=timeout,
    )
    escalation: Escalation | None = None
    if dispatch.escalated or not dispatch.performed:
        escalation = Escalation(
            reason=dispatch.refusal or "outcome_uncertain",
            question=(
                f"The overseer could not {args.operation} on {candidate.plan_dir}: "
                f"{dispatch.detail or dispatch.refusal}. How should this proceed?"
            ),
            blocked=(f"{candidate.plan_dir}::{args.operation}",),
            independent=_independent_plans(inventory, excluding=plan_dir_of(candidate)),
        )
    changed = bool(binding["observed"]) and binding["observed"] != binding["reviewed"]
    payload = responses.success_payload(
        "overseer-act",
        dispatch=_dispatch_payload(dispatch),
        evidence={
            "reviewed": binding["reviewed"],
            "observed": binding["observed"],
            "changed": changed,
        },
        escalation=None if escalation is None else escalation.payload(),
    )
    lines = [
        f"{args.operation} on {candidate.plan_dir}: "
        f"{'performed' if dispatch.performed else f'refused ({dispatch.refusal})'}"
    ]
    if dispatch.detail:
        lines.append(f"  {dispatch.detail}")
    if changed:
        lines.append(
            f"  evidence moved: reviewed {binding['reviewed']}, "
            f"observed {binding['observed']}"
        )
        lines.append("  re-run decide and read the record again before acting")
    if dispatch.recoveries:
        lines.append(
            f"  recoveries: {dispatch.recoveries}/{overseer_dispatch.RECOVERY_LIMIT}"
        )
    if escalation is not None:
        lines.append(f"  ask a human: {escalation.question}")
        lines.append(
            "  meanwhile: "
            f"{', '.join(escalation.independent) or 'nothing else in scope'}"
        )
    _emit(payload, lines, as_json=as_json)
    return EXIT_OK if dispatch.performed else EXIT_REFUSED


def reconcile_from_args(
    args: argparse.Namespace, *, seams: Seams = DEFAULT_SEAMS
) -> int:
    """Settle rows a previous dispatch left in doubt.

    A dispatch that dies between performing an action and recording its receipt
    leaves a row saying "maybe". Only the adapter that would have performed it
    can answer, and it answers from the ledger row alone, because the process
    that held the action is gone. Running this before acting again is what keeps
    a retry from becoming a duplicate.

    Answering that question means reading the run: state files, mailboxes, Git
    history. So this leaf asks AgentMux first, exactly as ``act`` does and under
    the same access category, and a refused or unreachable answer ends the
    invocation before an adapter is built. Reconciliation is the one operation
    whose whole job is to look at what a previous process touched, which makes
    it the easiest place for a withdrawn opt-in to be read straight past.
    """
    as_json = bool(getattr(args, "json", False))
    state_dir = _state_dir(args)
    try:
        ledger = seams.open_ledger(state_dir=state_dir)
        inventory = seams.build_inventory(state_dir=state_dir)
        candidate = _resolve_candidate(inventory, args.plan_dir)
    except OverseerCliError as error:
        return _failure("overseer-reconcile", error, as_json=as_json)

    decision, grant = _grant_for(
        candidate,
        access=overseer_policy_client.ACCESS_ACTION,
        seams=seams,
        timeout=float(args.timeout),
    )
    ledger.record_decision(decision)
    if decision.config_fingerprint:
        ledger.note_policy(config_fingerprint=decision.config_fingerprint)
    if grant is None:
        refusal = "unauthorized" if decision.available else "policy_unavailable"
        detail = decision.summary or decision.reason
        payload = responses.error_payload(
            "overseer-reconcile",
            reason=refusal,
            error=detail,
            plan_dir=candidate.plan_dir,
            authorized=False,
            settled=False,
            outcomes=[],
        )
        _emit(
            payload,
            [f"reconcile on {candidate.plan_dir}: refused ({refusal})", f"  {detail}"],
            as_json=as_json,
        )
        return EXIT_REFUSED

    def authority(
        action: overseer_dispatch.Action,
    ) -> overseer_decisions.Ruling | None:
        return None

    outcomes = overseer_dispatch.reconcile(
        plan_dir=plan_dir_of(candidate),
        ledger=ledger,
        adapters=_adapters(
            authority=authority,
            ledger=ledger,
            state_dir=state_dir,
            seams=seams,
            timeout=float(args.timeout),
        ),
        grant=grant,
    )
    settled = all(item.settled for item in outcomes)
    payload = responses.success_payload(
        "overseer-reconcile",
        plan_dir=candidate.plan_dir,
        authorized=True,
        settled=settled,
        outcomes=[
            {
                "record": _record_payload(item.record),
                "receipt": _receipt_payload(item.receipt),
                "settled": item.settled,
                "detail": item.detail,
            }
            for item in outcomes
        ],
    )
    lines = [f"{len(outcomes)} in-doubt action(s) on {candidate.plan_dir}"]
    for item in outcomes:
        lines.append(
            f"  {item.record.operation} #{item.record.action_id}: "
            f"{'settled' if item.settled else 'still in doubt'} {item.detail}".rstrip()
        )
    _emit(payload, lines, as_json=as_json)
    return EXIT_OK if settled else EXIT_REFUSED
