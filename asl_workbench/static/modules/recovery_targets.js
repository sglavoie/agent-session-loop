/* Recovery target normalization, navigation and command copying. */

import {
  DETAIL_FOCUS_TARGETS,
  RECOVERY_ACTION_TARGETS,
  RECOVERY_ACTION_TARGETS_BY_ID,
  RECOVERY_TARGET_LABELS,
} from "./constants.js";
import { handleLaunchClick, handleUnlockClick } from "./controls.js";

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
