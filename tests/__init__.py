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
