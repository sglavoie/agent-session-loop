/* overview: filters. */
const {
  test,
  expect,
  PLAN_NAMES,
  openQueue,
  catalog,
  rowFor,
  addressOf,
  renderedProject,
  hashParam,
} = require("./overview_support.js");

module.exports = function register(deployment) {

  // -- Project grouping --------------------------------------------------

  test("every drawn row sits under a project heading", async ({ page }) => {
    await openQueue(page, deployment, "#older=1");
    const body = await catalog(page);
    for (const name of PLAN_NAMES) {
      const plan = rowFor(body.plans, name);
      const heading = await renderedProject(page, plan);
      expect(heading, name).toBeTruthy();
      // The heading is the plan's own grouping, not one the browser invented
      // from whichever project sorted first.
      expect(heading, name).toBe(plan.project_group);
    }
  });


  test("a project heading is drawn once and names itself", async ({
    page,
  }) => {
    await openQueue(page, deployment, "#older=1");
    const groups = await page
      .locator(".wb-project-group")
      .evaluateAll((nodes) =>
        nodes.map((node) => ({
          project: node.getAttribute("data-project"),
          heading: (
            node.querySelector("[data-group-heading]").textContent || ""
          ).trim(),
          rows: node.querySelectorAll(".wb-row").length,
        })),
      );
    expect(groups.length).toBeGreaterThan(0);
    for (const group of groups) {
      expect(group.heading).toBe(group.project);
      // An empty heading is a heading that outlived its rows.
      expect(group.rows).toBeGreaterThan(0);
    }
    // One heading per project *per section*: the sections are separate
    // lists, so the same project may head a group in each.
    const perSection = await page
      .locator("#wb-queue .wb-section")
      .evaluateAll((nodes) =>
        nodes.map((node) =>
          Array.from(node.querySelectorAll(".wb-project-group")).map((group) =>
            group.getAttribute("data-project"),
          ),
        ),
      );
    for (const names of perSection) {
      expect(new Set(names).size).toBe(names.length);
    }
  });


  // -- Facets, filtering and the URL ------------------------------------

  test("the facet selects offer what the catalog holds", async ({ page }) => {
    await openQueue(page, deployment);
    const body = await catalog(page);
    const projects = await page
      .locator("#wb-project-filter option")
      .evaluateAll((nodes) => nodes.map((node) => node.value));
    // A leading blank for "all projects", then the server's facet list --
    // taken from the unfiltered catalog, so the operator can always leave.
    expect(projects).toEqual([""].concat(body.projects));
    const harnesses = await page
      .locator("#wb-harness-filter option")
      .evaluateAll((nodes) => nodes.map((node) => node.value));
    expect(harnesses).toEqual([""].concat(body.harnesses));
  });


  test("choosing a project narrows the queue and lands in the URL", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const body = await catalog(page);
    const project = body.projects[0];
    expect(project, "the fixture publishes no project to filter by").toBeTruthy();

    await page.locator("#wb-project-filter").selectOption(project);
    await hashParam(page, "project").toBe(project);
    await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
      "data-state",
      "idle",
    );
    // Every seeded plan is in this project, so the queue is unchanged in
    // content -- what is being asserted is that the narrowing round-tripped
    // through the server rather than being applied to a page in the browser.
    await expect(page.locator(".wb-row")).toHaveCount(PLAN_NAMES.length);
    const narrowed = await catalog(
      page,
      `?project=${encodeURIComponent(project)}`,
    );
    expect(narrowed.filters.project).toBe(project);
    expect(narrowed.catalog_matched).toBe(PLAN_NAMES.length);
  });


  test("a project nothing is in empties the queue and says why", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    await page.evaluate(() => {
      const select = document.getElementById("wb-project-filter");
      const option = document.createElement("option");
      option.value = "nothing-is-here";
      option.textContent = "nothing-is-here";
      select.appendChild(option);
    });
    await page.locator("#wb-project-filter").selectOption("nothing-is-here");
    await expect(page.locator(".wb-row")).toHaveCount(0);
    // "No plans match this filter" and "there are no plans" are different
    // sentences, and only the first comes with something to undo.
    await expect(page.locator("#wb-empty")).toBeVisible();
    await expect(page.locator("#wb-empty-reset-filter")).toBeVisible();
  });


  test("a search narrows the queue to the matching plan", async ({ page }) => {
    await openQueue(page, deployment, "#older=1");
    const charlie = rowFor((await catalog(page)).plans, "charlie");
    await page.locator("#wb-search").fill("charlie");
    await expect(page.locator(".wb-row")).toHaveCount(1);
    await expect(
      page.locator(`.wb-row[data-plan-dir="${addressOf(charlie)}"]`),
    ).toBeVisible();
    await expect(page).toHaveURL(/q=charlie/);
  });


  test("a link carrying filters opens the view it describes", async ({
    page,
  }) => {
    await openQueue(page, deployment, "#q=charlie&older=1&sort=label", 1);
    await expect(page.locator("#wb-search")).toHaveValue("charlie");
    await expect(page.locator("#wb-sort")).toHaveValue("label");
    await expect(page.locator("#wb-older-toggle")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(page.locator(".wb-row")).toHaveCount(1);
  });


  test("a label sort orders rows by the label they display", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const order = await page.evaluate(async () => {
      const queue = await import("/static/modules/queue.js");
      // A Pi row carries neither `label` nor `plan_dir`, and a Mac row for
      // an unnamed plan carries a null `label`: both display a fallback, and
      // the sort has to read the same one or it orders nothing.
      const rows = [
        { plan_key: "k1", display_label: "zeta", urgency_rank: 10 },
        { plan_dir: "/z/plans/x", label: null, title: "Beta", urgency_rank: 10 },
        { plan_key: "k2", display_label: "alpha", urgency_rank: 50 },
      ];
      return rows
        .sort((a, b) => queue.comparePlanRowsFor("label", a, b))
        .map((row) => row.display_label || row.title);
    });
    expect(order).toEqual(["alpha", "Beta", "zeta"]);
  });


  test("every sort mode the shell offers is one the server accepts", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const offered = await page
      .locator("#wb-sort option")
      .evaluateAll((nodes) => nodes.map((node) => node.value));
    for (const mode of offered) {
      const body = await catalog(page, `?sort=${encodeURIComponent(mode)}`);
      expect(body.filters.sort, mode).toBe(mode);
      expect(body.plans.length, mode).toBe(PLAN_NAMES.length);
    }
  });


  test("the default view asks the server to hide completed work", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    const asked = await page.evaluate(
      () =>
        new Promise((resolve) => {
          const original = window.fetch;
          window.fetch = function (input, init) {
            const url = String(
              typeof input === "string" ? input : input.url || "",
            );
            if (url.indexOf("/api/queue") !== -1) {
              window.fetch = original;
              resolve(url);
            }
            return original.call(window, input, init);
          };
        }),
    );
    expect(asked).toContain("include_completed=0");
    // The narrowing is the view's opinion, so the view is what carries it: a
    // bare `GET /api/queue` still answers with everything.
    const bare = await catalog(page);
    expect(bare.filters.include_completed).toBe(true);
  });


  test("show-completed widens the request rather than the page", async ({
    page,
  }) => {
    await openQueue(page, deployment);
    await page.locator("#wb-show-completed").check();
    await expect(page).toHaveURL(/completed=1/);
    await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
      "data-state",
      "idle",
    );
    await expect(page.locator(".wb-row")).toHaveCount(PLAN_NAMES.length);
  });
};
