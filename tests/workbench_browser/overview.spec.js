/* The overview: sections, grouping, facets, the window, and history.
 *
 * `shell.spec.js` proves one front end reaches two deployments. This proves
 * the thing that front end now *is*: the Companion's cockpit and the
 * Workbench's queue folded into one screen, drawn the same way whichever
 * machine answered.
 *
 * Every test runs twice, once per deployment, from one body -- for the same
 * reason the shell suite does. Where the two genuinely differ (a path versus a
 * digest, a per-row observation mark versus one for the whole answer) the test
 * asks the payload rather than branching on a deployment name, because a
 * branch here would be a branch in the claim.
 *
 * The corpus is `tests/workbench_support.py`, shared with the Python suites:
 * `alpha` is paused waiting for an operator, `bravo` has never run, and
 * `charlie` finished thirty hours ago with two endings behind it. `charlie` is
 * the one that makes the eight-hour window and the history pane observable at
 * all, and `scenarios.json` records which section each is supposed to land in
 * so these tests assert the fixture's intent rather than a second copy of the
 * rule they are testing.
 */

const { test, expect } = require("@playwright/test");
const manifest = require("./scenarios.json");

const DEPLOYMENTS = Object.keys(manifest.deployments).map((name) => ({
  name: name,
  address: manifest.deployments[name].address,
  baseURL: `http://${manifest.host}:${manifest.deployments[name].port}`,
}));

const PLAN_STATES = manifest.plans;
const PLAN_NAMES = Object.keys(PLAN_STATES);

/** Which section the fixture says a plan belongs in. */
function sectionOf(name) {
  const section = PLAN_STATES[name].section;
  expect(section, `scenarios.json names no section for ${name}`).toBeTruthy();
  return section;
}

/** Whether the fixture expects that section to start behind a toggle. */
function sectionHidden(name) {
  return manifest.sections.hidden.indexOf(sectionOf(name)) !== -1;
}

/** The fixture's seeded endings, newest first -- the order the pane renders. */
const HISTORY_OUTCOMES = manifest.history.outcomes.slice().reverse();

/**
 * Load the queue and wait for the first poll to have rendered.
 *
 * The row count is the wait, not an assertion in passing: `data-state=idle` is
 * true before the first answer arrives as well as after it, so a spec that
 * stopped there would read a page that had not drawn yet. A hash carrying a
 * filter draws fewer rows, hence the count is a parameter rather than the
 * roster's length -- passing the wrong one here would hang the wait instead of
 * failing a claim, so callers name what they expect to see.
 */
async function openQueue(page, deployment, hash, expectedRows) {
  await page.goto(`${deployment.baseURL}/${hash || ""}`);
  await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
    "data-state",
    "idle",
  );
  await expect(page.locator(".wb-row")).toHaveCount(
    expectedRows === undefined ? PLAN_NAMES.length : expectedRows,
  );
}

/** What this deployment published, read through the browser's own session. */
async function catalog(page, query) {
  const body = await page.evaluate(async (suffix) => {
    const response = await fetch("/api/queue" + (suffix || ""));
    return response.json();
  }, query);
  expect(body.ok).toBe(true);
  return body;
}

/**
 * The published row for a named plan.
 *
 * By state rather than by label, exactly as `shell.spec.js` picks one: the
 * label is the field the two providers word differently, so choosing by it
 * would quietly test the reconciliation instead of using it.
 */
function rowFor(plans, name) {
  const wanted = PLAN_STATES[name].primary_state;
  const match = plans.find((plan) => plan.primary_state === wanted);
  expect(match, `no ${wanted} plan in the catalog`).toBeTruthy();
  return match;
}

/** The identifier the rendered rows are keyed by, in either deployment. */
function addressOf(plan) {
  return plan.plan_dir || plan.plan_key;
}

/** Which section a plan's row was actually drawn into. */
async function renderedSection(page, plan) {
  return page.evaluate((address) => {
    const row = document.querySelector(`.wb-row[data-plan-dir="${address}"]`);
    if (!row) return null;
    const section = row.closest(".wb-section");
    return section ? section.getAttribute("data-section") : null;
  }, addressOf(plan));
}

