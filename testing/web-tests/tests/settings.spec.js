// Settings page: every save button works, and the always-visible numeric
// fields save + persist + restore correctly (values are restored so the
// frame's configuration is left unchanged).
const { test, expect } = require("@playwright/test");
const { goToPage, collectErrors, expectNoErrors, assertSaveRestores } = require("./helpers");

const SAVE_BUTTONS = [
    "btn-save-local-sync",
    "btn-save-slideshow",
    "btn-save-video",
    "btn-save-image-opt",
    "btn-save-transcode",
];

test.describe("settings", () => {
    test("page loads with all five save buttons", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "settings");
        for (const id of SAVE_BUTTONS) {
            await expect(page.locator("#" + id)).toBeVisible();
        }
        expectNoErrors(errors);
    });

    test("slideshow duration save + restore", async ({ page }) => {
        await goToPage(page, "settings");
        await assertSaveRestores(page, {
            field: "#cfg-duration",
            saveBtn: "#btn-save-slideshow",
            value: 45,
        });
    });

    test("local sync interval save + restore", async ({ page }) => {
        await goToPage(page, "settings");
        await assertSaveRestores(page, {
            field: "#cfg-local-interval",
            saveBtn: "#btn-save-local-sync",
            value: 60,
        });
    });

    test("video max duration save + restore", async ({ page }) => {
        await goToPage(page, "settings");
        await assertSaveRestores(page, {
            field: "#cfg-video-max-duration",
            saveBtn: "#btn-save-video",
            value: 90,
        });
    });

    test("image optimisation save button fires", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "settings");
        await page.locator("#btn-save-image-opt").click();
        await expect(page.locator(".toast").first()).toBeVisible();
        expectNoErrors(errors);
    });

    test("transcode save button fires", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "settings");
        await page.locator("#btn-save-transcode").click();
        await expect(page.locator(".toast").first()).toBeVisible();
        expectNoErrors(errors);
    });
});
