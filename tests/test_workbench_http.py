"""Test entry point for focused test_workbench_http test groups."""

from __future__ import annotations

import unittest

from .support_workbench_http_remote import (
    RemoteProviderTests,
)
from .support_workbench_http_requests import (
    AuthenticatorTests,
    CallTests,
    ContainerAccessTests,
    StaticTests,
)
from .support_workbench_http_routing import (
    EnvelopeTests,
    PipelineTests,
    PolicyTests,
    RouteTableTests,
)

ASL_TEST_PORTABLE = True

for _test_case in (
    EnvelopeTests,
    RouteTableTests,
    PipelineTests,
    PolicyTests,
    CallTests,
    AuthenticatorTests,
    ContainerAccessTests,
    StaticTests,
    RemoteProviderTests,
):
    _test_case.__module__ = __name__
del _test_case

if __name__ == "__main__":
    unittest.main()
