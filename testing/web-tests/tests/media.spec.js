// Media page: the library loads, the filters work, and the destructive
// actions (per-item delete, folder create/delete) actually do what the UI
// says they do.
//
// The unit suite covers the HTTP layer thoroughly (test_routes_media.py,
// test_routes_browse.py, test_routes_browse_delete.py).  What it cannot
// reach is the BROWSER behaviour: the confirm dialog, the toast, the tile
// disappearing in place, and the folder-browser modal round-trip.  That is
// what these tests add.
//
// These tests CREATE and DELETE real files on the frame, so every one of
// them cleans up in a `finally`.  The media library is the user's actual
// library — leaving a `webtest-*` file or folder behind on a failure would
// be a real defect, not just untidy.
const { test, expect } = require("@playwright/test");
const { goToPage, collectErrors, expectNoErrors } = require("./helpers");
const { ssh, removeTestFiles } = require("../ssh-utils");

//: Prefix identifying every fixture this spec creates.  Cleanup is a glob on
//: this, so a crash mid-test still leaves the frame tidy on the next run.
const TEST_PREFIX = "webtest-";

//: The user-media folder the frame ships with (the library's default watch
//: path and where uploads land).
const MEDIA_DIR = "/opt/metixel/data/media/my_media";

/**
 * Create a fixture image in the library through the app's own upload API.
 *
 * Deliberately NOT a direct `ssh` write: uploading keeps the backend's
 * file-list cache and the folder watcher consistent, so the tile really does
 * appear in the UI.  Writing behind the app's back would create a file the
 * library may not list until its cache TTL expires, which would make these
 * tests flaky for a reason that has nothing to do with what they assert.
 *
 * The PNG is a 1×1 transparent image — the smallest valid thing that passes
 * the extension whitelist and the optimiser.
 *
 * Navigates to the app first: `page.evaluate` runs in the browser, so it needs
 * a real page origin before a same-origin `fetch` can resolve (a fresh page
 * sits on `about:blank`, where `window.location.origin` is `"null"`).
 */
async function uploadFixture(page, name) {
    const PNG_1X1 = Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        "base64"
    );
    if (!page.url().startsWith("http")) {
        await page.goto("/");
    }
    const result = await page.evaluate(
        async ({ fileName, b64 }) => {
            const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
            const form = new FormData();
            form.append("files", new Blob([bytes], { type: "image/png" }), fileName);
            const resp = await fetch(window.location.origin + "/api/media/upload", {
                method: "POST",
                body: form,
            });
            return resp.ok
                ? await resp.json()
                : { saved: [], errors: [{ name: fileName, error: `HTTP ${resp.status}` }] };
        },
        { fileName: name, b64: PNG_1X1.toString("base64") }
    );
    // The API returns saved as [{name, saved_as, size}], not bare names.
    const savedNames = (result.saved || []).map((s) => (typeof s === "string" ? s : s.name));
    expect(
        savedNames,
        `upload of ${name} failed: ${JSON.stringify(result.errors)}`
    ).toContain(name);
}

/** Whether a fixture file is present on the frame. */
function fixtureExists(name) {
    try {
        const out = ssh(`test -e ${MEDIA_DIR}/${name} && echo yes || echo no`);
        return out.trim() === "yes";
    } catch (_) {
        return false;
    }
}

/** Whether a fixture folder is present on the frame. */
function folderExists(name) {
    return fixtureExists(name);
}

//: How long to wait for an uploaded fixture to become visible in the library.
//:
//: The media list is cached server-side (``_CACHE_TTL = 60.0``), so a
//: just-uploaded file can take up to a minute to appear.  That lag is
//: DELIBERATE, not a defect: the frame still has to generate thumbnails and
//: frames, and the upload result tells the user exactly that —
//: "Saved N file — they'll appear in the slideshow shortly."  Tests must
//: therefore WAIT for the tile rather than assert it immediately; treating the
//: lag as a failure would be asserting against intended behaviour.
//:
//: Must stay BELOW the per-test timeout below, or Playwright kills the test
//: first and reports its own timeout instead of this descriptive one.
const VISIBILITY_TIMEOUT_MS = 75_000;

