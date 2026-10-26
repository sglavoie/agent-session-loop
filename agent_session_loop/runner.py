"""The runner's remaining surface: two argv entry points and nothing else.

Nothing runs here. Every boundary this module once held has moved into a
package module of its own, and what is left is the pair of names the rest of
the tree still addresses at this spelling: ``run_from_args`` and
``doctor_from_args``, which ``cli.py`` reaches through ``handler_ref`` strings
rather than through an import. Both delegate in one line.

This is also where the extraction's alias convention ends. Earlier moves kept a
module-level ``X = <module>.Y`` re-export here for every symbol they relocated,
so a ``runner.X`` spelling that predated the move -- in a call site, in a skill,
in a ``mock.patch.object(runner, "X")`` target -- kept resolving. That
convention bought compatibility with the one thing this file cannot afford,
size: 226 re-exports is a runner that is nothing but a re-export table, and a
name reachable at two spellings is a name whose owner a reader has to go and
look up. The re-exports are retired; every caller names the module that owns
the behaviour.

``RunError`` is the single deliberate exception, and it is an import rather than
an alias. It is the run's fatal-error type, defined in
:mod:`agent_session_loop.run_errors`; importing it here keeps ``runner.RunError``
the *same class object*, so an ``except runner.RunError`` anywhere in the tree
catches what ``run_errors.RunError`` raises. An alias assignment would do the
same thing, but an exception class has no owner to repoint a raise site to --
the seam is the class identity itself.
"""

from __future__ import annotations

import argparse

from . import run_doctor, run_entry, run_loop

# Re-exported, not used here: `run_errors.py` owns the run's fatal-error type
# and this import is what keeps `runner.RunError` the same class object, so
# every `except runner.RunError` in the tree is unchanged.
from .run_errors import RunError  # noqa: F401


def run_from_args(
    args: argparse.Namespace,
    *,
    run_argv: list[str] | None = None,
) -> int:
    """Run one or more pending sessions from CLI arguments.

    The package's public entry point, reached by ``cli.py`` through the string
    ``handler_ref="runner:run_from_args"``, its only caller: the Workbench and
    the autopilot start a run as an ``asl run`` child process. The admission
    sequence and the resume overlay live in
    :mod:`agent_session_loop.run_entry`; this shim supplies the one collaborator
    that module may not import, the locked run in
    :mod:`agent_session_loop.run_loop`.
    """
    return run_entry.run_from_args(
        args, run_argv=run_argv, run_locked=run_loop.run_locked
    )


def doctor_from_args(args: argparse.Namespace) -> int:
    """Check local harness prerequisites from CLI arguments.

    Reached by ``cli.py`` through the string
    ``handler_ref="runner:doctor_from_args"``, so the name is a contract. The
    staged resolution and every refusal it prints live in
    :mod:`agent_session_loop.run_doctor`; this boundary keeps only the name.
    """
    return run_doctor.doctor_from_args(args)
