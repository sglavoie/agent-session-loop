/* interactions: detail. */
const {
  test,
  expect,
  PLAN_STATES,
  catalog,
  rowFor,
  detailHash,
  openDetail,
} = require("./interactions_support.js");

module.exports = function register(deployment) {

  test("a detail poll leaves the operator's edits and errors alone", async ({
    page,
  }) => {
    await openDetail(page, deployment, "bravo");
    const stopPolling = () =>
      page.evaluate(async () => {
        const detail = await import("/static/modules/detail.js");
        detail.stopDetailTimer();
      });
    const poll = () =>
      page.evaluate(async () => {
        const detail = await import("/static/modules/detail.js");
        await detail.refreshDetail();
        detail.stopDetailTimer();
      });
    await stopPolling();

    // The harness picker: a pick not yet applied survives the poll, so
    // "apply" sends what the operator chose rather than what the plan had.
    const harness = page.locator("#wb-control-harness");
    const options = await harness
      .locator("option")
      .evaluateAll((nodes) => nodes.map((node) => node.value).filter(Boolean));
    const current = await harness.inputValue();
    const pick = options.find((value) => value !== current);
    expect(pick, "the picker offers another harness").toBeTruthy();
    await harness.selectOption(pick);
    await harness.evaluate((el) => el.blur());
    await poll();
    await expect(harness).toHaveValue(pick);

    // The launch profile only exists where launching does.
    const form = page.locator("#wb-profile-form");
    if (!(await form.isVisible())) return;
    const label = page.locator("#wb-profile-label");
    await label.fill("draft label nobody saved");
    await label.evaluate((el) => el.blur());
    await page.locator("#wb-profile-max-review-fix-cycles").fill("-1");
    await form.evaluate((el) => el.requestSubmit());
    const errors = page.locator("#wb-profile-form-errors");
    await expect(errors).toBeVisible();
    await expect(errors).toContainText("max review-fix cycles");

    await poll();
    await expect(label).toHaveValue("draft label nobody saved");
    await expect(errors).toBeVisible();
    await expect(errors).toContainText("max review-fix cycles");
  });


  test("opening another plan forgets the last one before the read lands", async ({
    page,
  }) => {
    const alpha = await openDetail(page, deployment, "alpha");
    const bravo = rowFor(await catalog(page), "bravo");
    // Hold bravo's read so the window between the route change and the
    // first answer is wide enough to look into.
    let release;
    const held = new Promise((resolve) => {
      release = resolve;
    });
    await page.route("**/api/plan?**", async (route) => {
      await held;
      await route.continue();
    });
    const during = await page.evaluate(async (hash) => {
      await new Promise((resolve) => {
        window.addEventListener("hashchange", resolve, { once: true });
        window.location.hash = hash;
      });
      const { state } = await import("/static/modules/state.js");
      const identity = await import("/static/modules/identity.js");
      return {
        detailPlan: state.detailPlan,
        detailRegistry: state.detailRegistry,
        detailOutstanding: state.detailOutstanding,
        runKey: identity.routeRunKey(),
        path: document.getElementById("wb-detail-path").textContent,
        workspace: document.getElementById("wb-detail-workspace").textContent,
        state: document.getElementById("wb-detail-state").textContent.trim(),
      };
    }, detailHash(bravo));
    expect(during.detailPlan).toBeNull();
    expect(during.detailRegistry).toBeNull();
    expect(during.detailOutstanding).toBeNull();
    expect(during.runKey).not.toBe(alpha.run_key);
    expect(during.path).toBe("");
    expect(during.workspace).toBe("");
    expect(during.state).toBe("unknown");
    release();
    await expect(page.locator("#wb-detail-state")).toHaveAttribute(
      "data-state",
      PLAN_STATES.bravo.primary_state,
    );
  });


  test("a panel read that lands after a plan switch is not drawn", async ({
    page,
  }) => {
    const alpha = await openDetail(page, deployment, "alpha");
    const bravo = rowFor(await catalog(page), "bravo");
    await page.evaluate(async () => {
      const detail = await import("/static/modules/detail.js");
      detail.stopDetailTimer();
    });
    await page.waitForFunction(async () => {
      const { state } = await import("/static/modules/state.js");
      return !state.detailInFlight;
    });
    const param = alpha.plan_dir ? "plan_dir" : "plan_key";
    const value = alpha.plan_dir || alpha.plan_key;
    // Hold alpha's panel reads, then answer them with alpha's data only
    // once bravo is on screen: the late answers must be dropped.
    let release;
    const held = new Promise((resolve) => {
      release = resolve;
    });
    const late = async (route) => {
      const url = new URL(route.request().url());
      if (url.searchParams.get(param) !== value) return route.continue();
      await held;
      const body = url.pathname === "/api/logs"
        ? { ok: true, text: "ALPHA-LATE" }
        : {
            ok: true,
            action_token: "alpha-token",
            permitted_actions: ["redirect"],
            disabled_reasons: {},
            live_target: null,
          };
      return route.fulfill({ json: body });
    };
    await page.route("**/api/logs?**", late);
    await page.route("**/api/redirect-status?**", late);
    await page.evaluate(async () => {
      const panels = await import("/static/modules/panels.js");
      const redirect = await import("/static/modules/redirect.js");
      window.__lateReads = Promise.all([
        panels.loadAndRenderLog("stdout"),
        redirect.loadAndRenderRedirect(),
      ]);
    });
    await page.evaluate((hash) => {
      window.location.hash = hash;
    }, detailHash(bravo));
    await expect(page.locator("#wb-detail-state")).toHaveAttribute(
      "data-state",
      PLAN_STATES.bravo.primary_state,
    );
    release();
    const after = await page.evaluate(async () => {
      await window.__lateReads;
      const { state } = await import("/static/modules/state.js");
      return {
        stdout: document.getElementById("wb-detail-stdout").textContent,
        token: state.redirect.actionToken,
      };
    });
    expect(after.stdout).not.toBe("ALPHA-LATE");
    expect(after.token).not.toBe("alpha-token");
  });


  test("a detail error leaves nothing of the plan behind it", async ({
    page,
  }) => {
    await openDetail(page, deployment, "alpha");
    await page.evaluate(async () => {
      const detail = await import("/static/modules/detail.js");
      detail.stopDetailTimer();
      detail.renderDetailError("plan not found");
    });
    await expect(page.locator("#wb-detail-label")).toHaveText("(error)");
    await expect(page.locator("#wb-detail-hint")).toHaveText("plan not found");
    await expect(page.locator("#wb-detail-path")).toHaveText("");
    await expect(page.locator("#wb-detail-workspace")).toHaveText("");
    await expect(page.locator("#wb-detail-state")).toHaveAttribute(
      "data-state",
      "unknown",
    );
  });


  test("closing a plan routes back to the queue exactly once", async ({
    page,
  }) => {
    await openDetail(page, deployment, "bravo");
    await page.evaluate(async () => {
      const detail = await import("/static/modules/detail.js");
      detail.stopDetailTimer();
    });
    await page.waitForFunction(async () => {
      const { state } = await import("/static/modules/state.js");
      return !state.fetchInFlight && !state.detailInFlight;
    });
    // Each time the queue route runs it reads the queue; a second run while
    // the first read is on the wire queues a second read behind it.
    const reads = [];
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/queue") reads.push(request);
    });
    await page.evaluate(async () => {
      const router = await import("/static/modules/router.js");
      router.navigateToQueue();
    });
    await expect(page.locator("#wb-shell")).toHaveAttribute(
      "data-route",
      "queue",
    );
    await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
      "data-state",
      "idle",
    );
    await page.waitForTimeout(500);
    expect(reads.length).toBe(1);
  });
};
