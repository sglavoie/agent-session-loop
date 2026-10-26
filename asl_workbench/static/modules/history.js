/* What has already finished.
 *
 * A separate read from the queue, because it is a separate question with a
 * separate retention: the queue holds work that still needs watching, and ASL
 * stops projecting a run the moment it ends. Without this pane a run that
 * finished while the operator was away simply vanishes, which is exactly the
 * case a notification link most often names.
 *
 * It is narrowed by the same words the queue is. A project filter that applied
 * to one pane and not the other would put two different projects on one
 * screen under one heading.
 */

import { apiRequest, describeError } from "./api.js";
import { HISTORY_PAGE_LIMIT, HISTORY_OUTCOMES } from "./constants.js";
import { state } from "./state.js";

export function historyQuery() {
  const params = new URLSearchParams();
  if (state.projectFilter) params.set("project", state.projectFilter);
  if (state.searchText) params.set("q", state.searchText);
  if (state.harnessFilter) params.set("harness", state.harnessFilter);
  if (state.historyOutcome) params.set("outcome", state.historyOutcome);
  params.set("limit", String(HISTORY_PAGE_LIMIT));
  return "/api/history?" + params.toString();
}

export async function refreshHistory() {
  if (state.historyInFlight) {
    // A filter changed while a slow read was on the wire. That read answers
    // the old filter, so dropping this request would leave the pane showing
    // the wrong project; it runs once the current read settles instead.
    state.historyPending = true;
    return;
  }
  state.historyInFlight = true;
  state.historyPending = false;
  try {
    const body = await apiRequest(historyQuery());
    // Superseded while on the wire: the follow-up read draws instead.
    if (state.historyPending) return;
    if (!body || body.ok !== true) {
      renderHistoryError(body && body.reason ? body.reason : "unknown");
      return;
    }
    state.historyEntries = Array.isArray(body.entries) ? body.entries : [];
    state.historyMeta = {
      observedAt: typeof body.observed_at === "number" ? body.observed_at : null,
      complete: body.complete !== false,
      warnings: Array.isArray(body.warnings) ? body.warnings : [],
    };
    renderHistory();
  } catch (err) {
    if (state.historyPending) return;
    renderHistoryError(describeError(err));
  } finally {
    state.historyInFlight = false;
    if (state.historyPending) {
      state.historyPending = false;
      refreshHistory();
    }
  }
}

export function renderHistoryError(reason) {
  const status = document.getElementById("wb-history-status");
  if (!status) return;
  status.hidden = false;
  status.setAttribute("data-state", "error");
  status.textContent = "history unavailable: " + reason;
}

export function renderHistory() {
  const list = document.getElementById("wb-history-list");
  const section = document.getElementById("wb-history");
  if (!list || !section) return;
  const entries = state.historyEntries || [];
  const count = document.getElementById("wb-history-count");
  if (count) count.textContent = String(entries.length);
  const empty = document.getElementById("wb-history-empty");
  if (empty) empty.hidden = entries.length !== 0;

  const status = document.getElementById("wb-history-status");
  if (status) {
    const meta = state.historyMeta || {};
    const notes = [];
    if (meta.complete === false) {
      // Said plainly rather than implied by a short list: a window that could
      // not be read whole is a window an absent run may simply be missing from.
      notes.push("this window could not be read in full");
    }
    (meta.warnings || []).forEach((warning) => notes.push(warning));
    status.hidden = notes.length === 0;
    status.removeAttribute("data-state");
    status.textContent = notes.join(" | ");
  }

  list.textContent = "";
  entries.forEach((entry) => list.appendChild(buildHistoryCard(entry)));
  focusLinkedEntry();
}