//: Uploading fixtures has to outlive the cache TTL, which the suite default
//: (60 s) does not allow for.  Only the tests that seed via upload raise it.
const UPLOAD_TEST_TIMEOUT_MS = 120_000;

/**
 * Wait until a fixture is listed by the media API.
 *
 * Polls ``/api/media/list`` (cheap, server-side filtered) instead of the DOM so
 * the wait is independent of which page/filter the UI happens to be showing.
 * Used before driving the UI so a failure downstream is about the feature under
 * test, not about how long the cache took to expire.
 */
async function waitForFixtureListed(page, name) {
    await expect
        .poll(
            async () =>
                page.evaluate(async (file) => {
                    const r = await fetch(
                        window.location.origin +
                            "/api/media/list?limit=200&name=" +
                            encodeURIComponent(file)
                    );
                    if (!r.ok) return -1;
                    const d = await r.json();
                    return (d.items || []).filter((i) => i.name === file).length;
                }, name),
            {
                timeout: VISIBILITY_TIMEOUT_MS,
                message:
                    `${name} never appeared in the media list. The list is cached ` +
                    `for up to 60s by design, so this failing means the upload did ` +
                    `not reach an enabled watch folder.`,
            }
        )
        .toBeGreaterThan(0);
}

/**
 * Read the persisted ``system.upload_dir`` through the API.
 *
 * The page must already be on the app origin for the same-origin fetch to
 * resolve; every caller navigates (via `uploadFixture` or `goToPage`) first.
 *
 * @returns {Promise<string|null>} the configured value, or null if unreadable.
 */
async function readUploadDir(page) {
    if (!page.url().startsWith("http")) await page.goto("/");
    return page.evaluate(async () => {
        const r = await fetch(window.location.origin + "/api/config/system");
        return r.ok ? (await r.json()).upload_dir || "" : null;
    });
}

