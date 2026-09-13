// Dashboard page: live stats, current media, and the prev/next/pause
// controls that drive the real frame over IPC.
const { test, expect } = require("@playwright/test");
const { goToPage, collectErrors, expectNoErrors } = require("./helpers");

test.describe("dashboard", () => {
    test("loads live system stats", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "dashboard");
        // Stats are polled by the SPA and can take a few seconds to arrive on
        // a busy frame, so poll the assertion (expect's timeout) and retry the
        // whole navigation — the first paint after a backend restart is the
        // slowest case.
        await expect(async () => {
            await expect(page.locator("#stat-uptime-val")).not.toHaveText("--", { timeout: 5_000 });
            await expect(page.locator("#stat-mem-val")).not.toHaveText("--", { timeout: 5_000 });
            await expect(page.locator("#stat-temp-val")).not.toHaveText("--", { timeout: 5_000 });
        }).toPass({ timeout: 30_000 });
        expectNoErrors(errors);
    });

    test("shows current media and playlist count", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "dashboard");
        await expect(page.locator("#current-media")).toBeVisible();
        // Polled by the SPA — can take a few seconds to populate after a
        // backend restart, so retry rather than assert once.
        await expect(async () => {
            await expect(page.locator("#current-media")).not.toHaveText("", { timeout: 5_000 });
            await expect(page.locator("#stat-playlist-val")).not.toHaveText("--", { timeout: 5_000 });
        }).toPass({ timeout: 30_000 });
        expectNoErrors(errors);
    });

    test("prev / next / pause controls fire without errors", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "dashboard");
        const pause = page.locator("#btn-pause-toggle");
        await page.locator("#btn-next").click();
        await page.locator("#btn-prev").click();
        await pause.click(); // pause the live slideshow...
        await pause.click(); // ...then resume so the frame is left running
        expectNoErrors(errors);
    });
});
