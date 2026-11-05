// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Google Photos sync module (Sources page).
 *
 * Mirrors the Immich card: connection test, sync settings, manual sync with
 * live progress and status.  The frame mirrors one public shared album — the
 * user pastes the album's share link and every sync re-reads the whole album
 * (with strict sync, removed photos leave the frame too).  No Google account
 * access is involved.
 */

import {
    apiGet,
    apiLastError,
    apiPost,
    apiPut,
    escapeHtml,
    setButtonBusy,
    setChecked,
    setValue,
    showToast,
    timeAgo
} from "./core.js";

import { loadMedia } from "./media-page.js";

    var _bound = false;
    var _syncPollTimer = null;
    var _syncWasActive = false;
    var _syncRestoreBtn = null;   // setButtonBusy restore fn for Sync Now
    var _lastStatus = null;       // most recent /google-photos/status payload
    // A manual sync is "running" from the click until either a poll has seen
    // it in progress or last_sync advances past what it was at the click — the
    // backend runs a network check before it writes its first progress file,
    // so the first poll can land before there is anything to see.
    var _syncSeen = false;
    var _syncStartedAt = 0;        // Date.now() at the click
    var _syncBaselineFinished = 0; // last_sync.finished_at at the click
    var SYNC_START_GRACE_MS = 30000;

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

    // -- Rendering ------------------------------------------------------------

    function _toggleInterval(enabled) {
        var el = document.getElementById("gphotos-interval-group");
        if (el) el.style.display = enabled ? "" : "none";
    }

    function _albumUrl() {
        var el = document.getElementById("cfg-gphotos-album-url");
        return el ? el.value.trim() : "";
    }

    /** Refresh the status card (last sync + live progress); returns the payload. */

    async function refreshStatus() {
        var statusEl = document.getElementById("gphotos-sync-status");
        var textEl = document.getElementById("gphotos-status-text");
        var detailEl = document.getElementById("gphotos-status-detail");
        var errorsEl = document.getElementById("gphotos-errors");
        var progressDiv = document.getElementById("gphotos-progress");
        var cancelBtn = document.getElementById("btn-gphotos-cancel-sync");
        if (!statusEl || !textEl || !detailEl) return;

        var data = await apiGet("/google-photos/status");
        if (!data) return null;
        _lastStatus = data;

        // ── Live progress ──────────────────────────────────────────
        var prog = data.progress;
        if (prog && prog.syncing) {
            _syncSeen = true;
            if (progressDiv) progressDiv.classList.remove("hidden");
            if (cancelBtn) cancelBtn.classList.remove("hidden");

            var phaseText = {
                "starting": "Starting…",
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

            if (_syncWasActive && _syncIsOver(data)) {
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
            return data;
        }

        var s = data.last_sync;
        var ago = timeAgo(s.finished_at) || "just now";

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
        if (s.total_remote > 0) parts.push(s.total_remote + " in album");
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
        return data;
    }

    /** True once a sync started by this page has demonstrably finished. */

    function _syncIsOver(data) {
        if (_syncSeen) return true;
        var finished = (data && data.last_sync && data.last_sync.finished_at) || 0;
        if (finished > _syncBaselineFinished) return true;
        // Never saw it start and nothing new was recorded: give it a while,
        // then assume the thread died before writing anything.
        return Date.now() - _syncStartedAt > SYNC_START_GRACE_MS;
    }

    // -- Actions --------------------------------------------------------------

    /** Kick off a background sync of the shared album. */

    async function _startSync(opts) {
        var btn = document.getElementById("btn-gphotos-sync-now");

        // The route refuses an unconfigured sync with a 400 whose message
        // apiLastError() carries, so no pre-flight status request is needed.
        _syncRestoreBtn = setButtonBusy(btn, "Syncing…");
        _syncBaselineFinished =
            (_lastStatus && _lastStatus.last_sync && _lastStatus.last_sync.finished_at) || 0;
        _syncSeen = false;
        _syncStartedAt = Date.now();

        var result = await apiPost("/google-photos/sync", {});
        if (result && result.status === "started") {
            if (!opts || !opts.silent) showToast("Sync started — check status below", "info");
            _syncWasActive = true;  // sync may finish before the first poll
            startSyncPolling();
            return true;
        }
        showToast(
            "Could not start sync: " +
                ((result && (result.error || result.message)) || apiLastError() || "unknown error"),
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
        setValue("cfg-gphotos-album-url", cfg.shared_album_url || "");
        setValue("cfg-gphotos-sync-dir", cfg.sync_dir || "media/sync/google_photos/");
        setValue("cfg-gphotos-interval", ((cfg.poll_interval_seconds || 3600) / 3600).toFixed(1));
        setChecked("cfg-gphotos-strict", cfg.strict_sync === true);
        _toggleInterval(cfg.enabled || false);

        // Resume polling if a sync is already running.
        var statusData = await refreshStatus();
        if (statusData && statusData.progress && statusData.progress.syncing) {
            _syncWasActive = true;
            startSyncPolling();
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

            var payload = { shared_album_url: _albumUrl() };
            // The route always answers 200 once the body is valid: an upstream
            // failure comes back as {ok:false, error} rather than a 502 the
            // API layer would mistake for OUR backend being down.
            var data = await apiPost("/google-photos/test-connection", payload);
            restore();
            if (!data) {
                if (resultEl) {
                    resultEl.textContent = apiLastError() || "Request failed";
                    resultEl.style.color = "var(--danger)";
                }
                return;
            }
            if (data.ok) {
                if (resultEl) { resultEl.textContent = data.message; resultEl.style.color = "var(--text)"; }
                showToast("Shared album found!", "success");
                // Persist the link that was just proven to work, so Sync Now
                // works without a separate save step.
                await apiPut("/config/sync", { google_photos: payload });
            } else {
                if (resultEl) { resultEl.textContent = data.error || "Unknown error"; resultEl.style.color = "var(--danger)"; }
                showToast("Connection failed: " + (data.error || "unknown error"), "error", 6000);
            }
        });

        // -- Save Settings --
        document.getElementById("btn-save-gphotos")?.addEventListener("click", async function () {
            var restore = setButtonBusy(this, "Saving…");
            var result = await apiPut("/config/sync", {
                google_photos: {
                    enabled: document.getElementById("cfg-gphotos-enabled").checked,
                    shared_album_url: _albumUrl(),
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
