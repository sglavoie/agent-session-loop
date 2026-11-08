/* Queue row rendering, selection and artifact summaries. */

import { planIsSelectable, setCleanupSelected } from "./cleanup.js";
import {
  detailHashForPlan,
  planHint,
  planIdOf,
  planLabel,
  shortenHome,
} from "./identity.js";
import { setSelectedPlan } from "./prefs.js";
import { handleRowAction } from "./router.js";
import { state } from "./state.js";
import {
  updatePreview,
  formatRunId,
  plainPreview,
  formatLastActivity,
  setRowFact,
  formatIdle,
  formatCompletedAt,
} from "./preview.js";

export function buildRow(planDir) {
  const tmpl = document.getElementById("wb-row-template");
  const fragment = tmpl.content.cloneNode(true);
  const row = fragment.querySelector(".wb-row");
  row.setAttribute("data-plan-dir", planDir);
  row.addEventListener("click", (event) => {
    // Let the label link and action button keep their explicit navigation
    // behavior; clicks anywhere else on the row select the plan so the
    // preview pane can update without leaving the queue.
    const target = event.target;
    if (!target || typeof target.closest !== "function") return;
    if (target.closest('[data-field="label"]')) return;
    if (target.closest('[data-field="action"]')) return;
    if (target.closest('[data-field="cleanup-select"]')) return;
    selectPlanFromRow(planDir);
  });
  row.addEventListener("keydown", (event) => {
    // The keyboard twin of the click above, for the row itself only: the
    // label link, the action button and the checkbox inside it already answer
    // Enter and Space as themselves.
    if (event.target !== row) return;
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    selectPlanFromRow(planDir);
  });
  const cleanupBox = row.querySelector(
    '[data-field="cleanup-select"] input'
  );
  if (cleanupBox) {
    cleanupBox.addEventListener("change", () => {
      setCleanupSelected(planDir, cleanupBox.checked);
    });
  }
  return row;
}

export function selectPlanFromRow(planDir) {
  const changed = setSelectedPlan(planDir);
  if (!changed) return;
  syncSelectedPlanView();
}

export function syncSelectedPlanView() {
  // Re-render row selection markers and the preview against the cached
  // visible list so the operator does not have to wait for the next
  // queue poll cycle.
  state.rowsByPlan.forEach((row, dir) => {
    markRowSelected(row, dir === state.selectedPlan);
  });
  updatePreview(state.lastVisiblePlans || []);
}

/**
 * Mark a queue row as the selected one, for the eye and for a screen reader.
 *
 * `data-selected` drives the styling; `aria-current` is what an assistive
 * technology announces on the focusable row, which would otherwise say
 * nothing about which plan the preview is showing.
 */
export function markRowSelected(row, selected) {
  row.setAttribute("data-selected", selected ? "true" : "false");
  if (selected) {
    row.setAttribute("aria-current", "true");
  } else {
    row.removeAttribute("aria-current");
  }
}

export function populateRow(row, plan) {
  const badge = row.querySelector('[data-field="state"]');
  badge.textContent = plan.primary_state || "unknown";
  badge.setAttribute("data-state", plan.primary_state || "unknown");

  const label = row.querySelector('[data-field="label"]');
  label.textContent = planLabel(plan);
  label.setAttribute("href", detailHashForPlan(plan));

  const path = row.querySelector('[data-field="path"]');
  path.textContent = shortenHome(plan.plan_dir) || planIdOf(plan);
  path.title = plan.plan_dir || planIdOf(plan);

  setRowFact(
    row,
    "session",
    plan.next_pending_session_id ? "next " + plan.next_pending_session_id : null
  );
  setRowFact(row, "harness", plan.harness);
  setRowFact(row, "idle", formatIdle(plan), formatLastActivity(plan.last_activity_at));
  setRowFact(row, "run", formatRunId(plan.run_id), plan.run_id);
  setRowFact(row, "counts", formatCounts(plan.counts));
  setProgressMeter(row, plan.counts);
  setRowFact(row, "completed", formatCompletedAt(plan));

  const hint = row.querySelector('[data-field="hint"]');
  hint.textContent = planHint(plan);

  const preview = row.querySelector('[data-field="preview"]');
  if (plan.last_message_preview) {
    preview.hidden = false;
    preview.textContent = plainPreview(plan.last_message_preview);
  } else {
    preview.hidden = true;
    preview.textContent = "";
  }

  const warningsEl = row.querySelector('[data-field="warnings"]');
  const warnings = Array.isArray(plan.warnings) ? plan.warnings : [];
  if (warnings.length === 0) {
    warningsEl.hidden = true;
    warningsEl.innerHTML = "";
  } else {
    warningsEl.hidden = false;
    warningsEl.innerHTML = "";
    warnings.forEach((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      warningsEl.appendChild(li);
    });
  }

  const action = row.querySelector('[data-field="action"]');
  action.textContent = describeAction(plan.recommended_action);
  action.title = "Recommended action: " + (plan.recommended_action || "view");
  action.setAttribute("data-action-kind", plan.recommended_action || "view");
  action.onclick = (event) => handleRowAction(plan, event);

  populateCleanupCell(row, plan);
  populateArtifacts(row, plan);
}

