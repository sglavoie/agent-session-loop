/* The detail view's panels: state, events, progress, logs, artifacts, reply.
 *
 * Each one is a bounded read rendered into its own region, so a panel that
 * fails says so where it lives instead of taking the detail view with it.
 */

import { apiRequest, describeError, request, submit } from "./api.js";
import { REPLY_CONTEXT_ERROR_HINTS } from "./constants.js";
import { planUrl } from "./identity.js";
import { applyActionAvailability } from "./operations.js";
import { clearFormErrors, showFormError } from "./profile.js";
import { state } from "./state.js";

export function isPinned(planDir) {
  if (!state.detailRegistry || !Array.isArray(state.detailRegistry.pins)) {
    return false;
  }
  return state.detailRegistry.pins.some((pin) => pin.plan_dir === planDir);
}

export function findPin(planDir) {
  if (!state.detailRegistry || !Array.isArray(state.detailRegistry.pins)) {
    return null;
  }
  return (
    state.detailRegistry.pins.find((pin) => pin.plan_dir === planDir) || null
  );
}

export async function loadAndRenderState() {
  try {
    const body = await apiRequest(
      planUrl("/api/state")
    );
    const statePayload = body && body.state ? body.state : null;
    renderActive(statePayload);
    renderVerification(statePayload);
    renderSessionsFromState(body || null);
  } catch (err) {
    // soft fail; the global status line already surfaced the issue.
  }
}

export function renderActive(payload) {
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

export function setField(root, name, value) {
  const el = root.querySelector('[data-field="' + name + '"]');
  if (!el) return;
  el.textContent =
    value === null || value === undefined || value === "" ? "-" : String(value);
}

export function renderVerification(payload) {
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

export function renderSessionsFromState(payload) {
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

export async function loadAndRenderEvents() {
  try {
    const body = await apiRequest(
      planUrl("/api/events", { limit: 20 })
    );
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
    const payloadEl = li.querySelector('[data-field="payload"]');
    if (payloadEl) {
      payloadEl.textContent = event.payload
        ? JSON.stringify(event.payload, null, 2)
        : "";
    }
    list.appendChild(li);
  });
}

export async function loadAndRenderProgress() {
  try {
    const body = await apiRequest(
      planUrl("/api/progress")
    );
    renderProgress(body && Array.isArray(body.entries) ? body.entries : []);
  } catch (err) {
    // soft fail
  }
}

export function renderProgress(entries) {
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

export async function loadAndRenderLog(stream) {
  const el = document.getElementById("wb-detail-" + stream);
  if (!el) return;
  try {
    const body = await apiRequest(
      planUrl("/api/logs", { stream: stream, tail_lines: 120 })
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

export async function loadAndRenderArtifacts() {
  try {
    const body = await apiRequest(
      planUrl("/api/artifacts")
    );
    renderArtifactsPanel(body && body.artifacts ? body.artifacts : {});
  } catch (err) {
    renderArtifactsPanel({});
  }
}

export function renderArtifactsPanel(artifacts) {
  const list = document.getElementById("wb-detail-artifacts");
  if (!list) return;
  list.innerHTML = "";
  const tmpl = document.getElementById("wb-artifact-row-template");
  const labels = [
    ["state_path", "state.json"],
    ["events_path", "events.jsonl"],
    ["progress_path", "progress.md"],
    ["final_review_path", "final-review.md"],
    ["final_review_artifact_path", "final-review.json"],
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

export async function loadAndRenderReply() {
  const panel = document.getElementById("wb-reply-panel");
  if (panel) panel.hidden = false;
  clearFormErrors("wb-reply-form-errors");
  try {
    const body = await apiRequest(
      planUrl("/api/questions")
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

export function renderReplyContextError(payload) {
  state.detailReplyContext = null;
  const reason = payload && payload.reason ? String(payload.reason) : null;
  const error =
    payload && payload.error
      ? String(payload.error)
      : "no paused-Q&A context available";
  const hint = reason ? REPLY_CONTEXT_ERROR_HINTS[reason] : null;
  const message = reason
    ? reason + ": " + (hint ? hint + " " + error : error)
    : error;
  // The same sentence goes under the shut button and into the form error. The
  // prompt box it used to live in alone is inside a collapsed `<details>`, so
  // an operator could otherwise meet a dead "submit reply" with nothing on it.
  setReplyFormDisabled(true, message);
  const promptEl = document.getElementById("wb-reply-prompt");
  if (promptEl) promptEl.textContent = message;
  const lastEl = document.getElementById("wb-reply-last-message");
  if (lastEl) lastEl.textContent = "";
  showFormError("wb-reply-form-errors", message);
}

export function hideReplyPanel() {
  const panel = document.getElementById("wb-reply-panel");
  if (panel) panel.hidden = true;
  state.detailReplyContext = null;
  setReplyFormDisabled(false);
}

/**
 * Shut or reopen the reply form, saying why on the button when it is shut.
 *
 * The reason is written as `data-blocked-reason` rather than left as a bare
 * `disabled`, for two reasons: the capability pass would otherwise re-enable
 * the button on its next sweep, and a greyed-out control with no sentence on
 * it is the thing this view exists not to show.
 */
export function setReplyFormDisabled(disabled, reason) {
  const form = document.getElementById("wb-reply-form");
  if (!form) return;
  const controls = form.querySelectorAll("textarea, button[type=\"submit\"]");
  controls.forEach((control) => {
    control.disabled = Boolean(disabled);
  });
  const button = form.querySelector("button[type=\"submit\"]");
  if (button) {
    if (disabled) {
      button.setAttribute(
        "data-blocked-reason",
        reason || "there is no paused question on this plan to answer"
      );
    } else {
      button.removeAttribute("data-blocked-reason");
    }
  }
  applyActionAvailability(form);
}
