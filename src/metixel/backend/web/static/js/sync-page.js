// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Immich sync page module. Album picker, synced-albums list, sync controls and live sync-status polling.
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

    // -- Sync ---------------------------------------------------------------

    var _syncBound = false;
    var _syncPollTimer = null;
    var _syncWasActive = false;
    var _syncRestoreBtn = null;  // setButtonBusy restore fn for the Sync Now button
    var _albumData = null;  // Cached album list from GET /api/immich/albums

    function startSyncPolling() {
        if (_syncPollTimer) return;
        function _poll() {
            _syncPollTimer = setTimeout(async function () {
                await refreshSyncStatus();
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
        var cancelBtn = document.getElementById("btn-cancel-sync");
        if (cancelBtn) cancelBtn.classList.add("hidden");
        var progressDiv = document.getElementById("immich-progress");
        if (progressDiv) progressDiv.classList.add("hidden");
    }

    function _toggleImmichInterval(enabled) {
        var el = document.getElementById("immich-interval-group");
        if (el) {
            el.style.display = enabled ? "" : "none";
        }
    }

    function _populateAlbumPicker(albums, filter) {
        var select = document.getElementById("album-picker");
        if (!select) return;
        var q = (filter || "").toLowerCase().trim();
        var html = "";
        var count = 0;
        albums.forEach(function (album) {
            if (q && (album.name || "").toLowerCase().indexOf(q) === -1) return;
            html += '<option value="' + escapeHtml(album.id) + '">'
                + escapeHtml(album.name) + ' (' + album.assetCount + ' assets)</option>';
            count++;
        });
        if (count === 0) {
            html = '<option value="" disabled>' + (q ? "No matching albums" : "— No albums found —") + '</option>';
        }
        select.innerHTML = html;
        select.disabled = count === 0;

        var countEl = document.getElementById("album-picker-count");
        if (countEl) countEl.textContent = count + " album(s)";

        var addBtn = document.getElementById("btn-add-album");
        if (addBtn) addBtn.disabled = count === 0;
    }

    /** Render the "Synced Albums" list with a Remove button per album. */

    function _renderSyncedAlbums(albums) {
        var list = document.getElementById("synced-albums-list");
        if (!list) return;
        albums = albums || [];
        if (albums.length === 0) {
            list.innerHTML = '<li style="font-size:0.78rem;color:var(--text-muted)">No albums synced yet — add one above.</li>';
            return;
        }
        var html = "";
        albums.forEach(function (album) {
            var id = escapeHtml(album.id || "");
            var name = escapeHtml(album.name || "Untitled");
            html += '<li class="synced-album" data-id="' + id + '" style="display:flex;align-items:center;gap:0.5rem;padding:0.35rem 0.5rem;border:1px solid var(--border);border-radius:6px;margin-bottom:0.3rem">'
                + '<span class="material-symbols-outlined" style="font-size:1.1rem;color:var(--text-muted);flex-shrink:0">photo_library</span>'
                + '<div style="flex:1;min-width:0">'
                + '<div style="font-size:0.82rem">' + name + '</div>'
                + '<div style="font-size:0.68rem;color:var(--text-muted);word-break:break-all">album_' + id + '</div>'
                + '</div>'
                + '<button type="button" class="btn-remove-album btn--sm btn--danger" data-id="' + id + '" data-name="' + name + '" title="Remove album and delete its local files" style="margin:0;flex-shrink:0">Remove</button>'
                + '</li>';
        });
        list.innerHTML = html;

        // Bind remove handlers
        list.querySelectorAll(".btn-remove-album").forEach(function (btn) {
            btn.addEventListener("click", async function () {
                var albumId = btn.getAttribute("data-id");
                var albumName = btn.getAttribute("data-name");
                if (!(await confirmDialog('Remove "' + albumName + '" from sync and delete its local folder (album_' + albumId + ')?', { danger: true, okText: "Remove" }))) return;
                var result = await apiPost("/immich/albums/remove", { id: albumId });
                if (result && result.status === "ok") {
                    showToast("Removed " + albumName, "success");
                    _loadSyncedAlbums();
                    if (typeof loadMedia === "function") loadMedia();
                } else {
                    showToast("Failed to remove album", "error");
                }
            });
        });
    }

    /** Load the configured albums from config and render the synced list. */

    async function _loadSyncedAlbums() {
        var config = await apiGet("/config");
        if (!config) return;
        var albums = (config.sync && config.sync.immich && config.sync.immich.albums) || [];
        _renderSyncedAlbums(albums);
    }

    async function loadSync() {
        const config = await apiGet("/config");
        if (!config) return;

        const imm = config.sync?.immich || {};
        setChecked("cfg-immich-enabled", imm.enabled || false);
        setValue("cfg-immich-url", imm.server_url || "");
        setValue("cfg-immich-key", imm.api_key || "");
        setValue("cfg-immich-sync-dir", imm.sync_dir || "media/sync/immich/");
        setValue("cfg-immich-interval", ((imm.poll_interval_seconds || 3600) / 3600).toFixed(1));
        setChecked("cfg-immich-strict", imm.strict_sync === true);
        _toggleImmichInterval(imm.enabled || false);

        // Render the configured (synced) albums list
        await _loadSyncedAlbums();

        // Refresh sync status
        await refreshSyncStatus();

        // If a sync is already running, start live polling
        if (_syncPollTimer === null || _syncPollTimer === undefined) {
            var statusData = await apiGet("/immich/status");
            if (statusData && statusData.progress && statusData.progress.syncing) {
                startSyncPolling();
            }
        }

        if (!_syncBound) {
            _syncBound = true;

            // Toggle poll interval visibility
            document.getElementById("cfg-immich-enabled")?.addEventListener("change", function () {
                _toggleImmichInterval(this.checked);
            });

            // -- Test Connection --
            document.getElementById("btn-test-immich")?.addEventListener("click", async () => {
                var resultEl = document.getElementById("immich-test-result");
                if (resultEl) resultEl.textContent = "Testing…";
                var data = await apiPost("/immich/test-connection", {
                    server_url: document.getElementById("cfg-immich-url").value,
                    api_key: document.getElementById("cfg-immich-key").value,
                });
                if (!data) {
                    if (resultEl) { resultEl.textContent = "Request failed"; resultEl.style.color = "var(--danger)"; }
                    return;
                }
                if (data.ok) {
                    if (resultEl) { resultEl.textContent = data.message; resultEl.style.color = "var(--text)"; }
                    showToast("Connection successful!", "success");
                    // Save server URL and API key so Fetch Albums works
                    // immediately without a separate save step.
                    await apiPut("/config/sync", {
                        immich: {
                            server_url: document.getElementById("cfg-immich-url").value,
                            api_key: document.getElementById("cfg-immich-key").value,
                        },
                    });
                } else {
                    if (resultEl) { resultEl.textContent = (data.error || "Unknown error"); resultEl.style.color = "var(--danger)"; }
                    showToast("Connection failed: " + data.error, "error", 5000);
                }
            });

            // -- Fetch Albums --
            document.getElementById("btn-fetch-albums")?.addEventListener("click", async () => {
                var picker = document.getElementById("album-picker");
                if (!picker) return;
                picker.disabled = true;
                picker.innerHTML = '<option value="">Loading…</option>';

                var data = await apiGet("/immich/albums");
                if (!data || data.error) {
                    picker.innerHTML = '<option value="">— Failed to load —</option>';
                    picker.disabled = false;
                    showToast("Failed to fetch albums: " + ((data && data.error) || "Network error"), "error", 5000);
                    return;
                }

                _albumData = data;
                _populateAlbumPicker(data);
                showToast("Loaded " + data.length + " album(s)", "info");
            });

            // Album search filter
            document.getElementById("album-search")?.addEventListener("input", function () {
                if (_albumData) _populateAlbumPicker(_albumData, this.value);
            });

            // -- Add selected album --
            document.getElementById("btn-add-album")?.addEventListener("click", async () => {
                var picker = document.getElementById("album-picker");
                if (!picker || !picker.value) return;
                var album = null;
                for (var i = 0; i < _albumData.length; i++) {
                    if (_albumData[i].id === picker.value) { album = _albumData[i]; break; }
                }
                if (!album) return;
                var result = await apiPost("/immich/albums/add", { id: album.id, name: album.name });
                if (result && result.status === "ok") {
                    showToast("Added \"" + album.name + "\" to sync", "success");
                    await _loadSyncedAlbums();
                } else {
                    showToast("Failed to add album", "error");
                }
            });

            // -- Save Immich Settings --
            document.getElementById("btn-save-immich")?.addEventListener("click", async () => {
                var result = await apiPut("/config/sync", {
                    immich: {
                        enabled: document.getElementById("cfg-immich-enabled").checked,
                        server_url: document.getElementById("cfg-immich-url").value,
                        api_key: document.getElementById("cfg-immich-key").value,
                        sync_dir: document.getElementById("cfg-immich-sync-dir").value,
                        strict_sync: document.getElementById("cfg-immich-strict").checked,
                        poll_interval_seconds: Math.round(parseFloat(document.getElementById("cfg-immich-interval").value) * 3600) || 3600,
                    },
                });
                if (result) {
                    showToast("Immich settings saved!", "success");
                } else {
                    showToast("Failed to save Immich settings", "error");
                }
            });

            // -- Sync Now --
            document.getElementById("btn-sync-now")?.addEventListener("click", async () => {
                _syncRestoreBtn = setButtonBusy(document.getElementById("btn-sync-now"), "Syncing…");

                var result = await apiPost("/immich/sync", {});
                if (result && result.status === "started") {
                    showToast("Sync started — check status below", "info");
                    _syncWasActive = true;  // Set immediately — sync may finish before first poll
                    startSyncPolling();
                } else {
                    showToast("Failed to start sync", "error");
                    if (_syncRestoreBtn) { _syncRestoreBtn(); _syncRestoreBtn = null; }
                }
            });

            // -- Cancel Sync --
            document.getElementById("btn-cancel-sync")?.addEventListener("click", async () => {
                var result = await apiPost("/immich/cancel");
                if (result && result.status === "ok") {
                    showToast("Cancelling sync…", "info");
                }
            });
        }
    }

    /** Refresh the Immich sync status display. */

    async function refreshSyncStatus() {
        var statusEl = document.getElementById("immich-sync-status");
        var textEl = document.getElementById("sync-status-text");
        var detailEl = document.getElementById("sync-status-detail");
        var errorsEl = document.getElementById("sync-errors");
        var progressDiv = document.getElementById("immich-progress");
        var cancelBtn = document.getElementById("btn-cancel-sync");
        if (!statusEl || !textEl || !detailEl) return;

        var data = await apiGet("/immich/status");
        if (!data) return;

        // ── Live progress ──────────────────────────────────────────
        var prog = data.progress;
        if (prog && prog.syncing) {
            if (progressDiv) progressDiv.classList.remove("hidden");
            if (cancelBtn) cancelBtn.classList.remove("hidden");

            var phaseLabel = prog.phase || "";
            var phaseText = {
                "starting": "Starting\u2026",
                "resolving_album": "Looking up album\u2026",
                "fetching_assets": "Fetching asset list\u2026",
                "downloading": "Downloading",
                "cleaning": "Cleaning up\u2026",
                "cancelled": "Cancelled",
                "error": "Error",
            }[phaseLabel] || phaseLabel;

            var phaseEl = document.getElementById("sync-progress-phase");
            if (phaseEl) phaseEl.textContent = phaseText;

            var countEl = document.getElementById("sync-progress-count");
            if (countEl && prog.total > 0) {
                countEl.textContent = prog.processed + " / " + prog.total;
            } else if (countEl) {
                countEl.textContent = "";
            }

            var barEl = document.getElementById("sync-progress-bar");
            if (barEl && prog.total > 0) {
                barEl.style.width = Math.round(prog.processed / prog.total * 100) + "%";
            } else if (barEl) {
                barEl.style.width = "0%";
            }

            var fileEl = document.getElementById("sync-current-file");
            if (fileEl) fileEl.textContent = prog.current_file || "";

            // Mark that we've seen an active sync — used below to
            // detect when it finishes and auto-stop polling.
            _syncWasActive = true;
        } else {
            if (progressDiv) progressDiv.classList.add("hidden");
            if (cancelBtn) cancelBtn.classList.add("hidden");

            // If a sync was running and now it's finished, stop the
            // polling interval and re-enable the Sync Now button.
            if (_syncWasActive) {
                _syncWasActive = false;
                stopSyncPolling();
                if (_syncRestoreBtn) { _syncRestoreBtn(); _syncRestoreBtn = null; }
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

        // Summary line
        var parts = [];
        if (s.total_remote > 0) parts.push(s.total_remote + " assets");
        if (s.downloaded > 0) parts.push(s.downloaded + " downloaded");
        if (s.skipped > 0) parts.push(s.skipped + " skipped");
        if (s.deleted > 0) parts.push(s.deleted + " deleted");
        if (s.duration_seconds) parts.push("took " + s.duration_seconds + "s");
        detailEl.textContent = parts.join(" · ");

        // Per-album rows
        var albumsEl = document.getElementById("sync-albums");
        if (albumsEl) {
            var perAlbum = s.albums || [];
            if (perAlbum.length > 0) {
                var ah = '<div style="font-weight:600;font-size:0.75rem;color:var(--text-muted);margin-bottom:0.2rem;text-transform:uppercase;letter-spacing:0.04em">Albums</div>';
                perAlbum.forEach(function (a) {
                    var statusTxt = a.success ? "OK" : (a.errors && a.errors.length ? a.errors[0] : "Error");
                    var color = a.success ? "var(--text)" : "#f0a030";
                    var counts = [];
                    if (a.total_remote > 0) counts.push(a.total_remote + " remote");
                    if (a.downloaded > 0) counts.push(a.downloaded + " down");
                    if (a.deleted > 0) counts.push(a.deleted + " del");
                    ah += '<div style="display:flex;align-items:center;gap:0.4rem;padding:0.25rem 0;border-bottom:1px solid var(--border)">'
                        + '<span class="material-symbols-outlined" style="font-size:0.9rem;color:var(--text-muted)">photo_library</span>'
                        + '<span style="flex:1;font-size:0.78rem">' + escapeHtml(a.album_name || "?") + '</span>'
                        + '<span style="font-size:0.7rem;color:var(--text-muted)">' + counts.join(" · ") + '</span>'
                        + '<span style="font-size:0.72rem;color:' + color + '">' + escapeHtml(statusTxt) + '</span>'
                        + '</div>';
                });
                albumsEl.innerHTML = ah;
            } else {
                albumsEl.innerHTML = "";
            }
        }

        // Error list (excluding Cancelled which is shown in the status line)
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

export { loadSync };
