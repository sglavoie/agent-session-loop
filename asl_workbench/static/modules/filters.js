/* Filter chips.
 *
 * The chips are the queue's triage: each one names a group of states, and the
 * pressed state is restored from preferences so a reload lands where the
 * operator left off.
 */

import { FILTER_GROUPS } from "./constants.js";
import { refreshDetail } from "./detail.js";
import { persistFilter } from "./prefs.js";
import { refresh } from "./queue.js";
import { state } from "./state.js";

export function wireFilterChips() {
  document.querySelectorAll(".wb-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const name = chip.getAttribute("data-filter");
      if (!name || !Object.prototype.hasOwnProperty.call(FILTER_GROUPS, name)) {
        return;
      }
      if (state.activeFilter === name) return;
      state.activeFilter = name;
      persistFilter(name);
      applyChipPressedState();
      refresh();
    });
  });
}

export function applyChipPressedState() {
  document.querySelectorAll(".wb-chip").forEach((chip) => {
    const name = chip.getAttribute("data-filter");
    chip.setAttribute(
      "aria-pressed",
      name === state.activeFilter ? "true" : "false"
    );
  });
}

export function wireRefreshButton() {
  const button = document.getElementById("wb-refresh-now");
  if (!button) return;
  button.addEventListener("click", () => {
    if (state.route.name === "detail") {
      refreshDetail();
    } else {
      refresh();
    }
  });
}
