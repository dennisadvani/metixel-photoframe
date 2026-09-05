// Samba share + device-password flow (System page → Security → Device
// Password), plus confirming the SMB share actually works FROM THE WORKSTATION
// with the credentials that were just set through the web UI.
//
// Flow:
//   1. Set a throwaway device password via the System page (accepting the
//      custom confirm modal).  The device password is synced to SSH console +
//      Samba share by the backend.
//   2. From the workstation (Node), map the [metixel-media] SMB share with
//      those credentials and list its contents — proving the share works.
//   3. Restore the original device password (default `raspberry`) and verify
//      the share still lists.
//
// The frame's device password is assumed to be the default `raspberry` (the
// value the setup script seeds).  If it differs, set `METIXEL_DEVICE_PW` and
// `METIXEL_SMB_SHARE` (default \\\\<host>\\metixel-media).
const { test, expect } = require("@playwright/test");
const { execSync } = require("child_process");
const { goToPage, collectErrors, expectNoErrors } = require("./helpers");

const { clearWebPasswordAndRestart } = require("../ssh-utils");

const HOST = process.env.METIXEL_HOST || "192.168.222.122";
const SMB_USER = process.env.METIXEL_SSH_USER || "pi";
const SMB_SHARE = process.env.METIXEL_SMB_SHARE || `\\\\${HOST}\\metixel-media`;
// The device password the frame starts with (default seeded by setup script).
const ORIGINAL_DEVICE_PW = process.env.METIXEL_DEVICE_PW || "raspberry";
// A throwaway password used during the test (restored afterwards).
const TEST_DEVICE_PW = "SmbTestPass123!";

/**
 * Map the SMB share with the given password, list its contents, then unmount.
 * Runs from the workstation (Windows `net use` + `dir`).
 *
 * To defeat Windows SMB credential caching, we drop all SMB connections and
 * delete any Credential-Manager entry for the host, so each call authenticates
 * fresh under the given password.  A single retry after a short delay handles
 * the SMB session teardown being lazy on Windows.
 *
 * @returns {boolean} true if the share listed successfully with the creds.
 */
function smbShareLists(password) {
    const share = SMB_SHARE;
    const user = SMB_USER;
    const attempts = [0, 1];
    for (const i of attempts) {
        if (i > 0) execSync(`ping -n 2 127.0.0.1 >nul`, { stdio: "ignore", timeout: 5000 });
        // Drop any existing SMB session + cached host credential so the auth
        // below is fresh.  Ignore failures (nothing cached yet).
        try { execSync(`net use * /delete /y`, { stdio: "pipe", timeout: 30000 }); } catch (_) { }
        try { execSync(`cmdkey /delete:${HOST}`, { stdio: "pipe", timeout: 30000 }); } catch (_) { }
        try {
            execSync(
                `net use "${share}" /persistent:no /user:${user} "${password}"`,
                { stdio: "pipe", timeout: 30000 }
            );
            const listing = execSync(
                `dir "${share}" /b`,
                { stdio: "pipe", timeout: 30000, encoding: "utf-8" }
            );
            // This call only succeeds if the listing is non-empty AND the auth
            // used the given password (fresh session above).
            return listing.trim().length > 0;
        } catch (err) {
            // Best-effort cleanup, then retry once more.
            try { execSync(`net use * /delete /y`, { stdio: "pipe", timeout: 30000 }); } catch (_) { }
            try { execSync(`cmdkey /delete:${HOST}`, { stdio: "pipe", timeout: 30000 }); } catch (_) { }
        }
    }
    return false;
}

async function setDevicePassword(page, password) {
    // The custom confirm modal (#confirm-ok) must be accepted — the JS uses a
    // modal, not a native dialog, so page.on("dialog") would NOT catch it.
    await page.locator("#cfg-device-password").fill(password);
    await page.locator("#cfg-device-password-confirm").fill(password);
    await page.locator("#btn-save-device-password").click();
    await expect(page.locator("#confirm-modal")).toHaveClass(/open/);
    await page.locator("#confirm-ok").click();
    await expect(page.locator(".toast").first()).toBeVisible();
}

test.describe("samba", () => {
    test.beforeAll(async () => {
        await clearWebPasswordAndRestart();
    });

    test("device password set via UI + SMB share works from workstation", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "system");

        // 1. Set the throwaway device password through the web UI.
        await setDevicePassword(page, TEST_DEVICE_PW);
        expectNoErrors(errors);

        // 2. Verify the SMB share lists with the NEW password from the
        //    workstation.  The password change is synchronous server-side.
        expect(smbShareLists(TEST_DEVICE_PW), "SMB share should be reachable with the UI-set password").toBe(true);

        // 3. Restore the original device password.
        await setDevicePassword(page, ORIGINAL_DEVICE_PW);

        // 4. Confirm the share still works after restoring.
        expect(smbShareLists(ORIGINAL_DEVICE_PW), "SMB share should be reachable with the restored password").toBe(true);
    });
});