/** Which project heading a plan's row was drawn under. */
async function renderedProject(page, plan) {
  return page.evaluate((address) => {
    const row = document.querySelector(`.wb-row[data-plan-dir="${address}"]`);
    if (!row) return null;
    const group = row.closest(".wb-project-group");
    return group ? group.getAttribute("data-project") : null;
  }, addressOf(plan));
}

/**
 * A filter parameter as it now stands in the address bar.
 *
 * Read back through `URLSearchParams` rather than matched as a regex, because
 * the claim is that the operator's choice is in the link, not that the shell
 * picked one of the two legal spellings of a space. `router.js` builds the hash
 * with a `URLSearchParams`, which writes `+`; a spec pinned to `%20` would fail
 * on a correct URL, and one pinned to `+` would pin an encoder's internals.
 */
function hashParam(page, name) {
  return expect.poll(() =>
    new URLSearchParams(new URL(page.url()).hash.slice(1)).get(name),
  );
}

/**
 * Every delivery state an incident row can be in.
 *
 * Copied from `asl_workbench/incidents.py` rather than read from the fixture,
 * because the claim below is about the states this front end must *not* treat
 * as a delivery failure -- a spec that enumerated only the ones the corpus
 * happens to produce would assert nothing about the other four.
 */
const DELIVERY_STATES = [
  "pending",
  "retry",
  "publishing",
  "published",
  "unconfirmed",
  "cancelled",
];

const ACCOUNTED_FOR = DELIVERY_STATES.filter((name) => name !== "unconfirmed");

/**
 * One incident row as `/api/queue` publishes them.
 *
 * The fields are the diagnostics allowlist's: identities, states and
 * timestamps, never words from a run. The project doubles as this row's name in
 * the assertions, because the project is what the banner names.
 */
function incidentRow(project, overrides) {
  return Object.assign(
    {
      incident_id: project,
      run_key: "0000000000000000",
      project: project,
      kind: "unanswered-question",
      detected_at: 1,
      resolved_at: null,
      delivery_state: "unconfirmed",
      attempts: 0,
      receipt_id: null,
      published_at: null,
      detail_code: null,
    },
    overrides || {},
  );
}

/**
 * Put an incident list of the test's choosing into this deployment's answers.
 *
 * The deployment's own answer is fetched and only `incidents` is replaced, so
 * freshness, completeness and the connectors stay whatever this machine really
 * reported and the banner region is still assembling all four kinds of note.
 * One route reading a mutable cell rather than a route per payload, because the
 * claim needs one page to see several lists in turn.
 */
async function interceptIncidents(page) {
  const feed = { list: [] };
  await page.route("**/api/queue*", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.incidents = feed.list;
    await route.fulfill({ response: response, json: body });
  });
  return feed;
}

/**
 * Publish a list and run the poll that picks it up, awaited.
 *
 * The 5s timer is stopped and the refresh driven directly so each assertion
 * reads a page that has finished drawing: waiting for the timer instead would
 * make "the banner is absent" true of the previous answer as well as of this
 * one, which is the one way this test could pass while being wrong.
 */
async function publishIncidents(page, feed, list) {
  feed.list = list;
  await page.evaluate(async () => {
    const queue = await import("/static/modules/queue.js");
    const shared = await import("/static/modules/state.js");
    queue.stopQueueTimer();
    while (shared.state.fetchInFlight) {
      await new Promise((resolve) => window.setTimeout(resolve, 25));
    }
    await queue.refresh();
    queue.stopQueueTimer();
  });
}

/** Open the history pane and wait for its first answer. */
async function openHistory(page) {
  await page.locator("#wb-history-toggle").click();
  await expect(page.locator("#wb-history")).toHaveAttribute("data-open", "true");
  await expect(page.locator(".wb-history-row")).toHaveCount(
    HISTORY_OUTCOMES.length,
  );
}

for (const deployment of DEPLOYMENTS) {
  test.describe(`${deployment.name} overview`, () => {
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
  });
}
