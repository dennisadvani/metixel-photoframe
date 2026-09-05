// System + playback pages: system info, timezone/clock, display save, and the
// embedded updates + keyboard sections.  Restart/reboot/shutdown/clear-cache
// are NOT clicked here (see destructive.spec.js).
//
// The SPA was restructured: the old "advanced" page was split into the
// "playback" route (clock/timezone/display) and the "system" route (system
// info, updates, keyboard, security).
const { test, expect } = require("@playwright/test");
const { goToPage, collectErrors, expectNoErrors, assertSaveRestores } = require("./helpers");

const INFO_IDS = [
    "info-app-version",
    "info-pi-model",
    "info-os-release",
    "info-kernel",
    "info-python",
    "info-pi3d",
    "info-gpu-mem",
    "info-drm-driver",
    "info-hostname",
];

test.describe("system", () => {
    test("system info loads", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "system");
        for (const id of INFO_IDS) {
            await expect(page.locator("#" + id)).not.toHaveText("--");
        }
        expectNoErrors(errors);
    });

    test("updates section renders", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "system");
        await expect(page.locator("#btn-check-updates")).toBeVisible();
        await expect(page.locator("#cfg-update-channel")).toBeVisible();
        expectNoErrors(errors);
    });

    test("keyboard map table loads", async ({ page }) => {
        await goToPage(page, "system");
        await expect(page.locator("#kbd-map-body")).toBeVisible();
    });
});

test.describe("playback", () => {
    test("server clock shows a real time", async ({ page }) => {
        await goToPage(page, "playback");
        await expect(page.locator("#server-clock")).not.toHaveText("--:--:--");
    });

    test("timezone dropdown is populated", async ({ page }) => {
        await goToPage(page, "playback");
        expect(await page.locator("#cfg-timezone option").count()).toBeGreaterThan(1);
    });

    test("display fps-limit save + restore", async ({ page }) => {
        await goToPage(page, "playback");
        await assertSaveRestores(page, {
            field: "#cfg-fps-limit",
            saveBtn: "#btn-save-display",
            value: 45,
        });
    });
});
