"""``OpenBoxMailAgent``: a governed drop-in for ``agentmail.AgentMail``.

The wrapper mirrors the native resource tree (``mail.inboxes.messages.send(...)``)
so existing code changes one line. Every method resolves through the catalogue;
uncatalogued methods are refused, never passed through.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from openbox_core.runtime import OpenBoxRuntime

from .catalog import ActionSpec, lookup
from .config import AgentMailSettings, resolve_openbox_config
from .errors import OpenBoxConfigError, UncataloguedActionError
from .governor import MailGovernor
from .runtime import build_runtime

__all__ = ["OpenBoxMailAgent", "AsyncOpenBoxMailAgent", "create_openbox_mail_agent", "create_async_openbox_mail_agent"]

_SETTINGS_FIELDS = set(AgentMailSettings.__dataclass_fields__)


def _bind(method: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Normalize positional + keyword call args into a flat kwargs dict."""
    try:
        sig = inspect.signature(method)
        bound = sig.bind(*args, **kwargs)
    except TypeError:
        return {**{f"arg{i}": a for i, a in enumerate(args)}, **kwargs}
    out = dict(bound.arguments)
    # VAR_KEYWORD params bind as a nested dict; expand so callers and the wire
    # see the real names (``add_labels=[...]``, not ``kw={"add_labels": ...}``).
    for name, param in sig.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD and isinstance(out.get(name), dict):
            out.update(out.pop(name))
    return out


def _inject_idempotency(method: Callable[..., Any], call_args: dict[str, Any], key: str) -> None:
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return
    if "idempotency_key" in params and not call_args.get("idempotency_key"):
        call_args["idempotency_key"] = key


class _GovernedResource:
    """Recursive attribute proxy over an ``agentmail`` resource object."""

    def __init__(self, target: Any, path: tuple[str, ...], governor: MailGovernor, is_async: bool):
        self._target = target
        self._path = path
        self._governor = governor
        self._async = is_async

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._target, name)
        path = (*self._path, name)
        if callable(attr):
            return self._wrap(attr, path)
        return _GovernedResource(attr, path, self._governor, self._async)

    def _spec(self, path: tuple[str, ...]) -> ActionSpec:
        try:
            return lookup(path)
        except KeyError:
            raise UncataloguedActionError(
                f"AgentMail method {'.'.join(path)} is not in the OpenBox action catalogue; "
                "refusing to call it ungoverned. Add it via tool_type_map or upgrade the SDK."
            ) from None

    def _wrap(self, method: Callable[..., Any], path: tuple[str, ...]) -> Callable[..., Any]:
        spec = self._spec(path)
        governor = self._governor

        if self._async:

            async def agoverned(*args: Any, **kwargs: Any) -> Any:
                call_args = _bind(method, args, kwargs)

                async def execute(final: dict[str, Any]) -> Any:
                    ctx = governor.runtime.context_store.current_activity_context()
                    if ctx and ctx.activity_id:
                        _inject_idempotency(method, final, ctx.activity_id)
                    return await method(**final)

                return await governor.arun(spec, call_args, execute)

            agoverned.__name__ = method.__name__
            agoverned.__doc__ = method.__doc__
            return agoverned

        def governed(*args: Any, **kwargs: Any) -> Any:
            call_args = _bind(method, args, kwargs)

            def execute(final: dict[str, Any]) -> Any:
                ctx = governor.runtime.context_store.current_activity_context()
                if ctx and ctx.activity_id:
                    _inject_idempotency(method, final, ctx.activity_id)
                return method(**final)

            return governor.run(spec, call_args, execute)

        governed.__name__ = method.__name__
        governed.__doc__ = method.__doc__
        return governed


