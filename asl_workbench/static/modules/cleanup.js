/* Cleanup mode — bulk review, then archive, of stale plans.
 *
 * Selection is a review step with its own confirmation, and a plan an active
 * lock blocks is never selectable: bulk is a convenience for the reviewing,
 * never a way around the per-plan refusal.
 */

import { apiRequest, OUTCOME_PENDING, OUTCOME_RESULT } from "./api.js";
import { DAY_MS } from "./constants.js";
import { planIdOf, planLabel } from "./identity.js";
import {
  HELD_MESSAGE,
  applyActionAvailability,
  holdOperation,
  releaseOperation,
  runMutation,
} from "./operations.js";
import {
  normalizeThresholdDays,
  persistCleanupMode,
  persistCleanupThreshold,
  setSortMode,
} from "./prefs.js";
import { oldestSortTimestampFor, queueQuery, refresh } from "./queue.js";
import { state } from "./state.js";

export function wireCleanupControls() {
  const toggle = document.getElementById("wb-cleanup-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      setCleanupMode(!state.cleanupMode);
    });
  }
  const threshold = document.getElementById("wb-cleanup-threshold");
  if (threshold) {
    threshold.addEventListener("change", () => {
      const days = normalizeThresholdDays(threshold.value);
      state.cleanupThresholdDays = days;
      threshold.value = String(days);
      persistCleanupThreshold(days);
      refresh();
    });
  }
  const selectCandidatesBtn = document.getElementById(
    "wb-cleanup-select-candidates"
  );
  if (selectCandidatesBtn) {
    selectCandidatesBtn.addEventListener("click", selectCandidates);
  }
  const selectVisible = document.getElementById("wb-cleanup-select-visible");
  if (selectVisible) {
    selectVisible.addEventListener("click", selectVisibleRows);
  }
  const clear = document.getElementById("wb-cleanup-clear");
  if (clear) {
    clear.addEventListener("click", clearCleanupSelection);
  }
  const archiveBtn = document.getElementById("wb-cleanup-archive");
  if (archiveBtn) {
    archiveBtn.addEventListener("click", handleBulkArchive);
  }
  document.getElementById("wb-cleanup-review-cancel")?.addEventListener(
    "click", closeCleanupReview
  );
  document.getElementById("wb-cleanup-review-confirm")?.addEventListener(
    "click", performBulkArchive
  );
}

export function setCleanupMode(enabled) {
  const next = enabled === true;
  if (state.cleanupMode === next) return;
  state.cleanupMode = next;
  persistCleanupMode(next);
  if (next) {
    // Cleanup defaults to the oldest-first ordering so the stalest plans
    // (including never-run ones) sort to the top of the review list.
    if (setSortMode("oldest")) {
      const sortSelect = document.getElementById("wb-sort");
      if (sortSelect) sortSelect.value = "oldest";
    }
  } else {
    state.cleanupSelection.clear();
    state.cleanupCandidates.clear();
    closeCleanupReview();
  }
  applyCleanupMode();
  refresh();
}

export function applyCleanupMode() {
  const bar = document.getElementById("wb-cleanup-bar");
  if (bar) {
    bar.hidden = !state.cleanupMode || state.route.name !== "queue";
  }
  const toggle = document.getElementById("wb-cleanup-toggle");
  if (toggle) {
    toggle.setAttribute(
      "aria-pressed",
      state.cleanupMode ? "true" : "false"
    );
    toggle.textContent = state.cleanupMode ? "exit cleanup" : "cleanup";
  }
  const shell = document.getElementById("wb-shell");
  if (shell) {
    shell.setAttribute("data-cleanup", state.cleanupMode ? "active" : "off");
  }
  updateCleanupReview();
}

export function planIsArchivableCandidate(plan) {
  // Cleanup candidates are non-active plans whose stale age is older than
  // the operator threshold; plans with no timestamp always qualify. Complete
  // plans age off their completion time (completed_at) while everything else
  // uses last activity, matching the completion-aware oldest sort.
  if (!planIsSelectable(plan)) return false;
  const ts = oldestSortTimestampFor(plan);
  if (ts === null) return true;
  const ageDays = (Date.now() - ts) / DAY_MS;
  return ageDays >= state.cleanupThresholdDays;
}

export function planIsSelectable(plan) {
  return !!(
    plan && plan.archivable !== false &&
    !["running", "verifying"].includes(plan.primary_state)
  );
}

