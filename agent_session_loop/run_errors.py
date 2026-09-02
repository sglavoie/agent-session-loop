"""The run's fatal-error type, on its own so extractions may raise it.

An exception is the one thing a boundary seam cannot carry. A parameter, a
``Protocol``, or a callback all describe something a caller *hands in*, but a
module that refuses a bad run has to raise a type its caller already catches,
and there is no seam shape for that: the class has to be importable by both
halves. Keeping :class:`RunError` in ``runner.py`` while the run's own
selection, validation, guard, and commit layers move out of it would make every
one of those modules import the runner, which the Boundary table forbids and
``tests/test_asl_typing_ratchet.py`` enforces.

So the class lives here, in a leaf that imports nothing from the package, and
``runner.py`` imports the name back. Three consequences are deliberate:

* ``runner.RunError`` keeps resolving, so every ``except runner.RunError`` and
  ``assertRaises(runner.RunError)`` in the tree is unchanged and the runner
  stays the public exception surface an operator reads about.
* ``from .run_errors import RunError`` binds a name without creating an
  ``X = <module>.Y`` assignment, so it is an import rather than the re-export
  alias the extraction program is retiring, and the shrink ratchet in
  ``tests/test_asl_runner_shrink.py`` reads it as one.
* The identity of the class is preserved rather than mirrored. There is exactly
  one ``RunError`` in the process, so a raise from an extracted module and a
  catch in the run loop still describe the same failure.

This module writes no durable record and holds no state.
"""

from __future__ import annotations


class RunError(Exception):
    """Raised when the run loop cannot continue safely."""
