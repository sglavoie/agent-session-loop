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
  await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
    "data-state",
    "idle",
  );
}

for (const deployment of DEPLOYMENTS) {
  test.describe(`${deployment.name} deployment`, () => {
    test("the queue lists exactly the plans this deployment published", async ({
      page,
    }) => {
      await openQueue(page, deployment);
      const plans = await catalog(page);
      const published = plans
        .map((plan) => plan.plan_dir || plan.plan_key)
        .sort();
      const rendered = (
        await page
          .locator(".wb-row")
          .evaluateAll((rows) =>
            rows.map((row) => row.getAttribute("data-plan-dir")),
          )
      ).sort();
      expect(rendered).toEqual(published);

      // The addressing the rows were keyed by is the addressing this
      // deployment has, not whichever one happened to be implemented first.
      for (const value of rendered) {
        expect(typeof value).toBe("string");
        expect(value.length).toBeGreaterThan(0);
      }
      const states = await page
        .locator('.wb-row [data-field="state"]')
        .allTextContents();
      expect(states.map((text) => text.trim()).sort()).toEqual(
        Object.keys(PLAN_STATES)
          .map((name) => PLAN_STATES[name].primary_state)
          .sort(),
      );
    });

    test("every row carries a name and a way back to its plan", async ({
      page,
    }) => {
      await openQueue(page, deployment);
      const labels = page.locator('.wb-row [data-field="label"]');
      const count = await labels.count();
      for (let index = 0; index < count; index += 1) {
        const label = labels.nth(index);
        const text = (await label.textContent()).trim();
        // "(unknown plan)" is the fallback for a plan with neither a name nor
        // a path. The Pi has no paths, so a row reaching that fallback means
        // the shell read the laptop's field name and found nothing.
        expect(text).not.toBe("");
        expect(text).not.toBe("(unknown plan)");
        const href = await label.getAttribute("href");
        expect(href).toMatch(/^#plan\?(dir|key)=/);
      }
    });

    test("a row opens its plan in the shape the viewport calls for", async ({
      page,
    }) => {
      await openQueue(page, deployment);
      const plans = await catalog(page);
      const alpha = rowFor(plans, "alpha");
      const address = addressOf(alpha);

      await page
        .locator(`.wb-row[data-plan-dir="${address.value}"]`)
        .locator('[data-field="label"]')
        .click();

      const shell = page.locator("#wb-shell");
      await expect(shell).toHaveAttribute("data-route", "detail");
      await expect(page.locator("#wb-detail")).toBeVisible();

      if (isDesktop(page)) {
        // Two panes: the queue stays alongside the plan so the operator can
        // see the rest of the work while reading one item.
        await expect(shell).toHaveAttribute("data-viewport", "desktop");
        await expect(page.locator("#wb-queue")).toBeVisible();
        await expect(page.locator("#wb-filters")).toBeVisible();
      } else {
        // Stacked: one column, the queue replaced rather than shrunk, and a
        // back button as the only way out of it.
        await expect(shell).toHaveAttribute("data-viewport", "mobile");
        await expect(page.locator("#wb-queue")).toBeHidden();
        await expect(page.locator("#wb-filters")).toBeHidden();
        await expect(page.locator("#wb-detail-back")).toBeVisible();
        await page.locator("#wb-detail-back").click();
        await expect(shell).toHaveAttribute("data-route", "queue");
        await expect(page.locator("#wb-queue")).toBeVisible();
        await expect(page.locator("#wb-detail")).toBeHidden();
      }
    });

    test("both historic link formats land on the same plan", async ({
      page,
    }) => {
      await openQueue(page, deployment);
      const plans = await catalog(page);
      const alpha = rowFor(plans, "alpha");

      // The Workbench's own format, in whichever address this mode carries.
      await openDetail(page, deployment, alpha);
      const viaPlan = await detailHeader(page);
      expect(viaPlan.state).toBe(PLAN_STATES.alpha.primary_state);

      // The Companion's format, minted into every ntfy notification a phone
      // has ever received. It names a run rather than a plan, so the shell has
      // to resolve it against a catalog page before it can read anything.
      await page.goto(
        `${deployment.baseURL}/#run=${encodeURIComponent(alpha.run_key)}`,
      );
      await expect(page.locator("#wb-shell")).toHaveAttribute(
        "data-route",
        "detail",
      );
      // The link is rewritten to the canonical form on the way, so the back
      // button and a bookmark both behave as if the operator had arrived that
      // way. This is also the moment the run key became an address -- and the
      // key rides along afterwards, because which run the notification named
      // is what lets the overview file an old one under its own heading
      // instead of at the top of today's work.
      await expect
        .poll(() => new URL(page.url()).hash)
        .toBe(`${detailHash(alpha)}&run=${encodeURIComponent(alpha.run_key)}`);
      await expect(page.locator("#wb-detail-label")).toHaveText(viaPlan.label);
      await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
        "data-state",
        "idle",
      );
      expect(await detailHeader(page)).toEqual(viaPlan);
    });

    test("an unresolvable run link says so instead of showing an empty plan", async ({
      page,
    }) => {
      await page.goto(`${deployment.baseURL}/#run=0000000000000000`);
      await expect(page.locator("#wb-shell")).toHaveAttribute(
        "data-route",
        "detail",
      );
      await expect(page.locator("#wb-detail-label")).toHaveText("(opening run)");
      await expect(page.locator("#wb-detail-path")).toContainText(
        "0000000000000000",
      );
      // The hash is left alone: the next catalog page may still carry it, and
      // rewriting it to nothing would destroy the deep link.
      expect(new URL(page.url()).hash).toBe("#run=0000000000000000");
    });

    for (const name of Object.keys(PLAN_STATES)) {
      test(`every detail pane of ${name} shows material or says it has none`, async ({
        page,
      }) => {
        await openQueue(page, deployment);
        const plans = await catalog(page);
        await openDetail(page, deployment, rowFor(plans, name));

        for (const pane of DETAIL_PANES) {
          const locator = page.locator(`#${pane.id}`);
          await expect(locator).toBeVisible();
          const text = (await locator.textContent()).trim();
          expect(
            text.length,
            `${pane.id} rendered nothing at all`,
          ).toBeGreaterThan(0);
        }
        // The header is part of the answer: a pane full of "-" under a title
        // that never loaded is not a detail view.
        await expect(page.locator("#wb-detail-label")).not.toHaveText(
          "(loading)",
        );
        await expect(page.locator("#wb-detail-label")).not.toHaveText(
          "(error)",
        );
        await expect(page.locator("#wb-detail-state")).toHaveText(
          PLAN_STATES[name].primary_state,
        );
      });
    }

    test("a focus deep link puts the keyboard where the link asked", async ({
      page,
    }) => {
      await openQueue(page, deployment);
      const plans = await catalog(page);
      await openDetail(page, deployment, rowFor(plans, "alpha"), "&focus=unlock");

      // `DETAIL_FOCUS_TARGETS.unlock` scrolls the controls into view and hands
      // focus to the button, so an operator who followed a notification can
      // act without hunting for the control on a phone-sized screen.
      await expect
        .poll(() => page.evaluate(() => document.activeElement.id))
        .toBe("wb-control-unlock");
    });

    test("a poll that redraws the same queue does not move the page", async ({
      page,
    }) => {
      // The failure this guards is invisible in a screenshot and silent in a
      // log. WebKit reads a document scroll between `touchstart` and `touchend`
      // as a pan and throws the tap away, so a refresh that scrolls the page
      // back to where it already was -- every five seconds, forever -- eats the
      // press of an operator reaching for a control on a phone. No request, no
      // feedback, nothing to report: the one failure mode this interface may not
      // have. So a redraw that displaced nobody must move nobody.
      await recordPageScrolls(page);
      await interceptCatalog(page);
      await openTallQueue(page, deployment);

      const before = await scrollToBottom(page);
      expect(
        before.window,
        "the padded catalog does not scroll in this viewport",
      ).toBeGreaterThan(0);

      await pollNow(page);

      const after = await placeState(page);
      // The same answer drawn again is the same height, and the page never
      // moved, so there is nothing to put back.
      expect(after.height).toBe(before.height);
      expect(after.moves).toEqual([]);
      expect(after.window).toBe(before.window);
    });

    test("a poll that shortens the page puts the operator back", async ({
      page,
    }) => {
      // The other half of the same rule, and the reason the restore is still
      // there: when a redraw *does* displace the operator, dropping it would
      // leave them reading a different part of the queue than the one they were
      // looking at. Banners clearing is that redraw -- the notes above the queue
      // go away, the page gets shorter, and the browser clamps the offset to the
      // new bottom before the shell has drawn anything.
      await recordPageScrolls(page);
      const feed = await interceptCatalog(page);
      await openTallQueue(page, deployment);
      await expect(
        page.locator('#wb-observation .wb-observation__note[data-kind="connector"]'),
      ).toHaveCount(DEGRADED_CONNECTORS);

      const before = await scrollToBottom(page);
      expect(before.window).toBeGreaterThan(0);

      feed.connectors = 0;
      await pollNow(page);
      await expect(page.locator("#wb-observation")).toBeHidden();

      const after = await placeState(page);
      expect(after.height).toBeLessThan(before.height);
      // The place the operator was in, asked for again by name.
      expect(after.moves).toContainEqual({ x: 0, y: before.window });
      // And granted as far as the shorter page allows, rather than lost: the
      // bottom of the queue, not the top of it.
      expect(after.window).toBeGreaterThan(0);
      expect(Math.abs(after.window - after.maxWindow)).toBeLessThanOrEqual(2);
    });

    test("the keyboard can reach a plan without a pointer", async ({
      page,
    }) => {
      await openQueue(page, deployment);
      const plans = await catalog(page);
      const alpha = rowFor(plans, "alpha");
      const link = page
        .locator(`.wb-row[data-plan-dir="${addressOf(alpha).value}"]`)
        .locator('[data-field="label"]');
      await link.focus();
      await page.keyboard.press("Enter");
      await expect(page.locator("#wb-shell")).toHaveAttribute(
        "data-route",
        "detail",
      );
      expect(new URL(page.url()).hash).toBe(detailHash(alpha));
      // The view that replaced the queue is announced by its heading, rather
      // than leaving the keyboard on a link the phone layout just hid.
      await expect
        .poll(async () => page.evaluate(() => document.activeElement.id))
        .toBe("wb-detail-label");
    });

    test("a row itself selects its plan from the keyboard", async ({ page }) => {
      await openQueue(page, deployment);
      const plans = await catalog(page);
      const bravo = rowFor(plans, "bravo");
      const row = page.locator(
        `.wb-row[data-plan-dir="${addressOf(bravo).value}"]`,
      );
      await row.focus();
      await page.keyboard.press("Enter");
      await expect(row).toHaveAttribute("data-selected", "true");
      // Selecting is not navigating: the queue stays the route.
      await expect(page.locator("#wb-shell")).not.toHaveAttribute(
        "data-route",
        "detail",
      );
    });
  });
}

test("both deployments show the same plan under the same keys", async ({
  page,
}) => {
  const seen = [];
  for (const deployment of DEPLOYMENTS) {
    await openQueue(page, deployment);
    const plans = await catalog(page);
    const alpha = rowFor(plans, "alpha");
    await openDetail(page, deployment, alpha);
    seen.push({
      mode: deployment.name,
      plan_key: alpha.plan_key,
      run_key: alpha.run_key,
      state: (await detailHeader(page)).state,
      badge: await page.locator("#wb-detail-state").getAttribute("data-state"),
    });
  }
  expect(seen[0].plan_key).toBe(seen[1].plan_key);
  expect(seen[0].run_key).toBe(seen[1].run_key);
  expect(seen[0].state).toBe(seen[1].state);
  expect(seen[0].badge).toBe(seen[1].badge);
  // The addresses are allowed to differ -- that is the whole asymmetry -- but
  // only in the direction the deployments actually differ in.
  expect(DEPLOYMENTS.map((entry) => entry.address)).toEqual([
    "plan_dir",
    "plan_key",
  ]);
});
