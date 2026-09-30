"""A governed send endpoint — the realistic integration shape, no frontend.

    .venv\\Scripts\\python.exe examples\\api_server.py        (listens on :8080)

Then, from anywhere:

    curl -X POST http://localhost:8080/send ^
      -H "content-type: application/json" ^
      -H "x-caller: billing-service" ^
      -d "{\\"to\\":\\"recipient@example.com\\",\\"subject\\":\\"Invoice reminder\\",\\"text\\":\\"Past due.\\"}"

Each request declares its caller as a trigger, then sends through the governed
client — so a session reads: SignalReceived (who asked) -> send_message
(what happened). Verdicts map onto HTTP status, which is what a real caller
needs: 403 means policy refused, and no email exists.

Stdlib only - no framework to install. One agent is shared across requests, so
all traffic lands in one OpenBox session and behavioral rules can see across
requests. Ctrl+C to stop.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    ContractError,
)

PORT = int(os.environ.get("PORT", "8080"))
INBOX = os.environ["AGENTMAIL_INBOX_ID"]
# One workflow per request (WorkflowStarted -> ... -> WorkflowCompleted), so a
# session on the dashboard is exactly one email. Set SESSION_PER_REQUEST=0 for
# one long-lived session instead - needed if behavioural rules must match
# across requests, since they only see prior activity within a session.
SESSION_PER_REQUEST = os.environ.get("SESSION_PER_REQUEST", "1") != "0"
mail = create_openbox_mail_agent(agent_name="MailApiService")
# The governor is shared, so per-request sessions must not interleave.
_session_lock = threading.Lock()


def handle_send(body: dict, caller: str, request_id: str) -> tuple[int, dict]:
    """Returns (http_status, json_body). Every governance outcome maps to a
    status a caller can act on; a refusal is never reported as success."""
    to = body.get("to")
    if not to:
        return 400, {"error": "bad_request", "detail": "'to' is required"}
    args = {
        "inbox_id": body.get("inbox_id") or INBOX,
        "to": [to] if isinstance(to, str) else list(to),
        "subject": body.get("subject") or "(no subject)",
        "text": body.get("text") or "",
    }
    if SESSION_PER_REQUEST:
        with _session_lock, mail.session(fail_on_error=False):
            return _governed_send(args, caller, request_id, body)
    return _governed_send(args, caller, request_id, body)


def _governed_send(args: dict, caller: str, request_id: str, body: dict) -> tuple[int, dict]:
    try:
        # WHY this send is happening - recorded before it happens.
        mail.emit_trigger(
            "api_request",
            {"request_id": request_id, "reason": body.get("reason"), "to": args["to"]},
            source=caller,
        )
        res = mail.inboxes.messages.send(**args)
        if isinstance(res, PendingApproval):
            return 202, {"status": "pending_approval", "draft_id": res.draft_id,
                         "detail": "staged as a draft; awaiting a human decision"}
        return 200, {"status": "sent", "message_id": getattr(res, "message_id", None)}
    except AgentMailHaltedError as e:
        return 503, {"error": "halted", "reason": str(e),
                     "detail": "session halted by policy; restart the service"}
    except AgentMailBlockedError as e:
        return 403, {"error": "blocked", "reason": str(e), "policy_id": e.policy_id,
                     "detail": "no email was sent"}
    except (ApprovalRejectedError, ApprovalExpiredError, ApprovalTimeoutError) as e:
        return 403, {"error": "approval_failed", "reason": str(e),
                     "detail": "no email was sent"}
    except ContractError as e:
        return 400, {"error": "invalid_request", "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        # AgentMail itself refused (spam classification, permissions, outage).
        # Governance allowed it; the provider did not. A caller needs to tell
        # those apart, so it is 502, not 403.
        name = type(e).__name__
        if "AgentMail" in name or "Error" in name and hasattr(e, "status_code"):
            return 502, {"error": "provider_rejected", "reason": f"{name}: {e}"[:400],
                         "detail": "OpenBox allowed it; AgentMail refused to send"}
        raise


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(200, {"status": "ok", "workflow_id": mail.governor.workflow_id})
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/send":
            self._reply(404, {"error": "not_found"})
            return
        try:
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except (ValueError, json.JSONDecodeError) as e:
            self._reply(400, {"error": "invalid_json", "reason": str(e)})
            return
        caller = self.headers.get("x-caller") or "unknown-caller"
        request_id = self.headers.get("x-request-id") or "-"
        try:
            status, payload = handle_send(body, caller, request_id)
        except Exception as e:  # noqa: BLE001 - never leak a stack trace to a caller
            print(f"  !! unhandled: {type(e).__name__}: {e}")
            status, payload = 500, {"error": "internal_error"}
        print(f"  {caller} -> POST /send -> {status} {payload.get('status') or payload.get('error')}")
        self._reply(status, payload)

    def log_message(self, *args) -> None:  # quieter default logging
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"governed mail API on http://127.0.0.1:{PORT}")
    print("  POST /send   {\"to\": \"...\", \"subject\": \"...\", \"text\": \"...\"}")
    print(f"  GET  /health\ninbox: {INBOX}\nCtrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("\nworkflow_id:", mail.governor.workflow_id)
        mail.close()
        print("session closed")
