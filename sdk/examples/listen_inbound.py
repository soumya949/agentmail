"""Governed INBOUND listener over AgentMail's WebSocket — no public URL /
ngrok needed. Every email that arrives in AGENTMAIL_INBOX_ID becomes an
``agentmail.receive_message`` activity in OpenBox BEFORE your handler sees it:
ActivityStarted screens the pushed email, the message is fetched with
messages.get inside the activity (HTTP GET spans on the dashboard), and
ActivityCompleted screens the fetched copy. Blocked mail never reaches the
handler; mail that can't be screened is dead-lettered, never delivered.

    .venv\\Scripts\\python.exe examples\\listen_inbound.py

Then send an email TO the inbox from anywhere (Gmail, AgentMail console,
examples\\my_test.py agent@your-domain.com). Ctrl+C to stop.
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

from openbox_agentmail import (  # noqa: E402
    WebsocketInbound,
    create_openbox_mail_agent,
    quarantine_on_blocked,
)

inbox = os.environ["AGENTMAIL_INBOX_ID"]
mail = create_openbox_mail_agent(agent_name="InboundListener")
# Labels blocked mail "quarantine" in AgentMail via a governed messages.update,
# so a block is visible in the AgentMail console too - not only in OpenBox.
quarantine = quarantine_on_blocked(mail)


def on_mail(inbound):
    msg = inbound.event.get("message") or {}
    print(f"\n[DELIVERED to agent | verdict={inbound.verdict} | activity={inbound.activity_id}]")
    print("  from:   ", msg.get("from") or msg.get("from_"))
    print("  subject:", msg.get("subject"))
    print("  text:   ", (msg.get("text") or msg.get("extracted_text") or "")[:300])


def on_blocked(event, err):
    msg = event.get("message") or {}
    print(f"\n[BLOCKED by OpenBox] {type(err).__name__}: {err}")
    print("  from:   ", msg.get("from") or msg.get("from_"))
    print("  subject:", msg.get("subject"))
    # The message stays in the inbox - AgentMail already accepted it - but it
    # gets labelled so humans and filters can see it was refused.
    try:
        quarantine(event, err)
        print("  -> labelled 'quarantine' in AgentMail")
    except Exception as e:  # noqa: BLE001 - labelling is best effort
        print(f"  -> could not label it: {type(e).__name__}: {e}")


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
    # One workflow per email, opened by a SignalReceived recording the arrival:
    #   WorkflowStarted -> SignalReceived -> ActivityStarted/Completed -> WorkflowCompleted
    # Behavioural rules only see within a session, so drop session_per_message
    # if you need rules that look across several emails.
    arrival_signal=True,
    session_per_message=True,
)
thread = threading.Thread(target=listener.listen, daemon=True)
thread.start()
print(f"listening on {inbox} ... send it an email. Ctrl+C to stop.")
print("workflow_id (OpenBox session) appears after the first email arrives.")

try:
    while thread.is_alive():
        time.sleep(0.5)
        if mail.governor.workflow_id and not getattr(on_mail, "_shown", False):
            print("workflow_id:", mail.governor.workflow_id)
            on_mail._shown = True
except KeyboardInterrupt:
    pass
finally:
    listener.stop()
    mail.close()
    print("\nstopped; session closed")
