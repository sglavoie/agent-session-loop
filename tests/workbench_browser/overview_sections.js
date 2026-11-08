/* overview: sections. */
const {
  test,
  expect,
  PLAN_NAMES,
  sectionOf,
  sectionHidden,
  openQueue,
  catalog,
  rowFor,
  addressOf,
  renderedSection,
  hashParam,
} = require("./overview_support.js");

module.exports = function register(deployment) {
  // -- Sections ---------------------------------------------------------

  test("every plan is drawn in the section the fixture says it belongs in", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const body = await catalog(page);
    for (const name of PLAN_NAMES) {
      const plan = rowFor(body.plans, name);
      // `charlie` is inside the hidden older section, so it is in the DOM
      // and not on the screen. Section membership is the claim; visibility
      // is the toggle's, one test down.
      expect(await renderedSection(page, plan), name).toBe(sectionOf(name));
    }
  });


  test("the sections are drawn in the order the shell declares", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const order = await page
      .locator("#wb-queue .wb-section")
      .evaluateAll((nodes) =>
        nodes.map((node) => node.getAttribute("data-section")),
      );
    const declared = await page.evaluate(() =>
      import("/static/modules/constants.js").then((m) => m.OVERVIEW_SECTIONS),
    );
    // A subsequence rather than an equality: a section with nothing in it is
    // hidden, and the point is that the ones that *are* drawn keep the
    // declared order rather than the order their first row arrived in.
    expect(declared.filter((name) => order.indexOf(name) !== -1)).toEqual(
      order.filter((name) => declared.indexOf(name) !== -1),
    );
  });


  test("a section says how many rows it holds, including hidden ones", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const counted = await page
      .locator("#wb-queue .wb-section")
      .evaluateAll((nodes) =>
        nodes
          .filter((node) => !node.hidden)
          .map((node) => ({
            section: node.getAttribute("data-section"),
            count: Number(node.getAttribute("data-count")),
            rows: node.querySelectorAll(".wb-row").length,
          })),
      );
    expect(counted.length).toBeGreaterThan(0);
    for (const entry of counted) {
      // The count has to match the rows the section holds even when the list
      // is hidden, because "show older (1)" is the only thing telling the
      // operator the row exists at all.
      expect(entry.count, entry.section).toBe(entry.rows);
    }
    expect(counted.reduce((sum, entry) => sum + entry.rows, 0)).toBe(
      PLAN_NAMES.length,
    );
  });


  // -- The eight-hour inactive-work window -------------------------------

  test("the quiet plan starts behind the toggle and the toggle reveals it", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const charlie = rowFor((await catalog(page)).plans, "charlie");
    const row = page.locator(
      `.wb-row[data-plan-dir="${addressOf(charlie)}"]`,
    );
    await expect(row).toBeHidden();

    const toggle = page.locator("#wb-older-toggle");
    await expect(toggle).toHaveAttribute("data-count", "1");
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    await expect(toggle).toHaveText("show older (1)");

    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    await expect(toggle).toHaveText("hide older (1)");
    await expect(row).toBeVisible();

    await toggle.click();
    await expect(row).toBeHidden();
  });


  test("current work stays on the screen without the toggle", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const body = await catalog(page);
    for (const name of PLAN_NAMES) {
      if (sectionHidden(name)) continue;
      const plan = rowFor(body.plans, name);
      await expect(
        page.locator(`.wb-row[data-plan-dir="${addressOf(plan)}"]`),
        name,
      ).toBeVisible();
    }
  });


  test("the older toggle survives a reload through the URL", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    await page.locator("#wb-older-toggle").click();
    await expect(page).toHaveURL(/older=1/);

    // The link is the point: the operator sends the view they were looking
    // at, and the person who opens it sees the quiet row too.
    await openQueue(page, deployment, "#older=1");
    const charlie = rowFor((await catalog(page)).plans, "charlie");
    await expect(
      page.locator(`.wb-row[data-plan-dir="${addressOf(charlie)}"]`),
    ).toBeVisible();
    await expect(page.locator("#wb-older-toggle")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });


  // -- The run a notification named -------------------------------------

  test("a link to a run that has gone quiet gets its own heading", async ({
    page,
  }) => {
    // The notification is old news by the time it is opened, and the queue
    // must not read as though it is not: the run gets a heading of its own
    // rather than a row among the work that is actually current.
    await openQueue(page, deployment, "#older=1");
    const charlie = rowFor((await catalog(page)).plans, "charlie");
    expect(sectionOf("charlie")).toBe("older");

    await page.goto(
      `${deployment.baseURL}/#run=${encodeURIComponent(charlie.run_key)}&older=1`,
    );
    await expect(page.locator("#wb-shell")).toHaveAttribute(
      "data-route",
      "detail",
    );
    await expect(page.locator(".wb-row")).toHaveCount(PLAN_NAMES.length);
    // Resolved into an address, and still carrying which run it was for --
    // the second half is what the heading is drawn from.
    await hashParam(page, "run").toBe(charlie.run_key);
    await expect
      .poll(() => renderedSection(page, charlie))
      .toBe("older-linked");

    const linked = page.locator('.wb-section[data-section="older-linked"]');
    await expect(linked).toHaveAttribute("data-count", "1");
    // And it is out of the older section rather than in both.
    await expect(
      page.locator('.wb-section[data-section="older"] .wb-row'),
    ).toHaveCount(0);
  });


  test("a link to a run that is still current work is left where it is", async ({
    page,
  }) => {
    // The separation is the window's, not the link's: a linked run that has
    // not gone quiet belongs with the rest of today's work, and giving it a
    // heading would be moving current work out of the way.
    await openQueue(page, deployment);
    const alpha = rowFor((await catalog(page)).plans, "alpha");

    await page.goto(
      `${deployment.baseURL}/#run=${encodeURIComponent(alpha.run_key)}`,
    );
    await expect(page.locator("#wb-shell")).toHaveAttribute(
      "data-route",
      "detail",
    );
    await expect(page.locator(".wb-row")).toHaveCount(PLAN_NAMES.length);
    await expect
      .poll(() => renderedSection(page, alpha))
      .toBe(sectionOf("alpha"));
    await expect(
      page.locator('.wb-section[data-section="older-linked"]'),
    ).toBeHidden();
  });
};
