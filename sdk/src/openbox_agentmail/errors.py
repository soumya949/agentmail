"""Error types. Everything derives from ``openbox_core`` errors so callers that
already handle the OpenBox family keep working unchanged."""

from __future__ import annotations

from typing import Any

from openbox_core.errors import (
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
    ContractError,
    GovernanceAPIError,
    GovernanceBlockedError,
    GovernanceHaltError,
    GuardrailsValidationError,
    OpenBoxAuthError,
    OpenBoxConfigError,
    OpenBoxError,
)

__all__ = [
    "OpenBoxError",
    "ContractError",
    "OpenBoxConfigError",
    "OpenBoxAuthError",
    "GovernanceAPIError",
    "GovernanceBlockedError",
    "GovernanceHaltError",
    "GuardrailsValidationError",
    "ApprovalExpiredError",
    "ApprovalRejectedError",
    "ApprovalTimeoutError",
    "AgentMailBlockedError",
    "AgentMailHaltedError",
    "UncataloguedActionError",
    "InboundFetchError",
]


class AgentMailBlockedError(GovernanceBlockedError):
    """OpenBox returned BLOCK for an AgentMail operation. AgentMail was not called."""

    def __init__(
        self,
        reason: str,
        *,
        activity_type: str = "",
        policy_id: str | None = None,
        governance_event_id: str | None = None,
        patch: Any = None,
        result: Any = None,
    ):
        super().__init__("block", reason, url=activity_type)
        self.activity_type = activity_type
        self.policy_id = policy_id
        self.governance_event_id = governance_event_id
        self.patch = patch
        self.result = result

    def patched_args(self, original_args: dict[str, Any]) -> dict[str, Any] | None:
        """If Core attached a patch directive, return the call args with
        ``patch.new_input`` merged over them. Retrying with these args is a
        NEW governed call — it is evaluated fresh, never auto-applied.
        Returns ``None`` when no usable patch is present."""
        if self.patch is None:
            return None
        new_input = getattr(self.patch, "new_input", None)
        if not isinstance(new_input, dict):
            return None
        return {**original_args, **new_input}


class AgentMailHaltedError(GovernanceHaltError):
    """OpenBox returned HALT. The whole governed session is stopped; every
    further call short-circuits locally until a new session is started."""

    def __init__(self, reason: str, *, activity_type: str = "", result: Any = None):
        super().__init__(reason)
        self.reason = reason
        self.activity_type = activity_type
        self.result = result


class UncataloguedActionError(OpenBoxConfigError):
    """An AgentMail method has no entry in the action catalogue. Refused rather
    than passed through ungoverned."""


class InboundFetchError(OpenBoxError):
    """The authoritative copy of an inbound message could not be fetched from
    AgentMail inside the ``receive_message`` activity. Transient: the message
    was NOT delivered to the handler (fail closed), and transports retry it
    (webhook: 503 so AgentMail redelivers; websocket: bounded local retry)."""

    def __init__(self, message: str, *, inbox_id: str | None = None, message_id: str | None = None):
        super().__init__(message)
        self.inbox_id = inbox_id
        self.message_id = message_id
