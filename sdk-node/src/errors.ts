export class OpenBoxError extends Error {}
export class OpenBoxConfigError extends OpenBoxError {}
export class OpenBoxAuthError extends OpenBoxError {}
export class GovernanceAPIError extends OpenBoxError {}
export class ContractError extends OpenBoxError {
  constructor(message: string, public code = "AGENTMAIL_CONTRACT", public detail: unknown = null) {
    super(message);
  }
}
export class GuardrailsValidationError extends OpenBoxError {
  constructor(public reasons: string[]) {
    super(reasons.join("; ") || "Guardrails validation failed");
  }
}
export class ApprovalRejectedError extends OpenBoxError {}
export class ApprovalExpiredError extends OpenBoxError {}
export class ApprovalTimeoutError extends OpenBoxError {}

export interface PatchDirective {
  new_input?: unknown;
  governance_event_id?: string | null;
  reason?: string | null;
}

export class AgentMailBlockedError extends OpenBoxError {
  constructor(
    reason: string,
    public activityType = "",
    public policyId: string | null = null,
    public patch: PatchDirective | null = null,
  ) {
    super(reason);
  }
  /** Merge a Core patch over the original args for a retry. The retried call
   *  is evaluated fresh — never a bypass. */
  patchedArgs(original: Record<string, unknown>): Record<string, unknown> | null {
    const ni = this.patch?.new_input;
    if (ni === null || ni === undefined || typeof ni !== "object" || Array.isArray(ni)) return null;
    return { ...original, ...(ni as Record<string, unknown>) };
  }
}

export class AgentMailHaltedError extends OpenBoxError {
  constructor(reason: string, public activityType = "") {
    super(reason);
  }
}

export class UncataloguedActionError extends OpenBoxConfigError {}
