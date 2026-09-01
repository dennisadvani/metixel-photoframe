// Security flows: web password login gate, device password (SSH+Samba),
// and screen PIN.  These tests are destructive — they set and clear
// credentials — so each test restores the frame to a known state.
//
// NOTE: these tests assume the frame starts with NO web password set
// (auth disabled).  If a password is already set, the login gate will
// appear and these tests will fail.  Run them on a fresh/known frame.
const { test, expect } = require("@playwright/test");
const { goToPage, collectErrors, expectNoErrors } = require("./helpers");

const WEB_PW = "TestWebPass123!";
const DEVICE_PW = "TestDevicePass123!";
const PIN = "123456";

test.describe("security", () => {
    test("login gate appears when web password is set", async ({ page }) => {
        const errors = collectErrors(page);

        // Set a web password via the Settings Security card.
        await goToPage(page, "settings");
        await page.locator("#cfg-web-password").fill(WEB_PW);
        await page.locator("#cfg-web-password-confirm").fill(WEB_PW);
        await page.locator("#btn-save-web-password").click();
        await expect(page.locator(".toast").first()).toBeVisible();

        // Reload → the login gate should appear (not the dashboard).
        await page.reload();
        await expect(page.locator("#login-overlay")).toBeVisible();
        await expect(page.locator("#login-password")).toBeVisible();

        // Wrong password → inline error.
        await page.locator("#login-password").fill("wrongpassword");
        await page.locator("#btn-login").click();
        await expect(page.locator("#login-error")).toBeVisible();

        // Correct password → dashboard unlocks.
        await page.locator("#login-password").fill(WEB_PW);
        await page.locator("#btn-login").click();
        await expect(page.locator("#login-overlay")).toBeHidden();
        await expect(page.locator("#page-dashboard")).toHaveClass(/active/);

        // Cleanup: clear the web password (requires auth, which we have).
        await goToPage(page, "settings");
        await page.locator("#cfg-web-password").fill("");
        await page.locator("#cfg-web-password-confirm").fill("");
        await page.locator("#btn-save-web-password").click();
        await expect(page.locator(".toast").first()).toBeVisible();
        expectNoErrors(errors);
    });

    test("device password mismatch is rejected", async ({ page }) => {
        await goToPage(page, "settings");
        await page.locator("#cfg-device-password").fill(DEVICE_PW);
        await page.locator("#cfg-device-password-confirm").fill("different");
        await page.locator("#btn-save-device-password").click();
        await expect(page.locator(".toast").first()).toContainText("do not match");
    });

    test("device password empty is rejected", async ({ page }) => {
        await goToPage(page, "settings");
        await page.locator("#cfg-device-password").fill("");
        await page.locator("#cfg-device-password-confirm").fill("");
        await page.locator("#btn-save-device-password").click();
        await expect(page.locator(".toast").first()).toContainText("Enter a new device password");
    });

    test("screen PIN set + clear", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "settings");

        // Set a screen PIN.
        await page.locator("#cfg-screen-pin").fill(PIN);
        await page.locator("#cfg-screen-pin-confirm").fill(PIN);
        await page.locator("#btn-save-screen-pin").click();
        await expect(page.locator(".toast").first()).toBeVisible();

        // Clear it.
        await page.locator("#cfg-screen-pin").fill("");
        await page.locator("#cfg-screen-pin-confirm").fill("");
        await page.locator("#btn-save-screen-pin").click();
        await expect(page.locator(".toast").first()).toBeVisible();
        expectNoErrors(errors);
    });

    test("screen PIN invalid length is rejected", async ({ page }) => {
        await goToPage(page, "settings");
        await page.locator("#cfg-screen-pin").fill("123");
        await page.locator("#cfg-screen-pin-confirm").fill("123");
        await page.locator("#btn-save-screen-pin").click();
        await expect(page.locator(".toast").first()).toContainText("4-6 digits");
    });
});