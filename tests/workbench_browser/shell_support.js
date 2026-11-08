/* One shell, two deployments, three viewports.
 *
 * Every test in this file runs twice, once against each deployment, from the
 * same body -- because the claim being tested is that there is one front end
 * rather than two that resemble each other. A test that had to be written
 * differently for the Pi would be evidence against the thing it is checking,
 * so where the deployments genuinely differ (a path versus a digest) the test
 * asks the payload which one it is looking at instead of branching on a name.
 *
 * The viewport is handled the same way. `desktop-chromium`, `desktop-webkit`
 * and `mobile-webkit` all run every test, and the tests ask how wide the
 * window is rather than being split by project, so the stacked shell and the
 * two-pane shell are both asserted on every run.
 *
 * The plan corpus underneath comes from `tests/workbench_support.py`, shared
 * with `tests/test_workbench_shell.py`. `alpha` is paused waiting for an
 * operator and has material in every pane; `bravo` has never run, and is here
 * so "the pane is empty" is exercised as a rendered sentence rather than as a
 * blank region.
 */

const { test, expect } = require("@playwright/test");
const manifest = require("./scenarios.json");

const DEPLOYMENTS = Object.keys(manifest.deployments).map((name) => ({
  name: name,
  address: manifest.deployments[name].address,
  baseURL: `http://${manifest.host}:${manifest.deployments[name].port}`,
}));

const PLAN_STATES = manifest.plans;

// Every pane the detail view assembles, with the sentence it renders when the
// plan has nothing to put in it. A pane that shows neither material nor its
// own empty sentence is a pane that failed silently, which on a phone over
// Tailscale is indistinguishable from a plan that has nothing to say.
const DETAIL_PANES = [
  { id: "wb-detail-events", empty: "no events yet" },
  { id: "wb-detail-progress", empty: "no entries" },
  { id: "wb-detail-artifacts", empty: "no artifacts yet" },
  { id: "wb-detail-verification", empty: "no active verification" },
  { id: "wb-detail-sessions", empty: null },
  { id: "wb-detail-stdout", empty: "(no log)" },
  { id: "wb-detail-stderr", empty: "(no log)" },
];

function isDesktop(page) {
  const size = page.viewportSize();
  return !!size && size.width >= manifest.desktop_min_width;
}

// -- A poll that must not move the page -------------------------------------
//
// The fixture corpus is three plans, which fits on every viewport with room to
// spare, so the scroll tests below synthesise a catalog long enough to scroll
// and a set of banner notes above it whose appearance and disappearance is what
// changes the page's height. Both are injected into this deployment's own
// answer rather than seeded into the corpus: the claim is about what the browser
// does with a redraw, and neither a 60-plan corpus nor a deliberately broken
// connector belongs in a fixture the other suites share.

/** How many rows the catalog is padded to, so the page scrolls at all. */
const TALL_CATALOG = 60;

/** How many degraded connectors are reported, so the banner region has height. */
const DEGRADED_CONNECTORS = 8;

/** A padded catalog: the fixture's rows, then clones of its busiest plan. */
function padPlans(plans, total) {
  const wanted = PLAN_STATES.alpha.primary_state;
  const source = plans.find((plan) => plan.primary_state === wanted) || plans[0];
  expect(source, "the fixture published no plan to pad the catalog with").toBeTruthy();
  const padded = plans.slice();
  while (padded.length < total) {
    const clone = Object.assign({}, source);
    const suffix = String(padded.length).padStart(4, "0");
    // Distinct in every field the shell keys a row by, in both deployments:
    // clones sharing an identity would be reconciled back down to one row.
    if (typeof clone.plan_dir === "string") {
      clone.plan_dir = `${clone.plan_dir}-${suffix}`;
    }
    if (typeof clone.plan_key === "string") {
      clone.plan_key = `${clone.plan_key.slice(0, -4)}${suffix}`;
    }
    if (typeof clone.run_key === "string") {
      clone.run_key = `${clone.run_key.slice(0, -4)}${suffix}`;
    }
    if (typeof clone.label === "string") clone.label = `${clone.label}-${suffix}`;
    padded.push(clone);
  }
  return padded;
}

/** Connectors that have stopped being live, one banner note each. */
function degradedConnectors(count) {
  const connectors = [];
  for (let index = 0; index < count; index += 1) {
    connectors.push({
      connector_id: `pi-${index}`,
      freshness: "stale",
      degraded_reason: "no observation since the link dropped",
    });
  }
  return connectors;
}

/**
 * Serve this deployment's catalog with a length and a banner count of our own.
 *
 * `counts` is dropped rather than rewritten: it is the server's tally over the
 * real catalog, and leaving a tally that disagrees with the rows would have the
 * chips describing a different page than the list. Dropped, the shell computes
 * them from what it was handed, which is what it already does for a deployment
 * that sends none.
 */
async function interceptCatalog(page) {
  const feed = { rows: TALL_CATALOG, connectors: DEGRADED_CONNECTORS };
  await page.route("**/api/queue*", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.plans = padPlans(body.plans, feed.rows);
    body.connectors = degradedConnectors(feed.connectors);
    delete body.counts;
    body.catalog_total = body.plans.length;
    body.catalog_matched = body.plans.length;
    await route.fulfill({ response: response, json: body });
  });
  return feed;
}

