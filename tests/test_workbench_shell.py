"""The one browser shell, checked against both deployments at once.

Session 15's claim is that there is now a single front end -- one source, one
router, one API layer -- and that the laptop and the Pi serve it unchanged. A
test that started one provider could not fail that claim: every divergence
this consolidation is meant to remove lives in the gap between the two, and a
gap needs both sides to be visible at the same time.

So the fixture here (:func:`tests.workbench_support.start_shell_deployments`)
seeds one plan corpus and serves it twice -- through the real local server and
through the remote provider over a seeded observation store -- and every test
below asks the same question of both: *is this the same shell, over the same
plans?*

Three kinds of check:

* **Source.** One static root, no build step, no runtime dependency. Read off
  the filesystem, because "there is one front end" is a fact about the tree.
* **Parity.** Byte-identical assets, identical plan and run identities, and
  detail reads that carry the same material through two different wrappers.
* **Difference that is supposed to exist.** The Pi refuses a mutation it has
  not been asked for properly and records rather than runs the ones it takes.
  Those are not divergences to remove; they are the reason the remote mode is
  a separate provider, and a parity suite that did not pin them would read as
  though the two modes were interchangeable.

The Playwright harness in ``tests/workbench_browser`` drives the same two
servers through a real browser. This module is what runs everywhere, including
where no browser is installed.
"""

import unittest

from . import workbench_shell_parity, workbench_shell_source

ASL_TEST_LANE = "integration"


class ShellSourceTests(workbench_shell_source.ShellSourceTests):
    """Preserve the established test IDs."""


class ShellAccessibilityTests(workbench_shell_source.ShellAccessibilityTests):
    """Preserve the established test IDs."""


class ShellRequestSourceTests(workbench_shell_source.ShellRequestSourceTests):
    """Preserve the established test IDs."""


class ServedAssetParityTests(workbench_shell_parity.ServedAssetParityTests):
    """Preserve the established test IDs."""


class PlanIdentityParityTests(workbench_shell_parity.PlanIdentityParityTests):
    """Preserve the established test IDs."""


class DetailParityTests(workbench_shell_parity.DetailParityTests):
    """Preserve the established test IDs."""


class DeploymentDifferenceTests(workbench_shell_parity.DeploymentDifferenceTests):
    """Preserve the established test IDs."""


if __name__ == "__main__":
    unittest.main()
