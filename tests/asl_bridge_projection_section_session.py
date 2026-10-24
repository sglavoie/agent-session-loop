# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import test_asl_bridge_projection_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_bridge_projection_shared import *
from .support_base import load_section_tests
from .asl_bridge_projection_section_hello import *

# A star import skips private names; the helpers shared across sections
# are named explicitly.
from .asl_bridge_projection_section_hello import _Corpus, _request
from .asl_bridge_projection_section_runs import *


class BridgeSessionTests(unittest.TestCase):
    def _session(
        self, snapshot: dict[str, Any] | None = None
    ) -> bridge_session.BridgeSession:
        payload = snapshot or {
            "protocol": 1,
            "rows": [],
            "row_count": 0,
            "warnings": [],
            "truncated": False,
            "digest": "seed",
        }
        return bridge_session.BridgeSession(snapshot_builder=lambda: payload)

    def test_runs_snapshot_request_returns_a_correlated_response(self) -> None:
        response = self._session().handle_frame(_request("req-1", "runs.snapshot"))

        self.assertEqual("response", response["type"])
        self.assertEqual("req-1", response["id"])
        self.assertTrue(response["ok"])
        self.assertEqual(0, response["result"]["row_count"])
        self.assertNotIn("error", response)

    def test_unknown_method_returns_a_stable_error_without_ending_the_loop(
        self,
    ) -> None:
        session = self._session()

        response = session.handle_frame(_request("req-1", "runs.explode"))

        self.assertEqual("req-1", response["id"])
        self.assertFalse(response["ok"])
        self.assertEqual("unknown-method", response["error"]["code"])
        self.assertFalse(response["error"]["retryable"])
        self.assertEqual("runs.explode", response["error"]["details"]["method"])
        self.assertTrue(
            session.handle_frame(_request("req-2", "runs.snapshot"))["ok"],
            "server rejected a valid request after an unknown method",
        )

    def test_invalid_protocol_version_is_rejected_by_code(self) -> None:
        frame = json.dumps(
            {
                "type": "request",
                "protocol": 99,
                "id": "req-1",
                "method": "runs.snapshot",
                "params": {},
            }
        ).encode("utf-8")

        response = self._session().handle_frame(frame)

        self.assertFalse(response["ok"])
        self.assertEqual("unsupported-protocol", response["error"]["code"])
        self.assertEqual("2", response["error"]["details"]["supported"])

    def test_malformed_json_is_answered_without_a_correlation_id(self) -> None:
        response = self._session().handle_frame(b"{not json")

        self.assertIsNone(response["id"])
        self.assertFalse(response["ok"])
        self.assertEqual("malformed-json", response["error"]["code"])

    def test_missing_request_id_and_method_are_invalid_requests(self) -> None:
        session = self._session()
        no_id = json.dumps(
            {"type": "request", "protocol": 2, "method": "runs.snapshot"}
        ).encode("utf-8")
        no_method = json.dumps(
            {"type": "request", "protocol": 2, "id": "req-1"}
        ).encode("utf-8")
        bad_params = json.dumps(
            {
                "type": "request",
                "protocol": 2,
                "id": "req-1",
                "method": "runs.snapshot",
                "params": [],
            }
        ).encode("utf-8")

        for frame in (no_id, no_method, bad_params):
            response = session.handle_frame(frame)
            self.assertFalse(response["ok"])
            self.assertEqual("invalid-request", response["error"]["code"])

    def test_a_failing_projection_is_a_retryable_internal_error(self) -> None:
        def explode() -> dict[str, Any]:
            raise RuntimeError("state root vanished")

        session = bridge_session.BridgeSession(snapshot_builder=explode)

        response = session.handle_frame(_request("req-1", "runs.snapshot"))

        self.assertFalse(response["ok"])
        self.assertEqual("internal-error", response["error"]["code"])
        self.assertTrue(response["error"]["retryable"])


