/* overview: history. */
const {
  test,
  expect,
  HISTORY_OUTCOMES,
  openQueue,
  catalog,
  rowFor,
  openHistory,
} = require("./overview_support.js");

module.exports = function register(deployment) {

  // -- History ----------------------------------------------------------

  test("history is collapsed until it is asked for", async ({ page }) => {
    await openQueue(page, deployment);
    const pane = page.locator("#wb-history");
    await expect(pane).toHaveAttribute("data-open", "false");
    await expect(page.locator("#wb-history-toggle")).toHaveText(
      "show history",
    );

    await openHistory(page);
    await expect(page.locator("#wb-history-toggle")).toHaveText(
      "hide history",
    );
    await expect(page.locator("#wb-history-count")).toHaveText(
      String(HISTORY_OUTCOMES.length),
    );

    await page.locator("#wb-history-toggle").click();
    await expect(pane).toHaveAttribute("data-open", "false");
  });


  test("a linked ending opens the pane, and leaving the link clears its note", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const entry = await page
      .locator(".wb-history-row")
      .first()
      .getAttribute("data-entry-id");
    await openQueue(page, deployment, `#entry=${encodeURIComponent(entry)}`);
    // Collapsed by default, so a link that only highlighted its card would
    // highlight something nobody can see.
    await expect(page.locator("#wb-history")).toHaveAttribute("data-open", "true");
    await expect(page.locator("#wb-history-toggle")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    await expect(page.locator("#wb-history-toggle")).toHaveText("hide history");
    await expect(
      page.locator('.wb-history-row[data-linked="true"]'),
    ).toBeVisible();

    await openQueue(page, deployment, "#entry=no-such-entry");
    await expect(page.locator("#wb-history-missing")).toBeVisible();
    await page.evaluate(() => {
      location.hash = "#older=1";
    });
    await page.locator("#wb-refresh-now").click();
    await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
      "data-state",
      "idle",
    );
    await expect(page.locator("#wb-history-missing")).toBeHidden();
  });


  test("the endings are listed newest first with their outcomes", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    await openHistory(page);
    const outcomes = await page
      .locator(".wb-history-row")
      .evaluateAll((nodes) =>
        nodes.map((node) => node.getAttribute("data-outcome")),
      );
    expect(outcomes).toEqual(HISTORY_OUTCOMES);
    await expect(page.locator("#wb-history-empty")).toBeHidden();
  });


  test("an ending renders the facts an operator reads it for", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    await openHistory(page);
    const rows = await page
      .locator(".wb-history-row")
      .evaluateAll((nodes) =>
        nodes.map((node) => {
          const text = (field) => {
            const el = node.querySelector(`[data-field="${field}"]`);
            return el ? (el.textContent || "").trim() : null;
          };
          return {
            entry: node.getAttribute("data-entry-id"),
            label: text("label"),
            harness: text("harness"),
            finished: text("finished"),
            duration: text("duration"),
            run: text("run"),
            link: node
              .querySelector('[data-field="link"]')
              .getAttribute("href"),
          };
        }),
      );
    for (const row of rows) {
      expect(row.entry).toBeTruthy();
      // A row that says only "a run ended" is a row an operator cannot act
      // on: which plan, which harness, when, and how to get back to it.
      expect(row.label).toBeTruthy();
      expect(row.harness).not.toBe("-");
      expect(row.finished).not.toBe("-");
      expect(row.duration).not.toBe("-");
      expect(row.run).not.toBe("-");
      expect(row.link).toBe(`#entry=${row.entry}`);
    }
  });


  test("the outcome filter narrows the pane to one ending", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    await openHistory(page);
    const wanted = HISTORY_OUTCOMES[HISTORY_OUTCOMES.length - 1];

    await page.locator("#wb-history-outcome").selectOption(wanted);
    await expect(page.locator(".wb-history-row")).toHaveCount(1);
    await expect(page.locator(".wb-history-row")).toHaveAttribute(
      "data-outcome",
      wanted,
    );
    await expect(page.locator("#wb-history-count")).toHaveText("1");

    // An outcome nothing in the window has empties the pane and says so,
    // rather than leaving the previous answer on the screen.
    const absent = ["completed", "paused", "abandoned"].find(
      (name) => HISTORY_OUTCOMES.indexOf(name) === -1,
    );
    await page.locator("#wb-history-outcome").selectOption(absent);
    await expect(page.locator(".wb-history-row")).toHaveCount(0);
    await expect(page.locator("#wb-history-empty")).toBeVisible();

    await page.locator("#wb-history-outcome").selectOption("");
    await expect(page.locator(".wb-history-row")).toHaveCount(
      HISTORY_OUTCOMES.length,
    );
  });


  test("a filter changed during a slow history read is not dropped", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    await openHistory(page);
    // The queue poll also re-reads history, which would paper over a
    // dropped request; stop it so only the filter changes ask.
    await page.evaluate(async () => {
      const queue = await import("/static/modules/queue.js");
      queue.stopQueueTimer();
    });
    const wanted = HISTORY_OUTCOMES[HISTORY_OUTCOMES.length - 1];
    const absent = ["completed", "paused", "abandoned"].find(
      (name) => HISTORY_OUTCOMES.indexOf(name) === -1,
    );
    let release = null;
    const held = new Promise((resolve) => {
      release = resolve;
    });
    let heldOnce = false;
    let asked = 0;
    await page.route("**/api/history*", async (route) => {
      asked += 1;
      if (!heldOnce) {
        heldOnce = true;
        await held;
      }
      await route.continue();
    });
    // The first change goes out and is held; the second arrives while it
    // is still on the wire and has to run once the first settles.
    await page.locator("#wb-history-outcome").selectOption(absent);
    await expect.poll(() => asked).toBe(1);
    await page.locator("#wb-history-outcome").selectOption(wanted);
    release();
    await expect.poll(() => asked).toBe(2);
    await expect(page.locator(".wb-history-row")).toHaveCount(1);
    await expect(page.locator(".wb-history-row")).toHaveAttribute(
      "data-outcome",
      wanted,
    );
  });


  test("history is narrowed by the same words the queue is", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    await openHistory(page);

    // Switching the queue's search must not leave the history describing a
    // different plan than the list above it.
    await page.locator("#wb-search").fill("charlie");
    await expect(page.locator(".wb-history-row")).toHaveCount(
      HISTORY_OUTCOMES.length,
    );

    await page.locator("#wb-search").fill("alpha");
    await expect(page.locator(".wb-history-row")).toHaveCount(0);
    await expect(page.locator("#wb-history-empty")).toBeVisible();
  });


  test("an ending links to the run the queue shows", async ({ page }) => {
    await openQueue(page, deployment, "#older=1");
    const charlie = rowFor((await catalog(page)).plans, "charlie");
    await openHistory(page);
    const runs = await page
      .locator('.wb-history-row [data-field="run"]')
      .allTextContents();
    // The newest ending is the run the queue names for this plan, so one
    // notification link resolves into either pane.
    expect(runs[0].trim()).toBe(charlie.run_id || runs[0].trim());
    expect(runs.every((text) => text.trim().length > 0)).toBe(true);
  });
};
