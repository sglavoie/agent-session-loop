/* Plan preview panel — the desktop right-pane summary.
 *
 * Keyed off the selected plan, falling back to the highest-priority visible
 * one, so the pane is never blank while the queue has something to say.
 */

import {
  detailHashForPlan,
  planHint,
  planIdOf,
  planLabel,
  shortenHome,
} from "./identity.js";
import { setField, setText } from "./panels.js";
import { navigateToDetail } from "./router.js";
import { applyShellLayout } from "./shell.js";
import { state } from "./state.js";
import { toEpochMs } from "./time.js";
import { formatCounts, formatRepositoryAliases, describeAction } from "./queue_rows.js";

export const ACTIVITY_SOURCE_LABELS = {
  "active-verification": "active verification",
  "active-turn": "active turn",
  "turn-record": "turn record",
  "run-finished": "run finished",
  "run-started": "run started",
  "lock-created": "lock created",
  "progress-journal": "progress journal",
};

export function previewPlanFor(visibleSorted) {
  if (!Array.isArray(visibleSorted) || visibleSorted.length === 0) {
    return null;
  }
  if (state.selectedPlan) {
    const match = visibleSorted.find(
      (plan) => plan && planIdOf(plan) === state.selectedPlan
    );
    if (match) return match;
  }
  // visibleSorted is already ordered by the active sort mode (urgency
  // first by default), so the first entry is the highest-priority plan.
  return visibleSorted[0] || null;
}

export function updatePreview(visibleSorted) {
  const next = previewPlanFor(visibleSorted);
  state.previewPlan = next;
  renderPreview(next);
  applyShellLayout();
}

export function renderPreview(plan) {
  const panel = document.getElementById("wb-shell-preview");
  if (!panel) return;
  if (!plan) return;

  const labelEl = document.getElementById("wb-preview-label");
  if (labelEl) {
    setText(labelEl, planLabel(plan));
  }
  const pathEl = document.getElementById("wb-preview-path");
  // The Pi has no path to show, so it shows the digest it addresses by --
  // blank here would read as "this plan is nowhere".
  if (pathEl) {
    setText(pathEl, shortenHome(plan.plan_dir) || planIdOf(plan));
    pathEl.title = plan.plan_dir || planIdOf(plan);
  }

  const stateEl = document.getElementById("wb-preview-state");
  if (stateEl) {
    setText(stateEl, plan.primary_state || "unknown");
    stateEl.setAttribute("data-state", plan.primary_state || "unknown");
  }
  const activityEl = document.getElementById("wb-preview-activity");
  if (activityEl) {
    if (typeof plan.activity_label === "string" && plan.activity_label) {
      activityEl.hidden = false;
      setText(activityEl, plan.activity_label);
    } else {
      activityEl.hidden = true;
      setText(activityEl, "");
    }
  }
  const sourceEl = document.getElementById("wb-preview-activity-source");
  if (sourceEl) {
    const source = plan.last_activity_source;
    if (typeof source === "string" && source) {
      sourceEl.hidden = false;
      sourceEl.setAttribute("data-source", source);
      const label = ACTIVITY_SOURCE_LABELS[source] || source;
      setText(sourceEl, "from " + label);
    } else {
      sourceEl.hidden = true;
      sourceEl.setAttribute("data-source", "");
      setText(sourceEl, "");
    }
  }
  const reasonEl = document.getElementById("wb-preview-reason");
  if (reasonEl) {
    const reason =
      (typeof plan.concise_reason === "string" && plan.concise_reason) ||
      plan.detail ||
      "";
    if (reason) {
      reasonEl.hidden = false;
      setText(reasonEl, reason);
    } else {
      reasonEl.hidden = true;
      setText(reasonEl, "");
    }
  }
  const actionEl = document.getElementById("wb-preview-action");
  if (actionEl) {
    const hint = planHint(plan);
    const action = plan.recommended_action
      ? describeAction(plan.recommended_action)
      : "";
    const parts = [];
    if (action) parts.push("action: " + action);
    if (hint) parts.push(hint);
    const text = parts.join(" — ");
    if (text) {
      actionEl.hidden = false;
      setText(actionEl, text);
    } else {
      actionEl.hidden = true;
      setText(actionEl, "");
    }
  }

  const facts = document.getElementById("wb-preview-facts");
  if (facts) {
    setField(
      facts,
      "next",
      plan.next_pending_session_id
        ? "session " + plan.next_pending_session_id
        : null
    );
    setField(facts, "counts", formatCounts(plan.counts));
    setField(facts, "harness", plan.harness || null);
    setField(facts, "run", formatRunId(plan.run_id));
    setFieldTitle(facts, "run", plan.run_id);
    setField(facts, "workspace", plan.workspace_root || plan.cwd || null);
    setField(facts, "repositories", formatRepositoryAliases(plan.repositories));
    setField(facts, "activity", formatActivityAge(plan.last_activity_at));
    setFieldTitle(facts, "activity", formatLastActivity(plan.last_activity_at));
    setField(facts, "completed", formatCompletedAt(plan));
  }

  const pauseSection = document.getElementById("wb-preview-pause");
  const pauseReasonEl = document.getElementById("wb-preview-pause-reason");
  const pauseMessageEl = document.getElementById("wb-preview-pause-message");
  const hasPause =
    (typeof plan.pause_reason === "string" && plan.pause_reason) ||
    (typeof plan.pause_message === "string" && plan.pause_message);
  if (pauseSection) {
    pauseSection.hidden = !hasPause;
  }
  if (pauseReasonEl) {
    setText(pauseReasonEl, plan.pause_reason || "");
  }
  if (pauseMessageEl) {
    setText(pauseMessageEl, plan.pause_message || "");
  }

  const lastSection = document.getElementById(
    "wb-preview-last-message-section"
  );
  const lastBody = document.getElementById("wb-preview-last-message-body");
  const lastPreview =
    typeof plan.last_message_preview === "string" && plan.last_message_preview
      ? plan.last_message_preview
      : "";
  if (lastSection) {
    lastSection.hidden = !lastPreview;
  }
  if (lastBody) {
    setText(lastBody, plainPreview(lastPreview));
  }

  const openEl = document.getElementById("wb-preview-open");
  if (openEl) {
    openEl.setAttribute(
      "href",
      detailHashForPlan(plan)
    );
    openEl.setAttribute("data-plan-dir", planIdOf(plan));
  }
}

