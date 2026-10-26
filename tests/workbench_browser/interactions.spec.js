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
      // laptop does, but not while this plan's run is paused on a reply that
      // would resume it.
      await expect(launch).toBeDisabled();
      const reason = await launch.getAttribute("data-disabled-reason");
      expect(reason, "a disabled control must explain itself").toBeTruthy();
      if (byOperation.launch.available) {
        expect(reason).toBe(
          "the run is paused waiting for a reply; answering it resumes the run",
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

    test("bulk cleanup is a review before it is an action", async ({ page }) => {
      // The bar exists to make archiving many plans one reviewed decision
      // rather than many unreviewed ones, so its archive button starts shut
      // and says which of the two reasons applies.
      await openQueue(page, deployment);
      const { byOperation } = await capabilities(page);
      const cleanupAction =
        deployment.address === "plan_key" ? "plan-archive" : "plans-archive";
      await page.locator("#wb-cleanup-toggle").click();
      const bar = page.locator("#wb-cleanup-bar");
      await expect(bar).toBeVisible();

      const archive = page.locator("#wb-cleanup-archive");
      await expect(archive).toBeDisabled();
      await expect(archive).toHaveAttribute(
        "data-disabled-reason",
        byOperation[cleanupAction].available
          ? "no plans are selected"
          : byOperation[cleanupAction].reason,
      );

      // Selecting is a read: it fills the review list and arms nothing. The
      // count is what the operator is being asked to agree with.
      await page.locator("#wb-cleanup-select-visible").click();
      await expect(page.locator("#wb-cleanup-count")).not.toContainText(
        "0 plans selected",
      );
      if (byOperation[cleanupAction].available) {
        await expect(archive).toBeEnabled();
      } else {
        // Still shut on the Pi, and now for the mode's reason rather than the
        // selection's -- the nearer explanation stepped aside for the truer one.
        await expect(archive).toBeDisabled();
        await expect(archive).toHaveAttribute(
          "data-disabled-reason",
          byOperation[cleanupAction].reason,
        );
      }
      await page.locator("#wb-cleanup-clear").click();
      await expect(page.locator("#wb-cleanup-count")).toContainText(
        "0 plans selected",
      );
    });

    test("select candidates includes matches beyond the first catalog page", async ({ page }) => {
      await openQueue(page, deployment);
      await page.route("**/api/queue?**", async (route) => {
        const url = new URL(route.request().url());
        const offset = Number(url.searchParams.get("offset") || 0);
        const makePlan = (index) => ({
          plan_key: `candidate-${index}`,
          display_label: `Candidate ${index}`,
          primary_state: "ready",
          archivable: true,
          last_activity_at: null,
        });
        const plans = offset === 0
          ? Array.from({ length: 200 }, (_, index) => makePlan(index))
          : [makePlan(200)];
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            ok: true,
            plans,
            catalog_matched: 201,
            catalog_revision: "stable-catalog",
          }),
        });
      });
      await page.locator("#wb-cleanup-toggle").click();
      await page.locator("#wb-cleanup-select-candidates").click();
      await expect(page.locator("#wb-cleanup-count")).toContainText("201 plans selected");
      await expect(page.locator("#wb-cleanup-feedback")).toContainText(
        "201 old plans selected from 201 matching plans",
      );
      const selected = await page.evaluate(async () => {
        const { state } = await import("/static/modules/state.js");
        return [...state.cleanupSelection];
      });
      expect(selected).toContain("candidate-200");
    });

    test("select candidates walks every page this deployment really serves", async ({
      page,
    }) => {
      // Not mocked: the pages come from the deployment itself, cut one plan
      // at a time so the fixture's handful of plans spans several. Each page
      // has to name the total it was cut from and one revision, or the walk
      // stops at the first page (no total) or refuses the second (no
      // revision) -- which is what the local provider used to do.
      await openQueue(page, deployment);
      const pages = [];
      await page.route("**/api/queue?**", async (route) => {
        const url = new URL(route.request().url());
        url.searchParams.set("limit", "1");
        pages.push(Number(url.searchParams.get("offset") || 0));
        await route.continue({ url: url.toString() });
      });
      await page.locator("#wb-cleanup-toggle").click();
      pages.length = 0;
      await page.locator("#wb-cleanup-select-candidates").click();
      const feedback = page.locator("#wb-cleanup-feedback");
      await expect(feedback).toContainText(/selected from \d+ matching plans/);
      const matched = Number(
        /from (\d+) matching plans/.exec(await feedback.textContent())[1],
      );
      expect(matched).toBeGreaterThan(1);
      for (let offset = 0; offset < matched; offset += 1) {
        expect(pages).toContain(offset);
      }
    });

    test("select candidates accepts pages that carry no revision at all", async ({
      page,
    }) => {
      // A provider that publishes no revision publishes none on any page.
      // Missing on both sides is agreement, not a catalog that changed.
      await openQueue(page, deployment);
      await page.route("**/api/queue?**", async (route) => {
        const url = new URL(route.request().url());
        const offset = Number(url.searchParams.get("offset") || 0);
        const plan = {
          plan_key: `unrevised-${offset}`,
          display_label: `Unrevised ${offset}`,
          primary_state: "ready",
          archivable: true,
          last_activity_at: null,
        };
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ ok: true, plans: [plan], catalog_matched: 2 }),
        });
      });
      await page.locator("#wb-cleanup-toggle").click();
      await page.locator("#wb-cleanup-select-candidates").click();
      await expect(page.locator("#wb-cleanup-feedback")).toContainText(
        "2 old plans selected from 2 matching plans",
      );
    });

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
  });
}
