// Playwright config — targets a LIVE Metixel frame over the LAN.
//
// The browser runs on the workstation and talks to the frame's web server,
// so METIXEL_URL is just the frame's dashboard address.
//
//   METIXEL_URL=http://192.168.222.122:8080 npx playwright test
//
// Defaults to the dev Pi. All tests run sequentially (workers=1) because
// they exercise one real frame and must not fight over config/hardware.
const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
    testDir: "./tests",
    timeout: 60_000,
    expect: { timeout: 15_000 },
    fullyParallel: false,
    workers: 1,
    retries: 0,
    reporter: [["list"]],
    use: {
        baseURL: process.env.METIXEL_URL || "http://192.168.222.122",
        headless: true,
        viewport: { width: 1280, height: 900 },
        actionTimeout: 15_000,
        trace: "retain-on-failure",
        screenshot: "only-on-failure",
    },
    projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
