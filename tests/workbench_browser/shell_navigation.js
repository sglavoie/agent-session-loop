/* shell: navigation. */
const {
  test,
  expect,
  PLAN_STATES,
  DETAIL_PANES,
  isDesktop,
  openQueue,
  catalog,
  rowFor,
  addressOf,
  detailHash,
  detailHeader,
  openDetail,
} = require("./shell_support.js");

module.exports = function register(deployment) {
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


  test("a plan opened while another's read is in flight shows the one opened last", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const plans = await catalog(page);
    const alpha = rowFor(plans, "alpha");
    const bravo = rowFor(plans, "bravo");
    await openDetail(page, deployment, alpha);
    const alphaLabel = (await detailHeader(page)).label;
    await openDetail(page, deployment, bravo);
    const bravoLabel = (await detailHeader(page)).label;
    expect(alphaLabel).not.toBe(bravoLabel);

    // Hold alpha's plan read on the wire, so the switch to bravo lands
    // while it is still in flight.
    // The API names a plan as `plan_dir` / `plan_key`, not as the hash's
    // `dir` / `key`.
    const held = {
      param: alpha.plan_dir ? "plan_dir" : "plan_key",
      value: addressOf(alpha).value,
    };
    let release;
    const gate = new Promise((resolve) => {
      release = resolve;
    });
    let markHeld;
    const holding = new Promise((resolve) => {
      markHeld = resolve;
    });
    const isAlphaRead = (url) =>
      /\/api\/plan\?/.test(url) &&
      new URL(url).searchParams.get(held.param) === held.value;
    await page.route(/\/api\/plan\?/, async (route) => {
      if (isAlphaRead(route.request().url())) {
        markHeld();
        await gate;
      }
      await route.continue();
    });
    await page.evaluate((hash) => {
      window.location.hash = hash;
    }, detailHash(alpha));
    await holding;
    // Every label the header shows once bravo is opened: alpha's late
    // answer must never be drawn over it, not merely be corrected by the
    // next poll.
    await page.evaluate((hash) => {
      const label = document.getElementById("wb-detail-label");
      window.__wbLabels = [];
      new MutationObserver(() => {
        window.__wbLabels.push((label.textContent || "").trim());
      }).observe(label, { childList: true, characterData: true, subtree: true });
      window.location.hash = hash;
    }, detailHash(bravo));
    const bravoRead = page.waitForResponse(
      (response) =>
        /\/api\/plan\?/.test(response.url()) && !isAlphaRead(response.url()),
    );
    const alphaRead = page.waitForResponse((response) =>
      isAlphaRead(response.url()),
    );
    release();
    await alphaRead;
    // The follow-up read is for the plan now on screen.
    await bravoRead;
    await expect(page.locator("#wb-detail-label")).toHaveText(bravoLabel);
    await expect
      .poll(() =>
        page.evaluate(async () => {
          const { state } = await import("/static/modules/state.js");
          return state.detailInFlight;
        }),
      )
      .toBe(false);
    const labels = await page.evaluate(() => window.__wbLabels);
    expect(labels).not.toContain(alphaLabel);
    await expect(page.locator("#wb-detail-label")).toHaveText(bravoLabel);
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
};
