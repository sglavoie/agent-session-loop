"""Public contract and report API for the independent supervisor audit."""

from . import assurance_projection
from .supervisor_audit_agent_catalog import (
    EMPTY_AGENT_CATALOG,
    _agent_catalog_entry,
    _build_agent_catalog,
    read_agent_catalog,
    read_identity_catalog,
    widen_agent_catalog,
)
from .supervisor_audit_agent_contract import *
from .supervisor_audit_agent_models import *
from .supervisor_audit_agent_parse import *
from .supervisor_audit_agent_render import *
from .supervisor_audit_agent_reports import *
from .supervisor_audit_agent_resolution import *


def build_agent_catalog(catalog: assurance_projection.IdentityCatalog) -> AgentCatalog:
    """Build the bounded catalog using this compatibility module's limit."""
    return _build_agent_catalog(catalog, maximum_bytes=MAX_AGENT_CATALOG_BYTES)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
