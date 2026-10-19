"""Incident context for the overseer: bounded, referenced, and re-authorized.

The watcher establishes that a run is blocked. This is the boundary that says
*what about it* -- and almost every decision here is about handing over less
than was asked for.

## A packet is references plus projections, never a copy

:func:`build_packet` reads six families of record -- the run's structured
status, its open questions, its run configuration, its verification results,
what the worker changed, and the supervision records that bear on it -- and
writes into the ledger exactly what
:class:`agent_session_loop.overseer_store_models.EvidenceInput` allows: a locator and
a digest. No content is stored. The consequence is the point: a packet cannot
outlive the authorization it was assembled under, because there is nothing
durable to outlive it with. Every later read goes back to the source, under a
fresh grant, or it does not happen.

That is also why a summary is regenerated rather than recalled. Loading a
packet re-reads each recorded source and re-summarizes it, so what an overseer
is shown describes the incident *now*: a question answered since the packet was
built no longer appears in it, and the source it came from is reported as
``changed`` rather than quietly served from a stale copy. A stored summary
would be a copy of project content by another name, and would say what was true
once with the confidence of what is true now.

## Three identities, and what each one refuses

A packet carries the plan, the run, and the question identities that make its
evidence applicable, plus the ``request_digest`` and ``config_fingerprint`` of
the grant that produced it. :func:`revalidate` compares a packet against a
freshly discovered candidate and rejects it when the *scope* moved -- a changed
repository declaration, a moved workspace root, a re-registered digest -- and
separately when the run identity moved. Scope is checked first: the grant was
issued for a repository set, so a declaration that has moved invalidates the
permission itself rather than merely the evidence it covered.

Exclusion is a different refusal and a stronger one. Loading and expanding both
gate through
:meth:`agent_session_loop.overseer_store_ledger.Ledger.read_evidence`, so a revoked
enrollment, an unobserved policy and a superseded one withhold the rows before
any file is touched -- and a revoked plan can never produce the fresh
``context`` grant that would unseal them. "Cannot be loaded or expanded after
exclusion" is therefore an operation with no success path rather than a rule
somebody has to remember.

## Bounds

Summaries are one clipped sentence and expansion is opt-in, one source at a
time, capped by :data:`MAX_EXCERPT_CHARS` and :data:`MAX_EXCERPT_RECORDS`. A
plan's mailbox and a run's turn artifacts are both unbounded in principle, and
an overseer that loads one of them whole has spent its own context on the first
incident it looked at. What was left out is counted rather than dropped:
:attr:`Source.truncated` says the summary does not name everything, and a
family that could not be read at all becomes a :class:`Gap` rather than an
absence the reader has to notice.

Durable records this boundary writes: **ledger rows only** -- one incident and
its evidence references. It writes nothing into any plan's state directory and
nothing into any repository; every plan-side path it knows is opened read-only.


Implementation is split into focused sibling modules; exports here preserve
the existing import surface. Patch collaborators in their owning modules.
"""

from __future__ import annotations

from .overseer_context_excerpts import (
    expand as expand,
)
from .overseer_context_models import (
    _SCOPE_DOMAIN as _SCOPE_DOMAIN,
)
from .overseer_context_models import (
    COMMIT_PREFIX as COMMIT_PREFIX,
)
from .overseer_context_models import (
    CONTEXT_ACCESS as CONTEXT_ACCESS,
)
from .overseer_context_models import (
    GAP_REASONS as GAP_REASONS,
)
from .overseer_context_models import (
    MAX_EXCERPT_CHARS as MAX_EXCERPT_CHARS,
)
from .overseer_context_models import (
    MAX_EXCERPT_RECORDS as MAX_EXCERPT_RECORDS,
)
from .overseer_context_models import (
    MAX_RECORDS as MAX_RECORDS,
)
from .overseer_context_models import (
    MAX_SUMMARY_CHARS as MAX_SUMMARY_CHARS,
)
from .overseer_context_models import (
    REFUSALS as REFUSALS,
)
from .overseer_context_models import (
    SCOPE_KIND as SCOPE_KIND,
)
from .overseer_context_models import (
    SOURCE_KINDS as SOURCE_KINDS,
)
from .overseer_context_models import (
    STALENESS_REASONS as STALENESS_REASONS,
)
from .overseer_context_models import (
    STATE_SPACE as STATE_SPACE,
)
from .overseer_context_models import (
    SUPERVISION_KINDS as SUPERVISION_KINDS,
)
from .overseer_context_models import (
    TURN_PREFIX as TURN_PREFIX,
)
from .overseer_context_models import (
    BuildInventory as BuildInventory,
)
from .overseer_context_models import (
    ContextError as ContextError,
)
from .overseer_context_models import (
    Excerpt as Excerpt,
)
from .overseer_context_models import (
    Gap as Gap,
)
from .overseer_context_models import (
    GapReason as GapReason,
)
from .overseer_context_models import (
    Packet as Packet,
)
from .overseer_context_models import (
    ReadMailbox as ReadMailbox,
)
from .overseer_context_models import (
    Refusal as Refusal,
)
from .overseer_context_models import (
    Revalidation as Revalidation,
)
from .overseer_context_models import (
    Source as Source,
)
from .overseer_context_models import (
    SourceKind as SourceKind,
)
from .overseer_context_models import (
    Staleness as Staleness,
)
from .overseer_context_models import (
    _Projection as _Projection,
)
from .overseer_context_packets import (
    build_packet as build_packet,
)
from .overseer_context_packets import (
    load_packet as load_packet,
)
from .overseer_context_projections import (
    _project_questions as _project_questions,
)
from .overseer_context_projections import (
    _project_run_config as _project_run_config,
)
from .overseer_context_projections import (
    _project_status as _project_status,
)
from .overseer_context_projections import (
    _project_supervision as _project_supervision,
)
from .overseer_context_projections import (
    _project_verification as _project_verification,
)
from .overseer_context_projections import (
    _project_worker_changes as _project_worker_changes,
)
from .overseer_context_reader import (
    _ABSENT_DETAIL as _ABSENT_DETAIL,
)
from .overseer_context_reader import (
    _Reader as _Reader,
)
from .overseer_context_scope import (
    _candidate_for as _candidate_for,
)
from .overseer_context_scope import (
    _request_digest as _request_digest,
)
from .overseer_context_scope import (
    _source_kind as _source_kind,
)
from .overseer_context_scope import (
    revalidate as revalidate,
)
from .overseer_context_scope import (
    scope_digest as scope_digest,
)
from .overseer_context_values import (
    _clip as _clip,
)
from .overseer_context_values import (
    _first as _first,
)
from .overseer_context_values import (
    _lines as _lines,
)
from .overseer_context_values import (
    _named as _named,
)
from .overseer_context_values import (
    _unavailable as _unavailable,
)
from .overseer_context_values import (
    _value as _value,
)
from .overseer_context_values import (
    _withheld as _withheld,
)

__all__ = [
    "CONTEXT_ACCESS",
    "GAP_REASONS",
    "MAX_EXCERPT_CHARS",
    "MAX_EXCERPT_RECORDS",
    "MAX_RECORDS",
    "MAX_SUMMARY_CHARS",
    "REFUSALS",
    "SOURCE_KINDS",
    "STALENESS_REASONS",
    "STATE_SPACE",
    "SUPERVISION_KINDS",
    "ContextError",
    "Excerpt",
    "Gap",
    "Packet",
    "Revalidation",
    "Source",
    "build_packet",
    "expand",
    "load_packet",
    "revalidate",
    "scope_digest",
]
