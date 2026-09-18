/* Profile form: the launch profile, and the registry pin that holds it.
 *
 * Tri-state fields serialize to true, false, or null, and the blank option is
 * null rather than false -- a profile nobody has answered must not launch a
 * run with a flag nobody asked for.
 */

import { apiRequest, describeError, submit } from "./api.js";
import { COMMIT_MODES, HARNESS_NAMES } from "./constants.js";
import { showControlsFeedback } from "./controls.js";
import { renderDetailHeader } from "./detail.js";
import { planBody, routePlanId } from "./identity.js";
import { findPin } from "./panels.js";
import { state } from "./state.js";

export function renderProfileForm(plan, registry) {
  const form = document.getElementById("wb-profile-form");
  if (!form || !plan) return;
  const pin = findPin(plan.plan_dir);
  const profile = (pin && pin.profile) || plan.profile || {};
  setInputValue(form, "label", pin ? pin.label || "" : plan.label || "");
  setInputValue(form, "cwd", plan.workspace_root || profile.cwd || plan.cwd || "");
  setInputValue(form, "harness", profile.harness || "");
  setInputValue(form, "harness_model", profile.harness_model || "");
  setInputValue(form, "effort", profile.effort || "");
  setInputValue(
    form,
    "timeout",
    profile.timeout === null || profile.timeout === undefined
      ? ""
      : String(profile.timeout)
  );
  setInputValue(form, "commit_mode", profile.commit_mode || "");
  setInputValue(form, "review_harness", profile.review_harness || "");
  setInputValue(
    form,
    "review_harness_model",
    profile.review_harness_model || ""
  );
  setInputValue(
    form,
    "max_review_fix_cycles",
    profile.max_review_fix_cycles === null ||
    profile.max_review_fix_cycles === undefined
      ? ""
      : String(profile.max_review_fix_cycles)
  );
  setTriStateValue(form, "unsafe_bypass", profile.unsafe_bypass);
  setTriStateValue(
    form,
    "allow_not_loop_ready",
    profile.allow_not_loop_ready
  );
  setInputValue(form, "note", profile.note || "");
  clearFormErrors("wb-profile-form-errors");
}

export function setInputValue(form, name, value) {
  const el = form.elements.namedItem(name);
  if (!el) return;
  el.value = value;
}

export function setTriStateValue(form, name, value) {
  const el = form.elements.namedItem(name);
  if (!el) return;
  if (value === true) {
    el.value = "true";
  } else if (value === false) {
    el.value = "false";
  } else {
    el.value = "";
  }
}

export function readTriStateValue(form, name) {
  const el = form.elements.namedItem(name);
  if (!el) return null;
  const raw = el.value;
  if (raw === "true") return true;
  if (raw === "false") return false;
  return null;
}

export function clearFormErrors(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.hidden = true;
  el.textContent = "";
}

export function showFormError(id, message) {
  const el = document.getElementById(id);
  if (!el) return;
  el.hidden = false;
  el.textContent = message;
}

export function readProfileFormPayload() {
  const form = document.getElementById("wb-profile-form");
  if (!form) return { label: null, profile: {} };
  const label = (form.elements.namedItem("label").value || "").trim();
  const profile = {
    cwd: (form.elements.namedItem("cwd").value || "").trim() || null,
    harness:
      (form.elements.namedItem("harness").value || "").trim() || null,
    harness_model:
      (form.elements.namedItem("harness_model").value || "").trim() || null,
    effort: (form.elements.namedItem("effort").value || "").trim() || null,
    commit_mode:
      (form.elements.namedItem("commit_mode").value || "").trim() || null,
    review_harness:
      (form.elements.namedItem("review_harness").value || "").trim() || null,
    review_harness_model:
      (form.elements.namedItem("review_harness_model").value || "").trim() ||
      null,
    unsafe_bypass: readTriStateValue(form, "unsafe_bypass"),
    allow_not_loop_ready: readTriStateValue(form, "allow_not_loop_ready"),
    note: (form.elements.namedItem("note").value || "").trim() || null,
  };
  const rawTimeout = (form.elements.namedItem("timeout").value || "").trim();
  if (rawTimeout) {
    const parsed = parseInt(rawTimeout, 10);
    profile.timeout = Number.isFinite(parsed) ? parsed : null;
  } else {
    profile.timeout = null;
  }
  const rawReviewFixCycles = (
    form.elements.namedItem("max_review_fix_cycles").value || ""
  ).trim();
  if (rawReviewFixCycles) {
    const parsed = parseInt(rawReviewFixCycles, 10);
    profile.max_review_fix_cycles = Number.isFinite(parsed) ? parsed : null;
  } else {
    profile.max_review_fix_cycles = null;
  }
  return { label: label || null, profile: profile };
}

