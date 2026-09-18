/* Controls: launch, stop, abort, harness, unlock, archive.
 *
 * Every one of these changes something, so every one of them confirms first
 * where the change is hard to undo, and none of them reports success from a
 * status code alone.
 */

import { apiRequest, describeError } from "./api.js";
import {
  ABORT_DEFAULT_GRACE_SECONDS,
  ABORT_MAX_GRACE_SECONDS,
  ABORT_MIN_GRACE_SECONDS,
  HARNESS_NAMES,
} from "./constants.js";
import { planBody, routePlanId } from "./identity.js";
import { clearSelectedPlan } from "./prefs.js";
import {
  clearFormErrors,
  readProfileFormPayload,
  showFormError,
  validateLaunchPayload,
} from "./profile.js";
import { refresh } from "./queue.js";
import { navigateToQueue } from "./router.js";
import { state } from "./state.js";

export function showControlsFeedback(kind, message) {
  const el = document.getElementById("wb-controls-feedback");
  if (!el) return;
  if (!message) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.setAttribute("data-kind", kind);
  el.textContent = message;
}

export function wireControls() {
  const launch = document.getElementById("wb-control-launch");
  if (launch) {
    launch.addEventListener("click", handleLaunchClick);
  }
  const stop = document.getElementById("wb-control-stop");
  if (stop) {
    stop.addEventListener("click", handleStopClick);
  }
  const abort = document.getElementById("wb-control-abort");
  if (abort) {
    abort.addEventListener("click", handleAbortClick);
  }
  const harnessApply = document.getElementById("wb-control-harness-apply");
  if (harnessApply) {
    harnessApply.addEventListener("click", handleHarnessSelectClick);
  }
  const pin = document.getElementById("wb-control-pin");
  if (pin) {
    pin.addEventListener("click", () => {
      const form = document.getElementById("wb-profile-form");
      if (form) {
        form.scrollIntoView({ behavior: "smooth", block: "start" });
        const labelInput = form.elements.namedItem("label");
        if (labelInput && typeof labelInput.focus === "function") {
          labelInput.focus();
        }
      }
    });
  }
  const unlock = document.getElementById("wb-control-unlock");
  if (unlock) {
    unlock.addEventListener("click", handleUnlockClick);
  }
  const archivePlan = document.getElementById("wb-control-archive");
  if (archivePlan) {
    archivePlan.addEventListener("click", handleArchiveClick);
  }
}

export async function handleLaunchClick() {
  if (!routePlanId()) return;
  clearFormErrors("wb-profile-form-errors");
  showControlsFeedback("", "");
  const payload = readProfileFormPayload();
  const errors = validateLaunchPayload(payload);
  if (errors.length > 0) {
    showFormError("wb-profile-form-errors", errors.join("; "));
    return;
  }
  if (payload.profile.unsafe_bypass) {
    const confirmed = window.confirm(
      "Launch with --unsafe-bypass? This skips the loop-ready safety gate."
    );
    if (!confirmed) return;
  }
  try {
    const body = await apiRequest("/api/launch", {
      method: "POST",
      body: planBody({ profile: payload.profile }),
    });
    if (body && body.ok) {
      const pid = body.launch && body.launch.pid;
      showControlsFeedback(
        "info",
        "launched" + (pid ? " (pid " + pid + ")" : "")
      );
    } else {
      showControlsFeedback(
        "error",
        (body && body.error) || "launch failed"
      );
    }
  } catch (err) {
    showControlsFeedback("error", describeError(err));
  }
}

export async function handleStopClick() {
  if (!routePlanId()) return;
  showControlsFeedback("", "");
  try {
    const body = await apiRequest(
      "/api/control/stop-after-current",
      { method: "POST", body: planBody() }
    );
    if (body && body.ok) {
      showControlsFeedback("info", "stop queued; applies at next checkpoint");
    } else {
      showControlsFeedback(
        "error",
        (body && body.error) || "failed to enqueue stop"
      );
    }
  } catch (err) {
    showControlsFeedback("error", describeError(err));
  }
}

