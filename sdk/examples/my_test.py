"""Custom governed send — edit the values or pass args. Sends a REAL email.

    .venv\\Scripts\\python.exe examples\\my_test.py [to_address] [--subject "..."] [--text "..."] [--draft]

Optional: --trigger NAME [--trigger-source SRC] records what caused the
send as a SignalReceived emitted BEFORE the send activity, so the session
shows why the agent acted, not just that it did.

Defaults: to = recipient@example.com, subject = "test".
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

from openbox_agentmail import PendingApproval, create_openbox_mail_agent  # noqa: E402
from openbox_agentmail.errors import (  # noqa: E402
    AgentMailBlockedError,
    AgentMailHaltedError,
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
)


def opt(name: str, default: str) -> str:
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


skip = set()
for i, a in enumerate(sys.argv[1:], 1):
    if a in ("--subject", "--text", "--trigger", "--trigger-source") and i + 1 < len(sys.argv):
        skip.add(i + 1)
positional = [a for i, a in enumerate(sys.argv[1:], 1) if not a.startswith("--") and i not in skip]
to = positional[0] if positional else os.environ.get("TEST_RECIPIENT", "recipient@example.com")
subject = opt("--subject", "test")
text = opt("--text", "Test email governed by OpenBox via AgentMail.")
draft = "--draft" in sys.argv
inbox = os.environ["AGENTMAIL_INBOX_ID"]

mail = create_openbox_mail_agent(
    agent_name="MyCustomMailTest",
    approval_mode="draft" if draft else "wait",
)
try:
    # Declare WHY this send is happening. The SDK cannot observe the caller's
    # trigger (an API request, a bot, a cron tick), so it is declared here and
    # recorded as a SignalReceived ordered BEFORE the send it caused.
    trigger = opt("--trigger", "")
    if trigger:
        mail.emit_trigger(
            trigger,
            {"reason": "requested via CLI", "subject": subject, "to": to},
            source=opt("--trigger-source", "cli"),
        )
    res = mail.inboxes.messages.send(
        inbox_id=inbox,
        to=[to],
        subject=subject,
        text=text,
    )
    if isinstance(res, PendingApproval):
        print(f"approval required -> draft {res.draft_id} staged; approve/reject in the OpenBox dashboard...")
        outcome = res.wait()
        print("draft outcome:", outcome.status)
    else:
        print("sent:", getattr(res, "message_id", res))
except (ApprovalRejectedError, ApprovalExpiredError, ApprovalTimeoutError) as e:
    print("approval did not pass:", type(e).__name__, e)
except (AgentMailBlockedError, AgentMailHaltedError) as e:
    print("blocked by OpenBox:", type(e).__name__, e)
finally:
    print("workflow_id:", mail.governor.workflow_id)
    mail.close()
