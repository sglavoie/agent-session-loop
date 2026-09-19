/* Shared API handling for both deployments.
 *
 * One request path serves the loopback server and the Pi, because there is
 * one application behind both and the four things an answer can be are the
 * same in either: an immediate result, a request the far side only accepted,
 * a refusal it is willing to explain, and no answer at all.
 *
 * Those four are named here (`OUTCOME_*`) and classified in exactly one
 * place (`request`), so a panel never has to re-derive "did this happen?"
 * from a status code. The distinction that matters most is the second one:
 * a `202` means the server wrote the request down and has not carried it
 * out, and rendering that as success is the single falsehood the whole
 * mutation path is built to avoid.
 *
 * `apiRequest` keeps the historic contract every read in this front end was
 * written against -- it returns the parsed envelope, refusal envelopes
 * included, and throws only when there was no answer to parse. Callers that
 * mutate use `submit`, which hands back the classified answer along with the
 * operation identity the far side recorded.
 */

import { state } from "./state.js";

/** An answer with a body: the thing asked for is in `payload`. */
export const OUTCOME_RESULT = "result";
/** Written down, not carried out. `202`, in both modes. */
export const OUTCOME_PENDING = "pending";
/** The server answered and declined; `reason` and `error` say why. */
export const OUTCOME_REFUSED = "refused";
/** No answer to read: transport failure, or a body that was not an envelope. */
export const OUTCOME_UNREACHABLE = "unreachable";

export const API_OUTCOMES = [
  OUTCOME_RESULT,
  OUTCOME_PENDING,
  OUTCOME_REFUSED,
  OUTCOME_UNREACHABLE,
];

/* Mirrors asl_workbench.operations.OPERATION_STATUSES. The front end never
 * decides an operation's status; it only has to know which words mean the
 * question is still open, so a reload can keep showing an outstanding
 * mutation instead of quietly dropping it. */
export const OPERATION_STATUSES = [
  "pending",
  "dispatched",
  "accepted",
  "consumed",
  "rejected",
  "cancelled",
  "superseded",
  "expired",
  "unconfirmed",
];
export const OPERATION_OUTSTANDING_STATUSES = [
  "pending",
  "dispatched",
  "accepted",
];
/* `unconfirmed` is outstanding *and* uncertain: it may have run. It is held
 * apart because the two call for different words in front of an operator --
 * "still going" versus "nobody can say". */
export const OPERATION_UNCERTAIN_STATUSES = ["unconfirmed"];

/** A refusal or a dead link, carrying everything the envelope said. */
export class ApiError extends Error {
  constructor(answer) {
    super(answer.error || answer.reason || "request failed");
    this.name = "ApiError";
    this.outcome = answer.outcome;
    this.status = answer.status;
    this.reason = answer.reason;
    this.details = answer.details || {};
    this.paused = !!answer.paused;
    this.payload = answer.payload;
    this.operation = answer.operation || null;
  }
}

export function describeError(err) {
  if (err && typeof err.message === "string" && err.message) {
    return err.message;
  }
  return String(err);
}