class OpenBoxMailAgent(_GovernedResource):
    """Governed sync AgentMail client.

    Args:
        agentmail_client: an ``agentmail.AgentMail`` instance (built from
            ``AGENTMAIL_API_KEY`` when omitted).
        runtime: a prepared ``OpenBoxRuntime`` (tests inject one bound to FakeCore).
        settings: SDK behaviour settings.
    """

    def __init__(self, agentmail_client: Any, runtime: OpenBoxRuntime, settings: AgentMailSettings | None = None):
        settings = settings or AgentMailSettings()
        super().__init__(
            agentmail_client, (), MailGovernor(runtime, settings, mail_client=agentmail_client), is_async=False
        )
        self.runtime = runtime
        self.settings = settings

    @property
    def governor(self) -> MailGovernor:
        return self._governor

    @property
    def raw(self) -> Any:
        """The underlying ungoverned client. Use deliberately."""
        return self._target

    @contextmanager
    def session(self, *, fail_on_error: bool = True) -> Any:
        """Scope one OpenBox session (Workflow) to a block of work.

        On exit the session is closed — ``WorkflowCompleted`` — and the next
        governed call opens a fresh one. Use it to make each task, request or
        email its own workflow instead of one long-lived session:

            with agent.session():
                agent.emit_trigger("api_request", source="billing")
                agent.inboxes.messages.send(...)

        Note this scopes what behavioural rules can see: they match on prior
        activity *within a session*, so a rule like "must read mail before
        sending" cannot look back past the block. Keep one long session when
        cross-task sequences matter. A HALT still persists across sessions —
        it poisons the agent, not just the workflow.
        """
        try:
            yield self
        except BaseException as e:
            self._governor.close_session(repr(e) if fail_on_error else None)
            raise
        else:
            self._governor.close_session()

    def emit_trigger(self, name: str, data: Any = None, **kw: Any) -> Any:
        """Record what caused the agent to act, before it acts — see
        ``MailGovernor.emit_trigger``. Emits a ``SignalReceived`` ordered ahead
        of the activities that follow, so the session shows WHY, not just what.
        """
        return self._governor.emit_trigger(name, data, **kw)

    def close(self, error: str | None = None) -> None:
        self._governor.close_session(error)
        self.runtime.close()

    def __enter__(self) -> OpenBoxMailAgent:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(repr(exc) if exc else None)


class AsyncOpenBoxMailAgent(_GovernedResource):
    def __init__(self, agentmail_client: Any, runtime: OpenBoxRuntime, settings: AgentMailSettings | None = None):
        settings = settings or AgentMailSettings()
        super().__init__(
            agentmail_client, (), MailGovernor(runtime, settings, mail_client=agentmail_client), is_async=True
        )
        self.runtime = runtime
        self.settings = settings

    @property
    def governor(self) -> MailGovernor:
        return self._governor

    @property
    def raw(self) -> Any:
        return self._target

    @asynccontextmanager
    async def session(self, *, fail_on_error: bool = True) -> Any:
        """Async :meth:`OpenBoxMailAgent.session`."""
        try:
            yield self
        except BaseException as e:
            await self._governor.aclose_session(repr(e) if fail_on_error else None)
            raise
        else:
            await self._governor.aclose_session()

    async def emit_trigger(self, name: str, data: Any = None, **kw: Any) -> Any:
        """Async ``MailGovernor.aemit_trigger``."""
        return await self._governor.aemit_trigger(name, data, **kw)

    async def aclose(self, error: str | None = None) -> None:
        await self._governor.aclose_session(error)
        await self.runtime.aclose()

    async def __aenter__(self) -> AsyncOpenBoxMailAgent:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose(repr(exc) if exc else None)


def _split_options(opts: dict[str, Any]) -> tuple[AgentMailSettings, dict[str, Any]]:
    settings_kwargs = {k: opts.pop(k) for k in list(opts) if k in _SETTINGS_FIELDS}
    settings = AgentMailSettings(**settings_kwargs)
    return settings, opts


def _build(
    async_client: bool,
    *,
    agentmail_client: Any = None,
    agentmail_api_key: str | None = None,
    runtime: OpenBoxRuntime | None = None,
    install_instrumentation: bool = True,
    validate: bool = True,
    **opts: Any,
) -> tuple[Any, OpenBoxRuntime, AgentMailSettings]:
    settings, config_opts = _split_options(opts)
    settings.agent_name = config_opts.get("agent_name") or settings.agent_name
    if runtime is None:
        config = resolve_openbox_config(**config_opts)
        runtime = build_runtime(config)
        if validate:
            runtime.client.validate_api_key()
        if install_instrumentation:
            runtime.install_instrumentation()
    if agentmail_client is None:
        import os

        from agentmail import AgentMail, AsyncAgentMail

        key = agentmail_api_key or os.environ.get("AGENTMAIL_API_KEY")
        if not key:
            raise OpenBoxConfigError("AGENTMAIL_API_KEY is required (or pass agentmail_client=)")
        agentmail_client = (AsyncAgentMail if async_client else AgentMail)(api_key=key)
    return agentmail_client, runtime, settings


def create_openbox_mail_agent(**opts: Any) -> OpenBoxMailAgent:
    """One-call setup. Accepts ``AgentMailSettings`` fields, ``OpenBoxConfig``
    fields (``env_prefix``, ``api_url``, ``on_api_error``, ``hitl=``, ...),
    plus ``agentmail_client=``, ``agentmail_api_key=``, ``runtime=``,
    ``install_instrumentation=``, ``validate=``."""
    client, runtime, settings = _build(False, **opts)
    return OpenBoxMailAgent(client, runtime, settings)


def create_async_openbox_mail_agent(**opts: Any) -> AsyncOpenBoxMailAgent:
    client, runtime, settings = _build(True, **opts)
    return AsyncOpenBoxMailAgent(client, runtime, settings)
