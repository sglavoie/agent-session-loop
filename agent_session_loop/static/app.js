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
  const TOKEN_STORAGE_KEY = "wb.token";
  const FILTER_STORAGE_KEY = "wb.filter";
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

  const state = {
    token: null,
    activeFilter: "all",
    rowsByPlan: new Map(),
    refreshTimer: null,
    fetchInFlight: false,
    lastQueueMeta: null,
    route: { name: "queue", planDir: null },
    detailTimer: null,
    detailInFlight: false,
    detailPlan: null,
    detailRegistry: null,
    detailReplyContext: null,
    detailHistoryByPlan: new Map(),
  };

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    state.token = resolveToken();
    state.activeFilter = restoreFilter();
    wireFilterChips();
    wireRefreshButton();
    wireDetailBack();
    wireControls();
    wireProfileForm();
    wireReplyForm();
    applyChipPressedState();
    applyRoute();
    window.addEventListener("hashchange", applyRoute);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      if (state.route.name === "detail") {
        refreshDetail();
      } else {
        refresh();
      }
    });
  }

  // ---------------------------------------------------------------------
  // Routing

  function parseHashRoute() {
    const hash = window.location.hash || "";
    if (!hash.startsWith("#plan")) {
      return { name: "queue", planDir: null };
    }
    const querySep = hash.indexOf("?");
    if (querySep === -1) return { name: "queue", planDir: null };
    const search = hash.substring(querySep + 1);
    const params = new URLSearchParams(search);
    const dir = params.get("dir");
    if (!dir) return { name: "queue", planDir: null };
    return { name: "detail", planDir: dir };
  }

  function applyRoute() {
    const next = parseHashRoute();
    state.route = next;
    const queueEl = document.getElementById("wb-queue");
    const filtersEl = document.getElementById("wb-filters");
    const warningsEl = document.getElementById("wb-warnings");
    const queueMetaEl = document.getElementById("wb-queue-meta");
    const detailEl = document.getElementById("wb-detail");
    if (next.name === "detail") {
      if (queueEl) queueEl.hidden = true;
      if (filtersEl) filtersEl.hidden = true;
      if (warningsEl) warningsEl.hidden = true;
      if (queueMetaEl) queueMetaEl.hidden = true;
      if (detailEl) detailEl.hidden = false;
      stopQueueTimer();
      refreshDetail();
      scheduleDetailRefresh();
    } else {
      if (detailEl) detailEl.hidden = true;
      if (queueEl) queueEl.hidden = false;
      if (filtersEl) filtersEl.hidden = false;
      if (queueMetaEl && state.lastQueueMeta) queueMetaEl.hidden = false;
      stopDetailTimer();
      refresh();
      scheduleRefresh();
    }
  }

  function navigateToDetail(planDir) {
    if (!planDir) return;
    const encoded = encodeURIComponent(planDir);
    window.location.hash = "#plan?dir=" + encoded;
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

  // ---------------------------------------------------------------------
  // Queue refresh loop

  function scheduleRefresh() {
    stopQueueTimer();
    state.refreshTimer = window.setTimeout(() => {
      refresh().finally(() => {
        if (state.route.name === "queue") scheduleRefresh();
      });
    }, REFRESH_INTERVAL_MS);
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
    const plansList = Array.isArray(payload.plans) ? payload.plans : [];
    const sorted = plansList.slice().sort(comparePlanRows);
    const counts = computeCounts(sorted);
    renderCounts(counts);
    renderGlobalWarnings(payload);
    renderQueueMeta(payload, sorted.length, updatedAt);

    const seen = new Set();
    const visible = sorted.filter((plan) => isVisible(plan, state.activeFilter));

    const emptyEl = document.getElementById("wb-empty");
    if (visible.length === 0) {
      if (emptyEl) {
        emptyEl.hidden = false;
        emptyEl.textContent =
          plansList.length === 0
            ? "no plans discovered"
            : "no plans match filter: " + state.activeFilter;
      }
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
    const orderA = stateOrder(a.primary_state);
    const orderB = stateOrder(b.primary_state);
    if (orderA !== orderB) return orderA - orderB;
    const labelA = (a.label || a.plan_dir || "").toLowerCase();
    const labelB = (b.label || b.plan_dir || "").toLowerCase();
    return labelA.localeCompare(labelB);
  }

  function stateOrder(primaryState) {
    const ranking = {
      "paused-for-reply": 0,
      failed: 1,
      invalid: 2,
      running: 3,
      verifying: 4,
      stale: 5,
      ready: 6,
      recent: 7,
      complete: 8,
    };
    if (Object.prototype.hasOwnProperty.call(ranking, primaryState)) {
      return ranking[primaryState];
    }
    return 99;
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
    if (activeFilter === "all") return true;
    const group = FILTER_GROUPS[activeFilter] || [];
    return group.indexOf(plan.primary_state) !== -1;
  }

  function upsertRow(plan) {
    let row = state.rowsByPlan.get(plan.plan_dir);
    if (!row) {
      row = buildRow(plan.plan_dir);
      state.rowsByPlan.set(plan.plan_dir, row);
    }
    populateRow(row, plan);
    return row;
  }

  function buildRow(planDir) {
    const tmpl = document.getElementById("wb-row-template");
    const fragment = tmpl.content.cloneNode(true);
    const row = fragment.querySelector(".wb-row");
    row.setAttribute("data-plan-dir", planDir);
    return row;
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
    action.onclick = () => navigateToDetail(plan.plan_dir);

    populateArtifacts(row, plan);
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
        loadAndRenderState(planDir),
        loadAndRenderEvents(planDir),
        loadAndRenderProgress(planDir),
        loadAndRenderLog(planDir, "stdout"),
        loadAndRenderLog(planDir, "stderr"),
        loadAndRenderArtifacts(planDir),
      ];
      if (state.detailPlan && state.detailPlan.primary_state === "paused-for-reply") {
        tasks.push(loadAndRenderReply(planDir));
      } else {
        hideReplyPanel();
      }
      await Promise.all(tasks);
      const ts = new Date().toLocaleTimeString();
      setRefreshStatus("idle", "updated " + ts);
    } catch (err) {
      setRefreshStatus("error", "error: " + describeError(err));
    } finally {
      state.detailInFlight = false;
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
    try {
      const body = await apiRequest(
        "/api/questions?plan_dir=" + encodeURIComponent(planDir)
      );
      if (body && body.ok) {
        state.detailReplyContext = body;
        const promptEl = document.getElementById("wb-reply-prompt");
        if (promptEl) promptEl.textContent = body.rendered_prompt || "";
        const lastEl = document.getElementById("wb-reply-last-message");
        if (lastEl) lastEl.textContent = body.last_message || "";
      } else {
        const promptEl = document.getElementById("wb-reply-prompt");
        if (promptEl) {
          promptEl.textContent =
            (body && body.error) || "no paused-Q&A context available";
        }
      }
    } catch (err) {
      const promptEl = document.getElementById("wb-reply-prompt");
      if (promptEl) promptEl.textContent = describeError(err);
    }
  }

  function hideReplyPanel() {
    const panel = document.getElementById("wb-reply-panel");
    if (panel) panel.hidden = true;
    state.detailReplyContext = null;
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
