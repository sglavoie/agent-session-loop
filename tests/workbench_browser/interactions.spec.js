/* The mutating half of the matrix, driven by a real browser.
 *
 * `tests/test_workbench_interactions.py` proves what each deployment will
 * carry out and what it is entitled to claim. None of that is visible from
 * Python: whether the sentence reaches the operator, whether the keyboard
 * comes back after a confirm dialog, whether a recorded-but-unrun operation is
 * still on screen after a reload, and whether any of it fits on a phone are
 * facts about a rendered page. This file is where they are asserted.
 *
 * Like the other specs here, every test runs against both deployments from
 * one body and asks the payload which mode it is looking at rather than
 * branching on a name -- because "one interface, two deployments" is the claim
 * under test, and a test written twice would be evidence against it. It also
 * runs at all three viewports, so the stacked phone shell is exercised by the
 * same assertions as the two-pane one.
 *
 * Two things are deliberately never done here.
 *
 * **Nothing launches a run.** In local mode `/api/launch` spawns a real `asl
 * run` against the fixture corpus, and a browser suite that started a harness
 * would be a browser suite that left one behind. Launch is asserted through
 * its disabled state and its refusal, which is where all of its interface
 * behaviour lives anyway.
 *
 * **Nothing purges.** The destructive path is driven up to the gate -- the
 * expired-entry review -- and the gate is what is asserted. A spec that got
 * past it would be deleting the fixture other specs are reading.
 */

const { test, expect } = require("@playwright/test");
const manifest = require("./scenarios.json");

const DEPLOYMENTS = Object.keys(manifest.deployments).map((name) => ({
  name: name,
  address: manifest.deployments[name].address,
  baseURL: `http://${manifest.host}:${manifest.deployments[name].port}`,
}));

const PLAN_STATES = manifest.plans;
const ARCHIVE = manifest.archive;

/** The key the shell stores operation receipts under. */
const RECEIPTS_KEY = "wb.operations";

function isDesktop(page) {
  const size = page.viewportSize();
  return !!size && size.width >= manifest.desktop_min_width;
}

/**
 * Where the mutating routes are reachable from.
 *
 * The local deployment holds a secret and the remote one trusts the identity
 * header Tailscale Serve stamped (supplied for every request by the config).
 * The token goes in the query string exactly as an operator's bookmark carries
 * it; `prefs.js` moves it into session storage and strips it from the URL.
 */
function authorizedURL(deployment, suffix) {
  const query =
    deployment.address === "plan_dir"
      ? `?token=${encodeURIComponent(manifest.token)}`
      : "";
  return `${deployment.baseURL}/${query}${suffix || ""}`;
}

async function catalog(page) {
  const body = await page.evaluate(async () => {
    const response = await fetch("/api/queue");
    return response.json();
  });
  expect(body.ok).toBe(true);
  return body.plans;
}

function rowFor(plans, name) {
  const wanted = PLAN_STATES[name].primary_state;
  const match = plans.find((plan) => plan.primary_state === wanted);
  expect(match, `no ${wanted} plan in the catalog`).toBeTruthy();
  return match;
}

function detailHash(plan) {
  if (plan.plan_dir) return `#plan?dir=${encodeURIComponent(plan.plan_dir)}`;
  return `#plan?key=${encodeURIComponent(plan.plan_key)}`;
}

/** What this deployment says it can carry out, read through the page. */
async function capabilities(page) {
  const body = await page.evaluate(async () => {
    const response = await fetch("/api/capabilities");
    return response.json();
  });
  expect(body.ok).toBe(true);
  const byOperation = {};
  for (const entry of body.operations) byOperation[entry.operation] = entry;
  return { mode: body.mode, byOperation: byOperation };
}

/** Load the queue at an address that may carry out mutations. */
async function openQueue(page, deployment) {
  await page.goto(authorizedURL(deployment));
  await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
    "data-state",
    "idle",
  );
}

