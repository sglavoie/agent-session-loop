/* Recovery through the real UI and API, over fresh on-disk state per test.
 * The local provider executes an unlock; the remote provider durably queues
 * it. Neither deployment launches a harness or executes a copied command.
 */
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const readline = require("node:readline");
const { test: base, expect } = require("@playwright/test");
const manifest = require("./scenarios.json");

const test = base.extend({
  recoveryScenario: ["stale-lock", { option: true }],
  recovery: async ({ recoveryScenario }, use) => {
    const child = spawn("python3", ["recovery_fixture_server.py", recoveryScenario], {
      cwd: __dirname,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr = (stderr + chunk).slice(-16000);
    });
    const exited = once(child, "close");
    const lines = readline.createInterface({ input: child.stdout });
    let readyTimer;
    try {
      const ready = await new Promise((resolve, reject) => {
        readyTimer = setTimeout(() => reject(new Error(`fixture startup: ${stderr}`)), 15000);
        child.once("error", reject);
        child.once("exit", (code) => reject(new Error(`fixture exited ${code}: ${stderr}`)));
        lines.once("line", (line) => {
          try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
        });
      });
      clearTimeout(readyTimer);
      await use(ready);
    } finally {
      clearTimeout(readyTimer);
      child.stdin.end();
      const killTimer = setTimeout(() => child.kill("SIGKILL"), 5000);
      const [code, signal] = await exited;
      clearTimeout(killTimer);
      lines.close();
      expect({ code, signal }, stderr).toEqual({ code: 0, signal: null });
    }
  },
});

function planQuery(recovery, deployment) {
  return new URLSearchParams({
    [manifest.deployments[deployment].address]: recovery[manifest.deployments[deployment].address],
  }).toString();
}

async function readDetail(page, recovery, deployment, endpoint) {
  return page.evaluate(async (path) => {
    const response = await fetch(path);
    const body = await response.json();
    // The remote responder carries an object detail as a one-row page.
    return body.detail && body.entries?.length === 1
      ? { ...body, ...body.entries[0] }
      : body;
  }, `/api/${endpoint}?${planQuery(recovery, deployment)}`);
}

async function openRecovery(page, recovery, deployment, kind) {
  const writes = [];
  page.on("request", (request) => {
    if (request.method() !== "GET" && request.method() !== "HEAD") {
      writes.push(new URL(request.url()).pathname);
    }
  });
  // Observe the browser's clipboard boundary in all three engines, including
  // WebKit where granting clipboard-read permission is not supported.
  await page.addInitScript(() => {
    window.copiedRecoveryCommands = [];
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (text) => window.copiedRecoveryCommands.push(text) },
    });
  });
  const token = deployment === "local" ? `?token=${manifest.token}` : "";
  const route = deployment === "local"
    ? `dir=${encodeURIComponent(recovery.plan_dir)}`
    : `key=${recovery.plan_key}`;
  await page.goto(`${recovery[deployment]}/${token}#plan?${route}`);
  await expect(page.locator("#wb-recovery-kind")).toHaveAttribute("data-kind", kind);
  await expect(page.locator("#wb-refresh-status")).toHaveAttribute("data-state", "idle");
  return writes;
}

async function copyCommand(page, row, expected) {
  await expect(row.locator("code")).toHaveText(expected);
  const copy = row.getByRole("button");
  await expect(copy).toHaveText("copy");
  await copy.click();
  await expect(copy).toHaveText("copied");
  expect(await page.evaluate(() => window.copiedRecoveryCommands.at(-1))).toBe(expected);
}

async function confirmAction(page, button, accept) {
  // Safari does not retain button focus on pointer clicks. Activate from the
  // keyboard so the cancellation check measures keyboard focus restoration.
  await button.focus();
  const shown = page.waitForEvent("dialog");
  const clicked = button.press("Enter");
  const dialog = await shown;
  expect(dialog.type()).toBe("confirm");
  expect(dialog.message()).toContain("Force-unlock this plan?");
  if (accept) await dialog.accept(); else await dialog.dismiss();
  await clicked;
}