export async function handleAbortClick() {
  if (!routePlanId()) return;
  const graceInput = document.getElementById("wb-control-abort-grace");
  const raw = graceInput ? String(graceInput.value).trim() : "";
  const grace = raw === "" ? ABORT_DEFAULT_GRACE_SECONDS : Number(raw);
  if (
    !Number.isInteger(grace) ||
    grace < ABORT_MIN_GRACE_SECONDS ||
    grace > ABORT_MAX_GRACE_SECONDS
  ) {
    showControlsFeedback(
      "error",
      "grace must be a whole number of seconds between " +
        ABORT_MIN_GRACE_SECONDS +
        " and " +
        ABORT_MAX_GRACE_SECONDS
    );
    return;
  }
  const confirmed = window.confirm(
    "Abort the in-flight turn? The worker gets " +
      grace +
      "s to wind down, then the harness is killed and the session is left pending."
  );
  if (!confirmed) return;
  showControlsFeedback("", "");
  try {
    const body = await apiRequest("/api/control/abort", {
      method: "POST",
      body: planBody({ grace_seconds: grace }),
    });
    if (body && body.ok) {
      showControlsFeedback(
        "info",
        "abort queued with " +
          grace +
          "s grace; " +
          ((body && body.note) || "applies at the next drain")
      );
    } else {
      showControlsFeedback(
        "error",
        (body && body.error) || "failed to enqueue abort"
      );
    }
  } catch (err) {
    showControlsFeedback("error", describeError(err));
  }
}

export async function handleHarnessSelectClick() {
  if (!routePlanId()) return;
  const select = document.getElementById("wb-control-harness");
  if (!select) return;
  const harness = select.value;
  if (!harness) {
    showControlsFeedback("error", "choose a harness first");
    return;
  }
  if (HARNESS_NAMES.indexOf(harness) === -1) {
    showControlsFeedback("error", "unknown harness");
    return;
  }
  showControlsFeedback("", "");
  try {
    const body = await apiRequest(
      "/api/control/select-harness",
      {
        method: "POST",
        body: planBody({ harness: harness }),
      }
    );
    if (body && body.ok) {
      showControlsFeedback(
        "info",
        "harness " + harness + " queued for next session"
      );
    } else {
      showControlsFeedback(
        "error",
        (body && body.error) || "failed to enqueue harness select"
      );
    }
  } catch (err) {
    showControlsFeedback("error", describeError(err));
  }
}

export async function handleUnlockClick() {
  if (!routePlanId()) return;
  const confirmed = window.confirm(
    "Force-unlock this plan? Only do this when you are sure no runner is active; mid-run unlock can corrupt state."
  );
  if (!confirmed) return;
  showControlsFeedback("", "");
  try {
    const body = await apiRequest("/api/unlock", {
      method: "POST",
      body: planBody({ force: true }),
    });
    if (body && body.ok) {
      const removed = body.removed ? "removed" : "no lock present";
      showControlsFeedback("info", "unlock: " + removed);
    } else {
      showControlsFeedback(
        "error",
        (body && body.error) || "unlock failed"
      );
    }
  } catch (err) {
    showControlsFeedback("error", describeError(err));
  }
}

export async function handleArchiveClick() {
  // Confirmed by typing the plan's own path, so a mode that addresses
  // plans by digest and never publishes a path has nothing to confirm
  // against and does not offer the archive from here.
  const planDir =
    (state.detailPlan && state.detailPlan.plan_dir) || state.route.planDir;
  if (!planDir || !routePlanId()) return;
  const typed = window.prompt(
    "Archive this plan? This moves the plan directory, Workbench pin, and ASL state into the archive, where it can be restored until retention expires. Type the full plan path to confirm:\n\n" +
      planDir,
    ""
  );
  if (typed === null) return;
  const confirmPlanDir = (typed || "").trim();
  if (!confirmPlanDir) {
    showControlsFeedback("error", "archive confirmation required");
    return;
  }
  if (confirmPlanDir !== planDir) {
    showControlsFeedback("error", "confirmation must match the full plan path");
    return;
  }
  showControlsFeedback("", "");
  try {
    const body = await apiRequest("/api/plan/archive", {
      method: "POST",
      body: planBody({ confirm_plan_dir: confirmPlanDir }),
    });
    if (body && body.ok) {
      state.detailPlan = null;
      state.detailRegistry = null;
      state.detailReplyContext = null;
      clearSelectedPlan();
      navigateToQueue();
      refresh();
    } else {
      showControlsFeedback(
        "error",
        (body && body.error) || "archive failed"
      );
    }
  } catch (err) {
    showControlsFeedback("error", describeError(err));
  }
}
