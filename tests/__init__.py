"""Tests for repository-local utilities."""

import os

from agent_session_loop import cli_trace

# Every `cli.main` call in the suite is a real traced invocation. Without this
# the suite would append thousands of records to the developer's live state
# root and skew `asl metrics`. Trace tests re-enable tracing explicitly inside a
# temporary state root.
os.environ[cli_trace.TRACE_ENV_VAR] = "0"
