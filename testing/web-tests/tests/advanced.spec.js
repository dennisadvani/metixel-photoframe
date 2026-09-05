// Advanced page: system info, timezone/clock, display save, and the embedded
// updates + keyboard sections.  Restart/reboot/shutdown/clear-cache are NOT
// clicked here (see destructive.spec.js).
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

test.describe("advanced", () => {
    test("system info loads", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "advanced");
        for (const id of INFO_IDS) {
            await expect(page.locator("#" + id)).not.toHaveText("--");
        }
        expectNoErrors(errors);
    });

    test("server clock shows a real time", async ({ page }) => {
        await goToPage(page, "advanced");
        await expect(page.locator("#server-clock")).not.toHaveText("--:--:--");
    });

    test("timezone dropdown is populated", async ({ page }) => {
        await goToPage(page, "advanced");
        expect(await page.locator("#cfg-timezone option").count()).toBeGreaterThan(1);
    });

    test("display fps-limit save + restore", async ({ page }) => {
        await goToPage(page, "advanced");
        await assertSaveRestores(page, {
            field: "#cfg-fps-limit",
            saveBtn: "#btn-save-display",
            value: 45,
        });
    });

    test("updates section renders", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "advanced");
        await expect(page.locator("#btn-check-updates")).toBeVisible();
        await expect(page.locator("#cfg-update-channel")).toBeVisible();
        expectNoErrors(errors);
    });

    test("keyboard map table loads", async ({ page }) => {
        await goToPage(page, "advanced");
        await expect(page.locator("#kbd-map-body")).toBeVisible();
    });
});
