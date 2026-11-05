// Sources page (Immich + Google Photos): fields load.  Sync Now / Fetch Albums
// / Test Connection are deliberately NOT clicked — they would
// start a real download or hit the network (which logs console errors on
// failure).
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

    test("page loads with google photos fields", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "sources");
        for (const id of [
            "cfg-gphotos-album-url",
            "btn-test-gphotos",
            "cfg-gphotos-strict",
            "btn-save-gphotos",
            "btn-gphotos-sync-now",
        ]) {
            await expect(page.locator("#" + id)).toBeVisible();
        }
        // The retired Picker mode's controls are gone for good.
        for (const id of [
            "cfg-gphotos-mode",
            "cfg-gphotos-client-id",
            "cfg-gphotos-client-secret",
            "cfg-gphotos-refresh-token",
            "btn-gphotos-pick",
        ]) {
            await expect(page.locator("#" + id)).toHaveCount(0);
        }
        expectNoErrors(errors);
    });
});
