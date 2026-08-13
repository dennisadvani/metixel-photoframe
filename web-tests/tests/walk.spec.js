// Regression net: walk every top-level SPA route and assert there are no
// console errors, page errors, or failed network requests.  This is the
// highest-value test — it catches broken imports, wrong API paths (like a
// URL restructure), and page modules that fail to load.
const { test } = require("@playwright/test");
const { goToPage, collectErrors, expectNoErrors } = require("./helpers");

// The 6 top-level SPA routes (logs + updates are embedded in Advanced).
const PAGES = ["dashboard", "media", "settings", "sync", "network", "advanced"];

for (const name of PAGES) {
    test(`navigates to ${name} with no console/network errors`, async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, name);
        await page.waitForTimeout(750); // let async page loads settle
        expectNoErrors(errors);
    });
}
