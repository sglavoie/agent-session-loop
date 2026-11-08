/* The overview: sections, grouping, facets, the window, and history.
 *
 * `shell.spec.js` proves one front end reaches two deployments. This proves
 * the thing that front end now *is*: the Companion's cockpit and the
 * Workbench's queue folded into one screen, drawn the same way whichever
 * machine answered.
 *
 * Every test runs twice, once per deployment, from one body -- for the same
 * reason the shell suite does. Where the two genuinely differ (a path versus a
 * digest, a per-row observation mark versus one for the whole answer) the test
 * asks the payload rather than branching on a deployment name, because a
 * branch here would be a branch in the claim.
 *
 * The corpus is `tests/workbench_support.py`, shared with the Python suites:
 * `alpha` is paused waiting for an operator, `bravo` has never run, and
 * `charlie` finished thirty hours ago with two endings behind it. `charlie` is
 * the one that makes the eight-hour window and the history pane observable at
 * all, and `scenarios.json` records which section each is supposed to land in
 * so these tests assert the fixture's intent rather than a second copy of the
 * rule they are testing.
 */

const { test, expect } = require("@playwright/test");
const manifest = require("./scenarios.json");

const DEPLOYMENTS = Object.keys(manifest.deployments).map((name) => ({
  name: name,
  address: manifest.deployments[name].address,
  baseURL: `http://${manifest.host}:${manifest.deployments[name].port}`,
}));

const PLAN_STATES = manifest.plans;
const PLAN_NAMES = Object.keys(PLAN_STATES);

/** Which section the fixture says a plan belongs in. */
function sectionOf(name) {
  const section = PLAN_STATES[name].section;
  expect(section, `scenarios.json names no section for ${name}`).toBeTruthy();
  return section;
}

/** Whether the fixture expects that section to start behind a toggle. */
function sectionHidden(name) {
  return manifest.sections.hidden.indexOf(sectionOf(name)) !== -1;
}

/** The fixture's seeded endings, newest first -- the order the pane renders. */
const HISTORY_OUTCOMES = manifest.history.outcomes.slice().reverse();

/**
 * Load the queue and wait for the first poll to have rendered.
 *
 * The row count is the wait, not an assertion in passing: `data-state=idle` is
 * true before the first answer arrives as well as after it, so a spec that
 * stopped there would read a page that had not drawn yet. A hash carrying a
 * filter draws fewer rows, hence the count is a parameter rather than the
 * roster's length -- passing the wrong one here would hang the wait instead of
 * failing a claim, so callers name what they expect to see.
 */
async function openQueue(page, deployment, hash, expectedRows) {
  await page.goto(`${deployment.baseURL}/${hash || ""}`);
  await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
    "data-state",
    "idle",
  );
  await expect(page.locator(".wb-row")).toHaveCount(
    expectedRows === undefined ? PLAN_NAMES.length : expectedRows,
  );
}

/** What this deployment published, read through the browser's own session. */
async function catalog(page, query) {
  const body = await page.evaluate(async (suffix) => {
    const response = await fetch("/api/queue" + (suffix || ""));
    return response.json();
  }, query);
  expect(body.ok).toBe(true);
  return body;
}

/**
 * The published row for a named plan.
 *
 * By state rather than by label, exactly as `shell.spec.js` picks one: the
 * label is the field the two providers word differently, so choosing by it
 * would quietly test the reconciliation instead of using it.
 */
function rowFor(plans, name) {
  const wanted = PLAN_STATES[name].primary_state;
  const match = plans.find((plan) => plan.primary_state === wanted);
  expect(match, `no ${wanted} plan in the catalog`).toBeTruthy();
  return match;
}

/** The identifier the rendered rows are keyed by, in either deployment. */
function addressOf(plan) {
  return plan.plan_dir || plan.plan_key;
}

/** Which section a plan's row was actually drawn into. */
async function renderedSection(page, plan) {
  return page.evaluate((address) => {
    const row = document.querySelector(`.wb-row[data-plan-dir="${address}"]`);
    if (!row) return null;
    const section = row.closest(".wb-section");
    return section ? section.getAttribute("data-section") : null;
  }, addressOf(plan));
}

