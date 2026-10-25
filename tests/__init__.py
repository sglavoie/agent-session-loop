"""Tests for repository-local utilities."""

import os

#: ``agent_session_loop.cli_trace.TRACE_ENV_VAR``, spelled out rather than
#: imported. The portable lane runs ``tests.test_workbench_portable`` under the
#: Pi's interpreter, which cannot parse the Mac worker runtime at all, so
#: importing it here would make every portable test fail with a SyntaxError
#: from a module none of them use. The duplication is held to one value by
#: ``tests/test_workbench_lifecycle.py``, which imports both and compares them.
TRACE_ENV_VAR = "ASL_CLI_TRACE"

# Every `cli.main` call in the suite is a real traced invocation. Without this
# the suite would append thousands of records to the developer's live state
# root and skew `asl metrics`. Trace tests re-enable tracing explicitly inside a
# temporary state root.
os.environ[TRACE_ENV_VAR] = "0"

#: Placeholder ``codex``, ``claude`` and ``copilot`` executables, put first on
#: PATH. Much of the suite mocks the harness call but still goes through the
#: real "is it installed" check (``shutil.which``), so without these it passes
#: only on a machine with every harness installed and fails -- or waits out
#: its timeouts -- on one without, such as CI. First, so a developer's installed
#: harness is never what a test resolves or runs; a test's own fake still wins
#: because it is put in front later, and a test that swaps PATH out to simulate
#: a missing harness is unaffected.
HARNESS_STUBS = os.path.join(os.path.dirname(__file__), "fixtures", "harness_stubs")
os.environ["PATH"] = os.pathsep.join(
    entry for entry in (HARNESS_STUBS, os.environ.get("PATH", "")) if entry
)
