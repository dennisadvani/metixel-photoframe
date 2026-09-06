// Sync (Immich) page: fields load.  Sync Now / Fetch Albums / Test Connection
// are deliberately NOT clicked — they would start a real download from the
// Immich server or hit the network (which logs console errors on failure).
const { test, expect } = require("@playwright/test");
const { goToPage, collectErrors, expectNoErrors } = require("./helpers");

test.describe("sync", () => {
    test("page loads with immich fields", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "sources");
        for (const id of [
            "cfg-immich-url",
            "cfg-immich-key",
            "btn-test-immich",
            "btn-save-immich",
            "btn-sync-now",
        ]) {
            await expect(page.locator("#" + id)).toBeVisible();
        }
        expectNoErrors(errors);
    });
});