class RunsChangedEventTests(unittest.TestCase):
    def test_unchanged_digest_never_emits_a_duplicate_event(self) -> None:
        payload = {
            "protocol": 1,
            "rows": [],
            "row_count": 0,
            "warnings": [],
            "truncated": False,
            "digest": "same",
        }
        session = bridge_session.BridgeSession(snapshot_builder=lambda: payload)

        first = session.poll()
        second = session.poll()
        third = session.poll()

        self.assertEqual("runs.changed", first["event"])
        self.assertEqual(1, first["seq"])
        self.assertIsNone(second)
        self.assertIsNone(third)

    def test_event_sequence_is_monotonic_across_changes(self) -> None:
        digests = iter(["a", "b", "b", "c"])
        session = bridge_session.BridgeSession(
            snapshot_builder=lambda: {
                "protocol": 1,
                "rows": [],
                "row_count": 0,
                "warnings": [],
                "truncated": False,
                "digest": next(digests),
            }
        )

        events = [event for event in (session.poll() for _ in range(4)) if event]
        seqs = [event["seq"] for event in events]

        self.assertEqual([1, 2, 3], seqs)

    def test_a_served_snapshot_suppresses_the_next_unchanged_event(self) -> None:
        payload = {
            "protocol": 1,
            "rows": [],
            "row_count": 0,
            "warnings": [],
            "truncated": False,
            "digest": "same",
        }
        session = bridge_session.BridgeSession(snapshot_builder=lambda: payload)

        session.handle_frame(_request("req-1", "runs.snapshot"))

        self.assertIsNone(session.poll())
        self.assertEqual(0, session.seq)


def _frames_from(data: bytes, *, read_size: int | None = None) -> list[Any]:
    """Every item a frame reader yields for ``data``, an oversize as the class.

    ``read_size`` caps each read, the way a pipe hands a long write over in
    pieces, so a frame split across reads is exercised as well as a whole one.
    """
    stream = io.BytesIO(data)

    def read(size: int) -> bytes:
        return stream.read(size if read_size is None else min(size, read_size))

    frames = bridge_server._FrameReader(read)
    items: list[Any] = []
    while True:
        while not frames.pending():
            frames.fill()
        try:
            frame = frames.next_frame()
        except bridge_server._FrameTooLarge:
            items.append(bridge_server._FrameTooLarge)
            continue
        if frame is None:
            return items
        items.append(frame)


