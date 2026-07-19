// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Metixel Photoframe Dashboard — Vanilla JS SPA
 *
 * Lightweight single-page application for the web dashboard.
 * No frameworks — targets under 200KB total bundle size.
 */

(function () {
    "use strict";

    // -- Toast Notifications ---------------------------------------------------

    /**
     * Show a toast notification in the top-right corner.
     * Auto-dismisses after the given duration (default 3 seconds).
     *
     * @param {string} message - The message text.
     * @param {'success'|'error'|'info'} type - Toast style variant.
     * @param {number} durationMs - How long the toast stays visible.
     */
    function showToast(message, type, durationMs) {
        type = type || "success";
        durationMs = durationMs || 3000;

        // Ensure the container exists
        var container = document.getElementById("toast-container");
        if (!container) {
            container = document.createElement("div");
            container.id = "toast-container";
            container.className = "toast-container";
            document.body.appendChild(container);
        }

        var toast = document.createElement("div");
        toast.className = "toast toast--" + type;
        toast.textContent = message;

        container.appendChild(toast);

        // Remove after animation completes
        setTimeout(function () {
            if (toast.parentNode) {
                toast.parentNode.removeChild(toast);
            }
        }, durationMs);
    }

    // -- Page Navigation ----------------------------------------------------

    document.querySelectorAll("nav a[data-page]").forEach((link) => {
        link.addEventListener("click", (e) => {
            e.preventDefault();
            const page = link.dataset.page;
            navigateTo(page);
        });
    });

    function navigateTo(page) {
        // Update nav active state
        document.querySelectorAll("nav a").forEach((a) => a.classList.remove("active"));
        document.querySelector(`nav a[data-page="${page}"]`)?.classList.add("active");

        // Show the selected page
        document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
        const target = document.getElementById(`page-${page}`);
        if (target) target.classList.add("active");

        // Load page data
        if (page === "dashboard") loadDashboard();
        else if (page === "settings") loadSettings();
        else if (page === "sync") loadSync();
        else if (page === "media") loadMedia();
        else if (page === "advanced") loadAdvanced();
    }

    /** Refresh the dashboard health + current media without re-binding controls. */
    async function refreshDashboard() {
        const health = await apiGet("/config/health");
        if (!health) return;

        var uptimeH = Math.floor(health.uptime_seconds / 3600);
        var uptimeM = Math.floor((health.uptime_seconds % 3600) / 60);

        // Only update system-health if the element still exists
        var shEl = document.getElementById("system-health");
        if (shEl) {
            shEl.innerHTML =
                '<div class="stat-item"><div class="stat-label">Uptime</div><div class="stat-value">' + uptimeH + 'h ' + uptimeM + 'm</div></div>' +
                '<div class="stat-item"><div class="stat-label">Disk Used</div><div class="stat-value">' + health.disk_used_gb + ' / ' + health.disk_total_gb + ' GB</div></div>' +
                '<div class="stat-item"><div class="stat-label">Disk Free</div><div class="stat-value">' + health.disk_free_gb + ' GB</div></div>' +
                '<div class="stat-item"><div class="stat-label">Usage</div><div class="stat-value">' + health.disk_used_percent + '%</div></div>';
        }

        // Current media — use a stable DOM so the card doesn't jump on each poll
        var cm = health.current_media;
        var cmEl = document.getElementById("current-media");
        if (cmEl) {
            // Ensure stable structure exists (only built once)
            if (!cmEl.dataset.stable) {
                cmEl.innerHTML =
                    '<div class="current-media-preview" id="cm-preview">'
                    + '<img id="cm-thumb" src="" alt="Now playing" style="display:none" />'
                    + '</div>'
                    + '<p id="cm-file" style="font-size:0.95rem;min-height:1.4em"></p>'
                    + '<p id="cm-status" style="font-size:0.8rem;color:var(--text-muted);min-height:1.2em"></p>';
                cmEl.dataset.stable = "1";
            }

            var thumbEl = document.getElementById("cm-thumb");
            var fileEl = document.getElementById("cm-file");
            var statusEl = document.getElementById("cm-status");

            if (cm && cm.file) {
                var mediaLabel = (cm.media_type === "video") ? "Video" : "Image";
                var pausedBadge = cm.paused ? ' <span style="color:#f0a030;font-size:0.75rem">(paused)</span>' : '';

                // Update thumbnail src — only change if different (avoids img flicker)
                // Use a data attribute to track the last-set URL since img.src
                // returns a fully-resolved absolute URL.
                var newSrc = cm.thumbnail_url || "";
                var lastSrc = thumbEl.getAttribute("data-src") || "";
                if (lastSrc !== newSrc) {
                    thumbEl.setAttribute("data-src", newSrc);
                    if (newSrc) {
                        thumbEl.src = newSrc;
                        thumbEl.style.display = "block";
                        thumbEl.onerror = function () { this.style.display = "none"; };
                    } else {
                        thumbEl.removeAttribute("src");
                        thumbEl.style.display = "none";
                    }
                }

                // Always keep the preview container visible — it has a fixed
                // aspect-ratio that reserves space and prevents layout shift.
                // The ::before placeholder icon shows when the img is hidden.

                fileEl.innerHTML = '<strong>' + escapeHtml(cm.file) + '</strong>' + pausedBadge;
                statusEl.textContent = mediaLabel + ' ' + (cm.index + 1) + ' of ' + cm.total;
            } else {
                // No media — hide the image but keep the preview container
                // so the card height stays stable
                if (thumbEl) { thumbEl.removeAttribute("src"); thumbEl.style.display = "none"; }
                fileEl.innerHTML = '<span style="color:var(--text-muted)">No media playing</span>';
                statusEl.textContent = "";
            }
        }

        // Sync pause button with actual state
        var pauseBtn = document.getElementById("btn-pause-toggle");
        if (pauseBtn) {
            var isPaused = cm && cm.paused;
            if (isPaused && pauseBtn.innerHTML.indexOf("Resume") === -1) {
                pauseBtn.innerHTML = "▶ Resume";
            } else if (!isPaused && pauseBtn.innerHTML.indexOf("Pause") !== -1) {
                pauseBtn.innerHTML = "⏸ Pause";
            }
        }

        var uptimeEl = document.getElementById("uptime");
        if (uptimeEl) {
            uptimeEl.textContent = "Uptime " + uptimeH + "h " + uptimeM + "m";
        }
    }

    // -- UI Helpers ---------------------------------------------------------

    /**
     * Enable or disable the resolution override fields based on auto-detect.
     * @param {boolean} isAuto - Whether auto-detect is enabled.
     */
    function toggleResolutionFields(isAuto) {
        var fields = document.getElementById("display-resolution-fields");
        if (fields) {
            var inputs = fields.querySelectorAll("input");
            inputs.forEach(function (input) {
                input.disabled = isAuto;
            });
            if (isAuto) {
                fields.classList.add("is-disabled");
            } else {
                fields.classList.remove("is-disabled");
            }
        }
    }

    /**
     * Parse an input value as an integer, returning a safe fallback.
     * Prevents NaN from leaking into JSON payloads (NaN → null in JSON).
     * @param {string|number} val
     * @param {number} fallback
     * @returns {number}
     */
    function sanitizeInt(val, fallback) {
        var n = parseInt(val, 10);
        return isNaN(n) ? (fallback || 0) : n;
    }

    /**
     * Set a checkbox's checked state, silently ignoring missing elements.
     * @param {string} id - Element ID.
     * @param {boolean} state
     */
    function setChecked(id, state) {
        var el = document.getElementById(id);
        if (el) el.checked = state;
    }

    /**
     * Set an input's value, silently ignoring missing elements.
     * @param {string} id - Element ID.
     * @param {*} value
     */
    function setValue(id, value) {
        var el = document.getElementById(id);
        if (el) el.value = value;
    }

    // -- API Helper ---------------------------------------------------------

    async function apiGet(path) {
        try {
            const res = await fetch(`/api${path}`);
            if (!res.ok) {
                console.error("API GET %s failed: %s %s", path, res.status, res.statusText);
                return null;
            }
            return await res.json();
        } catch (err) {
            console.error("API error:", err);
            return null;
        }
    }

    async function apiPut(path, data) {
        try {
            const res = await fetch(`/api${path}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(data),
            });
            if (!res.ok) {
                console.error("API PUT %s failed: %s %s", path, res.status, res.statusText);
                return null;
            }
            return await res.json();
        } catch (err) {
            console.error("API error:", err);
            return null;
        }
    }

    async function apiPost(path, data) {
        try {
            const res = await fetch(`/api${path}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: data ? JSON.stringify(data) : undefined,
            });
            return await res.json();
        } catch (err) {
            console.error("API error:", err);
            return null;
        }
    }

    // -- Dashboard ----------------------------------------------------------

    var _dashboardBound = false;
    var _dashboardTimer = null;

    async function loadDashboard() {
        // Clear any existing polling timer
        if (_dashboardTimer) {
            clearInterval(_dashboardTimer);
            _dashboardTimer = null;
        }

        // Initial load
        await refreshDashboard();
        await refreshLogs();

        // Poll every 3 seconds for live updates
        _dashboardTimer = setInterval(async function () {
            // Only refresh if dashboard page is still active
            if (document.getElementById("page-dashboard").classList.contains("active")) {
                await refreshDashboard();
                await refreshLogs();
            } else if (_dashboardTimer) {
                clearInterval(_dashboardTimer);
                _dashboardTimer = null;
            }
        }, 3000);

        // Quick controls — bind once
        if (!_dashboardBound) {
            _dashboardBound = true;
            document.getElementById("btn-next")?.addEventListener("click", async () => {
                await apiPost("/config/control", { cmd: "next" });
                // Next implicitly resumes — sync the button
                var pauseBtn = document.getElementById("btn-pause-toggle");
                if (pauseBtn) pauseBtn.innerHTML = "⏸ Pause";
                showToast("Skipped to next", "info");
            });
            document.getElementById("btn-prev")?.addEventListener("click", async () => {
                await apiPost("/config/control", { cmd: "prev" });
                // Prev implicitly resumes — sync the button
                var pauseBtn = document.getElementById("btn-pause-toggle");
                if (pauseBtn) pauseBtn.innerHTML = "⏸ Pause";
                showToast("Went to previous", "info");
            });
            var pauseBtn = document.getElementById("btn-pause-toggle");
            pauseBtn?.addEventListener("click", async () => {
                // Read actual state from the health data (updated every poll)
                // rather than trusting the button text.
                var health = await apiGet("/config/health");
                var isPaused = health && health.current_media && health.current_media.paused;
                if (isPaused) {
                    await apiPost("/config/control", { cmd: "resume" });
                    pauseBtn.innerHTML = "⏸ Pause";
                    showToast("Slideshow resumed", "info");
                } else {
                    await apiPost("/config/control", { cmd: "pause" });
                    pauseBtn.innerHTML = "▶ Resume";
                    showToast("Slideshow paused", "info");
                }
            });

            // Log wordwrap toggle — must update BOTH the list AND its <li>
            // children because the CSS rule `.log-list li { white-space: nowrap }`
            // would otherwise override the parent.
            document.getElementById("log-wordwrap")?.addEventListener("change", function () {
                var wrap = this.checked;
                var logEl = document.querySelector(".log-list");
                if (logEl) {
                    logEl.classList.toggle("wordwrap-off", !wrap);
                }
            });

            // Log severity filter checkboxes
            document.querySelectorAll(".log-filter").forEach(function (cb) {
                cb.addEventListener("change", function () {
                    _applyLogFilter();
                    _updateLogPagination();
                });
            });
        }
    }

    // -- Settings -----------------------------------------------------------

    var _settingsBound = false;
    var _syncBound = false;

    async function loadSettings() {
        const config = await apiGet("/config");
        if (!config) return;

        // Slideshow
        const s = config.slideshow || {};
        setValue("cfg-duration", s.image_duration_seconds || 30);
        setValue("cfg-transition-duration", s.transition_duration_ms || 1500);
        var ttLabel = document.getElementById("cfg-transition-duration-label");
        if (ttLabel) ttLabel.textContent = (s.transition_duration_ms || 1500) + " ms";
        setValue("cfg-transition", s.transition_style || "crossfade");
        setValue("cfg-fit", s.fit_mode || "contain");
        setChecked("cfg-smart-cover", s.smart_cover !== false);
        var mc = s.matte_color || [0, 0, 0];
        setValue("cfg-matte-r", mc[0] || 0);
        setValue("cfg-matte-g", mc[1] || 0);
        setValue("cfg-matte-b", mc[2] || 0);
        setChecked("cfg-shuffle", s.shuffle !== false);
        setChecked("cfg-video-enabled", s.video_playback_enabled === true);
        setValue("cfg-video-max-duration", s.video_max_duration_seconds || 0);

        // Display
        const d = config.display || {};
        const isAuto = (d.width === 0 && d.height === 0);
        setChecked("cfg-display-auto", isAuto);
        setValue("cfg-display-width", d.width || 0);
        setValue("cfg-display-height", d.height || 0);
        setValue("cfg-fps-limit", d.fps_limit || 30);
        setChecked("cfg-fullscreen", d.fullscreen !== false);
        setChecked("cfg-hide-cursor", d.hide_cursor !== false);
        toggleResolutionFields(isAuto);

        // Event listeners — bind once
        if (!_settingsBound) {
            _settingsBound = true;

            document.getElementById("cfg-transition-duration")?.addEventListener("input", function () {
                document.getElementById("cfg-transition-duration-label").textContent = this.value + " ms";
            });
            document.getElementById("cfg-display-auto")?.addEventListener("change", function () {
                toggleResolutionFields(this.checked);
            });

            document.getElementById("btn-save-slideshow")?.addEventListener("click", async () => {
                var result = await apiPut("/config/slideshow", {
                    image_duration_seconds: sanitizeInt(document.getElementById("cfg-duration").value, 30),
                    transition_duration_ms: sanitizeInt(document.getElementById("cfg-transition-duration").value, 1500),
                    transition_style: document.getElementById("cfg-transition").value,
                    fit_mode: document.getElementById("cfg-fit").value,
                    smart_cover: document.getElementById("cfg-smart-cover").checked,
                    matte_color: [
                        sanitizeInt(document.getElementById("cfg-matte-r").value, 0),
                        sanitizeInt(document.getElementById("cfg-matte-g").value, 0),
                        sanitizeInt(document.getElementById("cfg-matte-b").value, 0),
                    ],
                    shuffle: document.getElementById("cfg-shuffle").checked,
                    video_playback_enabled: document.getElementById("cfg-video-enabled").checked,
                    video_max_duration_seconds: sanitizeInt(document.getElementById("cfg-video-max-duration")?.value, 120),
                });
                if (result) {
                    showToast("Slideshow settings saved!", "success");
                } else {
                    showToast("Failed to save slideshow settings — check server logs", "error");
                }
            });

            document.getElementById("btn-save-display")?.addEventListener("click", async () => {
                const isAutoSave = document.getElementById("cfg-display-auto").checked;
                var result = await apiPut("/config/display", {
                    width: isAutoSave ? 0 : sanitizeInt(document.getElementById("cfg-display-width").value, 0),
                    height: isAutoSave ? 0 : sanitizeInt(document.getElementById("cfg-display-height").value, 0),
                    fps_limit: sanitizeInt(document.getElementById("cfg-fps-limit").value, 30),
                    hide_cursor: document.getElementById("cfg-hide-cursor").checked,
                });
                if (result) {
                    showToast("Display settings saved!", "success");
                } else {
                    showToast("Failed to save display settings", "error");
                }
            });

            // Display power toggle
            var powerBtn = document.getElementById("btn-display-power");
            var _displayOn = true;
            powerBtn?.addEventListener("click", async () => {
                _displayOn = !_displayOn;
                await apiPost("/config/control", { cmd: _displayOn ? "power_on" : "power_off" });
                powerBtn.innerHTML = _displayOn ? "⏻ Turn Display Off" : "⏻ Turn Display On";
                powerBtn.classList.toggle("btn--danger", !_displayOn);
                showToast(_displayOn ? "Display turned on" : "Display turned off", "info");
            });
        }
        const imm = config.sync?.immich || {};
        setChecked("cfg-immich-enabled", imm.enabled || false);
        setValue("cfg-immich-url", imm.server_url || "");
        setValue("cfg-immich-key", imm.api_key || "");
        setValue("cfg-immich-interval", imm.poll_interval_seconds || 300);

        // Local
        const local = config.sync?.local || {};
        setChecked("cfg-local-enabled", local.enabled !== false);
        setValue("cfg-local-paths", (local.watch_paths || ["media/"]).join(", "));
        setValue("cfg-local-interval", local.poll_interval_seconds || 30);

        if (!_syncBound) {
            _syncBound = true;

            document.getElementById("btn-save-immich")?.addEventListener("click", async () => {
                var result = await apiPut("/config/sync", {
                    immich: {
                        enabled: document.getElementById("cfg-immich-enabled").checked,
                        server_url: document.getElementById("cfg-immich-url").value,
                        api_key: document.getElementById("cfg-immich-key").value,
                        poll_interval_seconds: sanitizeInt(document.getElementById("cfg-immich-interval").value, 300),
                    },
                });
                if (result) {
                    showToast("Immich settings saved!", "success");
                } else {
                    showToast("Failed to save Immich settings", "error");
                }
            });

            document.getElementById("btn-save-local-sync")?.addEventListener("click", async () => {
                var result = await apiPut("/config/sync", {
                    local: {
                        enabled: document.getElementById("cfg-local-enabled").checked,
                        watch_paths: document.getElementById("cfg-local-paths").value.split(",").map(function (s) { return s.trim(); }),
                        poll_interval_seconds: sanitizeInt(document.getElementById("cfg-local-interval").value, 30),
                    },
                });
                if (result) {
                    showToast("Local sync settings saved!", "success");
                } else {
                    showToast("Failed to save local sync settings", "error");
                }
            });
        }
    }

    // -- Media --------------------------------------------------------------

    var _mediaOffset = 0;
    var _mediaLimit = 50;
    var _mediaHasMore = false;
    var _mediaLoading = false;

    async function loadMedia() {
        _mediaOffset = 0;
        _mediaHasMore = false;
        _mediaLoading = false;

        var el = document.getElementById("media-list");
        el.innerHTML = '<p style="color:var(--text-muted)">Loading…</p>';

        await _fetchMediaPage(0);
    }

    async function _fetchMediaPage(offset) {
        if (_mediaLoading) return;
        _mediaLoading = true;

        var el = document.getElementById("media-list");
        var data = await apiGet("/media/list?offset=" + offset + "&limit=" + _mediaLimit);

        if (!data || !data.items || data.items.length === 0) {
            if (offset === 0) {
                el.innerHTML = '<p style="color:var(--text-muted)">No media found. Add images or videos to the media folder.</p>';
            }
            _mediaLoading = false;
            return;
        }

        _mediaOffset = data.offset + data.items.length;
        _mediaHasMore = data.has_more;

        // Build summary on first page
        var html = '';
        if (offset === 0) {
            var summaryParts = [];
            if (data.images) summaryParts.push(data.images + " images");
            if (data.videos) summaryParts.push(data.videos + " videos");
            html += '<p style="margin-bottom:0.5rem;font-size:0.85rem;color:var(--text-muted)">'
                + (summaryParts.length ? summaryParts.join(", ") : data.total + " files") + '</p>'
                + '<div class="media-grid" id="media-grid"></div>';
            el.innerHTML = html;
        }

        var grid = document.getElementById("media-grid");
        if (!grid) {
            // Rebuild if grid element missing (shouldn't happen)
            el.innerHTML = '<div class="media-grid" id="media-grid"></div>';
            grid = document.getElementById("media-grid");
        }

        // Render items in small batches to avoid blocking the main thread
        _renderMediaBatch(grid, data.items, 0);

        // Show "Load more" button
        _updateLoadMoreButton(el);
        _mediaLoading = false;
    }

    function _renderMediaBatch(grid, items, startIdx) {
        var batchSize = 10;
        var end = Math.min(startIdx + batchSize, items.length);

        for (var i = startIdx; i < end; i++) {
            var item = items[i];
            var isVideo = item.media_type === "video";
            var thumbHtml = '';
            if (item.thumbnail_url) {
                thumbHtml = '<div class="media-thumb">'
                    + '<img src="' + escapeHtml(item.thumbnail_url) + '" alt="' + escapeHtml(item.name) + '" loading="lazy"'
                    + ' onerror="this.parentElement.style.display=\'none\'" />'
                    + '</div>';
            }
            var typeBadge = isVideo
                ? ' <span class="media-badge media-badge--video">Video</span>'
                : '';
            var infoText;
            if (isVideo) {
                infoText = (item.width && item.height)
                    ? item.width + '\u00d7' + item.height + ' \u00b7 ' + item.size_kb + ' KB'
                    : item.size_kb + ' KB';
            } else {
                infoText = item.width + '\u00d7' + item.height + ' \u00b7 ' + item.size_kb + ' KB';
            }
            var div = document.createElement("div");
            div.className = "media-item";
            div.innerHTML = thumbHtml
                + '<div class="media-name">' + escapeHtml(item.name) + typeBadge + '</div>'
                + '<div class="media-info">' + infoText + '</div>';
            grid.appendChild(div);
        }

        // Schedule next batch if there are more items
        if (end < items.length) {
            requestAnimationFrame(function () {
                _renderMediaBatch(grid, items, end);
            });
        }
    }

    function _updateLoadMoreButton(el) {
        // Remove existing button
        var existing = document.getElementById("media-load-more");
        if (existing) existing.remove();

        if (_mediaHasMore) {
            var btn = document.createElement("button");
            btn.id = "media-load-more";
            btn.textContent = "Load more\u2026";
            btn.className = "btn--secondary";
            btn.style.marginTop = "1rem";
            btn.addEventListener("click", function () {
                btn.textContent = "Loading\u2026";
                btn.disabled = true;
                _fetchMediaPage(_mediaOffset);
            });
            el.appendChild(btn);
        }
    }

    // -- Logs ----------------------------------------------------------------

    /** Refresh logs (alias for polling). */
    // -- Log Viewer (List.js) -----------------------------------------------

    /** List.js instance — created once when the dashboard first loads. */
    var _logList = null;
    /** How many entries per page. */
    var _logPageSize = 100;
    /** Track which page we're on. */
    var _logCurrentPage = 1;
    /** Guard against recursive "updated" → filter → "updated" cycles. */
    var _logFiltering = false;

    function _initLogList() {
        if (_logList) return;
        if (typeof List === "undefined") {
            console.warn("List.js not loaded (CDN unreachable?) — log viewer search disabled");
            return;
        }
        // We use List.js only for DOM re-indexing and the items/matchingItems
        // arrays.  Search and severity filtering are handled by our own
        // _applyLogFilter() so they never fight across refreshes.
        _logList = new List("log-viewer", {
            valueNames: ["log-ts", "log-level", "log-logger", "log-msg"],
            listClass: "log-list",
            item: '<li><span class="log-ts"></span> <span class="log-level"></span> <span class="log-logger"></span> <span class="log-msg"></span></li>',
        });

        // Update pagination whenever the list changes (filter)
        _logList.on("updated", function () {
            _updateLogPagination();
        });

        // ── Search input: trigger our unified filter on every keystroke ──
        var searchInput = document.getElementById("log-search");
        if (searchInput) {
            searchInput.addEventListener("input", function () {
                _applyLogFilter();
            });
        }

        // Wire custom pagination buttons
        document.querySelector(".log-prev-page")?.addEventListener("click", function () {
            if (_logCurrentPage > 1) {
                _logCurrentPage--;
                _renderLogPage();
            }
        });
        document.querySelector(".log-next-page")?.addEventListener("click", function () {
            var totalPages = Math.ceil(_logList.matchingItems.length / _logPageSize) || 1;
            if (_logCurrentPage < totalPages) {
                _logCurrentPage++;
                _renderLogPage();
            }
        });
    }

    function _renderLogPage() {
        if (!_logList) return;
        var matching = _logList.matchingItems;
        var total = matching.length;
        var totalPages = Math.ceil(total / _logPageSize) || 1;
        var start = (_logCurrentPage - 1) * _logPageSize;
        var end = Math.min(start + _logPageSize, total);

        // Hide all items first, then show only the current page slice
        for (var i = 0; i < _logList.items.length; i++) {
            _logList.items[i].elm.style.display = "none";
        }
        for (var j = start; j < end; j++) {
            matching[j].elm.style.display = "";
        }

        // Update page info
        var infoEl = document.querySelector(".log-page-info");
        if (infoEl) infoEl.textContent = "Page " + _logCurrentPage + " of " + totalPages;

        var prevBtn = document.querySelector(".log-prev-page");
        var nextBtn = document.querySelector(".log-next-page");
        if (prevBtn) prevBtn.disabled = _logCurrentPage <= 1;
        if (nextBtn) nextBtn.disabled = _logCurrentPage >= totalPages;

        // Update count
        var countEl = document.querySelector(".log-count");
        if (countEl) countEl.textContent = total + " entries";
    }

    function _updateLogPagination() {
        if (!_logList) return;
        _logCurrentPage = 1;
        _renderLogPage();
    }

    async function refreshLogs() {
        await loadLogs();
    }

    async function loadLogs() {
        var data = await apiGet("/logs/recent?count=500");
        var el = document.getElementById("log-output");
        if (!el) return;

        _initLogList();

        if (!data || !data.logs || data.logs.length === 0) {
            el.innerHTML = '<li class="log-empty">No log entries yet.</li>';
            if (_logList) _logList.reIndex();
            var countEl = document.querySelector(".log-count");
            if (countEl) countEl.textContent = "0 entries";
            _updateLogPagination();
            return;
        }

        // ── Fallback rendering when List.js isn't available ──────────
        if (!_logList) {
            var fbHtml = "";
            data.logs.forEach(function (entry) {
                var ts, level, loggerName, msg;
                if (typeof entry === "object" && entry.message !== undefined) {
                    ts = entry.timestamp || "";
                    level = entry.level || "INFO";
                    loggerName = entry.logger || "";
                    msg = entry.message;
                } else {
                    ts = "";
                    level = "INFO";
                    loggerName = "";
                    msg = String(entry);
                }
                fbHtml += '<li class="log-' + level.toLowerCase() + '">'
                    + '<span class="log-ts">' + escapeHtml(ts) + '</span> '
                    + '<span class="log-level">' + escapeHtml(level) + '</span> '
                    + '<span class="log-logger">' + escapeHtml(loggerName) + '</span> '
                    + '<span class="log-msg">' + escapeHtml(msg) + '</span>'
                    + '</li>';
            });
            el.innerHTML = fbHtml;
            var fbCount = document.querySelector(".log-count");
            if (fbCount) fbCount.textContent = data.logs.length + " entries (search unavailable)";
            _scrollToBottomIfFollowing();
            return;
        }

        // ── List.js path: build <li> elements, append to DOM, re-index ─
        // Track existing log IDs to avoid duplicating entries.
        var existingIds = {};
        var children = el.children;
        for (var c = 0; c < children.length; c++) {
            var id = children[c].getAttribute("data-log-id");
            if (id) existingIds[id] = true;
        }

        var newCount = 0;
        data.logs.forEach(function (entry) {
            var ts, level, loggerName, msg;
            if (typeof entry === "object" && entry.message !== undefined) {
                ts = entry.timestamp || "";
                level = entry.level || "INFO";
                loggerName = entry.logger || "";
                msg = entry.message;
            } else {
                ts = "";
                level = "INFO";
                loggerName = "";
                msg = String(entry);
            }
            var logId = ts + "|" + level + "|" + loggerName + "|" + msg;
            if (existingIds[logId]) return;

            var li = document.createElement("li");
            li.setAttribute("data-log-id", logId);
            li.className = "log-" + level.toLowerCase();
            li.innerHTML = '<span class="log-ts">' + escapeHtml(ts) + '</span> '
                + '<span class="log-level">' + escapeHtml(level) + '</span> '
                + '<span class="log-logger">' + escapeHtml(loggerName) + '</span> '
                + '<span class="log-msg">' + escapeHtml(msg) + '</span>';
            el.appendChild(li);
            existingIds[logId] = true;
            newCount++;
        });

        // Trim oldest entries to stay within capacity
        while (el.children.length > 500) {
            el.removeChild(el.firstChild);
        }

        // Tell List.js to re-parse the DOM, then re-apply the unified
        // search + severity filter in one pass.
        if (newCount > 0) {
            _logList.reIndex();
        }

        // Single combined filter: searches BOTH the text input AND severity
        // checkboxes — no separate search() / filter() calls that can clash.
        _applyLogFilter();
        _scrollToBottomIfFollowing();
    }

    function _scrollToBottomIfFollowing() {
        var follow = document.getElementById("log-follow");
        if (follow && follow.checked) {
            var el = document.getElementById("log-output");
            if (el) el.scrollTop = el.scrollHeight;
        }
    }

    /** Apply the unified search + severity filter in a single pass.

        Reads the current search text from ``#log-search`` and the checked
        severity boxes.  Both constraints are evaluated inside ONE
        ``_logList.filter()`` call so they never overwrite each other —
        the root cause of search breaking on refresh. */
    function _applyLogFilter() {
        if (_logFiltering || !_logList) return;
        _logFiltering = true;

        // Gather active severity levels
        var active = {};
        document.querySelectorAll(".log-filter").forEach(function (cb) {
            active[cb.dataset.level.toUpperCase()] = cb.checked;
        });

        // Read current search term
        var searchTerm = "";
        var searchInput = document.getElementById("log-search");
        if (searchInput) {
            searchTerm = searchInput.value.trim().toLowerCase();
        }

        // Single filter: severity AND optional text search
        _logList.filter(function (item) {
            // Severity check
            var level = (item.values()["log-level"] || "").toUpperCase();
            if (active[level] !== true) return false;

            // Text search (searches across timestamp, level, logger, message)
            if (searchTerm) {
                var haystack = (
                    (item.values()["log-ts"] || "") + "\t" +
                    (item.values()["log-level"] || "") + "\t" +
                    (item.values()["log-logger"] || "") + "\t" +
                    (item.values()["log-msg"] || "")
                ).toLowerCase();
                if (haystack.indexOf(searchTerm) === -1) return false;
            }

            return true;
        });

        _logFiltering = false;
        _updateLogPagination();
    }

    /** Escape HTML special characters to prevent XSS in log output. */
    function escapeHtml(text) {
        var map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };
        return String(text).replace(/[&<>"']/g, function (m) { return map[m]; });
    }

    // -- Advanced ------------------------------------------------------------

    var _advancedBound = false;

    async function loadAdvanced() {
        const config = await apiGet("/config");
        if (!config) return;

        // MQTT
        var mqtt = config.mqtt || {};
        setChecked("cfg-mqtt-enabled", mqtt.enabled || false);
        setValue("cfg-mqtt-broker", mqtt.broker || "localhost");
        setValue("cfg-mqtt-port", mqtt.port || 1883);
        setValue("cfg-mqtt-prefix", mqtt.topic_prefix || "metixel");
        setValue("cfg-mqtt-user", mqtt.username || "");
        setValue("cfg-mqtt-pass", mqtt.password || "");

        // Input
        var inp = config.input || {};
        setChecked("cfg-cec-enabled", inp.cec_enabled !== false);
        setChecked("cfg-ir-enabled", inp.ir_enabled || false);
        setValue("cfg-ir-device", inp.ir_device || "/dev/lirc0");

        // System
        var sys = config.system || {};
        setValue("cfg-log-level", sys.log_level || "INFO");
        setValue("cfg-media-folder", sys.media_folder || "media/");
        setValue("cfg-cache-dir", sys.cache_dir || "cache/");

        // Web
        var web = config.web || {};
        setValue("cfg-web-host", web.host || "0.0.0.0");
        setValue("cfg-web-port", web.port || 8080);
        setChecked("cfg-web-debug", web.debug || false);

        if (!_advancedBound) {
            _advancedBound = true;

            document.getElementById("btn-save-mqtt")?.addEventListener("click", async () => {
                var result = await apiPut("/config/mqtt", {
                    enabled: document.getElementById("cfg-mqtt-enabled").checked,
                    broker: document.getElementById("cfg-mqtt-broker").value,
                    port: sanitizeInt(document.getElementById("cfg-mqtt-port").value, 1883),
                    topic_prefix: document.getElementById("cfg-mqtt-prefix").value,
                    username: document.getElementById("cfg-mqtt-user").value,
                    password: document.getElementById("cfg-mqtt-pass").value,
                });
                if (result) {
                    showToast("MQTT settings saved!", "success");
                } else {
                    showToast("Failed to save MQTT settings", "error");
                }
            });

            document.getElementById("btn-save-input")?.addEventListener("click", async () => {
                var result = await apiPut("/config/input", {
                    cec_enabled: document.getElementById("cfg-cec-enabled").checked,
                    ir_enabled: document.getElementById("cfg-ir-enabled").checked,
                    ir_device: document.getElementById("cfg-ir-device").value,
                });
                if (result) {
                    showToast("Input settings saved!", "success");
                } else {
                    showToast("Failed to save input settings", "error");
                }
            });

            document.getElementById("btn-save-system")?.addEventListener("click", async () => {
                var logLevel = document.getElementById("cfg-log-level").value;
                var sysResult = await apiPut("/config/system", {
                    log_level: logLevel,
                    media_folder: document.getElementById("cfg-media-folder").value,
                    cache_dir: document.getElementById("cfg-cache-dir").value,
                });
                // Also apply the file-handler log level immediately at runtime
                var logResult = await apiPost("/logs/level", { level: logLevel });
                if (sysResult && logResult && logResult.status === "ok") {
                    showToast("System settings saved! File log level: " + logLevel, "success");
                } else if (sysResult) {
                    showToast("System settings saved (log level apply failed — check server)", "info");
                } else {
                    showToast("Failed to save system settings", "error");
                }
            });

            document.getElementById("btn-save-web")?.addEventListener("click", async () => {
                var result = await apiPut("/config/web", {
                    host: document.getElementById("cfg-web-host").value,
                    port: sanitizeInt(document.getElementById("cfg-web-port").value, 8080),
                    debug: document.getElementById("cfg-web-debug").checked,
                });
                if (result) {
                    showToast("Web settings saved! Restart backend to apply.", "success");
                } else {
                    showToast("Failed to save web settings", "error");
                }
            });

            // Clear image cache
            var clearCacheBtn = document.getElementById("btn-clear-cache");
            clearCacheBtn?.addEventListener("click", async () => {
                if (!confirm("Clear all cached and thumbnail images? The next slideshow cycle will re-process source images.")) {
                    return;
                }
                clearCacheBtn.disabled = true;
                clearCacheBtn.textContent = "Clearing…";
                var result = await apiPost("/media/cache/clear");
                clearCacheBtn.disabled = false;
                clearCacheBtn.textContent = "Clear Image Cache";
                if (result && result.status === "ok") {
                    showToast(
                        "Cache cleared: " + result.deleted_files + " files, " + result.freed_mb + " MB freed",
                        "success", 5000
                    );
                } else {
                    showToast("Failed to clear image cache", "error");
                }
            });
        }
    }

    // -- Init ----------------------------------------------------------------

    loadDashboard();
})();
