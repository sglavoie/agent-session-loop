# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
"""Parallel shard driver for the agent_session_loop test suite.

``just test-fast`` runs the unit/schema/parser modules; ``just test-all`` runs
every test module plus the standalone helper-script suites, one isolated
subprocess per shard. Shards are ordered longest-first so the slowest module
starts immediately and bounds wall-clock time.

``just test-portable`` runs the subset that needs nothing but the Python
interpreter, which is what a non-macOS CI lane can honestly gate on.

Usage:
    python3 -m tests.run_suite --fast
    python3 -m tests.run_suite --portable
    python3 -m tests.run_suite --all [--jobs N]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any
