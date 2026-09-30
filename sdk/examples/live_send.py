"""Live governed SEND — sends a REAL email. Loads sdk/.env.

    .venv\\Scripts\\python.exe examples\\live_send.py [to_address] [--draft]

Default recipient: recipient@example.com. With --draft, a REQUIRE_APPROVAL
verdict stages an AgentMail draft and waits for the dashboard decision
(approve → sent, reject/expire → draft deleted).
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

args = [a for a in sys.argv[1:] if not a.startswith("--")]
to = args[0] if args else os.environ.get("TEST_RECIPIENT", "recipient@example.com")
draft = "--draft" in sys.argv
inbox = os.environ["AGENTMAIL_INBOX_ID"]

mail = create_openbox_mail_agent(
    agent_name="AgentMailLiveSend",
    approval_mode="draft" if draft else "wait",
)
try:
    res = mail.inboxes.messages.send(
        inbox_id=inbox,
        to=[to],
        subject="OpenBox x AgentMail governed send test",
        text="This email passed OpenBox governance before AgentMail sent it.",
    )
    if isinstance(res, PendingApproval):
        print(f"approval required -> draft {res.draft_id} staged; approve/reject it in the OpenBox dashboard...")
        outcome = res.wait()
        print("draft outcome:", outcome.status, outcome)
    else:
        print("sent:", getattr(res, "message_id", res))
except (ApprovalRejectedError, ApprovalExpiredError, ApprovalTimeoutError) as e:
    print("approval did not pass:", type(e).__name__, e)
except (AgentMailBlockedError, AgentMailHaltedError) as e:
    print("blocked by OpenBox:", type(e).__name__, e)
finally:
    print("workflow_id:", mail.governor.workflow_id)
    mail.close()