class FrameBoundTests(unittest.TestCase):
    def test_oversize_inbound_frame_is_rejected_and_the_line_is_drained(self) -> None:
        oversize = b"x" * (bridge_protocol.MAX_FRAME_BYTES + 10) + b"\n"

        self.assertEqual(
            [bridge_server._FrameTooLarge, b'{"type":"request"}'],
            _frames_from(oversize + b'{"type":"request"}\n'),
        )

    def test_frame_of_exactly_the_ceiling_is_accepted(self) -> None:
        payload = b"y" * bridge_protocol.MAX_FRAME_BYTES

        self.assertEqual([payload], _frames_from(payload + b"\n"))
        self.assertEqual([payload], _frames_from(payload + b"\n", read_size=4096))

    def test_one_byte_over_the_ceiling_is_rejected_across_reads(self) -> None:
        payload = b"z" * (bridge_protocol.MAX_FRAME_BYTES + 1)

        self.assertEqual(
            [bridge_server._FrameTooLarge, b"{}"],
            _frames_from(payload + b"\n{}\n", read_size=4096),
        )

    def test_end_of_stream_reads_as_none(self) -> None:
        self.assertEqual([], _frames_from(b""))

    def test_an_unterminated_last_frame_is_still_delivered_at_end_of_stream(
        self,
    ) -> None:
        self.assertEqual([b"{}", b'{"a":1}'], _frames_from(b'{}\n{"a":1}'))

    def test_one_read_holding_several_frames_queues_every_one(self) -> None:
        frames = bridge_server._FrameReader(io.BytesIO(b"{}\n[]\n").read)

        frames.fill()

        self.assertEqual(b"{}", frames.next_frame())
        # The second frame is answerable without another read: this is what
        # keeps the serve loop from waiting on a select that cannot see it.
        self.assertTrue(frames.pending())
        self.assertEqual(b"[]", frames.next_frame())

    def test_per_field_bounds_keep_corrupt_state_inside_the_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = _make_workbench_plan(corpus.plans_root)
            corpus.write_state(
                plan_dir,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "z" * 100_000,
                    "pause_reason": "z" * 100_000,
                    "run_id": "r" * 100_000,
                    "harness": "h" * 100_000,
                    "turns": [{"session_id": "1", "turn_index": 1}],
                },
            )

            snapshot = corpus.snapshot()
            (row,) = snapshot["rows"]

            self.assertLessEqual(
                len(row["pause_reason"]), bridge_protocol.MAX_TEXT_CHARS
            )
            self.assertLessEqual(len(row["run_id"]), bridge_protocol.MAX_ID_CHARS)
            self.assertLessEqual(len(row["harness"]), bridge_protocol.MAX_ID_CHARS)
            self.assertLessEqual(
                len(bridge_protocol.encode_frame(snapshot)),
                bridge_protocol.MAX_FRAME_BYTES,
            )

    def test_aggregate_row_bound_is_enforced_before_serialization(self) -> None:
        self.assertEqual(200, bridge_protocol.MAX_ROWS)
        rows = [
            {
                "urgency_rank": 10,
                "plan_dir": f"/plans/{index:04d}",
                "label": None,
                "last_activity_at": None,
            }
            for index in range(bridge_protocol.MAX_ROWS + 5)
        ]
        ordered = sorted(rows, key=bridge_projection._row_sort_key)

        self.assertEqual(rows, ordered)

    def test_backlog_cannot_crowd_live_work_out_of_capped_snapshot(self) -> None:
        backlog = [
            {
                "primary_state": "invalid",
                "urgency_rank": 40,
                "plan_dir": f"/plans/{index:04d}",
                "last_activity_at": None,
            }
            for index in range(bridge_protocol.MAX_ROWS + 5)
        ]
        live = [
            {
                "primary_state": status,
                "urgency_rank": 60,
                "plan_dir": f"/plans/{status}",
                "last_activity_at": "2026-09-17T17:00:00+00:00",
            }
            for status in ("running", "verifying")
        ]
        recent = {
            "primary_state": "paused-for-reply",
            "urgency_rank": 10,
            "plan_dir": "/plans/recent",
            "last_activity_at": "2026-09-17T16:00:00+00:00",
        }
        discovered = workbench_registry_models.DiscoveryResult(
            plans=tuple(range(len(backlog) + 3))
        )
        summaries = [
            mock.Mock(primary_state=row["primary_state"])
            for row in [*backlog, *live, recent]
        ]
        with (
            mock.patch.object(
                workbench_discovery, "discover_plans", return_value=discovered
            ),
            mock.patch.object(
                workbench_summary, "build_workbench_summary", side_effect=summaries
            ),
            mock.patch.object(
                bridge_projection, "project_row", side_effect=[*backlog, *live, recent]
            ),
        ):
            snapshot = bridge_projection.build_runs_snapshot()
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(bridge_protocol.MAX_ROWS, snapshot["row_count"])
        for row in [*live, recent]:
            self.assertIn(row, snapshot["rows"])
        self.assertEqual(
            sorted(snapshot["rows"], key=bridge_projection._row_sort_key),
            snapshot["rows"],
        )


class _PrimedProjectionWorker(bridge_workers.ProjectionWorker):
    """A worker whose first walk has landed before the loop reads a frame.

    A cold `runs.snapshot` is refused retryably by design, and a client answers
    that by asking again. These tests are about framing and dispatch over a real
    pipe, and a one-shot stream cannot re-ask; walking the corpus once on the
    loop thread before the loop starts is what makes them assert a projection
    instead of a race with the worker.
    """

    def start(self) -> None:
        self.cycle()
        super().start()