function buildInit(opts) {
  const headers = Object.assign({ Accept: "application/json" }, opts.headers || {});
  if (opts.method && opts.method !== "GET") {
    if (state.token) {
      headers["Authorization"] = "Bearer " + state.token;
    }
    // Remote mode refuses a mutation that does not carry this, because it is
    // a header no cross-origin form can set. Sent in both modes so there is
    // one request path rather than one per deployment.
    headers["X-Workbench-Request"] = "1";
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
  return init;
}

/** The widest identifier the operations journal will accept. */
export const MAX_OPERATION_ID_CHARS = 128;

/**
 * A fresh identity for one mutation.
 *
 * The remote mode requires every submission to name one, and it is the client
 * that has to mint it: the id is what makes a resend the *same* request rather
 * than a second one, so a server-assigned id would be assigned after the only
 * moment it could have helped. The local mode ignores it, which is why it can
 * be attached unconditionally instead of per deployment.
 */
export function mintOperationId() {
  const random = window.crypto;
  if (random && typeof random.randomUUID === "function") {
    return random.randomUUID().replace(/-/g, "");
  }
  if (random && typeof random.getRandomValues === "function") {
    const bytes = new Uint8Array(16);
    random.getRandomValues(bytes);
    return Array.prototype.map
      .call(bytes, (byte) => byte.toString(16).padStart(2, "0"))
      .join("");
  }
  // Last resort only. Collisions here cost correctness, so it is padded well
  // inside MAX_OPERATION_ID_CHARS rather than shortened.
  return (
    "wb" +
    Math.random().toString(16).slice(2) +
    Math.random().toString(16).slice(2)
  ).slice(0, MAX_OPERATION_ID_CHARS);
}

/* Whether a request body is one this layer may add a field to: a plain
 * object, not a list and not something already serialized. */
function isPlainBody(body) {
  return !!body && typeof body === "object" && !Array.isArray(body);
}

function unreachable(path, status, message, payload, operationId) {
  return {
    outcome: OUTCOME_UNREACHABLE,
    ok: false,
    status: status,
    path: path,
    payload: payload === undefined ? null : payload,
    reason: "unreachable",
    error: message,
    details: {},
    paused: false,
    // No envelope came back, so the only operation identity there is is the
    // one this client sent. Carrying it is what lets a retry be the same
    // request instead of a second one nobody asked for.
    operation: operationId
      ? normalizeOperation({
          id: operationId,
          status: null,
          method: null,
          detail: null,
          replay: false,
          source: "client",
        })
      : null,
    replay: false,
  };
}

/**
 * One request, classified. Never throws for anything the server said.
 *
 * A thrown error here means the exchange itself failed, which is why the
 * unreachable outcome exists: "the Mac never answered" and "the Mac said no"
 * are different facts and a caller that cannot tell them apart will offer the
 * wrong next step.
 */
export async function request(path, options) {
  const opts = Object.assign({}, options || {});
  let operationId = null;
  if (opts.method && opts.method !== "GET" && isPlainBody(opts.body)) {
    // Every mutation carries an identity, minted here once so that each of
    // them behaves the same way in both modes: the Pi refuses a submission
    // that names none, and a caller that wants to display or resend its own
    // id (the redirect composer does) passes one in and gets it back.
    operationId = opts.body.operation_id || opts.operationId || mintOperationId();
    opts.body = Object.assign({}, opts.body, { operation_id: operationId });
  }
  let response;
  try {
    response = await window.fetch(path, buildInit(opts));
  } catch (err) {
    return unreachable(path, 0, describeError(err), undefined, operationId);
  }
  let text;
  try {
    text = await response.text();
  } catch (err) {
    return unreachable(
      path,
      response.status,
      describeError(err),
      undefined,
      operationId
    );
  }
  let parsed = {};
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch (err) {
      return unreachable(
        path,
        response.status,
        "invalid JSON response: " + err.message,
        undefined,
        operationId
      );
    }
  }
  const envelope = parsed && typeof parsed === "object" ? parsed : {};
  const refused = envelope.ok === false;
  if (!response.ok && !refused) {
    // A non-2xx with no envelope: the server did not get as far as having an
    // opinion, so there is nothing to explain and nothing to retry against.
    return unreachable(
      path,
      response.status,
      "HTTP " + response.status,
      parsed,
      operationId
    );
  }
  const operation = readOperation(envelope);
  let outcome = OUTCOME_RESULT;
  if (refused) {
    outcome = OUTCOME_REFUSED;
  } else if (response.status === 202) {
    outcome = OUTCOME_PENDING;
  }
  return {
    outcome: outcome,
    ok: !refused,
    status: response.status,
    path: path,
    payload: parsed,
    reason: typeof envelope.reason === "string" ? envelope.reason : null,
    error: typeof envelope.error === "string" ? envelope.error : null,
    details:
      envelope.details && typeof envelope.details === "object"
        ? envelope.details
        : {},
    paused: !!envelope.paused,
    operation: operation,
    replay: !!(envelope.replay || (operation && operation.replay)),
  };
}

/**
 * The historic read contract: the parsed envelope, refusals included.
 *
 * Reads in this front end branch on `body.ok`, so a refusal has to come back
 * rather than be thrown; only an exchange with no answer in it raises.
 */
export async function apiRequest(path, options) {
  const answer = await request(path, options);
  if (answer.outcome === OUTCOME_UNREACHABLE) {
    throw new ApiError(answer);
  }
  return unwrapDetail(answer.payload);
}

/* The detail kinds whose remote answer is a sequence, and the key each one
 * has locally. Mirrors ROW_DETAILS in
 * agent_session_loop/workbench_remote_reads.py. */
const DETAIL_ROW_KEYS = {
  events: "events",
  commits: "commits",
  progress: "entries",
};

/* The detail kinds whose remote answer is bounded text. Mirrors TEXT_DETAILS
 * in the same module; locally the same read answers with `text`. */
const DETAIL_TEXT_KINDS = ["logs", "last-message", "final-review"];

/**
 * One detail read, in the shape the panes were written against.
 *
 * The two providers answer the same read with the same material in two
 * wrappers. The Mac reads the artifact and answers with it directly. The Pi
 * has no artifact to read: it asks over the link and answers with whatever
 * came back, inside the transport's own `entries`/`content` fields. Both are
 * the truth about the same file, and neither pane should have to ask which
 * deployment it is running in to find it, so the wrapper is removed once,
 * here, rather than branched on in twelve renderers.
 *
 * A local answer has no `detail` key and passes through untouched.
 */
