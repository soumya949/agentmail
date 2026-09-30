"""Structured ``CONSTRAIN`` handling for email (ARCHITECTURE.md §6, P6).

OpenBox may answer CONSTRAIN with ``constraints=[{...}]``. Until Core defines
a formal email schema this SDK recognises a conservative vocabulary:

    {"type": "max_recipients", "count": N}        recipient cap on sends
    {"type": "allowed_domains", "domains": [...]} recipient allowlist
    {"type": "strip_attachments"}                 drop attachments before send
    {"type": "force_bcc", "address": "audit@x"}   append a BCC recipient
    {"type": "require_approval"}                  escalate to approval

Constraints that can be satisfied are applied to the real call arguments and
recorded on the activity for the dashboard. A constraint the call violates
(over the cap, outside the allowlist) or an unknown type makes the caller
FAIL CLOSED — ``MailGovernor`` turns the ``ConstraintViolation`` into an
``AgentMailBlockedError``. It does not escalate to approval: Core registers an
approval for ``REQUIRE_APPROVAL`` only, so polling on a CONSTRAIN would wait
forever (verified live, Sep 2026).

Reality check: the OpenBox rule builder currently cannot author constraints at
all — a CONSTRAIN rule only ever emits ``["run_in_sandbox"]``, which is
meaningless for email. In practice this whole vocabulary is unreachable and a
CONSTRAIN rule always refuses. Use REQUIRE APPROVAL or BLOCK for mail.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import domain_of

__all__ = ["ConstraintViolation", "apply_constraints", "describe_constraints"]


# Core-level directives aimed at code execution. They are valid constraints,
# just meaningless for "send an email" — naming them gives the operator a far
# better error than "unknown constraint type".
_NOT_APPLICABLE_TO_EMAIL = frozenset({"run_in_sandbox", "sandbox", "dry_run"})


class ConstraintViolation(Exception):
    """A constraint cannot be satisfied by rewriting the call.

    The governor converts this into ``AgentMailBlockedError`` — fail closed.
    It is never escalated to a human: see the module docstring."""


def _as_list(value: Any) -> list[str]:
    if value is None or value is Ellipsis:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _recipient_domains(args: Mapping[str, Any]) -> set[str]:
    out: set[str] = set()
    for f in ("to", "cc", "bcc"):
        for r in _as_list(args.get(f)):
            if "@" in r:
                out.add(domain_of(r))
    return out


def apply_constraints(args: dict[str, Any], constraints: list[dict[str, Any]] | None) -> tuple[dict[str, Any], list[str]]:
    """Apply satisfiable constraints to send arguments.

    Returns ``(new_args, applied_descriptions)``. Raises
    ``ConstraintViolation`` when a constraint can't be satisfied or isn't
    recognised — callers FAIL CLOSED (never escalate; see module docstring).

    ``constraints`` accepts Core's bare-string form (``["run_in_sandbox"]``) as
    well as objects; strings are normalised to ``{"type": <string>}``."""
    if not constraints:
        return args, []
    out = dict(args)
    applied: list[str] = []
    for raw in constraints:
        # Core sends bare strings (e.g. "run_in_sandbox") as well as objects;
        # normalise so a string constraint is looked up like {"type": "..."}.
        c: Mapping[str, Any] = {"type": raw} if isinstance(raw, str) else raw
        if not isinstance(c, Mapping):
            raise ConstraintViolation(f"unrecognised constraint {raw!r}")
        ctype = str(c.get("type", ""))
        if ctype == "max_recipients":
            cap = c.get("count")
            if not isinstance(cap, int):
                raise ConstraintViolation("max_recipients needs an integer 'count'")
            total = sum(len(_as_list(out.get(f))) for f in ("to", "cc", "bcc"))
            if total > cap:
                raise ConstraintViolation(f"recipient count {total} exceeds max_recipients {cap}")
            applied.append(f"max_recipients<={cap}")
        elif ctype == "allowed_domains":
            domains = {str(d).lower() for d in (c.get("domains") or [])}
            bad = _recipient_domains(out) - domains
            if bad:
                raise ConstraintViolation(f"recipient domains outside allowed set: {sorted(bad)}")
            applied.append(f"allowed_domains={sorted(domains)}")
        elif ctype == "strip_attachments":
            if out.get("attachments"):
                out["attachments"] = []
            applied.append("strip_attachments")
        elif ctype == "force_bcc":
            address = c.get("address")
            if not isinstance(address, str) or "@" not in address:
                raise ConstraintViolation("force_bcc needs an 'address'")
            bcc = _as_list(out.get("bcc"))
            if address not in bcc:
                out["bcc"] = [*bcc, address]
            applied.append(f"force_bcc={address}")
        elif ctype in ("require_approval", "approval"):
            raise ConstraintViolation("constraint requires human approval")
        elif ctype in _NOT_APPLICABLE_TO_EMAIL:
            raise ConstraintViolation(
                f"constraint {ctype!r} has no meaning for an email action — there is no "
                "code to run. Use REQUIRE APPROVAL or BLOCK for mail instead"
            )
        else:
            raise ConstraintViolation(f"unknown constraint type {ctype!r}")
    return out, applied


def describe_constraints(constraints: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """JSON-safe echo of applied constraints for activity metadata."""
    return [dict(c) for c in constraints if isinstance(c, Mapping)]
