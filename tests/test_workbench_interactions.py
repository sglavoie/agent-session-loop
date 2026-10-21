"""Every action the shared interface offers, and what it is allowed to claim.

Session 15 put one front end in front of both deployments and session 16 made
it read the same way on a laptop and on a phone. What was still missing is the
other half of the matrix: the *mutations*. Nine of them, across launch, reply,
the run controls, the redirect journal, pins, recovery and the archive -- and
each one is offered by a mode that may not carry it out, aimed at a run that
may have moved on, and answered by a service that may only have written it
down.

The failure this module exists to catch is a front end that quietly overclaims.
A ``202`` drawn as success is the worst version of it: the operator reads "done"
about work nobody has confirmed ran, and the whole reason the remote provider
hands down rather than executes is that nobody can confirm it yet. A disabled
button with no sentence under it is the same failure in a smaller way -- it
tells the operator that something is impossible without telling them which of
the three reasons applies.

So the checks here come in three kinds:

* **Contract.** Read off the source, because "every control in the markup is a
  control this application knows about" is a fact about the tree and is not
  observable from any single interaction.
* **Capability.** Asked of both live deployments: what each mode says it can
  do, and whether the sentence it hands the interface in *advance* is the same
  sentence it would have given on refusal. A greyed-out control whose tooltip
  disagrees with the refusal is a control explaining the wrong thing.
* **Identity and archive.** That a recorded mutation carries an id, that
  resubmitting it replays rather than duplicates, that the service publishes
  what it still holds, and that the destructive end of the archive refuses
  without its confirmation.

The Playwright harness in ``tests/workbench_browser/interactions.spec.js``
drives the same two servers through a real browser, at phone and desktop
widths, for the parts that are only true once a human clicks them: focus after
a confirm dialog, a receipt that survives a reload, a disabled control's reason
being readable without a hover. This module is what runs everywhere, including
where no browser is installed.
"""

import unittest

from .workbench_interactions_cases_capabilities import CapabilityContractTests
from .workbench_interactions_cases_contract import InteractionContractTests
from .workbench_interactions_cases_mutations import (
    ArchiveReviewTests,
    OperationIdentityTests,
)

__all__ = [name for name in globals() if name.endswith("Tests")]

if __name__ == "__main__":
    unittest.main()
