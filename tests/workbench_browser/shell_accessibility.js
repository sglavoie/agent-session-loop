/* shell: accessibility. */
const {
  test,
  expect,
  DEGRADED_CONNECTORS,
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
} = require("./shell_support.js");

module.exports = function register(deployment) {

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


  test("the refresh line is a status region that speaks only errors", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const status = page.locator("#wb-refresh-status");
    await expect(status).toHaveAttribute("role", "status");
    // Permanently polite: toggling it on in the same tick as the error text
    // left the announcement to each screen reader's timing.
    await expect(status).toHaveAttribute("aria-live", "polite");
    // Every poll rewrites "refreshing…" and "updated …"; announcing those
    // would talk over everything else, so routine text is aria-hidden.
    const routine = page.locator("#wb-refresh-routine");
    const spoken = page.locator("#wb-refresh-error");
    await expect(routine).toHaveAttribute("aria-hidden", "true");
    await expect(spoken).toHaveText("");
    await page.evaluate(async () => {
      const queue = await import("/static/modules/queue.js");
      queue.stopQueueTimer();
      queue.setRefreshStatus("error", "error: unreachable");
    });
    await expect(status).toHaveAttribute("aria-live", "polite");
    await expect(status).toHaveText("error: unreachable");
    await expect(spoken).toHaveText("error: unreachable");
    await page.evaluate(async () => {
      const queue = await import("/static/modules/queue.js");
      queue.setRefreshStatus("idle", "updated now");
    });
    await expect(status).toHaveText("updated now");
    await expect(spoken).toHaveText("");
  });


  test("leaving a plan gives the keyboard back to the queue", async ({
    page,
  }) => {
    // On the stacked shell "← queue" hides itself when pressed; on the
    // two-pane shell it may too. Either way focus must not fall to the body.
    await openQueue(page, deployment);
    const bravo = rowFor(await catalog(page), "bravo");
    await page.goto(deployment.baseURL + "/" + detailHash(bravo));
    await expect(page.locator("#wb-shell")).toHaveAttribute(
      "data-route",
      "detail",
    );
    const back = page.locator("#wb-detail-back");
    if (!(await back.isVisible())) {
      test.skip(true, "this shell shows no back button");
    }
    await back.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator("#wb-shell")).not.toHaveAttribute(
      "data-route",
      "detail",
    );
    await expect
      .poll(async () =>
        page.evaluate(() => {
          const active = document.activeElement;
          if (!active || active === document.body) return "body";
          if (active.id === "wb-queue") return "queue";
          return active.classList.contains("wb-row") ? "row" : active.id;
        }),
      )
      .toMatch(/^(row|queue)$/);
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
    // The selection is announced, not only drawn: a screen reader on the
    // focused row hears it is the current one, and no other row claims it.
    await expect(row).toHaveAttribute("aria-current", "true");
    await expect(page.locator('.wb-row[aria-current="true"]')).toHaveCount(1);
    // Selecting is not navigating: the queue stays the route.
    await expect(page.locator("#wb-shell")).not.toHaveAttribute(
      "data-route",
      "detail",
    );
  });
};
