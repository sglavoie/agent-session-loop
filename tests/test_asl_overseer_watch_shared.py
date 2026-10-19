# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
"""The invoked watcher: what it takes on, what it leaves alone, and who owns it.

Six claims, and none of them is provable by reading ``overseer_watch``.

*Age boundaries.* The invocation window is a trailing 24 hours, so the pause
exactly on the edge and the pause one second past it must land on opposite
sides -- and an undated pause has to fall outside rather than inside. Every one
of these is asserted against an injected clock rather than the wall, because a
boundary condition tested at "roughly now" is a boundary condition tested
nowhere.

*Newly started runs.* The watcher watches while invoked, so a run that starts
between two sweeps has to be enrolled without the invocation restarting.
``NewlyStartedRunTests`` grows the corpus from inside the sleep hook, which is
the only honest way to stage "while invoked".

*Restart.* Ownership and the servicing rotation both have to survive the
process. Each is re-read through a second ``open_ledger`` over the same file,
and the run that was replaced while nobody was watching has to be reported as
superseded rather than silently continued.

*Duplicate watchers.* Two watchers over one state root must not both service a
plan, the second must not spend an authorization subprocess discovering that,
and the only thing that moves a lease off an unwilling holder is the expiry of
its term -- never a liveness guess about its process.

*Exclusion changes.* An authorization that was granted and is later refused has
to revoke the enrollment *and* let go of the lease, or an excluded project stays
pinned to a watcher that is no longer allowed to watch it. The same holds for a
run that ages out of the window and for a selector that stops naming it.

*Fair servicing.* With a fleet larger than one sweep's budget, no plan may be
serviced twice before every plan has been serviced once. The fixture charges a
fixed cost per authorization against the fake clock, so "the budget ran out" is
an exact number of plans rather than a race.

Nothing here spawns a process or touches a repository -- the authorization seam
is injected, which is what keeps this module in `PORTABLE_MODULES`;
`test_watching_spawns_no_process` asserts that rather than assuming it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args
from unittest import mock

from agent_session_loop import (
    clock,
    events,
    overseer_discovery,
    overseer_policy_client,
    overseer_store_ledger,
    overseer_store_models,
    overseer_store_schema,
    overseer_watch,
    pause_policy,
    plans_files,
    state,
)

from .support import assert_no_runner_import

#: The fixed moment every fake clock starts from. Far enough from any real
#: "now" that a test accidentally reading the wall clock fails loudly.
