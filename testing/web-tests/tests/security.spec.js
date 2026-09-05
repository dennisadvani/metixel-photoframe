// Security flows: web password login gate, device password (SSH+Samba),
// and screen PIN.  These tests are destructive — they set and clear
// credentials — so each test restores the frame to a known state.
//
// The suite's global-setup.js clears any pre-existing web password over SSH
// so the frame starts with auth disabled.  A beforeAll here re-asserts that
// (in case the security spec is run in isolation) so the login-gate test can
// set the password itself.
const { test, expect } = require("@playwright/test");
const { goToPage, collectErrors, expectNoErrors } = require("./helpers");
const { clearWebPasswordAndRestart } = require("../ssh-utils");

const WEB_PW = "TestWebPass123!";
const DEVICE_PW = "TestDevicePass123!";
const PIN = "123456";

test.describe("security", () => {
    test.beforeAll(async () => {
        // Ensure the frame starts with no web password (auth disabled).
        await clearWebPasswordAndRestart();
    });

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

        // Correct password → dashboard unlocks.  main.js reloads the page after a
        // successful login (to re-boot the SPA with a valid session).  The URL
        // hash is still #settings (from the initial goToPage), so after the
        // reload the SPA re-boots to the settings page — assert the login
        // overlay is gone and the SPA is usable.
        await page.locator("#login-password").fill(WEB_PW);
        await page.locator("#btn-login").click();
        await expect(page.locator("#login-overlay")).toBeHidden();
        await expect(page.locator("#page-settings")).toHaveClass(/active/, { timeout: 20000 });

        // Cleanup: clear the web password (requires auth, which we have).
        await goToPage(page, "settings");
        await page.locator("#cfg-web-password").fill("");
        await page.locator("#cfg-web-password-confirm").fill("");
        await page.locator("#btn-save-web-password").click();
        await expect(page.locator(".toast").first()).toBeVisible();

        // 401 responses are EXPECTED here — the login gate intentionally
        // blocks API calls while the session is locked.  Filter them out and
        // assert there are no OTHER console/page/network errors.
        const unexpected = errors.filter((e) => !e.includes("401"));
        expect(unexpected, "unexpected console/page/network errors:\n" + unexpected.join("\n")).toEqual([]);
    });

    test("device password mismatch is rejected", async ({ page }) => {
        await goToPage(page, "settings");
        await page.locator("#cfg-device-password").fill(DEVICE_PW);
        await page.locator("#cfg-device-password-confirm").fill("different");
        await page.locator("#btn-save-device-password").click();
        // Assert on a toast containing the expected text (not just the first
        // toast, which may be a stale one from a previous test).
        await expect(page.locator(".toast", { hasText: "do not match" }).first()).toBeVisible();
    });

    test("device password empty is rejected", async ({ page }) => {
        await goToPage(page, "settings");
        await page.locator("#cfg-device-password").fill("");
        await page.locator("#cfg-device-password-confirm").fill("");
        await page.locator("#btn-save-device-password").click();
        await expect(page.locator(".toast", { hasText: "Enter a new device password" }).first()).toBeVisible();
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