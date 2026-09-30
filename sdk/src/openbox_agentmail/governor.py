"""``MailGovernor``: the single choke point every surface (REST proxy, toolkit,
MCP proxy, inbound relay) calls. Owns the session (Workflow) and runs the
evaluate -> enforce -> execute -> evaluate sequence for each AgentMail action.

Sequence per action (see ARCHITECTURE.md §5.1):

    halt check -> build activity_input (ContractError) -> WorkflowStarted (once)
    -> ActivityStarted (enforced) -> approval / redaction
    -> real AgentMail call -> ActivityCompleted (output guardrails)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from openbox_core.context import activity_scope
from openbox_core.contracts import events
from openbox_core.contracts.context import ActivityContext
from openbox_core.contracts.results import EvaluationResult, Verdict
from openbox_core.errors import (
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
    GovernanceAPIError,
    GuardrailsValidationError,
    OpenBoxConfigError,
)
from openbox_core.runtime import OpenBoxRuntime

from .adapter import AgentMailAdapter, raise_native
from .approvals import (
    ApprovalStore,
    MemoryApprovalStore,
    PendingApproval,
    attach_resolvers,
    draft_args_from_send,
    emit_resume_signal,
    finish_draft_async,
    finish_draft_sync,
)
from .approvals import (
    decision_of as _decision_of,
)
from .catalog import RECEIVE_MESSAGE, ActionClass, ActionSpec, lookup
from .config import EVENT_SOURCE, AgentMailSettings
from .constraints import ConstraintViolation, apply_constraints
from .contracts import (
    build_activity_input,
    build_activity_output,
    build_inbound_activity_input,
    build_inbound_signal,
    build_trigger_signal,
)
from .errors import AgentMailBlockedError, AgentMailHaltedError, InboundFetchError
from .redaction import redact_args, redact_result

__all__ = ["MailGovernor", "GovernedResult", "ReceivedMessage"]

logger = logging.getLogger(__name__)

Executor = Callable[[dict[str, Any]], Any]
AsyncExecutor = Callable[[dict[str, Any]], Awaitable[Any]]


class GovernedResult:
    """Wrapper returned when a completed-stage verdict flagged a write that
    already happened. ``value`` is the AgentMail response."""

    def __init__(self, value: Any, warning: EvaluationResult):
        self.value = value
        self.governance_warning = warning

    def __getattr__(self, name: str) -> Any:
        return getattr(self.value, name)


class ReceivedMessage:
    """Outcome of a governed ``agentmail.receive_message`` activity.

    ``message`` is the fetched AgentMail message as a dict, with guardrail
    redactions from both stages applied — this is what the handler must see.
    ``fields`` is the ``activity_input`` OpenBox evaluated. ``result`` is the
    final verdict (``ActivityCompleted`` when it was evaluated, else
    ``ActivityStarted``)."""

    def __init__(self, message: dict[str, Any], fields: dict[str, Any],
                 result: EvaluationResult, activity_id: str):
        self.message = message
        self.fields = fields
        self.result = result
        self.activity_id = activity_id


class MailGovernor:
    def __init__(
        self,
        runtime: OpenBoxRuntime,
        settings: AgentMailSettings,
        *,
        mail_client: Any = None,
        approval_store: ApprovalStore | None = None,
    ):
        if settings.approval_mode not in ("wait", "draft"):
            raise OpenBoxConfigError(f"unknown approval_mode {settings.approval_mode!r}")
        if settings.approval_mode == "draft" and mail_client is None:
            raise OpenBoxConfigError(
                "approval_mode='draft' requires an AgentMail client so pending sends "
                "can be staged as drafts"
            )
        self.runtime = runtime
        self.settings = settings
        self._mail_client = mail_client
        self._approval_store: ApprovalStore = (
            approval_store or settings.approval_store or MemoryApprovalStore()
        )
        self._lock = threading.Lock()
        self.workflow_id: str | None = None
        self.run_id: str | None = None
        self._halted = False
        self._closed_status: str | None = None

    # ── session (Workflow) ────────────────────────────────────────────────

    @property
    def halted(self) -> bool:
        return self._halted or self.runtime.context_store.halt_requested

    def _workflow_fields(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id or "",
            "run_id": self.run_id or "",
            "workflow_type": self.settings.resolved_workflow_type(),
        }

    def _base_extra(self, inbox_id: str | None) -> dict[str, Any]:
        import openbox_core

        from . import __version__

        extra: dict[str, Any] = {
            "agent_name": self.settings.agent_name,
            "surface": self.settings.surface,
            "agentmail_sdk_version": __version__,
            "openbox_sdk_version": openbox_core.__version__,
        }
        if self.settings.pod_id:
            extra["pod_id"] = self.settings.pod_id
        if inbox_id:
            extra["inbox_id"] = inbox_id
        return extra

    def _workflow_started_event(self) -> events.EventEnvelope:
        with self._lock:
            if self.workflow_id is not None:
                return None  # type: ignore[return-value]
            self.workflow_id = str(uuid.uuid4())
            self.run_id = str(uuid.uuid4())
            self._halted = False
        ev = events.workflow_started(**self._workflow_fields(), extra=self._base_extra(None))
        return _tag(ev)

    def ensure_session(self) -> None:
        ev = self._workflow_started_event()
        if ev is not None:
            self.runtime.evaluate_lifecycle(ev)

    async def aensure_session(self) -> None:
        ev = self._workflow_started_event()
        if ev is not None:
            await self.runtime.aevaluate_lifecycle(ev)

    def _closing_event(self, error: str | None) -> events.EventEnvelope | None:
        if self.workflow_id is None:
            return None
        status = "halted" if self.halted else ("failed" if error else "completed")
        fields = self._workflow_fields()
        extra = {**self._base_extra(None), "status": status}
        ev = (
            events.workflow_failed(**fields, error=error, extra=extra)
            if error and not self.halted
            else events.workflow_completed(**fields, extra=extra)
        )
        self.workflow_id = self.run_id = None
        return _tag(ev)

    def close_session(self, error: str | None = None) -> None:
        ev = self._closing_event(error)
        if ev is not None:
            _swallow(lambda: self.runtime.gate.evaluate(ev))

    async def aclose_session(self, error: str | None = None) -> None:
        ev = self._closing_event(error)
        if ev is not None:
            try:
                await self.runtime.gate.aevaluate(ev)
            except Exception as e:  # noqa: BLE001
                logger.warning("WorkflowCompleted not delivered: %s", e)

    def emit_handoff(self, target_agent_id: str, reason: str | None = None) -> dict[str, Any]:
        """Emit a multi-agent handoff event linking this governed agent's
        session to ``target_agent_id`` (e.g. when a host framework hands work
        to another governed agent)."""
        return self.runtime.client.emit_handoff(target_agent_id, reason)

    async def aemit_handoff(self, target_agent_id: str, reason: str | None = None) -> dict[str, Any]:
        return await self.runtime.client.aemit_handoff(target_agent_id, reason)

    # ── per-action ────────────────────────────────────────────────────────

    def _prepare(self, spec: ActionSpec, args: Mapping[str, Any]) -> dict[str, Any]:
        """Halt check + local contract validation. Runs BEFORE ensure_session so
        malformed calls never emit a WorkflowStarted."""
        if self.halted:
            raise AgentMailHaltedError(
                "Session halted by a previous OpenBox verdict", activity_type=spec.activity_type
            )
        return build_activity_input(
            spec, args, self.settings, self.runtime.config.privacy.max_body_size
        )

    def _context_and_started(
        self, spec: ActionSpec, activity_input: dict[str, Any], inbox_id: Any
    ) -> tuple[ActivityContext, events.EventEnvelope]:
        ctx = ActivityContext(
            workflow_id=self.workflow_id,
            run_id=self.run_id,
            workflow_type=self.settings.resolved_workflow_type(),
            task_queue=str(inbox_id) if inbox_id else None,
            activity_id=str(uuid.uuid4()),
            activity_type=spec.activity_type,
            activity_input=activity_input,
            agent_name=self.settings.agent_name,
            metadata=self._base_extra(str(inbox_id) if inbox_id else None),
        )
        started = events.activity_started(
            **self._workflow_fields(),
            task_queue=ctx.task_queue,
            activity_id=ctx.activity_id,  # type: ignore[arg-type]
            activity_type=spec.activity_type,
            activity_input=activity_input,
            extra=dict(ctx.metadata),
        )
        return ctx, _tag(started)

    def _completed_event(self, ctx: ActivityContext, spec: ActionSpec, result: Any = None, error: str | None = None) -> events.EventEnvelope:
        extra = dict(ctx.metadata)
        if error is None:
            extra["activity_output"] = build_activity_output(
                spec, result, self.settings, self.runtime.config.privacy.max_body_size
            )
        else:
            # Core rejects an ActivityCompleted carrying a top-level ``error``
            # field outright (HTTP 400 "invalid request body"), so the whole
            # event is lost and the failure is never recorded — verified live
            # against every shape, including a one-word error. Carry the
            # failure inside activity_output, which Core accepts. Revert to the
            # factory's ``error=`` once Core accepts it.
            extra["failed"] = True
            extra["activity_output"] = {
                "action": spec.action,
                "status": "failed",
                "error": error,
            }
        ev = events.activity_completed(
            **self._workflow_fields(),
            task_queue=ctx.task_queue,
            activity_id=ctx.activity_id,  # type: ignore[arg-type]
            activity_type=spec.activity_type,
            extra=extra,
        )
        return _tag(ev)

    def _after_started(
        self, spec: ActionSpec, r1: EvaluationResult, args: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Post-ActivityStarted checks shared by sync/async paths.

        Returns ``(exec_args, applied)`` — the (possibly redacted and
        constraint-rewritten) arguments for the real AgentMail call, and the
        descriptions of any constraints applied, for the activity record.

        Only REQUIRE_APPROVAL drives the approval flow. A CONSTRAIN that cannot
        be satisfied raises ``AgentMailBlockedError`` here rather than
        escalating, because Core registers no approval to escalate to.
        """
        self._check_fallback(spec, r1)
        applied: list[str] = []
        exec_args = redact_args(args, r1)
        if r1.verdict is Verdict.CONSTRAIN:
            if spec.action_class.is_write:
                # A CONSTRAIN we cannot apply must fail closed HERE. It must not
                # fall through to the approval poll: Core registers an approval
                # for REQUIRE_APPROVAL only, so polling on CONSTRAIN waits on an
                # approval that will never exist — with the default
                # hitl.max_wait_ms=None that hangs the caller forever (verified
                # live). A refusal the operator can read beats a silent hang.
                if not r1.constraints:
                    raise AgentMailBlockedError(
                        f"policy returned CONSTRAIN for {spec.activity_type} without any "
                        "constraints, so there is nothing to apply and the call cannot be "
                        "made compliant. Use REQUIRE APPROVAL or BLOCK instead",
                        activity_type=spec.activity_type,
                        policy_id=r1.policy_id,
                        governance_event_id=r1.governance_event_id,
                        result=r1,
                    )
                try:
                    exec_args, applied = apply_constraints(exec_args, r1.constraints)
                    logger.info("CONSTRAIN %s applied: %s", spec.activity_type, applied)
                except ConstraintViolation as e:
                    raise AgentMailBlockedError(
                        f"policy returned CONSTRAIN for {spec.activity_type} but it could not "
                        f"be satisfied: {e}",
                        activity_type=spec.activity_type,
                        policy_id=r1.policy_id,
                        governance_event_id=r1.governance_event_id,
                        result=r1,
                    ) from e
            else:
                logger.info("CONSTRAIN on read %s: constraints=%s", spec.activity_type, r1.constraints)
        return exec_args, applied

    @property
    def _adapter(self) -> AgentMailAdapter:
        return self.runtime.adapter  # type: ignore[return-value]

    # ── approvals ─────────────────────────────────────────────────────────

    def _hitl_skips(self, spec: ActionSpec) -> bool:
        """``hitl.skip_activity_types``: listed activities proceed without
        requesting approval (they are evaluated, just never held)."""
        hitl = self.runtime.config.hitl
        return bool(hitl.enabled and spec.activity_type in (hitl.skip_activity_types or ()))

    def _draft_eligible(self, spec: ActionSpec) -> bool:
        """Only SEND actions can be staged as drafts; other writes that need
        approval still poll (delete_draft as a draft makes no sense)."""
        return self.settings.approval_mode == "draft" and spec.action_class is ActionClass.SEND

    def _enforce_started(self, spec: ActionSpec, r1: EvaluationResult) -> None:
        """Mirrors ``OpenBoxRuntime._enforce_lifecycle``: stop verdicts and
        guardrail failures raise before any approval flow."""
        if r1.verdict.should_stop():
            if r1.verdict is Verdict.HALT:
                self._halted = True
                self.runtime.context_store.request_halt()
            self._adapter.raise_lifecycle_blocked(r1)
        if r1.guardrails and not r1.guardrails.validation_passed:
            raise GuardrailsValidationError(
                r1.guardrails.get_reason_strings() or ["Guardrails validation failed"]
            )

    def _wait_approval_sync(self, r1: EvaluationResult, ctx: ActivityContext) -> None:
        exc: BaseException | None = None
        try:
            self._adapter.handle_approval_sync(r1, ctx)
        except (ApprovalRejectedError, ApprovalExpiredError, ApprovalTimeoutError) as e:
            exc = e
            raise
        finally:
            decision, reason = _decision_of(exc)
            emit_resume_signal(self, activity_id=str(ctx.activity_id), decision=decision, reason=reason)

    async def _wait_approval_async(self, r1: EvaluationResult, ctx: ActivityContext) -> None:
        exc: BaseException | None = None
        try:
            await self._adapter.handle_approval(r1, ctx)
        except (ApprovalRejectedError, ApprovalExpiredError, ApprovalTimeoutError) as e:
            exc = e
            raise
        finally:
            decision, reason = _decision_of(exc)
            emit_resume_signal(self, activity_id=str(ctx.activity_id), decision=decision, reason=reason)

    def _new_pending(self, spec: ActionSpec, r1: EvaluationResult, ctx: ActivityContext,
                     draft_id: str | None, created: bool, inbox_id: Any) -> PendingApproval:
        pending = PendingApproval(
            approval_id=r1.approval_id,
            activity_id=str(ctx.activity_id),
            workflow_id=self.workflow_id or "",
            run_id=self.run_id or "",
            activity_type=spec.activity_type,
            inbox_id=str(inbox_id) if inbox_id else None,
            draft_id=draft_id,
            created_draft=created,
            reason=r1.reason,
            policy_id=r1.policy_id,
        )
        attach_resolvers(pending, self)
        self._approval_store.put(pending)
        return pending

    def _draft_create_args(self, spec: ActionSpec, args: dict[str, Any], ctx: ActivityContext) -> dict[str, Any]:
        draft_args = draft_args_from_send(args)
        if spec.method in ("reply", "reply_all"):
            draft_args["in_reply_to"] = args.get("message_id")
            if spec.method == "reply_all":
                draft_args["reply_all"] = True
        elif spec.method == "forward":
            draft_args["forward_of"] = args.get("message_id")
        # client_id makes the draft creation itself idempotent across retries
        draft_args["client_id"] = str(ctx.activity_id)
        return draft_args

    def _defer_to_draft(self, spec: ActionSpec, args: dict[str, Any], r1: EvaluationResult, ctx: ActivityContext) -> PendingApproval:
        if spec.resource == "drafts" and spec.method == "send":
            # send_draft: the draft already exists; only its send is held.
            return self._new_pending(spec, r1, ctx, args.get("draft_id"), False, args.get("inbox_id"))
        create = lookup(("drafts", "create"))
        draft = self.run(
            create,
            self._draft_create_args(spec, args, ctx),
            lambda a: self._mail_client.inboxes.drafts.create(**a),
            _defer=False,
        )
        return self._new_pending(spec, r1, ctx, _draft_id_of(draft), True, args.get("inbox_id"))

    async def _adefer_to_draft(self, spec: ActionSpec, args: dict[str, Any], r1: EvaluationResult, ctx: ActivityContext) -> PendingApproval:
        if spec.resource == "drafts" and spec.method == "send":
            return self._new_pending(spec, r1, ctx, args.get("draft_id"), False, args.get("inbox_id"))
        create = lookup(("drafts", "create"))

        async def make(a: dict[str, Any]) -> Any:
            return await self._mail_client.inboxes.drafts.create(**a)

        draft = await self.arun(create, self._draft_create_args(spec, args, ctx), make, _defer=False)
        return self._new_pending(spec, r1, ctx, _draft_id_of(draft), True, args.get("inbox_id"))

    # Resolver plumbing used by ``approvals.finish_draft_*``.
    def _resolve_draft(self, pending: PendingApproval):
        return finish_draft_sync(self, pending)

    async def _aresolve_draft(self, pending: PendingApproval):
        return await finish_draft_async(self, pending)

    def _store_remove(self, activity_id: str) -> None:
        try:
            self._approval_store.remove(activity_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("approval store remove failed: %s", e)

    def pending_approvals(self) -> list[PendingApproval]:
        """All unresolved approvals, with resolvers re-attached."""
        return [attach_resolvers(p, self) for p in self._approval_store.pending()]

    def rehydrate(self, pending: PendingApproval) -> PendingApproval:
        """Re-register a record restored from an external store."""
        attach_resolvers(pending, self)
        self._approval_store.put(pending)
        return pending

    def _check_fallback(self, spec: ActionSpec, result: EvaluationResult) -> None:
        if not result.fallback_used:
            return
        if spec.action_class.is_write and not self.settings.allow_fallback_for_writes:
            raise GovernanceAPIError(
                f"OpenBox unreachable and fail_open fallback refused for write action {spec.activity_type}"
            )
        if spec.action_class.is_read and self.settings.read_on_api_error == "fail_closed":
            raise GovernanceAPIError(f"OpenBox unreachable (fail_closed) for {spec.activity_type}")

    def _after_completed(self, spec: ActionSpec, r2: EvaluationResult | None, result: Any) -> Any:
        if r2 is None:
            return result
        if r2.verdict.should_stop():
            if r2.verdict is Verdict.HALT:
                self._halted = True
                self.runtime.context_store.request_halt()
            if spec.action_class.is_read:
                raise_native(r2, spec.activity_type)
            logger.warning("Completed-stage %s on %s: %s", r2.verdict.value, spec.activity_type, r2.reason)
            return GovernedResult(result, r2)
        if r2.guardrails and not r2.guardrails.validation_passed:
            if spec.action_class.is_read:
                raise GuardrailsValidationError(r2.guardrails.get_reason_strings())
            return GovernedResult(result, r2)
        return redact_result(result, r2)

    def _completed_error_policy(self, spec: ActionSpec, e: Exception) -> None:
        # Fail-closed on the completed stage only matters for reads: the agent
        # must not see unscreened content. Writes already happened.
        if spec.action_class.is_read and isinstance(e, GovernanceAPIError):
            raise e
        logger.warning("ActivityCompleted evaluation failed for %s: %s", spec.activity_type, e)

    # ── public API ───────────────────────────────────────────────────────

    def run(self, spec: ActionSpec, args: Mapping[str, Any], executor: Executor, *, _defer: bool = True) -> Any:
        activity_input = self._prepare(spec, args)
        self.ensure_session()
        ctx, started = self._context_and_started(spec, activity_input, args.get("inbox_id"))
        with activity_scope(ctx, store=self.runtime.context_store):
            r1 = self.runtime.evaluate_lifecycle(started)  # sync path returns REQUIRE_APPROVAL undriven
            exec_args, applied = self._after_started(spec, r1, dict(args))
            if applied:
                ctx.metadata["applied_constraints"] = applied
            if r1.verdict is Verdict.REQUIRE_APPROVAL:
                if _defer and self._draft_eligible(spec):
                    return self._defer_to_draft(spec, exec_args, r1, ctx)
                if not self._hitl_skips(spec):
                    self._wait_approval_sync(r1, ctx)
            try:
                result = executor(exec_args)  # type: ignore[arg-type]
            except Exception as e:
                err = _error_text(e)
                _swallow(lambda: self.runtime.gate.evaluate(self._completed_event(ctx, spec, error=err)))
                raise
            r2: EvaluationResult | None = None
            try:
                r2 = self.runtime.gate.evaluate(self._completed_event(ctx, spec, result))
            except Exception as e:  # noqa: BLE001
                self._completed_error_policy(spec, e)
            return self._after_completed(spec, r2, result)

    async def arun(self, spec: ActionSpec, args: Mapping[str, Any], executor: AsyncExecutor, *, _defer: bool = True) -> Any:
        activity_input = self._prepare(spec, args)
        await self.aensure_session()
        ctx, started = self._context_and_started(spec, activity_input, args.get("inbox_id"))
        with activity_scope(ctx, store=self.runtime.context_store):
            # gate.aevaluate + local enforcement (not aevaluate_lifecycle) so the
            # approval decision stays in this method: draft-mode defers instead
            # of polling, and approval_resume is emitted either way.
            r1 = await self.runtime.gate.aevaluate(started)
            self._enforce_started(spec, r1)
            exec_args, applied = self._after_started(spec, r1, dict(args))
            if applied:
                ctx.metadata["applied_constraints"] = applied
            if r1.verdict is Verdict.REQUIRE_APPROVAL:
                if _defer and self._draft_eligible(spec):
                    return await self._adefer_to_draft(spec, exec_args, r1, ctx)
                if not self._hitl_skips(spec):
                    await self._wait_approval_async(r1, ctx)
            try:
                result = await executor(exec_args)  # type: ignore[arg-type]
            except Exception as e:
                try:
                    await self.runtime.gate.aevaluate(self._completed_event(ctx, spec, error=_error_text(e)))
                except Exception as e2:  # noqa: BLE001
                    logger.warning("ActivityCompleted(error) not delivered: %s", e2)
                raise
            r2: EvaluationResult | None = None
            try:
                r2 = await self.runtime.gate.aevaluate(self._completed_event(ctx, spec, result))
            except Exception as e:  # noqa: BLE001
                self._completed_error_policy(spec, e)
            return self._after_completed(spec, r2, result)

    # ── inbound ──────────────────────────────────────────────────────────

    def _signal_event(self, event: Mapping[str, Any], *, enforce: bool) -> tuple[events.EventEnvelope, dict[str, Any]]:
        fields = build_inbound_signal(event, self.settings, self.runtime.config.privacy.max_body_size)
        name = f"agentmail.{str(event.get('event_type', 'unknown')).replace('.', '_')}"
        ev = events.signal_received(
            **self._workflow_fields(),
            task_queue=fields.get("inbox_id"),
            signal_name=name,
            extra={**self._base_extra(fields.get("inbox_id")), **fields, "enforced": enforce},
        )
        return _tag(ev), fields

    def screen_inbound(self, event: Mapping[str, Any], *, enforce: bool = True) -> tuple[EvaluationResult, dict[str, Any]]:
        """Evaluate an inbound AgentMail event as ``SignalReceived``.

        ``enforce=True`` (message.received*) raises on BLOCK/HALT/guardrail
        failure and drives approval. ``enforce=False`` (delivery-status events)
        records telemetry only. Returns ``(result, redacted_fields)``.
        """
        if self.halted and enforce:
            raise AgentMailHaltedError("Session halted by a previous OpenBox verdict")
        self.ensure_session()
        ev, fields = self._signal_event(event, enforce=enforce)
        if not enforce:
            r = self.runtime.gate.evaluate(ev)
            return r, fields
        r = self.runtime.evaluate_lifecycle(ev)
        _refuse_inbound_constrain(r)
        if r.verdict is Verdict.REQUIRE_APPROVAL:
            self._adapter.handle_approval_sync(
                r, ActivityContext(workflow_id=self.workflow_id, run_id=self.run_id, activity_id="")
            )
        if r.fallback_used and self.settings.read_on_api_error == "fail_closed":
            raise GovernanceAPIError("OpenBox unreachable (fail_closed) while screening inbound mail")
        gr = r.guardrails
        if gr and gr.input_type in ("activity_input", "signal") and isinstance(gr.redacted_input, dict):
            fields = {**fields, **gr.redacted_input}
        return r, fields

    async def ascreen_inbound(self, event: Mapping[str, Any], *, enforce: bool = True) -> tuple[EvaluationResult, dict[str, Any]]:
        """Async ``screen_inbound``. ``aevaluate_lifecycle`` drives approval for
        REQUIRE_APPROVAL; CONSTRAIN is driven manually for parity with the sync
        path (inbound mail is treated like a write)."""
        if self.halted and enforce:
            raise AgentMailHaltedError("Session halted by a previous OpenBox verdict")
        await self.aensure_session()
        ev, fields = self._signal_event(event, enforce=enforce)
        if not enforce:
            r = await self.runtime.gate.aevaluate(ev)
            return r, fields
        r = await self.runtime.aevaluate_lifecycle(ev)
        _refuse_inbound_constrain(r)
        if r.fallback_used and self.settings.read_on_api_error == "fail_closed":
            raise GovernanceAPIError("OpenBox unreachable (fail_closed) while screening inbound mail")
        gr = r.guardrails
        if gr and gr.input_type in ("activity_input", "signal") and isinstance(gr.redacted_input, dict):
            fields = {**fields, **gr.redacted_input}
        return r, fields

    # ── caller-declared trigger (why the agent is acting) ────────────────
    #
    # The SDK sees AgentMail calls, never the thing that caused them: an API
    # request, a bot, a cron tick, a user instruction. Without this the session
    # shows an email going out with no indication of why. The caller declares
    # the trigger and it is recorded as a real SignalReceived, ordered before
    # the activities it caused. Fabricating one the caller did not declare
    # would put untrue events in an audit trail, so nothing is emitted
    # automatically.

    def _trigger_event(self, fields: dict[str, Any], inbox_id: str | None,
                       enforce: bool) -> events.EventEnvelope:
        safe = str(fields["trigger"]).replace(".", "_")
        ev = events.signal_received(
            **self._workflow_fields(),
            task_queue=inbox_id,
            signal_name=f"agentmail.trigger.{safe}",
            extra={**self._base_extra(inbox_id), **fields, "enforced": enforce},
        )
        return _tag(ev)

    def _trigger_verdict(self, r: EvaluationResult, name: str) -> EvaluationResult:
        """Stop verdicts are enforced. REQUIRE_APPROVAL cannot be: a signal
        envelope carries no activity_id, and approval polls are keyed by one,
        so the hold could never resolve. Fail closed and say where the rule
        belongs rather than silently dropping a verdict."""
        if r.verdict in (Verdict.REQUIRE_APPROVAL, Verdict.CONSTRAIN):
            raise OpenBoxConfigError(
                f"policy returned {r.verdict.value} for trigger {name!r}, which cannot be held "
                "for approval (a signal has no activity_id to key the approval poll on). "
                "Put approval/constraint rules on the AgentMail activity they should gate "
                "(e.g. activity_input.action is send_message) instead of on the trigger."
            )
        if r.fallback_used and not self.settings.allow_fallback_for_writes:
            raise GovernanceAPIError(
                f"OpenBox unreachable and fail_open fallback refused for trigger {name!r}"
            )
        return r

    def emit_trigger(self, name: str, data: Mapping[str, Any] | None = None, *,
                     source: str | None = None, inbox_id: str | None = None,
                     enforce: bool = True) -> EvaluationResult:
        """Record what caused the agent to act, before it acts.

        ``name`` groups the trigger (``signal_name`` becomes
        ``agentmail.trigger.<name>``); ``data`` is free-form caller context
        recorded under ``trigger_data``; ``source`` names the system that asked.

        With ``enforce=True`` (default) a BLOCK or HALT verdict stops the agent
        before it touches AgentMail — so a policy can refuse to act on a trigger
        from an untrusted source. ``enforce=False`` records it as telemetry only.
        """
        if self.halted and enforce:
            raise AgentMailHaltedError("Session halted by a previous OpenBox verdict")
        fields = build_trigger_signal(
            name, data, source, self.settings, self.runtime.config.privacy.max_body_size
        )
        self.ensure_session()
        ev = self._trigger_event(fields, inbox_id, enforce)
        if not enforce:
            return self.runtime.gate.evaluate(ev)
        return self._trigger_verdict(self.runtime.evaluate_lifecycle(ev), name)

    async def aemit_trigger(self, name: str, data: Mapping[str, Any] | None = None, *,
                            source: str | None = None, inbox_id: str | None = None,
                            enforce: bool = True) -> EvaluationResult:
        """Async :meth:`emit_trigger`."""
        if self.halted and enforce:
            raise AgentMailHaltedError("Session halted by a previous OpenBox verdict")
        fields = build_trigger_signal(
            name, data, source, self.settings, self.runtime.config.privacy.max_body_size
        )
        await self.aensure_session()
        ev = self._trigger_event(fields, inbox_id, enforce)
        if not enforce:
            return await self.runtime.gate.aevaluate(ev)
        r = await self.runtime.gate.aevaluate(ev)
        if r.verdict.should_stop():
            if r.verdict is Verdict.HALT:
                self._halted = True
                self.runtime.context_store.request_halt()
            self._adapter.raise_lifecycle_blocked(r)
        if r.guardrails and not r.guardrails.validation_passed:
            raise GuardrailsValidationError(
                r.guardrails.get_reason_strings() or ["Guardrails validation failed"]
            )
        return self._trigger_verdict(r, name)

    # ── inbound as an activity (agentmail.receive_message) ───────────────
    #
    # ActivityStarted (screen what AgentMail pushed: sender, auth headers,
    # body…) -> approval -> fetch the authoritative copy with messages.get
    # INSIDE the activity (HTTP instrumentation records it as child spans) ->
    # ActivityCompleted (output guardrails on the fetched content) -> the
    # handler gets the fetched, redacted message. Every failure is fail-closed:
    # nothing reaches the handler unscreened.

    def _receive_prepare(self, event: Mapping[str, Any], transport: str, client: Any) -> tuple[dict[str, Any], Callable[..., Any]]:
        if self.halted:
            raise AgentMailHaltedError(
                "Session halted by a previous OpenBox verdict", activity_type=RECEIVE_MESSAGE.activity_type
            )
        fields = build_inbound_activity_input(
            event, self.settings, self.runtime.config.privacy.max_body_size, transport
        )
        client = client if client is not None else self._mail_client
        if client is None:
            raise OpenBoxConfigError("receive_inbound needs an AgentMail client to fetch the inbound message")
        return fields, client.inboxes.messages.get

    def _receive_needs_approval(self, r1: EvaluationResult) -> bool:
        """Only REQUIRE_APPROVAL may poll. A received message has nothing to
        rewrite, so CONSTRAIN cannot be satisfied — and Core registers no
        approval for it, so polling would wait forever (same hang as the send
        path). Fail closed instead."""
        if r1.verdict is Verdict.CONSTRAIN:
            raise AgentMailBlockedError(
                "policy returned CONSTRAIN for inbound mail, which cannot be applied — a "
                "received message cannot be rewritten. Use REQUIRE APPROVAL or BLOCK instead",
                activity_type=RECEIVE_MESSAGE.activity_type,
                policy_id=r1.policy_id,
                governance_event_id=r1.governance_event_id,
                result=r1,
            )
        return r1.verdict is Verdict.REQUIRE_APPROVAL and not self._hitl_skips(RECEIVE_MESSAGE)

    def _receive_finish(self, ctx: ActivityContext, fields: dict[str, Any], r1: EvaluationResult,
                        r2: EvaluationResult, fetched: Any) -> ReceivedMessage:
        self._check_fallback(RECEIVE_MESSAGE, r2)
        # Raises on BLOCK/HALT/guardrail failure (reads); applies output redaction.
        message = _as_dict(self._after_completed(RECEIVE_MESSAGE, r2, fetched))
        message.pop("action", None)
        # Started-stage redactions were computed on the pushed copy; apply them
        # last so a completed-stage redaction can never re-expose those fields.
        gr = r1.guardrails
        if gr and gr.input_type in ("activity_input", "signal") and isinstance(gr.redacted_input, dict):
            fields = {**fields, **gr.redacted_input}
            for k, v in gr.redacted_input.items():
                if k in message:
                    message[k] = v
        return ReceivedMessage(message, fields, r2, str(ctx.activity_id))

    def _fetch_failed(self, e: Exception, fields: dict[str, Any]) -> InboundFetchError:
        return InboundFetchError(
            f"could not fetch inbound message {fields['message_id']!r} from AgentMail: {e!r}",
            inbox_id=fields["inbox_id"],
            message_id=fields["message_id"],
        )

    def receive_inbound(self, event: Mapping[str, Any], *, agentmail_client: Any = None,
                        transport: str = "webhook") -> ReceivedMessage:
        """Govern one inbound content event as an ``agentmail.receive_message``
        activity (see the section comment). ``agentmail_client`` overrides the
        governor's own client for the fetch; pass the RAW client, not a governed
        proxy, or the fetch becomes a nested ``get_message`` activity.

        Raises ``AgentMailBlockedError`` / ``AgentMailHaltedError`` /
        ``GuardrailsValidationError`` / approval errors when OpenBox refuses the
        message, ``GovernanceAPIError`` when it could not be screened, and
        ``InboundFetchError`` when AgentMail could not return it."""
        spec = RECEIVE_MESSAGE
        fields, get = self._receive_prepare(event, transport, agentmail_client)
        self.ensure_session()
        ctx, started = self._context_and_started(spec, fields, fields["inbox_id"])
        with activity_scope(ctx, store=self.runtime.context_store):
            r1 = self.runtime.evaluate_lifecycle(started)  # raises on stop verdicts / guardrails
            self._check_fallback(spec, r1)
            if self._receive_needs_approval(r1):
                self._wait_approval_sync(r1, ctx)
            try:
                fetched = get(inbox_id=fields["inbox_id"], message_id=fields["message_id"])
            except Exception as e:
                err = _error_text(e)
                _swallow(lambda: self.runtime.gate.evaluate(self._completed_event(ctx, spec, error=err)))
                raise self._fetch_failed(e, fields) from e
            try:
                r2 = self.runtime.gate.evaluate(self._completed_event(ctx, spec, fetched))
            except Exception as e:
                raise _screen_failed(e) from e
            return self._receive_finish(ctx, fields, r1, r2, fetched)

    async def areceive_inbound(self, event: Mapping[str, Any], *, agentmail_client: Any = None,
                               transport: str = "webhook") -> ReceivedMessage:
        """Async ``receive_inbound``. Works with ``AsyncAgentMail`` (awaited) or
        a sync ``AgentMail`` (run in a worker thread; the activity context is a
        ContextVar, so the fetch's HTTP spans still attach to this activity)."""
        spec = RECEIVE_MESSAGE
        fields, get = self._receive_prepare(event, transport, agentmail_client)
        await self.aensure_session()
        ctx, started = self._context_and_started(spec, fields, fields["inbox_id"])
        with activity_scope(ctx, store=self.runtime.context_store):
            r1 = await self.runtime.gate.aevaluate(started)
            self._enforce_started(spec, r1)
            self._check_fallback(spec, r1)
            if self._receive_needs_approval(r1):
                await self._wait_approval_async(r1, ctx)
            kw = {"inbox_id": fields["inbox_id"], "message_id": fields["message_id"]}
            try:
                if inspect.iscoroutinefunction(get):
                    fetched = await get(**kw)
                else:
                    fetched = await asyncio.to_thread(get, **kw)
                    if inspect.isawaitable(fetched):
                        fetched = await fetched
            except Exception as e:
                try:
                    await self.runtime.gate.aevaluate(self._completed_event(ctx, spec, error=_error_text(e)))
                except Exception as e2:  # noqa: BLE001
                    logger.warning("ActivityCompleted(error) not delivered: %s", e2)
                raise self._fetch_failed(e, fields) from e
            try:
                r2 = await self.runtime.gate.aevaluate(self._completed_event(ctx, spec, fetched))
            except Exception as e:
                raise _screen_failed(e) from e
            return self._receive_finish(ctx, fields, r1, r2, fetched)


# Some SDK exceptions repr to kilobytes of HTTP headers, sometimes with
# undecodable bytes. Core rejects such an ``error`` field with HTTP 400 and the
# telemetry is lost entirely — so the failure that mattered goes unrecorded.
# Bound it, keep the type name first so truncation never costs the useful part.
_MAX_ERROR_CHARS = 1000


def _error_text(e: BaseException) -> str:
    """Bounded, wire-safe text for an ``ActivityCompleted`` error field."""
    name = type(e).__name__
    try:
        detail = str(e)
    except Exception:  # noqa: BLE001 - a broken __str__ must not mask the error
        detail = "<unprintable>"
    text = f"{name}: {detail}" if detail else name
    text = text.encode("utf-8", "replace").decode("utf-8", "replace")
    text = "".join(c if c.isprintable() or c == " " else " " for c in text)
    if len(text) > _MAX_ERROR_CHARS:
        text = text[:_MAX_ERROR_CHARS] + " ...[truncated]"
    return text


def _refuse_inbound_constrain(r: EvaluationResult) -> None:
    """Shared by the legacy SignalReceived path — see ``_receive_needs_approval``."""
    if r.verdict is Verdict.CONSTRAIN:
        raise AgentMailBlockedError(
            "policy returned CONSTRAIN for inbound mail, which cannot be applied — a "
            "received message cannot be rewritten. Use REQUIRE APPROVAL or BLOCK instead",
            activity_type="agentmail.receive_message",
            policy_id=r.policy_id,
            governance_event_id=r.governance_event_id,
            result=r,
        )


def _screen_failed(e: Exception) -> Exception:
    """The fetched content could not be screened: never hand it over."""
    if isinstance(e, GovernanceAPIError):
        return e
    return GovernanceAPIError(f"inbound message could not be screened (fail_closed): {e!r}")


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json", by_alias=True))
    raise TypeError(f"unexpected AgentMail message type {type(value).__name__}")


def _tag(ev: events.EventEnvelope) -> events.EventEnvelope:
    return events.EventEnvelope(
        event_type=ev.event_type,
        payload=ev.payload,
        spans=ev.spans,
        hook_trigger=ev.hook_trigger,
        activity_id=ev.activity_id,
        activity_type=ev.activity_type,
        timestamp=ev.timestamp,
        source=EVENT_SOURCE,
    )


def _draft_id_of(draft: Any) -> str | None:
    if isinstance(draft, Mapping):
        return draft.get("draft_id")
    val = getattr(draft, "draft_id", None)
    if val is None and hasattr(draft, "model_dump"):
        val = draft.model_dump().get("draft_id")
    return val


def _swallow(fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        logger.warning("telemetry event not delivered: %s", e)
