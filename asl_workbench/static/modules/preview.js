/* Plan preview panel — the desktop right-pane summary.
 *
 * Keyed off the selected plan, falling back to the highest-priority visible
 * one, so the pane is never blank while the queue has something to say.
 */

import { planIsSelectable, setCleanupSelected } from "./cleanup.js";
import {
  detailHashForPlan,
  planHint,
  planIdOf,
  planLabel,
} from "./identity.js";
import { setField } from "./panels.js";
import { setSelectedPlan } from "./prefs.js";
import { matchesFilter } from "./queue.js";
import { handleRowAction, navigateToDetail } from "./router.js";
import { applyShellLayout } from "./shell.js";
import { state } from "./state.js";

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
    labelEl.textContent = planLabel(plan);
  }
  const pathEl = document.getElementById("wb-preview-path");
  // The Pi has no path to show, so it shows the digest it addresses by --
  // blank here would read as "this plan is nowhere".
  if (pathEl) pathEl.textContent = plan.plan_dir || planIdOf(plan);

  const stateEl = document.getElementById("wb-preview-state");
  if (stateEl) {
    stateEl.textContent = plan.primary_state || "unknown";
    stateEl.setAttribute("data-state", plan.primary_state || "unknown");
  }
  const activityEl = document.getElementById("wb-preview-activity");
  if (activityEl) {
    if (typeof plan.activity_label === "string" && plan.activity_label) {
      activityEl.hidden = false;
      activityEl.textContent = plan.activity_label;
    } else {
      activityEl.hidden = true;
      activityEl.textContent = "";
    }
  }
  const sourceEl = document.getElementById("wb-preview-activity-source");
  if (sourceEl) {
    const source = plan.last_activity_source;
    if (typeof source === "string" && source) {
      sourceEl.hidden = false;
      sourceEl.setAttribute("data-source", source);
      const label = ACTIVITY_SOURCE_LABELS[source] || source;
      sourceEl.textContent = "from " + label;
    } else {
      sourceEl.hidden = true;
      sourceEl.setAttribute("data-source", "");
      sourceEl.textContent = "";
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
      reasonEl.textContent = reason;
    } else {
      reasonEl.hidden = true;
      reasonEl.textContent = "";
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
      actionEl.textContent = text;
    } else {
      actionEl.hidden = true;
      actionEl.textContent = "";
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
    setField(facts, "run", plan.run_id || null);
    setField(facts, "workspace", plan.workspace_root || plan.cwd || null);
    setField(facts, "repositories", formatRepositoryAliases(plan.repositories));
    setField(facts, "activity", formatLastActivity(plan.last_activity_at));
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
    pauseReasonEl.textContent = plan.pause_reason || "";
  }
  if (pauseMessageEl) {
    pauseMessageEl.textContent = plan.pause_message || "";
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
    lastBody.textContent = lastPreview;
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

export function formatLastActivity(value) {
  if (typeof value !== "string" || !value) return "no recent activity";
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return value;
  try {
    return new Date(parsed).toLocaleString();
  } catch (err) {
    return value;
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
  const value = plan.last_activity_at;
  if (typeof value !== "string" || !value) return null;
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return null;
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
  if (typeof value !== "string" || !value) return "not recorded";
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return value;
  try {
    return new Date(parsed).toLocaleString();
  } catch (err) {
    return value;
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

/** The facet narrowings in force, worded for the operator. */
function activeFacets() {
  const active = [];
  if (state.projectFilter) active.push("project: " + state.projectFilter);
  if (state.harnessFilter) active.push("harness: " + state.harnessFilter);
  return active;
}

export function classifyEmptyState(plansList) {
  const facets = activeFacets();
  if (!Array.isArray(plansList) || plansList.length === 0) {
    // An empty list is no longer evidence that the machine is empty. Since the
    // narrowing moved to the server, every filter that matches nothing arrives
    // here as zero rows -- the ones that did not match were never sent. Saying
    // "no plans discovered" under a project filter would tell an operator
    // their corpus is gone when they have merely filtered it away, and it
    // would say so with no button to undo.
    if (facets.length || state.searchText || state.activeFilter !== "all") {
      const tail = facets.slice();
      if (state.activeFilter !== "all") {
        tail.push("filter: " + state.activeFilter);
      }
      return {
        kind: state.searchText ? "no-search-results" : "no-filter-matches",
        primary: state.searchText
          ? 'no plans match search "' + state.searchText + '"'
          : "no plans match the current filters",
        secondary: tail.length ? "active: " + tail.join(" | ") : "",
        searchText: state.searchText,
        activeFilter: state.activeFilter,
        showCompleted: state.showCompleted,
        // Unknowable from here for the same reason: a completed plan hidden by
        // this request was never sent, so the page cannot say whether one
        // exists. Offering the toggle is the honest move -- it widens the
        // question rather than claiming an answer.
        completedHidden: false,
        canClearSearch: !!state.searchText,
        canShowCompleted:
          !state.showCompleted && state.activeFilter !== "complete",
        canResetFilter: facets.length > 0 || state.activeFilter !== "all",
      };
    }
    return {
      kind: "no-plans",
      primary: "no plans discovered",
      secondary: "",
      searchText: "",
      activeFilter: state.activeFilter,
      showCompleted: state.showCompleted,
      completedHidden: false,
      canClearSearch: false,
      canShowCompleted: false,
      canResetFilter: false,
    };
  }
  const completedHiding =
    !state.showCompleted && state.activeFilter !== "complete";
  const completedExists = plansList.some(
    (plan) =>
      (plan && plan.urgency_group === "complete") ||
      (plan && plan.primary_state === "complete")
  );
  const filterMatchedAtAll = plansList.some((plan) =>
    matchesFilter(plan, state.activeFilter)
  );
  const completedHidden = completedHiding && completedExists;

  if (state.searchText) {
    const primary = 'no plans match search "' + state.searchText + '"';
    const tail = facets.slice();
    if (state.activeFilter !== "all") {
      tail.push("filter: " + state.activeFilter);
    }
    if (completedHidden) tail.push("completed hidden");
    return {
      kind: "no-search-results",
      primary: primary,
      secondary: tail.length ? "active: " + tail.join(" | ") : "",
      searchText: state.searchText,
      activeFilter: state.activeFilter,
      showCompleted: state.showCompleted,
      completedHidden: completedHidden,
      canClearSearch: true,
      canShowCompleted: completedHidden,
      canResetFilter: facets.length > 0 || state.activeFilter !== "all",
    };
  }

  if (!filterMatchedAtAll && state.activeFilter !== "all") {
    const tail = facets.slice();
    if (completedHidden) tail.push("completed hidden");
    return {
      kind: "no-filter-matches",
      primary: 'no plans match filter "' + state.activeFilter + '"',
      secondary: tail.length ? "active: " + tail.join(" | ") : "",
      searchText: "",
      activeFilter: state.activeFilter,
      showCompleted: state.showCompleted,
      completedHidden: completedHidden,
      canClearSearch: false,
      canShowCompleted: completedHidden,
      canResetFilter: true,
    };
  }

  if (completedHidden) {
    return {
      kind: "completed-hidden",
      primary: "no actionable plans — completed hidden",
      secondary: "toggle “show completed” to see the rest",
      searchText: "",
      activeFilter: state.activeFilter,
      showCompleted: state.showCompleted,
      completedHidden: true,
      canClearSearch: false,
      canShowCompleted: true,
      canResetFilter: facets.length > 0 || state.activeFilter !== "all",
    };
  }

  return {
    kind: "no-actionable",
    primary: "no actionable plans",
    secondary: "",
    searchText: "",
    activeFilter: state.activeFilter,
    showCompleted: state.showCompleted,
    completedHidden: false,
    canClearSearch: false,
    canShowCompleted: false,
    canResetFilter: state.activeFilter !== "all",
  };
}

export function describeEmptyState(plansList) {
  const info = classifyEmptyState(plansList);
  const parts = [info.primary];
  if (info.secondary) parts.push(info.secondary);
  return parts.join(" | ");
}

export function renderEmptyState(plansList) {
  const emptyEl = document.getElementById("wb-empty");
  if (!emptyEl) return;
  const info = classifyEmptyState(plansList);
  emptyEl.hidden = false;
  emptyEl.setAttribute("data-state", info.kind);

  const primaryEl = document.getElementById("wb-empty-primary");
  if (primaryEl) primaryEl.textContent = info.primary;

  const secondaryEl = document.getElementById("wb-empty-secondary");
  if (secondaryEl) {
    if (info.secondary) {
      secondaryEl.hidden = false;
      secondaryEl.textContent = info.secondary;
    } else {
      secondaryEl.hidden = true;
      secondaryEl.textContent = "";
    }
  }

  const actionsEl = document.getElementById("wb-empty-actions");
  const clearSearchBtn = document.getElementById("wb-empty-clear-search");
  const showCompletedBtn = document.getElementById("wb-empty-show-completed");
  const resetFilterBtn = document.getElementById("wb-empty-reset-filter");
  if (clearSearchBtn) clearSearchBtn.hidden = !info.canClearSearch;
  if (showCompletedBtn) showCompletedBtn.hidden = !info.canShowCompleted;
  if (resetFilterBtn) resetFilterBtn.hidden = !info.canResetFilter;
  if (actionsEl) {
    const anyVisible =
      info.canClearSearch || info.canShowCompleted || info.canResetFilter;
    actionsEl.hidden = !anyVisible;
  }
}

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
    row.setAttribute(
      "data-selected",
      dir === state.selectedPlan ? "true" : "false"
    );
  });
  updatePreview(state.lastVisiblePlans || []);
}

export function populateRow(row, plan) {
  const badge = row.querySelector('[data-field="state"]');
  badge.textContent = plan.primary_state || "unknown";
  badge.setAttribute("data-state", plan.primary_state || "unknown");

  const label = row.querySelector('[data-field="label"]');
  label.textContent = planLabel(plan);
  label.setAttribute("href", detailHashForPlan(plan));

  const path = row.querySelector('[data-field="path"]');
  path.textContent = plan.plan_dir || planIdOf(plan);

  setRowFact(
    row,
    "session",
    plan.next_pending_session_id ? "next " + plan.next_pending_session_id : null
  );
  setRowFact(row, "harness", plan.harness);
  setRowFact(row, "idle", formatIdle(plan), formatLastActivity(plan.last_activity_at));
  setRowFact(row, "run", plan.run_id);
  setRowFact(row, "counts", formatCounts(plan.counts));
  setRowFact(row, "completed", formatCompletedAt(plan));

  const hint = row.querySelector('[data-field="hint"]');
  hint.textContent = planHint(plan);

  const preview = row.querySelector('[data-field="preview"]');
  if (plan.last_message_preview) {
    preview.hidden = false;
    preview.textContent = plan.last_message_preview;
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

export function formatCounts(counts) {
  if (!counts || typeof counts !== "object") return "-";
  const parts = [];
  if (typeof counts.pending === "number") parts.push("p" + counts.pending);
  if (typeof counts.complete === "number") parts.push("c" + counts.complete);
  if (typeof counts.empty === "number" && counts.empty > 0) {
    parts.push("e" + counts.empty);
  }
  if (typeof counts.unknown === "number" && counts.unknown > 0) {
    parts.push("u" + counts.unknown);
  }
  return parts.length ? parts.join(" / ") : "-";
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
