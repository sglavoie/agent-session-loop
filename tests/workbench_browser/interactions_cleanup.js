/* interactions: cleanup. */
const {
  test,
  expect,
  catalog,
  capabilities,
  openQueue,
} = require("./interactions_support.js");

module.exports = function register(deployment) {

  test("bulk cleanup is a review before it is an action", async ({ page }) => {
    // The bar exists to make archiving many plans one reviewed decision
    // rather than many unreviewed ones, so its archive button starts shut
    // and says which of the two reasons applies.
    await openQueue(page, deployment);
    const { byOperation } = await capabilities(page);
    const cleanupAction =
      deployment.address === "plan_key" ? "plan-archive" : "plans-archive";
    await page.locator("#wb-cleanup-toggle").click();
    const bar = page.locator("#wb-cleanup-bar");
    await expect(bar).toBeVisible();

    const archive = page.locator("#wb-cleanup-archive");
    await expect(archive).toBeDisabled();
    await expect(archive).toHaveAttribute(
      "data-disabled-reason",
      byOperation[cleanupAction].available
        ? "no plans are selected"
        : byOperation[cleanupAction].reason,
    );

    // Selecting is a read: it fills the review list and arms nothing. The
    // count is what the operator is being asked to agree with.
    await page.locator("#wb-cleanup-select-visible").click();
    await expect(page.locator("#wb-cleanup-count")).not.toContainText(
      "0 plans selected",
    );
    if (byOperation[cleanupAction].available) {
      await expect(archive).toBeEnabled();
    } else {
      // Still shut on the Pi, and now for the mode's reason rather than the
      // selection's -- the nearer explanation stepped aside for the truer one.
      await expect(archive).toBeDisabled();
      await expect(archive).toHaveAttribute(
        "data-disabled-reason",
        byOperation[cleanupAction].reason,
      );
    }
    await page.locator("#wb-cleanup-clear").click();
    await expect(page.locator("#wb-cleanup-count")).toContainText(
      "0 plans selected",
    );
  });


  test("select candidates includes matches beyond the first catalog page", async ({ page }) => {
    await openQueue(page, deployment);
    await page.route("**/api/queue?**", async (route) => {
      const url = new URL(route.request().url());
      const offset = Number(url.searchParams.get("offset") || 0);
      const makePlan = (index) => ({
        plan_key: `candidate-${index}`,
        display_label: `Candidate ${index}`,
        primary_state: "ready",
        archivable: true,
        last_activity_at: null,
      });
      const plans = offset === 0
        ? Array.from({ length: 200 }, (_, index) => makePlan(index))
        : [makePlan(200)];
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          plans,
          catalog_matched: 201,
          catalog_revision: "stable-catalog",
        }),
      });
    });
    await page.locator("#wb-cleanup-toggle").click();
    await page.locator("#wb-cleanup-select-candidates").click();
    await expect(page.locator("#wb-cleanup-count")).toContainText("201 plans selected");
    await expect(page.locator("#wb-cleanup-feedback")).toContainText(
      "201 old plans selected from 201 matching plans",
    );
    const selected = await page.evaluate(async () => {
      const { state } = await import("/static/modules/state.js");
      return [...state.cleanupSelection];
    });
    expect(selected).toContain("candidate-200");
  });


  test("select candidates walks every page this deployment really serves", async ({
    page,
  }) => {
    // Not mocked: the pages come from the deployment itself, cut one plan
    // at a time so the fixture's handful of plans spans several. Each page
    // has to name the total it was cut from and one revision, or the walk
    // stops at the first page (no total) or refuses the second (no
    // revision) -- which is what the local provider used to do.
    await openQueue(page, deployment);
    const pages = [];
    await page.route("**/api/queue?**", async (route) => {
      const url = new URL(route.request().url());
      url.searchParams.set("limit", "1");
      pages.push(Number(url.searchParams.get("offset") || 0));
      await route.continue({ url: url.toString() });
    });
    await page.locator("#wb-cleanup-toggle").click();
    pages.length = 0;
    await page.locator("#wb-cleanup-select-candidates").click();
    const feedback = page.locator("#wb-cleanup-feedback");
    await expect(feedback).toContainText(/selected from \d+ matching plans/);
    const matched = Number(
      /from (\d+) matching plans/.exec(await feedback.textContent())[1],
    );
    expect(matched).toBeGreaterThan(1);
    for (let offset = 0; offset < matched; offset += 1) {
      expect(pages).toContain(offset);
    }
  });


  test("select candidates accepts pages that carry no revision at all", async ({
    page,
  }) => {
    // A provider that publishes no revision publishes none on any page.
    // Missing on both sides is agreement, not a catalog that changed.
    await openQueue(page, deployment);
    await page.route("**/api/queue?**", async (route) => {
      const url = new URL(route.request().url());
      const offset = Number(url.searchParams.get("offset") || 0);
      const plan = {
        plan_key: `unrevised-${offset}`,
        display_label: `Unrevised ${offset}`,
        primary_state: "ready",
        archivable: true,
        last_activity_at: null,
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, plans: [plan], catalog_matched: 2 }),
      });
    });
    await page.locator("#wb-cleanup-toggle").click();
    await page.locator("#wb-cleanup-select-candidates").click();
    await expect(page.locator("#wb-cleanup-feedback")).toContainText(
      "2 old plans selected from 2 matching plans",
    );
  });
};
