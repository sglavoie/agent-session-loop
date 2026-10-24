# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: F401, I001
"""The typing ratchet: repository-wide suppressions may only shrink.

The repository still hides nine mypy error codes globally, because clearing
them needs container and generic annotations across a 125-file untyped tree
rather than defect fixes. That baseline is a debt, and debt without a ratchet
grows: a new module lands untyped, a tenth code joins the list to keep the
gate green, and the extraction program's typed boundaries stop being a
boundary at all.

These tests are that ratchet. ``pyproject.toml`` carries *two*
``enable_error_code`` override tables, under two contracts:

* **Boundary** -- mypy codes *and* the linter below: annotated public
  signatures, parameterized containers, no ``type: ignore``, no import of the
  runner. That is extraction discipline, which is meaningful for a package
  boundary and meaningless for a test module or for a package module whose job
  is to drive the runner.
* **Checked** -- the same nine mypy codes and nothing else. Where a module
  lands when it is clean under mypy but cannot meet the extraction rules.

Neither table can name itself: mypy rejects an extra key in an overrides table
as an unrecognized option, so each is looked up by an anchor module only that
table may list (see ``TABLE_ANCHORS``).

Together the tests assert seven things a future session cannot quietly undo:

* the repository-wide ``disable_error_code`` list may only *shrink*, and no
  per-module override -- in either table -- may add a suppression of its own;
* both tables re-enable all nine codes, list only modules that exist, and stay
  sorted and disjoint;
* each table's module list may only *grow*, measured against its own floor set
  (``REQUIRED_RATCHETED_MODULES``, ``REQUIRED_CHECKED_MODULES``);
* every module in the package appears on exactly one of the two tables, so a
  module added tomorrow cannot sit under the global suppressions unnoticed --
  the floors only stop listed modules from leaving;
* every module on the **Boundary** table keeps its durable-boundary payloads
  typed under the linter rules above;
* a third, **Scripts** table lists every skill script under its bare module
  name and re-enables the six codes the scripts are clean under;
* the files that carry an inline ``# mypy: disable-error-code`` comment, and
  the codes each disables, may only *shrink*.

That last check is a linter, not a mypy run: it fails on the *shape* mypy
would have to trust, so a public entry point that takes a bare ``dict`` is
caught even though ``dict`` is a perfectly valid annotation. The bare-container
rule reads the whole annotation rather than its root, because ``dict | None``
and ``list[dict]`` restore the untyped payload just as surely. It applies to
the Boundary table only.
"""

from __future__ import annotations

import ast
import tomllib
import unittest
from pathlib import Path
from typing import Any


#: Not a shard of its own: shared helpers for the facade's sections.
ASL_TEST_SHARDS: tuple[str, ...] = ()
