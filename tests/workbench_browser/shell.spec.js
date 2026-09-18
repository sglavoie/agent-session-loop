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
      // way. This is also the moment the run key became an address.
      await expect
        .poll(() => new URL(page.url()).hash)
        .toBe(detailHash(alpha));
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