/** Put a hover title on a preview fact, or take a stale one off. */
function setFieldTitle(root, name, title) {
  const el = root.querySelector('[data-field="' + name + '"]');
  if (!el) return;
  if (title) {
    el.title = String(title);
  } else {
    el.removeAttribute("title");
  }
}

/**
 * How long ago the last activity was, for the preview's narrow fact cell.
 *
 * The absolute time goes in the cell's tooltip, as it does on the card.
 */
export function formatActivityAge(value, now) {
  const parsed = toEpochMs(value);
  if (parsed === null) return formatLastActivity(value);
  const reference = typeof now === "number" ? now : Date.now();
  return formatAge(Math.max(0, (reference - parsed) / 1000)) + " ago";
}

/**
 * A run id read as the time it started.
 *
 * Run ids lead with a compact UTC stamp (`20261002T233510506873Z-94023`), and
 * the card only ever had room for that stamp's first half. The local time is
 * what an operator compares against; the full id stays in the tooltip.
 */
export function formatRunId(runId) {
  if (typeof runId !== "string" || !runId) return null;
  const match = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})\d*Z/.exec(runId);
  if (!match) return runId;
  const started = new Date(
    Date.UTC(+match[1], +match[2] - 1, +match[3], +match[4], +match[5], +match[6])
  );
  if (!Number.isFinite(started.getTime())) return runId;
  try {
    return started.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch (err) {
    return runId;
  }
}

/**
 * A last-message preview with its Markdown markers taken out.
 *
 * The preview is plain text in a narrow box, so `**bold**` and backticks read
 * as noise. It is also cut short server side, which can leave a marker
 * unpaired, so leftovers are dropped after the pairs are.
 */
export function plainPreview(text) {
  if (typeof text !== "string" || !text) return "";
  return text
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^(\s*)[-*]\s+/gm, "$1• ")
    .replace(/\*\*|`/g, "");
}

export function formatLastActivity(value) {
  if (value === null || value === undefined || value === "") {
    return "no recent activity";
  }
  const parsed = toEpochMs(value);
  if (parsed === null) return String(value);
  try {
    return new Date(parsed).toLocaleString();
  } catch (err) {
    return String(value);
  }
}

/**
 * Fill one fact on a queue card, and drop the fact when there is nothing to say.
 *
 * A card is read at a glance: a grid where half the cells say "-" pushes the
 * hint and the last message below the fold for no information at all.
 */
export function setRowFact(row, field, value, title) {
  const el = row.querySelector('[data-field="' + field + '"]');
  if (!el) return;
  const empty = value === null || value === undefined || value === "" || value === "-";
  el.textContent = empty ? "-" : String(value);
  if (title) {
    el.title = title;
  } else {
    el.removeAttribute("title");
  }
  if (el.parentElement) el.parentElement.hidden = empty;
}

/**
 * How long a plan that is not finished has gone without activity.
 *
 * Complete plans have a completion time instead, and a plan with no recorded
 * activity has nothing to measure from.
 */
export function formatIdle(plan, now) {
  if (!plan || plan.primary_state === "complete") return null;
  const parsed = toEpochMs(plan.last_activity_at);
  if (parsed === null) return null;
  const reference = typeof now === "number" ? now : Date.now();
  return formatAge(Math.max(0, (reference - parsed) / 1000));
}

/** A coarse age: two units at most, since this is read rather than computed with. */
export function formatAge(seconds) {
  const total = Math.floor(seconds);
  if (total < 60) return total + "s";
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return minutes + "m";
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + "h " + (minutes % 60) + "m";
  const days = Math.floor(hours / 24);
  return days + "d " + (hours % 24) + "h";
}

export function formatCompletedAt(plan) {
  // Completion time is only meaningful for complete plans. Non-complete
  // plans show "-"; complete plans with no recorded timestamp show
  // "not recorded" so the empty state is distinguishable from a real time.
  if (!plan || plan.primary_state !== "complete") return "-";
  const value = plan.completed_at;
  if (value === null || value === undefined || value === "") return "not recorded";
  const parsed = toEpochMs(value);
  if (parsed === null) return String(value);
  try {
    return new Date(parsed).toLocaleString();
  } catch (err) {
    return String(value);
  }
}

export function wirePreviewOpen() {
  const openEl = document.getElementById("wb-preview-open");
  if (!openEl) return;
  openEl.addEventListener("click", (event) => {
    const planDir = openEl.getAttribute("data-plan-dir");
    if (!planDir) return;
    event.preventDefault();
    navigateToDetail(planDir);
  });
}

export {
  classifyEmptyState,
  describeEmptyState,
  renderEmptyState,
} from "./queue_empty.js";
export {
  buildRow,
  selectPlanFromRow,
  syncSelectedPlanView,
  markRowSelected,
  populateRow,
  populateCleanupCell,
  displayName,
  sessionProgress,
  formatCounts,
  setProgressMeter,
  formatRepositoryAliases,
  describeAction,
  populateArtifacts,
} from "./queue_rows.js";