export function validateProfilePayload(payload) {
  const errors = [];
  if (
    payload.profile.max_review_fix_cycles !== null &&
    payload.profile.max_review_fix_cycles !== undefined &&
    payload.profile.max_review_fix_cycles < 0
  ) {
    errors.push("max review-fix cycles must be zero or greater");
  }
  return errors;
}

export function validateLaunchPayload(payload) {
  const errors = validateProfilePayload(payload);
  if (!payload.profile.cwd) {
    errors.push("repository or workspace root is required to launch");
  }
  if (!payload.profile.harness) {
    errors.push("harness is required to launch");
  } else if (HARNESS_NAMES.indexOf(payload.profile.harness) === -1) {
    errors.push("harness must be one of " + HARNESS_NAMES.join(", "));
  }
  if (
    payload.profile.review_harness &&
    HARNESS_NAMES.indexOf(payload.profile.review_harness) === -1
  ) {
    errors.push("review harness must be one of " + HARNESS_NAMES.join(", "));
  }
  if (
    payload.profile.review_harness_model &&
    !payload.profile.review_harness
  ) {
    errors.push("review model requires a review harness");
  }
  if (
    payload.profile.commit_mode &&
    COMMIT_MODES.indexOf(payload.profile.commit_mode) === -1
  ) {
    errors.push("commit_mode must be one of " + COMMIT_MODES.join(", "));
  }
  if (
    payload.profile.timeout !== null &&
    payload.profile.timeout !== undefined &&
    payload.profile.timeout <= 0
  ) {
    errors.push("timeout must be positive");
  }
  return errors;
}

export function wireProfileForm() {
  const form = document.getElementById("wb-profile-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearFormErrors("wb-profile-form-errors");
    if (!routePlanId()) return;
    const payload = readProfileFormPayload();
    const errors = validateProfilePayload(payload);
    if (errors.length > 0) {
      showFormError("wb-profile-form-errors", errors.join("; "));
      return;
    }
    try {
      const body = await apiRequest("/api/registry/pin", {
        method: "POST",
        body: planBody({
          label: payload.label,
          profile: payload.profile,
        }),
      });
      if (body && body.ok) {
        if (body.registry) state.detailRegistry = body.registry;
        showControlsFeedback("info", "profile saved");
        renderDetailHeader(state.detailPlan);
      } else {
        showFormError(
          "wb-profile-form-errors",
          (body && body.error) || "failed to save profile"
        );
      }
    } catch (err) {
      showFormError(
        "wb-profile-form-errors",
        "save failed: " + describeError(err)
      );
    }
  });

  const unpinButton = document.getElementById("wb-profile-unpin");
  if (unpinButton) {
    unpinButton.addEventListener("click", async () => {
      if (!routePlanId()) return;
      try {
        const body = await apiRequest("/api/registry/unpin", {
          method: "POST",
          body: planBody(),
        });
        if (body && body.ok) {
          if (body.registry) state.detailRegistry = body.registry;
          showControlsFeedback("info", "pin removed");
          renderDetailHeader(state.detailPlan);
        } else {
          showControlsFeedback(
            "error",
            (body && body.error) || "failed to unpin"
          );
        }
      } catch (err) {
        showControlsFeedback("error", describeError(err));
      }
    });
  }
}