export function buildHistoryCard(entry) {
  const template = document.getElementById("wb-history-row-template");
  const card = template.content.cloneNode(true).querySelector(".wb-history-row");
  const entryId = entry.entry_id || "";
  card.setAttribute("data-entry-id", entryId);
  card.setAttribute("data-outcome", outcomeOf(entry));
  if (entry.reconstructed === true) {
    // The ending was inferred from an absence rather than read from an event.
    // An operator deciding whether to rerun needs to know which one they have.
    card.setAttribute("data-reconstructed", "true");
  }

  setField(card, "outcome", outcomeOf(entry));
  setField(card, "label", historyLabel(entry));
  setField(card, "project", entry.project || "");
  setField(card, "harness", entry.harness || "-");
  setField(card, "finished", formatMoment(entry.run_finished_at));
  setField(card, "duration", formatDuration(entry.duration_seconds));
  setField(card, "sessions", formatSessions(entry));
  setField(card, "run", entry.run_id || "-");
  setField(
    card,
    "note",
    entry.reconstructed === true
      ? "reconstructed: no ending was recorded, so this was inferred"
      : entry.pause_reason || ""
  );
  const note = card.querySelector('[data-field="note"]');
  if (note) note.hidden = !note.textContent;

  const link = card.querySelector('[data-field="link"]');
  if (link && entryId) link.setAttribute("href", "#entry=" + encodeURIComponent(entryId));
  return card;
}

function outcomeOf(entry) {
  const value = entry && entry.outcome;
  return HISTORY_OUTCOMES.indexOf(value) !== -1 ? value : "unknown";
}

/** What a finished run is called, under whichever name its mode published. */
export function historyLabel(entry) {
  if (!entry) return "(unknown run)";
  return (
    entry.plan_title || entry.plan_label || entry.project || entry.run_id ||
    "(unknown run)"
  );
}

function setField(card, name, text) {
  const el = card.querySelector('[data-field="' + name + '"]');
  if (el) el.textContent = text === null || text === undefined ? "" : String(text);
}

export function formatMoment(epochSeconds) {
  if (typeof epochSeconds !== "number" || !Number.isFinite(epochSeconds)) {
    return "-";
  }
  return new Date(epochSeconds * 1000).toLocaleString();
}

export function formatDuration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) {
    return "-";
  }
  const total = Math.round(seconds);
  if (total < 60) return total + "s";
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return minutes + "m " + (total % 60) + "s";
  const hours = Math.floor(minutes / 60);
  return hours + "h " + (minutes % 60) + "m";
}

export function formatSessions(entry) {
  const done = entry && entry.completed_session_count;
  const pending = entry && entry.pending_session_count;
  if (typeof done !== "number") return "-";
  if (typeof pending !== "number") return String(done);
  return done + " done, " + pending + " pending";
}

/**
 * Scroll to the entry an `#entry=` link named, if it is on this page.
 *
 * A link that names an entry the retention window has dropped is left alone
 * rather than answered with an error: the run happened, the record aged out,
 * and the honest thing is the history pane as it stands.
 */
export function focusLinkedEntry() {
  const entryId = state.route && state.route.entryId;
  if (!entryId) return;
  const card = document.querySelector(
    '.wb-history-row[data-entry-id="' + entryId.replace(/"/g, "") + '"]'
  );
  const missing = document.getElementById("wb-history-missing");
  if (!card) {
    if (missing) {
      missing.hidden = false;
      missing.textContent =
        "That finished run is not in this window. It may have aged out of " +
        "retention, or the current filters may exclude it.";
    }
    return;
  }
  if (missing) missing.hidden = true;
  card.setAttribute("data-linked", "true");
  if (typeof card.scrollIntoView === "function") {
    card.scrollIntoView({ block: "nearest" });
  }
}

export function wireHistoryControls() {
  const select = document.getElementById("wb-history-outcome");
  if (select) {
    select.addEventListener("change", () => {
      const value = select.value || null;
      if (state.historyOutcome === value) return;
      state.historyOutcome = value;
      refreshHistory();
    });
  }
  const toggle = document.getElementById("wb-history-toggle");
  const section = document.getElementById("wb-history");
  if (toggle && section) {
    toggle.addEventListener("click", () => {
      const open = section.getAttribute("data-open") !== "true";
      section.setAttribute("data-open", open ? "true" : "false");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.textContent = open ? "hide history" : "show history";
    });
  }
}
