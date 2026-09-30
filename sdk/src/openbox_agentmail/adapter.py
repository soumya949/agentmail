"""``FrameworkAdapter`` implementation: the only place verdicts become
AgentMail-native errors."""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from openbox_core.adapters.base import _approval_poll_ids
from openbox_core.approvals import ApprovalPoller
from openbox_core.contracts.results import EvaluationResult, Verdict, handle_patch
from openbox_core.errors import ApprovalExpiredError, ApprovalRejectedError

from .errors import AgentMailBlockedError, AgentMailHaltedError

if TYPE_CHECKING:
    from openbox_core.contracts.context import ActivityContext

__all__ = ["AgentMailAdapter", "raise_native"]


def raise_native(result: EvaluationResult, activity_type: str = "") -> NoReturn:
    if result.verdict is Verdict.HALT:
        raise AgentMailHaltedError(
            result.reason or "Halted by OpenBox policy", activity_type=activity_type, result=result
        )
    raise AgentMailBlockedError(
        result.reason or "Blocked by OpenBox policy",
        activity_type=activity_type,
        policy_id=result.policy_id,
        governance_event_id=result.governance_event_id,
        patch=handle_patch(result),
        result=result,
    )


class AgentMailAdapter:
    name = "agentmail"

    def __init__(self, approval_poller: ApprovalPoller | None = None):
        self._poller = approval_poller

    # ── approvals ────────────────────────────────────────────────────────

    async def handle_approval(
        self, result: EvaluationResult, context: ActivityContext | None = None
    ) -> None:
        self._require_poller(result)
        ids = _approval_poll_ids(result, context)
        self._finish(await self._poller.await_decision(*ids))  # type: ignore[union-attr]

    def handle_approval_sync(
        self, result: EvaluationResult, context: ActivityContext | None = None
    ) -> None:
        self._require_poller(result)
        ids = _approval_poll_ids(result, context)
        self._finish(self._poller.wait_for_decision(*ids))  # type: ignore[union-attr]

    def _require_poller(self, result: EvaluationResult) -> None:
        # Polls are keyed by (workflow_id, run_id, activity_id) — see
        # ``_approval_poll_ids`` — so ``approval_id`` is NOT needed to poll and
        # must never gate it: Core registers the approval and may omit the id
        # from the evaluate response, and refusing then turns a real pending
        # approval into an instant false rejection. Escalated CONSTRAIN writes
        # have no id either. Only a missing poller is unrecoverable.
        if self._poller is None:
            raise ApprovalRejectedError(
                "Approval required but HITL polling is disabled "
                "(hitl.enabled=False); failing safe (AgentMail not called)"
            )

    @staticmethod
    def _finish(approval) -> None:
        if approval.allow_shaped:
            return
        if approval.expired:
            raise ApprovalExpiredError(approval.reason or "Approval window expired")
        raise ApprovalRejectedError(approval.reason or "Approval rejected")

    # ── stop verdicts ────────────────────────────────────────────────────

    def raise_lifecycle_blocked(self, result: EvaluationResult) -> NoReturn:
        raise_native(result)

    def raise_hook_blocked(self, result: EvaluationResult) -> NoReturn:
        raise_native(result)

    def on_completed_hook_result(
        self, result: EvaluationResult, context: ActivityContext | None = None
    ) -> None:
        # The HTTP call already happened; the runtime records abort/halt flags
        # for future work. Nothing AgentMail-specific to undo.
        return None
