/* Where a row is drawn, and what the page says about itself.
 *
 * The server decides which plans a page holds; this decides where each of them
 * goes once it is here. The two are deliberately separate: membership has to
 * be settled before a read limit and so cannot be settled in a browser, while
 * "is this still current work" is a reading of one row against one clock and
 * so should not cost a round trip.
 *
 * The sections are the Companion's, because they are the ones an operator
 * already reads a phone screen with: what is waiting on you, what is moving,
 * what could not be confirmed, what has just happened, and — kept apart rather
 * than mixed in — the run a notification named that has since gone quiet, and
 * everything the eight-hour window has moved out of the way.
 *
 * Inside each section, rows are grouped by project. A plan registered under
 * two projects is found by either (the server matches membership) and is drawn
 * once, under the heading that names both: one plan, one row, one archive
 * button.
 */

import {
  ACTIVE_WINDOW_SECONDS,
  COMPLETENESS_NOTES,
  FRESHNESS_NOTES,
  OBSERVATION_ABSENT,
  OBSERVATION_CURRENT,
  OBSERVATION_LABELS,
  OBSERVATION_UNCONFIRMED,
  OVERVIEW_SECTIONS,
  SECTION_EMPTY_TEXT,
  SECTION_IN_PROGRESS,
  SECTION_LABELS,
  SECTION_NEEDS_ATTENTION,
  SECTION_OLDER,
  SECTION_OLDER_LINKED,
  SECTION_RECENT,
  SECTION_UNCONFIRMED,
  UNGROUPED_PROJECT,
} from "./constants.js";
import { planIdOf } from "./identity.js";
import { toEpochSeconds } from "./time.js";
import { comparePlanRows, upsertRow } from "./queue.js";
import { state } from "./state.js";

/** States that mean a run is moving under its own power right now. */
const ACTIVE_STATES = ["running", "verifying"];

/** Mirrors overview.URGENCY_NEEDS_ACTION. */
const URGENCY_NEEDS_ACTION = "needs-action";

/** The observation vocabulary, in the order a banner would report it. */
const RETAINED_STATUSES = [OBSERVATION_UNCONFIRMED, OBSERVATION_ABSENT];

/**
 * What a row says about the observation it came from.
 *
 * The local mode publishes `current` because it read a filesystem; a payload
 * that says nothing is treated the same way, because the alternative is
 * marking every row of a working deployment as unconfirmed.
 */
export function observationStatusOf(plan) {
  const value = plan && plan.observation_status;
  return typeof value === "string" && value ? value : OBSERVATION_CURRENT;
}

/** True when this row is retained rather than live. */
export function isRetained(plan) {
  return RETAINED_STATUSES.indexOf(observationStatusOf(plan)) !== -1;
}

/**
 * The moment this row last moved, in epoch seconds, or null when it never did.
 *
 * Mirrors overview.activity_at, field for field and in the same order. The
 * fields are read rather than picked by deployment because the two modes fill
 * in different ones: the Mac knows when the plan last moved, and the Pi may
 * only know when it last looked.
 */
export function activityAt(plan) {
  if (!plan) return null;
  const candidates = [
    plan.last_activity_at,
    plan.run_started_at,
    plan.last_observed_at,
  ];
  for (let index = 0; index < candidates.length; index += 1) {
    const epoch = toEpochSeconds(candidates[index]);
    if (epoch !== null) return epoch;
  }
  return null;
}

/* `toEpochSeconds` lives in time.js so every view that measures a moment reads
 * it the same way; re-exported here because this is where it was first named. */
export { toEpochSeconds };

/**
 * Whether a row still counts as current work.
 *
 * Mirrors overview.recently_active. A run that is running or verifying is
 * active by observation rather than by recency, so it is never moved out of
 * the way by the clock; everything else has eight hours from its last movement.
 * A row that has never moved is current too: the window asks "has this gone
 * quiet", and a plan that has never been run has not gone quiet -- it is
 * queued work, and hiding the newest plan on the machine behind a toggle is
 * the opposite of what the window is for.
 */
export function recentlyActive(plan, nowSeconds) {
  if (plan && ACTIVE_STATES.indexOf(plan.primary_state) !== -1) return true;
  const at = activityAt(plan);
  if (at === null) return true;
  return nowSeconds - at <= ACTIVE_WINDOW_SECONDS;
}

