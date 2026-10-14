"""Static diagnosis of whether a command check is runnable and well scoped.

Nothing here executes the check: it asks whether the executable resolves, a
``python -m`` module imports, and the command is narrow enough for ASL session
verification.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .contracts import CommandCheck
from .verification_models import _resolve_under_root

CommandDiagnosticReason = Literal["executable-not-found", "module-not-importable"]
CommandScopeDiagnosticReason = Literal[
    "operator-health-check-all",
    "operator-health-unscoped",
]

_PYTHON_BASENAME_PATTERN = re.compile(r"python(\d+(\.\d+)?)?\Z")
_MODULE_PROBE_TIMEOUT_SECONDS = 15.0
_OPERATOR_HEALTH_SCOPED_SUBCOMMANDS = frozenset(("check-ts", "lint", "test"))
_OPERATOR_HEALTH_NON_SELECTOR_FLAGS = frozenset(("--failed-only", "--resume", "--help"))


@dataclass(frozen=True)
class CommandDiagnostic:
    """Why a completion-contract command check is not runnable in this env.

    ``reason`` is a stable enum the preflight layer maps to a severity; ``detail``
    is a human-readable one-liner. A ``None`` diagnostic (returned by
    :func:`diagnose_command`) means "no problem positively determined" — the
    helper is deliberately fail-open so environment quirks never manufacture a
    false finding.
    """

    reason: CommandDiagnosticReason
    detail: str


@dataclass(frozen=True)
class CommandScopeDiagnostic:
    """Why a completion-contract command is too broad for ASL verification."""

    reason: CommandScopeDiagnosticReason
    detail: str


def _has_path_separator(value: str) -> bool:
    if os.sep in value:
        return True
    return os.altsep is not None and os.altsep in value


def _is_operator_health_command(argv: tuple[str, ...]) -> bool:
    if len(argv) < 3:
        return False
    return Path(argv[0]).name == "operator" and argv[1] == "health"


def _has_operator_health_target_flag(argv_tail: tuple[str, ...]) -> bool:
    for arg in argv_tail:
        if not arg.startswith("--"):
            continue
        if arg in _OPERATOR_HEALTH_NON_SELECTOR_FLAGS:
            continue
        return True
    return False


def _is_module_invocation(argv: tuple[str, ...]) -> bool:
    """True when ``argv`` looks like ``[<python>, "-m", <module>, ...]``."""
    if len(argv) < 3 or argv[1] != "-m":
        return False
    basename = Path(argv[0]).name
    return _PYTHON_BASENAME_PATTERN.fullmatch(basename) is not None


def _is_bare_python_invocation(argv: tuple[str, ...]) -> bool:
    """True when ``argv[0]`` is the *unversioned* ``python`` interpreter.

    A completion-contract command that shells out to bare, unversioned
    ``python`` is not portable: ``python`` is frequently absent in environments
    that ship only a versioned ``python3``. The actionable remediation is to
    use ``python3`` — matching the ``session-expand`` skill guidance.

    A versioned ``python3``/``python3.11`` invocation is *not* flagged here,
    regardless of whether it uses ``-m``, ``-c``, or a script path — those are
    valid inline evidence checks. Module importability for the ``-m`` form is
    diagnosed separately by :func:`diagnose_command`.
    """
    if not argv:
        return False
    basename = Path(argv[0]).name
    if _PYTHON_BASENAME_PATTERN.fullmatch(basename) is None:
        return False
    # Only the unversioned ``python`` command is flagged; ``python3`` and
    # ``python3.11`` carry an explicit version and are treated as available.
    return basename == "python"


def _default_module_probe(interpreter: str, module: str, *, cwd: Path) -> bool | None:
    """Bounded, read-only importability probe.

    Returns ``True`` when the module is importable, ``False`` on an unambiguous
    "missing" signal (exit 3 from ``find_spec`` returning ``None``, or an
    ``ImportError``/``ModuleNotFoundError`` raised by ``find_spec`` when a parent
    package on a dotted module path is missing), and ``None`` for any other
    error, timeout, or non-decisive exit so callers can fail open. Models the
    subprocess degradation shape of ``verification_checks._run_command_check``.
    """
    source = (
        "import importlib.util, sys\n"
        "try:\n"
        f"    found = importlib.util.find_spec({module!r}) is not None\n"
        "except ImportError:\n"
        "    found = False\n"
        "except Exception:\n"
        "    sys.exit(2)\n"
        "sys.exit(0 if found else 3)"
    )
    try:
        completed = subprocess.run(
            [interpreter, "-c", source],
            cwd=str(cwd),
            timeout=_MODULE_PROBE_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired, FileNotFoundError, OSError:
        return None
    if completed.returncode == 0:
        return True
    if completed.returncode == 3:
        return False
    return None


def diagnose_command(
    check: CommandCheck,
    *,
    repo_root: Path | None,
    which: Callable[[str], str | None] = shutil.which,
    module_probe: Callable[..., bool | None] = _default_module_probe,
) -> CommandDiagnostic | None:
    """Diagnose whether a command check is runnable, without running it.

    Returns ``None`` unless a problem is positively determined:

    - ``executable-not-found`` when ``argv[0]`` has no path separator and does
      not resolve via ``which`` (a path-bearing ``argv[0]`` is skipped — it may
      be a repo-relative binary the plan builds).
    - ``module-not-importable`` when ``argv`` is ``[<python>, "-m", <module>, …]``,
      the interpreter resolves, and ``module_probe`` reports the module missing.

    The check is skipped entirely when ``repo_root`` is ``None`` (mirrors the
    preflight cwd guard). Every ambiguous or failing probe fails open.
    """
    if repo_root is None:
        return None
    argv = check.argv
    if not argv:
        return None

    executable = argv[0]
    has_separator = _has_path_separator(executable)
    resolved_interpreter: str | None
    if has_separator:
        resolved_interpreter = executable
    else:
        resolved_interpreter = which(executable)
        if resolved_interpreter is None:
            return CommandDiagnostic(
                reason="executable-not-found",
                detail=f"executable not found on PATH: {executable}",
            )

    if _is_module_invocation(argv) and resolved_interpreter is not None:
        module = argv[2]
        probe_cwd = (
            repo_root
            if check.cwd is None
            else _resolve_under_root(repo_root, check.cwd)
        )
        outcome = module_probe(resolved_interpreter, module, cwd=probe_cwd)
        if outcome is False:
            return CommandDiagnostic(
                reason="module-not-importable",
                detail=(f"module not importable by {executable}: {module}"),
            )

    return None


def diagnose_command_scope(
    check: CommandCheck,
) -> CommandScopeDiagnostic | None:
    """Diagnose whether a command is overly broad for ASL session verification.

    ASL session verification must stay focused on the packages actually touched
    by the session. Repository-wide ``./operator health`` commands are too broad
    because, when no package flags are supplied, the operator CLI fans out
    across every workspace package and may start shared services.
    """
    argv = check.argv
    if not _is_operator_health_command(argv):
        return None

    subcommand = argv[2]
    rendered = " ".join(argv)
    if subcommand == "check-all":
        return CommandScopeDiagnostic(
            reason="operator-health-check-all",
            detail=(
                "overly broad verification command: "
                f"{rendered}; `./operator health check-all` is forbidden in "
                "ASL session verification"
            ),
        )

    if (
        subcommand in _OPERATOR_HEALTH_SCOPED_SUBCOMMANDS
        and not _has_operator_health_target_flag(argv[3:])
    ):
        return CommandScopeDiagnostic(
            reason="operator-health-unscoped",
            detail=(
                "overly broad verification command: "
                f"{rendered}; add one or more package target flags such as "
                "`--core-oidc` so the ASL verification stays session-scoped"
            ),
        )

    return None
