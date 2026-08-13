// Network page: live status loads and the Wi-Fi country field is editable.
// Scan-for-networks and AP-mode toggles are NOT clicked (real radio/AP
// side effects).
const { test, expect } = require("@playwright/test");
const { goToPage, collectErrors, expectNoErrors } = require("./helpers");

test.describe("network", () => {
    test("page loads with live status", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "network");
        await expect(page.locator("#network-status")).not.toHaveText(/Loading/);
        await expect(page.locator("#btn-network-scan")).toBeVisible();
        await expect(page.locator("#cfg-wifi-country")).toBeVisible();
        await expect(page.locator("#btn-save-wifi-country")).toBeVisible();
        expectNoErrors(errors);
    });
});
