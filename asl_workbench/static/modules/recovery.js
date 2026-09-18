/* Recovery Center loading, and the conversation-recovery group.
 *
 * The compact recovery projection on a queue row renders first and the full
 * `/api/recovery` read replaces it, so the panel is populated immediately and
 * accurate a moment later. Conversation recovery is reported and never
 * performed: it renders the decision and the commands, and offers no button
 * that carries either out.
 */

import { apiRequest, describeError } from "./api.js";
import {
  DETAIL_FOCUS_TARGETS,
  RECOVERY_ACTION_TARGETS,
  RECOVERY_ACTION_TARGETS_BY_ID,
  RECOVERY_TARGET_LABELS,
} from "./constants.js";
import { handleLaunchClick, handleUnlockClick } from "./controls.js";
import { planUrl, routePlanId } from "./identity.js";
import { hideReplyPanel, loadAndRenderReply } from "./panels.js";
import { redirectStageLine } from "./redirect.js";
import { state } from "./state.js";

export async function loadAndRenderRecovery(plan) {
  const planId = routePlanId();
  const compact = plan && plan.recovery ? plan.recovery : null;
  let renderedRecovery = null;
  if (compact) {
    renderedRecovery = compact;
    // The compact projection omits evidence and artifacts. Replacing an
    // already populated panel with it on every poll collapses the detail
    // layout until the full recovery request completes.
    if (state.recoveryPanelPlanId !== planId) {
      renderRecoveryPanel(compact);
      state.recoveryPanelPlanId = planId;
    }
  }
  try {
    const body = await apiRequest(
      planUrl("/api/recovery")
    );
    if (body && body.ok === true && body.recovery) {
      renderedRecovery = body.recovery;
      renderRecoveryPanel(body.recovery);
      state.recoveryPanelPlanId = planId;
    } else if (!compact) {
      renderRecoveryError(
        (body && body.error) || "recovery diagnosis unavailable"
      );
      renderedRecovery = null;
    }
  } catch (err) {
    if (compact) {
      const fallback = Object.assign({}, compact, {
        warnings: [
          "Recovery details failed to load: " + describeError(err),
        ],
      });
      renderedRecovery = fallback;
      renderRecoveryPanel(fallback);
    } else {
      renderRecoveryError(describeError(err));
      renderedRecovery = null;
    }
  }
  if (renderedRecovery && renderedRecovery.kind === "reply-needed") {
    await loadAndRenderReply();
  } else {
    hideReplyPanel();
  }
  if (renderedRecovery && renderedRecovery.kind === "conversation-recovery") {
    await loadAndRenderConversationRecovery();
  } else {
    hideConversationRecoveryPanel();
  }
}

// ---------------------------------------------------------------------
// Conversation recovery — reported, never performed
//
// Both pauses this covers hand a decision to a person: one discards an
// agent's accumulated context under a one-use authorization, the other has
// no authorization at all. So this panel renders the decision, the commands
// to copy, and the corrections a fresh start would still carry — and offers
// no button that performs any of it.

