"""Receive an email, then reply to it — both governed, in ONE session.

    .venv\\Scripts\\python.exe examples\\reply_flow.py

This is the "prerequisite met" case for a behavioral rule of
Trigger=http_post / Prior State=http_get: the inbound fetch inside
agentmail.receive_message logs an http_get, so the reply's http_post is
allowed to proceed. Run examples\\my_test.py on its own for the opposite case
(a cold send with no prior read).

Replies once, then keeps listening without replying again, so a bounce or an
auto-responder can't start a mail loop. Ctrl+C to stop.
"""

import logging
import os
import threading
import time
from pathlib import Path


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env(Path(__file__).resolve().parent.parent / ".env")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from openbox_agentmail import WebsocketInbound, create_openbox_mail_agent  # noqa: E402
from openbox_agentmail.errors import (  # noqa: E402
    AgentMailBlockedError,
    AgentMailHaltedError,
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
)

inbox = os.environ["AGENTMAIL_INBOX_ID"]
mail = create_openbox_mail_agent(agent_name="ReplyFlowAgent")
replied = threading.Event()


def on_mail(inbound):
    msg = inbound.event.get("message") or {}
    sender = msg.get("from") or msg.get("from_")
    print(f"\n[RECEIVED | verdict={inbound.verdict} | activity={inbound.activity_id}]")
    print("  from:   ", sender)
    print("  subject:", msg.get("subject"))

    if replied.is_set():
        print("  (already replied once this run - not replying again)")
        return
    replied.set()

    print("\n[REPLYING in the same session ...]")
    try:
        res = mail.inboxes.messages.reply(
            inbox_id=msg.get("inbox_id") or inbox,
            message_id=msg.get("message_id"),
            text="Thanks - this reply was governed by OpenBox before it went out.",
        )
        print("  reply sent:", getattr(res, "message_id", res))
    except (ApprovalRejectedError, ApprovalExpiredError, ApprovalTimeoutError) as e:
        print("  reply held then refused:", type(e).__name__, e)
    except (AgentMailBlockedError, AgentMailHaltedError) as e:
        print("  reply refused:", type(e).__name__, e)


def on_blocked(event, err):
    msg = event.get("message") or {}
    print(f"\n[BLOCKED by OpenBox] {type(err).__name__}: {err}")
    print("  subject:", msg.get("subject"))


def on_dead_letter(event, err):
    msg = event.get("message") or {}
    print(f"\n[NOT SCREENED - held back] {type(err).__name__}: {err}")
    print("  subject:", msg.get("subject"), "(still in the AgentMail inbox)")


listener = WebsocketInbound(
    mail.governor,
    mail.raw,
    handler=on_mail,
    on_blocked=on_blocked,
    on_dead_letter=on_dead_letter,
    subscribe={"event_types": ["message.received"], "inbox_ids": [inbox]},
)
thread = threading.Thread(target=listener.listen, daemon=True)
thread.start()
print(f"listening on {inbox} ... send it an email and it will reply once. Ctrl+C to stop.")

try:
    while thread.is_alive():
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
finally:
    listener.stop()
    print("workflow_id:", mail.governor.workflow_id)
    mail.close()
    print("stopped; session closed")
