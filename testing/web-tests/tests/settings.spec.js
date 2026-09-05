// Settings page: every save button works, and the always-visible numeric
// fields save + persist + restore correctly (values are restored so the
// frame's configuration is left unchanged).
//
// The SPA was restructured: the old "settings" page was split into the
// "playback" route (slideshow/video/display/time) and the "optimisation"
// route (image + transcode), while local-sync lives on the "sources" route.
const { test, expect } = require("@playwright/test");
const { goToPage, collectErrors, expectNoErrors, assertSaveRestores } = require("./helpers");

test.describe("settings", () => {
    test("playback page loads with slideshow + video save buttons", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "playback");
        for (const id of ["btn-save-slideshow", "btn-save-video", "btn-save-display"]) {
            await expect(page.locator("#" + id)).toBeVisible();
        }
        expectNoErrors(errors);
    });

    test("optimisation page loads with image + transcode save buttons", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "optimisation");
        for (const id of ["btn-save-image-opt", "btn-save-transcode"]) {
            await expect(page.locator("#" + id)).toBeVisible();
        }
        expectNoErrors(errors);
    });

    test("slideshow duration save + restore", async ({ page }) => {
        await goToPage(page, "playback");
        await assertSaveRestores(page, {
            field: "#cfg-duration",
            saveBtn: "#btn-save-slideshow",
            value: 45,
        });
    });

    test("local sync interval save + restore", async ({ page }) => {
        await goToPage(page, "sources");
        await assertSaveRestores(page, {
            field: "#cfg-local-interval",
            saveBtn: "#btn-save-local-sync",
            value: 60,
        });
    });

    test("video max duration save + restore", async ({ page }) => {
        await goToPage(page, "playback");
        await assertSaveRestores(page, {
            field: "#cfg-video-max-duration",
            saveBtn: "#btn-save-video",
            value: 90,
        });
    });

    test("image optimisation save button fires", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "optimisation");
        await page.locator("#btn-save-image-opt").click();
        await expect(page.locator(".toast").first()).toBeVisible();
        expectNoErrors(errors);
    });

    test("transcode save button fires", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "optimisation");
        await page.locator("#btn-save-transcode").click();
        await expect(page.locator(".toast").first()).toBeVisible();
        expectNoErrors(errors);
    });
});
