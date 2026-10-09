// Samba share + device-password flow (System page → Security → Device
// Password), plus confirming the SMB share actually works FROM THE WORKSTATION
// with the credentials that were just set through the web UI.
//
// Flow:
//   1. Set a throwaway device password via the System page (accepting the
//      custom confirm modal).  The device password is synced to SSH console +
//      Samba share by the backend.
//   2. From THIS workstation, connect to the [metixel-media] SMB share with
//      those credentials and list its contents — proving the share works.
//   3. ALWAYS restore the original device password (default `raspberry`) and
//      verify the share still lists with it.
//
// The workstation side is PLATFORM-SPECIFIC, and BOTH paths are implemented:
//
//   * Windows     — `net use` + `dir`.  Windows caches SMB sessions per user,
//                   so every session and the cached host credential are
//                   dropped first; without that the mapping silently re-uses
//                   the PREVIOUS session and the assertion passes for the
//                   wrong reason.
//   * Linux/macOS — `smbclient -U user%pass -c ls`.  smbclient authenticates
//                   per invocation and keeps no session cache, so there is no
//                   teardown step.  Needs smbclient on the workstation
//                   (`apt install smbclient` / `pacman -S samba`); when it is
//                   absent the test SKIPS with that reason rather than
//                   reporting a false SMB regression.
//
// The frame's device password is assumed to be the default `raspberry` (the
// value the setup script seeds).  If it differs, set `METIXEL_DEVICE_PW`.  The
// share can be overridden with `METIXEL_SMB_SHARE` in either form
// (`\\<host>\<share>` or `//<host>/<share>`).
const { test, expect } = require("@playwright/test");
const { execFileSync, execSync } = require("child_process");
const { goToPage, collectErrors, expectNoErrors } = require("./helpers");

const { clearWebPasswordAndRestart } = require("../ssh-utils");

const IS_WINDOWS = process.platform === "win32";

const HOST = process.env.METIXEL_HOST || "192.168.222.122";
const SMB_USER = process.env.METIXEL_SSH_USER || "pi";
// The device password the frame starts with (default seeded by setup script).
const ORIGINAL_DEVICE_PW = process.env.METIXEL_DEVICE_PW || "raspberry";
// A throwaway password used during the test (restored afterwards).
const TEST_DEVICE_PW = "SmbTestPass123!";

// The share as host + name, so each platform can build the form its own client
// wants (Windows UNC `\\host\share`, POSIX `//host/share`).
function parseShare(spec) {
    const match = spec.replace(/\\/g, "/").match(/^\/{2}([^/]+)\/(.+)$/);
    if (!match) throw new Error(`Unrecognised METIXEL_SMB_SHARE: ${JSON.stringify(spec)}`);
    return { host: match[1], share: match[2] };
}
const { host: SHARE_HOST, share: SHARE_NAME } = parseShare(
    process.env.METIXEL_SMB_SHARE || `//${HOST}/metixel-media`
);

