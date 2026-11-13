"""The durable half of mutation admission for the operations journal.

:mod:`agent_session_loop.operation_rules` decides what may be done and what an honest
answer about an interrupted mutation is. It touches no filesystem on purpose,
so those rules stay pure. This module is where those decisions become
durable: a per-plan journal, an ``flock`` that serializes read-decide-write
across processes, a dispatch record written *before* the side effect, and a
restart pass that reconciles what it finds against ASL's own state.

Three properties are the point, and each is a file fact rather than an
in-process one:

* **The journal outlives the process.** Launch receipts kept in a dict make a
  retry idempotent only while the process that answered the first request is
  still running. The journal lives beside the redirect journal in the plan's
  state directory, so a restarted bridge -- or a second one, or another local
  caller -- reads the same record.

* **Admission is serialized between processes.** Every mutating entry point
  here holds ``LOCK_EX`` on the journal fd across the whole read-decide-append,
  which is the same discipline ``redirect_store_journal`` applies to
  corrections. Two submissions racing from two processes therefore produce one
  admission and one refusal, never two admissions.

* **The plan lock stays the execution authority.** Nothing here acquires,
  releases, inspects-for-reclaim, or otherwise touches ASL's plan lock;
  :func:`reconcile_plan` reads it through ``intervention.inspect_run``, which
  classifies without reclaiming. Admission is a gate *in front of* that
  authority: it stops a second attempt from being started, and the lock stops a
  second run from executing. Removing either would be a regression, and they
  are not alternatives to one another.

The journal holds identifiers only. Every row goes through
:func:`agent_session_loop.operation_rules.persist_operation` on the way in and
on the way out, so the reply text an operator typed and the action token that
would let it be sent again cannot reach the file even if a caller hands them
over.
"""

from __future__ import annotations

import contextlib as contextlib
import fcntl as fcntl
import json as json
import os as os
import socket as socket
from collections.abc import Callable as Callable
from collections.abc import Iterator as Iterator
from collections.abc import Mapping as Mapping
from dataclasses import dataclass as dataclass
from dataclasses import field as field
from pathlib import Path as Path
from typing import Any as Any
from typing import Never as Never

from . import clock as clock
from . import durability as durability
from . import events as events
from . import intervention as intervention
from . import mailbox as mailbox
from . import mailbox_models as mailbox_models
from . import plans_files as plans_files
from . import state as state
from .operation_evidence import _evidence_for as _evidence_for
from .operation_evidence import _owner_alive as _owner_alive
from .operation_evidence import _reply_evidence as _reply_evidence
from .operation_evidence import _run_facts as _run_facts
from .operation_evidence import events_path_for_plan as events_path_for_plan
from .operation_evidence import owner_alive as owner_alive
from .operation_journal import ERROR_JOURNAL_FULL as ERROR_JOURNAL_FULL
from .operation_journal import ERROR_JOURNAL_READ as ERROR_JOURNAL_READ
from .operation_journal import ERROR_JOURNAL_WRITE as ERROR_JOURNAL_WRITE
from .operation_journal import (
    OPERATIONS_JOURNAL_FILE_NAME as OPERATIONS_JOURNAL_FILE_NAME,
)
from .operation_journal import (
    OPERATIONS_JOURNAL_MAX_BYTES as OPERATIONS_JOURNAL_MAX_BYTES,
)
from .operation_journal import (
    OPERATIONS_JOURNAL_SUPPORTED_VERSIONS as OPERATIONS_JOURNAL_SUPPORTED_VERSIONS,
)
from .operation_journal import (
    OPERATIONS_JOURNAL_VERSION as OPERATIONS_JOURNAL_VERSION,
)
from .operation_journal import (
    OPERATIONS_RETAINED_SETTLED as OPERATIONS_RETAINED_SETTLED,
)
from .operation_journal import _append_locked as _append_locked
from .operation_journal import _compact_locked as _compact_locked
from .operation_journal import _encode as _encode
from .operation_journal import _ends_with_newline as _ends_with_newline
from .operation_journal import _fail as _fail
from .operation_journal import _hostname as _hostname
from .operation_journal import _locked_journal as _locked_journal
from .operation_journal import _moment as _moment
from .operation_journal import _parse_line as _parse_line
from .operation_journal import _read_locked as _read_locked
from .operation_journal import _read_shared as _read_shared
from .operation_journal import _Replay as _Replay
from .operation_journal import _replay as _replay
from .operation_journal import _settled_order as _settled_order
from .operation_journal import _write_at as _write_at
from .operation_journal import export_operations as export_operations
from .operation_journal import journal_path_for_plan as journal_path_for_plan
from .operation_journal import new_operation_id as new_operation_id
from .operation_journal import open_operations as open_operations
from .operation_journal import plan_target as plan_target
from .operation_journal import read_operation as read_operation
from .operation_journal import read_operations as read_operations
from .operation_mutations import _MAX_RECONCILE_STEPS as _MAX_RECONCILE_STEPS
from .operation_mutations import _reconcile_locked as _reconcile_locked
from .operation_mutations import _update as _update
from .operation_mutations import admit_operation as admit_operation
from .operation_mutations import confirm_operation as confirm_operation
from .operation_mutations import mark_dispatched as mark_dispatched
from .operation_mutations import reconcile_plan as reconcile_plan
from .operation_mutations import settle_operation as settle_operation
