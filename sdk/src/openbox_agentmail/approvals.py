"""Draft-mode approvals (ARCHITECTURE.md §6).

``approval_mode="draft"``: when a send-like action needs approval the SDK
creates an AgentMail *draft* (from the guardrail-redacted arguments) and
returns a ``PendingApproval`` instead of blocking. The draft is sent only
after the dashboard approves; it is deleted on rejection/expiry.

``PendingApproval.wait()`` / ``aresolve()`` drive the decision. Records live in
an ``ApprovalStore`` (in-memory default; inject Redis/DB for multi-replica) so
a resolver can resume pending approvals after a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from openbox_core.contracts import events
from openbox_core.errors import (
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
    GovernanceAPIError,
)

from .catalog import ActionSpec, lookup
from .errors import AgentMailBlockedError

__all__ = [
    "PendingApproval",
    "DraftResolution",
    "ApprovalStore",
    "MemoryApprovalStore",
    "draft_args_from_send",
]

logger = logging.getLogger(__name__)


class ApprovalStore(Protocol):
    """Persistence for ``PendingApproval`` records. The default
    ``MemoryApprovalStore`` is per-process; production deployments should
    inject a shared store so resolvers on any replica can finish approvals."""

    def put(self, record: PendingApproval) -> None: ...
    def get(self, activity_id: str) -> PendingApproval | None: ...
    def remove(self, activity_id: str) -> None: ...
    def pending(self) -> list[PendingApproval]: ...


class MemoryApprovalStore:
    def __init__(self) -> None:
        self._records: dict[str, PendingApproval] = {}

    def put(self, record: PendingApproval) -> None:
        self._records[record.activity_id] = record

    def get(self, activity_id: str) -> PendingApproval | None:
        return self._records.get(activity_id)

    def remove(self, activity_id: str) -> None:
        self._records.pop(activity_id, None)

    def pending(self) -> list[PendingApproval]:
        return list(self._records.values())


@dataclass
class DraftResolution:
    """Outcome of resolving a ``PendingApproval``."""

    status: str  # "sent" | "rejected" | "expired" | "failed"
    draft_id: str | None
    activity_id: str
    result: Any = None
    error: str | None = None


@dataclass
class PendingApproval:
    """Handle returned by a governed send under ``approval_mode="draft"``.

    ``wait()`` polls OpenBox for the dashboard decision, sends the draft when
    approved and deletes it when rejected/expired. ``aresolve()`` is the async
    twin. The record is serialisable (drop ``_resolve``/``_aresolve``) so a
    store can persist it across restarts.
    """

    approval_id: str | None
    activity_id: str  # id of the ORIGINAL send activity — reused as idempotency key
    workflow_id: str
    run_id: str
    activity_type: str
    inbox_id: str | None
    draft_id: str | None
    created_draft: bool  # False for send_draft: the caller's draft is never deleted
    reason: str | None = None
    policy_id: str | None = None
    _resolve: Callable[[PendingApproval], DraftResolution] | None = field(
        default=None, repr=False, compare=False
    )
    _aresolve: Callable[[PendingApproval], Any] | None = field(
        default=None, repr=False, compare=False
    )

    def wait(self) -> DraftResolution:
        if self._resolve is None:
            raise GovernanceAPIError("This PendingApproval has no resolver attached")
        return self._resolve(self)

    async def aresolve(self) -> DraftResolution:
        if self._aresolve is None:
            raise GovernanceAPIError("This PendingApproval has no async resolver attached")
        return await self._aresolve(self)


def draft_args_from_send(args: Mapping[str, Any]) -> dict[str, Any]:
    """Map send/reply/forward arguments onto ``inboxes.drafts.create`` kwargs.

    ``thread_id``/``message_id`` are preserved on the PendingApproval record
    for dashboard context; the draft itself carries recipients and content.
    """
    keys = (
        "inbox_id", "to", "cc", "bcc", "reply_to", "subject", "text", "html",
        "attachments", "labels", "send_at", "thread_id", "headers",
    )
    return {k: v for k, v in args.items() if k in keys and v is not None}


def attach_resolvers(pending: PendingApproval, governor: Any) -> PendingApproval:
    """Wire the resolver closures for a record (e.g. rehydrated from a store)."""
    pending._resolve = governor._resolve_draft  # noqa: SLF001
    pending._aresolve = governor._aresolve_draft  # noqa: SLF001
    return pending


class _DraftOps:
    """Nested governed calls the resolver uses: send / delete on drafts.

    ``_defer=False`` prevents draft-mode recursion — a draft send that itself
    needs approval falls back to wait-mode polling rather than nesting.
    """

    def __init__(self, governor: Any):
        self._governor = governor

    def send(self, pending: PendingApproval) -> Any:
        spec: ActionSpec = lookup(("drafts", "send"))
        client = self._governor._mail_client  # noqa: SLF001

        def execute(a: dict[str, Any]) -> Any:
            a.setdefault("idempotency_key", pending.activity_id)
            return client.inboxes.drafts.send(**a)

        return self._governor.run(
            spec,
            {"inbox_id": pending.inbox_id, "draft_id": pending.draft_id},
            execute,
            _defer=False,
        )

    async def asend(self, pending: PendingApproval) -> Any:
        spec: ActionSpec = lookup(("drafts", "send"))
        client = self._governor._mail_client  # noqa: SLF001

        async def execute(a: dict[str, Any]) -> Any:
            a.setdefault("idempotency_key", pending.activity_id)
            return await client.inboxes.drafts.send(**a)

        return await self._governor.arun(
            spec,
            {"inbox_id": pending.inbox_id, "draft_id": pending.draft_id},
            execute,
            _defer=False,
        )

    def delete(self, pending: PendingApproval) -> Any:
        spec: ActionSpec = lookup(("drafts", "delete"))
        client = self._governor._mail_client  # noqa: SLF001
        return self._governor.run(
            spec,
            {"inbox_id": pending.inbox_id, "draft_id": pending.draft_id},
            lambda a: client.inboxes.drafts.delete(**a),
            _defer=False,
        )

    async def adelete(self, pending: PendingApproval) -> Any:
        spec: ActionSpec = lookup(("drafts", "delete"))
        client = self._governor._mail_client  # noqa: SLF001

        async def execute(a: dict[str, Any]) -> Any:
            return await client.inboxes.drafts.delete(**a)

        return await self._governor.arun(
            spec,
            {"inbox_id": pending.inbox_id, "draft_id": pending.draft_id},
            execute,
            _defer=False,
        )


def emit_resume_signal(governor: Any, *, activity_id: str, decision: str, reason: str | None) -> None:
    """``SignalReceived(approval_resume)`` — telemetry marking that the agent
    resumed after a human decision (wait and draft modes)."""
    try:
        ev = events.signal_received(
            workflow_id=governor.workflow_id or "",
            run_id=governor.run_id or "",
            workflow_type=governor.settings.resolved_workflow_type(),
            signal_name="approval_resume",
            extra={
                **governor._base_extra(None),  # noqa: SLF001
                "activity_id": activity_id,
                "decision": decision,
                "reason": reason,
            },
        )
        from .governor import _tag

        governor.runtime.gate.evaluate(_tag(ev))
    except Exception as e:  # noqa: BLE001 — telemetry must never break the flow
        logger.warning("approval_resume signal not delivered: %s", e)


def decision_of(exc: BaseException | None, approval: Any = None) -> tuple[str, str | None]:
    """Normalise an approval outcome into (decision, reason)."""
    if exc is None:
        return "approved", getattr(approval, "reason", None)
    if isinstance(exc, ApprovalExpiredError):
        return "expired", str(exc)
    if isinstance(exc, ApprovalTimeoutError):
        return "timeout", str(exc)
    if isinstance(exc, ApprovalRejectedError):
        return "rejected", str(exc)
    return "failed", str(exc)


def finish_draft_sync(governor: Any, pending: PendingApproval) -> DraftResolution:
    """Poll the decision, emit ``approval_resume``, send or delete the draft."""
    ops = _DraftOps(governor)
    poller = governor._adapter._poller  # noqa: SLF001
    try:
        approval = poller.wait_for_decision(pending.workflow_id, pending.run_id, pending.activity_id)
        error: BaseException | None = None
        if not approval.allow_shaped:
            error = (
                ApprovalExpiredError(approval.reason or "Approval window expired")
                if approval.expired
                else ApprovalRejectedError(approval.reason or "Approval rejected")
            )
    except BaseException as e:  # noqa: BLE001
        approval, error = None, e

    decision, reason = decision_of(error, approval)
    emit_resume_signal(governor, activity_id=pending.activity_id, decision=decision, reason=reason)

    try:
        if error is None:
            result = ops.send(pending)
            return DraftResolution("sent", pending.draft_id, pending.activity_id, result=result)
        if isinstance(error, GovernanceAPIError):
            return DraftResolution("failed", pending.draft_id, pending.activity_id, error=str(error))
        if pending.created_draft and pending.draft_id:
            ops.delete(pending)
        status = {"expired": "expired", "timeout": "expired"}.get(decision, "rejected")
        return DraftResolution(status, pending.draft_id, pending.activity_id, error=reason)
    except AgentMailBlockedError as e:
        return DraftResolution("failed", pending.draft_id, pending.activity_id, error=str(e))
    except Exception as e:  # noqa: BLE001
        return DraftResolution("failed", pending.draft_id, pending.activity_id, error=repr(e))
    finally:
        governor._store_remove(pending.activity_id)  # noqa: SLF001


async def finish_draft_async(governor: Any, pending: PendingApproval) -> DraftResolution:
    ops = _DraftOps(governor)
    poller = governor._adapter._poller  # noqa: SLF001
    try:
        approval = await poller.await_decision(pending.workflow_id, pending.run_id, pending.activity_id)
        error: BaseException | None = None
        if not approval.allow_shaped:
            error = (
                ApprovalExpiredError(approval.reason or "Approval window expired")
                if approval.expired
                else ApprovalRejectedError(approval.reason or "Approval rejected")
            )
    except BaseException as e:  # noqa: BLE001
        approval, error = None, e

    decision, reason = decision_of(error, approval)
    emit_resume_signal(governor, activity_id=pending.activity_id, decision=decision, reason=reason)

    try:
        if error is None:
            result = await ops.asend(pending)
            return DraftResolution("sent", pending.draft_id, pending.activity_id, result=result)
        if isinstance(error, GovernanceAPIError):
            return DraftResolution("failed", pending.draft_id, pending.activity_id, error=str(error))
        if pending.created_draft and pending.draft_id:
            await ops.adelete(pending)
        status = {"expired": "expired", "timeout": "expired"}.get(decision, "rejected")
        return DraftResolution(status, pending.draft_id, pending.activity_id, error=reason)
    except AgentMailBlockedError as e:
        return DraftResolution("failed", pending.draft_id, pending.activity_id, error=str(e))
    except Exception as e:  # noqa: BLE001
        return DraftResolution("failed", pending.draft_id, pending.activity_id, error=repr(e))
    finally:
        governor._store_remove(pending.activity_id)  # noqa: SLF001
