"""Shared fixtures and helpers for the verification test modules."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    contracts,
    plans_session_ids,
    review_session_body,
    review_session_contract,
    verification,
)
from tests.support import (
    kill_marked_pids,
    write_process_tree_scripts,
)

#: A descendant that leaves the check's process group and keeps the inherited
#: stdout/stderr open, so the reader threads draining them never see EOF while
#: it lives.
_DETACHED_ESCAPEE_SOURCE = (
    "import os, pathlib, sys, time\n"
    "root = pathlib.Path(sys.argv[1])\n"
    "pending = root / 'pending' / 'escapee'\n"
    "pending.write_text(str(os.getpid()))\n"
    "os.replace(pending, root / 'escapees' / 'escapee')\n"
    "time.sleep(120)\n"
)


#: The command a check runs: start the escapee, wait until it is live, exit 0.
_DETACHED_SPAWNER_SOURCE = (
    "import pathlib, subprocess, sys, time\n"
    "root = pathlib.Path(sys.argv[1])\n"
    "subprocess.Popen(\n"
    "    [sys.executable, str(root / 'escapee.py'), str(root)],\n"
    "    start_new_session=True,\n"
    ")\n"
    "while not (root / 'escapees' / 'escapee').exists():\n"
    "    time.sleep(0.01)\n"
)


def write_detached_spawner_script(root: Path) -> Path:
    """Lay out a command that exits 0 leaving one `setsid` escapee behind."""
    for name in ("pending", "escapees"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "escapee.py").write_text(_DETACHED_ESCAPEE_SOURCE, encoding="utf-8")
    spawner = root / "spawn_detached.py"
    spawner.write_text(_DETACHED_SPAWNER_SOURCE, encoding="utf-8")
    return spawner


class VerificationExecutorFixtures(unittest.TestCase):
    def _session_id(self, value: str = "1") -> plans_session_ids.SessionId:
        return plans_session_ids.parse_session_id(value)

    def _contract(
        self,
        checks: tuple[contracts.VerificationCheck, ...],
        *,
        parallel: bool = False,
    ) -> contracts.CompletionContract:
        return contracts.CompletionContract(
            session_id=self._session_id(),
            done_when=("done",),
            verify=contracts.VerifyRecipe(checks=checks, parallel=parallel),
        )

    #: Leave one background process behind in the check's own process group and
    #: exit 0. Its output is redirected so it does not also hold the captured
    #: pipes -- that is the separate escapee case.
    LEFTOVER_COMMAND = (
        "mkdir -p sleepers; sleep 300 >/dev/null 2>&1 & "
        "echo $! > sleepers/leftover; exit 0"
    )

    def _detached_tree_contract(self, root: Path) -> contracts.CompletionContract:
        """A one-check sequential recipe whose tree leaves a `setsid` escapee."""
        parent_script, grandchild_script = write_process_tree_scripts(root)
        self.addCleanup(kill_marked_pids, root / "grandchildren")
        return self._contract(
            (
                contracts.CommandCheck(
                    name="tree",
                    argv=(
                        "python3",
                        str(parent_script),
                        "tree",
                        str(root),
                        str(grandchild_script),
                        "detached",
                    ),
                    # Far above the interrupt window, so an interrupt rather
                    # than the timeout is what ends the check; still bounded so
                    # a fixture that never starts fails instead of hanging.
                    timeout_seconds=30.0,
                ),
            )
        )

    def _interrupt_when_marker_appears(self, marker: Path) -> None:
        """Ctrl-C the calling thread once ``marker`` exists.

        The check runs in a session of its own, so a SIGINT aimed at this
        process reaches the test runner alone -- everything the check spawned
        is left for the code under test to clean up. Nothing is signalled if
        the marker never appears: the check's own timeout bounds that instead.
        """

        def interrupt() -> None:
            deadline = time.monotonic() + 20
            while not marker.exists():
                if time.monotonic() > deadline:
                    return
                time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGINT)

        thread = threading.Thread(target=interrupt, daemon=True)
        self.addCleanup(thread.join, 5.0)
        thread.start()

    def _record_observer(
        self,
    ) -> tuple[verification.VerificationObserver, list[tuple[str, Any]]]:
        events: list[tuple[str, Any]] = []
        observer = verification.VerificationObserver(
            on_check_started=lambda event: events.append(("started", event)),
            on_check_finished=lambda event: events.append(("finished", event)),
        )
        return observer, events

    def _command(
        self,
        argv: tuple[str, ...],
        *,
        name: str = "cmd",
        timeout_seconds: float = 10.0,
        generated_gate: bool = False,
    ) -> contracts.CommandCheck:
        return contracts.CommandCheck(
            name=name,
            argv=argv,
            timeout_seconds=timeout_seconds,
            generated_gate=generated_gate,
        )

    #: One of the two names the runner gives its generated review gates, which
    #: is also the ``CHECK`` constant that gate stamps on its rejection line.
    GATE = "final-review-artifact-consistent"

    #: Application content that fits every token shape a gate's own values do.
    TOKEN = "INTERNAL_PROPRIETARY_TOKEN_ABC123"

    def _diagnostic_line(self, **fields: Any) -> str:
        """One marked rejection line, as a generated gate's ``fail`` writes it."""
        return (
            json.dumps(
                {
                    **fields,
                    verification.FAILURE_DIAGNOSTIC_MARKER_KEY: (
                        verification.FAILURE_DIAGNOSTIC_MARKER_VALUE
                    ),
                },
                sort_keys=True,
            )
            + "\n"
        )

    def _writes_stderr(self, *lines: str, exit_code: int) -> tuple[str, ...]:
        source = "import sys\n" + "".join(
            f"sys.stderr.write({line!r})\n" for line in lines
        )
        return ("python3", "-c", f"{source}sys.exit({exit_code})")

    def _materialized_review_contract(
        self, plan_dir: Path
    ) -> contracts.CompletionContract:
        """The review recipe as the runner writes it, then authenticates it.

        Nothing here is hand-built: the session file is the runner's own
        rendering, the contract is parsed back out of it, and provenance is
        re-derived from a fresh rendering — the same three steps a review turn
        takes.
        """
        session_id = plans_session_ids.parse_session_id("9")
        (plan_dir / f"session-{session_id.value}.md").write_text(
            review_session_body.build_generated_review_session_body(
                session_id, review_harness="codex", plan_dir=plan_dir
            ),
            encoding="utf-8",
        )
        contract = contracts.read_completion_contract(plan_dir, session_id.value)
        assert contract is not None
        return review_session_contract._authenticated_generated_review_contract(
            contract, plan_dir=plan_dir
        )
