// Settings page: every save button works, and the always-visible numeric
// fields save + persist + restore correctly (values are restored so the
// frame's configuration is left unchanged).
//
// The SPA was restructured: the old "settings" page was split into the
// "playback" route (slideshow/video/display/monitor-control/time) and the
// "optimisation" route (image + transcode), while local-sync lives on the
// "sources" route.
const { test, expect } = require("@playwright/test");
const {
    goToPage,
    collectErrors,
    expectNoErrors,
    assertSaveRestores,
    waitForBackendRestart,
} = require("./helpers");

test.describe("settings", () => {
    test("playback page loads with slideshow/video/display/monitor-control save buttons", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "playback");
        for (const id of ["btn-save-slideshow", "btn-save-video", "btn-save-display", "btn-save-ddc"]) {
            await expect(page.locator("#" + id)).toBeVisible();
        }
        expectNoErrors(errors);
    });

    test("monitor control (DDC/CI) card lives on the playback page", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "playback");
        await expect(page.locator("#card-monitor-control")).toBeVisible();
        await expect(page.locator("#btn-ddc-refresh")).toBeVisible();
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
        // btn-save-local-sync sends watch_paths → routes/config.py sets
        // needs_rebuild → the backend schedules a restart.  Wait it out here
        // rather than with a blind sleep, or the restart lands in the NEXT test.
        await assertSaveRestores(page, {
            field: "#cfg-local-interval",
            saveBtn: "#btn-save-local-sync",
            value: 60,
            restartsBackend: true,
        });
    });

    test("video max duration save + restore", async ({ page }) => {
        await goToPage(page, "playback");
        // btn-save-video posts /config/video → ALWAYS schedules a restart.
        await assertSaveRestores(page, {
            field: "#cfg-video-max-duration",
            saveBtn: "#btn-save-video",
            value: 90,
            restartsBackend: true,
        });
    });

    test("image optimisation save button fires", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "optimisation");
        await page.locator("#btn-save-image-opt").click();
        await expect(page.locator(".toast").first()).toBeVisible();
        expectNoErrors(errors);
        // btn-save-image-opt posts /config/image → schedules a restart.
        await waitForBackendRestart();
    });

    test("transcode save button fires", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "optimisation");
        await page.locator("#btn-save-transcode").click();
        await expect(page.locator(".toast").first()).toBeVisible();
        expectNoErrors(errors);
        // btn-save-transcode posts /config/video → schedules a restart.
        await waitForBackendRestart();
    });
});