/** The section a row belongs to, given the clock and the route. */
export function sectionFor(plan, nowSeconds, linkedRunKey) {
  if (isRetained(plan)) return SECTION_UNCONFIRMED;
  if (ACTIVE_STATES.indexOf(plan && plan.primary_state) !== -1) {
    return SECTION_IN_PROGRESS;
  }
  // Needs-action is not windowed: a run that paused for a reply nine hours ago
  // is still a run waiting for a reply. The window sorts quiet work out of the
  // way, and work that is waiting on a person is not quiet.
  if (plan && plan.urgency_group === URGENCY_NEEDS_ACTION) {
    return SECTION_NEEDS_ATTENTION;
  }
  if (recentlyActive(plan, nowSeconds)) return SECTION_RECENT;
  // Checked after the window rather than before it, so a link to a run that is
  // still current work opens it where it actually is. Only once it has gone
  // quiet does the link get its own heading, which is the whole point: the
  // notification is old news and must not sit at the top of today's queue.
  if (linkedRunKey && plan && plan.run_key === linkedRunKey) {
    return SECTION_OLDER_LINKED;
  }
  return SECTION_OLDER;
}

/** The single heading a row is filed under. Mirrors overview.project_group. */
export function projectGroupOf(plan) {
  if (!plan) return UNGROUPED_PROJECT;
  if (typeof plan.project_group === "string" && plan.project_group.trim()) {
    return plan.project_group.trim();
  }
  if (Array.isArray(plan.projects) && plan.projects.length) {
    return plan.projects.join(" + ");
  }
  if (typeof plan.project === "string" && plan.project.trim()) {
    return plan.project.trim();
  }
  return UNGROUPED_PROJECT;
}

/** Group rows by project heading, preserving the order they arrived in. */
export function groupByProject(rows) {
  const groups = new Map();
  rows.forEach((plan) => {
    const name = projectGroupOf(plan);
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(plan);
  });
  return groups;
}

/**
 * Project headings in the order they are drawn.
 *
 * Newest movement first, falling back to the name: the project something is
 * happening in should not move around as its neighbours are renamed, and two
 * projects with nothing happening in either should still have a stable order.
 */
export function orderProjectGroups(groups) {
  return Array.from(groups.keys()).sort((a, b) => {
    const first = newestActivity(groups.get(a));
    const second = newestActivity(groups.get(b));
    if (first !== second) {
      if (first === null) return 1;
      if (second === null) return -1;
      return second - first;
    }
    return a.localeCompare(b);
  });
}

function newestActivity(rows) {
  let newest = null;
  (rows || []).forEach((plan) => {
    const at = activityAt(plan);
    if (at === null) return;
    if (newest === null || at > newest) newest = at;
  });
  return newest;
}

/**
 * Draw every section into the queue element and return the ids that were kept.
 *
 * Returns the set rather than removing the leftovers itself, because the rows
 * it did not draw are still the queue's to account for: `renderQueue` owns the
 * row cache and this owns the layout.
 */
export function renderSections(queueEl, emptyEl, visible) {
  const now = clockSeconds();
  const linkedRunKey = (state.route && state.route.runKey) || null;
  const buckets = new Map();
  OVERVIEW_SECTIONS.forEach((name) => buckets.set(name, []));
  visible.forEach((plan) => {
    buckets.get(sectionFor(plan, now, linkedRunKey)).push(plan);
  });

  const seen = new Set();
  let previous = emptyEl;
  OVERVIEW_SECTIONS.forEach((name) => {
    const rows = buckets.get(name);
    const section = sectionElement(queueEl, name);
    const hidden = name === SECTION_OLDER && !state.showOlderRuns;
    renderSectionRows(section, rows, hidden, seen);
    section.setAttribute("data-count", String(rows.length));
    section.hidden = rows.length === 0;
    if (previous && previous.nextSibling !== section) {
      queueEl.insertBefore(section, previous.nextSibling);
    }
    previous = section;
  });
  renderOlderToggle(buckets.get(SECTION_OLDER).length);
  return seen;
}

