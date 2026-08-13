// Media page: the library loads (no longer "Loading…") and the filter
// inputs are present.
const { test, expect } = require("@playwright/test");
const { goToPage, collectErrors, expectNoErrors } = require("./helpers");

test.describe("media", () => {
    test("page loads a populated media list", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "media");
        const list = page.locator("#media-list");
        await expect(list).toBeVisible();
        await expect(list).not.toHaveText(/Loading media/);
        for (const id of ["media-filter-name", "media-filter-folder", "media-filter-type"]) {
            await expect(page.locator("#" + id)).toBeVisible();
        }
        expectNoErrors(errors);
    });
});
