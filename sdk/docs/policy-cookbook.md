# Policy cookbook

What OpenBox policies see for each governed AgentMail call (the `activity_input`
on `ActivityStarted`, `activity_output` on `ActivityCompleted`). Examples in
Rego — adapt to your policy engine's dialect.

```json
// activity_input for a send
{
  "action": "send_message",
  "action_class": "send",
  "surface": "rest",
  "inbox_id": "agent@corp.example",
  "to": ["alice@example.com"],
  "cc": [], "bcc": [],
  "recipient_domains": ["example.com"],
  "recipient_count": 1,
  "external_recipients": true,
  "subject": "Pricing",
  "text": "...", "html": null,
  "html_present": false,
  "content_sha256": "...",
  "attachments": [{"filename": "q.pdf", "content_type": "application/pdf", "size": 42001}],
  "labels": [], "send_at": null,
  "message_id": null, "draft_id": null, "thread_id": null
}
```

## 1. Recipient allowlist

```rego
deny contains msg if {
    input.activity_input.action_class == "send"
    some r in input.activity_input.recipient_domains
    not r in data.allowed_domains
    msg := sprintf("recipient domain %v not allowed", [r])
}
```

## 2. External recipients need approval

```rego
require_approval if {
    input.activity_input.action_class == "send"
    input.activity_input.external_recipients
    input.activity_input.recipient_count > 0
}
```

## 3. Attachment rules

```rego
deny contains msg if {
    input.activity_input.action_class == "send"
    some a in input.activity_input.attachments
    a.content_type == "application/x-msdownload"
    msg := "executable attachments are forbidden"
}

require_approval if {
    input.activity_input.action_class == "send"
    some a in input.activity_input.attachments
    a.size > 5 * 1024 * 1024
}
```

## 4. Bulk-send limit

```rego
deny contains "bulk send over limit" if {
    input.activity_input.action_class == "send"
    input.activity_input.recipient_count > 25
}
```

## 5. Draft-then-approve pattern

Approve `create_draft` automatically; require approval only for the
irreversible step:

```rego
require_approval if {
    input.activity_input.action == "send_draft"
}
```

## 6. Inbound screening (`agentmail.receive_message`)

Each received email is an activity. `ActivityStarted` carries what AgentMail
pushed; `ActivityCompleted` carries the copy fetched with `messages.get`
(`activity_output`, full message: `text`, `extracted_text`, `html`, `headers`,
attachments). BLOCK/HALT/guardrail failure at either stage means the handler
never sees it; REQUIRE_APPROVAL at `ActivityStarted` holds it for a human
before it is even fetched. **CONSTRAIN refuses inbound mail** — a message that
has already arrived cannot be rewritten, so it fails closed (see §11).

```json
// activity_input for agentmail.receive_message
{
  "action": "receive_message", "action_class": "read", "direction": "inbound",
  "surface": "rest", "transport": "websocket", "pod_id": null,
  "agentmail_event_type": "message.received", "agentmail_event_id": "evt_...",
  "inbox_id": "agent@corp.example", "thread_id": "...", "message_id": "...",
  "sender": "Mallory <m@evil.example>", "sender_domain": "evil.example",
  "reply_to": [], "to": ["agent@corp.example"], "cc": [],
  "subject": "...", "labels": ["received"], "size": 2048,
  "attachments": [{"filename": "x.pdf", "content_type": "application/pdf", "size": 1234}],
  "headers": {"authentication-results": "spf=fail ..."},
  "text": "...", "extracted_text": "...", "html_present": false
}
```

With `content_mode="metadata_only"` the body fields are replaced by
`content_sha256` — content guardrails then have nothing to scan.

```rego
deny contains "possible injection" if {
    input.activity_input.action == "receive_message"
    contains(lower(input.activity_input.text), "ignore previous instructions")
}

require_approval if {
    input.activity_input.action == "receive_message"
    contains(input.activity_input.headers["authentication-results"], "spf=fail")
}

deny contains "sender not allowlisted" if {
    input.activity_input.action == "receive_message"
    not input.activity_input.sender_domain in {"partner.example", "corp.example"}
}
```

With `fetch_on_receive=False` the same fields arrive flat on a
`SignalReceived` (`input.signal_name == "agentmail.message_received"`,
`input.text`, `input.headers`, …) — the older shape; approvals on that path
have no activity id.

## 7. Spam / blocked / unauthenticated inbound

```rego
deny contains "quarantine" if {
    input.activity_input.action == "receive_message"
    input.activity_input.agentmail_event_type in {
        "message.received.spam",
        "message.received.blocked",
        "message.received.unauthenticated",
    }
}
```

