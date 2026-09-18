/* The one router, and the two link formats history left behind it.
 *
 * Two applications published deep links for years and both sets are still in
 * circulation: the Workbench's `#plan?dir=<plan_dir>&focus=<panel>`, minted by
 * the AgentMux bridge, and the Companion's `#run=<run_key>`, minted into every
 * ntfy notification a phone has ever received. Neither can be retired by
 * deciding to, because the links are already sent.
 *
 * So both are parsed here, into one route. A path and a digest are different
 * addresses (see `identity.js`) and a run key is neither: it names a run, not
 * a plan, so it is resolved against the catalog the next time one arrives and
 * the hash is then rewritten to the canonical form. Until that happens the
 * route is honest about being unresolved rather than rendering an empty plan.
 */

import { loadArchiveView } from "./archive.js";
import { DETAIL_FOCUS_TARGETS, ROW_ACTION_FOCUS } from "./constants.js";
import {
  refreshDetail,
  renderDetailPending,
  scheduleDetailRefresh,
  stopDetailTimer,
} from "./detail.js";
import {
  addressOfPlan,
  addressOfRoute,
  detailHash,
  planMatchesRunKey,
} from "./identity.js";
import { setSelectedPlan } from "./prefs.js";
import { syncSelectedPlanView } from "./preview.js";
import { refresh, scheduleRefresh, stopQueueTimer } from "./queue.js";
import { resetRedirectForPlan } from "./redirect.js";
import { applyShellLayout, isDesktopShell } from "./shell.js";
import { state } from "./state.js";

const EMPTY_ROUTE = {
  name: "queue",
  planDir: null,
  planKey: null,
  runKey: null,
  focus: null,
};

function queueRoute() {
  return Object.assign({}, EMPTY_ROUTE);
}

function focusFrom(params) {
  const raw = params.get("focus");
  return raw && Object.prototype.hasOwnProperty.call(DETAIL_FOCUS_TARGETS, raw)
    ? raw
    : null;
}

export function parseHashRoute() {
  const hash = window.location.hash || "";
  if (hash === "#archive" || hash.startsWith("#archive?")) {
    return Object.assign(queueRoute(), { name: "archive" });
  }
  if (hash.startsWith("#plan")) {
    const querySep = hash.indexOf("?");
    if (querySep === -1) return queueRoute();
    const params = new URLSearchParams(hash.substring(querySep + 1));
    const dir = params.get("dir");
    const key = params.get("key");
    if (!dir && !key) return queueRoute();
    return Object.assign(queueRoute(), {
      name: "detail",
      planDir: dir || null,
      planKey: dir ? null : key,
      focus: focusFrom(params),
    });
  }
  // The Companion's format. Its hash is a plain query string rather than a
  // named route, so anything else in it (`project`, `q`, `refresh`) stays a
  // queue view and only `run` opens a plan.
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const runKey = params.get("run");
  if (runKey) {
    return Object.assign(queueRoute(), {
      name: "detail",
      runKey: runKey,
      focus: focusFrom(params),
    });
  }
  return queueRoute();
}

/** True when the route names a plan it cannot yet read anything about. */
export function routeIsUnresolved(route) {
  return (
    !!route && route.name === "detail" && !addressOfRoute(route) && !!route.runKey
  );
}

/**
 * Turn a `#run=` route into an addressable one using a catalog page.
 *
 * Called from the queue render because that is where catalog rows arrive in
 * either mode. A run key that no page carries is left alone: the next page
 * may hold it, and rewriting the hash to nothing would lose the deep link.
 */
export function resolveRouteFromPlans(plans) {
  const route = state.route;
  if (!routeIsUnresolved(route)) return false;
  const rows = Array.isArray(plans) ? plans : [];
  for (let index = 0; index < rows.length; index += 1) {
    if (!planMatchesRunKey(rows[index], route.runKey)) continue;
    const address = addressOfPlan(rows[index]);
    if (!address) continue;
    // `replace` rather than a push: the notification link and the plan it
    // names are one destination, so the back button should leave the app the
    // way it would have from the canonical link.
    replaceHash(detailHash(address, route.focus));
    return true;
  }
  return false;
}

function replaceHash(hash) {
  const url =
    window.location.pathname + (window.location.search || "") + (hash || "");
  try {
    window.history.replaceState(null, "", url);
  } catch (err) {
    window.location.hash = hash;
    return;
  }
  applyRoute();
}

export function applyRoute() {
  const next = parseHashRoute();
  const previous = state.route || EMPTY_ROUTE;
  const previousPlanDir = previous.planDir;
  const previousKey = previous.planKey;
  if (next.planDir !== previousPlanDir || next.planKey !== previousKey) {
    // A correction's identity belongs to the plan it was written against.
    // Carrying it across a route change would let the next submit reuse an
    // id the journal already bound to a different run.
    resetRedirectForPlan();
  }
  state.route = next;
  applyShellLayout();
  const desktop = isDesktopShell();
  if (next.name === "archive") {
    stopDetailTimer();
    stopQueueTimer();
    loadArchiveView();
    return;
  }
  if (next.name === "detail") {
    if (routeIsUnresolved(next)) {
      // Nothing to read yet. Poll the queue rather than the detail: the
      // catalog is what turns this run key into a plan.
      stopDetailTimer();
      renderDetailPending(next.runKey);
      refresh();
      scheduleRefresh();
      return;
    }
    const address = addressOfRoute(next);
    setSelectedPlan(address ? address.value : null);
    syncSelectedPlanView();
    refreshDetail();
    scheduleDetailRefresh();
    if (desktop) {
      // Desktop keeps the queue visible alongside the detail; poll both so
      // operator can see queue changes while inspecting a plan.
      refresh();
      scheduleRefresh();
    } else {
      stopQueueTimer();
    }
  } else {
    stopDetailTimer();
    refresh();
    scheduleRefresh();
  }
}

export function navigateToDetail(planDir, options) {
  if (!planDir) return;
  navigateToAddress({ param: "plan_dir", value: planDir }, options);
}

export function navigateToAddress(address, options) {
  if (!address || !address.value) return;
  setSelectedPlan(address.value);
  const focus =
    options && typeof options.focus === "string" && options.focus
      ? options.focus
      : null;
  const resolved =
    focus && Object.prototype.hasOwnProperty.call(DETAIL_FOCUS_TARGETS, focus)
      ? focus
      : null;
  window.location.hash = detailHash(address, resolved);
}

export function navigateToPlan(plan, options) {
  navigateToAddress(addressOfPlan(plan), options);
}

export function handleRowAction(plan, event) {
  if (event && typeof event.preventDefault === "function") {
    event.preventDefault();
  }
  if (!plan || !addressOfPlan(plan)) return;
  const action = plan.recommended_action || "view";
  const focus = Object.prototype.hasOwnProperty.call(ROW_ACTION_FOCUS, action)
    ? ROW_ACTION_FOCUS[action]
    : null;
  // Row buttons never mutate directly — unlock/launch route through the
  // existing detail-view controls so the confirm-before-mutate gates
  // (Force-unlock confirm, --unsafe-bypass confirm) keep their teeth.
  navigateToPlan(plan, { focus: focus });
}

export function navigateToQueue() {
  if (window.location.hash) {
    window.location.hash = "";
  } else {
    applyRoute();
  }
}

export function wireDetailBack() {
  const btn = document.getElementById("wb-detail-back");
  if (!btn) return;
  btn.addEventListener("click", navigateToQueue);
}
