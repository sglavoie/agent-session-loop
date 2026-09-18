#!/usr/bin/env python3
"""Development-only fixture servers for the Playwright shell tests.

Nothing here ships anywhere. It starts the *two* deployments of the one front
end at once -- the laptop's ``LocalProvider`` and the Pi's ``RemoteProvider``
-- over a single seeded plan corpus, so a browser can be pointed at either and
the difference it sees is a difference between the deployments rather than
between two fixtures that were written separately.

The seeding lives in ``tests/workbench_support.py`` and is shared with
``tests/test_workbench_shell.py``. That sharing is the point: a scenario that
passes in the Python suite is not passing because that runner seeded something
this one did not.

Both servers are bound before this prints, so Playwright's ``webServer`` probe
against the local port is also proof the remote one is up.

Run it directly (``python3 tests/workbench_browser/fixture_server.py``) or let
Playwright start it.
"""

from __future__ import annotations

import json
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"
for entry in (str(REPO_ROOT), str(TESTS_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

# Deliberately after the path setup above: this file is executed as a script by
# Playwright, not collected by the test runner, so neither the packages nor the
# shared test helpers are importable until those two directories are on the
# path.
from workbench_support import start_shell_deployments  # noqa: E402

MANIFEST = json.loads(
    (Path(__file__).resolve().parent / "scenarios.json").read_text(encoding="utf-8")
)
HOST: str = str(MANIFEST["host"])
TOKEN: str = str(MANIFEST["token"])
LOCAL_PORT = int(MANIFEST["deployments"]["local"]["port"])
REMOTE_PORT = int(MANIFEST["deployments"]["remote"]["port"])


def main() -> int:
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        deployments = start_shell_deployments(
            root,
            host=HOST,
            token=TOKEN,
            local_port=LOCAL_PORT,
            remote_port=REMOTE_PORT,
        )
        stack.callback(deployments.shutdown)
        print(
            "workbench shell fixtures listening on "
            f"{deployments.local.base_url} (local) and "
            f"{deployments.remote.base_url} (remote)",
            flush=True,
        )
        try:
            for deployment in deployments.both:
                deployment.thread.join()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
