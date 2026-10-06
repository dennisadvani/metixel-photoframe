// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Google Photos sync module (Sources page).
 *
 * Mirrors the Immich card: connection test, sync settings, manual sync with
 * live progress and status.  Google removed library-wide access for apps in
 * March 2025, so there is no album list to browse and the card offers two
 * modes instead:
 *
 *   - "picker"     — the user selects media in Google's own picker (supported,
 *                    but a selection is a fixed snapshot).
 *   - "shared_url" — a public shared-album link, read from the share page
 *                    (undocumented, but follows the album as it grows).
 */

import {
    apiGet,
    apiPost,
    apiPut,
    confirmDialog,
    escapeHtml,
    setButtonBusy,
    setChecked,
    setValue,
    showToast
} from "./core.js";

import { loadMedia } from "./media-page.js";

    var MODE_PICKER = "picker";
    var MODE_SHARED_URL = "shared_url";

    var _bound = false;
    var _syncPollTimer = null;
    var _sessionPollTimer = null;
    var _syncWasActive = false;
    var _syncRestoreBtn = null;   // setButtonBusy restore fn for Sync Now
    var _awaitingPick = false;    // true while the user is picking in Google

    // -- Polling --------------------------------------------------------------

    function startSyncPolling() {
        if (_syncPollTimer) return;
        function _poll() {
            _syncPollTimer = setTimeout(async function () {
                await refreshStatus();
                _poll();
            }, 1500);
        }
        _poll();
    }

    function stopSyncPolling() {
        if (_syncPollTimer) {
            clearTimeout(_syncPollTimer);
            _syncPollTimer = null;
        }
        var cancelBtn = document.getElementById("btn-gphotos-cancel-sync");
        if (cancelBtn) cancelBtn.classList.add("hidden");
        var progressDiv = document.getElementById("gphotos-progress");
        if (progressDiv) progressDiv.classList.add("hidden");
    }

    /**
     * Poll the picking session until the user has finished choosing media.
     *
     * @param {number} seconds Google's recommended poll interval.
     */
    function startSessionPolling(seconds) {
        stopSessionPolling();
        var delay = Math.max(2, seconds || 5) * 1000;
        _sessionPollTimer = setTimeout(async function () {
            _sessionPollTimer = null;
            var data = await apiGet("/google-photos/session");
            var session = (data && data.session) || null;
            _renderSession(session);
            if (!session || session.expired) {
                _awaitingPick = false;
                if (session && session.expired) {
                    showToast("The Google Photos selection expired — pick again", "info", 6000);
                }
                return;
            }
            if (session.media_items_set) {
                // Picking finished — import it straight away, which is what
                // the user just asked for by picking.
                if (_awaitingPick) {
                    _awaitingPick = false;
                    await _startSync({ silent: false });
                }
                return;
            }
            startSessionPolling(session.poll_interval_seconds);
        }, delay);
    }

    function stopSessionPolling() {
        if (_sessionPollTimer) {
            clearTimeout(_sessionPollTimer);
            _sessionPollTimer = null;
        }
    }

    // -- Rendering ------------------------------------------------------------

    function _toggleInterval(enabled) {
        var el = document.getElementById("gphotos-interval-group");
        if (el) el.style.display = enabled ? "" : "none";
    }

    /** The mode the form is currently showing. */

    function _selectedMode() {
        var el = document.getElementById("cfg-gphotos-mode");
        return el && el.value === MODE_SHARED_URL ? MODE_SHARED_URL : MODE_PICKER;
    }

    function _albumUrl() {
        var el = document.getElementById("cfg-gphotos-album-url");
        return el ? el.value.trim() : "";
    }

    /**
     * Show only the fields the chosen mode uses.
     *
     * Picking is meaningless for a shared album, so that row goes away too —
     * leaving it would offer a button the backend refuses.
     */
    function _applyMode(mode) {
        var shared = mode === MODE_SHARED_URL;
        var pickerBlock = document.getElementById("gphotos-picker-block");
        var urlBlock = document.getElementById("gphotos-url-block");
        var pickRow = document.getElementById("gphotos-pick-row");
        if (pickerBlock) pickerBlock.classList.toggle("hidden", shared);
        if (urlBlock) urlBlock.classList.toggle("hidden", !shared);
        if (pickRow) pickRow.classList.toggle("hidden", shared);
        if (shared) {
            stopSessionPolling();
            _awaitingPick = false;
        }
        var resultEl = document.getElementById("gphotos-test-result");
        if (resultEl) { resultEl.textContent = ""; resultEl.style.color = ""; }
    }

    function _formatCountdown(seconds) {
        if (seconds === null || seconds === undefined) return "";
        var total = Math.max(0, Math.round(seconds));
        if (total < 60) return total + "s";
        var minutes = Math.floor(total / 60);
        if (minutes < 60) return minutes + "m";
        return Math.floor(minutes / 60) + "h " + (minutes % 60) + "m";
    }

    /** Show the picker link and what the session is currently doing. */

    function _renderSession(session) {
        var wrap = document.getElementById("gphotos-session");
        var link = document.getElementById("gphotos-picker-link");
        var stateEl = document.getElementById("gphotos-session-state");
        var clearBtn = document.getElementById("btn-gphotos-clear-session");
        if (!wrap || !link || !stateEl) return;

        if (!session) {
            wrap.classList.add("hidden");
            link.classList.add("hidden");
            link.removeAttribute("href");
            stateEl.textContent = "";
            if (clearBtn) clearBtn.classList.add("hidden");
            return;
        }

        wrap.classList.remove("hidden");
        if (clearBtn) clearBtn.classList.remove("hidden");

        if (session.picker_uri && !session.expired) {
            link.href = session.picker_uri;
            link.classList.remove("hidden");
        } else {
            link.classList.add("hidden");
            link.removeAttribute("href");
        }

        var left = _formatCountdown(session.expires_in_seconds);
        if (session.expired) {
            stateEl.innerHTML = "Selection expired — click Pick Photos to choose again.";
        } else if (!session.media_items_set) {
            stateEl.innerHTML = "Waiting for you to pick photos in Google Photos…"
                + (left ? " <span style=\"color:var(--text-muted)\">(expires in " + escapeHtml(left) + ")</span>" : "");
        } else {
            var count = session.picked_count
                ? session.picked_count + " item" + (session.picked_count === 1 ? "" : "s")
                : "Selection";
            stateEl.innerHTML = escapeHtml(count) + " picked"
                + (session.imported_at ? " · imported" : " · ready to sync")
                + (left ? " <span style=\"color:var(--text-muted)\">(expires in " + escapeHtml(left) + ")</span>" : "");
        }
    }

    /** Refresh the status card (last sync + live progress). */

    async function refreshStatus() {
        var statusEl = document.getElementById("gphotos-sync-status");
        var textEl = document.getElementById("gphotos-status-text");
        var detailEl = document.getElementById("gphotos-status-detail");
        var errorsEl = document.getElementById("gphotos-errors");
        var progressDiv = document.getElementById("gphotos-progress");
        var cancelBtn = document.getElementById("btn-gphotos-cancel-sync");
        if (!statusEl || !textEl || !detailEl) return;

        var data = await apiGet("/google-photos/status");
        if (!data) return;

        // A shared album has no picking session to show.
        _renderSession(data.mode === MODE_SHARED_URL ? null : data.session);

        // ── Live progress ──────────────────────────────────────────
        var prog = data.progress;
        if (prog && prog.syncing) {
            if (progressDiv) progressDiv.classList.remove("hidden");
            if (cancelBtn) cancelBtn.classList.remove("hidden");

            var phaseText = {
                "starting": "Starting…",
                "fetching_items": "Reading your selection…",
                "fetching_album": "Reading the shared album…",
                "downloading": "Downloading",
                "cleaning": "Cleaning up…",
                "cancelled": "Cancelled",
                "error": "Error",
            }[prog.phase || ""] || (prog.phase || "");

            var phaseEl = document.getElementById("gphotos-progress-phase");
            if (phaseEl) phaseEl.textContent = phaseText;

            var countEl = document.getElementById("gphotos-progress-count");
            if (countEl) countEl.textContent = prog.total > 0 ? prog.processed + " / " + prog.total : "";

            var barEl = document.getElementById("gphotos-progress-bar");
            if (barEl) {
                barEl.style.width = prog.total > 0
                    ? Math.round(prog.processed / prog.total * 100) + "%"
                    : "0%";
            }

            var fileEl = document.getElementById("gphotos-current-file");
            if (fileEl) fileEl.textContent = prog.current_file || "";

            _syncWasActive = true;
        } else {
            if (progressDiv) progressDiv.classList.add("hidden");
            if (cancelBtn) cancelBtn.classList.add("hidden");

            if (_syncWasActive) {
                _syncWasActive = false;
                stopSyncPolling();
                if (_syncRestoreBtn) { _syncRestoreBtn(); _syncRestoreBtn = null; }
                // New files mean new media — refresh the library view.
                if (typeof loadMedia === "function") loadMedia();
            }
        }

        // ── Last result ───────────────────────────────────────────
        statusEl.classList.remove("hidden");

        if (data.status === "never_run" || !data.last_sync) {
            textEl.textContent = "Never run";
            textEl.style.color = "var(--text-muted)";
            detailEl.textContent = "";
            if (errorsEl) { errorsEl.classList.add("hidden"); errorsEl.innerHTML = ""; }
            return;
        }

        var s = data.last_sync;
        var ago = "just now";
        if (s.finished_at) {
            var seconds = Math.max(0, Math.floor(Date.now() / 1000 - s.finished_at));
            if (seconds < 60) ago = seconds + "s ago";
            else if (seconds < 3600) ago = Math.floor(seconds / 60) + "m ago";
            else ago = Math.floor(seconds / 3600) + "h " + Math.floor((seconds % 3600) / 60) + "m ago";
        }

        var hasCancel = s.errors && s.errors.some(function (e) { return e.indexOf("Cancelled") >= 0; });

        if (s.success) {
            textEl.textContent = ago + " — Success";
            textEl.style.color = "var(--text)";
        } else if (hasCancel) {
            textEl.textContent = ago + " — Cancelled";
            textEl.style.color = "var(--text-muted)";
        } else {
            textEl.textContent = ago + " — Completed with errors";
            textEl.style.color = "#f0a030";
        }

        var parts = [];
        if (s.album_title) parts.push("“" + s.album_title + "”");
        if (s.total_remote > 0) {
            parts.push(s.total_remote + (data.mode === MODE_SHARED_URL ? " in album" : " picked"));
        }
        if (s.album_pages > 1) parts.push(s.album_pages + " pages read");
        if (s.downloaded > 0) parts.push(s.downloaded + " downloaded");
        if (s.skipped > 0) parts.push(s.skipped + " already here");
        if (s.deleted > 0) parts.push(s.deleted + " deleted");
        if (s.duration_seconds) parts.push("took " + s.duration_seconds + "s");
        detailEl.textContent = parts.join(" · ");

        if (errorsEl) {
            var realErrors = (s.errors || []).filter(function (e) { return e.indexOf("Cancelled") < 0; });
            if (realErrors.length > 0) {
                errorsEl.classList.remove("hidden");
                errorsEl.innerHTML = realErrors.map(function (e) { return "<li>" + escapeHtml(e) + "</li>"; }).join("");
            } else {
                errorsEl.classList.add("hidden");
                errorsEl.innerHTML = "";
            }
        }
    }

    // -- Actions --------------------------------------------------------------

    function _credentials() {
        return {
            client_id: document.getElementById("cfg-gphotos-client-id").value.trim(),
            client_secret: document.getElementById("cfg-gphotos-client-secret").value.trim(),
            refresh_token: document.getElementById("cfg-gphotos-refresh-token").value.trim(),
        };
    }

    /** What the Test Connection probe needs for the chosen mode. */

    function _probePayload() {
        var mode = _selectedMode();
        if (mode === MODE_SHARED_URL) {
            return { mode: mode, shared_album_url: _albumUrl() };
        }
        var creds = _credentials();
        creds.mode = mode;
        return creds;
    }

    /** Kick off a background import of the current selection. */

    async function _startSync(opts) {
        var btn = document.getElementById("btn-gphotos-sync-now");

        // The route refuses an import with nothing picked, but a non-2xx body
        // never reaches us (apiPost returns null), so say it here instead of
        // showing a useless "unknown error".
        var status = await apiGet("/google-photos/status");
        if (status && !status.configured) {
            showToast(
                status.mode === MODE_SHARED_URL
                    ? "Paste a shared album link first — then test the connection"
                    : "Connect Google Photos first — test the connection above",
                "info",
                6000
            );
            return false;
        }
        // A shared album is re-read on every sync, so nothing is picked.
        if (status && status.mode !== MODE_SHARED_URL) {
            if (!status.session) {
                showToast("Pick photos in Google Photos first", "info", 5000);
                return false;
            }
            if (status.session.expired) {
                showToast("The selection expired — click Pick Photos again", "info", 6000);
                return false;
            }
        }

        _syncRestoreBtn = setButtonBusy(btn, "Syncing…");

        var result = await apiPost("/google-photos/sync", {});
        if (result && result.status === "started") {
            if (!opts || !opts.silent) showToast("Sync started — check status below", "info");
            _syncWasActive = true;  // sync may finish before the first poll
            startSyncPolling();
            return true;
        }
        showToast(
            "Could not start sync: " + ((result && (result.error || result.message)) || "unknown error"),
            "error",
            5000
        );
        if (_syncRestoreBtn) { _syncRestoreBtn(); _syncRestoreBtn = null; }
        return false;
    }

    async function loadGooglePhotos() {
        var config = await apiGet("/config");
        if (!config) return;

        var cfg = (config.sync && config.sync.google_photos) || {};
        setChecked("cfg-gphotos-enabled", cfg.enabled || false);
        setValue("cfg-gphotos-client-id", cfg.client_id || "");
        setValue("cfg-gphotos-client-secret", cfg.client_secret || "");
        setValue("cfg-gphotos-refresh-token", cfg.refresh_token || "");
        setValue("cfg-gphotos-sync-dir", cfg.sync_dir || "media/sync/google_photos/");
        setValue("cfg-gphotos-interval", ((cfg.poll_interval_seconds || 3600) / 3600).toFixed(1));
        setChecked("cfg-gphotos-strict", cfg.strict_sync === true);
        _toggleInterval(cfg.enabled || false);

        await refreshStatus();

        // Resume polling if a sync is running, or if a pick is still open.
        var statusData = await apiGet("/google-photos/status");
        if (statusData && statusData.progress && statusData.progress.syncing) {
            startSyncPolling();
        }
        if (statusData && statusData.session
            && !statusData.session.media_items_set && !statusData.session.expired) {
            startSessionPolling(statusData.session.poll_interval_seconds);
        }

        if (_bound) return;
        _bound = true;

        document.getElementById("cfg-gphotos-enabled")?.addEventListener("change", function () {
            _toggleInterval(this.checked);
        });

        // -- Test Connection --
        document.getElementById("btn-test-gphotos")?.addEventListener("click", async function () {
            var resultEl = document.getElementById("gphotos-test-result");
            var restore = setButtonBusy(this, "Testing…");
            if (resultEl) { resultEl.textContent = "Testing…"; resultEl.style.color = ""; }

            var creds = _credentials();
            // The route always answers 200 once the body is valid: an upstream
            // failure comes back as {ok:false, error} rather than a 401/502
            // the API layer would mistake for OUR auth/backend state.
            var data = await apiPost("/google-photos/test-connection", creds);
            restore();
            if (!data) {
                if (resultEl) { resultEl.textContent = "Request failed"; resultEl.style.color = "var(--danger)"; }
                return;
            }
            if (data.ok) {
                if (resultEl) { resultEl.textContent = data.message; resultEl.style.color = "var(--text)"; }
                showToast("Connected to Google Photos!", "success");
                // Persist the credentials so Pick Photos works immediately.
                await apiPut("/config/sync", { google_photos: creds });
            } else {
                if (resultEl) { resultEl.textContent = data.error || "Unknown error"; resultEl.style.color = "var(--danger)"; }
                showToast("Connection failed: " + (data.error || "unknown error"), "error", 6000);
            }
        });

        // -- Pick Photos --
        document.getElementById("btn-gphotos-pick")?.addEventListener("click", async function () {
            var restore = setButtonBusy(this, "Opening…");
            var data = await apiPost("/google-photos/session", {});
            restore();
            if (!data || data.status !== "ok" || !data.session) {
                showToast(
                    "Could not open the Google picker: " + ((data && data.error) || "unknown error"),
                    "error",
                    6000
                );
                return;
            }
            _renderSession(data.session);
            _awaitingPick = true;
            // A popup may be blocked — the link stays on the card either way.
            if (data.session.picker_uri) {
                window.open(data.session.picker_uri, "_blank", "noopener");
            }
            showToast("Pick your photos in Google Photos, then come back", "info", 6000);
            startSessionPolling(data.session.poll_interval_seconds);
        });

        // -- Clear Selection --
        document.getElementById("btn-gphotos-clear-session")?.addEventListener("click", async function () {
            if (!(await confirmDialog(
                "Forget the current Google Photos selection?\n\nAlready-downloaded media stays on the frame.",
                { title: "Clear selection?", okText: "Clear" }
            ))) return;
            stopSessionPolling();
            _awaitingPick = false;
            var data = await apiPost("/google-photos/session/clear", {});
            if (data && data.status === "ok") {
                _renderSession(null);
                showToast("Selection cleared", "success");
            } else {
                showToast("Could not clear the selection", "error");
            }
        });

        // -- Save Settings --
        document.getElementById("btn-save-gphotos")?.addEventListener("click", async function () {
            var restore = setButtonBusy(this, "Saving…");
            var creds = _credentials();
            var result = await apiPut("/config/sync", {
                google_photos: {
                    enabled: document.getElementById("cfg-gphotos-enabled").checked,
                    client_id: creds.client_id,
                    client_secret: creds.client_secret,
                    refresh_token: creds.refresh_token,
                    sync_dir: document.getElementById("cfg-gphotos-sync-dir").value,
                    strict_sync: document.getElementById("cfg-gphotos-strict").checked,
                    poll_interval_seconds:
                        Math.round(parseFloat(document.getElementById("cfg-gphotos-interval").value) * 3600) || 3600,
                },
            });
            restore();
            showToast(
                result ? "Google Photos settings saved!" : "Failed to save Google Photos settings",
                result ? "success" : "error"
            );
        });

        // -- Sync Now --
        document.getElementById("btn-gphotos-sync-now")?.addEventListener("click", async function () {
            await _startSync({ silent: false });
        });

        // -- Cancel Sync --
        document.getElementById("btn-gphotos-cancel-sync")?.addEventListener("click", async function () {
            var result = await apiPost("/google-photos/cancel", {});
            if (result && result.status === "ok") {
                showToast("Cancelling sync…", "info");
            }
        });
    }

export { loadGooglePhotos };
