/* interactions: mutations. */
const {
  test,
  expect,
  PLAN_STATES,
  catalog,
  rowFor,
  detailHash,
  capabilities,
  openDetail,
  storedReceipts,
  receiptFor,
} = require("./interactions_support.js");

module.exports = function register(deployment) {

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
      "question_id",
      "reason",
      "settled",
      "status",
    ]);
    expect(receipt.question_id).toBeNull();

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


  test("a retry of an unanswered submission resends the same id", async ({
    page,
  }) => {
    // The feedback promises that retrying reuses the id. Each click builds a
    // fresh body, so the promise only holds if the outstanding receipt lends
    // its id to the second attempt.
    await openDetail(page, deployment, "charlie");
    const sent = [];
    await page.route("**/api/unlock", (route) => {
      sent.push(route.request().postDataJSON());
      return route.abort();
    });
    page.on("dialog", (dialog) => dialog.accept());
    const unlock = page.locator("#wb-control-unlock");
    await unlock.click();
    await expect.poll(() => sent.length).toBe(1);
    const receipt = await receiptFor(page, "unlock");
    await expect(page.locator("#wb-controls-feedback")).toContainText(
      "retrying reuses operation id " + receipt.operation_id,
    );
    await expect(unlock).toBeEnabled();
    await unlock.click();
    await expect.poll(() => sent.length).toBe(2);
    expect(sent[0].operation_id).toBe(receipt.operation_id);
    expect(sent[1].operation_id).toBe(receipt.operation_id);
  });


  test("a reply answered after the operator moved on stays with its own plan", async ({
    page,
  }) => {
    // The POST is held until the route has moved to another plan. What it
    // answers belongs to the plan it was sent for: the receipt is filed
    // there, and the plan now on screen keeps its own draft and form.
    await page.route("**/api/capabilities*", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      for (const operation of body.operations || []) {
        if (operation.operation !== "reply") continue;
        operation.available = true;
        operation.reason = null;
      }
      body.unavailable_operations = (body.unavailable_operations || []).filter(
        (operation) => operation !== "reply",
      );
      await route.fulfill({ response: response, json: body });
    });
    const alpha = await openDetail(page, deployment, "alpha");
    const bravo = rowFor(await catalog(page), "bravo");
    let release = null;
    const held = new Promise((resolve) => {
      release = resolve;
    });
    let sent = null;
    await page.route(/\/api\/(?:question-)?reply(?:\?|$)/, async (route) => {
      sent = route.request().postDataJSON();
      await held;
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          reason: "accepted",
          operation: {
            operation_id: sent.operation_id,
            status: "accepted",
            method: "run.reply",
          },
        }),
      });
    });
    page.on("dialog", (dialog) => dialog.accept());
    await page.evaluate(() => {
      document.getElementById("wb-reply-text").value = "Answer for alpha.";
      document.getElementById("wb-reply-form").requestSubmit();
    });
    await expect.poll(() => sent !== null).toBe(true);
    // Same-document navigation: the module state that held the request is
    // the one that has to keep its hands off the next plan.
    await page.evaluate((hash) => {
      window.location.hash = hash;
    }, detailHash(bravo));
    await expect(page.locator("#wb-detail-state")).toHaveAttribute(
      "data-state",
      PLAN_STATES.bravo.primary_state,
    );
    await page.evaluate(() => {
      document.getElementById("wb-reply-text").value = "Draft for bravo.";
    });
    release();
    const receipt = await receiptFor(page, "reply");
    expect(receipt.plan).toBe(alpha.plan_dir || alpha.plan_key);
    await expect(page.locator("#wb-reply-text")).toHaveValue("Draft for bravo.");
    await expect(page.locator("#wb-reply-form")).not.toHaveAttribute(
      "aria-busy",
      "true",
    );
    await expect(page.locator("#wb-reply-submit-status")).toBeHidden();
  });


  test("a correction answered after the operator moved on stays with its own plan", async ({
    page,
  }) => {
    await page.route("**/api/redirect-status*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          reason: "ok",
          action_token: "redirect-token",
          permitted_actions: ["run.redirect"],
          disabled_reasons: {},
          live_target: null,
          operations: [],
        }),
      }),
    );
    const alpha = await openDetail(page, deployment, "alpha");
    const bravo = rowFor(await catalog(page), "bravo");
    await expect(page.locator("#wb-redirect-submit")).toBeEnabled();
    let release = null;
    const held = new Promise((resolve) => {
      release = resolve;
    });
    let sent = null;
    await page.route("**/api/control/redirect", async (route) => {
      sent = route.request().postDataJSON();
      await held;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          reason: "ok",
          redirect: { operation_id: sent.operation_id, replayed: false },
        }),
      });
    });
    page.on("dialog", (dialog) => dialog.accept());
    await page.evaluate(() => {
      document.getElementById("wb-redirect-text").value = "Correction for alpha.";
      document.getElementById("wb-redirect-form").requestSubmit();
    });
    await expect.poll(() => sent !== null).toBe(true);
    await page.evaluate((hash) => {
      window.location.hash = hash;
    }, detailHash(bravo));
    await expect(page.locator("#wb-detail-state")).toHaveAttribute(
      "data-state",
      PLAN_STATES.bravo.primary_state,
    );
    await page.evaluate(() => {
      document.getElementById("wb-redirect-text").value = "Draft for bravo.";
    });
    release();
    const receipt = await receiptFor(page, "redirect");
    expect(receipt.plan).toBe(alpha.plan_dir || alpha.plan_key);
    expect(receipt.operation_id).toBe(sent.operation_id);
    await expect(page.locator("#wb-redirect-text")).toHaveValue("Draft for bravo.");
    await expect(page.locator("#wb-redirect-form-errors")).toBeHidden();
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
};
