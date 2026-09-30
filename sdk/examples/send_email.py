"""Minimal governed send. Set OPENBOX_AGENTMAIL_API_URL, OPENBOX_AGENTMAIL_API_KEY
(or OPENBOX_*), and AGENTMAIL_API_KEY first."""

from openbox_agentmail import create_openbox_mail_agent
from openbox_agentmail.errors import AgentMailBlockedError, ApprovalRejectedError

mail = create_openbox_mail_agent()

try:
    msg = mail.inboxes.messages.send(
        inbox_id="agent@your-domain.com",
        to="customer@example.com",
        subject="Hello from a governed agent",
        text="Every word of this email passed OpenBox policy first.",
    )
    print("sent:", msg.message_id)
except ApprovalRejectedError as e:
    print("a reviewer rejected the send:", e)
except AgentMailBlockedError as e:
    print("policy blocked the send:", e.policy_id, e)
finally:
    mail.close()