function renderSectionRows(section, rows, hidden, seen) {
  const list = section.querySelector("[data-section-list]");
  const empty = section.querySelector("[data-section-empty]");
  const count = section.querySelector("[data-section-count]");
  if (count) count.textContent = String(rows.length);
  if (empty) empty.hidden = rows.length !== 0 || hidden;
  // The window moved these rows behind a toggle; it did not drop them. Only
  // the list is hidden -- the rows inside it are built either way, so the
  // count above stays truthful, the toggle reveals them without a refetch, and
  // a row that moved *into* a hidden section is actually moved rather than
  // left drawn under the heading it has just stopped belonging to.
  if (list) list.hidden = hidden;
  if (!list || rows.length === 0) return;
  const groups = groupByProject(rows.slice().sort(comparePlanRows));
  let previous = null;
  orderProjectGroups(groups).forEach((project) => {
    const group = groupElement(list, project);
    const cards = group.querySelector("[data-group-rows]");
    let previousRow = null;
    groups.get(project).forEach((plan) => {
      const row = upsertRow(plan);
      seen.add(planIdOf(plan));
      if (row.parentNode !== cards) cards.appendChild(row);
      if (previousRow && previousRow.nextSibling !== row) {
        cards.insertBefore(row, previousRow.nextSibling);
      }
      annotateRow(row, plan);
      previousRow = row;
    });
    if (previous && previous.nextSibling !== group) {
      list.insertBefore(group, previous.nextSibling);
    } else if (!previous && list.firstChild !== group) {
      list.insertBefore(group, list.firstChild);
    }
    previous = group;
  });
  // Groups whose last row moved elsewhere this poll.
  Array.from(list.children).forEach((child) => {
    const rowsIn = child.querySelector("[data-group-rows]");
    if (rowsIn && rowsIn.children.length === 0) list.removeChild(child);
  });
}

/**
 * Mark a row with where its information came from.
 *
 * A retained row is not a live one, and the difference is not cosmetic: it is
 * the difference between "this run is paused" and "this run was paused the
 * last time anything could see it". The attribute is what the stylesheet and
 * the browser tests both read, and the prefix is what an operator reads.
 */
export function annotateRow(row, plan) {
  const status = observationStatusOf(plan);
  row.setAttribute("data-observation-status", status);
  row.setAttribute("data-freshness", freshnessOf(plan));
  const hint = row.querySelector('[data-field="hint"]');
  if (!hint) return;
  const retained = isRetained(plan);
  const prefix = "Last observed: ";
  const text = hint.textContent || "";
  if (retained && text && text.indexOf(prefix) !== 0) {
    hint.textContent = prefix + text;
  } else if (retained && !text) {
    hint.textContent = prefix + (OBSERVATION_LABELS[status] || status);
  }
}

export function freshnessOf(plan) {
  const value = plan && plan.freshness;
  if (typeof value === "string" && value) return value;
  return state.queueFreshness || "live";
}

function clockSeconds() {
  // The server's moment when it sent one, this browser's otherwise. The Pi and
  // the laptop it is watching are not guaranteed to agree, and the window is
  // about the observation rather than about the screen.
  if (typeof state.queueObservedAt === "number") return state.queueObservedAt;
  return Date.now() / 1000;
}

function sectionElement(queueEl, name) {
  const id = "wb-section-" + name;
  let section = document.getElementById(id);
  if (section) return section;
  const template = document.getElementById("wb-section-template");
  section = template.content.cloneNode(true).querySelector(".wb-section");
  section.id = id;
  section.setAttribute("data-section", name);
  const heading = section.querySelector("[data-section-heading]");
  if (heading) heading.textContent = SECTION_LABELS[name] || name;
  const empty = section.querySelector("[data-section-empty]");
  if (empty) empty.textContent = SECTION_EMPTY_TEXT[name] || "nothing here";
  queueEl.appendChild(section);
  return section;
}

function groupElement(list, project) {
  const existing = Array.from(list.children).find(
    (child) => child.getAttribute("data-project") === project
  );
  if (existing) return existing;
  const template = document.getElementById("wb-project-group-template");
  const group = template.content
    .cloneNode(true)
    .querySelector(".wb-project-group");
  group.setAttribute("data-project", project);
  const heading = group.querySelector("[data-group-heading]");
  if (heading) heading.textContent = project;
  list.appendChild(group);
  return group;
}

function renderOlderToggle(count) {
  const toggle = document.getElementById("wb-older-toggle");
  if (!toggle) return;
  // Nothing behind it is nothing to offer: a control that reveals an empty
  // list reads as "there is more here" and there is not.
  toggle.hidden = count === 0;
  toggle.setAttribute("data-count", String(count));
  toggle.setAttribute("aria-pressed", state.showOlderRuns ? "true" : "false");
  toggle.textContent = state.showOlderRuns
    ? "hide older (" + count + ")"
    : "show older (" + count + ")";
}

