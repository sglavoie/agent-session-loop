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
import {
  DEFAULT_SORT_MODE,
  DETAIL_FOCUS_TARGETS,
  FILTER_GROUPS,
  ROW_ACTION_FOCUS,
  SORT_MODES,
} from "./constants.js";
import {
  refreshDetail,
  renderDetailPending,
  scheduleDetailRefresh,
  stopDetailTimer,
} from "./detail.js";
import { applyChipPressedState } from "./filters.js";
import {
  addressOfPlan,
  addressOfRoute,
  detailHash,
  planMatchesRunKey,
} from "./identity.js";
import { hydrateQueueControls } from "./queue_controls.js";
import {
  persistFilter,
  setHarnessFilter,
  setProjectFilter,
  setSearchText,
  setShowCompleted,
  setShowOlderRuns,
  setSortMode,
  setSelectedPlan,
} from "./prefs.js";
import { syncSelectedPlanView } from "./preview.js";
import { refresh, scheduleRefresh, stopQueueTimer } from "./queue.js";
import { resetRedirectForPlan } from "./redirect.js";
import { resetReplyForPlan } from "./panels.js";
import { applyShellLayout, isDesktopShell } from "./shell.js";
import { state } from "./state.js";

const EMPTY_ROUTE = {
  name: "queue",
  planDir: null,
  planKey: null,
  runKey: null,
  entryId: null,
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

/**
 * The query part of the hash, whichever link format put it there.
 *
 * `#plan?dir=…` carries its parameters after a `?` and the Companion's
 * `#project=…` carries them directly, so one reader has to accept both or half
 * the links in circulation lose everything but their first field.
 */
export function hashParams(hash) {
  const raw = typeof hash === "string" ? hash : window.location.hash || "";
  const stripped = raw.replace(/^#/, "");
  const querySep = stripped.indexOf("?");
  const query = querySep === -1 ? stripped : stripped.substring(querySep + 1);
  return new URLSearchParams(query);
}

/* The filter state the URL carries.
 *
 * This is the Companion's `project`/`q`/`harness`/`sort` contract plus the two
 * the Workbench had only in storage, and it is in the URL for the reason the
 * Companion put it there: a view an operator wants a second person to look at
 * is a view they have to be able to send. sessionStorage remembers a reload;
 * only the hash survives being pasted into a message.
 *
 * `older` is here too, because "show me the quiet ones" is part of what was
 * being looked at rather than a property of the browser it was looked at in.
 */
export const FILTER_PARAM_NAMES = [
  "filter",
  "project",
  "q",
  "harness",
  "sort",
  "completed",
  "older",
];

export function parseQueueFilters(params) {
  return {
    filter: params.get("filter"),
    project: params.get("project"),
    search: params.get("q"),
    harness: params.get("harness"),
    sort: params.get("sort"),
    completed: params.get("completed"),
    older: params.get("older"),
  };
}

/**
 * Adopt the filters a link carried. Returns true when anything changed.
 *
 * A parameter the hash omits is left alone rather than reset: a `#run=` link
 * from a notification says nothing about filtering, and treating its silence
 * as "clear everything" would throw away the view the operator was in.
 */
export function applyQueueFilters(filters) {
  let changed = false;
  if (
    filters.filter &&
    Object.prototype.hasOwnProperty.call(FILTER_GROUPS, filters.filter) &&
    state.activeFilter !== filters.filter
  ) {
    state.activeFilter = filters.filter;
    persistFilter(filters.filter);
    changed = true;
  }
  if (filters.project !== null && setProjectFilter(filters.project)) changed = true;
  if (filters.harness !== null && setHarnessFilter(filters.harness)) changed = true;
  if (filters.search !== null && setSearchText(filters.search)) changed = true;
  if (
    filters.sort &&
    SORT_MODES.indexOf(filters.sort) !== -1 &&
    setSortMode(filters.sort)
  ) {
    changed = true;
  }
  if (filters.completed !== null && setShowCompleted(filters.completed === "1")) {
    changed = true;
  }
  if (filters.older !== null && setShowOlderRuns(filters.older === "1")) {
    changed = true;
  }
  return changed;
}

/** The hash that reproduces the queue exactly as it is being looked at now. */
export function queueHash() {
  const params = new URLSearchParams();
  if (state.activeFilter && state.activeFilter !== "all") {
    params.set("filter", state.activeFilter);
  }
  if (state.projectFilter) params.set("project", state.projectFilter);
  if (state.searchText) params.set("q", state.searchText);
  if (state.harnessFilter) params.set("harness", state.harnessFilter);
  if (state.sortMode && state.sortMode !== DEFAULT_SORT_MODE) {
    params.set("sort", state.sortMode);
  }
  if (state.showCompleted) params.set("completed", "1");
  if (state.showOlderRuns) params.set("older", "1");
  const query = params.toString();
  return query ? "#" + query : "";
}

/**
 * Rewrite the hash so it describes the current filters, without re-routing.
 *
 * `replaceState` rather than a push: narrowing a search is not a destination,
 * and a back button that walked every keystroke would be a back button nobody
 * could use to leave the queue.
 */
export function syncFilterHash() {
  if (!state.route || state.route.name !== "queue") return;
  const hash = queueHash();
  const url =
    window.location.pathname + (window.location.search || "") + hash;
  try {
    window.history.replaceState(null, "", url || "/");
  } catch (err) {
    // Older browsers and file:// loads. Assigning the hash would re-enter the
    // router for a change the router has already made, so leave it: the
    // filters are applied, only the link is not shareable.
  }
}

export function parseHashRoute() {
  const hash = window.location.hash || "";
  if (hash === "#archive" || hash.startsWith("#archive?")) {
    return Object.assign(queueRoute(), { name: "archive" });
  }
  if (hash.startsWith("#plan")) {
    const querySep = hash.indexOf("?");
    if (querySep === -1) return queueRoute();
    const params = hashParams(hash);
    const dir = params.get("dir");
    const key = params.get("key");
    if (!dir && !key) return queueRoute();
    return Object.assign(queueRoute(), {
      name: "detail",
      planDir: dir || null,
      planKey: dir ? null : key,
      // Carried through the canonical form as well, because it is not an
      // address -- it is where the operator came from. A notification names a
      // run, and once that run has gone quiet the queue owes them a heading
      // saying so rather than a row at the top of today's work. Dropping the
      // key at the rewrite below is what used to make `older-linked` a section
      // nothing could ever land in.
      runKey: params.get("run") || null,
      focus: focusFrom(params),
    });
  }
  // The Companion's format. Its hash is a plain query string rather than a
  // named route, so `project`, `q`, `harness`, `sort` and `older` stay a queue
  // view; `run` opens a plan and `entry` opens a finished run in history.
  const params = hashParams(hash);
  const runKey = params.get("run");
  if (runKey) {
    return Object.assign(queueRoute(), {
      name: "detail",
      runKey: runKey,
      focus: focusFrom(params),
    });
  }
  const entryId = params.get("entry");
  if (entryId) {
    // Not a detail route: a finished run has no plan left to poll, so the
    // queue stays up and history scrolls to the entry the link names.
    return Object.assign(queueRoute(), { entryId: entryId });
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
    // way it would have from the canonical link. The run key rides along so
    // the rewrite loses only the ambiguity, not the provenance.
    replaceHash(
      detailHash(address, route.focus) +
        "&run=" +
        encodeURIComponent(route.runKey)
    );
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
  // Before anything renders: a link's filters are part of what it addresses,
  // and the queue fetch below sends them to the server rather than applying
  // them here, so they have to be in hand first.
  applyQueueFilters(parseQueueFilters(hashParams()));
  hydrateQueueControls();
  applyChipPressedState();
  const previous = state.route || EMPTY_ROUTE;
  const previousPlanDir = previous.planDir;
  const previousKey = previous.planKey;
  if (next.planDir !== previousPlanDir || next.planKey !== previousKey) {
    // A correction's identity belongs to the plan it was written against.
    // Carrying it across a route change would let the next submit reuse an
    // id the journal already bound to a different run.
    resetRedirectForPlan();
    resetReplyForPlan();
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
  // Back to the queue the operator left, not to an unfiltered one: the hash
  // they came from named a plan, so the filters have to be written back out of
  // state rather than read out of the link.
  const hash = queueHash();
  if (window.location.hash !== hash) {
    window.location.hash = hash;
    if (!hash) {
      // Clearing a hash to the empty string does not always fire `hashchange`,
      // so the route would be left on the plan the operator just closed.
      applyRoute();
    }
  } else {
    applyRoute();
  }
}

export function wireDetailBack() {
  const btn = document.getElementById("wb-detail-back");
  if (!btn) return;
  btn.addEventListener("click", navigateToQueue);
}
