from .support_base import *

_TREE_GRANDCHILD_SOURCE = (
    "import os, pathlib, sys, time\n"
    "name, root = sys.argv[1], pathlib.Path(sys.argv[2])\n"
    "pending = root / 'pending' / (name + '-grandchild')\n"
    "pending.write_text(str(os.getpid()))\n"
    "os.replace(pending, root / 'grandchildren' / name)\n"
    "parent = os.getppid()\n"
    "while os.getppid() == parent:\n"
    "    time.sleep(0.01)\n"
    "(root / 'late' / name).write_text('orphaned')\n"
    "time.sleep(60)\n"
)

#: The command a check runs: spawn a grandchild, wait until it is live, publish
#: this process's PID under `started/`, then outlive any termination window.
#: A fourth argv entry of `detached` makes the grandchild call `setsid`, which
#: takes it out of the check's process group and so out of reach of `killpg`.
_TREE_PARENT_SOURCE = (
    "import os, pathlib, subprocess, sys, time\n"
    "name, root, child = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]\n"
    "detached = len(sys.argv) > 4 and sys.argv[4] == 'detached'\n"
    "subprocess.Popen(\n"
    "    [sys.executable, child, name, str(root)], start_new_session=detached\n"
    ")\n"
    "while not (root / 'grandchildren' / name).exists():\n"
    "    time.sleep(0.01)\n"
    "pending = root / 'pending' / name\n"
    "pending.write_text(str(os.getpid()))\n"
    "os.replace(pending, root / 'started' / name)\n"
    "time.sleep(60)\n"
)


def write_process_tree_scripts(root: Path) -> tuple[Path, Path]:
    """Lay out the marker directories and both tree scripts under ``root``.

    A ``started/<name>`` marker is written only once that check's grandchild is
    live, so waiting for it is enough to know the whole tree is running.
    """
    for name in ("pending", "started", "grandchildren", "late"):
        (root / name).mkdir(parents=True, exist_ok=True)
    parent = root / "spawn_tree.py"
    parent.write_text(_TREE_PARENT_SOURCE, encoding="utf-8")
    grandchild = root / "tree_grandchild.py"
    grandchild.write_text(_TREE_GRANDCHILD_SOURCE, encoding="utf-8")
    return parent, grandchild


def wait_for_process_exit(pid: int, *, timeout: float = 15.0) -> bool:
    """Report whether ``pid`` is still alive after waiting up to ``timeout``.

    A killed process is reaped by its parent (or, once orphaned, by init)
    shortly after the signal lands, so this polls rather than reading the PID
    table once. ``True`` means the process outlived the wait.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        if time.monotonic() > deadline:
            return True
        time.sleep(0.02)


def assert_marked_pids_exited(
    marker_dir: Path, *, label: str, timeout: float = 15.0
) -> None:
    """Assert every PID published under ``marker_dir`` is gone."""
    for marker in sorted(marker_dir.iterdir()):
        pid = int(marker.read_text(encoding="utf-8"))
        if wait_for_process_exit(pid, timeout=timeout):
            raise AssertionError(
                f"{label} for {marker.name} (pid {pid}) is still alive"
            )


def assert_marked_pids_alive(marker_dir: Path, *, label: str) -> None:
    """Assert every PID published under ``marker_dir`` is still running.

    No polling: the tree scripts sleep well past any assertion window, so a
    process that is gone here was terminated rather than merely slow.
    """
    for marker in sorted(marker_dir.iterdir()):
        pid = int(marker.read_text(encoding="utf-8"))
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            raise AssertionError(
                f"{label} for {marker.name} (pid {pid}) is gone"
            ) from None


def kill_marked_pids(marker_dir: Path) -> None:
    """Kill every PID published under ``marker_dir``, ignoring the dead ones.

    Cleanup for tests that deliberately let a detached descendant survive: it
    outlives the test process otherwise.
    """
    if not marker_dir.exists():
        return
    for marker in sorted(marker_dir.iterdir()):
        try:
            pid = int(marker.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)
