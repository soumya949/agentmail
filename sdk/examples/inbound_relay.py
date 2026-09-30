"""Governed inbound relay. AgentMail -> this endpoint -> your handler, after
authentication, dedupe, SignalReceived screening and guardrail redaction.

Run:  uvicorn examples.inbound_relay:app --port 8080
Register: mail.webhooks.create(url="https://you/webhooks/agentmail",
                               event_types=["message.received"],
                               headers={"Authorization": "Bearer <secret>"})
"""

import os

from openbox_agentmail import (
    InboundRelay,
    WebhookAuth,
    create_openbox_mail_agent,
)

agent = create_openbox_mail_agent()


def on_mail(inbound):
    msg = inbound.event["message"]
    # content here is post-guardrail — redactions already applied
    print(f"[{inbound.verdict}] {msg.get('from_') or msg.get('from')}: {msg.get('subject')}")


relay = InboundRelay(
    agent.governor,
    auth=WebhookAuth(secret=os.environ["AGENTMAIL_WEBHOOK_SECRET"]),
    handler=on_mail,
)
app = relay.asgi()
