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
  // The narrowing the server does, rather than the browser: project and
  // harness go out as query parameters and come back applied, so the counts
  // and the page describe the catalog instead of describing the first 200
  // rows of it. `null` is "every project" / "every harness".
  projectFilter: null,
  harnessFilter: null,
  // Whether the inactive-work window's overflow is drawn. Off by default,
  // because the window exists to get quiet work out of the way; the rows are
  // moved rather than dropped, and this is how they are reached.
  showOlderRuns: false,
  // What the last queue answer said about itself, as opposed to about plans:
  // the chip counts over the complete matched set, the facets to offer, how
  // much of the catalog the page covered, and how live the observation behind
  // it is. Held so a render that did not refetch still renders the truth.
  // The last answer, kept whole so a control that only changes where rows are
  // drawn can redraw without asking the server the same question again.
  lastQueuePayload: null,
  lastQueuePayloadAt: null,
  queueFilters: null,
  queueCounts: null,
  queueFacets: { projects: [], harnesses: [] },
  queuePage: null,
  queueFreshness: null,
  queueConnectors: [],
  queueIncidents: [],
  queueObservedAt: null,
  queueCompleteness: null,
  queueCatalogRevision: null,
  // Finished runs inside the retention window, newest first, and what the
  // read said about itself. Separate from the queue because history is a
  // different question with a different retention.
  historyEntries: [],
  historyMeta: null,
  historyInFlight: false,
  historyOutcome: null,
  selectedPlan: null,
  cleanupMode: false,
  cleanupThresholdDays: CLEANUP_DEFAULT_THRESHOLD_DAYS,
  cleanupSelection: new Set(),
  cleanupCandidates: new Map(),
  cleanupSelectionRevision: null,
  cleanupReviewOpen: false,
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
  // `entryId` is the Companion's third link format: a notification about a run
  // that has already finished names a history entry, not a plan, and there is
  // nothing live left to open.
  route: {
    name: "queue",
    planDir: null,
    planKey: null,
    runKey: null,
    questionId: null,
    entryId: null,
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
  // The live reply's retry identity, held on exactly the terms the correction
  // draft is: minted at the first submit, kept through every answer that did
  // not settle, and dropped the moment the draft it names is gone. The reply
  // text itself is not here for the same reason it is not in storage.
  reply: {
    operationId: null,
    submittedIds: [],
    questionId: null,
    recordedQuestionId: null,
    recordedOperationId: null,
    deliveredQuestionId: null,
    scrollToTopAfterDelivery: false,
    openingQuestionId: null,
    openingStartedAt: null,
  },
  // What `GET /api/capabilities` said this deployment can carry out, and the
  // read that is finding out. `null` means nobody has asked yet, which every
  // reader treats as "available": a capability read that has not answered is
  // not evidence that an operation is refused.
  capabilities: null,
  capabilitiesInFlight: null,
  // The mutations this tab has sent and not heard back about, by action name.
  // A guard rather than a record: what outlives the answer is the receipt.
  operationsInFlight: new Set(),
  // The operation the *server* still holds for the plan on screen, as the
  // remote mode reports it on every plan read. The authority on what is
  // outstanding, since it survives this tab entirely.
  detailOutstanding: null,
  // Archive ids whose expiry the operator has read and acknowledged. Purge
  // refuses until an expired selection has been through that review.
  archiveReviewed: new Set(),
  // The purge that is waiting on that review: the exact ids the operator asked
  // for, held so acknowledging resumes *that* request rather than whatever the
  // selection happens to be by then.
  archiveReviewPending: null,
  detailHistoryByPlan: new Map(),
  shellMediaQuery: null,
  searchDebounceTimer: null,
};
