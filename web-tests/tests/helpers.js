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
    return errors;
}

function expectNoErrors(errors) {
    expect(errors, "console/page/network errors:\n" + errors.join("\n")).toEqual([]);
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

module.exports = { goToPage, collectErrors, expectNoErrors, assertSaveRestores };
