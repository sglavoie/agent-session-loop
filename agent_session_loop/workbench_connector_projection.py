# ruff: noqa: F401
"""The Mac provider behind ``asl workbench connect``.

:mod:`asl_workbench.connector_runtime` decides *when* a connector publishes and *what*
a frame may contain; this module is the half that knows what ASL is. It holds
three things:

* :class:`AslObservationSource` -- one pass over the corpus that produces the
  catalog generation, the active run rows, and the history window *together*,
  from one discovery. Nothing here spawns a process.
* :func:`project_run` -- the bridge's snapshot row translated into the closed
  run shape the transport declares.
* :class:`HttpsChannel` -- one bounded POST per frame to the Pi service's
  ``/v1/ingest``, mirroring the Companion's own ``PiClient``.

**No bridge subprocess.** The Companion's connector launched ``asl bridge`` and
turned its frames into observations, which meant a second ASL process, a second
discovery pass, and a projection that could only carry what the bridge's frame
format already carried. This calls
:func:`agent_session_loop.workbench_catalog.scan` and
:func:`agent_session_loop.workbench_history.project_history` in process
instead. One walk of the corpus now answers all three views, the catalog is
uncapped where the bridge's snapshot was capped at 200 rows, and the frames are
built from the shared services rather than re-derived from a child's output.

**Observation changes nothing.** Every read on this path is one the catalog
scan already promised: ``build_workbench_summary(reclaim=False)`` for
classification, :func:`agent_session_loop.state.inspect_lock_info` for locks,
and a history import that persists its checkpoint only when the connector says
the batch was delivered. No archive retention runs, no stale lock is reclaimed,
and no plan is repaired as a side effect of being described.

**No prose leaves.** :func:`agent_session_loop.bridge_projection.project_row` already
refuses to copy a harness's words into a row, and :func:`project_run` narrows
further: it keeps a question's *id*, time and count and drops its body, and it
drops ``plan_dir`` and ``workspace_root`` entirely. A plan's identity on the
wire is its key -- a digest -- so a remote service can hold a full catalog
without ever learning the layout of this machine's disk.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import signal
import socket
import ssl
import stat as stat_bits
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from asl_workbench import connector_payloads as portable_payloads
from asl_workbench import connector_protocols as portable_protocols
from asl_workbench import connector_support as portable_support
from asl_workbench import history as portable_history
from asl_workbench.catalog import CatalogEntry
from asl_workbench.connector_protocols import ObservationCycle
from asl_workbench.connector_runtime import ObservationConnector
from asl_workbench.connector_support import (
    CATALOG_PAGE_LIMIT,
    MAX_PUBLISHED_RUNS,
    Backoff,
    ChannelError,
    ConnectorError,
    SingleInstanceLock,
)
from asl_workbench.operations import (
    DETAIL_ACTION_TOKEN_REFUSED,
    METHOD_ABORT,
    METHOD_QUESTION_REPLY,
    METHOD_REDIRECT,
    METHOD_REPLY,
    METHOD_STOP_AFTER_CURRENT,
)
from asl_workbench.remote_actions import (
    MAX_BODY_CHARS,
    BodyUnavailable,
    MutationRequest,
)
from asl_workbench.transport import MAX_FRAME_BYTES

from . import (
    bridge_projection,
    bridge_protocol,
    responses,
    state,
    workbench_catalog,
    workbench_history,
    workbench_remote_actions,
    workbench_remote_reads,
)

COMMAND_NAME = "workbench-connect"

#: The Pi service route that accepts an upward frame. One route for all five
#: envelope kinds: the kind is inside the envelope, and a service that routed
#: by URL would have two places to keep the vocabulary.
INGEST_PATH = "/v1/ingest"

#: The route that exchanges an operation's single-use action token for the
#: prose it names. Separate from ``/v1/ingest`` because the contract set is
#: pinned byte-for-byte: a reply's body has no envelope field to ride in, and
#: inventing one would break every participant's contract check at once.
REDEEM_PATH = "/v1/commands/redeem"

#: How long one request may take. Short on purpose -- a connector that blocks
#: for a minute on a dead link stops heartbeating, which is the failure this
#: whole design exists to avoid. Matches the Companion's ``PiClient``.
REQUEST_TIMEOUT_SECONDS = 2.0

#: Statuses that mean the frame was taken. ``202`` is the service accepting a
#: frame it will apply behind its own transaction.
ACCEPTED_STATUSES: tuple[int, ...] = (200, 202)

#: How often the publication loop wakes. Fine-grained relative to the
#: heartbeat interval so that a shutdown signal is noticed promptly.
LOOP_INTERVAL_SECONDS = 0.2

#: States a run row is published for. A catalog carries every plan; a snapshot
#: carries the ones an operator might act on. Borrowed from the bridge rather
#: than restated so the two projections cannot disagree about what "active"
#: means.
ACTIVE_PRIMARY_STATES: tuple[str, ...] = bridge_protocol.ACTIVE_PRIMARY_STATES

#: Live work, which reserves snapshot space after questions that need a reply.
_LIVE_PRIMARY_STATES = frozenset({"running", "verifying"})

#: What the Pi may ask for, keyed by what ASL calls it locally.
#:
#: ``intervention``'s vocabulary is about a button on the Mac; ``operations``'
#: is about a mutation an admission gate can refuse. They are deliberately not
#: the same list, and two local actions have no remote form at all:
#: ``question.resolve`` has no operation method, and ``workbench.ensure`` opens
#: a browser on this machine, which is not a thing a remote service can want.
#: Publishing an action with no method would offer the operator a control the
#: far end cannot route.
REMOTE_ACTIONS: Mapping[str, str] = {
    "question.reply": METHOD_QUESTION_REPLY,
    "run.resume": METHOD_REPLY,
    "run.stop_after_current": METHOD_STOP_AFTER_CURRENT,
    "run.abort": METHOD_ABORT,
    "run.redirect": METHOD_REDIRECT,
}

#: The transport's identifier rule, and its complement. Restated here because
#: this module has to *produce* identifiers -- a connector id from a hostname,
#: a version from a release string -- and a producer that cannot state the rule
#: can only find out it broke it by being refused.
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9._:-]+")


class ConnectorSetupError(ConnectorError):
    """The connector could not be built from what the operator supplied."""
