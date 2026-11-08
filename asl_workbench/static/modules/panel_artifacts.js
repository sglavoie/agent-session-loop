/* Log and artifact reads for the detail view. */

import { apiRequest } from "./api.js";
import { planReadGuard, planUrl } from "./identity.js";
import { setField } from "./panels.js";

export async function loadAndRenderLog(stream) {
  const el = document.getElementById("wb-detail-" + stream);
  if (!el) return;
  const stale = planReadGuard();
  try {
    const body = await apiRequest(
      planUrl("/api/logs", { stream: stream, tail_lines: 120 })
    );
    if (stale()) return;
    if (body && body.ok && typeof body.text === "string") {
      el.textContent = body.text || "(empty)";
    } else {
      el.textContent = "(no log)";
    }
  } catch (err) {
    if (stale()) return;
    el.textContent = "(no log)";
  }
}

export async function loadAndRenderArtifacts() {
  const stale = planReadGuard();
  try {
    const body = await apiRequest(
      planUrl("/api/artifacts")
    );
    if (stale()) return;
    renderArtifactsPanel(body && body.artifacts ? body.artifacts : {});
  } catch (err) {
    if (stale()) return;
    renderArtifactsPanel({});
  }
}

export function renderArtifactsPanel(artifacts) {
  const list = document.getElementById("wb-detail-artifacts");
  if (!list) return;
  list.innerHTML = "";
  const tmpl = document.getElementById("wb-artifact-row-template");
  const labels = [
    ["state_path", "state.json"],
    ["events_path", "events.jsonl"],
    ["progress_path", "progress.md"],
    ["final_review_path", "final-review.md"],
    ["final_review_artifact_path", "final-review.json"],
    ["artifact_dir", "artifact dir"],
    ["stdout_path", "stdout"],
    ["stderr_path", "stderr"],
    ["last_message_path", "last-message.txt"],
    ["result_path", "result.json"],
  ];
  let added = 0;
  labels.forEach((entry) => {
    const key = entry[0];
    const label = entry[1];
    const descriptor = artifacts[key];
    if (!descriptor || !descriptor.path) return;
    const fragment = tmpl.content.cloneNode(true);
    const li = fragment.querySelector("li");
    setField(li, "label", label);
    setField(li, "path", descriptor.path);
    const meta = descriptor.exists
      ? (descriptor.kind || "file") +
        (typeof descriptor.size === "number"
          ? " · " + descriptor.size + " B"
          : "")
      : "missing";
    setField(li, "meta", meta);
    list.appendChild(li);
    added += 1;
  });
  if (added === 0) {
    const empty = document.createElement("li");
    empty.className = "wb-empty";
    empty.textContent = "no artifacts yet";
    list.appendChild(empty);
  }
}
