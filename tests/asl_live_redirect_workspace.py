from .asl_live_redirect_prompts import _excerpt
from .asl_live_redirect_support import *


@dataclass(frozen=True)
class Workspace:
    """A throwaway repository plus the plan and state directories beside it."""

    root: Path
    repo: Path
    plan_dir: Path
    state_dir: Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@contextmanager
def disposable_workspace(label: str, *, keep: bool = False) -> Iterator[Workspace]:
    """Create a throwaway git repository and tear it down afterwards.

    A real repository rather than a bare directory: every harness ASL launches
    behaves differently inside version control (Codex reports the diff, Claude
    reads the status, Copilot scopes its writes), so a check that ran outside
    one would be exercising a configuration no run ever uses.
    """
    root = Path(tempfile.mkdtemp(prefix=f"asl-live-{label}-")).resolve()
    try:
        repo = root / "repo"
        plan_dir = root / "plan"
        state_dir = root / "state"
        for path in (repo, plan_dir, state_dir):
            path.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "acceptance@example.invalid")
        _git(repo, "config", "user.name", "ASL Live Acceptance")
        (repo / "README.md").write_text(
            "Disposable repository for an ASL live acceptance check.\n",
            encoding="utf-8",
        )
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-q", "-m", "seed")
        yield Workspace(root=root, repo=repo, plan_dir=plan_dir, state_dir=state_dir)
    finally:
        if keep:
            print(f"  workspace retained: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


@dataclass
class TurnRun:
    """One live harness turn and the observations taken around it."""

    result: harnesses.HarnessResult
    seconds: float
    spawn_pid: int | None
    conversation_id: str | None
    native_interrupt: bool | None
    descendant_evidence: dict[str, Any] | None
    released: bool

    @property
    def final_message(self) -> str:
        """What the worker said, bounded, for the report's evidence."""
        return _excerpt(self.result.last_message)

    def summary(self) -> dict[str, Any]:
        return {
            "returncode": self.result.returncode,
            "aborted": self.result.aborted,
            "timed_out": self.result.timed_out,
            "cancel_stage": self.result.cancel_stage,
            "transport": self.result.transport,
            "conversation_id": self.conversation_id,
            "seconds": round(self.seconds, 3),
        }


class _LiveTurn:
    """Drives one real harness turn with a watcher thread alongside it.

    The watcher exists because every interesting property here is about timing:
    a turn has to be interrupted *while* it is doing something, which means
    something outside the turn has to notice that the work started. It watches
    for a marker file the prompt tells the worker to create as part of the
    long-running command itself, so "the tool is running" is observed rather
    than assumed from elapsed time.
    """

    def __init__(self, harness: HarnessName, workspace: Workspace) -> None:
        self.harness = harness
        self.workspace = workspace
        self.abort = harnesses.AbortSignal()
        self.spawned = threading.Event()
        self.pid: int | None = None
        self.transport: harnesses.HarnessTransport | None = None
        self.conversation_id: str | None = None
        self.native_interrupt: bool | None = None
        self.descendant_evidence: dict[str, Any] | None = None

    def _on_spawn(self, pid: int) -> None:
        self.pid = pid
        self.spawned.set()

    def _on_transport(self, transport: harnesses.HarnessTransport | None) -> None:
        self.transport = transport

    def _on_conversation(self, native_id: str) -> None:
        self.conversation_id = native_id

    def wait_for_file(self, name: str, timeout: float) -> bool:
        """Poll for a marker the worker creates, bounded by ``timeout``."""
        target = self.workspace.repo / name
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if target.exists():
                return True
            time.sleep(TOOL_START_POLL_SECONDS)
        return target.exists()

    def collect_evidence(self) -> dict[str, Any]:
        """Record the worker's descendants while it is still alive.

        Taken before any kill on purpose: killing the group reparents an
        escapee to pid 1, after which no descendant walk can reach it, so the
        walk that makes :func:`process_tree.confirm_termination` meaningful has
        to happen first.
        """
        pid = self.pid
        evidence: dict[str, Any] = (
            {"collected": False, "root_pid": None, "entries": []}
            if pid is None
            else process_tree.collect_descendant_evidence(pid)
        )
        self.descendant_evidence = evidence
        return evidence

    def request_native_interrupt(self) -> bool:
        """Ask the harness's own protocol to stop the work it has in flight."""
        transport = self.transport
        if transport is None:
            self.native_interrupt = False
            return False
        try:
            answered = bool(transport.interrupt())
        except Exception:  # noqa: BLE001 - a refused interrupt is an observation
            answered = False
        self.native_interrupt = answered
        return answered

    def run(
        self,
        prompt: str,
        *,
        watch: Callable[[_LiveTurn], None] | None,
        resume_conversation_id: str | None = None,
        timeout: int = TURN_TIMEOUT_SECONDS,
    ) -> TurnRun:
        """Run one turn. ``watch`` has no default deliberately.

        The first live run of this module failed three scenarios because a
        watcher was defined and never passed: the turn simply ran to its timeout
        and every interruption row reported "not stopped". Making the parameter
        required means a caller has to say ``watch=None`` to mean it.
        """
        adapter = harnesses.get_adapter(self.harness)
        # Outside the repository on purpose: the last-message sink is runner
        # bookkeeping, and a file appearing in the worktree would muddy the
        # "existing edits remain" row it sits next to.
        last_message_path = self.workspace.root / f"last-message-{uuid.uuid4().hex}.txt"
        watcher: threading.Thread | None = None
        if watch is not None:

            def observe() -> None:
                try:
                    watch(self)
                except Exception as error:  # noqa: BLE001 - never wedge the turn
                    print(f"  watcher failed: {type(error).__name__}: {error}")

            watcher = threading.Thread(
                target=observe, name="live-acceptance-watch", daemon=True
            )
        started = time.monotonic()
        if watcher is not None:
            watcher.start()
        try:
            result = adapter.run(
                cwd=self.workspace.repo,
                plan_dir=self.workspace.plan_dir,
                prompt=prompt,
                unsafe_bypass=True,
                last_message_path=last_message_path,
                timeout=timeout,
                abort=self.abort,
                on_spawn=self._on_spawn,
                on_transport=self._on_transport,
                on_conversation=self._on_conversation,
                resume_conversation_id=resume_conversation_id,
            )
        finally:
            if watcher is not None:
                watcher.join(timeout=5.0)
        seconds = time.monotonic() - started
        released = (
            True
            if not self.abort.armed
            else self.abort.wait_completed(TERMINATION_WAIT_SECONDS)
        )
        return TurnRun(
            result=result,
            seconds=seconds,
            spawn_pid=self.pid,
            conversation_id=self.conversation_id or result.conversation_id,
            native_interrupt=self.native_interrupt,
            descendant_evidence=self.descendant_evidence,
            released=bool(released),
        )
