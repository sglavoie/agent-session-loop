/* Recovery panels, action cards, evidence, commands and artifacts. */

import { shortenStatePath } from "./identity.js";
import { applyActionAvailability } from "./operations.js";
import {
  createRecoveryLink,
  recoveryActionTarget,
  recoveryEvidenceTarget,
  recoveryArtifactTarget,
  handleRecoveryAction,
  copyRecoveryCommand,
} from "./recovery_targets.js";

/** The action name a recovery card's button is, when it is a mutation.
 *
 * Only the two that act. Everything else a card offers is navigation -- it
 * scrolls a pane into view and focuses it -- and navigation is available in
 * every mode, so naming it here would mean disabling a button that works. */
const RECOVERY_CARD_ACTIONS = {
  "/api/unlock": "unlock",
  "/api/launch": "launch",
  unlock: "unlock",
  launch: "launch",
  "retry-run": "launch",
};

function recoveryCardAction(action) {
  if (!action) return null;
  const endpoint = action.endpoint ? String(action.endpoint) : "";
  const id = action.id ? String(action.id) : "";
  return RECOVERY_CARD_ACTIONS[endpoint] || RECOVERY_CARD_ACTIONS[id] || null;
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

  // A row that only restates the summary printed just above it adds a second
  // copy of the same sentence and nothing else.
  const summaryText = String(recovery.summary || "").trim();
  const evidence = (Array.isArray(recovery.evidence) ? recovery.evidence : []).filter(
    (row) =>
      !row ||
      row.detail ||
      !summaryText ||
      String(row.value === null || row.value === undefined ? "" : row.value).trim() !==
        summaryText
  );
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
  // Asked after the cards are in the document, because the pass reads the
  // nodes: a card the deployment cannot act on is shut here with the same
  // sentence the controls bar would have shown for it.
  applyActionAvailability(host);
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
  // Read by the stylesheet: a reply card is folded away while the reply form
  // it would scroll to is already on screen right below it.
  card.setAttribute("data-target", target || "");
  const button = document.createElement("button");
  button.className = "wb-button wb-button--primary";
  button.type = "button";
  button.textContent = recoveryActionButtonLabel(action, target);
  const name = recoveryCardAction(action);
  if (name) {
    const reason = document.createElement("p");
    reason.className = "wb-recovery__action-description";
    reason.setAttribute("data-operation-reason", "");
    reason.hidden = true;
    text.appendChild(reason);
    button.setAttribute("data-operation-action", name);
    // The card's own verdict is written as a reason rather than as `disabled`,
    // so the availability pass keeps it instead of re-enabling a card the
    // diagnosis itself said not to offer.
    if (action.disabled || !recoveryActionCanRun(action, target)) {
      button.setAttribute(
        "data-blocked-reason",
        action.disabled_reason || "this recovery step is not offered right now"
      );
    }
  }
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
    path.textContent = shortenStatePath(artifact.path || "");
    path.title = artifact.path || "";
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
    // The shortened path is for reading; the copy is the whole of it.
    if (artifact.path) {
      const copy = document.createElement("button");
      copy.className = "wb-recovery__link";
      copy.type = "button";
      copy.textContent = "copy";
      copy.title = "Copy the full path";
      copy.addEventListener("click", () => {
        copyRecoveryCommand(copy, artifact.path);
      });
      if (meta.childNodes.length) meta.appendChild(document.createTextNode(" · "));
      meta.appendChild(copy);
    }
    li.appendChild(meta);
    list.appendChild(li);
  });
}