/**
 * Open the archive view and wait for its read to have finished.
 *
 * Settled rather than *succeeded*: on the Pi the archive is not published at
 * all, so the read ends in `error` and the view says so. Waiting for `idle`
 * here would be waiting for one deployment to behave like the other.
 */
async function openArchive(page, deployment) {
  await page.goto(authorizedURL(deployment, "#archive"));
  await expect(page.locator("#wb-archive")).toBeVisible();
  await expect(page.locator("#wb-refresh-status")).toHaveAttribute(
    "data-state",
    /idle|error/,
  );
}

/** Open one plan's detail view, authorized, and wait for the first poll. */
async function openDetail(page, deployment, name) {
  await openQueue(page, deployment);
  const plan = rowFor(await catalog(page), name);
  await page.goto(authorizedURL(deployment, detailHash(plan)));
  await expect(page.locator("#wb-shell")).toHaveAttribute(
    "data-route",
    "detail",
  );
  await expect(page.locator("#wb-detail-state")).toHaveAttribute(
    "data-state",
    PLAN_STATES[name].primary_state,
  );
  // The capability read is fired at startup and not awaited by anything on
  // screen, so the controls are enabled for an instant before it lands. Every
  // assertion below is about what it decided, so wait for it to have decided.
  await page.waitForFunction(() => {
    const button = document.getElementById("wb-control-unlock");
    return !!button && button.hasAttribute("data-operation-action");
  });
  return plan;
}

/** Every receipt this tab has written down, as it would survive a reload. */
async function storedReceipts(page) {
  return page.evaluate((key) => {
    const raw = window.sessionStorage.getItem(key);
    return raw ? JSON.parse(raw) : [];
  }, RECEIPTS_KEY);
}

/** Wait until an operation of this kind has been written down, and return it. */
async function receiptFor(page, action) {
  await expect
    .poll(async () => (await storedReceipts(page)).some((r) => r.action === action))
    .toBe(true);
  const receipts = await storedReceipts(page);
  return receipts.find((receipt) => receipt.action === action);
}

