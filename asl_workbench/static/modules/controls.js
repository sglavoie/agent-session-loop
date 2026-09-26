/* Controls: launch, stop, abort, harness, unlock, archive.
 *
 * Every one of these changes something, so every one of them confirms first
 * where the change is hard to undo, and none of them reports success from a
 * status code alone. The second half of that sentence is what `operations.js`
 * now carries: all six go through `runMutation`, which is the only place that
 * knows a `202` is not a success, that an unanswered request still has an
 * identity worth keeping, and that a control already waiting for an answer
 * must not be pressed twice.
 *
 * It matters in both modes, not just the remote one. `stop-after-current`,
 * `select-harness` and `abort` are queued rather than performed even on the
 * laptop -- `workbench_local._queued` answers `202` -- so "queued" was being
 * drawn from `body.ok` in exactly the case where `ok` does not mean done.
 */

import { OUTCOME_RESULT } from "./api.js";
import {
  ABORT_DEFAULT_GRACE_SECONDS,
  ABORT_MAX_GRACE_SECONDS,
  ABORT_MIN_GRACE_SECONDS,
  HARNESS_NAMES,
} from "./constants.js";
import { planBody, routePlanId } from "./identity.js";
import { captureFocus, runMutation } from "./operations.js";
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
  if (state.capabilities?.mode === "remote") {
    await runMutation({
      action: "launch",
      path: "/api/launch",
      body: planBody({}),
      subject: "the launch with the Mac default profile",
      feedback: showControlsFeedback,
    });
    return;
  }
  const payload = readProfileFormPayload();
  const errors = validateLaunchPayload(payload);
  if (errors.length > 0) {
    showFormError("wb-profile-form-errors", errors.join("; "));
    return;
  }
  if (payload.profile.unsafe_bypass) {
    // The dialog takes the keyboard whichever way it is answered, so the
    // control that opened it gets it back either way.
    const restore = captureFocus();
    const confirmed = window.confirm(
      "Launch with --unsafe-bypass? This skips the loop-ready safety gate."
    );
    restore();
    if (!confirmed) return;
  }
  await runMutation({
    action: "launch",
    path: "/api/launch",
    body: planBody({ profile: payload.profile }),
    feedback: showControlsFeedback,
    describe: (answer) => {
      if (answer.outcome !== OUTCOME_RESULT) return null;
      const launch = answer.payload && answer.payload.launch;
      const pid = launch && launch.pid;
      const replayed = answer.payload && answer.payload.replayed === true;
      if (replayed) {
        return "this launch was already recorded; nothing was started twice";
      }
      return "launched" + (pid ? " (pid " + pid + ")" : "");
    },
  });
}

export async function handleStopClick() {
  if (!routePlanId()) return;
  showControlsFeedback("", "");
  await runMutation({
    action: "stop",
    path: "/api/control/stop-after-current",
    body: planBody(),
    subject: "stop-after-current",
    feedback: showControlsFeedback,
  });
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
  const restore = captureFocus();
  const confirmed = window.confirm(
    "Abort the in-flight turn? The worker gets " +
      grace +
      "s to wind down, then the harness is killed and the session is left pending."
  );
  restore();
  if (!confirmed) return;
  showControlsFeedback("", "");
  await runMutation({
    action: "abort",
    path: "/api/control/abort",
    body: planBody({ grace_seconds: grace }),
    subject: "the abort",
    feedback: showControlsFeedback,
    describe: (answer) => {
      if (!answer.ok) return null;
      const note = (answer.payload && answer.payload.note) || "applies at the next drain";
      return (
        "abort recorded with " +
        grace +
        "s grace and has not run yet; " +
        note
      );
    },
  });
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
  await runMutation({
    action: "harness",
    path: "/api/control/select-harness",
    body: planBody({ harness: harness }),
    subject: "harness " + harness,
    feedback: showControlsFeedback,
  });
}

export async function handleUnlockClick() {
  if (!routePlanId()) return;
  const restore = captureFocus();
  const confirmed = window.confirm(
    "Force-unlock this plan? Only do this when you are sure no runner is active; mid-run unlock can corrupt state."
  );
  restore();
  if (!confirmed) return;
  showControlsFeedback("", "");
  await runMutation({
    action: "unlock",
    path: "/api/unlock",
    body: planBody({ force: true }),
    subject: "the unlock",
    feedback: showControlsFeedback,
    describe: (answer) => {
      if (answer.outcome !== OUTCOME_RESULT) return null;
      return (
        "unlock: " +
        (answer.payload && answer.payload.removed ? "removed" : "no lock present")
      );
    },
  });
}

export async function handleArchiveClick() {
  // Confirmed by typing the plan's own path, so a mode that addresses
  // plans by digest and never publishes a path has nothing to confirm
  // against and does not offer the archive from here.
  const remote = state.capabilities && state.capabilities.mode === "remote";
  const planDir =
    (state.detailPlan && state.detailPlan.plan_dir) || state.route.planDir;
  if (!routePlanId() || (!remote && !planDir)) return;
  const restore = captureFocus();
  const typed = remote
    ? window.prompt(
        "Archive this plan on the Mac? It can be restored until retention expires. Type ARCHIVE to confirm:\n\n" +
          ((state.detailPlan && state.detailPlan.display_label) || routePlanId()),
        ""
      )
    : window.prompt(
        "Archive this plan? This moves the plan directory, Workbench pin, and ASL state into the archive, where it can be restored until retention expires. Type the full plan path to confirm:\n\n" +
          planDir,
        ""
      );
  restore();
  if (typed === null) return;
  const confirmPlanDir = (typed || "").trim();
  if (!confirmPlanDir) {
    showControlsFeedback("error", "archive confirmation required");
    return;
  }
  if (remote ? confirmPlanDir !== "ARCHIVE" : confirmPlanDir !== planDir) {
    showControlsFeedback(
      "error",
      remote ? "type ARCHIVE to confirm" : "confirmation must match the full plan path"
    );
    return;
  }
  showControlsFeedback("", "");
  const answer = await runMutation({
    action: "archive-plan",
    path: "/api/plan/archive",
    body: planBody(
      remote
        ? { confirm_plan_key: routePlanId() }
        : { confirm_plan_dir: confirmPlanDir }
    ),
    subject: "the archive",
    feedback: showControlsFeedback,
  });
  // Leaving the detail view is only right once the plan is actually gone. A
  // recorded-but-not-run archive leaves the plan where it is, and navigating
  // away from it would be the success claim this whole path exists to avoid.
  if (answer && answer.outcome === OUTCOME_RESULT) {
    state.detailPlan = null;
    state.detailRegistry = null;
    state.detailReplyContext = null;
    clearSelectedPlan();
    navigateToQueue();
    refresh();
  }
}