/** Which project heading a plan's row was drawn under. */
async function renderedProject(page, plan) {
  return page.evaluate((address) => {
    const row = document.querySelector(`.wb-row[data-plan-dir="${address}"]`);
    if (!row) return null;
    const group = row.closest(".wb-project-group");
    return group ? group.getAttribute("data-project") : null;
  }, addressOf(plan));
}

/**
 * A filter parameter as it now stands in the address bar.
 *
 * Read back through `URLSearchParams` rather than matched as a regex, because
 * the claim is that the operator's choice is in the link, not that the shell
 * picked one of the two legal spellings of a space. `router.js` builds the hash
 * with a `URLSearchParams`, which writes `+`; a spec pinned to `%20` would fail
 * on a correct URL, and one pinned to `+` would pin an encoder's internals.
 */
function hashParam(page, name) {
  return expect.poll(() =>
    new URLSearchParams(new URL(page.url()).hash.slice(1)).get(name),
  );
}

/**
 * Every delivery state an incident row can be in.
 *
 * Copied from `asl_workbench/incidents.py` rather than read from the fixture,
 * because the claim below is about the states this front end must *not* treat
 * as a delivery failure -- a spec that enumerated only the ones the corpus
 * happens to produce would assert nothing about the other four.
 */
const DELIVERY_STATES = [
  "pending",
  "retry",
  "publishing",
  "published",
  "unconfirmed",
  "cancelled",
];

const ACCOUNTED_FOR = DELIVERY_STATES.filter((name) => name !== "unconfirmed");

/**
 * One incident row as `/api/queue` publishes them.
 *
 * The fields are the diagnostics allowlist's: identities, states and
 * timestamps, never words from a run. The project doubles as this row's name in
 * the assertions, because the project is what the banner names.
 */
function incidentRow(project, overrides) {
  return Object.assign(
    {
      incident_id: project,
      run_key: "0000000000000000",
      project: project,
      kind: "unanswered-question",
      detected_at: 1,
      resolved_at: null,
      delivery_state: "unconfirmed",
      attempts: 0,
      receipt_id: null,
      published_at: null,
      detail_code: null,
    },
    overrides || {},
  );
}

/**
 * Put an incident list of the test's choosing into this deployment's answers.
 *
 * The deployment's own answer is fetched and only `incidents` is replaced, so
 * freshness, completeness and the connectors stay whatever this machine really
 * reported and the banner region is still assembling all four kinds of note.
 * One route reading a mutable cell rather than a route per payload, because the
 * claim needs one page to see several lists in turn.
 */
async function interceptIncidents(page) {
  const feed = { list: [] };
  await page.route("**/api/queue*", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.incidents = feed.list;
    await route.fulfill({ response: response, json: body });
  });
  return feed;
}

/**
 * Publish a list and run the poll that picks it up, awaited.
 *
 * The 5s timer is stopped and the refresh driven directly so each assertion
 * reads a page that has finished drawing: waiting for the timer instead would
 * make "the banner is absent" true of the previous answer as well as of this
 * one, which is the one way this test could pass while being wrong.
 */
async function publishIncidents(page, feed, list) {
  feed.list = list;
  await page.evaluate(async () => {
    const queue = await import("/static/modules/queue.js");
    const shared = await import("/static/modules/state.js");
    queue.stopQueueTimer();
    while (shared.state.fetchInFlight) {
      await new Promise((resolve) => window.setTimeout(resolve, 25));
    }
    await queue.refresh();
    queue.stopQueueTimer();
  });
}

/** Open the history pane and wait for its first answer. */
async function openHistory(page) {
  await page.locator("#wb-history-toggle").click();
  await expect(page.locator("#wb-history")).toHaveAttribute("data-open", "true");
  await expect(page.locator(".wb-history-row")).toHaveCount(
    HISTORY_OUTCOMES.length,
  );
}

module.exports = {
  test,
  expect,
  manifest,
  DEPLOYMENTS,
  PLAN_STATES,
  PLAN_NAMES,
  sectionOf,
  sectionHidden,
  HISTORY_OUTCOMES,
  openQueue,
  catalog,
  rowFor,
  addressOf,
  renderedSection,
  renderedProject,
  hashParam,
  DELIVERY_STATES,
  ACCOUNTED_FOR,
  incidentRow,
  interceptIncidents,
  publishIncidents,
  openHistory,
};
