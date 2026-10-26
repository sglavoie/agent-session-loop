/* Detail view: one plan, refreshed on a timer, assembled from many reads.
 *
 * The header and the panels are loaded together on every poll so they cannot
 * disagree about which plan is on screen, and the scroll position and the
 * focused panel are restored afterwards so a poll is not a place the operator
 * loses their place.
 */

import { apiRequest, describeError } from "./api.js";
import { syncDetailNav } from "./detail_nav.js";
import {
  DETAIL_REFRESH_INTERVAL_MS,
  HARNESS_NAMES,
  LINKED_QUESTION_REFRESH_INTERVAL_MS,
} from "./constants.js";
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
  loadAndRenderReply,
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
  const linkedQuestion = state.route && state.route.questionId;
  const loadedQuestion =
    state.detailReplyContext && state.detailReplyContext.question_id;
  const delay =
    linkedQuestion && linkedQuestion !== loadedQuestion
      ? LINKED_QUESTION_REFRESH_INTERVAL_MS
      : DETAIL_REFRESH_INTERVAL_MS;
  state.detailTimer = window.setTimeout(() => {
    const update = linkedQuestion
      ? liveReplyFormIsInteractive()
        ? Promise.resolve()
        : loadAndRenderReply()
      : refreshDetail();
    update.finally(() => {
      if (state.route.name === "detail") scheduleDetailRefresh();
    });
  }, delay);
}

export function stopDetailTimer() {
  if (state.detailTimer !== null) {
    window.clearTimeout(state.detailTimer);
    state.detailTimer = null;
  }
}

/** Keep an unanswered live reply screen still while the operator uses it.
 *
 * The detail poll redraws several panels every five seconds. Even when the
 * reply form itself is unchanged, another panel can change the document
 * height and make mobile WebKit treat a tap as a scroll. The submit response
 * is authoritative, so no poll is needed until that response records the
 * reply; once recorded, polling resumes and can show its consumption.
 */
export function liveReplyFormIsInteractive() {
  const context = state.detailReplyContext;
  const linkedQuestion = state.route && state.route.questionId;
  if (
    !context ||
    context.context_kind !== "waiting" ||
    !context.question_id ||
    (linkedQuestion && context.question_id !== linkedQuestion) ||
    state.reply.recordedQuestionId === context.question_id
  ) {
    return false;
  }
  const panel = document.getElementById("wb-reply-panel");
  const form = document.getElementById("wb-reply-form");
  return Boolean(panel && !panel.hidden && form && !form.hidden);
}

export async function refreshDetail() {
  if (state.route.name !== "detail" || !routePlanId()) return;
  // A notification is a question screen, not a plan-detail shortcut. Reading
  // the other panels can surface another open question and lets unrelated
  // plan state compete with the one action the alert asked the operator to
  // take. Keep every refresh, including visibility refreshes, on the exact
  // question selected by the route.
  if (state.route.questionId) return loadAndRenderReply();
  if (state.detailInFlight) {
    // A route change (or a poll) arrived while a read is on the wire. That
    // read may be for another plan, so dropping this one would leave the
    // previous plan's answer on screen until the next poll; it is run once
    // the read in flight settles instead.
    state.detailRefreshPending = true;
    return;
  }
  if (liveReplyFormIsInteractive()) return;
  state.detailInFlight = true;
  state.detailRefreshPending = false;
  setRefreshStatus("loading", "refreshing…");
  const scrollSnapshot = captureDetailScroll();
  const pendingFocus = state.route.focus;
  // The plan this read is about. The panels below read the current route, so
  // an answer that arrives after the route moved on describes a plan that is
  // no longer on screen and must not be drawn beside the new one's panels.
  const requestedPlanId = routePlanId();
  const superseded = () => {
    if (routePlanId() === requestedPlanId) return false;
    state.detailRefreshPending = true;
    return true;
  };
  try {
    const [detailBody, registryBody] = await Promise.all([
      apiRequest(planUrl("/api/plan")),
      apiRequest("/api/registry"),
    ]);
    if (superseded()) return;
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
            : null),
        state.route.questionId || null
      ),
      loadAndRenderRedirect(),
      loadAndRenderState(),
      loadAndRenderEvents(),
      loadAndRenderProgress(),
      loadAndRenderLog("stdout"),
      loadAndRenderLog("stderr"),
      loadAndRenderArtifacts(),
      loadAndRenderReply(),
    ];
    await Promise.all(tasks);
    if (superseded()) return;
    const ts = new Date().toLocaleTimeString();
    setRefreshStatus("idle", "updated " + ts);
  } catch (err) {
    if (superseded()) return;
    setRefreshStatus("error", "error: " + describeError(err));
  } finally {
    state.detailInFlight = false;
    if (state.detailRefreshPending) {
      // The follow-up read restores scroll and focus for whatever it draws.
      state.detailRefreshPending = false;
      refreshDetail();
    } else if (state.reply.scrollToTopAfterDelivery) {
      state.reply.scrollToTopAfterDelivery = false;
      window.scrollTo(window.scrollX || 0, 0);
    } else if (pendingFocus) {
      applyDetailFocus();
    } else {
      restoreDetailScroll(scrollSnapshot);
    }
    syncDetailNav();
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
