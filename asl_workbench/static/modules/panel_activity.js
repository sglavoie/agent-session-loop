/* Event and progress reads, summaries and rendering. */

import { apiRequest } from "./api.js";
import { planReadGuard, planUrl, routePlanId } from "./identity.js";
import { setField } from "./panels.js";

export async function loadAndRenderEvents() {
  const stale = planReadGuard();
  try {
    const body = await apiRequest(
      planUrl("/api/events", { limit: 20 })
    );
    if (stale()) return;
    renderEvents(body && Array.isArray(body.events) ? body.events : []);
  } catch (err) {
    // soft fail
  }
}

export function renderEvents(events) {
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
    if (li && event.name === "agent_mux_delivery") {
      const payload = event.payload || {};
      const status = String(payload.final_status || "unknown");
      li.classList.add("wb-event-row--agent-mux");
      li.dataset.deliveryStatus = status;
      if (status === "accepted" || status === "duplicate") {
        li.classList.add("wb-event-row--delivery-success");
      } else if (status === "legacy_unacknowledged") {
        li.classList.add("wb-event-row--delivery-degraded");
      } else {
        li.classList.add("wb-event-row--delivery-failure");
      }
    }
    const summaryEl = li.querySelector('[data-field="summary"]');
    if (summaryEl) summaryEl.textContent = summarizeEventPayload(event.payload);
    const created = li.querySelector('[data-field="created_at"]');
    if (created && event.created_at) {
      created.textContent = formatEventTime(event.created_at);
      created.title = event.created_at;
    }
    const payloadEl = li.querySelector('[data-field="payload"]');
    const details = li.querySelector(".wb-event-row__details");
    if (payloadEl && details && event.payload) {
      payloadEl.textContent = JSON.stringify(event.payload, null, 2);
      details.hidden = false;
      // The list is rebuilt on every poll; an operator reading a payload
      // should not have it snap shut under them.
      const key = eventKey(event);
      details.open = openEventPayloads.has(key);
      details.addEventListener("toggle", () => {
        if (details.open) {
          openEventPayloads.add(key);
        } else {
          openEventPayloads.delete(key);
        }
      });
    }
    list.appendChild(li);
  });
}

/** Payloads the operator has opened, by event, across the poll's redraws. */
const openEventPayloads = new Set();

function eventKey(event) {
  return String(event.created_at || "") + "|" + String(event.name || "");
}

/** Fields worth a glance, in the order they are read. */
export const EVENT_SUMMARY_FIELDS = [
  "session_id",
  "phase",
  "action",
  "disposition",
  "final_status",
  "agent_mux_event",
  "harness",
  "approval_mode",
  "commit_mode",
  "elapsed_seconds",
  "exit_code",
  "ok",
  "reason",
  "detail",
];

/** How many fields the one-line summary carries before it stops. */
export const EVENT_SUMMARY_LIMIT = 4;

/**
 * One line that says what an event was about, so the list can be skimmed.
 *
 * Only top-level scalars are used; nested objects, paths and digests are left
 * to the full payload behind the toggle.
 */
export function summarizeEventPayload(payload) {
  if (!payload || typeof payload !== "object") return "";
  const parts = [];
  const session = payload.session_id ?? payload.session;
  for (const key of EVENT_SUMMARY_FIELDS) {
    if (parts.length >= EVENT_SUMMARY_LIMIT) break;
    const value = key === "session_id" ? session : payload[key];
    if (value === null || value === undefined || value === "") continue;
    if (typeof value === "object") continue;
    if (key === "session_id") {
      parts.push("session " + value);
    } else if (key === "elapsed_seconds" && typeof value === "number") {
      parts.push(Math.round(value) + "s elapsed");
    } else if (typeof value === "boolean" || typeof value === "number") {
      parts.push(key.replace(/_/g, " ") + " " + value);
    } else {
      const text = String(value);
      // A delivery's status and reason are often the same word.
      if (parts.indexOf(text) !== -1) continue;
      parts.push(text.length > 80 ? text.slice(0, 79) + "…" : text);
    }
  }
  return parts.join(" · ");
}

/** An event's time in local terms, to the second; the ISO stamp is its tooltip. */
export function formatEventTime(value) {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return String(value);
  try {
    return new Date(parsed).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch (err) {
    return String(value);
  }
}

export async function loadAndRenderProgress() {
  const stale = planReadGuard();
  try {
    const body = await apiRequest(
      planUrl("/api/progress")
    );
    if (stale()) return;
    renderProgress(body && Array.isArray(body.entries) ? body.entries : []);
  } catch (err) {
    // soft fail
  }
}

/** How many of the newest journal entries open expanded on a plan's first render. */
export const PROGRESS_OPEN_ENTRIES = 3;

/**
 * Draw the journal newest first, with only the latest few entries expanded.
 *
 * A long plan's journal runs to tens of thousands of pixels when every entry
 * is open, which buries the logs and artifacts below it. What an operator
 * opened or closed survives the periodic redraw: it is read back from the
 * entries already on screen, keyed by session, before they are replaced.
 */
export function renderProgress(entries) {
  const list = document.getElementById("wb-detail-progress");
  if (!list) return;
  const planKey = routePlanId();
  const previous = new Map();
  if (list.getAttribute("data-plan") === planKey) {
    list.querySelectorAll("li[data-session-id] > details").forEach((details) => {
      previous.set(details.parentElement.getAttribute("data-session-id"), details.open);
    });
  }
  list.setAttribute("data-plan", planKey);
  list.innerHTML = "";
  if (entries.length === 0) {
    const empty = document.createElement("li");
    empty.className = "wb-empty";
    empty.textContent = "no entries";
    list.appendChild(empty);
    return;
  }
  const tmpl = document.getElementById("wb-progress-row-template");
  const newestFirst = entries.slice().reverse();
  newestFirst.forEach((entry, index) => {
    const fragment = tmpl.content.cloneNode(true);
    const li = fragment.querySelector("li");
    const sessionKey = String(entry.session_id || "?") + "#" + (entries.length - 1 - index);
    li.setAttribute("data-session-id", sessionKey);
    const details = li.querySelector("details");
    if (details) {
      details.open = previous.has(sessionKey)
        ? previous.get(sessionKey)
        : index < PROGRESS_OPEN_ENTRIES;
    }
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

export function populateUL(el, items, label) {
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
