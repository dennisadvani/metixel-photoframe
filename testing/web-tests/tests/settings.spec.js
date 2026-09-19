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

    // The three Ambient Fill modes each use a different subset of the controls,
    // so the irrelevant rows are hidden.  A visible control that does nothing is
    // worse than a hidden one, and this is easy to get wrong when a mode is
    // added: the colour row originally stayed visible in "blur" mode, offering a
    // picker with no effect.
    test.describe("ambient fill controls follow the selected mode", () => {
        const select = "#cfg-ambient-strategy";
        const colourRow = "#ambient-colour-row";
        const blurRow = "#ambient-blur-row";
        const filterRow = "#ambient-blur-filter-row";
        const darkenRow = "#ambient-darken-row";

        test("solid colour shows only the colour picker", async ({ page }) => {
            await goToPage(page, "playback");
            await page.selectOption(select, "solid");
            await expect(page.locator(colourRow)).toBeVisible();
            await expect(page.locator(blurRow)).toBeHidden();
            await expect(page.locator(filterRow)).toBeHidden();
            await expect(page.locator(darkenRow)).toBeHidden();
        });

        test("black bars hides every ambient control", async ({ page }) => {
            await goToPage(page, "playback");
            await page.selectOption(select, "bars");
            await expect(page.locator(colourRow)).toBeHidden();
            await expect(page.locator(blurRow)).toBeHidden();
            await expect(page.locator(filterRow)).toBeHidden();
            await expect(page.locator(darkenRow)).toBeHidden();
        });

        test("blurred photo shows blur + brightness but NOT the colour picker", async ({ page }) => {
            await goToPage(page, "playback");
            await page.selectOption(select, "blur");
            await expect(page.locator(colourRow)).toBeHidden();
            await expect(page.locator(blurRow)).toBeVisible();
            await expect(page.locator(filterRow)).toBeVisible();
            await expect(page.locator(darkenRow)).toBeVisible();
        });

        test("the visibility rule survives a reload after saving blur", async ({ page }) => {
            // The mode is persisted, so the initial render must apply the rule
            // rather than only the change handler doing it.
            const errors = collectErrors(page);
            await goToPage(page, "playback");
            const original = await page.locator(select).inputValue();
            await page.selectOption(select, "blur");
            await page.click("#btn-save-slideshow");
            await page.waitForTimeout(500);
            await page.reload();
            await page.waitForTimeout(500);

            await expect(page.locator(colourRow)).toBeHidden();
            await expect(page.locator(blurRow)).toBeVisible();

            // Restore the frame's original configuration.
            await page.selectOption(select, original);
            await page.click("#btn-save-slideshow");
            await page.waitForTimeout(500);
            expectNoErrors(errors);
        });
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
        // btn-save-local-sync posts sync.local, which includes watch_paths — a
        // rebuild key — so the backend schedules a restart.  restartsBackend makes
        // the helper wait for the service to come back after EACH of its two
        // saves; the old blind sleep left the second restart to land in the next
        // test, where it surfaced as a random connection error.
        await assertSaveRestores(page, {
            field: "#cfg-local-interval",
            saveBtn: "#btn-save-local-sync",
            value: 60,
            restartsBackend: true,
        });
    });

    test("video max duration save + restore", async ({ page }) => {
        await goToPage(page, "playback");
        // btn-save-video posts the `video` section, which always rebuilds.
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
        // btn-save-image-opt posts the `image` section, which always rebuilds.
        await waitForBackendRestart();
    });

    test("transcode save button fires", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "optimisation");
        await page.locator("#btn-save-transcode").click();
        await expect(page.locator(".toast").first()).toBeVisible();
        expectNoErrors(errors);
        // btn-save-transcode posts the `video` section, which always rebuilds.
        await waitForBackendRestart();
    });
});