/**
 * Record every programmatic page scroll the shell performs.
 *
 * The call is the claim, not the resulting offset: restoring a scroll to the
 * place it is already at leaves the page looking identical, so a test that only
 * compared offsets could not tell the conditional restore from the
 * unconditional one it replaced. What the phone objects to is the call.
 */
async function recordPageScrolls(page) {
  await page.addInitScript(() => {
    window.__wbScrollCalls = [];
    const original = window.scrollTo.bind(window);
    window.scrollTo = function (...args) {
      window.__wbScrollCalls.push(
        args.length === 1 ? args[0] : { x: args[0], y: args[1] },
      );
      return original(...args);
    };
  });
}

/** Where the operator's place is, how far it can go, and what moved it. */
async function placeState(page) {
  return page.evaluate(() => {
    const queue = document.getElementById("wb-queue");
    const root = document.documentElement;
    return {
      window: window.scrollY,
      maxWindow: Math.max(0, root.scrollHeight - window.innerHeight),
      height: root.scrollHeight,
      queue: queue ? queue.scrollTop : 0,
      moves: window.__wbScrollCalls.slice(),
    };
  });
}

/** Put the operator at the bottom of the page, then forget we did it. */
async function scrollToBottom(page) {
  await page.evaluate(() => {
    const root = document.documentElement;
    window.scrollTo(0, Math.max(0, root.scrollHeight - window.innerHeight));
    const queue = document.getElementById("wb-queue");
    if (queue) queue.scrollTop = queue.scrollHeight;
  });
  await page.evaluate(() => {
    window.__wbScrollCalls.length = 0;
  });
  return placeState(page);
}

/**
 * Run one poll and wait for it to have finished drawing.
 *
 * Driven rather than waited for, and driven from the module rather than from
 * `#wb-refresh-now`: the button is at the top of the page, so clicking it while
 * scrolled to the bottom would have Playwright scroll it into view and destroy
 * the very thing under test. The 5s timer is stopped for the same reason a
 * settled page matters here -- an extra poll landing mid-assertion would decide
 * the outcome.
 */
async function pollNow(page) {
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

/** Open the padded queue and wait for all of it to have rendered. */
async function openTallQueue(page, deployment) {
  await page.goto(`${deployment.baseURL}/`);
  await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
    "data-state",
    "idle",
  );
  await expect(page.locator(".wb-row")).toHaveCount(TALL_CATALOG);
}

/** Load the queue and wait for the first poll to have rendered. */
async function openQueue(page, deployment) {
  await page.goto(`${deployment.baseURL}/`);
  await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
    "data-state",
    "idle",
  );
  await expect(page.locator(".wb-row")).toHaveCount(
    Object.keys(PLAN_STATES).length,
  );
}

/** What this deployment published, read through the browser's own session. */
async function catalog(page) {
  const body = await page.evaluate(async () => {
    const response = await fetch("/api/queue");
    return response.json();
  });
  expect(body.ok).toBe(true);
  return body.plans;
}

/**
 * The published row for a named plan.
 *
 * Named by state rather than by label, because the label is the one thing the
 * two providers word differently and picking a row by it would quietly test
 * the reconciliation rather than use it.
 */
function rowFor(plans, name) {
  const wanted = PLAN_STATES[name].primary_state;
  const match = plans.find((plan) => plan.primary_state === wanted);
  expect(match, `no ${wanted} plan in the catalog`).toBeTruthy();
  return match;
}

/** The address this deployment can resolve, as `identity.js` picks it. */
function addressOf(plan) {
  if (plan.plan_dir) return { param: "dir", value: plan.plan_dir };
  return { param: "key", value: plan.plan_key };
}

function detailHash(plan) {
  const address = addressOf(plan);
  return `#plan?${address.param}=${encodeURIComponent(address.value)}`;
}

/**
 * The three header fields, read in one pass.
 *
 * One `evaluate` rather than three `textContent` calls, because a `#run=`
 * link resolves between polls: reading the fields one at a time can catch the
 * label from before the catalog named the plan and the state from after.
 */
async function detailHeader(page) {
  return page.evaluate(() => {
    const text = (id) =>
      (document.getElementById(id).textContent || "").trim();
    return {
      label: text("wb-detail-label"),
      path: text("wb-detail-path"),
      state: text("wb-detail-state"),
    };
  });
}

/** Open a plan by deep link and wait for the detail view to be on it. */
async function openDetail(page, deployment, plan, extra) {
  await page.goto(`${deployment.baseURL}/${detailHash(plan)}${extra || ""}`);
  await expect(page.locator("#wb-shell")).toHaveAttribute(
    "data-route",
    "detail",
  );
  await expect(page.locator("#wb-detail")).toBeVisible();
  // A hash-only change keeps the document, so the refresh status can still
  // read idle from the previous plan while the header shows the placeholder
  // the route change put there; wait for this plan's read to have landed.
  await expect(page.locator("#wb-detail-label")).not.toHaveText("(loading)");
  await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
    "data-state",
    "idle",
  );
}

module.exports = {
  test,
  expect,
  manifest,
  DEPLOYMENTS,
  PLAN_STATES,
  DETAIL_PANES,
  isDesktop,
  TALL_CATALOG,
  DEGRADED_CONNECTORS,
  padPlans,
  degradedConnectors,
  interceptCatalog,
  recordPageScrolls,
  placeState,
  scrollToBottom,
  pollNow,
  openTallQueue,
  openQueue,
  catalog,
  rowFor,
  addressOf,
  detailHash,
  detailHeader,
  openDetail,
};
