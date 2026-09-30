"""Configuration: SDK-level settings plus resolution into ``OpenBoxConfig``.

Canonical env names come from ``openbox_core`` (``<PREFIX>_API_URL``,
``<PREFIX>_API_KEY``, ``<PREFIX>_AGENT_DID``, ``<PREFIX>_AGENT_PRIVATE_KEY``,
``<PREFIX>_ON_API_ERROR`` ... falling back to ``OPENBOX_*``). CrewAI-style
names (``OPENBOX_URL``, ``<PREFIX>_DID``, ``<PREFIX>_PRIVATE_KEY``) are accepted
as aliases with a deprecation warning.
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from openbox_core.config import HitlConfig, InstrumentationConfig, OpenBoxConfig, PrivacyConfig

__all__ = ["AgentMailSettings", "resolve_openbox_config", "DEFAULT_ENV_PREFIX", "SDK_ENGINE"]

logger = logging.getLogger(__name__)

DEFAULT_ENV_PREFIX = "OPENBOX_AGENTMAIL"
SDK_ENGINE = "agentmail"
EVENT_SOURCE = "agentmail-telemetry"

AGENTMAIL_API_URL = "https://api.agentmail.to"
AGENTMAIL_MCP_URL = "https://mcp.agentmail.to/mcp"

# alias env suffix -> canonical OpenBoxConfig field
_PREFIX_ALIASES = {"DID": "agent_did", "PRIVATE_KEY": "agent_private_key"}
_GLOBAL_ALIASES = {"OPENBOX_URL": "api_url"}


@dataclass
class AgentMailSettings:
    """Behaviour knobs that live in this SDK (not in ``openbox_core``)."""

    agent_name: str = "AgentMailAgent"
    workflow_type: str | None = None  # default: f"{agent_name} Agent"
    inbox_ids: set[str] | None = None  # None = any inbox the AgentMail key allows
    pod_id: str | None = None
    internal_domains: set[str] = field(default_factory=set)
    content_mode: Literal["full", "metadata_only"] = "full"
    attachment_scan: bool = False
    read_on_api_error: Literal["fail_open", "fail_closed"] = "fail_closed"
    allow_fallback_for_writes: bool = False
    approval_mode: Literal["wait", "draft"] = "wait"
    approval_store: Any = None  # approvals.ApprovalStore; default in-memory
    tool_type_map: dict[str, str] = field(default_factory=dict)
    surface: str = "rest"

    def resolved_workflow_type(self) -> str:
        return self.workflow_type or f"{self.agent_name} Agent"


def _apply_aliases(env_prefix: str, environ: Mapping[str, str], explicit: dict[str, Any]) -> None:
    """Fill ``explicit`` from CrewAI-style env names when the canonical ones are absent."""
    canonical = {
        "api_url": ("API_URL",),
        "agent_did": ("AGENT_DID",),
        "agent_private_key": ("AGENT_PRIVATE_KEY",),
    }

    def has_canonical(field_name: str) -> bool:
        if explicit.get(field_name) is not None:
            return True
        for suffix in canonical[field_name]:
            if environ.get(f"{env_prefix}_{suffix}") or environ.get(f"OPENBOX_{suffix}"):
                return True
        return False

    used: list[str] = []
    for suffix, field_name in _PREFIX_ALIASES.items():
        name = f"{env_prefix}_{suffix}"
        if not has_canonical(field_name) and environ.get(name):
            explicit[field_name] = environ[name]
            used.append(name)
    for name, field_name in _GLOBAL_ALIASES.items():
        if not has_canonical(field_name) and environ.get(name):
            explicit[field_name] = environ[name]
            used.append(name)
    if used:
        warnings.warn(
            f"Using legacy OpenBox env names {used}; prefer {env_prefix}_API_URL / "
            f"{env_prefix}_AGENT_DID / {env_prefix}_AGENT_PRIVATE_KEY",
            DeprecationWarning,
            stacklevel=3,
        )


def resolve_openbox_config(
    *,
    env_prefix: str = DEFAULT_ENV_PREFIX,
    environ: Mapping[str, str] | None = None,
    validate: bool = True,
    hitl: HitlConfig | Mapping[str, Any] | None = None,
    instrumentation: InstrumentationConfig | Mapping[str, Any] | None = None,
    privacy: PrivacyConfig | Mapping[str, Any] | None = None,
    **explicit: Any,
) -> OpenBoxConfig:
    """Resolve an ``OpenBoxConfig`` with this SDK's defaults.

    Defaults that differ from the base SDK, chosen because email is irreversible:
    - ``on_api_error="fail_closed"`` unless the env or caller says otherwise
    - HTTP instrumentation on; DB / file / function instrumentation off
    """
    if environ is None:
        import os

        environ = os.environ
    explicit = {k: v for k, v in explicit.items() if v is not None}
    _apply_aliases(env_prefix, environ, explicit)

    if explicit.get("on_api_error") is None and not (
        environ.get(f"{env_prefix}_ON_API_ERROR") or environ.get("OPENBOX_ON_API_ERROR")
    ):
        explicit["on_api_error"] = "fail_closed"

    from . import __version__

    explicit.setdefault("sdk_engine", SDK_ENGINE)
    explicit.setdefault("sdk_version", __version__)

    explicit["hitl"] = _coerce(HitlConfig, hitl)
    explicit["instrumentation"] = _coerce(
        InstrumentationConfig,
        instrumentation,
        defaults={"db_enabled": False, "file_enabled": False, "function_enabled": False},
    )
    explicit["privacy"] = _coerce(PrivacyConfig, privacy)

    return OpenBoxConfig.resolve(env_prefix=env_prefix, environ=environ, validate=validate, **explicit)


def _coerce(cls: type, value: Any, defaults: Mapping[str, Any] | None = None) -> Any:
    if isinstance(value, cls):
        return value
    kwargs = dict(defaults or {})
    if value:
        kwargs.update(dict(value))
    return cls(**kwargs)
