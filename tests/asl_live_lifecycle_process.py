"""Bounded CLI calls and owned-process cleanup for live lifecycle acceptance."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from agent_session_loop import process_tree

REPO_ROOT = Path(__file__).resolve().parents[1]
ASL = REPO_ROOT / "bin/.local/bin/asl"
HELPER = REPO_ROOT / "skills/asl/scripts/asl-autopilot.py"
MAX_HARNESS_LAUNCHES = 6


class Commands:
    """Keep every call's output, and never leave an owned child spending quota."""

    def __init__(self, root: Path, *, deadline: int) -> None:
        self.root = root
        self.until = time.monotonic() + deadline
        self.calls: list[dict[str, Any]] = []
        self.helpers: list[tuple[int, str | None]] = []
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = os.pathsep.join(
            [str(REPO_ROOT), self.env.get("PYTHONPATH", "")]
        )
        self.env["PATH"] = os.pathsep.join([str(ASL.parent), self.env.get("PATH", "")])

    def bound_harness(self, harness: str, executable: str) -> None:
        """Cap provider-capable process launches across validators and retries.

        Restore the caller's native config environment before exec, so ASL's
        isolated executable override cannot change the provider's credentials.
        """
        native_config = self.env.get("XDG_CONFIG_HOME")
        shim = self.root / "bounded-harness.py"
        counter = self.root / "harness-launches.jsonl"
        shim.write_text(
            "import fcntl, json, os, sys, time\n"
            f"executable = {executable!r}\n"
            "if sys.argv[1:] != ['--version']:\n"
            f"    with open({str(counter)!r}, 'a+') as ledger:\n"
            "        fcntl.flock(ledger, fcntl.LOCK_EX)\n"
            "        ledger.seek(0)\n"
            f"        if len(ledger.readlines()) >= {MAX_HARNESS_LAUNCHES}:\n"
            "            sys.exit('live acceptance harness launch budget exhausted')\n"
            "        ledger.write(json.dumps({'argv': sys.argv[1:], "
            "'at': time.time()}) + '\\n')\n"
            "        ledger.flush()\n"
            "        os.fsync(ledger.fileno())\n"
            f"native_config = {native_config!r}\n"
            "if native_config is None:\n"
            "    os.environ.pop('XDG_CONFIG_HOME', None)\n"
            "else:\n"
            "    os.environ['XDG_CONFIG_HOME'] = native_config\n"
            "os.execv(executable, [executable, *sys.argv[1:]])\n"
        )
        wrapper = self.root / "bounded-harness"
        # Shell argv is quoted independently; paths may contain spaces.
        import shlex

        wrapper.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
            f'{shlex.quote(str(shim))} "$@"\n'
        )
        wrapper.chmod(0o700)
        config = self.root / "asl-config/agent-session-loop/harnesses.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({harness: {"executable": str(wrapper)}}))
        self.env["XDG_CONFIG_HOME"] = str(self.root / "asl-config")

    def remaining(self) -> float:
        remaining = self.until - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("lifecycle wall-clock budget exhausted")
        return remaining

    def call(self, label: str, argv: list[str], *, timeout: float = 60) -> dict:
        limit = min(timeout, self.remaining())
        index = len(self.calls)
        stdout_path = self.root / f"{index:03d}-{label}.stdout"
        stderr_path = self.root / f"{index:03d}-{label}.stderr"
        started = time.monotonic()
        record: dict[str, Any] = {"label": label, "argv": argv}
        self.calls.append(record)
        with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
            child = subprocess.Popen(
                argv,
                cwd=self.root / "repo",
                env=self.env,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                record["returncode"] = child.wait(timeout=limit)
            except BaseException:
                evidence = process_tree.collect_descendant_evidence(child.pid)
                if child.poll() is None:
                    process_tree.force_stop_process_group(child.pid)
                process_tree.terminate_escaped_descendants(evidence)
                child.wait(timeout=10)
                raise
            finally:
                record["seconds"] = round(time.monotonic() - started, 3)
                record["stdout"] = str(stdout_path)
                record["stderr"] = str(stderr_path)
        try:
            payload = json.loads(stdout_path.read_text())
        except (ValueError, OSError) as error:
            raise RuntimeError(
                f"{label} returned no JSON; see {stdout_path}"
            ) from error
        if record["returncode"] or not isinstance(payload, dict):
            detail = (
                payload.get("error") or payload.get("reason") or str(payload)
                if isinstance(payload, dict)
                else str(payload)
            )
            raise RuntimeError(f"{label} failed: {detail}")
        return payload

    def asl(self, label: str, *args: str, timeout: float = 60) -> dict:
        return self.call(label, [str(ASL), *args, "--json"], timeout=timeout)

    def remember_helper(self, handshake: dict) -> None:
        if not handshake.get("ok"):
            raise RuntimeError(f"supervisor launch failed: {handshake}")
        pid = int(handshake["pid"])
        self.helpers.append((pid, process_tree.process_birth(pid)))

    def cleanup(self) -> list[dict]:
        """Stop only helpers and descendants whose sampled births still match."""
        outcomes = []
        for pid, birth in self.helpers:
            if birth is None or process_tree.process_birth(pid) != birth:
                continue
            evidence = process_tree.collect_descendant_evidence(pid)
            # The detached helper and runner use different process groups.
            # Treat the entire sampled tree as escapees, verifying each birth
            # immediately before signalling via ASL's existing termination seam.
            evidence["escaped_pids"] = [row["pid"] for row in evidence["entries"]]
            outcomes.append(process_tree.terminate_escaped_descendants(evidence))
            if process_tree.process_birth(pid) == birth:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
        return outcomes
