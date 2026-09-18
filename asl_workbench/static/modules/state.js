/* The whole of this tab's memory.
 *
 * One object, so what survives a poll and what does not is answerable by
 * reading a single declaration. Two fields are deliberately absent: the reply
 * text and the correction draft are held in the DOM and in `redirect`, and
 * neither is ever written to storage.
 */

import {
  CLEANUP_DEFAULT_THRESHOLD_DAYS,
  DEFAULT_SORT_MODE,
} from "./constants.js";

export const state = {
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
  // What retention makes of the loaded entries, never what it did to them:
  // the archive read no longer deletes, so this is a count of candidates.
  archiveCleanup: null,
  previewPlan: null,
  lastVisiblePlans: [],
  rowsByPlan: new Map(),
  refreshTimer: null,
  fetchInFlight: false,
  lastQueueMeta: null,
  // Both historic link formats land here: `planDir` is the Mac's address,
  // `planKey` the Pi's, and `runKey` is an unresolved Companion notification
  // link waiting for a catalog page to name the plan it belongs to.
  route: {
    name: "queue",
    planDir: null,
    planKey: null,
    runKey: null,
    focus: null,
  },
  detailTimer: null,
  detailInFlight: false,
  detailPlan: null,
  detailRegistry: null,
  recoveryPanelPlanId: null,
  detailReplyContext: null,
  // Urgent-correction composer state. `operationId` is the durable identity
  // of the correction being composed: minted at the first submit and kept
  // through every uncertain, refused, or failed answer so a retry is the
  // same request rather than a second correction. Like the draft it names,
  // it lives in memory only — never sessionStorage, never localStorage —
  // which is exactly why a reload recovers operations from the journal
  // instead of from this object.
  redirect: {
    // The draft's retry identity. Cleared once an answer is durable, because
    // past that point there is nothing left to retry under it.
    operationId: null,
    // The ids this tab submitted, kept after their drafts are gone purely so
    // the operator can find their own correction in a list that also holds
    // corrections sent from the CLI or another tab. Attribution, not
    // identity: it is never sent back to the server.
    submittedIds: [],
    inFlight: false,
    actionToken: null,
    permitted: false,
    disabledReason: null,
    target: null,
    status: null,
  },
  conversationRecovery: null,
  detailHistoryByPlan: new Map(),
  shellMediaQuery: null,
  searchDebounceTimer: null,
};
