// Shared helpers for the Metixel dashboard Playwright suite.
// Targets a LIVE frame — every page/field here is the real SPA served by
// the frame's backend (see playwright.config.js for METIXEL_URL).
const { expect } = require("@playwright/test");
const { waitForHealth, isHealthy } = require("../ssh-utils");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Navigating can lose a race with the frame's server and fail with a raw
// transport error (`net::ERR_CONNECTION_REFUSED` / `_RESET`) instead of an HTTP
// status.  That is NOT an app fault: the suite's own action tests save
// pipeline-affecting settings, and the backend schedules a restart for those
// (see routes/config.py → `systemctl restart metixel-backend`), so the server is
// briefly down while later tests run.  That is why the failure lands on a
// different test each run — whichever one happens to navigate inside the
// restart window.
//
// So on one of these errors, wait for /api/health before retrying: re-attempting
// instantly cannot win a race against a ~6 s service restart.  A real 404/500 or
// a broken page is not matched here and still fails loudly below.
const _TRANSIENT_NAV_ERROR =
    /net::ERR_(CONNECTION_RESET|CONNECTION_REFUSED|CONNECTION_CLOSED|CONNECTION_ABORTED|EMPTY_RESPONSE|NETWORK_CHANGED|SOCKET_NOT_CONNECTED)/;

// Navigate to a hash-based SPA route and wait until the page section is
// active and the API connection overlay has cleared.
async function goToPage(page, pageName) {
    for (let attempt = 0; ; attempt++) {
        try {
            await page.goto("/#" + pageName);
            break;
        } catch (err) {
            const message = String((err && err.message) || err);
            if (attempt >= 2 || !_TRANSIENT_NAV_ERROR.test(message)) throw err;
            await waitForHealth(30_000);
        }
    }
    await expect(page.locator("#page-" + pageName)).toHaveClass(/active/);
    await expect(page.locator("#connection-overlay")).toBeHidden();
}

/**
 * Wait for the backend to bounce and come back.
 *
 * A pipeline-affecting config save schedules `systemctl restart metixel-backend`
 * via schedule_sudo() (see routes/config.py), which DELAYS ~2 s so the HTTP
 * response flushes first.  So at the moment a save resolves the OLD server is
 * still answering — calling waitForHealth() straight away would succeed against
 * the server that is about to die, and the restart would then land in whatever
 * test runs next.  Hence: wait for it to go DOWN, then for it to serve again.
 *
 * Only call this for a save that actually schedules a restart (routes/config.py
 * `needs_rebuild`): a save that does not bounce the service would just spin here
 * until the timeout.  The two failure messages say which case happened.
 */
async function waitForBackendRestart(timeoutMs = 30_000) {
    const deadline = Date.now() + timeoutMs;
    let wentDown = false;
    while (Date.now() < deadline) {
        if (await isHealthy()) {
            if (wentDown) return;
        } else {
            wentDown = true;
        }
        await sleep(250);
    }
    throw new Error(
        wentDown
            ? `Backend went down but was still not serving after ${timeoutMs} ms`
            : `Backend never restarted within ${timeoutMs} ms — did this save actually ` +
              "schedule a restart? (see routes/config.py needs_rebuild)"
    );
}

// Attach listeners that record console/page/network errors for a page.
function collectErrors(page) {
    const errors = [];
    page.on("console", (msg) => {
        if (msg.type() === "error") errors.push("console: " + msg.text());
    });
    page.on("pageerror", (err) => errors.push("pageerror: " + String(err)));
    page.on("requestfailed", (req) => {
        // Record WHY it failed.  A connection-level reason means the frame's
        // backend went away (see _TRANSIENT_REQUEST_FAILURE); a resource the
        // server actually answered badly arrives as an HTTP status instead, via
        // the response handler below.
        const failure = req.failure();
        const reason = (failure && failure.errorText) || "";
        errors.push("requestfailed: " + req.url() + (reason ? " (" + reason + ")" : ""));
    });
    // Browser console messages don't include the URL for failed requests —
    // capture the response too so a 404/500 shows exactly which resource it was.
    page.on("response", (res) => {
        if (res.status() >= 400) errors.push("network: HTTP " + res.status() + " " + res.url());
    });
    return errors;
}

// A current-media thumbnail that was regenerated (or whose cache was cleared)
// can 404 for one poll cycle while the frontend rewrites current_media.json.
// That is a transient, self-healing condition — not a page fault — so drop the
// thumbnail endpoint from the collected errors.  The backend also withholds the
// URL when the file is missing; this is belt-and-braces for the poll race.
const _IGNORED_ERROR = /\/api\/media\/thumbnail\//;

// A connection-level request failure is not a page fault either: the suite's own
// action tests save pipeline-affecting settings and the backend schedules a
// restart for those (routes/config.py → `systemctl restart metixel-backend`), so
// anything in flight while the server goes down legitimately fails with
// ERR_CONNECTION_*.  Deliberately narrow — it matches only the transport reason,
// so a 404/500 or any server-answered failure still fails the test.
const _TRANSIENT_REQUEST_FAILURE =
    /requestfailed: .*\(net::ERR_(CONNECTION_RESET|CONNECTION_REFUSED|CONNECTION_CLOSED|CONNECTION_ABORTED|EMPTY_RESPONSE|NETWORK_CHANGED|SOCKET_NOT_CONNECTED)\)/;

function relevantErrors(errors) {
    return errors.filter(
        (e) => !_IGNORED_ERROR.test(e) && !_TRANSIENT_REQUEST_FAILURE.test(e)
    );
}

function expectNoErrors(errors) {
    const relevant = relevantErrors(errors);
    expect(relevant, "console/page/network errors:\n" + relevant.join("\n")).toEqual([]);
}

// Capture a field's value, change it, save, verify it persisted after a
// reload, then restore the original value so the frame is left unchanged.
//
// Set `restartsBackend` for a field whose save button posts a section/key that
// makes the backend schedule a restart, so each of the two saves is followed by
// a wait for the service to come back.  Without it the scheduled restart lands
// in a LATER test and surfaces there as a random connection error.
async function assertSaveRestores(page, { field, saveBtn, value, restartsBackend = false }) {
    const original = await page.locator(field).inputValue();
    await page.locator(field).fill(String(value));
    await page.locator(saveBtn).click();
    await expect(page.locator(".toast").first()).toBeVisible();
    if (restartsBackend) await waitForBackendRestart();
    // Reload → the saved value should have persisted on the frame.
    await page.reload();
    await expect(page.locator(field)).toHaveValue(String(value));
    // Restore the original value.
    await page.locator(field).fill(original);
    await page.locator(saveBtn).click();
    await expect(page.locator(".toast").first()).toBeVisible();
    if (restartsBackend) await waitForBackendRestart();
}

module.exports = {
    goToPage,
    collectErrors,
    expectNoErrors,
    relevantErrors,
    assertSaveRestores,
    waitForBackendRestart,
};

