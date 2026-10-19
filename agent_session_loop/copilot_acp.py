# mypy: ignore-errors
# ruff: noqa
"""Public ACP session facade assembled from focused protocol components."""

from . import copilot_acp_types as _types
from . import copilot_acp_utils as _utils
from .copilot_acp_types import (
    AGENT_CAPABILITY_LOAD_SESSION,
    CLIENT_NAME,
    CLIENT_TITLE,
    DECISION_ALLOWED,
    DECISION_CANCELLED,
    DECISION_REJECTED,
    PERMISSION_ALLOW_ALWAYS,
    PERMISSION_ALLOW_ONCE,
    PERMISSION_REJECT_ALWAYS,
    PERMISSION_REJECT_ONCE,
    PROTOCOL_VERSION,
    REQUEST_TIMEOUT_SECONDS,
    SHUTDOWN_GRACE_SECONDS,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    STOP_REASON_MAX_TOKENS,
    STOP_REASON_MAX_TURN_REQUESTS,
    STOP_REASON_REFUSAL,
    TRANSPORT_ACP,
    AcpError,
    PermissionDecision,
    PermissionPolicy,
    RestoreRefused,
)
from .copilot_acp_protocol import _AcpProtocolMixin
from .copilot_acp_transport import _AcpTransportMixin
from .copilot_acp_turn import _AcpTurnMixin

globals().update(
    {
        name: value
        for module in (_types, _utils)
        for name, value in vars(module).items()
        if not name.startswith("__")
    }
)


class CopilotAcpSession(_AcpProtocolMixin, _AcpTurnMixin, _AcpTransportMixin):
    """One ACP conversation, composed from protocol, turn, and transport layers."""
