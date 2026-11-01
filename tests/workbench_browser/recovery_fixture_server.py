#!/usr/bin/env python3
"""One recovery corpus per browser test, served on two ephemeral ports.

The parent reads the ready record, then closes stdin to stop both deployments.
No test endpoint or fixture control route is added to the application.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tests.workbench_support_recovery import (  # noqa: E402
    RETAINED_OPERATION_ID,
    start_recovery_deployments,
)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        deployments = start_recovery_deployments(Path(tmp), sys.argv[1])
        try:
            print(
                json.dumps(
                    {
                        "local": deployments.local.base_url,
                        "remote": deployments.remote.base_url,
                        "plan_dir": str(deployments.corpus.plan_dir("alpha")),
                        "plan_key": deployments.corpus.plan_keys["alpha"],
                        "retained_operation_id": RETAINED_OPERATION_ID,
                    }
                ),
                flush=True,
            )
            sys.stdin.read()
        finally:
            deployments.shutdown()


if __name__ == "__main__":
    main()