class BridgeServeStdioTests(unittest.TestCase):
    def _serve(self, request_frames: bytes, corpus: _Corpus) -> list[dict[str, Any]]:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, request_frames)
        os.close(write_fd)
        writer = io.StringIO()
        with (
            mock.patch.object(
                bridge_server, "ProjectionWorker", _PrimedProjectionWorker
            ),
            os.fdopen(read_fd, "rb") as reader,
        ):
            code = bridge_server.serve_stdio(
                reader=reader,
                writer=writer,
                diagnostics=io.StringIO(),
                state_dir=corpus.state_root,
                plan_roots=(corpus.plans_root,),
                include_default_dirs=False,
                poll_interval=3600.0,
            )
        self.assertEqual(0, code)
        return [
            json.loads(line) for line in writer.getvalue().splitlines() if line.strip()
        ]

    def test_server_speaks_hello_before_reading_and_exits_cleanly_at_eof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            frames = self._serve(b"", _Corpus(Path(tmp)))

            self.assertEqual(1, len(frames))
            self.assertEqual("hello", frames[0]["type"])

    def test_server_answers_a_snapshot_request_over_the_pipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            plan_dir = corpus.paused_plan()

            frames = self._serve(_request("req-1", "runs.snapshot") + b"\n", corpus)

            self.assertEqual("hello", frames[0]["type"])
            response = frames[1]
            self.assertEqual("response", response["type"])
            self.assertEqual("req-1", response["id"])
            self.assertEqual(
                [str(plan_dir)],
                [row["plan_dir"] for row in response["result"]["rows"]],
            )

    def test_server_survives_malformed_and_unknown_frames_in_one_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = _Corpus(Path(tmp))
            corpus.paused_plan()

            frames = self._serve(
                b"{not json\n"
                + _request("req-1", "runs.nope")
                + b"\n"
                + _request("req-2", "runs.snapshot")
                + b"\n",
                corpus,
            )

            self.assertEqual(
                ["malformed-json", "unknown-method"],
                [frame["error"]["code"] for frame in frames[1:3]],
            )
            self.assertTrue(frames[3]["ok"])

    def test_broken_stdout_stops_the_loop_without_raising(self) -> None:
        class BrokenWriter(io.StringIO):
            def write(self, _text: str) -> int:
                raise BrokenPipeError

        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_root.mkdir()
            read_fd, write_fd = os.pipe()
            os.close(write_fd)

            with os.fdopen(read_fd, "rb") as reader:
                code = bridge_server.serve_stdio(
                    reader=reader,
                    writer=BrokenWriter(),
                    diagnostics=io.StringIO(),
                    state_dir=state_root,
                )

            self.assertEqual(0, code)


class BridgeCliTests(unittest.TestCase):
    def test_bridge_serve_parses_the_documented_invocation(self) -> None:
        args = cli.build_parser().parse_args(
            ["bridge", "serve", "--stdio", "--protocol", "1"]
        )

        self.assertEqual("bridge", args.command)
        self.assertEqual("serve", args.bridge_command)
        self.assertTrue(args.stdio)
        self.assertEqual(1, args.protocol)
        self.assertEqual("bridge_server:bridge_serve_from_args", args.handler_ref)

    def test_bridge_serve_accepts_state_dir_and_repeated_plan_roots(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "bridge",
                "serve",
                "--stdio",
                "--state-dir",
                "/tmp/state",
                "--plan-root",
                "/tmp/a",
                "--plan-root",
                "/tmp/b",
            ]
        )

        self.assertEqual("/tmp/state", args.state_dir)
        self.assertEqual(["/tmp/a", "/tmp/b"], args.plan_root)

    def test_the_retired_protocol_cannot_be_selected(self) -> None:
        """Protocol 1 fails before a byte of protocol is written."""
        args = cli.build_parser().parse_args(
            ["bridge", "serve", "--stdio", "--protocol", "1"]
        )

        self.assertEqual(2, bridge_server.bridge_serve_from_args(args))


if __name__ == "__main__":
    unittest.main()


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    return load_section_tests(loader, __name__)
