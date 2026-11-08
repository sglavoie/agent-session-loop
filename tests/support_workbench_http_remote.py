"""Workbench HTTP remote provider.

Cases are grouped by responsibility in sibling modules.
"""

import unittest

from . import (
    workbench_http_remote_catalog,
    workbench_http_remote_mutations,
    workbench_http_remote_reads,
)


class RemoteProviderTests(
    workbench_http_remote_catalog.RemoteProviderTests,
    workbench_http_remote_reads.RemoteProviderTests,
    workbench_http_remote_mutations.RemoteProviderTests,
):
    """Preserve the established test IDs."""


if __name__ == "__main__":
    unittest.main()
