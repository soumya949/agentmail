"""Config resolution: env prefix, fail-closed default for email, aliases."""

import pytest

from openbox_agentmail.config import resolve_openbox_config

BASE = {"api_url": "https://core.test", "api_key": "obx_test_x"}


def test_defaults_fail_closed_and_engine():
    c = resolve_openbox_config(environ={}, **BASE)
    assert c.on_api_error == "fail_closed"
    assert c.sdk_engine == "agentmail"
    assert c.instrumentation.db_enabled is False
    assert c.instrumentation.file_enabled is False
    assert c.instrumentation.function_enabled is False


def test_env_overrides_default_fail_closed():
    env = {"OPENBOX_AGENTMAIL_ON_API_ERROR": "fail_open"}
    c = resolve_openbox_config(environ=env, **BASE)
    assert c.on_api_error == "fail_open"


def test_global_env_fallback():
    env = {"OPENBOX_ON_API_ERROR": "fail_open", "OPENBOX_API_URL": "https://core.test"}
    c = resolve_openbox_config(environ=env, api_key="obx_test_x")
    assert c.on_api_error == "fail_open"
    assert c.api_url == "https://core.test"


def test_prefixed_env_wins_over_global():
    env = {
        "OPENBOX_API_URL": "https://global.test",
        "OPENBOX_AGENTMAIL_API_URL": "https://prefixed.test",
    }
    c = resolve_openbox_config(environ=env, api_key="obx_test_x")
    assert c.api_url == "https://prefixed.test"


def test_crewai_style_aliases_warn():
    env = {"OPENBOX_URL": "https://legacy.test"}
    with pytest.warns(DeprecationWarning):
        c = resolve_openbox_config(environ=env, api_key="obx_test_x")
    assert c.api_url == "https://legacy.test"


def test_did_private_key_aliases_warn():
    import uuid

    env = {
        "OPENBOX_AGENTMAIL_API_URL": "https://x.test",
        "OPENBOX_AGENTMAIL_DID": f"did:aip:{uuid.uuid4()}",
        "OPENBOX_AGENTMAIL_PRIVATE_KEY": "not-a-real-key",
    }
    with pytest.warns(DeprecationWarning):
        try:
            resolve_openbox_config(environ=env, api_key="obx_test_x")
        except Exception:
            pass  # key format may fail validation; the alias warning is the assertion


def test_explicit_beats_env():
    env = {"OPENBOX_AGENTMAIL_API_URL": "https://env.test"}
    c = resolve_openbox_config(environ=env, api_url="https://explicit.test", api_key="obx_test_x")
    assert c.api_url == "https://explicit.test"


def test_hitl_mapping():
    c = resolve_openbox_config(
        environ={}, hitl={"enabled": True, "poll_interval_ms": 250, "max_wait_ms": 5000}, **BASE
    )
    assert c.hitl.enabled is True
    assert c.hitl.poll_interval_ms == 250
    assert c.hitl.max_wait_ms == 5000
