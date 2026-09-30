"""Test a BLOCK AND PATCH policy end to end.

!! CURRENTLY UNREACHABLE FOR EMAIL. The OpenBox rule builder pins BLOCK AND
PATCH to `activity_type == llm_call` and its patch vocabulary is LLM routing
(providers, regions), so an `agentmail.send_message` activity can never carry a
patch. This script will always report "No usable patch attached" - that is the
platform limitation, NOT a mistake in your rule. Kept so the path is ready if
Core gains patch support for non-LLM activities.

Sends a REAL email if the policy ever does supply a usable patch.

    .venv\\Scripts\\python.exe examples\\patch_retry.py [to_address] [--subject "redirect me"]

A BLOCK-with-patch is a refusal plus a remediation hint: OpenBox says "not like
that, like this". The SDK never applies it silently — this script asks for it
explicitly with ``err.patched_args()`` and re-sends, which is a brand new
governed call that is evaluated from scratch.
"""

import os
import sys
from pathlib import Path


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env(Path(__file__).resolve().parent.parent / ".env")

from openbox_agentmail import create_openbox_mail_agent  # noqa: E402
from openbox_agentmail.errors import (  # noqa: E402
    AgentMailBlockedError,
    AgentMailHaltedError,
)


def opt(name: str, default: str) -> str:
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


skip = {sys.argv.index("--subject") + 1} if "--subject" in sys.argv else set()
positional = [a for i, a in enumerate(sys.argv[1:], 1) if not a.startswith("--") and i not in skip]

inbox = os.environ["AGENTMAIL_INBOX_ID"]
args = {
    "inbox_id": inbox,
    "to": [positional[0] if positional else os.environ.get("TEST_RECIPIENT", "recipient@example.com")],
    "subject": opt("--subject", "redirect me"),
    "text": "Testing a BLOCK AND PATCH policy.",
}

mail = create_openbox_mail_agent(agent_name="PatchRetryTest")
print(f"attempt 1 -> to={args['to']} subject={args['subject']!r}")
try:
    res = mail.inboxes.messages.send(**args)
    print("SENT without a block:", getattr(res, "message_id", res))
    print("   (the BLOCK AND PATCH rule did not match this call)")
except AgentMailHaltedError as e:
    print("HALTED:", e)
except AgentMailBlockedError as e:
    print("BLOCKED:", e)
    print("  policy_id:", e.policy_id)
    print("  patch    :", e.patch)
    fixed = e.patched_args(args)
    if fixed is None:
        print("\nNo usable patch attached -> this is a plain BLOCK, nothing to retry.")
        print("If you expected a patch, check the rule emitted exactly one key: new_input.")
    else:
        changed = {k: v for k, v in fixed.items() if args.get(k) != v}
        print("\npolicy wants these changes:", changed)
        print(f"attempt 2 -> to={fixed['to']} (fresh governed call, evaluated again)")
        try:
            res = mail.inboxes.messages.send(**fixed)
            print("SENT after applying the patch:", getattr(res, "message_id", res))
        except (AgentMailBlockedError, AgentMailHaltedError) as e2:
            print("patched retry also refused:", type(e2).__name__, e2)
finally:
    print("workflow_id:", mail.governor.workflow_id)
    mail.close()
