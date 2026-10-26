/* The detail view's panels: state, events, progress, logs, artifacts, reply.
 *
 * Each one is a bounded read rendered into its own region, so a panel that
 * fails says so where it lives instead of taking the detail view with it.
 */

import {
  OUTCOME_PENDING,
  OUTCOME_RESULT,
  apiRequest,
  describeError,
} from "./api.js";
import {
  LINKED_QUESTION_OPEN_TIMEOUT_MS,
  REPLY_CONTEXT_ERROR_HINTS,
} from "./constants.js";
import { planReadGuard, planUrl, routePlanId } from "./identity.js";
import {
  applyActionAvailability,
  receiptsForPlan,
} from "./operations.js";
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

export async function loadAndRenderLog(stream) {
  const el = document.getElementById("wb-detail-" + stream);
  if (!el) return;
  const stale = planReadGuard();
  try {
    const body = await apiRequest(
      planUrl("/api/logs", { stream: stream, tail_lines: 120 })
    );
    if (stale()) return;
    if (body && body.ok && typeof body.text === "string") {
      el.textContent = body.text || "(empty)";
    } else {
      el.textContent = "(no log)";
    }
  } catch (err) {
    if (stale()) return;
    el.textContent = "(no log)";
  }
}

export async function loadAndRenderArtifacts() {
  const stale = planReadGuard();
  try {
    const body = await apiRequest(
      planUrl("/api/artifacts")
    );
    if (stale()) return;
    renderArtifactsPanel(body && body.artifacts ? body.artifacts : {});
  } catch (err) {
    if (stale()) return;
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
  const requestedPlan = routePlanId();
  const requestedQuestion = state.route && state.route.questionId;
  const panel = document.getElementById("wb-reply-panel");
  if (panel) panel.hidden = false;
  try {
    const body = await apiRequest(
      planUrl(
        "/api/questions",
        requestedQuestion ? { question_id: requestedQuestion } : null
      )
    );
    if (
      routePlanId() !== requestedPlan ||
      (state.route && state.route.questionId) !== requestedQuestion
    ) {
      return;
    }
    if (body && body.ok) {
      const questionId = body.question_id ? String(body.question_id) : null;
      const linkedQuestion = state.route && state.route.questionId;
      if (linkedQuestion && questionId !== linkedQuestion) {
        renderReplyPending(linkedQuestion);
        return;
      }
      state.reply.openingQuestionId = null;
      state.reply.openingStartedAt = null;
      const questionChanged = state.reply.questionId !== questionId;
      if (questionChanged) {
        state.reply.operationId = null;
        state.reply.questionId = questionId;
        state.reply.recordedQuestionId = null;
        state.reply.recordedOperationId = null;
        clearFormErrors("wb-reply-form-errors");
        showReplyStatus("", "");
      }
      restoreRecordedReply(questionId);
      if (
        questionId &&
        state.reply.deliveredQuestionId === questionId
      ) {
        // This read may have started before the reply settled. A late copy of
        // the formerly live question cannot downgrade the terminal delivery
        // state back to "recorded" or reopen its form.
        hideReplyPanel();
        return;
      }
      state.detailReplyContext = body;
      const form = document.getElementById("wb-reply-form");
      if (form) form.hidden = false;
      const recorded =
        questionId && state.reply.recordedQuestionId === questionId;
      setReplyFormDisabled(
        Boolean(recorded),
        recorded ? "reply recorded; waiting for the running session" : null
      );
      const submit = document.querySelector(
        '#wb-reply-form button[type="submit"]'
      );
      if (submit) {
        const label = recorded
          ? "reply recorded"
          : body.context_kind === "waiting"
            ? "send reply"
            : "reply and resume";
        if (submit.textContent !== label) submit.textContent = label;
      }
      if (recorded) {
        const recordedIdentity = state.reply.recordedOperationId
          ? " for question " +
            questionId +
            " as operation " +
            state.reply.recordedOperationId +
            "."
          : ".";
        showReplyStatus(
          "pending",
          "Reply recorded" +
            recordedIdentity +
            " Waiting for the running session to consume it."
        );
      }
      const promptEl = document.getElementById("wb-reply-prompt");
      const prompt = body.rendered_prompt || "";
      if (promptEl && promptEl.textContent !== prompt) promptEl.textContent = prompt;
      const promptDetails = document.getElementById("wb-reply-prompt-details");
      if (promptDetails && linkedQuestion) promptDetails.open = true;
      const hintEl = document.getElementById("wb-reply-hint");
      if (hintEl && linkedQuestion) {
        hintEl.textContent =
          "This notification opened the current question below. Reply sends the answer to the running session.";
      }
      if (linkedQuestion && !recorded && questionChanged) {
        showReplyStatus("info", "Current question loaded from this notification.");
      }
      if (
        linkedQuestion &&
        state.route &&
        state.route.focus === "reply" &&
        !recorded
      ) {
        const textarea = document.getElementById("wb-reply-text");
        if (textarea && typeof textarea.focus === "function") {
          try {
            textarea.focus({ preventScroll: true });
          } catch (err) {
            textarea.focus();
          }
        }
        // One-shot just like every other detail focus: a later question poll
        // must not steal the keyboard back from the operator.
        state.route.focus = null;
      }
      const lastEl = document.getElementById("wb-reply-last-message");
      const lastMessage = body.last_message || "";
      if (lastEl && lastEl.textContent !== lastMessage) {
        lastEl.textContent = lastMessage;
      }
    } else {
      const linkedQuestion = state.route && state.route.questionId;
      restoreRecordedReply(linkedQuestion);
      const recordedQuestion = state.reply.recordedQuestionId;
      const detailReason =
        body && body.details && typeof body.details === "object"
          ? body.details.reason
          : null;
      const questionNotFound =
        body &&
        body.reason === "detail-refused" &&
        detailReason === "not-found";
      const questionTimedOut = body && body.reason === "detail-timeout";
      const linkedNotFound = linkedQuestion && questionNotFound;
      if (
        recordedQuestion &&
        (!linkedQuestion || linkedQuestion === recordedQuestion) &&
        questionNotFound
      ) {
        // The recorded reply made this exact question disappear from the
        // connector.  That is its normal terminal state after ASL consumes the
        // reply, so preserve the durable submission feedback instead of
        // replacing it with the remote read's expected not-found refusal.
        hideReplyPanel();
        return;
      }
      if (linkedNotFound || (linkedQuestion && questionTimedOut)) {
        const startedAt = state.reply.openingStartedAt;
        if (
          state.reply.openingQuestionId !== linkedQuestion ||
          typeof startedAt !== "number" ||
          Date.now() - startedAt < LINKED_QUESTION_OPEN_TIMEOUT_MS
        ) {
          renderReplyPending(linkedQuestion);
          return;
        }
        renderReplyContextError({
          error:
            questionTimedOut
              ? "This alert's question could not be loaded before the deadline. Open the latest notification or return to the queue."
              : "This alert's question is no longer available. Open the latest notification or return to the queue.",
        });
        return;
      }
      renderReplyContextError(body);
    }
  } catch (err) {
    renderReplyContextError({
      reason: "request-failed",
      error: describeError(err),
    });
  }
}

/** Restore a durable live-reply receipt after Safari reloads the alert tab. */
export function restoreRecordedReply(questionId) {
  if (!questionId) return false;
  const outstanding = state.detailOutstanding;
  const target =
    outstanding && outstanding.target && typeof outstanding.target === "object"
      ? outstanding.target
      : {};
  const heldQuestion =
    outstanding && (outstanding.question_id || target.question_id);
  if (
    outstanding &&
    outstanding.operation_id &&
    outstanding.method === "question.reply" &&
    heldQuestion === questionId
  ) {
    state.reply.recordedQuestionId = questionId;
    state.reply.recordedOperationId = String(outstanding.operation_id);
    return true;
  }
  const receipt = receiptsForPlan(routePlanId()).find(
    (entry) =>
      entry.action === "reply" &&
      entry.question_id === questionId &&
      (entry.outcome === OUTCOME_PENDING || entry.outcome === OUTCOME_RESULT) &&
      entry.operation_id
  );
  if (!receipt) return false;
  state.reply.recordedQuestionId = questionId;
  state.reply.recordedOperationId = receipt.operation_id;
  return true;
}

/** Keep a question-bound notification on its exact question while data catches up. */
export function renderReplyPending(questionId) {
  if (state.reply.openingQuestionId !== questionId) {
    state.reply.openingQuestionId = questionId || null;
    state.reply.openingStartedAt = Date.now();
  }
  state.detailReplyContext = null;
  const panel = document.getElementById("wb-reply-panel");
  if (panel) panel.hidden = false;
  const form = document.getElementById("wb-reply-form");
  if (form) form.hidden = true;
  clearFormErrors("wb-reply-form-errors");
  const promptEl = document.getElementById("wb-reply-prompt");
  if (promptEl) promptEl.textContent = "Opening the question from this alert…";
  const promptDetails = document.getElementById("wb-reply-prompt-details");
  if (promptDetails) promptDetails.open = true;
  const hintEl = document.getElementById("wb-reply-hint");
  if (hintEl) {
    hintEl.textContent =
      "Waiting for the exact question carried by this notification. This page updates automatically.";
  }
  const lastEl = document.getElementById("wb-reply-last-message");
  if (lastEl) lastEl.textContent = questionId ? "question " + questionId : "";
  showReplyStatus("pending", "Opening the current question…");
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
  if (state.reply.recordedQuestionId) {
    const firstDelivery =
      state.reply.deliveredQuestionId !== state.reply.recordedQuestionId;
    state.reply.deliveredQuestionId = state.reply.recordedQuestionId;
    if (panel) panel.hidden = false;
    const form = document.getElementById("wb-reply-form");
    if (form) form.hidden = true;
    showReplyStatus(
      "info",
      "Reply delivered. The question is closed and the run resumed."
    );
    state.detailReplyContext = null;
    if (firstDelivery) {
      // A reply can settle inside a detail poll. That poll normally restores
      // the operator's old scroll position after rendering, which would hide
      // this terminal state below the fold. Defer the one-time return to the
      // top until the poll's finally block; outside a poll, do it now.
      if (state.detailInFlight) {
        state.reply.scrollToTopAfterDelivery = true;
      } else {
        window.scrollTo(window.scrollX || 0, 0);
      }
    }
    return;
  }
  if (panel) panel.hidden = true;
  state.detailReplyContext = null;
  setReplyFormDisabled(false);
}

export function resetReplyForPlan() {
  state.reply.operationId = null;
  state.reply.questionId = null;
  state.reply.recordedQuestionId = null;
  state.reply.recordedOperationId = null;
  state.reply.deliveredQuestionId = null;
  state.reply.scrollToTopAfterDelivery = false;
  state.reply.openingQuestionId = null;
  state.reply.openingStartedAt = null;
  const form = document.getElementById("wb-reply-form");
  if (form) {
    form.hidden = false;
    form.removeAttribute("aria-busy");
  }
  clearFormErrors("wb-reply-form-errors");
  showReplyStatus("", "");
}

export function showReplyStatus(kind, message) {
  const status = document.getElementById("wb-reply-submit-status");
  if (!status) return;
  if (!message) {
    status.hidden = true;
    status.textContent = "";
    status.removeAttribute("data-kind");
    return;
  }
  status.hidden = false;
  status.setAttribute("data-kind", kind || "info");
  status.textContent = message;
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
    const next = Boolean(disabled);
    if (control.disabled !== next) control.disabled = next;
  });
  const button = form.querySelector("button[type=\"submit\"]");
  if (button) {
    if (disabled) {
      const blockedReason =
        reason || "there is no paused question on this plan to answer";
      if (button.getAttribute("data-blocked-reason") !== blockedReason) {
        button.setAttribute("data-blocked-reason", blockedReason);
      }
    } else if (button.hasAttribute("data-blocked-reason")) {
      button.removeAttribute("data-blocked-reason");
    }
  }
  applyActionAvailability(form);
}
