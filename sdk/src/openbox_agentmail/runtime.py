"""Runtime factory: ``OpenBoxConfig`` -> ``OpenBoxRuntime`` with the AgentMail adapter."""

from __future__ import annotations

from typing import Any

from openbox_core.approvals import ApprovalPoller
from openbox_core.config import OpenBoxConfig
from openbox_core.context import ContextStore
from openbox_core.runtime import OpenBoxRuntime

from .adapter import AgentMailAdapter
from .config import resolve_openbox_config

__all__ = ["create_openbox_runtime", "build_runtime"]


def build_runtime(config: OpenBoxConfig, *, client: Any = None) -> OpenBoxRuntime:
    """Wire adapter + poller around a resolved config. ``client`` lets tests
    inject an ``EvaluationClient`` bound to ``FakeCore``."""
    # A fresh ContextStore per governed agent: the base default is process-global
    # and would let one session's HALT poison every other agent in the process.
    runtime = OpenBoxRuntime(
        config, adapter=AgentMailAdapter(), client=client, context_store=ContextStore()
    )
    if config.hitl.enabled:
        hitl = config.hitl
        runtime.adapter._poller = ApprovalPoller(  # type: ignore[attr-defined]
            runtime.client,
            poll_interval_seconds=hitl.poll_interval_ms / 1000.0,
            max_wait_seconds=(hitl.max_wait_ms / 1000.0) if hitl.max_wait_ms else None,
        )
    return runtime


def create_openbox_runtime(
    *, install_instrumentation: bool = True, validate_key: bool = True, **config_overrides: Any
) -> OpenBoxRuntime:
    config = resolve_openbox_config(**config_overrides)
    runtime = build_runtime(config)
    if validate_key:
        runtime.client.validate_api_key()
    if install_instrumentation:
        runtime.install_instrumentation()
    return runtime