(Subscribe explicitly to those event types — AgentMail excludes them by default.)

## 8. Guardrails (content transforms)

Guardrails return `redacted_input` with `input_type`:

| `input_type` | Applied where |
| --- | --- |
| `activity_input` | `subject`/`text`/`html` rewritten in the real AgentMail call |
| `activity_output` | read results (`get`, `list`, `search`, attachments) rewritten before the agent sees them |
| `activity_input` on `receive_message` | pushed `text`/`subject` rewritten; applied last, so the fetched copy can't re-expose it |
| `activity_output` on `receive_message` | the fetched message rewritten before your handler |
| `signal` | inbound `text`/`subject` rewritten before your handler (`fetch_on_receive=False`) |

`validation_passed=false` outranks approvals — the operation fails closed.

## 9. Reads are screened too

`get`, `list`, `search`, `get_attachment` produce `activity_output` containing
message fields (`text`, `extracted_text`, `html`, `headers`, attachments).
Output guardrails and completed-stage policies run on it — an email can't
smuggle instructions past governance just because it arrived earlier.

## 10. Admin surface

`create_inbox`, `delete_inbox`, webhook/domain/api-key changes land as
`action_class == "admin"`. Suggested default:

```rego
require_approval if { input.activity_input.action_class == "admin" }
```

## 11. CONSTRAIN does not work for email — use REQUIRE APPROVAL or BLOCK

The SDK implements a constraint vocabulary that rewrites a send to make it
compliant (`force_bcc`, `max_recipients`, `allowed_domains`,
`strip_attachments`, `require_approval`). **You cannot currently reach it.**

The OpenBox rule builder offers a CONSTRAIN decision but no way to author the
constraints themselves, so a CONSTRAIN rule only ever emits:

```json
{"verdict": "constrain", "constraints": ["run_in_sandbox"]}
```

`run_in_sandbox` is a code-execution directive — there is nothing to sandbox in
"send an email". The SDK therefore **fails closed**, raising
`AgentMailBlockedError` and naming the constraint. It deliberately does *not*
escalate to a human: Core registers an approval for `REQUIRE_APPROVAL` only, so
polling would wait forever.

The same applies inbound: a received message cannot be rewritten, so CONSTRAIN
refuses it.

**Write `REQUIRE APPROVAL` or `BLOCK` instead.** `BLOCK AND PATCH` is also
unreachable — the builder pins it to `activity_type == llm_call`.

## 12. Failures: there is no `input.error`

When an AgentMail call fails, the failure is recorded **inside
`activity_output`**, not as a top-level field:

```json
// ActivityCompleted after a failed send
{
  "failed": true,
  "activity_output": {
    "action": "send_message",
    "status": "failed",
    "error": "MessageRejectedError: classified as high-confidence spam"
  }
}
```

There is deliberately **no top-level `error`** — Core rejects any event
carrying one with `HTTP 400 "invalid request body"`, which drops the whole
event and loses the failure record entirely.

So a rule written against `input.error` will never fire. Match this instead:

```rego
deny contains "repeated send failures" if {
    input.event_type == "ActivityCompleted"
    input.failed
    contains(lower(input.activity_output.error), "spam")
}
```

Note the completed stage only affects **future** work — the call already ran.

## 13. Why the agent acted (`agentmail.trigger.*`)

The SDK cannot see what caused an agent to act — an API request, a bot, a cron
tick — so the caller declares it with `emit_trigger()`, and it arrives as a
`SignalReceived` ordered *before* the activities it caused:

```json
{
  "signal_name": "agentmail.trigger.api_request",
  "trigger": "api_request",
  "trigger_source": "billing-service",
  "trigger_data": {"request_id": "req-4471", "reason": "invoice overdue"},
  "direction": "inbound",
  "enforced": true
}
```

`BLOCK`/`HALT` here stop the agent before AgentMail is touched, so you can
refuse to act on an untrusted caller:

```rego
result := {"decision": "BLOCK", "reason": "unrecognised caller"} if {
    input.trigger == "api_request"
    not input.trigger_source in {"billing-service", "support-desk"}
}
```

Caller-supplied values live under `trigger_data` and cannot overwrite
`trigger`, `trigger_source` or `surface`.

`REQUIRE_APPROVAL` and `CONSTRAIN` on a trigger raise `OpenBoxConfigError`: a
signal has no `activity_id`, so an approval poll could never resolve. Put
approval rules on the activity they should gate.

## 14. `applied_constraints`

When a constraint *is* satisfiable, the descriptions of what was applied are
recorded on the activity as `applied_constraints`, so the dashboard shows the
call was rewritten rather than merely allowed. (Unreachable today — see §11.)
