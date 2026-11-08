/* shell browser coverage, grouped by responsibility. */
const {
  test,
  expect,
  DEPLOYMENTS,
  openQueue,
  catalog,
  rowFor,
  openDetail,
  detailHeader,
} = require("./shell_support.js");
const navigation = require("./shell_navigation.js");
const accessibility = require("./shell_accessibility.js");

for (const deployment of DEPLOYMENTS) {
  test.describe(`${deployment.name} deployment`, () => {
    navigation(deployment);
    accessibility(deployment);
  });
}

test("both deployments show the same plan under the same keys", async ({
  page,
}) => {
  const seen = [];
  for (const deployment of DEPLOYMENTS) {
    await openQueue(page, deployment);
    const plans = await catalog(page);
    const alpha = rowFor(plans, "alpha");
    await openDetail(page, deployment, alpha);
    seen.push({
      mode: deployment.name,
      plan_key: alpha.plan_key,
      run_key: alpha.run_key,
      state: (await detailHeader(page)).state,
      badge: await page.locator("#wb-detail-state").getAttribute("data-state"),
    });
  }
  expect(seen[0].plan_key).toBe(seen[1].plan_key);
  expect(seen[0].run_key).toBe(seen[1].run_key);
  expect(seen[0].state).toBe(seen[1].state);
  expect(seen[0].badge).toBe(seen[1].badge);
  // The addresses are allowed to differ -- that is the whole asymmetry -- but
  // only in the direction the deployments actually differ in.
  expect(DEPLOYMENTS.map((entry) => entry.address)).toEqual([
    "plan_dir",
    "plan_key",
  ]);
});
