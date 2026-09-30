"""HTTP spans (nested http_request/http_response under each AgentMail
activity in the dashboard) need openbox-sdk-python's ``[http]`` extra. Without
it the base SDK's instrumentation installs nothing and drops spans silently."""

import importlib.util
from importlib.metadata import requires


def test_httpx_instrumentor_available():
    # agentmail-python talks to AgentMail over httpx
    assert importlib.util.find_spec("opentelemetry.instrumentation.httpx") is not None


def test_package_declares_http_extra():
    reqs = requires("openbox-agentmail-sdk-python") or []
    assert any(r.startswith("openbox-sdk-python[http]") for r in reqs), reqs
