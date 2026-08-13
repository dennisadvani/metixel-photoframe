// OPT-IN destructive tests.  These restart/reboot/shutdown the LIVE frame.
// Only run them deliberately:
//
//   npx playwright test tests/destructive.spec.js
//
// reboot/shutdown take the Pi fully down and are intentionally NOT
// automated — do those manually.
const { test, expect } = require("@playwright/test");
const { goToPage } = require("./helpers");

const runDestructive = process.env.RUN_DESTRUCTIVE === "1";

test.describe("destructive (opt-in)", () => {
    test.skip(!runDestructive, "opt-in — set RUN_DESTRUCTIVE=1 to run");
    test("restart services button works", async ({ page }) => {
        await goToPage(page, "advanced");
        page.on("dialog", (dialog) => dialog.accept()); // dismiss the confirm()
        await page.locator("#btn-restart-services").click();
        // The backend restarts: the connection overlay appears, then clears
        // once the API is back (the SPA auto-reloads on reconnect).
        await expect(page.locator("#connection-overlay")).toBeHidden({ timeout: 45_000 });
    });
});
