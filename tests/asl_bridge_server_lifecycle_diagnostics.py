"""Diagnostics tests extracted from test_asl_bridge_server_lifecycle."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_session_loop import (
    bridge_server,
    intervention,
    mailbox_vocabulary,
)

from .asl_bridge_server_support import (
    REPLY_TEXT,
    WAIT,
    _Corpus,
    _handshake_request,
    _LineWriter,
    _request,
)


class BridgeDiagnosticsTests(unittest.TestCase):
    def test_serve_stdio_keeps_reply_text_out_of_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir, _ = corpus.waiting_plan()
            row = corpus.row_for(plan_dir)
            read_fd, write_fd = os.pipe()
            writer = _LineWriter()
            diagnostics = io.StringIO()

            def serve() -> None:
                with os.fdopen(read_fd, "rb") as reader:
                    bridge_server.serve_stdio(
                        reader=reader,
                        writer=writer,
                        diagnostics=diagnostics,
                        state_dir=corpus.state_root,
                        plan_roots=(corpus.plans_root,),
                        include_default_dirs=False,
                        poll_interval=3600.0,
                    )

            server = threading.Thread(target=serve, name="serve-stdio", daemon=True)
            server.start()
            try:
                writer.wait_for('"type":"hello"', WAIT)
                # The first projection is built off the protocol loop, so a
                # handshake can legitimately arrive before there is a digest to
                # name. That refusal is retryable, and retrying is what a real
                # client does: the digest is a function of the corpus alone, so
                # every attempt names the snapshot it is about to be served.
                for attempt in range(200):
                    request_id = f"hs-{attempt}"
                    os.write(
                        write_fd,
                        _handshake_request(corpus.snapshot()["digest"], request_id)
                        + b"\n",
                    )
                    settled = writer.wait_for(f'"id":"{request_id}"', WAIT)
                    if '"ok":true' in settled:
                        break
                    time.sleep(0.05)
                else:  # pragma: no cover - the worker never published
                    self.fail("the bridge never produced a first projection")

                os.write(
                    write_fd,
                    _request(
                        "r1",
                        intervention.ACTION_REPLY,
                        plan_dir=str(plan_dir),
                        operation_id="op-1",
                        action_token=row["action_token"],
                        text=REPLY_TEXT,
                    )
                    + b"\n",
                )
                writer.wait_for('"id":"r1"', WAIT)
            finally:
                os.close(write_fd)
                server.join(timeout=WAIT)

            self.assertFalse(server.is_alive())
            self.assertNotIn(REPLY_TEXT, diagnostics.getvalue())
            self.assertNotIn(REPLY_TEXT, writer.getvalue())
            self.assertIn('"ok":true', writer.getvalue())
            self.assertIn(
                mailbox_vocabulary.KIND_REPLY,
                [line["kind"] for line in corpus.mailbox_lines(plan_dir)],
            )


_SERVE_REAL_STDIO = """
import sys
from agent_session_loop import bridge_server
sys.exit(
    bridge_server.serve_stdio(
        reader=sys.stdin.buffer,
        writer=sys.stdout,
        diagnostics=sys.stderr,
        state_dir=sys.argv[1],
        include_default_dirs=False,
    )
)
"""


class BridgeClosedStdoutTests(unittest.TestCase):
    def test_a_client_closing_stdout_gets_a_clean_exit(self) -> None:
        """The frame that hit EPIPE stays buffered and was flushed again at exit.

        That second flush printed ``Exception ignored ... BrokenPipeError`` and
        turned the documented exit 0 into 120.
        """
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_root.mkdir()
            child = subprocess.Popen(
                [sys.executable, "-c", _SERVE_REAL_STDIO, str(state_root)],
                cwd=repo_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert child.stdout is not None and child.stdin is not None
                self.assertIn(b'"type":"hello"', child.stdout.readline())
                child.stdout.close()
                child.stdin.write(_request("r1", "runs.snapshot") + b"\n")
                child.stdin.flush()
                _, stderr = child.communicate(timeout=WAIT * 4)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()

        self.assertEqual(0, child.returncode, stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"BrokenPipeError", stderr)
