/* The detail view's panels: state, events, progress, logs, artifacts, reply.
 *
 * Each one is a bounded read rendered into its own region, so a panel that
 * fails says so where it lives instead of taking the detail view with it.
 */

import { apiRequest } from "./api.js";
import { planReadGuard, planUrl } from "./identity.js";
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
  const stale = planReadGuard();
  try {
    const body = await apiRequest(
      planUrl("/api/state")
    );
    if (stale()) return;
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
  setText(
    el,
    value === null || value === undefined || value === "" ? "-" : String(value)
  );
}

/**
 * Write text only when it differs from what is already there.
 *
 * A poll redraws the same facts every few seconds. Rewriting an unchanged
 * node is not free: it replaces the text node, drops a selection the operator
 * was making, and is a mutation assistive technology may announce.
 */
export function setText(el, text) {
  if (!el) return;
  const next = text === null || text === undefined ? "" : String(text);
  if (el.textContent !== next) el.textContent = next;
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

export {
  loadAndRenderEvents,
  renderEvents,
  EVENT_SUMMARY_FIELDS,
  EVENT_SUMMARY_LIMIT,
  summarizeEventPayload,
  formatEventTime,
  loadAndRenderProgress,
  PROGRESS_OPEN_ENTRIES,
  renderProgress,
  populateUL,
} from "./panel_activity.js";
export {
  loadAndRenderLog,
  loadAndRenderArtifacts,
  renderArtifactsPanel,
} from "./panel_artifacts.js";
export {
  loadAndRenderReply,
  restoreRecordedReply,
  renderReplyPending,
  renderReplyContextError,
  hideReplyPanel,
  resetReplyForPlan,
  showReplyStatus,
  setReplyFormDisabled,
} from "./panel_reply.js";