/**
 * Offer the facets the catalog actually holds.
 *
 * A remembered selection the catalog no longer holds is kept and offered
 * anyway: a plan can age out of the window under a project filter, and a
 * select that quietly reset itself would widen the view without being asked
 * and show the operator work they had filtered away.
 */
export function renderFacets(facets) {
  renderFacetSelect(
    "wb-project-filter",
    (facets && facets.projects) || [],
    state.projectFilter,
    "all projects"
  );
  renderFacetSelect(
    "wb-harness-filter",
    (facets && facets.harnesses) || [],
    state.harnessFilter,
    "all harnesses"
  );
}

function renderFacetSelect(id, values, selected, allLabel) {
  const select = document.getElementById(id);
  if (!select) return;
  const options = values.slice();
  if (selected && options.indexOf(selected) === -1) options.push(selected);
  const wanted = [""].concat(options);
  const current = Array.from(select.options).map((option) => option.value);
  if (current.length !== wanted.length ||
      wanted.some((value, index) => current[index] !== value)) {
    select.textContent = "";
    wanted.forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value || allLabel;
      select.appendChild(option);
    });
  }
  select.value = selected || "";
}

/**
 * Say what the page could not see, before an operator acts on what it shows.
 *
 * Three different silences, never collapsed into one: the connector may have
 * stopped reporting (freshness), the observation may have covered only part of
 * the catalog (completeness), and the deployment may have failed to deliver an
 * alert it raised (incidents). Each of them changes what "nothing is waiting
 * on you" means, and none of them can be inferred from the rows.
 */
export function renderObservationBanners(payload) {
  const el = document.getElementById("wb-observation");
  if (!el) return;
  const notes = [];
  const freshness = typeof payload.freshness === "string" ? payload.freshness : null;
  if (freshness && FRESHNESS_NOTES[freshness]) {
    notes.push({ kind: "freshness-" + freshness, text: FRESHNESS_NOTES[freshness] });
  }
  const completeness =
    typeof payload.catalog_completeness === "string"
      ? payload.catalog_completeness
      : null;
  if (completeness && COMPLETENESS_NOTES[completeness]) {
    notes.push({
      kind: "completeness-" + completeness,
      text: COMPLETENESS_NOTES[completeness],
    });
  }
  describeConnectors(payload).forEach((text) =>
    notes.push({ kind: "connector", text: text })
  );
  describeIncidents(payload).forEach((text) =>
    notes.push({ kind: "incident", text: text })
  );
  el.textContent = "";
  el.hidden = notes.length === 0;
  el.setAttribute("data-count", String(notes.length));
  notes.forEach((note) => {
    const item = document.createElement("p");
    item.className = "wb-observation__note";
    item.setAttribute("data-kind", note.kind);
    item.textContent = note.text;
    el.appendChild(item);
  });
}

function describeConnectors(payload) {
  const connectors = Array.isArray(payload.connectors) ? payload.connectors : [];
  const notes = [];
  connectors.forEach((connector) => {
    if (!connector || typeof connector !== "object") return;
    const label = connector.freshness;
    if (!label || label === "live") return;
    const name = connector.connector_id || "a connector";
    const why = connector.degraded_reason;
    notes.push(name + ": " + label + (why ? " — " + why : ""));
  });
  return notes;
}

/**
 * Alerts the deployment raised and can no longer account for.
 *
 * Only `unconfirmed` qualifies, and the delivery state is the whole test. An
 * incident that is `pending`, `retry` or `publishing` is still on its way --
 * the sentence below would be false about all three, and saying it anyway
 * would turn every raised alert into a delivery failure the moment it was
 * raised. `published` arrived and `cancelled` was withdrawn. That leaves the
 * one state the vocabulary keeps for "nobody can establish whether this
 * landed, and no path will send it again", which is the only case an operator
 * has to hear about before trusting a quiet screen.
 */
function describeIncidents(payload) {
  const incidents = Array.isArray(payload.incidents) ? payload.incidents : [];
  const undelivered = incidents.filter(
    (incident) =>
      incident &&
      incident.resolved_at === null &&
      incident.delivery_state === "unconfirmed"
  );
  if (undelivered.length === 0) return [];
  const named = undelivered
    .slice(0, 5)
    .map((incident) => incident.project || incident.kind || "an alert")
    .join("; ");
  return [
    "The deployment could not confirm " +
      (undelivered.length === 1 ? "an alert" : undelivered.length + " alerts") +
      " reached their destination and will not resend automatically: " +
      named,
  ];
}