for (const deployment of Object.keys(manifest.deployments)) {
  test.describe(`${deployment} recovery`, () => {
    test("a stale-lock card confirms an unlock and refresh preserves its actual outcome", async ({ page, recovery }) => {
      const writes = await openRecovery(page, recovery, deployment, "stale-lock");
      await expect(page.locator("#wb-recovery-title")).toHaveText("Runner lock is stale");
      await expect(page.locator("#wb-recovery-evidence")).toContainText("is not running");
      const unlock = page.locator('#wb-recovery-primary-action [data-operation-action="unlock"]');
      await expect(unlock).toBeEnabled();
      await confirmAction(page, unlock, false);
      await expect(unlock).toBeFocused();
      expect(writes).toEqual([]);
      expect((await readDetail(page, recovery, deployment, "recovery")).recovery.kind).toBe("stale-lock");

      await confirmAction(page, unlock, true);
      const feedback = page.locator("#wb-controls-feedback");
      if (deployment === "local") {
        await expect(feedback).toContainText("unlock: removed");
      } else {
        await expect(feedback).toContainText("not run");
      }
      expect(writes).toEqual(["/api/unlock"]);
      const receipts = await page.evaluate(() => JSON.parse(sessionStorage.getItem("wb.operations") || "[]"));
      if (deployment === "remote") {
        expect(receipts).toHaveLength(1);
        expect(receipts[0].action).toBe("unlock");
        expect(receipts[0].outcome).toBe("pending");
      }
      await page.reload();
      const expected = deployment === "local" ? "none" : "stale-lock";
      await expect(page.locator("#wb-recovery-kind")).toHaveAttribute("data-kind", expected);
      expect((await readDetail(page, recovery, deployment, "recovery")).recovery.kind).toBe(expected);
      if (deployment === "remote") {
        await expect(page.locator("#wb-operations-panel")).toContainText("not been confirmed as run");
        await expect(page.locator("#wb-operations-list")).toContainText(receipts[0].operation_id);
      }
      expect(writes).toEqual(["/api/unlock"]);
    });

    test("an unavailable unlock is disabled on the recovery card with the provider's reason", async ({ page, recovery }) => {
      const reason = "Unlock is unavailable on this provider.";
      let available = false;
      await page.route("**/api/capabilities*", async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        const unlock = body.operations.find((entry) => entry.operation === "unlock");
        expect(unlock).toBeTruthy();
        Object.assign(unlock, { available, reason: available ? null : reason });
        await route.fulfill({ response, json: body });
      });
      const writes = await openRecovery(page, recovery, deployment, "stale-lock");
      const card = page.locator('#wb-recovery-primary-action [data-target="unlock"]');
      const button = card.getByRole("button", { name: "unlock", exact: true });
      await expect(button).toBeDisabled();
      await expect(button).toHaveAttribute("data-disabled-reason", reason);
      await expect(card).toContainText(reason);
      await expect(card.locator("[data-operation-reason]")).toBeVisible();
      await expect(page.locator("#wb-control-unlock")).toBeDisabled();
      await button.evaluate((element) => element.click());
      available = true;
      await page.reload();
      await expect(button).toBeEnabled();
      await expect(card.locator("[data-operation-reason]")).toBeHidden();
      await expect(button).not.toHaveAttribute("data-disabled-reason");
      expect(writes).toEqual([]);
    });

    test.describe("failed verification", () => {
      test.use({ recoveryScenario: "verify-failed" });
      test("shows the failed check, copies repair commands, and opens its artifacts", async ({ page, recovery }) => {
        const writes = await openRecovery(page, recovery, deployment, "verify-failed");
        await expect(page.locator("#wb-recovery-title")).toHaveText("Verification failed");
        await expect(page.locator("#wb-recovery-evidence")).toContainText("expected-report");
        await expect(page.locator("#wb-recovery-evidence")).toContainText("file-missing");
        const diagnosis = (await readDetail(page, recovery, deployment, "recovery")).recovery;
        expect(diagnosis.commands).toHaveLength(2);
        expect(diagnosis.commands[0].command).toContain("--session 2 --json");
        const rows = page.locator("#wb-recovery-commands > li");
        for (let index = 0; index < diagnosis.commands.length; index += 1) {
          await copyCommand(page, rows.nth(index), diagnosis.commands[index].command);
        }
        await expect(page.locator("#wb-recovery-artifacts")).toContainText("Failed turn Result JSON");
        await page.locator('#wb-recovery-primary-action [data-target="artifacts"] button').click();
        await expect(page.locator("#wb-detail-artifacts")).toBeInViewport();
        // A retry card must have the same availability and explanation as the
        // control bar; this test never starts a real harness.
        const retry = page.locator('#wb-recovery-primary-action [data-operation-action="launch"]');
        const launch = page.locator("#wb-control-launch");
        await expect(retry).toHaveJSProperty("disabled", await launch.isDisabled());
        expect(await retry.getAttribute("data-disabled-reason")).toBe(await launch.getAttribute("data-disabled-reason"));
        await page.reload();
        await expect(page.locator("#wb-recovery-evidence")).toContainText("expected-report");
        expect(writes).toEqual([]);
      });
    });

    for (const scenario of ["fresh-conversation", "blocked-predecessor"]) {
      test.describe(scenario, () => {
        test.use({ recoveryScenario: scenario });
        test("reports the decision and preserves corrections without performing recovery", async ({ page, recovery }) => {
          const writes = await openRecovery(page, recovery, deployment, "conversation-recovery");
          const diagnosis = (await readDetail(page, recovery, deployment, "recovery")).recovery;
          expect(diagnosis.actions.every((action) => !action.endpoint)).toBe(true);
          await expect(page.locator("#wb-recovery-primary-action [data-operation-action]")).toHaveCount(0);
          const fresh = scenario === "fresh-conversation";
          expect(diagnosis.commands).toHaveLength(fresh ? 2 : 1);
          const commands = page.locator("#wb-recovery-commands > li");
          for (let index = 0; index < diagnosis.commands.length; index += 1) {
            await copyCommand(page, commands.nth(index), diagnosis.commands[index].command);
          }
          if (fresh) {
            expect(diagnosis.commands[1].command).toContain("--fresh-conversation");
            await expect(page.locator("#wb-recovery-summary")).toContainText("one-use");
          } else {
            expect(diagnosis.commands[0].command).toContain("redirect-status");
            await expect(page.locator("#wb-recovery-summary")).toContainText("no authorization");
          }
          const panel = page.locator("#wb-conversation-recovery");
          await expect(panel).toBeVisible();
          const detail = await readDetail(page, recovery, deployment, "conversation-recovery");
          if (deployment === "local") {
            expect(detail.ok).toBe(true);
            expect(detail.auto_resume).toBe(false);
            expect(detail.decision).toBe(fresh ? "fresh-conversation" : "predecessor-not-stopped");
            await expect(page.locator("#wb-conversation-facts")).toContainText(detail.decision);
            await expect(page.locator("#wb-conversation-facts")).toContainText("Automatic resumeno");
            await expect(page.locator("#wb-conversation-retained")).toContainText(recovery.retained_operation_id);
            const row = page.locator("#wb-conversation-retained > li");
            await expect(row).toHaveAttribute("data-stage", "unaccepted");
            const copyRows = page.locator("#wb-conversation-commands > li");
            await expect(copyRows).toHaveCount(fresh ? 2 : 1);
            await copyCommand(page, copyRows.first(), detail.inspect_command);
            if (fresh) {
              await expect(page.locator("#wb-conversation-note")).toContainText("one-use");
              await copyCommand(page, copyRows.nth(1), detail.authorization_command);
            } else {
              expect(detail.authorization_command).toBeNull();
              await expect(page.locator("#wb-conversation-note")).toBeHidden();
            }
            // A failed refresh must remove the old authorization and retained
            // list rather than leave readable-looking evidence from before it.
            const error = "Conversation evidence is temporarily unreadable.";
            await page.route("**/api/conversation-recovery?**", (route) =>
              route.fulfill({ status: 503, json: { ok: false, error } }),
            );
            await page.locator("#wb-refresh-now").click();
            await expect(panel).toContainText(error);
            await expect(page.locator("#wb-conversation-commands > li")).toHaveCount(0);
            await expect(page.locator("#wb-conversation-retained")).toContainText("unknown while recovery is unreadable");
            await expect(page.locator("#wb-conversation-note")).toBeHidden();
            await page.unroute("**/api/conversation-recovery?**");
          } else {
            // The remote provider intentionally has no harness session files.
            // Its refusal must be readable and must clear actionable commands.
            expect(detail.reason).toBe("operation-unavailable");
            await expect(panel).toContainText(detail.error);
            await expect(page.locator("#wb-conversation-commands > li")).toHaveCount(0);
            await expect(page.locator("#wb-conversation-retained")).toContainText("unknown while recovery is unreadable");
          }
          await expect(panel.locator("button").filter({ hasNotText: /^(copy|copied)$/ })).toHaveCount(0);
          await page.reload();
          await expect(panel).toBeVisible();
          if (deployment === "local") {
            await expect(page.locator("#wb-conversation-retained")).toContainText(recovery.retained_operation_id);
            expect((await readDetail(page, recovery, deployment, "conversation-recovery")).retained_operations).toEqual(detail.retained_operations);
          } else {
            await expect(panel).toContainText(detail.error);
          }
          expect(writes).toEqual([]);
        });
      });
    }
  });
}
