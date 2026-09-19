// Development-only. Nothing this installs reaches either deployment: the shell
// is native ES modules served straight from Python, so there is no build step
// for a browser engine to drift out from under.
//
// The version in `package.json` is pinned exactly rather than with a range,
// because a browser suite whose engine changes under it reports yesterday's
// behaviour as a regression.
//
// Three projects rather than two, for one reason each:
//
// - **desktop-chromium** is the laptop browser the Workbench has always been
//   read from, at a width above the 960px shell breakpoint.
// - **desktop-webkit** is Safari on the same laptop.
// - **mobile-webkit** is the phone over Tailscale, and it is the only project
//   that exercises the stacked shell: the queue and the detail are one column
//   there, with a back button instead of a second pane.
//
// The specs ask the viewport which shape to expect rather than being split by
// project, so a layout rule that only holds on one of them fails on the other
// instead of going unrun.

const fs = require("node:fs");
const path = require("node:path");
const { defineConfig, devices } = require("@playwright/test");

const manifest = JSON.parse(
  fs.readFileSync(path.join(__dirname, "scenarios.json"), "utf8"),
);

const localUrl = `http://${manifest.host}:${manifest.deployments.local.port}`;

module.exports = defineConfig({
  testDir: ".",
  testMatch: /.*\.spec\.js$/,
  outputDir: ".artifacts/results",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  // Every test drives both deployments, and each fixture server is a threaded
  // HTTP server over one SQLite store. Keep local and CI concurrency equal so
  // a failure here is about the shell rather than about contention.
  workers: 2,
  timeout: 30000,
  expect: { timeout: 7000 },
  reporter: [["list"]],

  use: {
    // Only a default. Each test names the deployment it is talking to, because
    // the point of the suite is that the two answer alike.
    baseURL: localUrl,
    // Tailscale Serve stamps this after verifying the tailnet identity and
    // strips any copy the client sent. Setting it here is what stands in for
    // Serve; the remote deployment refuses every route without it, which
    // `tests/test_workbench_shell.py` pins separately. The local deployment
    // has never heard of the header and ignores it.
    extraHTTPHeaders: {
      "Tailscale-User-Login": manifest.owner_identity,
    },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
  },

  projects: [
    {
      name: "desktop-chromium",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "desktop-webkit",
      use: { ...devices["Desktop Safari"] },
    },
    {
      name: "mobile-webkit",
      use: { ...devices["iPhone 13"] },
    },
  ],

  webServer: {
    command: "python3 fixture_server.py",
    cwd: __dirname,
    // Both deployments are bound by the same process before it prints, so an
    // answer on this one means the other is listening too.
    url: localUrl,
    reuseExistingServer: !process.env.CI,
    timeout: 60000,
    stdout: "ignore",
    stderr: "pipe",
  },
});
