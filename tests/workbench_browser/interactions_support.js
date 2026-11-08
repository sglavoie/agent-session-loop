/* The mutating half of the matrix, driven by a real browser.
 *
 * `tests/test_workbench_interactions.py` proves what each deployment will
 * carry out and what it is entitled to claim. None of that is visible from
 * Python: whether the sentence reaches the operator, whether the keyboard
 * comes back after a confirm dialog, whether a recorded-but-unrun operation is
 * still on screen after a reload, and whether any of it fits on a phone are
 * facts about a rendered page. This file is where they are asserted.
 *
 * Like the other specs here, every test runs against both deployments from
 * one body and asks the payload which mode it is looking at rather than
 * branching on a name -- because "one interface, two deployments" is the claim
 * under test, and a test written twice would be evidence against it. It also
 * runs at all three viewports, so the stacked phone shell is exercised by the
 * same assertions as the two-pane one.
 *
 * Two things are deliberately never done here.
 *
 * **Nothing launches a run.** In local mode `/api/launch` spawns a real `asl
 * run` against the fixture corpus, and a browser suite that started a harness
 * would be a browser suite that left one behind. Launch is asserted through
 * its disabled state and its refusal, which is where all of its interface
 * behaviour lives anyway.
 *
 * **Nothing purges.** The destructive path is driven up to the gate -- the
 * expired-entry review -- and the gate is what is asserted. A spec that got
 * past it would be deleting the fixture other specs are reading.
 */

const { test, expect } = require("@playwright/test");
const manifest = require("./scenarios.json");

const DEPLOYMENTS = Object.keys(manifest.deployments).map((name) => ({
  name: name,
  address: manifest.deployments[name].address,
  baseURL: `http://${manifest.host}:${manifest.deployments[name].port}`,
}));

const PLAN_STATES = manifest.plans;
const ARCHIVE = manifest.archive;

/** The key the shell stores operation receipts under. */
const RECEIPTS_KEY = "wb.operations";

function isDesktop(page) {
  const size = page.viewportSize();
  return !!size && size.width >= manifest.desktop_min_width;
}

/**
 * Where the mutating routes are reachable from.
 *
 * The local deployment holds a secret and the remote one trusts the identity
 * header Tailscale Serve stamped (supplied for every request by the config).
 * The token goes in the query string exactly as an operator's bookmark carries
 * it; `prefs.js` moves it into session storage and strips it from the URL.
 */
function authorizedURL(deployment, suffix) {
  const query =
    deployment.address === "plan_dir"
      ? `?token=${encodeURIComponent(manifest.token)}`
      : "";
  return `${deployment.baseURL}/${query}${suffix || ""}`;
}

async function catalog(page) {
  const body = await page.evaluate(async () => {
    const response = await fetch("/api/queue");
    return response.json();
  });
  expect(body.ok).toBe(true);
  return body.plans;
}

function rowFor(plans, name) {
  const wanted = PLAN_STATES[name].primary_state;
  const match = plans.find((plan) => plan.primary_state === wanted);
  expect(match, `no ${wanted} plan in the catalog`).toBeTruthy();
  return match;
}

function detailHash(plan) {
  if (plan.plan_dir) return `#plan?dir=${encodeURIComponent(plan.plan_dir)}`;
  return `#plan?key=${encodeURIComponent(plan.plan_key)}`;
}

/** What this deployment says it can carry out, read through the page. */
async function capabilities(page) {
  const body = await page.evaluate(async () => {
    const response = await fetch("/api/capabilities");
    return response.json();
  });
  expect(body.ok).toBe(true);
  const byOperation = {};
  for (const entry of body.operations) byOperation[entry.operation] = entry;
  return { mode: body.mode, byOperation: byOperation };
}

/** Load the queue at an address that may carry out mutations. */
async function openQueue(page, deployment) {
  await page.goto(authorizedURL(deployment));
  await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
    "data-state",
    "idle",
  );
}

/**
 * Open the archive view and wait for its read to have finished.
 *
 * Settled rather than *succeeded*: on the Pi the archive is not published at
 * all, so the read ends in `error` and the view says so. Waiting for `idle`
 * here would be waiting for one deployment to behave like the other.
 */
async function openArchive(page, deployment) {
  await page.goto(authorizedURL(deployment, "#archive"));
  await expect(page.locator("#wb-archive")).toBeVisible();
  await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
    "data-state",
    /idle|error/,
  );
}

/** Open one plan's detail view, authorized, and wait for the first poll. */
async function openDetail(page, deployment, name) {
  await openQueue(page, deployment);
  const plan = rowFor(await catalog(page), name);
  await page.goto(authorizedURL(deployment, detailHash(plan)));
  await expect(page.locator("#wb-shell")).toHaveAttribute(
    "data-route",
    "detail",
  );
  await expect(page.locator("#wb-detail-state")).toHaveAttribute(
    "data-state",
    PLAN_STATES[name].primary_state,
  );
  // The capability read is fired at startup and not awaited by anything on
  // screen, so the controls are enabled for an instant before it lands. Every
  // assertion below is about what it decided, so wait for it to have decided.
  await page.waitForFunction(() => {
    const button = document.getElementById("wb-control-unlock");
    return !!button && button.hasAttribute("data-operation-action");
  });
  return plan;
}

/** Every receipt this tab has written down, as it would survive a reload. */
async function storedReceipts(page) {
  return page.evaluate((key) => {
    const raw = window.sessionStorage.getItem(key);
    return raw ? JSON.parse(raw) : [];
  }, RECEIPTS_KEY);
}

/** Wait until an operation of this kind has been written down, and return it. */
async function receiptFor(page, action) {
  await expect
    .poll(async () => (await storedReceipts(page)).some((r) => r.action === action))
    .toBe(true);
  const receipts = await storedReceipts(page);
  return receipts.find((receipt) => receipt.action === action);
}

module.exports = {
  test,
  expect,
  manifest,
  DEPLOYMENTS,
  PLAN_STATES,
  ARCHIVE,
  RECEIPTS_KEY,
  isDesktop,
  authorizedURL,
  catalog,
  rowFor,
  detailHash,
  capabilities,
  openQueue,
  openArchive,
  openDetail,
  storedReceipts,
  receiptFor,
};
