# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
from . import test_asl_bridge_projection_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .test_asl_bridge_projection_shared import *

QUESTION_BODY = "Should we drop the v1 column before the backfill lands?"
PAUSE_MESSAGE = "Awaiting confirmation on the v1 column"


class _Corpus:
    """A plans root plus its ASL state root, wired the way discovery expects."""

    def __init__(self, tmp_path: Path) -> None:
        self.plans_root = tmp_path / "plans"
        self.plans_root.mkdir()
        self.state_root = tmp_path / "state"

    def snapshot(self) -> dict[str, Any]:
        return bridge_projection.build_runs_snapshot(
            state_dir=self.state_root,
            plan_roots=(self.plans_root,),
            include_default_dirs=False,
        )

    def write_state(self, plan_dir: Path, payload: dict[str, Any]) -> Path:
        return _write_state_payload(
            plan_dir, self.state_root, {"plan_dir": str(plan_dir), **payload}
        )

    def paused_plan(self, name: str = "plan-alpha") -> Path:
        plan_dir = _make_workbench_plan(self.plans_root, name)
        artifact_dir = state.plan_state_dir(plan_dir, self.state_root) / "runs"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        last_message = artifact_dir / "last-message.txt"
        last_message.write_text(QUESTION_BODY, encoding="utf-8")
        self.write_state(
            plan_dir,
            {
                "ok": False,
                "paused": True,
                "reason": "agent-question",
                "pause_reason": "agent-question",
                "pause_message": PAUSE_MESSAGE,
                "run_id": "20260619T140000Z-1",
                "harness": "claude",
                "kitty_window_id": 17,
                "run_started_at": "2026-06-19T14:00:00+00:00",
                "turns": [
                    {
                        "session_id": "1",
                        "turn_index": 1,
                        "last_message_path": str(last_message),
                        "harness": "claude",
                        "started_at": "2026-06-19T14:00:00+00:00",
                    }
                ],
            },
        )
        return plan_dir

    def ready_plan(self, name: str = "plan-ready") -> Path:
        return _make_workbench_plan(self.plans_root, name)

    def recent_plan(self, name: str = "plan-recent") -> Path:
        plan_dir = _make_workbench_plan(self.plans_root, name)
        self.write_state(
            plan_dir,
            {
                "ok": True,
                "paused": False,
                "run_id": "20260619T110000Z-1",
                "harness": "claude",
                "run_started_at": "2026-06-19T11:00:00+00:00",
                "run_finished_at": "2026-06-19T11:30:00+00:00",
            },
        )
        return plan_dir

    def complete_plan(self, name: str = "plan-complete") -> Path:
        plan_dir = self.plans_root / name
        write_plan(plan_dir, ("complete", "complete"))
        canonical = plans_files.canonicalize_path(plan_dir)
        self.write_state(
            canonical,
            {
                "ok": True,
                "paused": False,
                "run_id": "20260619T100000Z-1",
                "harness": "claude",
                "run_started_at": "2026-06-19T10:00:00+00:00",
                "run_finished_at": "2026-06-19T10:30:00+00:00",
            },
        )
        return canonical

    def running_plan(self, name: str = "plan-running") -> Path:
        plan_dir = _make_workbench_plan(self.plans_root, name)
        _write_lock_file(
            plan_dir,
            self.state_root,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        self.write_state(
            plan_dir,
            {
                "ok": None,
                "paused": False,
                "run_id": "20260619T150000Z-1",
                "harness": "codex",
                "run_started_at": "2026-06-19T15:00:00+00:00",
                "active_turn": {
                    "session_id": "2",
                    "turn_index": 3,
                    "started_at": "2026-06-19T15:00:00+00:00",
                    "last_activity_at": "2026-06-19T15:04:00+00:00",
                },
            },
        )
        return plan_dir

    def waiting_plan(self, name: str = "plan-waiting") -> Path:
        plan_dir = _make_workbench_plan(self.plans_root, name)
        _write_lock_file(
            plan_dir,
            self.state_root,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )
        question = mailbox_appenders.append_question(
            plan_dir,
            QUESTION_BODY,
            run_id="20260619T160000Z-1",
            session_id="1",
            turn_index=4,
            state_dir=self.state_root,
        )
        self.write_state(
            plan_dir,
            {
                "ok": None,
                "paused": False,
                "run_id": "20260619T160000Z-1",
                "harness": "codex",
                "run_started_at": "2026-06-19T16:00:00+00:00",
                "active_turn": None,
                "waiting_for_reply": {
                    "question_id": question.id,
                    "session_id": "1",
                    "turn_index": 4,
                    "started_at": "2026-06-19T16:05:00+00:00",
                    "base_deadline": "2099-06-19T16:10:00+00:00",
                    "hard_deadline": "2099-06-19T16:15:00+00:00",
                    "last_lease_activity_at": None,
                },
            },
        )
        return plan_dir


def _request(request_id: str, method: str, **params: Any) -> bytes:
    payload: dict[str, Any] = {
        "type": "request",
        "protocol": bridge_protocol.PROTOCOL_VERSION,
        "id": request_id,
        "method": method,
        "params": params,
    }
    return json.dumps(payload).encode("utf-8")


class BridgeHelloTests(unittest.TestCase):
    def test_hello_declares_protocol_version_and_ordered_capabilities(self) -> None:
        payload = bridge_session.hello_payload()

        self.assertEqual("hello", payload["type"])
        self.assertEqual(2, payload["protocol"])
        self.assertEqual(list(bridge_session.CAPABILITIES), payload["capabilities"])
        # The handshake leads because nothing else a client wants to do is
        # available until it has completed one.
        self.assertEqual(
            [bridge_protocol.METHOD_HANDSHAKE, "runs.snapshot"],
            payload["capabilities"][:2],
        )
        self.assertEqual(list(bridge_protocol.EVENT_NAMES), payload["events"])
        self.assertEqual(
            list(bridge_protocol.MUTATION_METHODS), payload["mutation_methods"]
        )
        self.assertEqual(1024 * 1024, payload["max_frame_bytes"])
        self.assertIsInstance(payload["asl_version"], str)

    def test_hello_names_the_contract_set_the_bridge_implements(self) -> None:
        payload = bridge_session.hello_payload()

        self.assertEqual(
            agent_mux_projection.CONTRACT_SET_ID, payload["contract_set_id"]
        )
        self.assertIs(True, payload["handshake_required"])

    def test_hello_frame_is_one_newline_terminated_utf8_line(self) -> None:
        frame = bridge_protocol.encode_frame(bridge_session.hello_payload())

        self.assertTrue(frame.endswith(b"\n"))
        self.assertEqual(1, frame.count(b"\n"))
        self.assertEqual(
            bridge_session.hello_payload(), json.loads(frame.decode("utf-8"))
        )


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """Load only test cases defined in this focused section."""
    suite = unittest.TestSuite()
    cases = sorted(
        (
            value
            for value in globals().values()
            if isinstance(value, type)
            and issubclass(value, unittest.TestCase)
            and value.__module__ == __name__
        ),
        key=lambda case: case.__name__,
    )
    for case in cases:
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite
