/* interactions: archive. */
const {
  test,
  expect,
  ARCHIVE,
  isDesktop,
  capabilities,
  openArchive,
} = require("./interactions_support.js");

module.exports = function register(deployment) {

  test("the archive is either browsable or explained, never blank", async ({
    page,
  }) => {
    await openArchive(page, deployment);
    await expect(page.locator("#wb-archive")).toBeVisible();
    const { byOperation } = await capabilities(page);
    const entries = page.locator("#wb-archive-entries");
    if (byOperation["archive-list"].available) {
      await expect(
        entries.locator(
          `[data-archive-id="${ARCHIVE.expired.archive_id}"]`,
        ),
      ).toBeVisible();
      await expect(
        entries.locator(`[data-archive-id="${ARCHIVE.retained.archive_id}"]`),
      ).toBeVisible();
    } else {
      // The archive lives on the Mac. An empty list here would read as an
      // archive with nothing in it, which is a different and false fact.
      await expect(entries).toContainText("archive unavailable");
      await expect(entries).toContainText(byOperation["archive-list"].reason);
    }
  });


  test("purging an expired entry asks to be looked at before it asks for a number", async ({
    page,
  }) => {
    await openArchive(page, deployment);
    const { byOperation } = await capabilities(page);
    test.skip(
      !byOperation["archive-list"].available,
      "this deployment does not publish the archive",
    );
    await expect(
      page.locator(
        `#wb-archive-entries [data-archive-id="${ARCHIVE.expired.archive_id}"]`,
      ),
    ).toBeVisible();

    // A typed count is a good guard against the wrong button and a poor one
    // against not having looked: "select candidates" can fill the selection
    // with plans the operator has never seen the names of.
    await page.locator("#wb-archive-select-candidates").click();
    await expect(page.locator("#wb-archive-count")).toContainText(
      `${ARCHIVE.expired_count} of ${ARCHIVE.entry_count} entries selected`,
    );

    // Any dialog here is a failure: the review has to come first, so there
    // is nothing yet to confirm.
    page.on("dialog", (dialog) => {
      dialog.dismiss();
      throw new Error("the purge asked for a number before the review");
    });
    await page.locator("#wb-archive-purge-selected").click();

    const review = page.locator("#wb-archive-review");
    await expect(review).toBeVisible();
    await expect(page.locator("#wb-archive-review-message")).toContainText(
      "past retention",
    );
    await expect(
      page.locator(
        `#wb-archive-review-list [data-archive-id="${ARCHIVE.expired.archive_id}"]`,
      ),
    ).toBeVisible();
    await expect(page.locator("#wb-archive-feedback")).toContainText(
      "review the 1 expired entry before purging",
    );
    // The acknowledgement is where the keyboard is put, because it is the
    // next thing the operator has to decide.
    await expect
      .poll(async () => page.evaluate(() => document.activeElement.id))
      .toBe("wb-archive-review-ack");

    // Backing out leaves the archive exactly as it was.
    await page.locator("#wb-archive-review-cancel").click();
    await expect(review).toBeHidden();
    await expect(
      page.locator(
        `#wb-archive-entries [data-archive-id="${ARCHIVE.expired.archive_id}"]`,
      ),
    ).toBeVisible();
    // Hiding the review took the focused button with it; the keyboard goes
    // back to the control that opened it, as the cleanup review does.
    await expect
      .poll(async () => page.evaluate(() => document.activeElement.id))
      .toBe("wb-archive-purge-selected");
  });


  test("a restore stays shut until its receipt settles", async ({ page }) => {
    await openArchive(page, deployment);
    const { byOperation } = await capabilities(page);
    test.skip(
      !byOperation["archive-list"].available ||
        !byOperation["archive-restore"].available,
      "this deployment does not carry out restores",
    );
    // The restore is only recorded, and its receipt stays pending until
    // released: the window a second click used to start a second loop in.
    let submissions = 0;
    await page.route("**/api/archive/restore", (route) => {
      submissions += 1;
      return route.fulfill({
        status: 202,
        json: { ok: true, operation: { operation_id: "op-held", status: "pending" } },
      });
    });
    let release;
    const held = new Promise((resolve) => {
      release = resolve;
    });
    await page.route("**/api/archive?operation_id=*", async (route) => {
      await held;
      return route.fulfill({
        json: { ok: true, operation: { operation_id: "op-held", status: "consumed" } },
      });
    });
    const row = page.locator(
      `#wb-archive-entries [data-archive-id="${ARCHIVE.expired.archive_id}"]`,
    );
    const restore = row.locator('[data-action="archive-restore"]');
    await restore.click();
    await expect.poll(() => submissions).toBe(1);
    await expect(restore).toBeDisabled();
    await expect(restore).toHaveAttribute(
      "data-disabled-reason",
      /still settling/,
    );
    // A second request from code rather than the (disabled) button is
    // refused too, and nothing more goes on the wire.
    await page.evaluate(async (id) => {
      const archive = await import("/static/modules/archive.js");
      await archive.handleArchiveRestore(id);
    }, ARCHIVE.expired.archive_id);
    expect(submissions).toBe(1);
    release();
    await expect(page.locator("#wb-archive-restore-selected")).not.toHaveAttribute(
      "data-disabled-reason",
      /still settling/,
    );
    expect(submissions).toBe(1);
  });


  test("a bulk action with nothing selected explains the selection, not the mode", async ({
    page,
  }) => {
    // Two reasons to be shut, and they are different sentences. An empty
    // selection is the operator's to fix; a mode that does not carry
    // restores out is not, and offering the first explanation for the second
    // situation would send them looking for a checkbox that cannot help.
    await openArchive(page, deployment);
    const { byOperation } = await capabilities(page);
    const restore = page.locator("#wb-archive-restore-selected");
    await expect(restore).toBeDisabled();
    if (byOperation["archive-restore"].available) {
      await expect(restore).toHaveAttribute(
        "data-disabled-reason",
        "no archived entries are selected",
      );
    } else {
      await expect(restore).toHaveAttribute(
        "data-disabled-reason",
        byOperation["archive-restore"].reason,
      );
    }
  });


  test("the archive review fits the screen it is read on", async ({ page }) => {
    await openArchive(page, deployment);
    const { byOperation } = await capabilities(page);
    test.skip(
      !byOperation["archive-list"].available,
      "this deployment does not publish the archive",
    );
    await page.locator("#wb-archive-select-candidates").click();
    page.on("dialog", (dialog) => dialog.dismiss());
    await page.locator("#wb-archive-purge-selected").click();
    await expect(page.locator("#wb-archive-review")).toBeVisible();

    // The rows carry absolute plan paths, which on the phone are several
    // times the width of the screen. A confirmation panel that pushed the
    // page sideways would put its own buttons off-screen.
    const overflow = await page.evaluate(() => {
      const root = document.documentElement;
      return root.scrollWidth - root.clientWidth;
    });
    expect(overflow, "the review panel widened the page").toBeLessThanOrEqual(1);
    // Asserted rather than assumed: a run where every project reported the
    // desktop width would pass this test without ever having drawn the
    // phone layout it is here for.
    await expect(page.locator("#wb-shell")).toHaveAttribute(
      "data-viewport",
      isDesktop(page) ? "desktop" : "mobile",
    );
  });
};