export async function loadAndRenderConversationRecovery() {
  try {
    const body = await apiRequest(
      planUrl("/api/conversation-recovery")
    );
    if (body && body.ok === true) {
      state.conversationRecovery = body;
      renderConversationRecovery(body);
    } else {
      renderConversationRecoveryError(
        (body && body.error) || "conversation recovery unavailable"
      );
    }
  } catch (err) {
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

// ---------------------------------------------------------------------
// Recovery Center panel rendering

export function renderRecoveryError(message) {
  renderRecoveryPanel({
    kind: "generic",
    title: "Recovery unavailable",
    summary: String(message || "recovery diagnosis unavailable"),
    severity: "warning",
    recoverable: false,
    evidence: [],
    actions: [],
    commands: [],
    artifacts: [],
    warnings: [],
  });
}

export function renderRecoveryPanel(recovery) {
  const panel = document.getElementById("wb-recovery-panel");
  if (!panel || !recovery) return;
  const kind = String(recovery.kind || "generic");
  const severity = recoverySeverity(recovery.severity);
  panel.setAttribute("data-severity", severity);

  const titleEl = document.getElementById("wb-recovery-title");
  if (titleEl) titleEl.textContent = recovery.title || "Recovery Center";
  const kindEl = document.getElementById("wb-recovery-kind");
  if (kindEl) {
    kindEl.textContent = kind;
    kindEl.setAttribute("data-kind", kind);
  }
  const summaryEl = document.getElementById("wb-recovery-summary");
  if (summaryEl) {
    summaryEl.textContent =
      recovery.summary || "No recovery diagnosis is available.";
  }

  const actions = Array.isArray(recovery.actions) ? recovery.actions : [];
  const primaryAction =
    recovery.primary_action ||
    (recovery.compact && recovery.compact.primary_action) ||
    null;
  renderRecoveryActions(
    actions.length ? actions : primaryAction ? [primaryAction] : []
  );

  const evidence = Array.isArray(recovery.evidence)
    ? recovery.evidence.slice()
    : [];
  if (Array.isArray(recovery.warnings)) {
    recovery.warnings.forEach((warning) => {
      evidence.push({
        label: "Warning",
        value: warning,
        detail: null,
        severity: "warning",
      });
    });
  }
  renderRecoveryEvidence(evidence);
  renderRecoveryCommands(
    Array.isArray(recovery.commands) ? recovery.commands : []
  );
  renderRecoveryArtifacts(
    Array.isArray(recovery.artifacts) ? recovery.artifacts : []
  );
}

export function recoverySeverity(value) {
  return ["none", "info", "warning", "error"].indexOf(value) === -1
    ? "none"
    : value;
}

// ---------------------------------------------------------------------
// Recovery Center actions

export function renderRecoveryActions(actions) {
  const host = document.getElementById("wb-recovery-primary-action");
  if (!host) return;
  const cards = actions.filter(Boolean);
  host.innerHTML = "";
  if (!cards.length) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  cards.forEach((action, index) => {
    host.appendChild(createRecoveryActionCard(action, index === 0));
  });
}

export function renderRecoveryPrimaryAction(action) {
  renderRecoveryActions(action ? [action] : []);
}

export function createRecoveryActionCard(action, primary) {
  const card = document.createElement("div");
  card.className = "wb-recovery__action-card";
  if (primary) card.setAttribute("data-primary", "true");
  if (action.disabled) card.setAttribute("data-disabled", "true");

  const text = document.createElement("div");
  const title = document.createElement("p");
  title.className = "wb-recovery__action-title";
  title.textContent = action.label || "Primary action";
  text.appendChild(title);
  if (action.description) {
    const description = document.createElement("p");
    description.className = "wb-recovery__action-description";
    description.textContent = action.description;
    text.appendChild(description);
  }
  card.appendChild(text);

  const target = recoveryActionTarget(action);
  const button = document.createElement("button");
  button.className = "wb-button wb-button--primary";
  button.type = "button";
  button.textContent = recoveryActionButtonLabel(action, target);
  button.disabled = Boolean(
    action.disabled || !recoveryActionCanRun(action, target)
  );
  button.addEventListener("click", () => {
    handleRecoveryAction(action);
  });
  card.appendChild(button);
  return card;
}

export function recoveryActionCanRun(action, target) {
  if (!action) return false;
  const endpoint = action.endpoint ? String(action.endpoint) : "";
  const id = action.id ? String(action.id) : "";
  if (endpoint === "/api/unlock" || id === "unlock") return true;
  if (
    endpoint === "/api/launch" ||
    id === "retry-run" ||
    id === "launch"
  ) {
    return true;
  }
  if (endpoint === "/api/reply" || id === "reply") return Boolean(target);
  return Boolean(target);
}

export function recoveryActionButtonLabel(action, target) {
  const id = action && action.id ? String(action.id) : "";
  if (id === "unlock" || target === "unlock") return "unlock";
  if (id === "reply" || target === "reply") return "reply";
  if (id === "review-profile" || target === "profile") return "profile";
  if (id === "retry-run" || target === "launch") return "launch";
  if (action && action.kind === "inspect") return "inspect";
  return "open";
}

// ---------------------------------------------------------------------
// Recovery Center evidence

export function renderRecoveryEvidence(rows) {
  const section = document.getElementById("wb-recovery-evidence-section");
  const list = document.getElementById("wb-recovery-evidence");
  if (!section || !list) return;
  list.innerHTML = "";
  if (!rows.length) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  rows.forEach((row) => {
    const li = document.createElement("li");
    li.className = "wb-recovery__evidence-row";
    li.setAttribute("data-severity", recoveryEvidenceSeverity(row.severity));

    const label = document.createElement("span");
    label.className = "wb-recovery__label";
    label.textContent = row.label || "Evidence";
    li.appendChild(label);

    const value = document.createElement("span");
    value.className = "wb-recovery__value";
    value.textContent =
      row.value === null || row.value === undefined || row.value === ""
        ? "-"
        : String(row.value);
    if (row.detail) {
      const detail = document.createElement("span");
      detail.className = "wb-recovery__detail";
      detail.textContent = row.detail;
      value.appendChild(detail);
    }
    li.appendChild(value);

    const target = recoveryEvidenceTarget(row);
    if (target) {
      li.appendChild(createRecoveryLink(target));
    }
    list.appendChild(li);
  });
}

export function recoveryEvidenceSeverity(value) {
  return ["info", "warning", "error"].indexOf(value) === -1
    ? "info"
    : value;
}

// ---------------------------------------------------------------------
// Recovery Center commands

export function renderRecoveryCommands(commands) {
  const section = document.getElementById("wb-recovery-commands-section");
  const list = document.getElementById("wb-recovery-commands");
  if (!section || !list) return;
  list.innerHTML = "";
  if (!commands.length) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  renderRecoveryCommandRows(list, commands);
}

// Shared by the Recovery Center and the conversation-recovery group: both
// present a command to copy rather than a button that runs it.
export function renderRecoveryCommandRows(list, commands) {
  if (!list) return;
  list.innerHTML = "";
  commands.forEach((command) => {
    const li = document.createElement("li");
    li.className = "wb-recovery__command-row";

    const label = document.createElement("span");
    label.className = "wb-recovery__label";
    label.textContent = command.label || "Command";
    li.appendChild(label);

    const body = document.createElement("span");
    const code = document.createElement("code");
    code.className = "wb-recovery__command";
    code.textContent = command.command || "";
    body.appendChild(code);
    if (command.description) {
      const description = document.createElement("span");
      description.className = "wb-recovery__command-description";
      description.textContent = command.description;
      body.appendChild(description);
    }
    li.appendChild(body);

    const button = document.createElement("button");
    button.className = "wb-button";
    button.type = "button";
    button.textContent = "copy";
    button.addEventListener("click", () => {
      copyRecoveryCommand(button, command.command || "");
    });
    li.appendChild(button);

    list.appendChild(li);
  });
}

// ---------------------------------------------------------------------
// Recovery Center artifacts

export function renderRecoveryArtifacts(artifacts) {
  const section = document.getElementById("wb-recovery-artifacts-section");
  const list = document.getElementById("wb-recovery-artifacts");
  if (!section || !list) return;
  list.innerHTML = "";
  if (!artifacts.length) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  artifacts.forEach((artifact) => {
    const li = document.createElement("li");
    li.className = "wb-recovery__artifact-row";

    const label = document.createElement("span");
    label.className = "wb-recovery__label";
    label.textContent = artifact.label || "Artifact";
    li.appendChild(label);

    const path = document.createElement("code");
    path.className = "wb-recovery__artifact-path";
    path.textContent = artifact.path || "";
    li.appendChild(path);

    const meta = document.createElement("span");
    meta.className = "wb-recovery__artifact-meta";
    meta.textContent = [artifact.kind, artifact.source]
      .filter(Boolean)
      .join(" · ");
    const target = recoveryArtifactTarget(artifact);
    if (target) {
      if (meta.textContent) meta.appendChild(document.createTextNode(" "));
      meta.appendChild(createRecoveryLink(target));
    }
    li.appendChild(meta);
    list.appendChild(li);
  });
}

// ---------------------------------------------------------------------
// Recovery Center target resolution

export function createRecoveryLink(target) {
  const button = document.createElement("button");
  button.className = "wb-recovery__link";
  button.type = "button";
  button.textContent = RECOVERY_TARGET_LABELS[target] || "view";
  button.addEventListener("click", () => focusDetailKey(target));
  return button;
}

export function isRecoveryActionTarget(target) {
  return RECOVERY_ACTION_TARGETS.indexOf(target) !== -1;
}

export function normalizeRecoveryActionTarget(target) {
  const value =
    target === null || target === undefined ? "" : String(target).trim();
  if (!value || !isRecoveryActionTarget(value)) return null;
  return Object.prototype.hasOwnProperty.call(DETAIL_FOCUS_TARGETS, value)
    ? value
    : null;
}

export function hasExplicitRecoveryActionTarget(action) {
  return Boolean(
    action &&
      action.target !== null &&
      action.target !== undefined &&
      String(action.target).trim() !== ""
  );
}

export function mappedRecoveryActionTarget(action) {
  if (!action || !action.id) return null;
  return normalizeRecoveryActionTarget(
    RECOVERY_ACTION_TARGETS_BY_ID[String(action.id)] || null
  );
}

export function legacyRecoveryActionTarget(action) {
  const haystack = recoveryText([
    action.id,
    action.label,
    action.description,
    action.endpoint,
  ]);
  if (haystack.indexOf("reply") !== -1) return "reply";
  if (haystack.indexOf("unlock") !== -1) return "unlock";
  if (
    haystack.indexOf("launch") !== -1 ||
    haystack.indexOf("retry") !== -1
  ) {
    return "launch";
  }
  if (
    haystack.indexOf("profile") !== -1 ||
    haystack.indexOf("harness") !== -1 ||
    haystack.indexOf("config") !== -1
  ) {
    return "profile";
  }
  if (
    haystack.indexOf("verify") !== -1 ||
    haystack.indexOf("contract") !== -1 ||
    haystack.indexOf("check") !== -1
  ) {
    return "verification";
  }
  if (haystack.indexOf("stderr") !== -1) return "stderr";
  if (haystack.indexOf("stdout") !== -1) return "stdout";
  if (haystack.indexOf("log") !== -1) return "stderr";
  if (
    haystack.indexOf("artifact") !== -1 ||
    haystack.indexOf("inspect") !== -1
  ) {
    return "artifacts";
  }
  return null;
}

export function recoveryActionTarget(action) {
  if (!action) return null;
  const explicit = normalizeRecoveryActionTarget(action.target);
  if (explicit) return explicit;
  if (hasExplicitRecoveryActionTarget(action)) return null;
  const mapped = mappedRecoveryActionTarget(action);
  if (mapped) return mapped;
  return legacyRecoveryActionTarget(action);
}

export function recoveryEvidenceTarget(row) {
  const haystack = recoveryText([row.label, row.value, row.detail]);
  if (haystack.indexOf("reply") !== -1) return "reply";
  if (haystack.indexOf("event") !== -1) return "events";
  if (haystack.indexOf("stderr") !== -1) return "stderr";
  if (haystack.indexOf("stdout") !== -1) return "stdout";
  if (haystack.indexOf("log") !== -1) return "stderr";
  if (
    haystack.indexOf("result") !== -1 ||
    haystack.indexOf("artifact") !== -1 ||
    haystack.indexOf("state.json") !== -1 ||
    haystack.indexOf("last-message") !== -1
  ) {
    return "artifacts";
  }
  if (
    haystack.indexOf("verify") !== -1 ||
    haystack.indexOf("verification") !== -1 ||
    haystack.indexOf("contract") !== -1 ||
    haystack.indexOf("check") !== -1
  ) {
    return "verification";
  }
  if (
    haystack.indexOf("progress") !== -1 ||
    haystack.indexOf("journal") !== -1
  ) {
    return "progress";
  }
  if (
    haystack.indexOf("profile") !== -1 ||
    haystack.indexOf("harness") !== -1 ||
    haystack.indexOf("config") !== -1
  ) {
    return "profile";
  }
  if (haystack.indexOf("lock") !== -1) return "unlock";
  if (haystack.indexOf("active") !== -1) return "active";
  return null;
}

export function recoveryArtifactTarget(artifact) {
  const haystack = recoveryText([
    artifact.label,
    artifact.path,
    artifact.kind,
    artifact.source,
  ]);
  if (haystack.indexOf("events") !== -1) return "events";
  if (haystack.indexOf("progress") !== -1) return "progress";
  if (haystack.indexOf("stdout") !== -1) return "stdout";
  if (haystack.indexOf("stderr") !== -1) return "stderr";
  if (haystack.indexOf("result") !== -1) return "artifacts";
  if (haystack.indexOf("state") !== -1) return "active";
  return "artifacts";
}

export function recoveryText(parts) {
  return parts
    .filter((part) => part !== null && part !== undefined)
    .map((part) => String(part).toLowerCase())
    .join(" ");
}

// ---------------------------------------------------------------------
// Recovery Center dispatch

export async function handleRecoveryAction(action) {
  if (!action || action.disabled) return;
  const endpoint = action.endpoint ? String(action.endpoint) : "";
  const id = action.id ? String(action.id) : "";
  if (endpoint === "/api/unlock" || id === "unlock") {
    await handleUnlockClick();
    return;
  }
  if (
    endpoint === "/api/launch" ||
    id === "retry-run" ||
    id === "launch"
  ) {
    await handleLaunchClick();
    return;
  }
  const target = recoveryActionTarget(action);
  if (target) focusDetailKey(target);
}

export function focusDetailKey(focusKey) {
  const target = DETAIL_FOCUS_TARGETS[focusKey];
  if (!target) return;
  const scrollEl = target.scroll
    ? document.getElementById(target.scroll)
    : null;
  if (scrollEl && typeof scrollEl.scrollIntoView === "function") {
    scrollEl.scrollIntoView({ block: "start", behavior: "smooth" });
  }
  if (target.focus) {
    const focusEl = document.getElementById(target.focus);
    if (focusEl && typeof focusEl.focus === "function") {
      try {
        focusEl.focus({ preventScroll: true });
      } catch (err) {
        focusEl.focus();
      }
    }
  }
}

// ---------------------------------------------------------------------
// Recovery Center copy UX

export async function copyRecoveryCommand(button, commandText) {
  if (!commandText) return;
  const original = button.textContent;
  button.disabled = true;
  try {
    await copyText(commandText);
    button.textContent = "copied";
  } catch (err) {
    button.textContent = "failed";
  } finally {
    window.setTimeout(() => {
      button.textContent = original || "copy";
      button.disabled = false;
    }, 1200);
  }
}

export async function copyText(text) {
  if (
    window.navigator &&
    window.navigator.clipboard &&
    typeof window.navigator.clipboard.writeText === "function"
  ) {
    await window.navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-1000px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  document.body.removeChild(textarea);
  if (!copied) throw new Error("copy failed");
}
