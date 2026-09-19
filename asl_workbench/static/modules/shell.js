/* Inbox shell layout — desktop two-pane vs mobile stacked.
 *
 * One document, two shapes. A media query decides which, and everything that
 * depends on the answer asks here rather than measuring the viewport again:
 * the queue and detail panes are the same DOM in both shapes, so a phone and
 * a laptop cannot end up rendering different things.
 */

import { DETAIL_FOCUS_TARGETS } from "./constants.js";
import { refresh, scheduleRefresh, stopQueueTimer } from "./queue.js";
import { state } from "./state.js";

export function isDesktopShell() {
  return state.shellMediaQuery ? !!state.shellMediaQuery.matches : false;
}

export function wireShellViewport() {
  const mql = state.shellMediaQuery;
  if (!mql) return;
  const onChange = () => {
    applyShellLayout();
    if (state.route.name !== "detail") return;
    if (isDesktopShell()) {
      refresh();
      scheduleRefresh();
    } else {
      stopQueueTimer();
    }
  };
  if (typeof mql.addEventListener === "function") {
    mql.addEventListener("change", onChange);
  } else if (typeof mql.addListener === "function") {
    mql.addListener(onChange);
  }
}

export function applyShellLayout() {
  const desktop = isDesktopShell();
  const route = state.route.name;
  const hasPreview = !!state.previewPlan;
  const shell = document.getElementById("wb-shell");
  if (shell) {
    shell.setAttribute("data-route", route);
    shell.setAttribute("data-viewport", desktop ? "desktop" : "mobile");
    shell.setAttribute("data-preview", hasPreview ? "active" : "empty");
  }
  const queueEl = document.getElementById("wb-queue");
  const detailEl = document.getElementById("wb-detail");
  const filtersEl = document.getElementById("wb-filters");
  const controlsEl = document.getElementById("wb-controls-bar");
  const warningsEl = document.getElementById("wb-warnings");
  const queueMetaEl = document.getElementById("wb-queue-meta");
  const placeholderEl = document.getElementById("wb-shell-placeholder");
  const previewEl = document.getElementById("wb-shell-preview");
  const archiveEl = document.getElementById("wb-archive");
  const cleanupBarEl = document.getElementById("wb-cleanup-bar");
  if (route === "archive") {
    if (archiveEl) archiveEl.hidden = false;
    if (shell) shell.hidden = true;
    if (filtersEl) filtersEl.hidden = true;
    if (controlsEl) controlsEl.hidden = true;
    if (cleanupBarEl) cleanupBarEl.hidden = true;
    if (queueMetaEl) queueMetaEl.hidden = true;
    if (warningsEl) warningsEl.hidden = true;
    return;
  }
  if (archiveEl) archiveEl.hidden = true;
  if (shell) shell.hidden = false;
  if (cleanupBarEl) cleanupBarEl.hidden = !state.cleanupMode || route === "detail";
  if (route === "detail") {
    if (detailEl) detailEl.hidden = false;
    if (queueEl) queueEl.hidden = !desktop;
    if (filtersEl) filtersEl.hidden = !desktop;
    if (controlsEl) controlsEl.hidden = !desktop;
    if (queueMetaEl) queueMetaEl.hidden = !desktop || !state.lastQueueMeta;
    if (warningsEl && !desktop) warningsEl.hidden = true;
    if (placeholderEl) placeholderEl.hidden = true;
    if (previewEl) previewEl.hidden = true;
  } else {
    if (queueEl) queueEl.hidden = false;
    if (filtersEl) filtersEl.hidden = false;
    if (controlsEl) controlsEl.hidden = false;
    if (queueMetaEl && state.lastQueueMeta) queueMetaEl.hidden = false;
    if (detailEl) detailEl.hidden = true;
    // The CSS placeholder and preview appear via [data-route="queue"] +
    // [data-preview="empty"/"active"] on desktop; keep both elements
    // rendered so the selectors can target them without re-toggling
    // `hidden` on every viewport change.
    if (placeholderEl) placeholderEl.hidden = false;
    if (previewEl) previewEl.hidden = false;
  }
}

// ---------------------------------------------------------------------
// Scroll + focus preservation across refresh polls.
// Polling re-renders panels every 5s; capture-before / restore-after
// keeps the operator's viewport from jumping while they read.
//
// Restoring is conditional on the re-render having actually displaced
// something, because a poll that redrew the same content has moved nobody and
// scrolling anyway is not free. A phone is where it costs: WebKit reads a
// document scroll between `touchstart` and `touchend` as a pan and throws the
// tap away, so an unconditional restore landing mid-press eats the press of an
// operator reaching for a control -- silently, with no request and no
// feedback, which is the one failure this interface may not have. The page
// height is what says whether a redraw displaced anything; when it did, the
// original behaviour stands and the operator keeps their place.

/** The document height, which is what a re-render changes when it displaces. */
function documentHeight() {
  const root = document.documentElement;
  return root ? root.scrollHeight : 0;
}

/** Put a scroll offset back only if it is not already where it belongs. */
function restoreScrollTop(el, offset) {
  if (!el || typeof offset !== "number") return;
  if (el.scrollTop === offset) return;
  el.scrollTop = offset;
}

/** Put the window back only if the redraw moved the page under the operator. */
function restoreWindowScroll(snapshot) {
  if (typeof snapshot.window !== "number") return;
  if (window.scrollY === snapshot.window) return;
  if (documentHeight() === snapshot.height) return;
  window.scrollTo(window.scrollX || 0, snapshot.window);
}

export function captureQueueScroll() {
  const queueEl = document.getElementById("wb-queue");
  return {
    queue: queueEl ? queueEl.scrollTop : 0,
    window: typeof window.scrollY === "number" ? window.scrollY : 0,
    height: documentHeight(),
  };
}

export function restoreQueueScroll(snapshot) {
  if (!snapshot) return;
  restoreScrollTop(document.getElementById("wb-queue"), snapshot.queue);
  restoreScrollTop(
    document.getElementById("wb-preview-last-message-body"),
    snapshot.preview
  );
  restoreWindowScroll(snapshot);
}

export function captureDetailScroll() {
  const events = document.getElementById("wb-detail-events");
  const previewBody = document.getElementById("wb-preview-last-message-body");
  return {
    window: typeof window.scrollY === "number" ? window.scrollY : 0,
    events: events ? events.scrollTop : 0,
    preview: previewBody ? previewBody.scrollTop : 0,
    height: documentHeight(),
  };
}

export function restoreDetailScroll(snapshot) {
  if (!snapshot) return;
  restoreScrollTop(
    document.getElementById("wb-detail-events"),
    snapshot.events
  );
  restoreScrollTop(
    document.getElementById("wb-preview-last-message-body"),
    snapshot.preview
  );
  restoreWindowScroll(snapshot);
}

export function applyDetailFocus() {
  const focusKey = state.route.focus;
  if (!focusKey) return;
  const target = DETAIL_FOCUS_TARGETS[focusKey];
  if (!target) {
    state.route.focus = null;
    return;
  }
  const scrollEl = target.scroll
    ? document.getElementById(target.scroll)
    : null;
  if (scrollEl && typeof scrollEl.scrollIntoView === "function") {
    scrollEl.scrollIntoView({ block: "start", behavior: "auto" });
  }
  if (target.focus) {
    const focusEl = document.getElementById(target.focus);
    if (focusEl && typeof focusEl.focus === "function") {
      try {
        focusEl.focus({ preventScroll: true });
      } catch (err) {
        focusEl.focus();
      }
    }
  }
  // One-shot: subsequent polls must not re-snap the viewport.
  state.route.focus = null;
}
