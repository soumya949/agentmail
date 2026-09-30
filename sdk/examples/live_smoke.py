"""Live read-only smoke test against real OpenBox Core + AgentMail.

Loads sdk/.env, validates the OpenBox key, runs one governed read
(messages.list on AGENTMAIL_INBOX_ID) and closes the session. Sends no email.

    .venv\\Scripts\\python.exe examples\\live_smoke.py
"""

import os
from pathlib import Path


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env(Path(__file__).resolve().parent.parent / ".env")

from openbox_agentmail import create_openbox_mail_agent  # noqa: E402

inbox = os.environ["AGENTMAIL_INBOX_ID"]
mail = create_openbox_mail_agent(agent_name="AgentMailSmoke", install_instrumentation=True)
try:
    res = mail.inboxes.messages.list(inbox_id=inbox, limit=5)
    count = len(getattr(res, "messages", None) or [])
    print(f"governed list OK: {count} message(s) in {inbox}")
    print("workflow_id:", mail.governor.workflow_id)
finally:
    mail.close()
    print("session closed (WorkflowCompleted sent)")
