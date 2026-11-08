"""The service's own half of the link.

``tests/test_workbench_connector.py`` covers the Mac end and
``tests/test_workbench_fullstack_*.py`` runs both ends over a link it can break.
This module is about the object in between: what an upload does to the store,
what rides back down in its acknowledgement, and how a read or a control
settles when nothing ever carries it.

Every clock here is an argument, so each test asserts about a decision rather
than racing one. :class:`WaitingReadTests` is the exception and says why.

Cases are grouped by responsibility in sibling modules.
"""

import unittest

from . import (
    workbench_uplink_bodies,
    workbench_uplink_carriage,
    workbench_uplink_reads,
    workbench_uplink_uploads,
    workbench_uplink_waiting,
)

ASL_TEST_PORTABLE = True


class UploadTests(
    workbench_uplink_uploads.UploadTests,
):
    """Preserve the established test IDs."""


class CarriageTests(
    workbench_uplink_carriage.CarriageTests,
):
    """Preserve the established test IDs."""


class VolatileBodyTests(
    workbench_uplink_bodies.VolatileBodyTests,
):
    """Preserve the established test IDs."""


class ReadTests(
    workbench_uplink_reads.ReadTests,
):
    """Preserve the established test IDs."""


class WaitingReadTests(
    workbench_uplink_waiting.WaitingReadTests,
):
    """Preserve the established test IDs."""


if __name__ == "__main__":
    unittest.main()