export async function selectCandidates() {
  // Fetch every page under one catalog revision. A partial selection is not
  // advertised as "all candidates", and a changed revision is never mixed
  // with entries from the old one.
  const initialQuery = queueQuery();
  const selectButton = document.getElementById("wb-cleanup-select-candidates");
  if (selectButton) selectButton.disabled = true;
  try {
    const first = await apiRequest(initialQuery);
    if (!first || first.ok !== true || !Array.isArray(first.plans)) {
      throw new Error("could not load the plan catalog");
    }
    const revision = first.catalog_revision || null;
    const matched = Number.isInteger(first.catalog_matched)
      ? first.catalog_matched
      : first.plans.length;
    const pages = [first];
    let offset = first.plans.length;
    while (offset < matched) {
      const params = new URLSearchParams(initialQuery.split("?")[1] || "");
      params.set("offset", String(offset));
      const page = await apiRequest("/api/queue?" + params.toString());
      if (!page || page.ok !== true || !Array.isArray(page.plans) ||
          page.plans.length === 0 || (page.catalog_revision || null) !== revision ||
          page.catalog_matched !== matched) {
        throw new Error("the catalog changed while candidates were loading; select again");
      }
      pages.push(page);
      offset += page.plans.length;
    }
    const candidates = new Map();
    pages.forEach((page) => page.plans.forEach((plan) => {
      const id = planIdOf(plan);
      if (id && planIsArchivableCandidate(plan)) candidates.set(id, plan);
    }));
    state.cleanupSelection = new Set(candidates.keys());
    state.cleanupCandidates = candidates;
    state.cleanupSelectionRevision = revision;
    syncCleanupCheckboxes();
    updateCleanupReview();
    showCleanupFeedback("info", candidates.size + " old plans selected from " + matched + " matching plans");
  } catch (error) {
    showCleanupFeedback("error", String(error.message || error));
  } finally {
    if (selectButton) selectButton.disabled = false;
  }
}

export function selectVisibleRows() {
  // "select visible" adds every currently visible archivable row after the
  // active filter/search/sort/completed-visibility gates have run. Rows with
  // an active lock (archivable === false) are skipped, preserving the same
  // blocking the per-row checkbox enforces.
  const visible = state.lastVisiblePlans || [];
  state.cleanupSelectionRevision = state.queueCatalogRevision;
  visible.forEach((plan) => {
    if (planIsSelectable(plan) && planIdOf(plan)) {
      state.cleanupSelection.add(planIdOf(plan));
      state.cleanupCandidates.set(planIdOf(plan), plan);
    }
  });
  syncCleanupCheckboxes();
  updateCleanupReview();
}

export function clearCleanupSelection() {
  if (state.cleanupSelection.size === 0) return;
  state.cleanupSelection.clear();
  state.cleanupCandidates.clear();
  state.cleanupSelectionRevision = null;
  syncCleanupCheckboxes();
  updateCleanupReview();
}

export function setCleanupSelected(planDir, selected) {
  if (!planDir) return;
  if (selected) {
    state.cleanupSelection.add(planDir);
    const plan = (state.lastVisiblePlans || []).find((row) => planIdOf(row) === planDir);
    if (plan) state.cleanupCandidates.set(planDir, plan);
  } else {
    state.cleanupSelection.delete(planDir);
    state.cleanupCandidates.delete(planDir);
  }
  updateCleanupReview();
}

export function syncCleanupCheckboxes() {
  state.rowsByPlan.forEach((row, planDir) => {
    const box = row.querySelector('[data-field="cleanup-select"] input');
    if (box) box.checked = state.cleanupSelection.has(planDir);
  });
}

export function updateCleanupReview() {
  const count = state.cleanupSelection.size;
  const countEl = document.getElementById("wb-cleanup-count");
  if (countEl) {
    countEl.textContent =
      count === 1 ? "1 plan selected" : count + " plans selected";
  }
  const archiveBtn = document.getElementById("wb-cleanup-archive");
  if (archiveBtn) {
    const remote = state.capabilities && state.capabilities.mode === "remote";
    archiveBtn.setAttribute(
      "data-operation-action",
      remote ? "archive-plan" : "archive-plans"
    );
    // An empty selection and a mode that cannot archive are two different
    // reasons to be shut, and the operator can only act on one of them.
    if (count === 0) {
      archiveBtn.setAttribute("data-blocked-reason", "no plans are selected");
    } else if (remote && state.queueFreshness !== "live") {
      archiveBtn.setAttribute("data-blocked-reason", "the Mac observation is not live; wait for the connector before archiving");
    } else {
      archiveBtn.removeAttribute("data-blocked-reason");
    }
  }
  const clearBtn = document.getElementById("wb-cleanup-clear");
  if (clearBtn) clearBtn.disabled = count === 0;
  applyActionAvailability(document.getElementById("wb-cleanup-bar"));
}

export function handleBulkArchive() {
  const ids = Array.from(state.cleanupSelection);
  if (ids.length === 0) return;
  const panel = document.getElementById("wb-cleanup-review-panel");
  const list = document.getElementById("wb-cleanup-review-list");
  const summary = document.getElementById("wb-cleanup-review-summary");
  if (!panel || !list || !summary) return;
  list.replaceChildren();
  ids.forEach((id) => {
    const plan = state.cleanupCandidates.get(id);
    const item = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = plan ? planLabel(plan) : id;
    const detail = document.createElement("small");
    detail.textContent = plan
      ? [plan.project_group, plan.primary_state, plan.last_activity_at || "never active"]
          .filter(Boolean).join(" · ")
      : id;
    item.append(title, detail);
    list.appendChild(item);
  });
  summary.textContent = ids.length + " plans selected. Each plan will be checked again on the Mac before it moves to the archive.";
  panel.hidden = false;
  state.cleanupReviewOpen = true;
  panel.scrollIntoView({ block: "nearest" });
  document.getElementById("wb-cleanup-review-confirm")?.focus();
}

