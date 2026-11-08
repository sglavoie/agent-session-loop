/* interactions: live reply. */
const {
  test,
  expect,
  authorizedURL,
  catalog,
  rowFor,
  capabilities,
  openQueue,
} = require("./interactions_support.js");

module.exports = function register(deployment) {

  test("an unanswered-question run link opens the live reply form", async (
    { page },
    testInfo,
  ) => {
    await openQueue(page, deployment);
    const plan = rowFor(await catalog(page), "charlie");
    expect(plan.run_key).toBeTruthy();

    let servedQuestion = "live-question-1";
    let questionClosed = false;
    let transientQuestionRefusals = 2;
    const requestedQuestionIds = [];
    await page.route("**/api/questions*", (route) => {
      requestedQuestionIds.push(
        new URL(route.request().url()).searchParams.get("question_id"),
      );
      if (transientQuestionRefusals > 0) {
        transientQuestionRefusals -= 1;
        if (transientQuestionRefusals === 1) {
          return route.fulfill({
            status: 504,
            contentType: "application/json",
            body: JSON.stringify({
              ok: false,
              reason: "detail-timeout",
              error: "no answer before the deadline",
            }),
          });
        }
        return route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            ok: false,
            reason: "detail-refused",
            error: "detail-refused",
            details: { outcome: "refused", reason: "not-found" },
          }),
        });
      }
      if (questionClosed) {
        return route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            ok: false,
            reason: "detail-refused",
            error: "detail-refused",
            details: { outcome: "refused", reason: "not-found" },
          }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          reason: "ok",
          context_kind: "waiting",
          run_id: "run-live-1",
          question_id: servedQuestion,
          action_token: "live-action-token",
          rendered_prompt: "Confirm " + servedQuestion + ".",
          last_message: "Confirm " + servedQuestion + ".",
        }),
      });
    });
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
    let replyAttempts = 0;
    await page.route(/\/api\/question-reply(?:\?|$)/, async (route) => {
      replyAttempts += 1;
      const request = route.request();
      const payload = request.postDataJSON();
      if (payload.reply_text === "Immediately consumed reply.") {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            ok: true,
            reason: "consumed",
            operation: {
              operation_id: "immediate-consumption-operation",
              status: "consumed",
              method: "question.reply",
            },
          }),
        });
        return;
      }
      if (payload.reply_text === "Receipt-less reply.") {
        await route.fulfill({
          status: 202,
          contentType: "application/json",
          body: JSON.stringify({
            ok: true,
            reason: "accepted",
          }),
        });
        return;
      }
      if (replyAttempts === 1) {
        await route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            ok: false,
            reason: "question-mismatch",
            error: "reply was not recorded",
          }),
        });
        return;
      }
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          ok: true,
          reason: "accepted",
          operation: {
            operation_id: payload.operation_id,
            status: "accepted",
            method: "question.reply",
          },
        }),
      });
    });

    await page.goto(
      authorizedURL(
        deployment,
        `#run=${encodeURIComponent(plan.run_key)}&focus=reply&question=live-question-1`,
      ),
    );
    await expect(page.locator("#wb-shell")).toHaveAttribute(
      "data-route",
      "detail",
    );
    await expect(page.locator("body")).toHaveClass(/wb-question-bound/);
    await expect(page.locator("#wb-shell")).toHaveAttribute(
      "data-question-bound",
      "true",
    );
    await expect(page.locator("#wb-queue")).toBeHidden();
    await expect(page.locator("#wb-detail > :visible")).toHaveCount(1);
    await expect(
      page.locator("#wb-recovery-panel > :visible"),
    ).toHaveCount(1);
    await expect(page.locator("#wb-reply-panel")).toBeVisible();
    await expect(page.locator("#wb-redirect-panel")).toBeHidden();
    await expect
      .poll(() => new URL(page.url()).hash)
      .toContain("question=live-question-1");
    const form = page.locator("#wb-reply-form");
    // The exact-question read is the entire notification view. No plan
    // payload is needed, and no plan panel can introduce another question
    // while the operator is answering this one.
    await expect(form).toBeVisible({ timeout: 2500 });
    const send = form.locator('button[type="submit"]');
    const activateSend = () =>
      testInfo.project.name === "mobile-webkit" ? send.tap() : send.click();
    await expect(send).toHaveText("send reply");
    await expect(send).toBeEnabled();
    await expect(send).not.toHaveAttribute("data-disabled-reason");
    await expect(page.locator("#wb-reply-prompt-details")).toHaveAttribute(
      "open",
      "",
    );
    await expect(page.locator("#wb-reply-prompt")).toHaveText(
      "Confirm live-question-1.",
    );
    await expect(page.locator("#wb-reply-submit-status")).toHaveText(
      "Current question loaded from this notification.",
    );
    expect(requestedQuestionIds).not.toContain(null);
    expect(requestedQuestionIds).toContain("live-question-1");
    await expect(page.locator("#wb-reply-text")).toBeFocused();

    let detailPolls = 0;
    const countDetailPoll = (request) => {
      if (new URL(request.url()).pathname === "/api/plan") detailPolls += 1;
    };
    page.on("request", countDetailPoll);
    await page.waitForTimeout(5500);
    page.off("request", countDetailPoll);
    expect(detailPolls).toBe(0);

    const unchangedRefreshMutations = await page.evaluate(async () => {
      const form = document.getElementById("wb-reply-form");
      const mutations = [];
      const observer = new MutationObserver((records) => {
        for (const record of records) {
          mutations.push({
            type: record.type,
            attribute: record.attributeName,
            target: record.target.id || record.target.tagName,
          });
        }
      });
      observer.observe(form, {
        attributes: true,
        childList: true,
        characterData: true,
        subtree: true,
      });
      const panels = await import("/static/modules/panels.js");
      await panels.loadAndRenderReply();
      observer.disconnect();
      return mutations;
    });
    expect(unchangedRefreshMutations).toEqual([]);

    // A later alert for this same run changes only the question identity.
    // The old interactive form must stop being authoritative immediately;
    // otherwise its own anti-poll guard freezes the tab on that old question
    // until the operator manually reloads it.
    await page.evaluate(() => {
      const params = new URLSearchParams(
        window.location.hash.substring(window.location.hash.indexOf("?") + 1),
      );
      params.set("question", "live-question-2");
      window.location.hash = "#plan?" + params.toString();
    });
    await expect(page.locator("#wb-reply-submit-status")).toHaveText(
      "Opening the current question…",
    );
    await expect(page.locator("#wb-reply-prompt-details")).toHaveAttribute(
      "open",
      "",
    );
    await expect(form).toBeHidden();

    // The connector projection catches up after the first question read.
    // The scheduled detail poll must adopt it without a page reload.
    servedQuestion = "live-question-2";
    await expect(form).toBeVisible({ timeout: 3000 });
    expect(requestedQuestionIds).toContain("live-question-2");
    await expect(page.locator("#wb-reply-prompt")).toHaveText(
      "Confirm live-question-2.",
    );
    await expect(send).toBeEnabled();

    await activateSend();
    await expect(page.locator("#wb-reply-form-errors")).toBeVisible();
    await expect(page.locator("#wb-reply-form-errors")).toHaveText(
      "reply must not be empty",
    );

    await page.locator("#wb-reply-text").fill("Phone acceptance confirmed.");
    page.once("dialog", (dialog) => dialog.accept());
    await activateSend();
    const status = page.locator("#wb-reply-submit-status");
    await expect(status).toBeVisible();
    await expect(status).toHaveAttribute("data-kind", "error");
    await expect(status).toHaveText("reply was not recorded (question-mismatch)");
    await expect(page.locator("#wb-reply-text")).toHaveValue(
      "Phone acceptance confirmed.",
    );
    await expect(send).toHaveText("send reply");
    await expect(send).toBeEnabled();

    await page.locator("#wb-reply-text").fill("Receipt-less reply.");
    page.once("dialog", (dialog) => dialog.accept());
    await activateSend();
    await expect(status).toHaveAttribute("data-kind", "error");
    await expect(status).toHaveText(
      "reply was not recorded: the server returned no durable operation receipt",
    );
    await expect(page.locator("#wb-reply-form-errors")).toHaveText(
      "reply was not recorded: the server returned no durable operation receipt",
    );
    await expect(page.locator("#wb-reply-text")).toHaveValue(
      "Receipt-less reply.",
    );
    await expect(send).toHaveText("send reply");
    await expect(send).toBeEnabled();

    await page.locator("#wb-reply-text").fill("Phone acceptance confirmed.");
    page.once("dialog", (dialog) => dialog.accept());
    await activateSend();
    await expect(status).toHaveAttribute("data-kind", "pending");
    await expect(status).toHaveText(
      /Reply recorded for question live-question-2 as operation [a-f0-9]+\. Waiting for the running session to consume it\./,
    );
    await expect(send).toHaveText("reply recorded");
    await expect(send).toBeDisabled();
    await expect(page.locator("#wb-reply-text")).toHaveValue("");

    // Safari may reload a notification tab while the connector is still
    // settling its accepted operation. The question and operation ids are
    // safe receipt metadata, so the page must restore the recorded state
    // instead of reopening the form or replacing it with a detail error.
    await page.reload();
    await expect(status).toHaveAttribute("data-kind", "pending");
    await expect(status).toHaveText(
      /Reply recorded for question live-question-2 as operation [a-f0-9]+\. Waiting for the running session to consume it\./,
    );
    await expect(send).toHaveText("reply recorded");
    await expect(send).toBeDisabled();

    // Once ASL consumes the durable reply, the exact question is no longer
    // a readable remote detail. That expected not-found must confirm closure
    // instead of replacing the recorded state with a detail-refused error.
    await page.evaluate(async () => {
      const detail = await import("/static/modules/detail.js");
      detail.stopDetailTimer();
    });
    await expect
      .poll(() =>
        page.evaluate(async () => {
          const { state } = await import("/static/modules/state.js");
          return state.detailInFlight;
        }),
      )
      .toBe(false);
    questionClosed = true;
    await page.evaluate(async () => {
      const detail = await import("/static/modules/detail.js");
      await detail.refreshDetail();
    });
    await expect(status).toHaveAttribute("data-kind", "info");
    await expect(status).toHaveText(
      "Reply delivered. The question is closed and the run resumed.",
    );
    await expect(page.locator("#wb-detail > :visible")).toHaveCount(1);
    await expect(
      page.locator("#wb-recovery-panel > :visible"),
    ).toHaveCount(1);
    await expect(page.locator("#wb-reply-form-errors")).toBeHidden();

    // A connector can consume the reply before the POST returns. That 200
    // result must establish the same durable question identity as a 202,
    // so the immediately following not-found read remains a delivered
    // state while keeping the notification view single-purpose.
    questionClosed = false;
    servedQuestion = "live-question-3";
    await page.evaluate(() => {
      const params = new URLSearchParams(
        window.location.hash.substring(window.location.hash.indexOf("?") + 1),
      );
      params.set("question", "live-question-3");
      window.location.hash = "#plan?" + params.toString();
    });
    await expect(form).toBeVisible({ timeout: 3000 });
    await page.locator("#wb-reply-text").fill("Immediately consumed reply.");
    page.once("dialog", (dialog) => dialog.accept());
    await activateSend();
    await expect(status).toHaveAttribute("data-kind", "info");
    await expect(status).toHaveText(
      "Reply delivered. The question is closed and the run resumed.",
    );
    await expect(form).toBeHidden();

    questionClosed = true;
    await page.evaluate(async () => {
      const panels = await import("/static/modules/panels.js");
      await panels.loadAndRenderReply();
    });
    await expect(status).toHaveAttribute("data-kind", "info");
    await expect(status).toHaveText(
      "Reply delivered. The question is closed and the run resumed.",
    );

    // A genuinely stale notification eventually stops retrying, but it
    // explains that state in operator language instead of exposing the
    // connector's detail-refused transport code.
    servedQuestion = "live-question-3";
    await page.evaluate(() => {
      const params = new URLSearchParams(
        window.location.hash.substring(window.location.hash.indexOf("?") + 1),
      );
      params.set("question", "live-question-4");
      window.location.hash = "#plan?" + params.toString();
    });
    await expect(status).toHaveText("Opening the current question…");
    await page.evaluate(async () => {
      const { state } = await import("/static/modules/state.js");
      state.reply.openingStartedAt = Date.now() - 20000;
      const panels = await import("/static/modules/panels.js");
      await panels.loadAndRenderReply();
    });
    await expect(page.locator("#wb-reply-form-errors")).toHaveText(
      "This alert's question is no longer available. Open the latest notification or return to the queue.",
    );
    await expect(page.locator("#wb-reply-form-errors")).not.toContainText(
      "detail-refused",
    );
    expect(requestedQuestionIds).not.toContain(null);
  });
};
