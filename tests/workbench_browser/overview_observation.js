/* overview: observation. */
const {
  test,
  expect,
  PLAN_NAMES,
  openQueue,
  catalog,
  ACCOUNTED_FOR,
  incidentRow,
  interceptIncidents,
  publishIncidents,
} = require("./overview_support.js");

module.exports = function register(deployment) {

  // -- What the page could not see --------------------------------------

  test("a complete live observation raises no banner", async ({ page }) => {
    await openQueue(page, deployment);
    const body = await catalog(page);
    expect(body.freshness).toBe("live");
    expect(body.catalog_completeness).toBe("complete");
    // `live` and `complete` are the two words that need no sentence, so the
    // banner region is empty rather than reassuring.
    await expect(page.locator("#wb-observation")).toHaveAttribute(
      "data-count",
      "0",
    );
    await expect(page.locator("#wb-observation")).toBeHidden();
  });


  test("only an alert nobody can account for raises the delivery banner", async ({
    page,
  }) => {
    // The banner's sentence is a strong one -- the deployment could not
    // confirm an alert landed and will not try again -- and it is the reason
    // an operator distrusts a quiet screen. Which means it has to be false of
    // every incident that is still on its way: `pending`, `retry` and
    // `publishing` are all "not yet", and rendering the sentence for them
    // would turn every raised alert into a delivery failure at the moment it
    // was raised, teaching the operator to ignore the one banner that means
    // something.
    const feed = await interceptIncidents(page);
    await openQueue(page, deployment);
    const note = page.locator(
      '#wb-observation .wb-observation__note[data-kind="incident"]',
    );

    // Unresolved and `unconfirmed`: the one state the vocabulary keeps for
    // "nobody can establish whether this landed, and no path will resend it".
    await publishIncidents(page, feed, [incidentRow("lost-alert")]);
    await expect(note).toHaveCount(1);
    await expect(note).toHaveText(
      "The deployment could not confirm an alert reached their destination " +
        "and will not resend automatically: lost-alert",
    );

    // `unconfirmed` but resolved: the alert never landed and the situation it
    // was raised about is over, so there is nothing left to act on.
    await publishIncidents(page, feed, [
      incidentRow("closed-alert", { resolved_at: 2 }),
    ]);
    await expect(note).toHaveCount(0);

    // The other five states, unresolved, all at once. Each row is named after
    // its own state, so a banner that appeared here would say which state
    // leaked rather than just that one did.
    const inFlight = ACCOUNTED_FOR.map((name) =>
      incidentRow(name, { delivery_state: name }),
    );
    await publishIncidents(page, feed, inFlight);
    await expect(note).toHaveCount(0);
    // And with nothing else to report, the region is gone rather than empty.
    await expect(page.locator("#wb-observation")).toBeHidden();

    // One qualifying alert among them: counted once, named alone.
    await publishIncidents(page, feed, inFlight.concat([incidentRow("lost-alert")]));
    await expect(note).toHaveCount(1);
    await expect(note).toContainText("an alert");
    await expect(note).toContainText("lost-alert");
    for (const name of ACCOUNTED_FOR) {
      await expect(note, name).not.toContainText(name);
    }
  });


  test("every row says whether it is live information", async ({ page }) => {
    await openQueue(page, deployment, "#older=1");
    const marks = await page
      .locator(".wb-row")
      .evaluateAll((nodes) =>
        nodes.map((node) => node.getAttribute("data-observation-status")),
      );
    expect(marks).toHaveLength(PLAN_NAMES.length);
    // Everything the fixture publishes was confirmed by the newest
    // observation, in both modes: the Mac is standing on these plans and the
    // Pi's newest generation named all of them. The retained case is a
    // partial generation, which `tests/test_workbench_overview_catalog.py` drives.
    for (const mark of marks) expect(mark).toBe("current");
    await expect(page.locator('.wb-section[data-section="unconfirmed"]')).toBeHidden();
  });
};
