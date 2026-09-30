/** Action catalogue — mirrors sdk/src/openbox_agentmail/catalog.py exactly. */

export enum ActionClass {
  SEND = "send",
  DRAFT = "draft",
  MODIFY = "modify",
  ADMIN = "admin",
  READ = "read",
  READ_ATTACHMENT = "read_attachment",
  UNKNOWN = "unknown",
}

const WRITE_CLASSES = new Set([
  ActionClass.SEND,
  ActionClass.DRAFT,
  ActionClass.MODIFY,
  ActionClass.ADMIN,
  ActionClass.UNKNOWN,
]);
const READ_CLASSES = new Set([ActionClass.READ, ActionClass.READ_ATTACHMENT]);

export const isWrite = (c: ActionClass) => WRITE_CLASSES.has(c);
export const isRead = (c: ActionClass) => READ_CLASSES.has(c);

export interface ActionSpec {
  action: string;
  activityType: string;
  actionClass: ActionClass;
  resource: string;
  method: string;
}

export const ACTIVITY_PREFIX = "agentmail.";

const FRIENDLY: Record<string, string> = {
  "messages.send": "send_message",
  "messages.reply": "reply_to_message",
  "messages.reply_all": "reply_all",
  "messages.forward": "forward_message",
  "messages.get": "get_message",
  "messages.get_raw": "get_raw_message",
  "messages.batch_get": "batch_get_messages",
  "messages.list": "list_messages",
  "messages.search": "search_messages",
  "messages.update": "update_message",
  "messages.batch_update": "batch_update_messages",
  "messages.delete": "delete_message",
  "messages.get_attachment": "get_attachment",
  "drafts.create": "create_draft",
  "drafts.update": "update_draft",
  "drafts.send": "send_draft",
  "drafts.delete": "delete_draft",
  "drafts.get": "get_draft",
  "drafts.list": "list_drafts",
  "drafts.get_attachment": "get_attachment",
  "threads.get": "get_thread",
  "threads.list": "list_threads",
  "threads.search": "search_threads",
  "threads.update": "update_thread",
  "threads.delete": "delete_thread",
  "threads.get_attachment": "get_attachment",
  "inboxes.create": "create_inbox",
  "inboxes.delete": "delete_inbox",
  "inboxes.get": "get_inbox",
  "inboxes.list": "list_inboxes",
  "inboxes.search": "search_inboxes",
  "inboxes.update": "update_inbox",
};

const SEND_METHODS = new Set(["send", "reply", "reply_all", "forward"]);
const READ_METHODS = new Set([
  "get", "list", "search", "batch_get", "get_raw", "me", "query_events",
  "query_rates", "query_usage", "get_setup_link", "get_zone_file",
  "list_accounts", "get_headers",
]);
const MODIFY_METHODS = new Set(["delete", "update", "batch_update"]);
const MAIL_RESOURCES = new Set(["messages", "drafts", "threads"]);
const ADMIN_RESOURCES = new Set([
  "inboxes", "pods", "domains", "api_keys", "webhooks", "lists", "accounts",
  "providers", "organizations", "agent", "auth", "metrics", "events", "websockets",
]);

function classify(resource: string, method: string): ActionClass {
  if (method === "get_attachment") return ActionClass.READ_ATTACHMENT;
  if (MAIL_RESOURCES.has(resource)) {
    if (SEND_METHODS.has(method)) return ActionClass.SEND;
    if (resource === "drafts" && (method === "create" || method === "update")) return ActionClass.DRAFT;
    if (MODIFY_METHODS.has(method)) return ActionClass.MODIFY;
    if (READ_METHODS.has(method)) return ActionClass.READ;
    return ActionClass.UNKNOWN;
  }
  if (ADMIN_RESOURCES.has(resource)) {
    return READ_METHODS.has(method) ? ActionClass.READ : ActionClass.ADMIN;
  }
  return ActionClass.UNKNOWN;
}

const TRANSPARENT = new Set(["with_raw_response", "with_options", "withOptions", "withRawResponse"]);