for (const deployment of DEPLOYMENTS) {
  test.describe(`${deployment.name} deployment`, () => {
    test("a control this mode cannot carry out says so in the mode's own words", async ({
      page,
    }) => {
      await openDetail(page, deployment, "alpha");
      const { byOperation } = await capabilities(page);
      const launch = page.locator("#wb-control-launch");
      // Disabled in both modes, for two different reasons -- which is the
      // whole point of separating them. The Pi does not spawn runs at all; the
      // laptop does, but not while this plan's run is still in flight.
      await expect(launch).toBeDisabled();
      const reason = await launch.getAttribute("data-disabled-reason");
      expect(reason, "a disabled control must explain itself").toBeTruthy();
      if (byOperation.launch.available) {
        expect(reason).toBe(
          "a run is already in flight for this plan; stop it before launching another",
        );
      } else {
        // Word for word what clicking it would have answered. Anything else
        // and the greyed-out control is explaining the wrong thing.
        expect(reason).toBe(byOperation.launch.reason);
      }
    });

    test("a disabled control's reason is readable without a pointer", async ({
      page,
    }) => {
      // A `title` is a hover tooltip, and the phone is half of where this
      // interface runs. The reason is carried as an attribute the layout can
      // render, so a touch-only operator is not the one operator who cannot
      // find out why a button is dead.
      await openDetail(page, deployment, "alpha");
      const disabled = page.locator(
        "#wb-detail [data-operation-action]:disabled",
      );
      const count = await disabled.count();
      expect(count, "alpha should have at least one shut control").toBeGreaterThan(0);
      for (let index = 0; index < count; index += 1) {
        const node = disabled.nth(index);
        await expect(node).toHaveAttribute("data-disabled-reason", /\S/);
      }
    });

    test("a reply form with no question behind it says so on the button", async ({
      page,
    }) => {
      // The paused-Q&A context is a second read, and it can fail on its own:
      // the question gets answered elsewhere, or the read never lands. The
      // form shuts either way, and the sentence has to end up somewhere the
      // operator is already looking -- the prompt box it used to go to alone
      // is inside a collapsed `<details>`.
      await page.route("**/api/questions*", (route) =>
        route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            ok: false,
            reason: "no-paused-question",
            error: "this plan is not waiting on an answer",
          }),
        }),
      );
      await openDetail(page, deployment, "alpha");
      const form = page.locator("#wb-reply-form");
      await expect(form).toBeVisible();
      const send = form.locator("button[type=\"submit\"]");
      await expect(send).toBeDisabled();
      await expect(send).toHaveAttribute(
        "data-disabled-reason",
        /this plan is not waiting on an answer/,
      );
    });

    test("an unanswered-question run link opens the live reply form", async ({
      page,
    }) => {
      await openQueue(page, deployment);
      const plan = rowFor(await catalog(page), "charlie");
      expect(plan.run_key).toBeTruthy();

      await page.route("**/api/plan*", async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        body.runs = [
          {
            run_key: plan.run_key,
            question_id: "live-question-1",
            unanswered_count: 1,
          },
        ];
        await route.fulfill({ response: response, json: body });
      });
      await page.route("**/api/questions*", (route) =>
        route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            ok: true,
            reason: "ok",
            context_kind: "waiting",
            run_id: "run-live-1",
            question_id: "live-question-1",
            action_token: "live-action-token",
            rendered_prompt: "Confirm the locked-phone alert.",
            last_message: "Confirm the locked-phone alert.",
          }),
        }),
      );

      await page.goto(
        authorizedURL(
          deployment,
          `#run=${encodeURIComponent(plan.run_key)}&focus=reply`,
        ),
      );
      await expect(page.locator("#wb-shell")).toHaveAttribute(
        "data-route",
        "detail",
      );
      const form = page.locator("#wb-reply-form");
      await expect(form).toBeVisible();
      await expect(form.locator('button[type="submit"]')).toHaveText(
        "send reply",
      );
      await expect(page.locator("#wb-reply-text")).toBeFocused();
      await expect(page.locator("#wb-redirect-submit")).toHaveText(
        "interrupt turn & resume with this correction",
      );
    });

    test("a control aimed at a run that is not there names the run, not the mode", async ({
      page,
    }) => {
      // `bravo` has never run. Stop and abort are perfectly available
      // operations pointed at nothing, and saying "this deployment cannot do
      // that" would be false in both modes.
      await openDetail(page, deployment, "bravo");
      await expect(page.locator("#wb-control-stop")).toBeDisabled();
      await expect(page.locator("#wb-control-stop")).toHaveAttribute(
        "data-disabled-reason",
        "no run is in flight to stop",
      );
      await expect(page.locator("#wb-control-abort")).toHaveAttribute(
        "data-disabled-reason",
        "no run is in flight to abort",
      );
    });

    test("a confirm dialog gives the keyboard back to the control that opened it", async ({
      page,
    }) => {
      // `window.confirm` hands focus to the document body. Without the capture
      // and restore around it, every confirmed control drops the keyboard at
      // exactly the moment a keyboard operator is using one.
      await openDetail(page, deployment, "charlie");
      page.once("dialog", (dialog) => dialog.dismiss());
      const unlock = page.locator("#wb-control-unlock");
      await unlock.focus();
      // Driven from the keyboard rather than with a click, because that is
      // whose problem this is -- and because WebKit deliberately does not
      // leave focus on a button a mouse pressed, so a click here would be
      // asserting the browser's focus policy instead of the shell's.
      await unlock.press("Enter");
      await expect
        .poll(async () => page.evaluate(() => document.activeElement.id))
        .toBe("wb-control-unlock");
    });

    test("a submission that gets no answer is reported as unknown and keeps its id", async ({
      page,
    }) => {
      // The hardest answer to render honestly, and the one a reload most needs
      // to survive: nobody knows whether it arrived. It may not be drawn as a
      // failure, because a retry under a fresh id could be a second unlock.
      await openDetail(page, deployment, "charlie");
      await page.route("**/api/unlock", (route) => route.abort());
      page.once("dialog", (dialog) => dialog.accept());
      await page.locator("#wb-control-unlock").click();

      const feedback = page.locator("#wb-controls-feedback");
      await expect(feedback).toBeVisible();
      // Not "error": a refusal is an answer and this is the absence of one.
      await expect(feedback).toHaveAttribute("data-kind", "warning");
      await expect(feedback).toContainText("no answer");

      const receipt = await receiptFor(page, "unlock");
      expect(receipt.outcome).toBe("unreachable");
      expect(receipt.settled).toBe(false);
      expect(receipt.operation_id).toBeTruthy();
      // The privacy line the receipt store draws: an identity, never words.
      expect(Object.keys(receipt).sort()).toEqual([
        "action",
        "operation_id",
        "outcome",
        "plan",
        "reason",
        "settled",
        "status",
      ]);

      // And it is still on screen after the page is thrown away and reloaded,
      // which is the only state in which an operator would go looking for it.
      const panel = page.locator("#wb-operations-panel");
      await expect(panel).toBeVisible();
      await page.reload();
      await expect(panel).toBeVisible();
      await expect(
        page.locator(
          `#wb-operations-list [data-operation-id="${receipt.operation_id}"]`,
        ),
      ).toBeVisible();
      await expect(panel).toContainText("retrying reuses this id");
    });

    test("an open operation is dismissible without being claimed as done", async ({
      page,
    }) => {
      await openDetail(page, deployment, "charlie");
      await page.route("**/api/unlock", (route) => route.abort());
      page.once("dialog", (dialog) => dialog.accept());
      await page.locator("#wb-control-unlock").click();
      const receipt = await receiptFor(page, "unlock");

      const row = page.locator(
        `#wb-operations-list [data-operation-id="${receipt.operation_id}"]`,
      );
      await expect(row).toBeVisible();
      await row.locator('[data-action="operation-dismiss"]').click();
      // Forgotten by this tab, and never marked settled: this front end is in
      // no position to say the operation ran.
      await expect(row).toHaveCount(0);
      expect(
        (await storedReceipts(page)).some(
          (entry) => entry.operation_id === receipt.operation_id,
        ),
      ).toBe(false);
    });

    test("bulk cleanup is a review before it is an action", async ({ page }) => {
      // The bar exists to make archiving many plans one reviewed decision
      // rather than many unreviewed ones, so its archive button starts shut
      // and says which of the two reasons applies.
      await openQueue(page, deployment);
      const { byOperation } = await capabilities(page);
      await page.locator("#wb-cleanup-toggle").click();
      const bar = page.locator("#wb-cleanup-bar");
      await expect(bar).toBeVisible();

      const archive = page.locator("#wb-cleanup-archive");
      await expect(archive).toBeDisabled();
      await expect(archive).toHaveAttribute(
        "data-disabled-reason",
        byOperation["plans-archive"].available
          ? "no plans are selected"
          : byOperation["plans-archive"].reason,
      );

      // Selecting is a read: it fills the review list and arms nothing. The
      // count is what the operator is being asked to agree with.
      await page.locator("#wb-cleanup-select-visible").click();
      await expect(page.locator("#wb-cleanup-count")).not.toContainText(
        "0 plans selected",
      );
      if (byOperation["plans-archive"].available) {
        await expect(archive).toBeEnabled();
      } else {
        // Still shut on the Pi, and now for the mode's reason rather than the
        // selection's -- the nearer explanation stepped aside for the truer one.
        await expect(archive).toBeDisabled();
        await expect(archive).toHaveAttribute(
          "data-disabled-reason",
          byOperation["plans-archive"].reason,
        );
      }
      await page.locator("#wb-cleanup-clear").click();
      await expect(page.locator("#wb-cleanup-count")).toContainText(
        "0 plans selected",
      );
    });

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
  });
}