test.describe("media", () => {
    test.beforeAll(async () => {
        // Sweep up anything a previous interrupted run left behind.
        removeTestFiles(MEDIA_DIR, TEST_PREFIX);
    });

    test.afterAll(async () => {
        removeTestFiles(MEDIA_DIR, TEST_PREFIX);
    });

    test("page loads a populated media list", async ({ page }) => {
        const errors = collectErrors(page);
        await goToPage(page, "media");
        const list = page.locator("#media-list");
        await expect(list).toBeVisible();
        await expect(list).not.toHaveText(/Loading media/);
        for (const id of ["media-filter-name", "media-filter-folder", "media-filter-type"]) {
            await expect(page.locator("#" + id)).toBeVisible();
        }
        expectNoErrors(errors);
    });

    test("deleting an item removes it from the library and from disk", async ({ page }) => {
        // Raised: seeding waits out the 60 s list cache (see VISIBILITY_TIMEOUT_MS).
        test.setTimeout(UPLOAD_TEST_TIMEOUT_MS);
        const errors = collectErrors(page);
        const name = `${TEST_PREFIX}delete-me.png`;

        // Seed via the API the UI itself uses (upload), so this exercises the
        // same path a user's upload takes rather than writing the file behind
        // the app's back.  Then wait for it to be listed — the list cache makes
        // that take up to 60s by design.
        try {
            await uploadFixture(page, name);
            await waitForFixtureListed(page, name);

            await goToPage(page, "media");
            // The library may be filtered from a previous test; search for the
            // exact name so the tile is guaranteed to be present.
            await page.locator("#media-filter-name").fill(name);
            await page.locator("#media-filter-name").press("Enter");

            const tile = page.locator(`.media-item[data-name="${name}"]`);
            await expect(tile).toBeVisible({ timeout: 20_000 });

            // Open the per-item "⋮" menu and choose Delete.
            await tile.locator(".media-menu-btn").click();
            await tile.locator(".media-delete").click();

            // The shared confirm dialog must appear before anything is removed —
            // a destructive action that fires without confirmation is the bug
            // this guards.
            await expect(page.locator("#confirm-modal")).toHaveClass(/open/);
            await expect(page.locator("#confirm-message")).toContainText(name);
            await page.locator("#confirm-ok").click();

            // The tile is removed in place (no full reload) and a toast confirms.
            await expect(tile).toHaveCount(0, { timeout: 20_000 });
            await expect(page.locator(".toast", { hasText: name }).first()).toBeVisible();

            // And the file is genuinely gone from the frame, not just hidden.
            expect(
                fixtureExists(name),
                `${name} should have been removed from the frame's disk`
            ).toBe(false);

            expectNoErrors(errors);
        } finally {
            removeTestFiles(MEDIA_DIR, TEST_PREFIX);
        }
    });

    test("cancelling the delete confirmation keeps the file", async ({ page }) => {
        // The mirror of the test above: a mis-click on Cancel must not destroy
        // anything.  Cheap to check and it pins the dialog's contract.
        test.setTimeout(UPLOAD_TEST_TIMEOUT_MS);
        const name = `${TEST_PREFIX}cancel-me.png`;
        try {
            await uploadFixture(page, name);
            await waitForFixtureListed(page, name);

            await goToPage(page, "media");
            await page.locator("#media-filter-name").fill(name);
            await page.locator("#media-filter-name").press("Enter");

            const tile = page.locator(`.media-item[data-name="${name}"]`);
            await expect(tile).toBeVisible({ timeout: 20_000 });
            await tile.locator(".media-menu-btn").click();
            await tile.locator(".media-delete").click();

            await expect(page.locator("#confirm-modal")).toHaveClass(/open/);
            await page.locator("#confirm-cancel").click();

            await expect(page.locator("#confirm-modal")).not.toHaveClass(/open/);
            // Still on the frame, and still listed.
            expect(fixtureExists(name), `${name} was deleted despite cancelling`).toBe(true);
            await expect(tile).toBeVisible();
        } finally {
            removeTestFiles(MEDIA_DIR, TEST_PREFIX);
        }
    });

    test("folder browser creates a new folder", async ({ page }) => {
        const folderName = `${TEST_PREFIX}created`;
        try {
            await goToPage(page, "media");

            // The "Save to" control opens the shared folder-browser modal.
            await page.locator("#btn-upload-destination").click();
            await expect(page.locator("#folder-browser-modal")).toHaveClass(/open/);

            // Navigate to the user-media folder so the new folder is created
            // somewhere we can clean up (and where creation is permitted).
            await page.locator("#browser-new-folder-name").fill(folderName);
            await page.locator("#btn-browser-create").click();

            await expect(
                page.locator(".toast", { hasText: folderName }).first()
            ).toBeVisible();

            // The modal navigates into the folder it just made, which is the
            // behaviour a user relies on to confirm with Select.
            await expect(page.locator("#browser-current-path")).toContainText(folderName);

            expect(folderExists(folderName), `folder ${folderName} was not created`).toBe(true);
        } finally {
            removeTestFiles(MEDIA_DIR, TEST_PREFIX);
        }
    });

    test("folder browser deletes the folder it is viewing", async ({ page }) => {
        const folderName = `${TEST_PREFIX}deletefolder`;
        try {
            // Seed the folder directly — this test is about deleting, and
            // creating it through the UI is already covered above.
            ssh(`mkdir -p ${MEDIA_DIR}/${folderName}`);
            expect(folderExists(folderName)).toBe(true);

            await goToPage(page, "media");
            await page.locator("#btn-upload-destination").click();
            await expect(page.locator("#folder-browser-modal")).toHaveClass(/open/);

            // Walk into the seeded folder so Delete targets it.
            await page.locator(`.browser-entry[data-path$="${folderName}"]`).first().click();
            await expect(page.locator("#browser-current-path")).toContainText(folderName);

            await page.locator("#btn-browser-delete").click();

            // Deleting a folder is destructive, so it must confirm first — and
            // the confirmation must name the folder.
            await expect(page.locator("#confirm-modal")).toHaveClass(/open/);
            await expect(page.locator("#confirm-message")).toContainText(folderName);
            await page.locator("#confirm-ok").click();

            await expect(
                page.locator(".toast", { hasText: folderName }).first()
            ).toBeVisible();
            expect(folderExists(folderName), `folder ${folderName} was not deleted`).toBe(false);
        } finally {
            removeTestFiles(MEDIA_DIR, TEST_PREFIX);
        }
    });

    test("infinite scroll loads the next page without clicking Load more", async ({ page }) => {
        // Seeds 25 files, each of which must survive the cache TTL.
        test.setTimeout(UPLOAD_TEST_TIMEOUT_MS);
        // The auto-load is an IntersectionObserver watching #media-load-more —
        // unreachable from the unit suite, and the reason this test exists.
        //
        // It needs more than one page of media (>20 items, the toolbar's page
        // size).  We seed exactly enough to force pagination.
        const needed = 25;
        try {
            for (let i = 0; i < needed; i++) {
                const name = `${TEST_PREFIX}scroll-${String(i).padStart(3, "0")}.png`;
                await uploadFixture(page, name);
            }
            // Wait for the LAST one: the list cache clears as a unit, so once
            // the final fixture is listed they all are.  Checking one beats
            // polling 25 times, and it is the same 60s-by-design lag.
            await waitForFixtureListed(page, `${TEST_PREFIX}scroll-024.png`);

            await goToPage(page, "media");
            // Filter to our fixtures only, so the page boundary lands inside a
            // set we control regardless of what else is in the library.
            await page.locator("#media-filter-name").fill(TEST_PREFIX);
            await page.locator("#media-filter-name").press("Enter");

            // Page 1 renders 20 tiles plus the fallback button.
            await expect(page.locator(".media-item").first()).toBeVisible({ timeout: 20_000 });
            const loadMore = page.locator("#media-load-more");
            await expect(loadMore).toBeVisible();

            const before = await page.locator(".media-item").count();
            expect(before).toBeLessThanOrEqual(20);

            // Scroll the button into view — the observer has a 300px bottom
            // rootMargin, so this is what a user's scroll would trigger.  We do
            // NOT click it: clicking is the fallback path, and using it would
            // make this pass even if the observer were broken.
            await loadMore.scrollIntoViewIfNeeded();

            await expect
                .poll(async () => page.locator(".media-item").count(), { timeout: 20_000 })
                .toBeGreaterThan(before);
        } finally {
            removeTestFiles(MEDIA_DIR, TEST_PREFIX);
        }
    });

    test("Save to picker persists a destination separate from the folder filter", async ({ page }) => {
        // Guards the decoupling established when the folder-filter-as-upload-
        // destination design was reverted: browsing must not decide where
        // uploads land.  A regression here would silently send uploads to
        // whatever folder happened to be selected in the filter.
        const original = await readUploadDir(page);
        test.skip(original === null, "could not read the current upload_dir");

        try {
            await goToPage(page, "media");
            // The label shows the persisted destination, not the filter.
            await expect(page.locator("#media-upload-destination")).not.toBeEmpty();

            // Change the FILTER — the persisted destination must not move.
            await page.locator("#media-filter-folder").selectOption({ index: 0 });
            const afterFilter = await readUploadDir(page);
            expect(afterFilter, "changing the folder filter must not change upload_dir").toBe(
                original
            );
        } finally {
            // Nothing was persisted by this test (that is the assertion), but
            // restore defensively in case a regression did write something.
            await page.evaluate(
                async (dir) => {
                    await fetch(window.location.origin + "/api/config/system", {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ upload_dir: dir }),
                    });
                },
                original
            );
        }
    });
});
