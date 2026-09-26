/* ASL Workbench — the one front end, served by both deployments.
 *
 * This file is the entry point and nothing else: it restores the preferences
 * the last visit left behind, wires every control, and hands the first render
 * to the router. The behavior lives in `modules/`, split by what it is
 * responsible for rather than by when it happens to run.
 *
 * Native ES modules, no build step and no runtime dependency — the same
 * assets the loopback server serves off a laptop are the assets the Pi serves
 * over Tailscale, because there is only one copy of them and no step in
 * between that could make the two differ.
 *
 * Two things are deliberately never persisted: the reply textarea and the
 * urgent-correction draft. Both are cleared from memory on submission and
 * neither is written to sessionStorage or localStorage, so closing the tab is
 * the end of them.
 */

import {
  wireArchiveControls,
  wireEmptyStateActions,
} from "./modules/archive.js";
import { updateCleanupReview, wireCleanupControls } from "./modules/cleanup.js";
import { DESKTOP_SHELL_MEDIA_QUERY } from "./modules/constants.js";
import { wireControls } from "./modules/controls.js";
import { refreshDetail } from "./modules/detail.js";
import {
  applyChipPressedState,
  wireFilterChips,
  wireRefreshButton,
} from "./modules/filters.js";
import { wireHistoryControls } from "./modules/history.js";
import {
  resolveToken,
  restoreCleanupMode,
  restoreCleanupThreshold,
  restoreFilter,
  restoreHarnessFilter,
  restoreProjectFilter,
  restoreSearchText,
  restoreSelectedPlan,
  restoreShowCompleted,
  restoreShowOlderRuns,
  restoreSortMode,
} from "./modules/prefs.js";
import {
  applyActionAvailability,
  loadCapabilities,
  renderOperationReceipts,
} from "./modules/operations.js";
import { wirePreviewOpen } from "./modules/preview.js";
import { wireProfileForm } from "./modules/profile.js";
import { refresh } from "./modules/queue.js";
import {
  hydrateQueueControls,
  wireFacetControls,
  wireOlderRunsControl,
  wireSearchControl,
  wireShowCompletedControl,
  wireSortControl,
} from "./modules/queue_controls.js";
import { wireRedirectForm } from "./modules/redirect.js";
import { wireReplyForm } from "./modules/reply.js";
import { applyRoute, wireDetailBack } from "./modules/router.js";
import { isDesktopShell, wireShellViewport } from "./modules/shell.js";
import { state } from "./modules/state.js";

export function init() {
  state.token = resolveToken();
  state.activeFilter = restoreFilter();
  state.searchText = restoreSearchText();
  state.sortMode = restoreSortMode();
  state.showCompleted = restoreShowCompleted();
  state.projectFilter = restoreProjectFilter();
  state.harnessFilter = restoreHarnessFilter();
  state.showOlderRuns = restoreShowOlderRuns();
  state.selectedPlan = restoreSelectedPlan();
  state.cleanupMode = restoreCleanupMode();
  state.cleanupThresholdDays = restoreCleanupThreshold();
  state.shellMediaQuery =
    typeof window.matchMedia === "function"
      ? window.matchMedia(DESKTOP_SHELL_MEDIA_QUERY)
      : null;
  wireFilterChips();
  wireRefreshButton();
  wireDetailBack();
  wireControls();
  wireProfileForm();
  wireReplyForm();
  wireRedirectForm();
  wireShellViewport();
  wirePreviewOpen();
  wireSearchControl();
  wireSortControl();
  wireShowCompletedControl();
  wireFacetControls();
  wireOlderRunsControl();
  wireHistoryControls();
  wireCleanupControls();
  wireArchiveControls();
  wireEmptyStateActions();
  hydrateQueueControls();
  applyChipPressedState();
  // Anything this tab already knows is outstanding is on screen before the
  // first read answers: an operation nobody confirmed is the thing a reload
  // most needs to show, and waiting for the network to say so would hide it
  // exactly when the network is the problem.
  renderOperationReceipts();
  applyRoute();
  // Asked once, not awaited: every control works on the assumption that an
  // operation is available until the deployment says otherwise, so the answer
  // narrows the interface rather than unlocking it.
  loadCapabilities().then(() => {
    updateCleanupReview();
    applyActionAvailability();
  });
  window.addEventListener("hashchange", applyRoute);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (state.route.name === "detail") {
      refreshDetail();
      if (isDesktopShell()) refresh();
    } else {
      refresh();
    }
  });
}

// A module script is deferred, so the document may already be parsed by the
// time this runs; waiting for an event that has been and gone would leave a
// blank page.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
