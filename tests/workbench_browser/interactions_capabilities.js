/* interactions: capabilities. */
const {
  test,
  expect,
  capabilities,
  openDetail,
} = require("./interactions_support.js");

module.exports = function register(deployment) {
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
};
