"""Owned runner crash injection and explicit predecessor cleanup evidence."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_session_loop import process_tree
from tests.asl_live_lifecycle_process import Commands


class CrashingRun:
    """Keep a real child handle; never infer its death from an observation timeout."""

    def __init__(
        self, commands: Commands, child: subprocess.Popen, record: dict
    ) -> None:
        self.commands = commands
        self.child = child
        self.record = record
        self.birth: str | None = None
        self.descendants: dict[str, Any] = {}
        self.evidence: dict[str, Any] = {"runner_pid": child.pid, "birth": self.birth}

    def save(self) -> None:
        (self.commands.root / "crash-processes.json").write_text(
            json.dumps(self.evidence, indent=2) + "\n"
        )

    def wait_for_checkpoint(self, state_path: Path, marker: Path, nonce: str) -> dict:
        while True:
            self.commands.remaining()
            if self.child.poll() is not None:
                raise RuntimeError("runner exited before the crash checkpoint")
            if marker.is_file():
                # The marker is written by a real blocking tool. A partial
                # marker write is an observation to retry, not a new launch.
                try:
                    checkpoint = json.loads(marker.read_text())
                    durable = json.loads(state_path.read_text())
                except ValueError, FileNotFoundError:
                    time.sleep(0.05)
                    continue
                turns = durable.get("turns", [])
                active = durable.get("active_turn") or {}
                if (
                    checkpoint.get("nonce") != nonce
                    or len(turns) != 1
                    or turns[0].get("session_id") != "1"
                    or turns[0].get("harness") != durable["run_config"]["harness"]
                    or not turns[0].get("verification", {}).get("passed")
                    or active.get("session_id") != "2"
                    or durable.get("run_finished_at")
                ):
                    raise RuntimeError(
                        "checkpoint lacks verified session 1 and active session 2"
                    )
                self.descendants = process_tree.collect_descendant_evidence(
                    self.child.pid
                )
                self.evidence["descendants"] = self.descendants
                self.evidence["checkpoint"] = checkpoint
                self.save()
                if (
                    self.birth is None
                    or process_tree.process_birth(self.child.pid) != self.birth
                    or not self.descendants.get("collected")
                    or checkpoint.get("pid")
                    not in [row["pid"] for row in self.descendants.get("entries", [])]
                ):
                    raise RuntimeError(
                        "blocking tool is not an identity-sampled runner descendant"
                    )
                return durable
            time.sleep(0.05)

    def crash(self) -> None:
        if (
            self.child.poll() is not None
            or process_tree.process_birth(self.child.pid) != self.birth
        ):
            raise RuntimeError(
                "the owned runner exited or changed identity before SIGKILL"
            )
        os.kill(self.child.pid, signal.SIGKILL)
        returncode = self.child.wait(timeout=min(10, self.commands.remaining()))
        self.record["returncode"] = returncode
        self.evidence.update(signal="SIGKILL", returncode=returncode)
        self.save()
        if returncode != -signal.SIGKILL:
            raise RuntimeError("runner did not die from the injected SIGKILL")

    def stop_predecessors(self) -> dict:
        """The operator's explicit stop of the sampled orphan processes.

        This is separate from crashing the runner: SIGKILL gives ASL no chance
        to stop its children. Every signal uses the existing birth-check seam.
        """
        evidence = dict(self.descendants)
        evidence["escaped_pids"] = [row["pid"] for row in evidence.get("entries", [])]
        stopped = process_tree.terminate_escaped_descendants(evidence)
        self.evidence["operator_stop"] = stopped
        self.save()
        until = min(self.commands.until, time.monotonic() + 10)
        while True:
            confirmation = self.confirm()
            if confirmation["complete"]:
                self.evidence["pre_resume_confirmation"] = confirmation
                self.save()
                return confirmation
            self.commands.remaining()
            if time.monotonic() >= until:
                raise RuntimeError(
                    f"predecessor termination is incomplete: {confirmation}"
                )
            time.sleep(0.05)

    def confirm(self) -> dict:
        confirmation = process_tree.confirm_termination(
            pgid=self.child.pid, evidence=self.descendants
        )
        self.evidence["confirmation"] = confirmation
        self.save()
        return confirmation

    def cleanup(self) -> None:
        # Cleanup cannot earn acceptance. Its result is retained separately,
        # including when sampling or the scenario failed before the crash.
        if self.child.poll() is None:
            self.descendants = process_tree.collect_descendant_evidence(self.child.pid)
            process_tree.force_stop_process_group(self.child.pid)
            self.child.wait(timeout=10)
        evidence = dict(self.descendants)
        evidence["escaped_pids"] = [row["pid"] for row in evidence.get("entries", [])]
        self.evidence["cleanup"] = process_tree.terminate_escaped_descendants(evidence)
        self.record["returncode"] = self.child.returncode
        self.save()


@contextmanager
def start_run(commands: Commands, argv: list[str]) -> Iterator[CrashingRun]:
    commands.remaining()
    index = len(commands.calls)
    out = commands.root / f"{index:03d}-crashed-run.stdout"
    err = commands.root / f"{index:03d}-crashed-run.stderr"
    record = {
        "label": "crashed-run",
        "argv": argv,
        "stdout": str(out),
        "stderr": str(err),
    }
    commands.calls.append(record)
    started = time.monotonic()
    with out.open("w") as stdout, err.open("w") as stderr:
        child = subprocess.Popen(
            argv,
            cwd=commands.root / "repo",
            env=commands.env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        run = CrashingRun(commands, child, record)
        try:
            # Sampling may itself be interrupted. Establish the cleanup scope
            # before spawning ps so even an interrupted __enter__ owns teardown.
            run.birth = process_tree.process_birth(child.pid)
            run.evidence["birth"] = run.birth
            yield run
        finally:
            run.cleanup()
            record["seconds"] = round(time.monotonic() - started, 3)
