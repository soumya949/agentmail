"""Apply ``guardrails.redacted_input`` returned by OpenBox to AgentMail call
arguments (before the call) or to AgentMail responses (before the agent sees them)."""

from __future__ import annotations

from typing import Any

from openbox_core.contracts.results import EvaluationResult

__all__ = ["redact_args", "redact_result", "REDACTABLE_ARGS"]

# Only content fields are overwritten from a redacted activity_input; recipients
# and identifiers are never rewritten by guardrails.
REDACTABLE_ARGS = ("subject", "text", "html")


def redact_args(args: dict[str, Any], result: EvaluationResult) -> dict[str, Any]:
    gr = result.guardrails
    if not gr or gr.input_type != "activity_input" or not isinstance(gr.redacted_input, dict):
        return args
    out = dict(args)
    for key in REDACTABLE_ARGS:
        if key in gr.redacted_input and key in out and out[key] is not Ellipsis:
            out[key] = gr.redacted_input[key]
    return out


def redact_result(value: Any, result: EvaluationResult) -> Any:
    """Return the redacted response. Pydantic models are re-validated from the
    redacted dict when possible so the caller keeps the same type; otherwise
    the redacted dict itself is returned."""
    gr = result.guardrails
    if not gr or gr.input_type != "activity_output" or gr.redacted_input is None:
        return value
    redacted = gr.redacted_input
    if isinstance(redacted, dict) and hasattr(value, "model_validate"):
        try:
            merged = {**value.model_dump(mode="json", by_alias=True), **redacted}
            merged.pop("action", None)
            return type(value).model_validate(merged)
        except Exception:
            return redacted
    return redacted
