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
  shortenHome,
} from "./identity.js";
import { setField, setText } from "./panels.js";
import { setSelectedPlan } from "./prefs.js";
import { matchesFilter } from "./queue.js";
import { handleRowAction, navigateToDetail } from "./router.js";
import { applyShellLayout } from "./shell.js";
import { state } from "./state.js";
import { toEpochMs } from "./time.js";

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
