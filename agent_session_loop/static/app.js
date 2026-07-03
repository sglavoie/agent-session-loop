/* ASL Workbench - action queue and plan detail UI.
 *
 * Stdlib-only vanilla JS. Reads the bearer token from the initial URL, polls
 * /api/queue on a fixed interval, and renders a queue grouped by filter
 * chips. A hash-based route (#plan?dir=<plan_dir>) switches to a per-plan
 * detail view with sessions, active turn, events, logs, progress journal,
 * profile editor, safe controls, and a reply form.
 *
 * Local UI state survives refresh cycles by reusing DOM nodes keyed by
 * plan_dir / session_id / event index / commit_index. The reply textarea
 * is never persisted to sessionStorage/localStorage and is cleared as soon
 * as a submission succeeds.
 */

(function () {
  "use strict";

  const REFRESH_INTERVAL_MS = 5000;
  const DETAIL_REFRESH_INTERVAL_MS = 5000;
  const SEARCH_DEBOUNCE_MS = 150;
  const DESKTOP_SHELL_MEDIA_QUERY = "(min-width: 960px)";
  const TOKEN_STORAGE_KEY = "wb.token";
  const FILTER_STORAGE_KEY = "wb.filter";
  const SEARCH_STORAGE_KEY = "wb.search";
  const SORT_STORAGE_KEY = "wb.sort";
  const COMPLETED_STORAGE_KEY = "wb.completed";
  const SELECTED_STORAGE_KEY = "wb.selected";
  const CLEANUP_MODE_STORAGE_KEY = "wb.cleanup";
  const CLEANUP_THRESHOLD_STORAGE_KEY = "wb.cleanupDays";
  const CLEANUP_DEFAULT_THRESHOLD_DAYS = 30;
  const DAY_MS = 24 * 60 * 60 * 1000;
  const HARNESS_NAMES = ["codex", "claude", "copilot"];
  const COMMIT_MODES = ["per-session", "end"];
  const PRIMARY_STATES = [
    "invalid",
    "verifying",
    "running",
    "paused-for-reply",
    "failed",
    "stale",
    "complete",
    "recent",
    "ready",
  ];
  const FILTER_GROUPS = {
    all: PRIMARY_STATES.slice(),
    running: ["running", "verifying"],
    paused: ["paused-for-reply"],
    failed: ["failed", "invalid"],
    stale: ["stale"],
    ready: ["ready"],
    complete: ["complete"],
    recent: ["recent"],
  };
  const SORT_MODES = ["urgency", "recent", "label", "path", "oldest"];
  const SORT_MODE_LABELS = {
    urgency: "urgency",
    recent: "last activity",
    label: "label",
    path: "plan path",
    oldest: "oldest activity",
  };
  const DEFAULT_SORT_MODE = "urgency";

  // Row action button intent → detail-view focus target. Reply jumps the
  // operator into the textarea so they can answer immediately; unlock and
  // launch surface the existing confirm-before-mutate controls (the row
  // never issues mutating POSTs itself).
  const ROW_ACTION_FOCUS = {
    watch: "active",
    reply: "reply",
    "inspect-failure": "events",
    unlock: "unlock",
    view: null,
    run: "launch",
    "repair-plan": null,
  };
  // Detail panel scroll target + optional autofocus element per focus key.
  // Scroll target may be a wrapping section (so the operator sees context
  // around the focused control); focus target is what receives keyboard
  // focus after the panel scrolls into view.
  const DETAIL_FOCUS_TARGETS = {
    recovery: { scroll: "wb-recovery-panel", focus: null },
    reply: { scroll: "wb-recovery-panel", focus: "wb-reply-text" },
    events: { scroll: "wb-detail-events", focus: null },
    active: { scroll: "wb-detail-active", focus: null },
    verification: { scroll: "wb-detail-verification", focus: null },
    progress: { scroll: "wb-detail-progress", focus: null },
    artifacts: { scroll: "wb-detail-artifacts", focus: null },
    stdout: { scroll: "wb-detail-stdout", focus: null },
    stderr: { scroll: "wb-detail-stderr", focus: null },
    profile: { scroll: "wb-profile-form", focus: null },
    launch: { scroll: "wb-controls", focus: "wb-control-launch" },
    unlock: { scroll: "wb-controls", focus: "wb-control-unlock" },
  };
  const RECOVERY_ACTION_TARGETS = [
    "reply",
    "events",
    "active",
    "verification",
    "progress",
    "artifacts",
    "stdout",
    "stderr",
    "profile",
    "launch",
    "unlock",
  ];
  const RECOVERY_TARGET_LABELS = {
    reply: "reply",
    events: "events",
    active: "active turn",
    verification: "verification",
    progress: "progress",
    artifacts: "artifacts",
    stdout: "stdout",
    stderr: "stderr",
    profile: "profile",
    launch: "launch",
    unlock: "unlock",
  };
  const RECOVERY_ACTION_TARGETS_BY_ID = {
    reply: "reply",
    unlock: "unlock",
    "retry-run": "launch",
    "inspect-artifacts": "artifacts",
    "review-profile": "profile",
  };
  const REPLY_CONTEXT_ERROR_HINTS = {
    "no-paused-run": "No paused run is waiting for an operator reply.",
    "missing-question-context": "The paused turn no longer has readable question context.",
    "reply-context-mismatch": "The paused session no longer matches the current pending session.",
  };

  const state = {
    token: null,
    activeFilter: "all",
    searchText: "",
    sortMode: DEFAULT_SORT_MODE,
    showCompleted: false,
    selectedPlan: null,
    cleanupMode: false,
    cleanupThresholdDays: CLEANUP_DEFAULT_THRESHOLD_DAYS,
    cleanupSelection: new Set(),
    archiveInFlight: false,
    archiveEntries: [],
    archiveSelection: new Set(),
    archiveRetentionDays: null,
    archiveMeta: null,
    previewPlan: null,
    lastVisiblePlans: [],
    rowsByPlan: new Map(),
    refreshTimer: null,
    fetchInFlight: false,
    lastQueueMeta: null,
    route: { name: "queue", planDir: null, focus: null },
    detailTimer: null,
    detailInFlight: false,
    detailPlan: null,
    detailRegistry: null,
    detailReplyContext: null,
    detailHistoryByPlan: new Map(),
    shellMediaQuery: null,
    searchDebounceTimer: null,
  };

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    state.token = resolveToken();
    state.activeFilter = restoreFilter();
    state.searchText = restoreSearchText();
    state.sortMode = restoreSortMode();
    state.showCompleted = restoreShowCompleted();
    state.selectedPlan = restoreSelectedPlan();
    state.cleanupMode = restoreCleanupMode();
    state.cleanupThresholdDays = restoreCleanupThreshold();
    state.shellMediaQuery =
      typeof window.matchMedia === "function"
        ? window.matchMedia(DESKTOP_SHELL_MEDIA_QUERY)
        : null;
    wireFilterChips();
    wireRefreshButton();
    wireDetailBack();
    wireControls();
    wireProfileForm();
    wireReplyForm();
    wireShellViewport();
    wirePreviewOpen();
    wireSearchControl();
    wireSortControl();
    wireShowCompletedControl();
    wireCleanupControls();
    wireArchiveControls();
    wireEmptyStateActions();
    hydrateQueueControls();
    applyChipPressedState();
    applyRoute();
    window.addEventListener("hashchange", applyRoute);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      if (state.route.name === "detail") {
        refreshDetail();
        if (isDesktopShell()) refresh();
      } else {
        refresh();
      }
    });
  }

  // ---------------------------------------------------------------------
  // Inbox shell layout — desktop two-pane vs mobile stacked

  function isDesktopShell() {
    return state.shellMediaQuery ? !!state.shellMediaQuery.matches : false;
  }

  function wireShellViewport() {
    const mql = state.shellMediaQuery;
    if (!mql) return;
    const onChange = () => {
      applyShellLayout();
      if (state.route.name !== "detail") return;
      if (isDesktopShell()) {
        refresh();
        scheduleRefresh();
      } else {
        stopQueueTimer();
      }
    };
    if (typeof mql.addEventListener === "function") {
      mql.addEventListener("change", onChange);
    } else if (typeof mql.addListener === "function") {
      mql.addListener(onChange);
    }
  }

  function applyShellLayout() {
    const desktop = isDesktopShell();
    const route = state.route.name;
    const hasPreview = !!state.previewPlan;
    const shell = document.getElementById("wb-shell");
    if (shell) {
      shell.setAttribute("data-route", route);
      shell.setAttribute("data-viewport", desktop ? "desktop" : "mobile");
      shell.setAttribute("data-preview", hasPreview ? "active" : "empty");
    }
    const queueEl = document.getElementById("wb-queue");
    const detailEl = document.getElementById("wb-detail");
    const filtersEl = document.getElementById("wb-filters");
    const controlsEl = document.getElementById("wb-controls-bar");
    const warningsEl = document.getElementById("wb-warnings");
    const queueMetaEl = document.getElementById("wb-queue-meta");
    const placeholderEl = document.getElementById("wb-shell-placeholder");
    const previewEl = document.getElementById("wb-shell-preview");
    const archiveEl = document.getElementById("wb-archive");
    const cleanupBarEl = document.getElementById("wb-cleanup-bar");
    if (route === "archive") {
      if (archiveEl) archiveEl.hidden = false;
      if (shell) shell.hidden = true;
      if (filtersEl) filtersEl.hidden = true;
      if (controlsEl) controlsEl.hidden = true;
      if (cleanupBarEl) cleanupBarEl.hidden = true;
      if (queueMetaEl) queueMetaEl.hidden = true;
      if (warningsEl) warningsEl.hidden = true;
      return;
    }
    if (archiveEl) archiveEl.hidden = true;
    if (shell) shell.hidden = false;
    if (cleanupBarEl) cleanupBarEl.hidden = !state.cleanupMode || route === "detail";
    if (route === "detail") {
      if (detailEl) detailEl.hidden = false;
      if (queueEl) queueEl.hidden = !desktop;
      if (filtersEl) filtersEl.hidden = !desktop;
      if (controlsEl) controlsEl.hidden = !desktop;
      if (queueMetaEl) queueMetaEl.hidden = !desktop || !state.lastQueueMeta;
      if (warningsEl && !desktop) warningsEl.hidden = true;
      if (placeholderEl) placeholderEl.hidden = true;
      if (previewEl) previewEl.hidden = true;
    } else {
      if (queueEl) queueEl.hidden = false;
      if (filtersEl) filtersEl.hidden = false;
      if (controlsEl) controlsEl.hidden = false;
      if (queueMetaEl && state.lastQueueMeta) queueMetaEl.hidden = false;
      if (detailEl) detailEl.hidden = true;
      // The CSS placeholder and preview appear via [data-route="queue"] +
      // [data-preview="empty"/"active"] on desktop; keep both elements
      // rendered so the selectors can target them without re-toggling
      // `hidden` on every viewport change.
      if (placeholderEl) placeholderEl.hidden = false;
      if (previewEl) previewEl.hidden = false;
    }
  }

  // ---------------------------------------------------------------------
  // Scroll + focus preservation across refresh polls.
  // Polling re-renders panels every 5s; capture-before / restore-after
  // keeps the operator's viewport from jumping while they read.

  function captureQueueScroll() {
    const queueEl = document.getElementById("wb-queue");
    return {
      queue: queueEl ? queueEl.scrollTop : 0,
      window: typeof window.scrollY === "number" ? window.scrollY : 0,
    };
  }

  function restoreQueueScroll(snapshot) {
    if (!snapshot) return;
    const queueEl = document.getElementById("wb-queue");
    if (queueEl && typeof snapshot.queue === "number") {
      queueEl.scrollTop = snapshot.queue;
    }
    const previewBody = document.getElementById("wb-preview-last-message-body");
    if (previewBody && typeof snapshot.preview === "number") {
      previewBody.scrollTop = snapshot.preview;
    }
    if (typeof snapshot.window === "number") {
      window.scrollTo(window.scrollX || 0, snapshot.window);
    }
  }

  function captureDetailScroll() {
    const events = document.getElementById("wb-detail-events");
    const previewBody = document.getElementById("wb-preview-last-message-body");
    return {
      window: typeof window.scrollY === "number" ? window.scrollY : 0,
      events: events ? events.scrollTop : 0,
      preview: previewBody ? previewBody.scrollTop : 0,
    };
  }

  function restoreDetailScroll(snapshot) {
    if (!snapshot) return;
    const events = document.getElementById("wb-detail-events");
    if (events && typeof snapshot.events === "number") {
      events.scrollTop = snapshot.events;
    }
    const previewBody = document.getElementById("wb-preview-last-message-body");
    if (previewBody && typeof snapshot.preview === "number") {
      previewBody.scrollTop = snapshot.preview;
    }
    if (typeof snapshot.window === "number") {
      window.scrollTo(window.scrollX || 0, snapshot.window);
    }
  }

  function applyDetailFocus() {
    const focusKey = state.route.focus;
    if (!focusKey) return;
    const target = DETAIL_FOCUS_TARGETS[focusKey];
    if (!target) {
      state.route.focus = null;
      return;
    }
    const scrollEl = target.scroll
      ? document.getElementById(target.scroll)
      : null;
    if (scrollEl && typeof scrollEl.scrollIntoView === "function") {
      scrollEl.scrollIntoView({ block: "start", behavior: "auto" });
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
    // One-shot: subsequent polls must not re-snap the viewport.
    state.route.focus = null;
  }

  // ---------------------------------------------------------------------
  // Routing

  function parseHashRoute() {
    const hash = window.location.hash || "";
    if (hash === "#archive" || hash.startsWith("#archive?")) {
      return { name: "archive", planDir: null, focus: null };
    }
    if (!hash.startsWith("#plan")) {
      return { name: "queue", planDir: null, focus: null };
    }
    const querySep = hash.indexOf("?");
    if (querySep === -1) return { name: "queue", planDir: null, focus: null };
    const search = hash.substring(querySep + 1);
    const params = new URLSearchParams(search);
    const dir = params.get("dir");
    if (!dir) return { name: "queue", planDir: null, focus: null };
    const focusRaw = params.get("focus");
    const focus =
      focusRaw && Object.prototype.hasOwnProperty.call(DETAIL_FOCUS_TARGETS, focusRaw)
        ? focusRaw
        : null;
    return { name: "detail", planDir: dir, focus: focus };
  }

  function applyRoute() {
    const next = parseHashRoute();
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
      setSelectedPlan(next.planDir);
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

  function navigateToDetail(planDir, options) {
    if (!planDir) return;
    setSelectedPlan(planDir);
    const encoded = encodeURIComponent(planDir);
    const focus =
      options && typeof options.focus === "string" && options.focus
        ? options.focus
        : null;
    let hash = "#plan?dir=" + encoded;
    if (focus && Object.prototype.hasOwnProperty.call(DETAIL_FOCUS_TARGETS, focus)) {
      hash += "&focus=" + encodeURIComponent(focus);
    }
    window.location.hash = hash;
  }

  function handleRowAction(plan, event) {
    if (event && typeof event.preventDefault === "function") {
      event.preventDefault();
    }
    if (!plan || !plan.plan_dir) return;
    const action = plan.recommended_action || "view";
    const focus = Object.prototype.hasOwnProperty.call(ROW_ACTION_FOCUS, action)
      ? ROW_ACTION_FOCUS[action]
      : null;
    // Row buttons never mutate directly — unlock/launch route through the
    // existing detail-view controls so the confirm-before-mutate gates
    // (Force-unlock confirm, --unsafe-bypass confirm) keep their teeth.
    navigateToDetail(plan.plan_dir, { focus: focus });
  }

  function navigateToQueue() {
    if (window.location.hash) {
      window.location.hash = "";
    } else {
      applyRoute();
    }
  }

  function wireDetailBack() {
    const btn = document.getElementById("wb-detail-back");
    if (!btn) return;
    btn.addEventListener("click", navigateToQueue);
  }

  // ---------------------------------------------------------------------
  // Token + URL bootstrap

  function resolveToken() {
    const url = new URL(window.location.href);
    const fromUrl = url.searchParams.get("token");
    if (fromUrl) {
      try {
        window.sessionStorage.setItem(TOKEN_STORAGE_KEY, fromUrl);
      } catch (err) {
        // sessionStorage unavailable; fall back to in-memory token.
      }
      url.searchParams.delete("token");
      const cleaned =
        url.pathname + (url.search ? url.search : "") + (url.hash || "");
      try {
        window.history.replaceState(null, "", cleaned || "/");
      } catch (err) {
        // ignore — older browsers, file:// loads, etc.
      }
      return fromUrl;
    }
    try {
      return window.sessionStorage.getItem(TOKEN_STORAGE_KEY) || null;
    } catch (err) {
      return null;
    }
  }

  function restoreFilter() {
    try {
      const stored = window.sessionStorage.getItem(FILTER_STORAGE_KEY);
      if (stored && Object.prototype.hasOwnProperty.call(FILTER_GROUPS, stored)) {
        return stored;
      }
    } catch (err) {
      // ignore
    }
    return "all";
  }

  function persistFilter(name) {
    try {
      window.sessionStorage.setItem(FILTER_STORAGE_KEY, name);
    } catch (err) {
      // ignore
    }
  }

  // Queue UI preferences (search/sort/completed visibility/selected plan).
  // Reply textarea text is intentionally never persisted — see
  // wb-reply-text handling and the privacy regression in
  // WorkbenchStaticAssetTests.

  function restoreSearchText() {
    try {
      const raw = window.sessionStorage.getItem(SEARCH_STORAGE_KEY);
      return normalizeSearchText(raw);
    } catch (err) {
      return "";
    }
  }

  function persistSearchText(value) {
    try {
      const normalized = normalizeSearchText(value);
      if (normalized) {
        window.sessionStorage.setItem(SEARCH_STORAGE_KEY, normalized);
      } else {
        window.sessionStorage.removeItem(SEARCH_STORAGE_KEY);
      }
    } catch (err) {
      // ignore
    }
  }

  function normalizeSearchText(value) {
    if (typeof value !== "string") return "";
    return value.toLowerCase().replace(/\s+/g, " ").trim();
  }

  function setSearchText(value) {
    const normalized = normalizeSearchText(value);
    if (normalized === state.searchText) return false;
    state.searchText = normalized;
    persistSearchText(normalized);
    return true;
  }

  function restoreSortMode() {
    try {
      const stored = window.sessionStorage.getItem(SORT_STORAGE_KEY);
      if (stored && SORT_MODES.indexOf(stored) !== -1) {
        return stored;
      }
    } catch (err) {
      // ignore
    }
    return DEFAULT_SORT_MODE;
  }

  function persistSortMode(name) {
    try {
      if (SORT_MODES.indexOf(name) === -1) return;
      window.sessionStorage.setItem(SORT_STORAGE_KEY, name);
    } catch (err) {
      // ignore
    }
  }

  function setSortMode(name) {
    if (SORT_MODES.indexOf(name) === -1) return false;
    if (state.sortMode === name) return false;
    state.sortMode = name;
    persistSortMode(name);
    return true;
  }

  function restoreShowCompleted() {
    try {
      const stored = window.sessionStorage.getItem(COMPLETED_STORAGE_KEY);
      if (stored === "true") return true;
      if (stored === "false") return false;
    } catch (err) {
      // ignore
    }
    return false;
  }

  function persistShowCompleted(value) {
    try {
      window.sessionStorage.setItem(
        COMPLETED_STORAGE_KEY,
        value ? "true" : "false"
      );
    } catch (err) {
      // ignore
    }
  }

  function setShowCompleted(value) {
    const next = value === true;
    if (state.showCompleted === next) return false;
    state.showCompleted = next;
    persistShowCompleted(next);
    return true;
  }

  function restoreSelectedPlan() {
    try {
      const stored = window.sessionStorage.getItem(SELECTED_STORAGE_KEY);
      if (typeof stored === "string" && stored) return stored;
    } catch (err) {
      // ignore
    }
    return null;
  }

  function persistSelectedPlan(planDir) {
    try {
      if (planDir) {
        window.sessionStorage.setItem(SELECTED_STORAGE_KEY, planDir);
      } else {
        window.sessionStorage.removeItem(SELECTED_STORAGE_KEY);
      }
    } catch (err) {
      // ignore
    }
  }

  function setSelectedPlan(planDir) {
    const next = typeof planDir === "string" && planDir ? planDir : null;
    if (state.selectedPlan === next) return false;
    state.selectedPlan = next;
    persistSelectedPlan(next);
    return true;
  }

  function clearSelectedPlan() {
    return setSelectedPlan(null);
  }

  // Cleanup mode is a session-scoped UI preference (mode toggle + candidate
  // threshold). The selected plan set is intentionally NOT persisted — a
  // bulk archive is a deliberate, in-session action.

  function restoreCleanupMode() {
    try {
      return window.sessionStorage.getItem(CLEANUP_MODE_STORAGE_KEY) === "true";
    } catch (err) {
      return false;
    }
  }

  function persistCleanupMode(value) {
    try {
      window.sessionStorage.setItem(
        CLEANUP_MODE_STORAGE_KEY,
        value ? "true" : "false"
      );
    } catch (err) {
      // ignore
    }
  }

  function restoreCleanupThreshold() {
    try {
      const raw = window.sessionStorage.getItem(CLEANUP_THRESHOLD_STORAGE_KEY);
      return normalizeThresholdDays(raw);
    } catch (err) {
      return CLEANUP_DEFAULT_THRESHOLD_DAYS;
    }
  }

  function persistCleanupThreshold(days) {
    try {
      window.sessionStorage.setItem(
        CLEANUP_THRESHOLD_STORAGE_KEY,
        String(normalizeThresholdDays(days))
      );
    } catch (err) {
      // ignore
    }
  }

  function normalizeThresholdDays(value) {
    const parsed = parseInt(value, 10);
    if (!Number.isFinite(parsed) || parsed < 0) {
      return CLEANUP_DEFAULT_THRESHOLD_DAYS;
    }
    return parsed;
  }

  // ---------------------------------------------------------------------
  // Queue refresh loop

  function scheduleRefresh() {
    stopQueueTimer();
    state.refreshTimer = window.setTimeout(() => {
      refresh().finally(() => {
        if (shouldPollQueue()) scheduleRefresh();
      });
    }, REFRESH_INTERVAL_MS);
  }

  function shouldPollQueue() {
    return (
      state.route.name === "queue" ||
      (state.route.name === "detail" && isDesktopShell())
    );
  }

  function stopQueueTimer() {
    if (state.refreshTimer !== null) {
      window.clearTimeout(state.refreshTimer);
      state.refreshTimer = null;
    }
  }

  async function refresh() {
    if (state.fetchInFlight) return;
    state.fetchInFlight = true;
    setRefreshStatus("loading", "refreshing…");
    try {
      const body = await apiRequest("/api/queue");
      if (!body || body.ok !== true) {
        const reason = body && body.reason ? body.reason : "unknown";
        setRefreshStatus("error", "error: " + reason);
        markQueueStale(reason);
        return;
      }
      const ts = new Date();
      renderQueue(body, ts);
      setRefreshStatus("idle", "updated " + ts.toLocaleTimeString());
    } catch (err) {
      setRefreshStatus("error", "error: " + describeError(err));
      markQueueStale(describeError(err));
    } finally {
      state.fetchInFlight = false;
    }
  }

  function describeError(err) {
    if (err && typeof err.message === "string" && err.message) {
      return err.message;
    }
    return String(err);
  }

  function setRefreshStatus(stateName, text) {
    const el = document.getElementById("wb-refresh-status");
    if (!el) return;
    el.setAttribute("data-state", stateName);
    el.textContent = text;
  }

  // ---------------------------------------------------------------------
  // API client

  async function apiRequest(path, options) {
    const opts = options || {};
    const headers = Object.assign({ Accept: "application/json" }, opts.headers || {});
    if (state.token && opts.method && opts.method !== "GET") {
      headers["Authorization"] = "Bearer " + state.token;
    }
    const init = {
      method: opts.method || "GET",
      headers: headers,
      cache: "no-store",
    };
    if (opts.body !== undefined) {
      init.body = JSON.stringify(opts.body);
      headers["Content-Type"] = "application/json";
    }
    const response = await window.fetch(path, init);
    const text = await response.text();
    let parsed = {};
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch (err) {
        throw new Error("invalid JSON response: " + err.message);
      }
    }
    if (!response.ok && (!parsed || parsed.ok !== false)) {
      throw new Error("HTTP " + response.status);
    }
    return parsed;
  }

  // ---------------------------------------------------------------------
  // Queue rendering

  function renderQueue(payload, updatedAt) {
    const queueEl = document.getElementById("wb-queue");
    if (!queueEl) return;
    const scrollSnapshot = captureQueueScroll();
    const previewBody = document.getElementById("wb-preview-last-message-body");
    if (previewBody) scrollSnapshot.preview = previewBody.scrollTop;
    const plansList = Array.isArray(payload.plans) ? payload.plans : [];
    reconcileSelectedPlan(plansList);
    const sorted = plansList.slice().sort(comparePlanRows);
    const counts = computeCounts(sorted);
    renderCounts(counts);
    renderGlobalWarnings(payload);
    renderQueueMeta(payload, sorted.length, updatedAt);

    const seen = new Set();
    const visible = sorted.filter((plan) => isVisible(plan, state.activeFilter));

    const emptyEl = document.getElementById("wb-empty");
    if (visible.length === 0) {
      renderEmptyState(plansList);
    } else if (emptyEl) {
      emptyEl.hidden = true;
    }

    let previous = emptyEl;
    visible.forEach((plan) => {
      const row = upsertRow(plan);
      seen.add(plan.plan_dir);
      if (row.parentNode !== queueEl) {
        queueEl.appendChild(row);
      }
      if (previous && previous.nextSibling !== row) {
        queueEl.insertBefore(row, previous.nextSibling);
      }
      previous = row;
    });

    state.rowsByPlan.forEach((row, planDir) => {
      if (!seen.has(planDir)) {
        if (row.parentNode === queueEl) {
          queueEl.removeChild(row);
        }
      }
    });

    state.lastVisiblePlans = visible;
    updatePreview(visible);
    restoreQueueScroll(scrollSnapshot);
  }

  function renderQueueMeta(payload, planCount, updatedAt) {
    const el = document.getElementById("wb-queue-meta");
    if (!el) return;
    const stateRoot =
      typeof payload.state_root === "string" && payload.state_root
        ? payload.state_root
        : null;
    const updatedText =
      updatedAt instanceof Date
        ? updatedAt.toLocaleTimeString()
        : new Date().toLocaleTimeString();
    state.lastQueueMeta = {
      stateRoot: stateRoot,
      stateRootExists: payload.state_root_exists !== false,
      planCount: planCount,
      updatedText: updatedText,
    };
    el.hidden = false;
    el.removeAttribute("data-state");
    el.textContent = formatQueueMeta(state.lastQueueMeta);
  }

  function markQueueStale(reason) {
    const el = document.getElementById("wb-queue-meta");
    if (!el) return;
    el.hidden = false;
    el.setAttribute("data-state", "stale");
    const message = reason ? "queue fetch failed: " + reason : "queue fetch failed";
    if (!state.lastQueueMeta) {
      el.textContent = message;
      return;
    }
    el.textContent = message + " | stale " + formatQueueMeta(state.lastQueueMeta);
  }

  function formatQueueMeta(meta) {
    const parts = [];
    if (meta.stateRoot) {
      parts.push("state root: " + meta.stateRoot);
      if (!meta.stateRootExists) parts.push("state root missing");
    }
    if (typeof meta.planCount === "number") {
      parts.push("plans: " + meta.planCount);
    }
    if (meta.updatedText) {
      parts.push("updated: " + meta.updatedText);
    }
    return parts.join(" | ");
  }

  function comparePlanRows(a, b) {
    return comparePlanRowsFor(state.sortMode, a, b);
  }

  function comparePlanRowsFor(sortMode, a, b) {
    if (sortMode === "label") {
      const cmp = compareLabels(a, b);
      if (cmp !== 0) return cmp;
      return compareByUrgencyRank(a, b);
    }
    if (sortMode === "recent") {
      const cmp = compareByActivity(a, b);
      if (cmp !== 0) return cmp;
      const rank = compareByUrgencyRank(a, b);
      if (rank !== 0) return rank;
      return compareLabels(a, b);
    }
    if (sortMode === "path") {
      const cmp = compareByPath(a, b);
      if (cmp !== 0) return cmp;
      const rank = compareByUrgencyRank(a, b);
      if (rank !== 0) return rank;
      return compareLabels(a, b);
    }
    if (sortMode === "oldest") {
      const cmp = compareByOldestActivity(a, b);
      if (cmp !== 0) return cmp;
      const rank = compareByUrgencyRank(a, b);
      if (rank !== 0) return rank;
      return compareLabels(a, b);
    }
    // Default: urgency tier first, freshest activity within tier, then label.
    const rank = compareByUrgencyRank(a, b);
    if (rank !== 0) return rank;
    const cmp = compareByActivity(a, b);
    if (cmp !== 0) return cmp;
    return compareLabels(a, b);
  }

  function compareByUrgencyRank(a, b) {
    const rankA = urgencyRankFor(a);
    const rankB = urgencyRankFor(b);
    if (rankA !== rankB) return rankA - rankB;
    return 0;
  }

  function compareByActivity(a, b) {
    // Newer activity first; missing timestamps sort last.
    const tsA = parseActivityTimestamp(a && a.last_activity_at);
    const tsB = parseActivityTimestamp(b && b.last_activity_at);
    if (tsA === tsB) return 0;
    if (tsA === null) return 1;
    if (tsB === null) return -1;
    return tsB - tsA;
  }

  function compareByOldestActivity(a, b) {
    // Oldest first; missing timestamps sort as oldest (first) so never-run and
    // completion-less plans surface at the top of the cleanup candidate list.
    // Complete plans compare by completion time; all others by last activity.
    const tsA = oldestSortTimestampFor(a);
    const tsB = oldestSortTimestampFor(b);
    if (tsA === tsB) return 0;
    if (tsA === null) return -1;
    if (tsB === null) return 1;
    return tsA - tsB;
  }

  function oldestSortTimestampFor(plan) {
    if (plan && plan.primary_state === "complete") {
      return parseActivityTimestamp(plan.completed_at);
    }
    return parseActivityTimestamp(plan && plan.last_activity_at);
  }

  function compareLabels(a, b) {
    const labelA = (a.label || a.plan_dir || "").toLowerCase();
    const labelB = (b.label || b.plan_dir || "").toLowerCase();
    return labelA.localeCompare(labelB);
  }

  function compareByPath(a, b) {
    const pathA = (a && a.plan_dir ? a.plan_dir : "").toLowerCase();
    const pathB = (b && b.plan_dir ? b.plan_dir : "").toLowerCase();
    return pathA.localeCompare(pathB);
  }

  function urgencyRankFor(plan) {
    if (plan && typeof plan.urgency_rank === "number") {
      return plan.urgency_rank;
    }
    return stateOrder(plan && plan.primary_state);
  }

  function parseActivityTimestamp(value) {
    if (typeof value !== "string" || !value) return null;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function stateOrder(primaryState) {
    // Legacy fallback when payload pre-dates triage fields. Keep aligned
    // with workbench.URGENCY_RANK_BY_STATE ordering so visual tiers stay
    // stable across server upgrades.
    const ranking = {
      "paused-for-reply": 10,
      "failed": 20,
      "stale": 30,
      "invalid": 40,
      "verifying": 50,
      "running": 60,
      "recent": 70,
      "ready": 80,
      "complete": 90,
    };
    if (Object.prototype.hasOwnProperty.call(ranking, primaryState)) {
      return ranking[primaryState];
    }
    return 990;
  }

  function computeCounts(plans) {
    const counts = { all: plans.length };
    Object.keys(FILTER_GROUPS).forEach((name) => {
      if (name === "all") return;
      counts[name] = 0;
    });
    plans.forEach((plan) => {
      Object.keys(FILTER_GROUPS).forEach((name) => {
        if (name === "all") return;
        if (FILTER_GROUPS[name].indexOf(plan.primary_state) !== -1) {
          counts[name] += 1;
        }
      });
    });
    return counts;
  }

  function renderCounts(counts) {
    document.querySelectorAll(".wb-chip__count").forEach((el) => {
      const name = el.getAttribute("data-count");
      const value = counts[name];
      el.textContent = String(typeof value === "number" ? value : 0);
    });
  }

  function renderGlobalWarnings(payload) {
    const el = document.getElementById("wb-warnings");
    if (!el) return;
    const items = [];
    appendWarnings(items, payload.registry_warnings, "registry");
    appendWarnings(items, payload.discovery_warnings, "discovery");
    if (items.length === 0) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    el.innerHTML = "<ul></ul>";
    const list = el.querySelector("ul");
    items.forEach((entry) => {
      const li = document.createElement("li");
      li.textContent = entry;
      list.appendChild(li);
    });
  }

  function appendWarnings(items, source, prefix) {
    if (!Array.isArray(source)) return;
    source.forEach((warning) => {
      if (typeof warning === "string" && warning) {
        items.push("[" + prefix + "] " + warning);
      }
    });
  }

  function isVisible(plan, activeFilter) {
    if (!matchesFilter(plan, activeFilter)) return false;
    if (!matchesCompletedVisibility(plan, activeFilter, state.showCompleted)) {
      return false;
    }
    if (!matchesSearchText(plan, state.searchText)) return false;
    return true;
  }

  function matchesFilter(plan, activeFilter) {
    if (activeFilter === "all") return true;
    const group = FILTER_GROUPS[activeFilter] || [];
    return group.indexOf(plan.primary_state) !== -1;
  }

  function matchesCompletedVisibility(plan, activeFilter, showCompleted) {
    // The "needs attention" default hides completed plans unless the
    // operator either toggles them back on or explicitly picks the
    // "complete" chip.
    //
    // Cleanup mode reveals completed plans for review without mutating the
    // persisted wb.completed preference, so stale completed plans surface in
    // the cleanup list even when the queue default would hide them.
    if (state.cleanupMode) return true;
    if (showCompleted) return true;
    if (activeFilter === "complete") return true;
    const group = plan && plan.urgency_group;
    if (group === "complete") return false;
    if (plan && plan.primary_state === "complete") return false;
    return true;
  }

  function matchesSearchText(plan, searchText) {
    if (!searchText) return true;
    const haystack = searchableTextFor(plan);
    if (!haystack) return false;
    return haystack.indexOf(searchText) !== -1;
  }

  function searchableTextFor(plan) {
    if (!plan) return "";
    if (typeof plan.searchable_text === "string" && plan.searchable_text) {
      return plan.searchable_text;
    }
    // Defensive fallback for legacy payloads pre-dating session 1.
    const parts = [
      plan.label,
      plan.plan_dir,
      plan.primary_state,
      plan.recommended_action,
      plan.next_action_hint,
      plan.detail,
    ];
    return parts
      .filter((part) => typeof part === "string" && part)
      .join(" ")
      .toLowerCase()
      .replace(/\s+/g, " ")
      .trim();
  }

  function upsertRow(plan) {
    let row = state.rowsByPlan.get(plan.plan_dir);
    if (!row) {
      row = buildRow(plan.plan_dir);
      state.rowsByPlan.set(plan.plan_dir, row);
    }
    populateRow(row, plan);
    const isSelected = state.selectedPlan === plan.plan_dir;
    row.setAttribute("data-selected", isSelected ? "true" : "false");
    return row;
  }

  function reconcileSelectedPlan(plansList) {
    if (!state.selectedPlan) return;
    const stillPresent = plansList.some(
      (plan) => plan && plan.plan_dir === state.selectedPlan
    );
    if (!stillPresent) {
      // Plan vanished from the payload (deleted on disk, hidden by a
      // discovery scope change, etc.). Drop the stale selection so the
      // next click on a visible row can take over.
      setSelectedPlan(null);
    }
  }

  // ---------------------------------------------------------------------
  // Plan preview panel — desktop right-pane summary keyed off the selected
  // plan (falls back to the highest-priority visible plan).

  const ACTIVITY_SOURCE_LABELS = {
    "active-verification": "active verification",
    "active-turn": "active turn",
    "run-finished": "run finished",
    "run-started": "run started",
    "lock-created": "lock created",
    "progress-journal": "progress journal",
  };

  function previewPlanFor(visibleSorted) {
    if (!Array.isArray(visibleSorted) || visibleSorted.length === 0) {
      return null;
    }
    if (state.selectedPlan) {
      const match = visibleSorted.find(
        (plan) => plan && plan.plan_dir === state.selectedPlan
      );
      if (match) return match;
    }
    // visibleSorted is already ordered by the active sort mode (urgency
    // first by default), so the first entry is the highest-priority plan.
    return visibleSorted[0] || null;
  }

  function updatePreview(visibleSorted) {
    const next = previewPlanFor(visibleSorted);
    state.previewPlan = next;
    renderPreview(next);
    applyShellLayout();
  }

  function renderPreview(plan) {
    const panel = document.getElementById("wb-shell-preview");
    if (!panel) return;
    if (!plan) return;

    const labelEl = document.getElementById("wb-preview-label");
    if (labelEl) {
      labelEl.textContent = plan.label || displayName(plan.plan_dir);
    }
    const pathEl = document.getElementById("wb-preview-path");
    if (pathEl) pathEl.textContent = plan.plan_dir || "";

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
      const hint = plan.next_action_hint || "";
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
        "#plan?dir=" + encodeURIComponent(plan.plan_dir)
      );
      openEl.setAttribute("data-plan-dir", plan.plan_dir);
    }
  }

  function formatLastActivity(value) {
    if (typeof value !== "string" || !value) return "no recent activity";
    const parsed = Date.parse(value);
    if (!Number.isFinite(parsed)) return value;
    try {
      return new Date(parsed).toLocaleString();
    } catch (err) {
      return value;
    }
  }

  function formatCompletedAt(plan) {
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

  function wirePreviewOpen() {
    const openEl = document.getElementById("wb-preview-open");
    if (!openEl) return;
    openEl.addEventListener("click", (event) => {
      const planDir = openEl.getAttribute("data-plan-dir");
      if (!planDir) return;
      event.preventDefault();
      navigateToDetail(planDir);
    });
  }

  function classifyEmptyState(plansList) {
    if (!Array.isArray(plansList) || plansList.length === 0) {
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
      const tail = [];
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
        canResetFilter: state.activeFilter !== "all",
      };
    }

    if (!filterMatchedAtAll && state.activeFilter !== "all") {
      const tail = [];
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
        canResetFilter: state.activeFilter !== "all",
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

  function describeEmptyState(plansList) {
    const info = classifyEmptyState(plansList);
    const parts = [info.primary];
    if (info.secondary) parts.push(info.secondary);
    return parts.join(" | ");
  }

  function renderEmptyState(plansList) {
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

  function buildRow(planDir) {
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

  function selectPlanFromRow(planDir) {
    const changed = setSelectedPlan(planDir);
    if (!changed) return;
    syncSelectedPlanView();
  }

  function syncSelectedPlanView() {
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

  function populateRow(row, plan) {
    const badge = row.querySelector('[data-field="state"]');
    badge.textContent = plan.primary_state || "unknown";
    badge.setAttribute("data-state", plan.primary_state || "unknown");

    const label = row.querySelector('[data-field="label"]');
    label.textContent = plan.label || displayName(plan.plan_dir);
    label.setAttribute("href", "#plan?dir=" + encodeURIComponent(plan.plan_dir));

    const path = row.querySelector('[data-field="path"]');
    path.textContent = plan.plan_dir;

    const session = row.querySelector('[data-field="session"]');
    session.textContent = plan.next_pending_session_id
      ? "next " + plan.next_pending_session_id
      : "-";

    const harness = row.querySelector('[data-field="harness"]');
    harness.textContent = plan.harness || "-";

    row.querySelector('[data-field="elapsed"]').textContent = "-";
    row.querySelector('[data-field="idle"]').textContent = "-";
    row.querySelector('[data-field="run"]').textContent = plan.run_id || "-";
    row.querySelector('[data-field="counts"]').textContent = formatCounts(plan.counts);
    row.querySelector('[data-field="completed"]').textContent = formatCompletedAt(plan);

    const hint = row.querySelector('[data-field="hint"]');
    hint.textContent = plan.next_action_hint || plan.detail || "";

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

  function populateCleanupCell(row, plan) {
    const cell = row.querySelector('[data-field="cleanup-select"]');
    if (!cell) return;
    const box = cell.querySelector("input");
    cell.hidden = !state.cleanupMode;
    const archivable = plan.archivable !== false;
    if (box) {
      box.checked = state.cleanupSelection.has(plan.plan_dir);
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

  function displayName(planDir) {
    if (!planDir) return "(unknown plan)";
    const parts = planDir.split("/");
    return parts[parts.length - 1] || planDir;
  }

  function formatCounts(counts) {
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

  function describeAction(action) {
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

  function populateArtifacts(row, plan) {
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

  // ---------------------------------------------------------------------
  // Search / sort / completed-visibility controls

  function hydrateQueueControls() {
    const searchInput = document.getElementById("wb-search");
    if (searchInput) searchInput.value = state.searchText || "";
    const sortSelect = document.getElementById("wb-sort");
    if (sortSelect) {
      // Defensive: only assign if the option exists in the markup.
      if (SORT_MODES.indexOf(state.sortMode) !== -1) {
        sortSelect.value = state.sortMode;
      } else {
        sortSelect.value = DEFAULT_SORT_MODE;
      }
    }
    const completedToggle = document.getElementById("wb-show-completed");
    if (completedToggle) completedToggle.checked = !!state.showCompleted;
    const thresholdInput = document.getElementById("wb-cleanup-threshold");
    if (thresholdInput) thresholdInput.value = String(state.cleanupThresholdDays);
    updateSearchClearVisibility();
    applyCleanupMode();
  }

  function updateSearchClearVisibility() {
    const clearBtn = document.getElementById("wb-search-clear");
    if (!clearBtn) return;
    clearBtn.hidden = !state.searchText;
  }

  function scheduleSearchRefresh() {
    if (state.searchDebounceTimer !== null) {
      window.clearTimeout(state.searchDebounceTimer);
    }
    state.searchDebounceTimer = window.setTimeout(() => {
      state.searchDebounceTimer = null;
      refresh();
    }, SEARCH_DEBOUNCE_MS);
  }

  function wireSearchControl() {
    const input = document.getElementById("wb-search");
    if (input) {
      input.addEventListener("input", () => {
        if (setSearchText(input.value)) {
          updateSearchClearVisibility();
          scheduleSearchRefresh();
        }
      });
    }
    const clearBtn = document.getElementById("wb-search-clear");
    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        if (setSearchText("")) {
          if (input) input.value = "";
          updateSearchClearVisibility();
          refresh();
        }
      });
    }
  }

  function wireSortControl() {
    const select = document.getElementById("wb-sort");
    if (!select) return;
    select.addEventListener("change", () => {
      if (setSortMode(select.value)) {
        refresh();
      }
    });
  }

  function wireShowCompletedControl() {
    const toggle = document.getElementById("wb-show-completed");
    if (!toggle) return;
    toggle.addEventListener("change", () => {
      if (setShowCompleted(toggle.checked)) {
        refresh();
      }
    });
  }

  // ---------------------------------------------------------------------
  // Cleanup mode — bulk review + archive of stale plans

  function wireCleanupControls() {
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
  }

  function setCleanupMode(enabled) {
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
    }
    applyCleanupMode();
    refresh();
  }

  function applyCleanupMode() {
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

  function planIsArchivableCandidate(plan) {
    // Cleanup candidates are non-active plans whose stale age is older than
    // the operator threshold; plans with no timestamp always qualify. Complete
    // plans age off their completion time (completed_at) while everything else
    // uses last activity, matching the completion-aware oldest sort.
    if (!plan || plan.archivable === false) return false;
    const ts = oldestSortTimestampFor(plan);
    if (ts === null) return true;
    const ageDays = (Date.now() - ts) / DAY_MS;
    return ageDays >= state.cleanupThresholdDays;
  }

  function selectCandidates() {
    // "select candidates" keeps the stale-threshold behavior: from the
    // currently visible rows, add only those older than the threshold.
    const visible = state.lastVisiblePlans || [];
    visible.forEach((plan) => {
      if (planIsArchivableCandidate(plan)) {
        state.cleanupSelection.add(plan.plan_dir);
      }
    });
    syncCleanupCheckboxes();
    updateCleanupReview();
  }

  function selectVisibleRows() {
    // "select visible" adds every currently visible archivable row after the
    // active filter/search/sort/completed-visibility gates have run. Rows with
    // an active lock (archivable === false) are skipped, preserving the same
    // blocking the per-row checkbox enforces.
    const visible = state.lastVisiblePlans || [];
    visible.forEach((plan) => {
      if (plan && plan.archivable !== false && plan.plan_dir) {
        state.cleanupSelection.add(plan.plan_dir);
      }
    });
    syncCleanupCheckboxes();
    updateCleanupReview();
  }

  function clearCleanupSelection() {
    if (state.cleanupSelection.size === 0) return;
    state.cleanupSelection.clear();
    syncCleanupCheckboxes();
    updateCleanupReview();
  }

  function setCleanupSelected(planDir, selected) {
    if (!planDir) return;
    if (selected) {
      state.cleanupSelection.add(planDir);
    } else {
      state.cleanupSelection.delete(planDir);
    }
    updateCleanupReview();
  }

  function syncCleanupCheckboxes() {
    state.rowsByPlan.forEach((row, planDir) => {
      const box = row.querySelector('[data-field="cleanup-select"] input');
      if (box) box.checked = state.cleanupSelection.has(planDir);
    });
  }

  function updateCleanupReview() {
    const count = state.cleanupSelection.size;
    const countEl = document.getElementById("wb-cleanup-count");
    if (countEl) {
      countEl.textContent =
        count === 1 ? "1 plan selected" : count + " plans selected";
    }
    const archiveBtn = document.getElementById("wb-cleanup-archive");
    if (archiveBtn) archiveBtn.disabled = count === 0;
    const clearBtn = document.getElementById("wb-cleanup-clear");
    if (clearBtn) clearBtn.disabled = count === 0;
  }

  async function handleBulkArchive() {
    const planDirs = Array.from(state.cleanupSelection);
    if (planDirs.length === 0) return;
    const confirmed = window.confirm(
      "Archive " +
        planDirs.length +
        " selected plan(s)? Archived plans move to the Workbench archive and can be restored until retention expires."
    );
    if (!confirmed) return;
    showCleanupFeedback("", "");
    try {
      const body = await apiRequest("/api/plans/archive", {
        method: "POST",
        body: { plan_dirs: planDirs },
      });
      if (body && body.ok) {
        const archived =
          typeof body.archived_count === "number" ? body.archived_count : 0;
        const failed =
          typeof body.failed_count === "number" ? body.failed_count : 0;
        state.cleanupSelection.clear();
        updateCleanupReview();
        showCleanupFeedback(
          failed > 0 ? "warning" : "info",
          "archived " + archived + (failed ? ", " + failed + " failed" : "")
        );
        refresh();
      } else {
        showCleanupFeedback(
          "error",
          (body && body.error) || "bulk archive failed"
        );
      }
    } catch (err) {
      showCleanupFeedback("error", describeError(err));
    }
  }

  function showCleanupFeedback(kind, message) {
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

  // ---------------------------------------------------------------------
  // Archive view — restore, permanent purge, retention

  function wireArchiveControls() {
    const back = document.getElementById("wb-archive-back");
    if (back) {
      back.addEventListener("click", navigateToQueue);
    }
    const refreshBtn = document.getElementById("wb-archive-refresh");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", loadArchiveView);
    }
    const retentionSave = document.getElementById("wb-archive-retention-save");
    if (retentionSave) {
      retentionSave.addEventListener("click", handleRetentionSave);
    }
    const purgeSelected = document.getElementById("wb-archive-purge-selected");
    if (purgeSelected) {
      purgeSelected.addEventListener("click", () => {
        handleArchivePurge(Array.from(state.archiveSelection));
      });
    }
    const clearSel = document.getElementById("wb-archive-clear");
    if (clearSel) {
      clearSel.addEventListener("click", () => {
        state.archiveSelection.clear();
        renderArchiveEntries(state.archiveEntries);
        updateArchiveBulk();
      });
    }
  }

  async function loadArchiveView() {
    if (state.archiveInFlight) return;
    state.archiveInFlight = true;
    setRefreshStatus("loading", "refreshing…");
    try {
      const body = await apiRequest("/api/archive");
      if (!body || body.ok !== true) {
        renderArchiveError((body && body.error) || "failed to load archive");
        setRefreshStatus("error", "error: archive");
        return;
      }
      state.archiveEntries = Array.isArray(body.entries) ? body.entries : [];
      state.archiveRetentionDays =
        typeof body.archive_retention_days === "number"
          ? body.archive_retention_days
          : null;
      state.archiveMeta = body.archive_maintenance || null;
      reconcileArchiveSelection();
      renderArchiveView(body);
      setRefreshStatus("idle", "updated " + new Date().toLocaleTimeString());
    } catch (err) {
      renderArchiveError(describeError(err));
      setRefreshStatus("error", "error: " + describeError(err));
    } finally {
      state.archiveInFlight = false;
    }
  }

  function reconcileArchiveSelection() {
    const present = new Set(
      state.archiveEntries.map((entry) => entry && entry.archive_id)
    );
    Array.from(state.archiveSelection).forEach((id) => {
      if (!present.has(id)) state.archiveSelection.delete(id);
    });
  }

  function renderArchiveError(message) {
    const list = document.getElementById("wb-archive-entries");
    if (!list) return;
    list.innerHTML = "";
    const li = document.createElement("li");
    li.className = "wb-empty";
    li.textContent = "archive unavailable: " + String(message);
    list.appendChild(li);
  }

  function renderArchiveView(payload) {
    const retentionInput = document.getElementById("wb-archive-retention");
    if (retentionInput && typeof payload.archive_retention_days === "number") {
      retentionInput.value = String(payload.archive_retention_days);
    }
    renderArchiveMaintenance(payload.archive_maintenance || null);
    renderArchiveWarnings(payload.warnings || []);
    renderArchiveEntries(state.archiveEntries);
    updateArchiveBulk();
  }

  function renderArchiveMaintenance(maintenance) {
    const el = document.getElementById("wb-archive-maintenance");
    if (!el) return;
    const removed =
      maintenance && typeof maintenance.removed_count === "number"
        ? maintenance.removed_count
        : 0;
    const failed =
      maintenance && typeof maintenance.failed_count === "number"
        ? maintenance.failed_count
        : 0;
    const warnings =
      maintenance && Array.isArray(maintenance.warnings)
        ? maintenance.warnings
        : [];
    if (removed === 0 && failed === 0 && warnings.length === 0) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    const parts = ["auto-empty: " + removed + " purged"];
    if (failed) parts.push(failed + " failed");
    if (warnings.length) parts.push(warnings.join("; "));
    el.textContent = parts.join(" | ");
  }

  function renderArchiveWarnings(warnings) {
    const el = document.getElementById("wb-archive-warnings");
    if (!el) return;
    const items = Array.isArray(warnings)
      ? warnings.filter((w) => typeof w === "string" && w)
      : [];
    if (items.length === 0) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    el.innerHTML = "<ul></ul>";
    const ul = el.querySelector("ul");
    items.forEach((w) => {
      const li = document.createElement("li");
      li.textContent = w;
      ul.appendChild(li);
    });
  }

  function renderArchiveEntries(entries) {
    const list = document.getElementById("wb-archive-entries");
    if (!list) return;
    list.innerHTML = "";
    if (!Array.isArray(entries) || entries.length === 0) {
      const li = document.createElement("li");
      li.className = "wb-empty";
      li.textContent = "no archived plans";
      list.appendChild(li);
      return;
    }
    const tmpl = document.getElementById("wb-archive-row-template");
    entries.forEach((entry) => {
      const fragment = tmpl.content.cloneNode(true);
      const li = fragment.querySelector("li");
      const archiveId =
        entry && entry.archive_id ? String(entry.archive_id) : "";
      li.setAttribute("data-archive-id", archiveId);
      setField(
        li,
        "label",
        entry.summary || displayName(entry.original_plan_dir)
      );
      setField(li, "path", entry.original_plan_dir || "");
      setField(
        li,
        "archived",
        "archived: " + formatLastActivity(entry.archived_at)
      );
      setField(
        li,
        "expires",
        "expires: " + formatLastActivity(entry.expires_at)
      );
      const box = li.querySelector('[data-field="archive-select"] input');
      if (box) {
        box.checked = state.archiveSelection.has(archiveId);
        box.addEventListener("change", () => {
          if (box.checked) {
            state.archiveSelection.add(archiveId);
          } else {
            state.archiveSelection.delete(archiveId);
          }
          updateArchiveBulk();
        });
      }
      const restoreBtn = li.querySelector('[data-action="archive-restore"]');
      if (restoreBtn) {
        restoreBtn.addEventListener("click", () =>
          handleArchiveRestore(archiveId)
        );
      }
      const purgeBtn = li.querySelector('[data-action="archive-purge"]');
      if (purgeBtn) {
        purgeBtn.addEventListener("click", () =>
          handleArchivePurge([archiveId])
        );
      }
      list.appendChild(li);
    });
  }

  function updateArchiveBulk() {
    const count = state.archiveSelection.size;
    const countEl = document.getElementById("wb-archive-count");
    if (countEl) {
      countEl.textContent =
        count === 1 ? "1 entry selected" : count + " entries selected";
    }
    const purgeBtn = document.getElementById("wb-archive-purge-selected");
    if (purgeBtn) purgeBtn.disabled = count === 0;
    const clearBtn = document.getElementById("wb-archive-clear");
    if (clearBtn) clearBtn.disabled = count === 0;
  }

  async function handleArchiveRestore(archiveId) {
    if (!archiveId) return;
    showArchiveFeedback("", "");
    try {
      const body = await apiRequest("/api/archive/restore", {
        method: "POST",
        body: { archive_id: archiveId },
      });
      if (body && body.ok) {
        showArchiveFeedback("info", "restored " + archiveId);
        loadArchiveView();
      } else {
        showArchiveFeedback("error", (body && body.error) || "restore failed");
      }
    } catch (err) {
      showArchiveFeedback("error", describeError(err));
    }
  }

  async function handleArchivePurge(archiveIds) {
    const ids = Array.isArray(archiveIds) ? archiveIds.filter(Boolean) : [];
    if (ids.length === 0) return;
    const typed = window.prompt(
      "Permanently purge " +
        ids.length +
        " archived plan(s)? This cannot be undone. Type the number of entries to confirm:",
      ""
    );
    if (typed === null) return;
    const confirmCount = parseInt((typed || "").trim(), 10);
    if (!Number.isFinite(confirmCount) || confirmCount !== ids.length) {
      showArchiveFeedback(
        "error",
        "confirmation count must equal " + ids.length
      );
      return;
    }
    showArchiveFeedback("", "");
    try {
      const body = await apiRequest("/api/archive/delete", {
        method: "POST",
        body: { archive_ids: ids, confirm_count: confirmCount },
      });
      if (body && body.ok) {
        const removed =
          typeof body.removed_count === "number" ? body.removed_count : 0;
        ids.forEach((id) => state.archiveSelection.delete(id));
        showArchiveFeedback("info", "purged " + removed);
        loadArchiveView();
      } else {
        showArchiveFeedback("error", (body && body.error) || "purge failed");
      }
    } catch (err) {
      showArchiveFeedback("error", describeError(err));
    }
  }

  async function handleRetentionSave() {
    const input = document.getElementById("wb-archive-retention");
    if (!input) return;
    const days = parseInt((input.value || "").trim(), 10);
    if (!Number.isFinite(days) || days < 1) {
      showArchiveFeedback(
        "error",
        "retention must be a positive number of days"
      );
      return;
    }
    showArchiveFeedback("", "");
    try {
      const body = await apiRequest("/api/archive/settings", {
        method: "POST",
        body: { archive_retention_days: days },
      });
      if (body && body.ok) {
        state.archiveRetentionDays =
          typeof body.archive_retention_days === "number"
            ? body.archive_retention_days
            : days;
        showArchiveFeedback(
          "info",
          "retention set to " + state.archiveRetentionDays + " days"
        );
        loadArchiveView();
      } else {
        showArchiveFeedback(
          "error",
          (body && body.error) || "failed to update retention"
        );
      }
    } catch (err) {
      showArchiveFeedback("error", describeError(err));
    }
  }

  function showArchiveFeedback(kind, message) {
    const el = document.getElementById("wb-archive-feedback");
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

  function wireEmptyStateActions() {
    const clearSearch = document.getElementById("wb-empty-clear-search");
    if (clearSearch) {
      clearSearch.addEventListener("click", () => {
        if (setSearchText("")) {
          const input = document.getElementById("wb-search");
          if (input) input.value = "";
          updateSearchClearVisibility();
          refresh();
        }
      });
    }
    const showCompleted = document.getElementById("wb-empty-show-completed");
    if (showCompleted) {
      showCompleted.addEventListener("click", () => {
        if (setShowCompleted(true)) {
          const toggle = document.getElementById("wb-show-completed");
          if (toggle) toggle.checked = true;
          refresh();
        }
      });
    }
    const resetFilter = document.getElementById("wb-empty-reset-filter");
    if (resetFilter) {
      resetFilter.addEventListener("click", () => {
        if (state.activeFilter === "all") return;
        state.activeFilter = "all";
        persistFilter("all");
        applyChipPressedState();
        refresh();
      });
    }
  }

  // ---------------------------------------------------------------------
  // Filter chips

  function wireFilterChips() {
    document.querySelectorAll(".wb-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        const name = chip.getAttribute("data-filter");
        if (!name || !Object.prototype.hasOwnProperty.call(FILTER_GROUPS, name)) {
          return;
        }
        if (state.activeFilter === name) return;
        state.activeFilter = name;
        persistFilter(name);
        applyChipPressedState();
        refresh();
      });
    });
  }

  function applyChipPressedState() {
    document.querySelectorAll(".wb-chip").forEach((chip) => {
      const name = chip.getAttribute("data-filter");
      chip.setAttribute(
        "aria-pressed",
        name === state.activeFilter ? "true" : "false"
      );
    });
  }

  function wireRefreshButton() {
    const button = document.getElementById("wb-refresh-now");
    if (!button) return;
    button.addEventListener("click", () => {
      if (state.route.name === "detail") {
        refreshDetail();
      } else {
        refresh();
      }
    });
  }

  // ---------------------------------------------------------------------
  // Detail view

  function scheduleDetailRefresh() {
    stopDetailTimer();
    state.detailTimer = window.setTimeout(() => {
      refreshDetail().finally(() => {
        if (state.route.name === "detail") scheduleDetailRefresh();
      });
    }, DETAIL_REFRESH_INTERVAL_MS);
  }

  function stopDetailTimer() {
    if (state.detailTimer !== null) {
      window.clearTimeout(state.detailTimer);
      state.detailTimer = null;
    }
  }

  async function refreshDetail() {
    if (state.route.name !== "detail" || !state.route.planDir) return;
    if (state.detailInFlight) return;
    state.detailInFlight = true;
    setRefreshStatus("loading", "refreshing…");
    const planDir = state.route.planDir;
    const scrollSnapshot = captureDetailScroll();
    const pendingFocus = state.route.focus;
    try {
      const [planBody, registryBody] = await Promise.all([
        apiRequest("/api/plan?plan_dir=" + encodeURIComponent(planDir)),
        apiRequest("/api/registry"),
      ]);
      if (!planBody || planBody.ok !== true) {
        renderDetailError(
          (planBody && planBody.error) || "failed to load plan"
        );
        return;
      }
      state.detailPlan = planBody.plan || null;
      state.detailRegistry = registryBody && registryBody.registry
        ? registryBody.registry
        : null;
      renderDetailHeader(state.detailPlan);
      renderProfileForm(state.detailPlan, state.detailRegistry);
      const tasks = [
        loadAndRenderRecovery(planDir, state.detailPlan),
        loadAndRenderState(planDir),
        loadAndRenderEvents(planDir),
        loadAndRenderProgress(planDir),
        loadAndRenderLog(planDir, "stdout"),
        loadAndRenderLog(planDir, "stderr"),
        loadAndRenderArtifacts(planDir),
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

  function renderDetailError(message) {
    const labelEl = document.getElementById("wb-detail-label");
    if (labelEl) labelEl.textContent = "(error)";
    const hintEl = document.getElementById("wb-detail-hint");
    if (hintEl) hintEl.textContent = String(message);
  }

  function renderDetailHeader(plan) {
    const labelEl = document.getElementById("wb-detail-label");
    const pathEl = document.getElementById("wb-detail-path");
    const stateEl = document.getElementById("wb-detail-state");
    const actionEl = document.getElementById("wb-detail-action-hint");
    const hintEl = document.getElementById("wb-detail-hint");
    if (!plan) return;
    if (labelEl) labelEl.textContent = plan.label || displayName(plan.plan_dir);
    if (pathEl) pathEl.textContent = plan.plan_dir || "";
    if (stateEl) {
      stateEl.textContent = plan.primary_state || "unknown";
      stateEl.setAttribute("data-state", plan.primary_state || "unknown");
    }
    if (actionEl) {
      actionEl.textContent = plan.recommended_action
        ? "action: " + describeAction(plan.recommended_action)
        : "";
    }
    if (hintEl) hintEl.textContent = plan.next_action_hint || plan.detail || "";

    const pinButton = document.getElementById("wb-control-pin");
    if (pinButton) {
      const pinned = isPinned(plan.plan_dir);
      pinButton.textContent = pinned ? "update pin" : "pin";
      pinButton.setAttribute("data-pinned", pinned ? "true" : "false");
    }
    const unpinButton = document.getElementById("wb-profile-unpin");
    if (unpinButton) {
      unpinButton.disabled = !isPinned(plan.plan_dir);
    }
    const harnessSelect = document.getElementById("wb-control-harness");
    if (harnessSelect && plan.harness) {
      if (HARNESS_NAMES.indexOf(plan.harness) !== -1) {
        harnessSelect.value = plan.harness;
      }
    }
  }

  // ---------------------------------------------------------------------
  // Recovery Center loading

  async function loadAndRenderRecovery(planDir, plan) {
    const compact = plan && plan.recovery ? plan.recovery : null;
    let renderedRecovery = null;
    if (compact) {
      renderedRecovery = compact;
      renderRecoveryPanel(compact);
    }
    try {
      const body = await apiRequest(
        "/api/recovery?plan_dir=" + encodeURIComponent(planDir)
      );
      if (body && body.ok === true && body.recovery) {
        renderedRecovery = body.recovery;
        renderRecoveryPanel(body.recovery);
      } else if (!compact) {
        renderRecoveryError(
          (body && body.error) || "recovery diagnosis unavailable"
        );
        renderedRecovery = null;
      }
    } catch (err) {
      if (compact) {
        const fallback = Object.assign({}, compact, {
          warnings: [
            "Recovery details failed to load: " + describeError(err),
          ],
        });
        renderedRecovery = fallback;
        renderRecoveryPanel(fallback);
      } else {
        renderRecoveryError(describeError(err));
        renderedRecovery = null;
      }
    }
    if (renderedRecovery && renderedRecovery.kind === "reply-needed") {
      await loadAndRenderReply(planDir);
    } else {
      hideReplyPanel();
    }
  }

  // ---------------------------------------------------------------------
  // Recovery Center panel rendering

  function renderRecoveryError(message) {
    renderRecoveryPanel({
      kind: "generic",
      title: "Recovery unavailable",
      summary: String(message || "recovery diagnosis unavailable"),
      severity: "warning",
      recoverable: false,
      evidence: [],
      actions: [],
      commands: [],
      artifacts: [],
      warnings: [],
    });
  }

  function renderRecoveryPanel(recovery) {
    const panel = document.getElementById("wb-recovery-panel");
    if (!panel || !recovery) return;
    const kind = String(recovery.kind || "generic");
    const severity = recoverySeverity(recovery.severity);
    panel.setAttribute("data-severity", severity);

    const titleEl = document.getElementById("wb-recovery-title");
    if (titleEl) titleEl.textContent = recovery.title || "Recovery Center";
    const kindEl = document.getElementById("wb-recovery-kind");
    if (kindEl) {
      kindEl.textContent = kind;
      kindEl.setAttribute("data-kind", kind);
    }
    const summaryEl = document.getElementById("wb-recovery-summary");
    if (summaryEl) {
      summaryEl.textContent =
        recovery.summary || "No recovery diagnosis is available.";
    }

    const actions = Array.isArray(recovery.actions) ? recovery.actions : [];
    const primaryAction =
      recovery.primary_action ||
      (recovery.compact && recovery.compact.primary_action) ||
      null;
    renderRecoveryActions(
      actions.length ? actions : primaryAction ? [primaryAction] : []
    );

    const evidence = Array.isArray(recovery.evidence)
      ? recovery.evidence.slice()
      : [];
    if (Array.isArray(recovery.warnings)) {
      recovery.warnings.forEach((warning) => {
        evidence.push({
          label: "Warning",
          value: warning,
          detail: null,
          severity: "warning",
        });
      });
    }
    renderRecoveryEvidence(evidence);
    renderRecoveryCommands(
      Array.isArray(recovery.commands) ? recovery.commands : []
    );
    renderRecoveryArtifacts(
      Array.isArray(recovery.artifacts) ? recovery.artifacts : []
    );
  }

  function recoverySeverity(value) {
    return ["none", "info", "warning", "error"].indexOf(value) === -1
      ? "none"
      : value;
  }

  // ---------------------------------------------------------------------
  // Recovery Center actions

  function renderRecoveryActions(actions) {
    const host = document.getElementById("wb-recovery-primary-action");
    if (!host) return;
    const cards = actions.filter(Boolean);
    host.innerHTML = "";
    if (!cards.length) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    cards.forEach((action, index) => {
      host.appendChild(createRecoveryActionCard(action, index === 0));
    });
  }

  function renderRecoveryPrimaryAction(action) {
    renderRecoveryActions(action ? [action] : []);
  }

  function createRecoveryActionCard(action, primary) {
    const card = document.createElement("div");
    card.className = "wb-recovery__action-card";
    if (primary) card.setAttribute("data-primary", "true");
    if (action.disabled) card.setAttribute("data-disabled", "true");

    const text = document.createElement("div");
    const title = document.createElement("p");
    title.className = "wb-recovery__action-title";
    title.textContent = action.label || "Primary action";
    text.appendChild(title);
    if (action.description) {
      const description = document.createElement("p");
      description.className = "wb-recovery__action-description";
      description.textContent = action.description;
      text.appendChild(description);
    }
    card.appendChild(text);

    const target = recoveryActionTarget(action);
    const button = document.createElement("button");
    button.className = "wb-button wb-button--primary";
    button.type = "button";
    button.textContent = recoveryActionButtonLabel(action, target);
    button.disabled = Boolean(
      action.disabled || !recoveryActionCanRun(action, target)
    );
    button.addEventListener("click", () => {
      handleRecoveryAction(action);
    });
    card.appendChild(button);
    return card;
  }

  function recoveryActionCanRun(action, target) {
    if (!action) return false;
    const endpoint = action.endpoint ? String(action.endpoint) : "";
    const id = action.id ? String(action.id) : "";
    if (endpoint === "/api/unlock" || id === "unlock") return true;
    if (
      endpoint === "/api/launch" ||
      id === "retry-run" ||
      id === "launch"
    ) {
      return true;
    }
    if (endpoint === "/api/reply" || id === "reply") return Boolean(target);
    return Boolean(target);
  }

  function recoveryActionButtonLabel(action, target) {
    const id = action && action.id ? String(action.id) : "";
    if (id === "unlock" || target === "unlock") return "unlock";
    if (id === "reply" || target === "reply") return "reply";
    if (id === "review-profile" || target === "profile") return "profile";
    if (id === "retry-run" || target === "launch") return "launch";
    if (action && action.kind === "inspect") return "inspect";
    return "open";
  }

  // ---------------------------------------------------------------------
  // Recovery Center evidence

  function renderRecoveryEvidence(rows) {
    const section = document.getElementById("wb-recovery-evidence-section");
    const list = document.getElementById("wb-recovery-evidence");
    if (!section || !list) return;
    list.innerHTML = "";
    if (!rows.length) {
      section.hidden = true;
      return;
    }
    section.hidden = false;
    rows.forEach((row) => {
      const li = document.createElement("li");
      li.className = "wb-recovery__evidence-row";
      li.setAttribute("data-severity", recoveryEvidenceSeverity(row.severity));

      const label = document.createElement("span");
      label.className = "wb-recovery__label";
      label.textContent = row.label || "Evidence";
      li.appendChild(label);

      const value = document.createElement("span");
      value.className = "wb-recovery__value";
      value.textContent =
        row.value === null || row.value === undefined || row.value === ""
          ? "-"
          : String(row.value);
      if (row.detail) {
        const detail = document.createElement("span");
        detail.className = "wb-recovery__detail";
        detail.textContent = row.detail;
        value.appendChild(detail);
      }
      li.appendChild(value);

      const target = recoveryEvidenceTarget(row);
      if (target) {
        li.appendChild(createRecoveryLink(target));
      }
      list.appendChild(li);
    });
  }

  function recoveryEvidenceSeverity(value) {
    return ["info", "warning", "error"].indexOf(value) === -1
      ? "info"
      : value;
  }

  // ---------------------------------------------------------------------
  // Recovery Center commands

  function renderRecoveryCommands(commands) {
    const section = document.getElementById("wb-recovery-commands-section");
    const list = document.getElementById("wb-recovery-commands");
    if (!section || !list) return;
    list.innerHTML = "";
    if (!commands.length) {
      section.hidden = true;
      return;
    }
    section.hidden = false;
    commands.forEach((command) => {
      const li = document.createElement("li");
      li.className = "wb-recovery__command-row";

      const label = document.createElement("span");
      label.className = "wb-recovery__label";
      label.textContent = command.label || "Command";
      li.appendChild(label);

      const body = document.createElement("span");
      const code = document.createElement("code");
      code.className = "wb-recovery__command";
      code.textContent = command.command || "";
      body.appendChild(code);
      if (command.description) {
        const description = document.createElement("span");
        description.className = "wb-recovery__command-description";
        description.textContent = command.description;
        body.appendChild(description);
      }
      li.appendChild(body);

      const button = document.createElement("button");
      button.className = "wb-button";
      button.type = "button";
      button.textContent = "copy";
      button.addEventListener("click", () => {
        copyRecoveryCommand(button, command.command || "");
      });
      li.appendChild(button);

      list.appendChild(li);
    });
  }

  // ---------------------------------------------------------------------
  // Recovery Center artifacts

  function renderRecoveryArtifacts(artifacts) {
    const section = document.getElementById("wb-recovery-artifacts-section");
    const list = document.getElementById("wb-recovery-artifacts");
    if (!section || !list) return;
    list.innerHTML = "";
    if (!artifacts.length) {
      section.hidden = true;
      return;
    }
    section.hidden = false;
    artifacts.forEach((artifact) => {
      const li = document.createElement("li");
      li.className = "wb-recovery__artifact-row";

      const label = document.createElement("span");
      label.className = "wb-recovery__label";
      label.textContent = artifact.label || "Artifact";
      li.appendChild(label);

      const path = document.createElement("code");
      path.className = "wb-recovery__artifact-path";
      path.textContent = artifact.path || "";
      li.appendChild(path);

      const meta = document.createElement("span");
      meta.className = "wb-recovery__artifact-meta";
      meta.textContent = [artifact.kind, artifact.source]
        .filter(Boolean)
        .join(" · ");
      const target = recoveryArtifactTarget(artifact);
      if (target) {
        if (meta.textContent) meta.appendChild(document.createTextNode(" "));
        meta.appendChild(createRecoveryLink(target));
      }
      li.appendChild(meta);
      list.appendChild(li);
    });
  }

  // ---------------------------------------------------------------------
  // Recovery Center target resolution

  function createRecoveryLink(target) {
    const button = document.createElement("button");
    button.className = "wb-recovery__link";
    button.type = "button";
    button.textContent = RECOVERY_TARGET_LABELS[target] || "view";
    button.addEventListener("click", () => focusDetailKey(target));
    return button;
  }

  function isRecoveryActionTarget(target) {
    return RECOVERY_ACTION_TARGETS.indexOf(target) !== -1;
  }

  function normalizeRecoveryActionTarget(target) {
    const value =
      target === null || target === undefined ? "" : String(target).trim();
    if (!value || !isRecoveryActionTarget(value)) return null;
    return Object.prototype.hasOwnProperty.call(DETAIL_FOCUS_TARGETS, value)
      ? value
      : null;
  }

  function hasExplicitRecoveryActionTarget(action) {
    return Boolean(
      action &&
        action.target !== null &&
        action.target !== undefined &&
        String(action.target).trim() !== ""
    );
  }

  function mappedRecoveryActionTarget(action) {
    if (!action || !action.id) return null;
    return normalizeRecoveryActionTarget(
      RECOVERY_ACTION_TARGETS_BY_ID[String(action.id)] || null
    );
  }

  function legacyRecoveryActionTarget(action) {
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

  function recoveryActionTarget(action) {
    if (!action) return null;
    const explicit = normalizeRecoveryActionTarget(action.target);
    if (explicit) return explicit;
    if (hasExplicitRecoveryActionTarget(action)) return null;
    const mapped = mappedRecoveryActionTarget(action);
    if (mapped) return mapped;
    return legacyRecoveryActionTarget(action);
  }

  function recoveryEvidenceTarget(row) {
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

  function recoveryArtifactTarget(artifact) {
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

  function recoveryText(parts) {
    return parts
      .filter((part) => part !== null && part !== undefined)
      .map((part) => String(part).toLowerCase())
      .join(" ");
  }

  // ---------------------------------------------------------------------
  // Recovery Center dispatch

  async function handleRecoveryAction(action) {
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

  function focusDetailKey(focusKey) {
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

  async function copyRecoveryCommand(button, commandText) {
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

  async function copyText(text) {
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

  function isPinned(planDir) {
    if (!state.detailRegistry || !Array.isArray(state.detailRegistry.pins)) {
      return false;
    }
    return state.detailRegistry.pins.some((pin) => pin.plan_dir === planDir);
  }

  function findPin(planDir) {
    if (!state.detailRegistry || !Array.isArray(state.detailRegistry.pins)) {
      return null;
    }
    return (
      state.detailRegistry.pins.find((pin) => pin.plan_dir === planDir) || null
    );
  }

  async function loadAndRenderState(planDir) {
    try {
      const body = await apiRequest(
        "/api/state?plan_dir=" + encodeURIComponent(planDir)
      );
      const statePayload = body && body.state ? body.state : null;
      renderActive(statePayload);
      renderVerification(statePayload);
      renderSessionsFromState(body || null);
    } catch (err) {
      // soft fail; the global status line already surfaced the issue.
    }
  }

  function renderActive(payload) {
    const dl = document.getElementById("wb-detail-active");
    if (!dl) return;
    const turn = payload && payload.active_turn ? payload.active_turn : null;
    setField(dl, "session", turn && turn.session_id);
    setField(dl, "turn", turn && turn.turn_index);
    setField(dl, "harness", turn && turn.harness);
    setField(dl, "model", turn && turn.model);
    setField(
      dl,
      "elapsed",
      turn && typeof turn.elapsed_seconds === "number"
        ? turn.elapsed_seconds.toFixed(1) + "s"
        : null
    );
    setField(
      dl,
      "idle",
      turn && typeof turn.idle_seconds === "number"
        ? turn.idle_seconds.toFixed(1) + "s"
        : null
    );
    setField(dl, "started", turn && turn.started_at);
    setField(dl, "run", turn && turn.run_id);
  }

  function setField(root, name, value) {
    const el = root.querySelector('[data-field="' + name + '"]');
    if (!el) return;
    el.textContent =
      value === null || value === undefined || value === "" ? "-" : String(value);
  }

  function renderVerification(payload) {
    const host = document.getElementById("wb-detail-verification");
    if (!host) return;
    const verification = payload && payload.active_verification
      ? payload.active_verification
      : null;
    host.innerHTML = "";
    if (!verification) {
      const empty = document.createElement("p");
      empty.className = "wb-empty";
      empty.textContent = "no active verification";
      host.appendChild(empty);
      return;
    }
    const pre = document.createElement("pre");
    pre.className = "wb-verification__body";
    pre.textContent = JSON.stringify(verification, null, 2);
    host.appendChild(pre);
  }

  function renderSessionsFromState(payload) {
    const list = document.getElementById("wb-detail-sessions");
    if (!list) return;
    const tmpl = document.getElementById("wb-session-row-template");
    list.innerHTML = "";
    const sessions =
      payload && Array.isArray(payload.sessions) ? payload.sessions : null;
    const runnerState = payload && payload.state ? payload.state : null;
    const activeTurn =
      runnerState && runnerState.active_turn ? runnerState.active_turn : null;
    const activeId =
      activeTurn && activeTurn.session_id ? String(activeTurn.session_id) : null;
    if (!sessions || !sessions.length) {
      const empty = document.createElement("li");
      empty.className = "wb-empty";
      empty.textContent =
        state.detailPlan && state.detailPlan.next_pending_session_id
          ? "next pending session: " + state.detailPlan.next_pending_session_id
          : "no session telemetry yet";
      list.appendChild(empty);
      return;
    }
    for (const session of sessions) {
      const fragment = tmpl.content.cloneNode(true);
      const li = fragment.querySelector("li");
      const id = session && session.id !== undefined && session.id !== null
        ? String(session.id)
        : "";
      li.setAttribute("data-session-id", id);
      setField(li, "id", "session-" + id);
      let status = session && session.status ? String(session.status) : "(unknown)";
      if (activeId && id === activeId) {
        status = "active";
      }
      setField(li, "status", status);
      const path = session && session.path ? String(session.path) : "";
      const filename = path
        ? path.substring(path.lastIndexOf("/") + 1)
        : "";
      setField(li, "path", filename);
      list.appendChild(li);
    }
  }

  async function loadAndRenderEvents(planDir) {
    try {
      const body = await apiRequest(
        "/api/events?plan_dir=" +
          encodeURIComponent(planDir) +
          "&limit=20"
      );
      renderEvents(body && Array.isArray(body.events) ? body.events : []);
    } catch (err) {
      // soft fail
    }
  }

  function renderEvents(events) {
    const list = document.getElementById("wb-detail-events");
    if (!list) return;
    list.innerHTML = "";
    if (events.length === 0) {
      const empty = document.createElement("li");
      empty.className = "wb-empty";
      empty.textContent = "no events yet";
      list.appendChild(empty);
      return;
    }
    const tmpl = document.getElementById("wb-event-row-template");
    events.slice(-20).forEach((event) => {
      const fragment = tmpl.content.cloneNode(true);
      const li = fragment.querySelector("li");
      setField(li, "created_at", event.created_at || "");
      setField(li, "name", event.name || "(unnamed)");
      const payloadEl = li.querySelector('[data-field="payload"]');
      if (payloadEl) {
        payloadEl.textContent = event.payload
          ? JSON.stringify(event.payload, null, 2)
          : "";
      }
      list.appendChild(li);
    });
  }

  async function loadAndRenderProgress(planDir) {
    try {
      const body = await apiRequest(
        "/api/progress?plan_dir=" + encodeURIComponent(planDir)
      );
      renderProgress(body && Array.isArray(body.entries) ? body.entries : []);
    } catch (err) {
      // soft fail
    }
  }

  function renderProgress(entries) {
    const list = document.getElementById("wb-detail-progress");
    if (!list) return;
    list.innerHTML = "";
    if (entries.length === 0) {
      const empty = document.createElement("li");
      empty.className = "wb-empty";
      empty.textContent = "no entries";
      list.appendChild(empty);
      return;
    }
    const tmpl = document.getElementById("wb-progress-row-template");
    entries.forEach((entry) => {
      const fragment = tmpl.content.cloneNode(true);
      const li = fragment.querySelector("li");
      setField(li, "session_id", "session " + (entry.session_id || "?"));
      setField(li, "status", entry.status || "");
      setField(li, "completed_at", entry.completed_at || "");
      const decisions = li.querySelector('[data-field="key_decisions"]');
      if (decisions) decisions.textContent = entry.key_decisions || "";
      populateUL(
        li.querySelector('[data-field="files_modified"]'),
        entry.files_modified,
        "files modified"
      );
      populateUL(
        li.querySelector('[data-field="discovered_facts"]'),
        entry.discovered_facts,
        "discovered facts"
      );
      populateUL(
        li.querySelector('[data-field="open_questions"]'),
        entry.open_questions,
        "open questions"
      );
      const blockers = li.querySelector('[data-field="blockers"]');
      if (blockers) {
        blockers.textContent = entry.blockers
          ? "blockers: " + entry.blockers
          : "";
      }
      list.appendChild(li);
    });
  }

  function populateUL(el, items, label) {
    if (!el) return;
    el.innerHTML = "";
    if (!Array.isArray(items) || items.length === 0) return;
    const header = document.createElement("li");
    header.className = "wb-progress-row__heading";
    header.textContent = label;
    el.appendChild(header);
    items.forEach((text) => {
      const li = document.createElement("li");
      li.textContent = String(text);
      el.appendChild(li);
    });
  }

  async function loadAndRenderLog(planDir, stream) {
    const el = document.getElementById("wb-detail-" + stream);
    if (!el) return;
    try {
      const body = await apiRequest(
        "/api/logs?plan_dir=" +
          encodeURIComponent(planDir) +
          "&stream=" +
          stream +
          "&tail_lines=120"
      );
      if (body && body.ok && typeof body.text === "string") {
        el.textContent = body.text || "(empty)";
      } else {
        el.textContent = "(no log)";
      }
    } catch (err) {
      el.textContent = "(no log)";
    }
  }

  async function loadAndRenderArtifacts(planDir) {
    try {
      const body = await apiRequest(
        "/api/artifacts?plan_dir=" + encodeURIComponent(planDir)
      );
      renderArtifactsPanel(body && body.artifacts ? body.artifacts : {});
    } catch (err) {
      renderArtifactsPanel({});
    }
  }

  function renderArtifactsPanel(artifacts) {
    const list = document.getElementById("wb-detail-artifacts");
    if (!list) return;
    list.innerHTML = "";
    const tmpl = document.getElementById("wb-artifact-row-template");
    const labels = [
      ["state_path", "state.json"],
      ["events_path", "events.jsonl"],
      ["progress_path", "progress.md"],
      ["final_review_path", "final-review.md"],
      ["artifact_dir", "artifact dir"],
      ["stdout_path", "stdout"],
      ["stderr_path", "stderr"],
      ["last_message_path", "last-message.txt"],
      ["result_path", "result.json"],
    ];
    let added = 0;
    labels.forEach((entry) => {
      const key = entry[0];
      const label = entry[1];
      const descriptor = artifacts[key];
      if (!descriptor || !descriptor.path) return;
      const fragment = tmpl.content.cloneNode(true);
      const li = fragment.querySelector("li");
      setField(li, "label", label);
      setField(li, "path", descriptor.path);
      const meta = descriptor.exists
        ? (descriptor.kind || "file") +
          (typeof descriptor.size === "number"
            ? " · " + descriptor.size + " B"
            : "")
        : "missing";
      setField(li, "meta", meta);
      list.appendChild(li);
      added += 1;
    });
    if (added === 0) {
      const empty = document.createElement("li");
      empty.className = "wb-empty";
      empty.textContent = "no artifacts yet";
      list.appendChild(empty);
    }
  }

  async function loadAndRenderReply(planDir) {
    const panel = document.getElementById("wb-reply-panel");
    if (panel) panel.hidden = false;
    clearFormErrors("wb-reply-form-errors");
    try {
      const body = await apiRequest(
        "/api/questions?plan_dir=" + encodeURIComponent(planDir)
      );
      if (body && body.ok) {
        state.detailReplyContext = body;
        setReplyFormDisabled(false);
        const promptEl = document.getElementById("wb-reply-prompt");
        if (promptEl) promptEl.textContent = body.rendered_prompt || "";
        const lastEl = document.getElementById("wb-reply-last-message");
        if (lastEl) lastEl.textContent = body.last_message || "";
      } else {
        renderReplyContextError(body);
      }
    } catch (err) {
      renderReplyContextError({
        reason: "request-failed",
        error: describeError(err),
      });
    }
  }

  function renderReplyContextError(payload) {
    state.detailReplyContext = null;
    setReplyFormDisabled(true);
    const reason = payload && payload.reason ? String(payload.reason) : null;
    const error =
      payload && payload.error
        ? String(payload.error)
        : "no paused-Q&A context available";
    const hint = reason ? REPLY_CONTEXT_ERROR_HINTS[reason] : null;
    const message = reason
      ? reason + ": " + (hint ? hint + " " + error : error)
      : error;
    const promptEl = document.getElementById("wb-reply-prompt");
    if (promptEl) promptEl.textContent = message;
    const lastEl = document.getElementById("wb-reply-last-message");
    if (lastEl) lastEl.textContent = "";
    showFormError("wb-reply-form-errors", message);
  }

  function hideReplyPanel() {
    const panel = document.getElementById("wb-reply-panel");
    if (panel) panel.hidden = true;
    state.detailReplyContext = null;
    setReplyFormDisabled(false);
  }

  function setReplyFormDisabled(disabled) {
    const form = document.getElementById("wb-reply-form");
    if (!form) return;
    const controls = form.querySelectorAll("textarea, button[type=\"submit\"]");
    controls.forEach((control) => {
      control.disabled = Boolean(disabled);
    });
  }

  // ---------------------------------------------------------------------
  // Profile form

  function renderProfileForm(plan, registry) {
    const form = document.getElementById("wb-profile-form");
    if (!form || !plan) return;
    const pin = findPin(plan.plan_dir);
    const profile = (pin && pin.profile) || plan.profile || {};
    setInputValue(form, "label", pin ? pin.label || "" : plan.label || "");
    setInputValue(form, "cwd", profile.cwd || "");
    setInputValue(form, "harness", profile.harness || "");
    setInputValue(form, "model", profile.model || "");
    setInputValue(form, "effort", profile.effort || "");
    setInputValue(
      form,
      "timeout",
      profile.timeout === null || profile.timeout === undefined
        ? ""
        : String(profile.timeout)
    );
    setInputValue(form, "commit_mode", profile.commit_mode || "");
    setInputValue(form, "review_harness", profile.review_harness || "");
    setTriStateValue(form, "unsafe_bypass", profile.unsafe_bypass);
    setTriStateValue(
      form,
      "allow_not_loop_ready",
      profile.allow_not_loop_ready
    );
    setInputValue(form, "note", profile.note || "");
    clearFormErrors("wb-profile-form-errors");
  }

  function setInputValue(form, name, value) {
    const el = form.elements.namedItem(name);
    if (!el) return;
    el.value = value;
  }

  function setTriStateValue(form, name, value) {
    const el = form.elements.namedItem(name);
    if (!el) return;
    if (value === true) {
      el.value = "true";
    } else if (value === false) {
      el.value = "false";
    } else {
      el.value = "";
    }
  }

  function readTriStateValue(form, name) {
    const el = form.elements.namedItem(name);
    if (!el) return null;
    const raw = el.value;
    if (raw === "true") return true;
    if (raw === "false") return false;
    return null;
  }

  function clearFormErrors(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.hidden = true;
    el.textContent = "";
  }

  function showFormError(id, message) {
    const el = document.getElementById(id);
    if (!el) return;
    el.hidden = false;
    el.textContent = message;
  }

  function readProfileFormPayload() {
    const form = document.getElementById("wb-profile-form");
    if (!form) return { label: null, profile: {} };
    const label = (form.elements.namedItem("label").value || "").trim();
    const profile = {
      cwd: (form.elements.namedItem("cwd").value || "").trim() || null,
      harness:
        (form.elements.namedItem("harness").value || "").trim() || null,
      model: (form.elements.namedItem("model").value || "").trim() || null,
      effort: (form.elements.namedItem("effort").value || "").trim() || null,
      commit_mode:
        (form.elements.namedItem("commit_mode").value || "").trim() || null,
      review_harness:
        (form.elements.namedItem("review_harness").value || "").trim() || null,
      unsafe_bypass: readTriStateValue(form, "unsafe_bypass"),
      allow_not_loop_ready: readTriStateValue(form, "allow_not_loop_ready"),
      note: (form.elements.namedItem("note").value || "").trim() || null,
    };
    const rawTimeout = (form.elements.namedItem("timeout").value || "").trim();
    if (rawTimeout) {
      const parsed = parseInt(rawTimeout, 10);
      profile.timeout = Number.isFinite(parsed) ? parsed : null;
    } else {
      profile.timeout = null;
    }
    return { label: label || null, profile: profile };
  }

  function validateLaunchPayload(payload) {
    const errors = [];
    if (!payload.profile.cwd) errors.push("cwd is required to launch");
    if (!payload.profile.harness) {
      errors.push("harness is required to launch");
    } else if (HARNESS_NAMES.indexOf(payload.profile.harness) === -1) {
      errors.push("harness must be one of " + HARNESS_NAMES.join(", "));
    }
    if (
      payload.profile.review_harness &&
      HARNESS_NAMES.indexOf(payload.profile.review_harness) === -1
    ) {
      errors.push("review harness must be one of " + HARNESS_NAMES.join(", "));
    }
    if (
      payload.profile.commit_mode &&
      COMMIT_MODES.indexOf(payload.profile.commit_mode) === -1
    ) {
      errors.push("commit_mode must be one of " + COMMIT_MODES.join(", "));
    }
    if (
      payload.profile.timeout !== null &&
      payload.profile.timeout !== undefined &&
      payload.profile.timeout <= 0
    ) {
      errors.push("timeout must be positive");
    }
    return errors;
  }

  function wireProfileForm() {
    const form = document.getElementById("wb-profile-form");
    if (!form) return;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearFormErrors("wb-profile-form-errors");
      const planDir = state.route.planDir;
      if (!planDir) return;
      const payload = readProfileFormPayload();
      try {
        const body = await apiRequest("/api/registry/pin", {
          method: "POST",
          body: {
            plan_dir: planDir,
            label: payload.label,
            profile: payload.profile,
          },
        });
        if (body && body.ok) {
          if (body.registry) state.detailRegistry = body.registry;
          showControlsFeedback("info", "profile saved");
          renderDetailHeader(state.detailPlan);
        } else {
          showFormError(
            "wb-profile-form-errors",
            (body && body.error) || "failed to save profile"
          );
        }
      } catch (err) {
        showFormError(
          "wb-profile-form-errors",
          "save failed: " + describeError(err)
        );
      }
    });

    const unpinButton = document.getElementById("wb-profile-unpin");
    if (unpinButton) {
      unpinButton.addEventListener("click", async () => {
        const planDir = state.route.planDir;
        if (!planDir) return;
        try {
          const body = await apiRequest("/api/registry/unpin", {
            method: "POST",
            body: { plan_dir: planDir },
          });
          if (body && body.ok) {
            if (body.registry) state.detailRegistry = body.registry;
            showControlsFeedback("info", "pin removed");
            renderDetailHeader(state.detailPlan);
          } else {
            showControlsFeedback(
              "error",
              (body && body.error) || "failed to unpin"
            );
          }
        } catch (err) {
          showControlsFeedback("error", describeError(err));
        }
      });
    }
  }

  // ---------------------------------------------------------------------
  // Controls

  function showControlsFeedback(kind, message) {
    const el = document.getElementById("wb-controls-feedback");
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

  function wireControls() {
    const launch = document.getElementById("wb-control-launch");
    if (launch) {
      launch.addEventListener("click", handleLaunchClick);
    }
    const stop = document.getElementById("wb-control-stop");
    if (stop) {
      stop.addEventListener("click", handleStopClick);
    }
    const harnessApply = document.getElementById("wb-control-harness-apply");
    if (harnessApply) {
      harnessApply.addEventListener("click", handleHarnessSelectClick);
    }
    const pin = document.getElementById("wb-control-pin");
    if (pin) {
      pin.addEventListener("click", () => {
        const form = document.getElementById("wb-profile-form");
        if (form) {
          form.scrollIntoView({ behavior: "smooth", block: "start" });
          const labelInput = form.elements.namedItem("label");
          if (labelInput && typeof labelInput.focus === "function") {
            labelInput.focus();
          }
        }
      });
    }
    const unlock = document.getElementById("wb-control-unlock");
    if (unlock) {
      unlock.addEventListener("click", handleUnlockClick);
    }
    const archivePlan = document.getElementById("wb-control-archive");
    if (archivePlan) {
      archivePlan.addEventListener("click", handleArchiveClick);
    }
  }

  async function handleLaunchClick() {
    const planDir = state.route.planDir;
    if (!planDir) return;
    clearFormErrors("wb-profile-form-errors");
    showControlsFeedback("", "");
    const payload = readProfileFormPayload();
    const errors = validateLaunchPayload(payload);
    if (errors.length > 0) {
      showFormError("wb-profile-form-errors", errors.join("; "));
      return;
    }
    if (payload.profile.unsafe_bypass) {
      const confirmed = window.confirm(
        "Launch with --unsafe-bypass? This skips the loop-ready safety gate."
      );
      if (!confirmed) return;
    }
    try {
      const body = await apiRequest("/api/launch", {
        method: "POST",
        body: { plan_dir: planDir, profile: payload.profile },
      });
      if (body && body.ok) {
        const pid = body.launch && body.launch.pid;
        showControlsFeedback(
          "info",
          "launched" + (pid ? " (pid " + pid + ")" : "")
        );
      } else {
        showControlsFeedback(
          "error",
          (body && body.error) || "launch failed"
        );
      }
    } catch (err) {
      showControlsFeedback("error", describeError(err));
    }
  }

  async function handleStopClick() {
    const planDir = state.route.planDir;
    if (!planDir) return;
    showControlsFeedback("", "");
    try {
      const body = await apiRequest(
        "/api/control/stop-after-current",
        { method: "POST", body: { plan_dir: planDir } }
      );
      if (body && body.ok) {
        showControlsFeedback("info", "stop queued; applies at next checkpoint");
      } else {
        showControlsFeedback(
          "error",
          (body && body.error) || "failed to enqueue stop"
        );
      }
    } catch (err) {
      showControlsFeedback("error", describeError(err));
    }
  }

  async function handleHarnessSelectClick() {
    const planDir = state.route.planDir;
    if (!planDir) return;
    const select = document.getElementById("wb-control-harness");
    if (!select) return;
    const harness = select.value;
    if (!harness) {
      showControlsFeedback("error", "choose a harness first");
      return;
    }
    if (HARNESS_NAMES.indexOf(harness) === -1) {
      showControlsFeedback("error", "unknown harness");
      return;
    }
    showControlsFeedback("", "");
    try {
      const body = await apiRequest(
        "/api/control/select-harness",
        {
          method: "POST",
          body: { plan_dir: planDir, harness: harness },
        }
      );
      if (body && body.ok) {
        showControlsFeedback(
          "info",
          "harness " + harness + " queued for next session"
        );
      } else {
        showControlsFeedback(
          "error",
          (body && body.error) || "failed to enqueue harness select"
        );
      }
    } catch (err) {
      showControlsFeedback("error", describeError(err));
    }
  }

  async function handleUnlockClick() {
    const planDir = state.route.planDir;
    if (!planDir) return;
    const confirmed = window.confirm(
      "Force-unlock this plan? Only do this when you are sure no runner is active; mid-run unlock can corrupt state."
    );
    if (!confirmed) return;
    showControlsFeedback("", "");
    try {
      const body = await apiRequest("/api/unlock", {
        method: "POST",
        body: { plan_dir: planDir, force: true },
      });
      if (body && body.ok) {
        const removed = body.removed ? "removed" : "no lock present";
        showControlsFeedback("info", "unlock: " + removed);
      } else {
        showControlsFeedback(
          "error",
          (body && body.error) || "unlock failed"
        );
      }
    } catch (err) {
      showControlsFeedback("error", describeError(err));
    }
  }

  async function handleArchiveClick() {
    const plan =
      state.detailPlan && state.detailPlan.plan_dir ? state.detailPlan : null;
    const planDir = (plan && plan.plan_dir) || state.route.planDir;
    if (!planDir) return;
    const typed = window.prompt(
      "Archive this plan? This moves the plan directory, Workbench pin, and ASL state into the archive, where it can be restored until retention expires. Type the full plan path to confirm:\n\n" +
        planDir,
      ""
    );
    if (typed === null) return;
    const confirmPlanDir = (typed || "").trim();
    if (!confirmPlanDir) {
      showControlsFeedback("error", "archive confirmation required");
      return;
    }
    if (confirmPlanDir !== planDir) {
      showControlsFeedback("error", "confirmation must match the full plan path");
      return;
    }
    showControlsFeedback("", "");
    try {
      const body = await apiRequest("/api/plan/archive", {
        method: "POST",
        body: {
          plan_dir: planDir,
          confirm_plan_dir: confirmPlanDir,
        },
      });
      if (body && body.ok) {
        state.detailPlan = null;
        state.detailRegistry = null;
        state.detailReplyContext = null;
        clearSelectedPlan();
        navigateToQueue();
        refresh();
      } else {
        showControlsFeedback(
          "error",
          (body && body.error) || "archive failed"
        );
      }
    } catch (err) {
      showControlsFeedback("error", describeError(err));
    }
  }

  // ---------------------------------------------------------------------
  // Reply form

  function wireReplyForm() {
    const form = document.getElementById("wb-reply-form");
    if (!form) return;
    form.addEventListener("submit", handleReplySubmit);
    const clearBtn = document.getElementById("wb-reply-clear");
    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        clearReplyTextarea();
        clearFormErrors("wb-reply-form-errors");
      });
    }
  }

  function clearReplyTextarea() {
    const textarea = document.getElementById("wb-reply-text");
    if (textarea) textarea.value = "";
  }

  async function handleReplySubmit(event) {
    event.preventDefault();
    clearFormErrors("wb-reply-form-errors");
    const planDir = state.route.planDir;
    if (!planDir) return;
    const textarea = document.getElementById("wb-reply-text");
    if (!textarea) return;
    const replyText = textarea.value;
    if (!replyText || !replyText.trim()) {
      showFormError("wb-reply-form-errors", "reply must not be empty");
      return;
    }
    const confirmed = window.confirm(
      "Submit this reply to resume the paused session? The text below will be streamed verbatim to the paused agent and is not retrievable from the UI afterward."
    );
    if (!confirmed) return;
    try {
      const body = await apiRequest("/api/reply", {
        method: "POST",
        body: { plan_dir: planDir, reply_text: replyText },
      });
      if (body && body.ok) {
        clearReplyTextarea();
        showControlsFeedback("info", "reply submitted; runner resumed");
        hideReplyPanel();
      } else {
        showFormError(
          "wb-reply-form-errors",
          (body && body.error) || "reply submission failed"
        );
      }
    } catch (err) {
      showFormError(
        "wb-reply-form-errors",
        "submission failed: " + describeError(err)
      );
    }
  }
})();