/** Resolve a client attribute path like ["inboxes", "messages", "send"].
 *  Throws RangeError when unclassifiable — callers refuse, never pass through. */
export function lookup(path: string[]): ActionSpec {
  const clean = path.filter((s) => !TRANSPARENT.has(s));
  if (clean.length < 2) throw new RangeError(path.join("."));
  const [resource, method] = [clean[clean.length - 2], clean[clean.length - 1]];
  const actionClass = classify(resource, method);
  if (actionClass === ActionClass.UNKNOWN) throw new RangeError(path.join("."));
  const action = FRIENDLY[`${resource}.${method}`] ?? `${resource}.${method}`;
  return { action, activityType: `${ACTIVITY_PREFIX}${action}`, actionClass, resource, method };
}

/** Hosted MCP tool names -> catalogue path. Mirrors catalog.py::_MCP_TOOLS. */
const MCP_TOOLS: Record<string, [string, string]> = {
  send_message: ["messages", "send"],
  reply_to_message: ["messages", "reply"],
  reply_all: ["messages", "reply_all"],
  forward_message: ["messages", "forward"],
  get_message: ["messages", "get"],
  list_messages: ["messages", "list"],
  search_messages: ["messages", "search"],
  update_message: ["messages", "update"],
  delete_message: ["messages", "delete"],
  get_attachment: ["messages", "get_attachment"],
  create_draft: ["drafts", "create"],
  update_draft: ["drafts", "update"],
  send_draft: ["drafts", "send"],
  delete_draft: ["drafts", "delete"],
  get_draft: ["drafts", "get"],
  list_drafts: ["drafts", "list"],
  get_thread: ["threads", "get"],
  list_threads: ["threads", "list"],
  search_threads: ["threads", "search"],
  update_thread: ["threads", "update"],
  delete_thread: ["threads", "delete"],
  create_inbox: ["inboxes", "create"],
  delete_inbox: ["inboxes", "delete"],
  get_inbox: ["inboxes", "get"],
  list_inboxes: ["inboxes", "list"],
  search_inboxes: ["inboxes", "search"],
  update_inbox: ["inboxes", "update"],
  list_list_entries: ["lists", "list"],
  get_list_entry: ["lists", "get"],
  create_list_entry: ["lists", "create"],
  delete_list_entry: ["lists", "delete"],
  agent_verify: ["agent", "verify"],
  list_providers: ["providers", "list"],
  search_providers: ["providers", "search"],
  get_provider: ["providers", "get"],
  connect_provider: ["providers", "connect"],
  list_accounts: ["accounts", "list"],
  auth_me: ["auth", "me"],
  list_organizations: ["organizations", "list"],
  select_organization: ["organizations", "select"],
};

const TYPE_TO_CLASS: Record<string, ActionClass> = {
  email_send: ActionClass.SEND,
  email_draft: ActionClass.DRAFT,
  email_read: ActionClass.READ,
  email_attachment_read: ActionClass.READ_ATTACHMENT,
  email_modify: ActionClass.MODIFY,
  email_admin: ActionClass.ADMIN,
};

export function classifyMcpTool(toolName: string, toolTypeMap?: Record<string, string>): ActionSpec {
  const mapped = toolTypeMap?.[toolName];
  if (mapped) {
    const cls =
      TYPE_TO_CLASS[mapped] ??
      (Object.values(ActionClass).includes(mapped as ActionClass) ? (mapped as ActionClass) : ActionClass.UNKNOWN);
    return { action: toolName, activityType: `${ACTIVITY_PREFIX}${toolName}`, actionClass: cls, resource: "mcp", method: toolName };
  }
  const path = MCP_TOOLS[toolName];
  if (path) {
    const spec = lookup([...path]);
    return { action: toolName, activityType: `${ACTIVITY_PREFIX}${toolName}`, actionClass: spec.actionClass, resource: spec.resource, method: spec.method };
  }
  return { action: toolName, activityType: `${ACTIVITY_PREFIX}${toolName}`, actionClass: ActionClass.UNKNOWN, resource: "mcp", method: toolName };
}