export function unwrapDetail(body) {
  if (!body || typeof body !== "object") return body;
  const kind = body.detail;
  if (typeof kind !== "string") return body;
  if (DETAIL_TEXT_KINDS.indexOf(kind) !== -1) {
    return Object.assign({}, body, {
      text: typeof body.content === "string" ? body.content : "",
    });
  }
  const entries = Array.isArray(body.entries) ? body.entries : [];
  const rowKey = DETAIL_ROW_KEYS[kind];
  if (rowKey) {
    const rows = {};
    rows[rowKey] = entries;
    return Object.assign({}, body, rows);
  }
  // Every other kind is one object, sent as a one-row page. Spreading it is
  // what makes `body.state` and `body.artifacts` mean the same thing in both
  // modes. A page that came back empty is left alone rather than invented:
  // "the link did not answer" is already an error envelope, and anything that
  // reaches here with no row genuinely has no material.
  const first = entries.length === 1 ? entries[0] : null;
  if (!first || typeof first !== "object" || Array.isArray(first)) return body;
  return Object.assign({}, body, first);
}

/**
 * A mutation, classified, with the far side's record of it.
 *
 * Mutations go through here rather than through `apiRequest` because the
 * answer they need is not a body: it is which of the four things happened,
 * and what identity the far side wrote down for it.
 */
export async function submit(path, body, options) {
  const opts = Object.assign({}, options || {}, {
    method: (options && options.method) || "POST",
    body: body,
  });
  return request(path, opts);
}

/**
 * The operation a mutation answer recorded, in one shape whatever wrote it.
 *
 * Three surfaces record a mutation three ways -- the remote provider under
 * `operation`, a local control under `queued_command`, an urgent correction
 * under `redirect` -- and a refusal carries its record in `details` instead.
 * A caller that had to know which is a caller that will read the wrong one.
 */
export function readOperation(payload) {
  if (!payload || typeof payload !== "object") return null;
  const details =
    payload.details && typeof payload.details === "object" ? payload.details : {};
  const remote = payload.operation || details.operation;
  if (remote && typeof remote === "object" && remote.operation_id) {
    return normalizeOperation({
      id: remote.operation_id,
      status: remote.status,
      method: remote.method,
      detail: remote.detail,
      replay: !!payload.replay,
      source: "operation",
    });
  }
  const redirect = payload.redirect || details.redirect;
  if (redirect && typeof redirect === "object") {
    const nested =
      redirect.operation && typeof redirect.operation === "object"
        ? redirect.operation
        : {};
    if (redirect.operation_id || nested.operation_id) {
      return normalizeOperation({
        id: redirect.operation_id || nested.operation_id,
        status: nested.status || redirect.outcome,
        method: "control.redirect",
        detail: nested.stage || redirect.refusal,
        replay: !!redirect.replayed,
        source: "redirect",
      });
    }
  }
  const queued = payload.queued_command;
  if (queued && typeof queued === "object" && queued.id) {
    return normalizeOperation({
      id: queued.id,
      // A queued control is written down and not yet read; the queue keeps no
      // status column, so the status is the fact that it is in the queue.
      status: "pending",
      method: queued.command || "control",
      detail: null,
      replay: false,
      source: "queued_command",
    });
  }
  return null;
}

function normalizeOperation(fields) {
  const status = typeof fields.status === "string" ? fields.status : null;
  return {
    id: String(fields.id),
    status: status,
    method: fields.method || null,
    detail: fields.detail || null,
    replay: !!fields.replay,
    source: fields.source,
    outstanding:
      status === null || OPERATION_OUTSTANDING_STATUSES.indexOf(status) !== -1,
    uncertain: status !== null && OPERATION_UNCERTAIN_STATUSES.indexOf(status) !== -1,
  };
}

/**
 * What to tell an operator about an answer, in the words its outcome earns.
 *
 * Deliberately never says a pending mutation succeeded, and never says an
 * unreachable one failed: neither is known.
 */
export function describeAnswer(answer, subject) {
  const what = subject || "the request";
  if (!answer) return what + " produced no answer";
  const identity =
    answer.operation && answer.operation.id ? " " + answer.operation.id : "";
  if (answer.outcome === OUTCOME_RESULT) {
    return what + identity + " completed";
  }
  if (answer.outcome === OUTCOME_PENDING) {
    if (answer.replay) {
      return what + identity + " was already recorded; nothing resubmitted";
    }
    return what + identity + " was recorded; it has not run yet";
  }
  if (answer.outcome === OUTCOME_REFUSED) {
    return (answer.error || "refused") + " (" + (answer.reason || "refused") + ")";
  }
  if (answer.operation && answer.operation.id) {
    // Uncertain rather than failed: the request may have arrived. Naming the
    // identity is what lets a retry be the same request instead of a second.
    return (
      "no answer: " +
      (answer.error || "the link is down") +
      " — retrying reuses operation id " +
      answer.operation.id
    );
  }
  return "no answer: " + (answer.error || "the link is down");
}
