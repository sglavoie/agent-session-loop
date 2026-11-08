/* interactions: polling. */
const {
  test,
  expect,
  openQueue,
  openArchive,
} = require("./interactions_support.js");

module.exports = function register(deployment) {

  test("the archive reloads after a refresh asked for mid-read", async ({
    page,
  }) => {
    await openArchive(page, deployment);
    let release;
    const held = new Promise((resolve) => {
      release = resolve;
    });
    let reads = 0;
    await page.route("**/api/archive", async (route) => {
      reads += 1;
      if (reads === 1) await held;
      await route.continue();
    });
    await page.evaluate(async () => {
      const archive = await import("/static/modules/archive.js");
      archive.loadArchiveView();
      archive.loadArchiveView();
    });
    await expect.poll(() => reads).toBe(1);
    release();
    await expect.poll(() => reads).toBe(2);
  });


  test("an unchanged poll writes nothing into the preview", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const preview = page.locator("#wb-shell-preview");
    await expect(preview).not.toHaveAttribute("aria-live", /.*/);
    const writes = await page.evaluate(async () => {
      const { state } = await import("/static/modules/state.js");
      const preview = await import("/static/modules/preview.js");
      const plan = state.previewPlan || state.lastVisiblePlans[0];
      const realNow = Date.now;
      const frozen = realNow();
      Date.now = () => frozen;
      try {
        preview.renderPreview(plan);
        const panel = document.getElementById("wb-shell-preview");
        const observer = new MutationObserver(() => {});
        observer.observe(panel, {
          subtree: true,
          childList: true,
          characterData: true,
        });
        preview.renderPreview(plan);
        const records = observer.takeRecords();
        observer.disconnect();
        return records.length;
      } finally {
        Date.now = realNow;
      }
    });
    expect(writes).toBe(0);
  });


  test("every view reads an epoch spelled as text as a moment", async ({
    page,
  }) => {
    // The Pi's TEXT columns hand back "1789466400.0". The overview window
    // always read it as a number; the sort, the idle cell, the preview and
    // the cleanup age now read it the same way.
    await openQueue(page, deployment);
    const read = await page.evaluate(async () => {
      const queue = await import("/static/modules/queue.js");
      const preview = await import("/static/modules/preview.js");
      const time = await import("/static/modules/time.js");
      const plan = { primary_state: "ready", last_activity_at: "1789466400.0" };
      return {
        seconds: time.toEpochSeconds("1789466400.0"),
        iso: time.toEpochSeconds("2026-09-15T08:00:00Z"),
        unzoned: time.toEpochSeconds("2026-09-15T08:00:00"),
        unzonedSpace: time.toEpochSeconds("2026-09-15 08:00:00.5"),
        sortKey: queue.parseActivityTimestamp("1789466400.0"),
        idle: preview.formatIdle(plan, 1789466400000 + 90000),
        age: preview.formatActivityAge("1789466400", 1789466400000 + 120000),
        garbage: queue.parseActivityTimestamp("not a time"),
      };
    });
    expect(read.seconds).toBe(1789466400);
    expect(read.iso).toBe(Date.parse("2026-09-15T08:00:00Z") / 1000);
    // Offset-less means UTC, as the Python half reads it, not local time.
    expect(read.unzoned).toBe(read.iso);
    expect(read.unzonedSpace).toBe(read.iso + 0.5);
    expect(read.sortKey).toBe(1789466400000);
    expect(read.idle).toBe("1m");
    expect(read.age).toBe("2m ago");
    expect(read.garbage).toBeNull();
  });
};
