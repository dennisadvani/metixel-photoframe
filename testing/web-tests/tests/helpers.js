// Shared helpers for the Metixel dashboard Playwright suite.
// Targets a LIVE frame — every page/field here is the real SPA served by
// the frame's backend (see playwright.config.js for METIXEL_URL).
const { expect } = require("@playwright/test");

// Navigate to a hash-based SPA route and wait until the page section is
// active and the API connection overlay has cleared.
async function goToPage(page, pageName) {
    await page.goto("/#" + pageName);
    await expect(page.locator("#page-" + pageName)).toHaveClass(/active/);
    await expect(page.locator("#connection-overlay")).toBeHidden();
}

// Attach listeners that record console/page/network errors for a page.
function collectErrors(page) {
    const errors = [];
    page.on("console", (msg) => {
        if (msg.type() === "error") errors.push("console: " + msg.text());
    });
    page.on("pageerror", (err) => errors.push("pageerror: " + String(err)));
    page.on("requestfailed", (req) => errors.push("requestfailed: " + req.url()));
    // Browser console messages don't include the URL for failed requests —
    // capture the response too so a 404/500 shows exactly which resource it was.
    page.on("response", (res) => {
        if (res.status() >= 400) errors.push("network: HTTP " + res.status() + " " + res.url());
    });
    return errors;
}

// A current-media thumbnail that was regenerated (or whose cache was cleared)
// can 404 for one poll cycle while the frontend rewrites current_media.json.
// That is a transient, self-healing condition — not a page fault — so drop the
// thumbnail endpoint from the collected errors.  The backend also withholds the
// URL when the file is missing; this is belt-and-braces for the poll race.
const _IGNORED_ERROR = /\/api\/media\/thumbnail\//;

function relevantErrors(errors) {
    return errors.filter((e) => !_IGNORED_ERROR.test(e));
}

function expectNoErrors(errors) {
    const relevant = relevantErrors(errors);
    expect(relevant, "console/page/network errors:\n" + relevant.join("\n")).toEqual([]);
}

// Capture a field's value, change it, save, verify it persisted after a
// reload, then restore the original value so the frame is left unchanged.
async function assertSaveRestores(page, { field, saveBtn, value }) {
    const original = await page.locator(field).inputValue();
    await page.locator(field).fill(String(value));
    await page.locator(saveBtn).click();
    await expect(page.locator(".toast").first()).toBeVisible();
    // Reload → the saved value should have persisted on the frame.
    await page.reload();
    await expect(page.locator(field)).toHaveValue(String(value));
    // Restore the original value.
    await page.locator(field).fill(original);
    await page.locator(saveBtn).click();
    await expect(page.locator(".toast").first()).toBeVisible();
}

module.exports = { goToPage, collectErrors, expectNoErrors, relevantErrors, assertSaveRestores };

