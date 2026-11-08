/* Conversation recovery loading and report-only rendering. */

import { apiRequest, describeError } from "./api.js";
import { planReadGuard, planUrl } from "./identity.js";
import { redirectStageLine } from "./redirect.js";
import { state } from "./state.js";
import { renderRecoveryCommandRows } from "./recovery_render.js";

// ---------------------------------------------------------------------
// Conversation recovery — reported, never performed
//
// Both pauses this covers hand a decision to a person: one discards an
// agent's accumulated context under a one-use authorization, the other has
// no authorization at all. So this panel renders the decision, the commands
// to copy, and the corrections a fresh start would still carry — and offers
// no button that performs any of it.

export async function loadAndRenderConversationRecovery() {
  const stale = planReadGuard();
  try {
    const body = await apiRequest(
      planUrl("/api/conversation-recovery")
    );
    if (stale()) return;
    if (body && body.ok === true) {
      state.conversationRecovery = body;
      renderConversationRecovery(body);
    } else {
      renderConversationRecoveryError(
        (body && body.error) || "conversation recovery unavailable"
      );
    }
  } catch (err) {
    if (stale()) return;
    renderConversationRecoveryError(describeError(err));
  }
}

export function hideConversationRecoveryPanel() {
  const panel = document.getElementById("wb-conversation-recovery");
  if (panel) panel.hidden = true;
  state.conversationRecovery = null;
}

export function renderConversationRecoveryError(message) {
  state.conversationRecovery = null;
  const panel = document.getElementById("wb-conversation-recovery");
  if (panel) panel.hidden = false;
  const facts = document.getElementById("wb-conversation-facts");
  if (facts) {
    facts.textContent = "";
    facts.appendChild(
      conversationFactRow("Recovery", String(message), "wb-conversation__fact-row")
    );
  }
  const note = document.getElementById("wb-conversation-note");
  if (note) {
    note.hidden = true;
    note.textContent = "";
  }
  const commands = document.getElementById("wb-conversation-commands");
  if (commands) commands.textContent = "";
  const retained = document.getElementById("wb-conversation-retained");
  if (retained) {
    retained.textContent = "";
    retained.appendChild(emptyListItem("unknown while recovery is unreadable"));
  }
}

export function renderConversationRecovery(payload) {
  const panel = document.getElementById("wb-conversation-recovery");
  if (panel) panel.hidden = false;
  const facts = document.getElementById("wb-conversation-facts");
  if (facts) {
    facts.textContent = "";
    facts.appendChild(
      conversationFactRow(
        "Decision",
        payload.decision || "none recorded",
        "wb-conversation__fact-row"
      )
    );
    facts.appendChild(
      conversationFactRow(
        "Pause reason",
        payload.pause_reason || "not paused",
        "wb-conversation__fact-row"
      )
    );
    // Stated rather than implied: neither pause clears itself, so "paused"
    // must never read here as "relaunch and see".
    facts.appendChild(
      conversationFactRow(
        "Automatic resume",
        payload.auto_resume === true ? "yes" : "no",
        "wb-conversation__fact-row"
      )
    );
    facts.appendChild(
      conversationFactRow(
        "Authorized by",
        payload.authorized_by || "operator",
        "wb-conversation__fact-row"
      )
    );
  }
  const note = document.getElementById("wb-conversation-note");
  if (note) {
    const text = payload.one_use === true ? payload.note || "" : "";
    note.hidden = !text;
    note.textContent = text;
  }
  renderRecoveryCommandRows(
    document.getElementById("wb-conversation-commands"),
    conversationRecoveryCommands(payload)
  );
  renderConversationRetained(payload.retained_operations);
}

export function conversationRecoveryCommands(payload) {
  const commands = [];
  if (payload.inspect_command) {
    commands.push({
      label: "Inspect recovery",
      command: payload.inspect_command,
      description: "Read the recorded binding and journal; writes nothing.",
    });
  }
  if (payload.authorization_command) {
    commands.push({
      label: "Authorize fresh conversation",
      command: payload.authorization_command,
      // The one-use warning is rendered once, above, where it applies to the
      // decision rather than to the line an operator is about to paste.
      description: "Discards the agent's saved context and starts over.",
    });
  }
  return commands;
}

export function renderConversationRetained(operations) {
  const list = document.getElementById("wb-conversation-retained");
  if (!list) return;
  list.textContent = "";
  const items = Array.isArray(operations) ? operations : [];
  if (items.length === 0) {
    list.appendChild(emptyListItem("no corrections outstanding"));
    return;
  }
  items.forEach((operation) => {
    const li = document.createElement("li");
    li.className = "wb-conversation__retained-row";
    li.setAttribute("data-stage", operation.stage || "unknown");
    const id = document.createElement("span");
    id.className = "wb-redirect__operation-id";
    id.textContent = operation.operation_id || "(unknown id)";
    const detail = document.createElement("span");
    detail.className = "wb-redirect__operation-detail";
    detail.textContent = redirectStageLine(operation);
    li.appendChild(id);
    li.appendChild(detail);
    list.appendChild(li);
  });
}

export function conversationFactRow(label, value, className) {
  const li = document.createElement("li");
  li.className = className;
  const labelEl = document.createElement("span");
  labelEl.className = "wb-recovery__label";
  labelEl.textContent = label;
  const valueEl = document.createElement("span");
  valueEl.className = "wb-recovery__value";
  valueEl.textContent = String(value);
  li.appendChild(labelEl);
  li.appendChild(valueEl);
  return li;
}

export function emptyListItem(text) {
  const li = document.createElement("li");
  li.className = "wb-empty";
  li.textContent = text;
  return li;
}
