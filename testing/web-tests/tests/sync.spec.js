// Sources page (Immich + Google Photos): fields load.  Sync Now / Fetch Albums
// / Test Connection / Pick Photos are deliberately NOT clicked — they would
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
            "cfg-gphotos-mode",
            "cfg-gphotos-client-id",
            "cfg-gphotos-client-secret",
            "cfg-gphotos-refresh-token",
            "btn-test-gphotos",
            "btn-gphotos-pick",
            "btn-save-gphotos",
            "btn-gphotos-sync-now",
        ]) {
            await expect(page.locator("#" + id)).toBeVisible();
        }
        // The shared-album fields belong to the other mode.
        await expect(page.locator("#cfg-gphotos-album-url")).toBeHidden();
        expectNoErrors(errors);
    });

    test("google photos mode switches the visible fields", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "sources");

        await page.locator("#cfg-gphotos-mode").selectOption("shared_url");
        await expect(page.locator("#cfg-gphotos-album-url")).toBeVisible();
        // Credentials and picking are picker-only.
        await expect(page.locator("#cfg-gphotos-client-id")).toBeHidden();
        await expect(page.locator("#btn-gphotos-pick")).toBeHidden();

        await page.locator("#cfg-gphotos-mode").selectOption("picker");
        await expect(page.locator("#cfg-gphotos-client-id")).toBeVisible();
        await expect(page.locator("#cfg-gphotos-album-url")).toBeHidden();

        expectNoErrors(errors);
    });
});