export function closeCleanupReview() {
  const panel = document.getElementById("wb-cleanup-review-panel");
  if (panel) panel.hidden = true;
  state.cleanupReviewOpen = false;
  document.getElementById("wb-cleanup-archive")?.focus();
}

async function waitForArchive(planKey, operationId) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    const body = await apiRequest(
      "/api/plan?plan_key=" + encodeURIComponent(planKey) +
      "&operation_id=" + encodeURIComponent(operationId)
    );
    const status = body && body.operation && body.operation.status;
    if (["consumed", "rejected", "expired", "superseded", "unconfirmed", "cancelled"].includes(status)) {
      return status;
    }
  }
  return "unconfirmed";
}

export async function performBulkArchive() {
  const ids = Array.from(state.cleanupSelection);
  if (!ids.length) return;
  closeCleanupReview();
  if (state.cleanupSelectionRevision &&
      state.queueCatalogRevision !== state.cleanupSelectionRevision) {
    showCleanupFeedback("error", "the catalog changed; select candidates again before archiving");
    return;
  }
  showCleanupFeedback("", "");
  if (state.capabilities && state.capabilities.mode === "remote") {
    if (state.queueFreshness !== "live") {
      showCleanupFeedback("error", "the Mac observation is not live; wait for the connector before archiving");
      return;
    }
    // Each plan is polled until the Mac settles it, so the archive button
    // stays shut for the whole batch rather than for each request: a second
    // click would otherwise start a second loop with new operation ids.
    if (!holdOperation("archive-plan")) {
      showCleanupFeedback("error", HELD_MESSAGE);
      return;
    }
    let archived = 0;
    let failed = 0;
    try {
      for (const id of ids) {
        showCleanupFeedback("pending", "archiving " + (archived + failed + 1) + " of " + ids.length + "…");
        const answer = await runMutation({
          action: "archive-plan",
          path: "/api/plan/archive",
          body: { plan_key: id, confirm_plan_key: id },
          subject: "archive of " + id,
          planId: id,
          withinHold: true,
          feedback: () => {},
        });
        if (answer && answer.outcome === OUTCOME_PENDING && answer.operation?.id) {
          let status;
          try {
            status = await waitForArchive(id, answer.operation.id);
          } catch (error) {
            status = "unconfirmed";
          }
          if (status !== "consumed") {
            failed += 1;
            showCleanupFeedback("warning", "stopped after " + archived + " archived; " + id + " is " + status + ". Review its receipt before retrying.");
            break;
          }
        } else if (!answer || answer.outcome !== OUTCOME_RESULT) {
          failed += 1;
          showCleanupFeedback(
            "warning",
            "stopped after " + archived + " archived; " + id + " was not admitted (" +
              (answer?.error || answer?.reason || "no answer") + ")."
          );
          break;
        }
        archived += 1;
        state.cleanupSelection.delete(id);
        state.cleanupCandidates.delete(id);
      }
    } finally {
      releaseOperation("archive-plan");
    }
    updateCleanupReview();
    if (!failed) showCleanupFeedback("info", "archived " + archived + " plans");
    refresh();
    return;
  }
  const answer = await runMutation({
    action: "archive-plans",
    path: "/api/plans/archive",
    body: { plan_dirs: ids },
    subject: "the bulk archive",
    feedback: showCleanupFeedback,
    // A bulk archive that completed can still have lost entries on the way, so
    // the tone is the count's rather than the outcome's.
    kindFor: (result) =>
      result.outcome === OUTCOME_RESULT &&
      (result.payload && result.payload.failed_count) > 0
        ? "warning"
        : null,
    describe: (result) => {
      if (result.outcome !== OUTCOME_RESULT) return null;
      const archived =
        typeof result.payload.archived_count === "number"
          ? result.payload.archived_count
          : 0;
      const failed =
        typeof result.payload.failed_count === "number"
          ? result.payload.failed_count
          : 0;
      return "archived " + archived + (failed ? ", " + failed + " failed" : "");
    },
  });
  // The selection is only emptied once the plans have actually moved: clearing
  // it on a recorded-but-not-run answer would take away the list the operator
  // would need to retry with.
  if (answer && answer.outcome === OUTCOME_RESULT) {
    state.cleanupSelection.clear();
    state.cleanupCandidates.clear();
    updateCleanupReview();
    refresh();
  }
}

export function showCleanupFeedback(kind, message) {
  const el = document.getElementById("wb-cleanup-feedback");
  if (!el) return;
  if (!message) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.setAttribute("data-kind", kind);
  el.textContent = message;
}