export function populateCleanupCell(row, plan) {
  const cell = row.querySelector('[data-field="cleanup-select"]');
  if (!cell) return;
  const box = cell.querySelector("input");
  cell.hidden = !state.cleanupMode;
  const archivable = planIsSelectable(plan);
  if (box) {
    box.checked = state.cleanupSelection.has(planIdOf(plan));
    box.disabled = !archivable;
  }
  if (archivable) {
    cell.removeAttribute("title");
    cell.removeAttribute("data-blocked");
  } else {
    cell.setAttribute("data-blocked", "true");
    cell.title =
      "not archivable: " + (plan.archive_blocker || "active-lock");
  }
}

export function displayName(planDir) {
  if (!planDir) return "(unknown plan)";
  const parts = planDir.split("/");
  return parts[parts.length - 1] || planDir;
}

/** Sessions done out of the plan's total, from the manifest's status counts. */
export function sessionProgress(counts) {
  if (!counts || typeof counts !== "object") return null;
  let total = 0;
  let seen = false;
  ["pending", "complete", "empty", "unknown"].forEach((key) => {
    if (typeof counts[key] === "number") {
      total += counts[key];
      seen = true;
    }
  });
  if (!seen || total === 0) return null;
  const done = typeof counts.complete === "number" ? counts.complete : 0;
  return { done: done, total: total };
}

export function formatCounts(counts) {
  const progress = sessionProgress(counts);
  if (!progress) return "-";
  const parts = [progress.done + " / " + progress.total + " done"];
  if (typeof counts.empty === "number" && counts.empty > 0) {
    parts.push(counts.empty + " empty");
  }
  if (typeof counts.unknown === "number" && counts.unknown > 0) {
    parts.push(counts.unknown + " unknown");
  }
  return parts.join(", ");
}

/** The thin bar under a card's progress fact. */
export function setProgressMeter(row, counts) {
  const meter = row.querySelector('[data-field="meter"]');
  if (!meter) return;
  const progress = sessionProgress(counts);
  meter.hidden = !progress;
  const ratio = progress ? progress.done / progress.total : 0;
  meter.style.setProperty("--wb-meter", String(Math.round(ratio * 100)) + "%");
}

export function formatRepositoryAliases(repositories) {
  if (!Array.isArray(repositories) || repositories.length === 0) return null;
  const limit = 12;
  const aliases = repositories
    .slice(0, limit)
    .map((repository) => repository && repository.alias)
    .filter(Boolean)
    .map(String);
  if (repositories.length > limit) {
    aliases.push("+" + String(repositories.length - limit) + " more");
  }
  return aliases.length ? aliases.join(", ") : null;
}

export function describeAction(action) {
  const map = {
    watch: "watch",
    reply: "reply",
    "inspect-failure": "inspect",
    unlock: "unlock",
    view: "view",
    run: "run",
    "repair-plan": "repair",
  };
  if (action && Object.prototype.hasOwnProperty.call(map, action)) {
    return map[action];
  }
  return "view";
}

export function populateArtifacts(row, plan) {
  const list = row.querySelector('[data-field="artifacts"]');
  list.innerHTML = "";
  const links = [
    { label: "events", value: plan.events_path },
    { label: "state", value: plan.state_path },
    { label: "progress", value: plan.progress_path },
    { label: "last-msg", value: plan.last_message_path },
    { label: "stdout", value: plan.stdout_path },
    { label: "stderr", value: plan.stderr_path },
  ];
  let added = 0;
  links.forEach((entry) => {
    if (!entry.value) return;
    const li = document.createElement("li");
    li.textContent = entry.label;
    li.title = entry.value;
    list.appendChild(li);
    added += 1;
  });
  if (added === 0) {
    const li = document.createElement("li");
    li.textContent = "no artifacts yet";
    list.appendChild(li);
  }
}
