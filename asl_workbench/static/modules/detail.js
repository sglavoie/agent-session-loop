/* Detail view: one plan, refreshed on a timer, assembled from many reads.
 *
 * The header and the panels are loaded together on every poll so they cannot
 * disagree about which plan is on screen, and the scroll position and the
 * focused panel are restored afterwards so a poll is not a place the operator
 * loses their place.
 */

import { apiRequest, describeError } from "./api.js";
import { DETAIL_REFRESH_INTERVAL_MS, HARNESS_NAMES } from "./constants.js";
import {
  planHint,
  planIdOf,
  planLabel,
  planUrl,
  routePlanId,
} from "./identity.js";
import {
  isPinned,
  loadAndRenderArtifacts,
  loadAndRenderEvents,
  loadAndRenderLog,
  loadAndRenderProgress,
  loadAndRenderState,
} from "./panels.js";
import {
  applyActionAvailability,
  renderOperationReceipts,
} from "./operations.js";
import { describeAction, formatRepositoryAliases } from "./preview.js";
import { renderProfileForm } from "./profile.js";
import { setRefreshStatus } from "./queue.js";
import { loadAndRenderRecovery } from "./recovery.js";
import { loadAndRenderRedirect } from "./redirect.js";
import {
  applyDetailFocus,
  captureDetailScroll,
  restoreDetailScroll,
} from "./shell.js";
import { state } from "./state.js";

export function scheduleDetailRefresh() {
  stopDetailTimer();
  state.detailTimer = window.setTimeout(() => {
    refreshDetail().finally(() => {
      if (state.route.name === "detail") scheduleDetailRefresh();
    });
  }, DETAIL_REFRESH_INTERVAL_MS);
}

export function stopDetailTimer() {
  if (state.detailTimer !== null) {
    window.clearTimeout(state.detailTimer);
    state.detailTimer = null;
  }
}

export async function refreshDetail() {
  if (state.route.name !== "detail" || !routePlanId()) return;
  if (state.detailInFlight) return;
  state.detailInFlight = true;
  setRefreshStatus("loading", "refreshing…");
  const scrollSnapshot = captureDetailScroll();
  const pendingFocus = state.route.focus;
  try {
    const [detailBody, registryBody] = await Promise.all([
      apiRequest(planUrl("/api/plan")),
      apiRequest("/api/registry"),
    ]);
    if (!detailBody || detailBody.ok !== true) {
      renderDetailError((detailBody && detailBody.error) || "failed to load plan");
      return;
    }
    state.detailPlan = detailBody.plan || null;
    state.detailRegistry = registryBody && registryBody.registry
      ? registryBody.registry
      : null;
    // What the service itself still holds for this plan. The remote provider
    // publishes it on every plan read and it is the authority on outstanding
    // work: it survives this tab, so it is how a mutation sent from a phone
    // that has since been closed is still visible from a laptop.
    state.detailOutstanding = detailBody.outstanding_operation || null;
    renderOperationReceipts(state.detailOutstanding);
    renderDetailHeader(state.detailPlan);
    renderProfileForm(state.detailPlan, state.detailRegistry);
    const tasks = [
      loadAndRenderRecovery(
        state.detailPlan,
        Array.isArray(detailBody.runs) ? detailBody.runs : [],
        state.route.runKey ||
          (state.detailPlan && state.detailPlan.run_key
            ? state.detailPlan.run_key
            : null)
      ),
      loadAndRenderRedirect(),
      loadAndRenderState(),
      loadAndRenderEvents(),
      loadAndRenderProgress(),
      loadAndRenderLog("stdout"),
      loadAndRenderLog("stderr"),
      loadAndRenderArtifacts(),
    ];
    await Promise.all(tasks);
    const ts = new Date().toLocaleTimeString();
    setRefreshStatus("idle", "updated " + ts);
  } catch (err) {
    setRefreshStatus("error", "error: " + describeError(err));
  } finally {
    state.detailInFlight = false;
    if (pendingFocus) {
      applyDetailFocus();
    } else {
      restoreDetailScroll(scrollSnapshot);
    }
  }
}

