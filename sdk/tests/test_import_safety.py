"""Root import must be cheap: no httpx, no agentmail, no crypto, no OTel."""

import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parent.parent / "src")


def test_root_import_has_no_heavy_modules():
    code = f"""
import sys
sys.path.insert(0, {SRC!r})
import openbox_agentmail
heavy = {{'httpx', 'agentmail', 'cryptography', 'opentelemetry'}}
leaked = heavy & set(sys.modules)
assert not leaked, f'root import pulled heavy modules: {{leaked}}'
print('OK', openbox_agentmail.__version__)
"""
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_lazy_exports_resolve():
    import openbox_agentmail as pkg

    assert pkg.OpenBoxMailAgent is not None
    assert pkg.AgentMailSettings is not None
    assert pkg.MailGovernor is not None
    assert callable(pkg.create_openbox_mail_agent)


def test_tool_type_map_covers_friendly_actions():
    import openbox_agentmail as pkg
    from openbox_agentmail.catalog import _FRIENDLY, _class_for, lookup

    for tool, cls in pkg.TOOL_TYPE_MAP.items():
        # every TOOL_TYPE_MAP key must be a catalogued friendly action and every
        # value must resolve through the shared semantic vocabulary
        assert _class_for(cls).name != "UNKNOWN", f"{tool}: bad type {cls}"
        found = any(
            lookup((res, meth)).action == tool for (res, meth) in _FRIENDLY
        )
        assert found, f"TOOL_TYPE_MAP key {tool} not in catalogue"