// Blocking sleep on the main thread.  Node permits Atomics.wait here (browsers
// do not), so this replaces the old `ping -n 2 127.0.0.1 >nul` trick — which
// was Windows-only and left a stray `nul` file behind when run on Linux.
function sleepSync(ms) {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function hasCommand(name) {
    try {
        execFileSync("sh", ["-c", `command -v ${name}`], { stdio: "pipe", timeout: 5000 });
        return true;
    } catch (_) {
        return false;
    }
}

// smbclient is the POSIX userspace SMB client: no mount, no sudo, no cached
// session.  Without it the POSIX branch cannot run at all.
const SMB_CLIENT_MISSING = !IS_WINDOWS && !hasCommand("smbclient");
const SMB_CLIENT_HINT =
    "smbclient is not installed on this workstation — `apt install smbclient` " +
    "(Debian/Ubuntu) or `pacman -S samba` (Arch)";

/**
 * List the SMB share with the given password, from this workstation.
 *
 * Dispatches on the host platform (see the module header).  Returns true only
 * if the listing is non-empty AND the connection authenticated with `password`.
 *
 * Retried once: an SMB session teardown can be lazy on Windows, and smbd may
 * still be settling after the backend rewrote its passdb.
 *
 * @returns {boolean} true if the share listed successfully with the creds.
 */
function smbShareLists(password) {
    for (let attempt = 0; attempt < 2; attempt++) {
        if (attempt > 0) sleepSync(2000);
        const listed = IS_WINDOWS
            ? smbShareListsWindows(password)
            : smbShareListsPosix(password);
        if (listed) return true;
    }
    return false;
}

/**
 * Windows: map the share with `net use`, list with `dir`, then unmap.
 *
 * Windows caches SMB sessions per user, so every existing session and the
 * cached host credential are dropped first — otherwise the mapping re-uses the
 * previous password and the assertion passes for the wrong reason.
 */
function smbShareListsWindows(password) {
    const share = `\\\\${SHARE_HOST}\\${SHARE_NAME}`;
    const dropSessions = () => {
        // Ignore failures: there may be nothing cached yet.
        try { execSync(`net use * /delete /y`, { stdio: "pipe", timeout: 30000 }); } catch (_) { }
        try { execSync(`cmdkey /delete:${SHARE_HOST}`, { stdio: "pipe", timeout: 30000 }); } catch (_) { }
    };
    dropSessions();
    try {
        execSync(
            `net use "${share}" /persistent:no /user:${SMB_USER} "${password}"`,
            { stdio: "pipe", timeout: 30000 }
        );
        const listing = execSync(
            `dir "${share}" /b`,
            { stdio: "pipe", timeout: 30000, encoding: "utf-8" }
        );
        return listing.trim().length > 0;
    } catch (_) {
        return false;
    } finally {
        dropSessions();
    }
}

/**
 * Linux/macOS: list the share with `smbclient`.
 *
 * Uses execFileSync with an argument vector rather than a shell string, so a
 * password containing shell metacharacters (`!`, `$`, spaces) cannot break out
 * of the command.  smbclient exits non-zero when the session setup is rejected
 * (NT_STATUS_LOGON_FAILURE), so a throw here means "these credentials were
 * refused" — exactly the signal the test needs.  It keeps no credential cache,
 * so no teardown step is required.
 */
function smbShareListsPosix(password) {
    try {
        const listing = execFileSync(
            "smbclient",
            [`//${SHARE_HOST}/${SHARE_NAME}`, "-U", `${SMB_USER}%${password}`, "-c", "ls"],
            { stdio: "pipe", timeout: 30000, encoding: "utf-8" }
        );
        return listing.trim().length > 0;
    } catch (_) {
        return false;
    }
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
        test.skip(SMB_CLIENT_MISSING, SMB_CLIENT_HINT);

        const errors = collectErrors(page);
        await goToPage(page, "system");

        try {
            // 1. Set the throwaway device password through the web UI.
            await setDevicePassword(page, TEST_DEVICE_PW);
            expectNoErrors(errors);

            // 2. Verify the SMB share lists with the NEW password from the
            //    workstation.  The password change is synchronous server-side.
            expect(
                smbShareLists(TEST_DEVICE_PW),
                "SMB share should be reachable with the UI-set password"
            ).toBe(true);
        } finally {
            // 3. ALWAYS restore, even when step 2 failed.  The device password
            //    is pushed straight to /etc/shadow and Samba's passdb — it is
            //    not in config.json and cannot be read back — so a failure here
            //    silently leaves the frame holding a test password.  That is
            //    exactly what happened before this was wrapped in a finally.
            try {
                await setDevicePassword(page, ORIGINAL_DEVICE_PW);
            } catch (err) {
                // Never mask the original failure, but make the leak loud.
                console.warn(
                    "[samba] Could NOT restore the device password — the frame may " +
                        `still hold the test password; reset it via System -> Security. (${err.message})`
                );
            }
        }

        // 4. Confirm the share still works after restoring.
        expect(
            smbShareLists(ORIGINAL_DEVICE_PW),
            "SMB share should be reachable with the restored password"
        ).toBe(true);
    });
});