/**
 * What a `#run=` deep link shows before the catalog names its plan.
 *
 * A notification link carries a run key, and a run key is not an address: the
 * only thing that can turn it into one is a catalog page. Until one arrives
 * this says so and names the key, rather than rendering an empty plan that
 * would read as "this run is gone".
 */
export function renderDetailPending(runKey) {
  const labelEl = document.getElementById("wb-detail-label");
  if (labelEl) labelEl.textContent = "(opening run)";
  const pathEl = document.getElementById("wb-detail-path");
  if (pathEl) pathEl.textContent = runKey ? "run " + runKey : "";
  const hintEl = document.getElementById("wb-detail-hint");
  if (hintEl) {
    hintEl.textContent = "looking this run up in the queue…";
  }
  const stateEl = document.getElementById("wb-detail-state");
  if (stateEl) {
    stateEl.textContent = "resolving";
    stateEl.setAttribute("data-state", "unknown");
  }
}

export function renderDetailError(message) {
  const labelEl = document.getElementById("wb-detail-label");
  if (labelEl) labelEl.textContent = "(error)";
  const hintEl = document.getElementById("wb-detail-hint");
  if (hintEl) hintEl.textContent = String(message);
}

export function renderDetailHeader(plan) {
  const labelEl = document.getElementById("wb-detail-label");
  const pathEl = document.getElementById("wb-detail-path");
  const workspaceEl = document.getElementById("wb-detail-workspace");
  const stateEl = document.getElementById("wb-detail-state");
  const actionEl = document.getElementById("wb-detail-action-hint");
  const hintEl = document.getElementById("wb-detail-hint");
  if (!plan) return;
  if (labelEl) labelEl.textContent = planLabel(plan);
  // Falls back to the digest: the Pi addresses plans by one and has no path,
  // and a blank line under the title would read as a plan with no home.
  if (pathEl) pathEl.textContent = plan.plan_dir || planIdOf(plan);
  if (workspaceEl) {
    const aliases = formatRepositoryAliases(plan.repositories);
    const root = plan.workspace_root || plan.cwd || "";
    workspaceEl.textContent = root
      ? "workspace: " + root + (aliases ? " · repositories: " + aliases : "")
      : "";
  }
  if (stateEl) {
    stateEl.textContent = plan.primary_state || "unknown";
    stateEl.setAttribute("data-state", plan.primary_state || "unknown");
  }
  if (actionEl) {
    actionEl.textContent = plan.recommended_action
      ? "action: " + describeAction(plan.recommended_action)
      : "";
  }
  if (hintEl) hintEl.textContent = planHint(plan);

  const pinButton = document.getElementById("wb-control-pin");
  if (pinButton) {
    const pinned = isPinned(plan.plan_dir);
    pinButton.textContent = pinned ? "update pin" : "pin";
    pinButton.setAttribute("data-pinned", pinned ? "true" : "false");
  }
  const unpinButton = document.getElementById("wb-profile-unpin");
  if (unpinButton) {
    // Written as a reason rather than as `disabled`, so the capability pass
    // below leaves it alone instead of re-enabling an unpin with no pin.
    if (isPinned(plan.plan_dir)) {
      unpinButton.removeAttribute("data-blocked-reason");
    } else {
      unpinButton.setAttribute("data-blocked-reason", "this plan is not pinned");
    }
  }
  const harnessSelect = document.getElementById("wb-control-harness");
  if (harnessSelect && plan.harness) {
    if (HARNESS_NAMES.indexOf(plan.harness) !== -1) {
      harnessSelect.value = plan.harness;
    }
  }
  // The plan's own state is half of what decides whether a control can do
  // anything, so every header render re-asks. A stop offered against a plan
  // that finished during the last poll is a button with nothing behind it.
  applyActionAvailability(document.getElementById("wb-detail"));
}
