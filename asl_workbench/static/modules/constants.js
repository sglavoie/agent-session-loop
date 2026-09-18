/* The vocabularies both ends share.
 *
 * Every list here mirrors one in the Python package, and the tests pin them
 * against each other: a word the front end renders that the server never
 * sends is a word an operator would be shown and could not act on.
 */

import { request } from "./api.js";

export const REFRESH_INTERVAL_MS = 5000;
export const DETAIL_REFRESH_INTERVAL_MS = 5000;
export const SEARCH_DEBOUNCE_MS = 150;
export const DESKTOP_SHELL_MEDIA_QUERY = "(min-width: 960px)";
export const TOKEN_STORAGE_KEY = "wb.token";
export const FILTER_STORAGE_KEY = "wb.filter";
export const SEARCH_STORAGE_KEY = "wb.search";
export const SORT_STORAGE_KEY = "wb.sort";
export const COMPLETED_STORAGE_KEY = "wb.completed";
export const SELECTED_STORAGE_KEY = "wb.selected";
export const CLEANUP_MODE_STORAGE_KEY = "wb.cleanup";
export const CLEANUP_THRESHOLD_STORAGE_KEY = "wb.cleanupDays";
export const CLEANUP_DEFAULT_THRESHOLD_DAYS = 30;
export const DAY_MS = 24 * 60 * 60 * 1000;
export const HARNESS_NAMES = ["codex", "claude", "copilot"];
// Mirrors control_queue.{DEFAULT,MIN,MAX}_ABORT_GRACE_SECONDS so the button
// rejects a window the server would reject anyway.
export const ABORT_DEFAULT_GRACE_SECONDS = 120;
export const ABORT_MIN_GRACE_SECONDS = 1;
export const ABORT_MAX_GRACE_SECONDS = 86400;
export const COMMIT_MODES = ["per-session", "end"];
export const PRIMARY_STATES = [
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
export const FILTER_GROUPS = {
  all: PRIMARY_STATES.slice(),
  running: ["running", "verifying"],
  paused: ["paused-for-reply"],
  failed: ["failed", "invalid"],
  stale: ["stale"],
  ready: ["ready"],
  complete: ["complete"],
  recent: ["recent"],
};
export const SORT_MODES = ["urgency", "recent", "label", "path", "oldest"];
export const SORT_MODE_LABELS = {
  urgency: "urgency",
  recent: "last activity",
  label: "label",
  path: "plan path",
  oldest: "oldest activity",
};
export const DEFAULT_SORT_MODE = "urgency";

// Row action button intent → detail-view focus target. Reply jumps the
// operator into the textarea so they can answer immediately; unlock and
// launch surface the existing confirm-before-mutate controls (the row
// never issues mutating POSTs itself).
export const ROW_ACTION_FOCUS = {
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
export const DETAIL_FOCUS_TARGETS = {
  recovery: { scroll: "wb-recovery-panel", focus: null },
  reply: { scroll: "wb-recovery-panel", focus: "wb-reply-text" },
  redirect: { scroll: "wb-redirect-panel", focus: "wb-redirect-text" },
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
export const RECOVERY_ACTION_TARGETS = [
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
export const RECOVERY_TARGET_LABELS = {
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
export const RECOVERY_ACTION_TARGETS_BY_ID = {
  reply: "reply",
  unlock: "unlock",
  "retry-run": "launch",
  "inspect-artifacts": "artifacts",
  "review-profile": "profile",
};
// Urgent-correction vocabulary. The stage strings mirror
// redirect_cli.STAGES; the phase grouping is this surface's own and it
// deliberately keeps acknowledgment apart from the cancellation that
// precedes it and the resumption that carries it, because a worker receipt
// is a different fact from either and collapsing the three would let a
// stopped turn read as an answered correction.
export const REDIRECT_PHASES = [
  "awaiting",
  "cancelling",
  "resuming",
  "acknowledged",
  "unresolved",
  "settled",
];
export const REDIRECT_PHASE_LABELS = {
  awaiting: "awaiting acceptance",
  cancelling: "cancelling the turn",
  resuming: "resuming with the correction",
  acknowledged: "worker acknowledged",
  unresolved: "unresolved",
  settled: "settled without action",
};
export const REDIRECT_STAGE_PHASES = {
  submitted: "awaiting",
  unaccepted: "awaiting",
  interrupting: "cancelling",
  stopped: "cancelling",
  resuming: "resuming",
  acknowledged: "acknowledged",
  rejected: "settled",
  blocked: "settled",
  uncertain: "unresolved",
};
// Stages whose answer is still an operation id rather than a verdict. These
// are what a reconnecting browser has to reconcile rather than resubmit:
// the draft is gone with the tab, but the correction's fate is in the
// journal and the id is how to ask after it.
export const REDIRECT_UNRESOLVED_STAGES = ["submitted", "uncertain"];
// Mirrors redirect_cli.RECONCILE_HINT.
export const REDIRECT_RECONCILE_HINT =
  "reconcile it by id with `asl control redirect-status` before resubmitting; " +
  "a resubmission carrying the same --operation-id is the same request, a new " +
  "one is a second correction";
// Mirrors intervention.ACTION_REDIRECT.
export const REDIRECT_ACTION = "run.redirect";
// Mirrors redirect_store.REDIRECT_MAX_FEEDBACK_BYTES so the composer refuses
// a body the journal would refuse anyway.
export const REDIRECT_MAX_FEEDBACK_BYTES = 65536;
// Mirrors redirect_store.REDIRECT_MAX_OPERATION_ID_CHARS.
export const REDIRECT_MAX_OPERATION_ID_CHARS = 64;
// Mirrors redirect_store.ERROR_REPLAY_CONFLICT. The server says this when the
// operation id in hand already names a *different* correction, which is the
// one refusal the composer cannot answer on the operator's behalf: the draft
// in the textarea and the words in the journal are two corrections, and only
// a person can say whether the second one should be sent at all.
export const REDIRECT_CONFLICT_REASON = "redirect-replay-conflict";
export const REDIRECT_CONFIRM =
  "Interrupt the in-flight turn now and resume the saved conversation with " +
  "this correction first? The session stays pending, the interrupted " +
  "attempt earns no completion, and the text is not retrievable from the UI " +
  "afterward.";
export const REPLY_CONTEXT_ERROR_HINTS = {
  "no-paused-run": "No paused run is waiting for an operator reply.",
  "missing-question-context": "The paused turn no longer has readable question context.",
  "reply-context-mismatch": "The paused session no longer matches the current pending session.",
};
