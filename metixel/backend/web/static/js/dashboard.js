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
            closeDrawer();
        });
    });

    // Burger menu toggle
    var drawer = document.getElementById("nav-drawer");
    var backdrop = document.getElementById("nav-backdrop");
    document.getElementById("btn-burger")?.addEventListener("click", function () {
        openDrawer();
    });
    document.getElementById("nav-close")?.addEventListener("click", function () {
        closeDrawer();
    });
    backdrop?.addEventListener("click", function () {
        closeDrawer();
    });

    function openDrawer() {
        if (drawer) drawer.classList.add("open");
        if (backdrop) backdrop.classList.add("open");
    }

    function closeDrawer() {
        if (drawer) drawer.classList.remove("open");
        if (backdrop) backdrop.classList.remove("open");
    }

    function navigateTo(page) {
        // Persist page in URL hash so refreshes stay on the same tab
        if (location.hash.substring(1) !== page) {
            history.replaceState(null, "", "#" + page);
        }

        // Update nav drawer active state
        document.querySelectorAll(".nav-drawer nav a").forEach((a) => a.classList.remove("active"));
        var drawerLink = document.querySelector('.nav-drawer nav a[data-page="' + page + '"]');
        if (drawerLink) drawerLink.classList.add("active");

        // Show the selected page
        document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
        const target = document.getElementById(`page-${page}`);
        if (target) target.classList.add("active");

        // Load page data
        if (page === "dashboard") loadDashboard();
        else if (page === "settings") loadSettings();
        else if (page === "sync") loadSync();
        else if (page === "media") loadMedia();
        else if (page === "network") loadNetwork();
        else if (page === "advanced") loadAdvanced();
    }

    /** Refresh the dashboard health + current media without re-binding controls. */

    // -- Sparkline ring buffers (last 20 samples = 60 seconds at 3s poll) --
    var _sparkBufs = { cpu: [], mem: [], swap: [] };
    var _SPARK_MAX = 20;

    /** Draw a filled-area sparkline on a canvas element. */
    function _drawSparkline(canvasId, values, maxVal, colorHex) {
        var canvas = document.getElementById(canvasId);
        if (!canvas || values.length < 2) return;
        var ctx = canvas.getContext("2d");
        var dpr = window.devicePixelRatio || 1;
        var rect = canvas.parentNode.getBoundingClientRect();
        var w = rect.width - 20;  // padding
        var h = 56;
        // Resize canvas backing store for HiDPI
        if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
            canvas.width = Math.round(w * dpr);
            canvas.height = Math.round(h * dpr);
            canvas.style.width = w + "px";
            canvas.style.height = h + "px";
        }
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);

        var n = values.length;
        var stepX = w / (n - 1);
        var ceil = maxVal > 0 ? maxVal : 100;

        // -- Fill area --
        ctx.beginPath();
        ctx.moveTo(0, h);
        for (var i = 0; i < n; i++) {
            var v = Math.min(values[i], ceil) / ceil;
            ctx.lineTo(i * stepX, h - v * (h - 4));
        }
        ctx.lineTo((n - 1) * stepX, h);
        ctx.closePath();
        var grad = ctx.createLinearGradient(0, 0, 0, h);
        grad.addColorStop(0, colorHex + "50");
        grad.addColorStop(1, colorHex + "08");
        ctx.fillStyle = grad;
        ctx.fill();

        // -- Line --
        ctx.beginPath();
        for (var i = 0; i < n; i++) {
            var v2 = Math.min(values[i], ceil) / ceil;
            if (i === 0) ctx.moveTo(i * stepX, h - v2 * (h - 4));
            else ctx.lineTo(i * stepX, h - v2 * (h - 4));
        }
        ctx.strokeStyle = colorHex;
        ctx.lineWidth = 1.5;
        ctx.lineJoin = "round";
        ctx.stroke();
    }

    /** Update a simple text stat element by ID. No-op if element missing. */
    function _setStat(id, value) {
        var el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    async function refreshDashboard() {
        const health = await apiGet("/config/health");
        if (!health) return;

        var uptimeH = Math.floor(health.uptime_seconds / 3600);
        var uptimeM = Math.floor((health.uptime_seconds % 3600) / 60);

        var cacheMB = health.cache_size_mb != null ? health.cache_size_mb : 0;
        var cacheLabel = cacheMB >= 1024
            ? (cacheMB / 1024).toFixed(1) + " GB"
            : cacheMB.toFixed(1) + " MB";

        var mediaSizeBytes = health.media_size_bytes || 0;
        var mediaSizeLabel = mediaSizeBytes >= 1073741824
            ? (mediaSizeBytes / 1073741824).toFixed(1) + " GB"
            : (mediaSizeBytes / 1048576).toFixed(1) + " MB";

        var imgCount = health.playlist_image_count || 0;
        var vidCount = health.playlist_video_count || 0;

        // -- CPU tile --
        var cpuPct = health.cpu_percent != null ? health.cpu_percent : 0;
        _setStat("stat-cpu-val", cpuPct + "%");
        _sparkBufs.cpu.push(cpuPct);
        if (_sparkBufs.cpu.length > _SPARK_MAX) _sparkBufs.cpu.shift();
        _drawSparkline("stat-cpu-canvas", _sparkBufs.cpu, 100, "#3b82f6");

        // -- Memory tile --
        var memPct = health.memory_percent != null ? health.memory_percent : 0;
        var memUsed = health.memory_used_gb != null ? health.memory_used_gb : 0;
        var memTotal = health.memory_total_gb != null ? health.memory_total_gb : 0;
        _setStat("stat-mem-val", memPct + "%  (" + memUsed.toFixed(1) + " / " + memTotal.toFixed(1) + " GB)");
        _sparkBufs.mem.push(memPct);
        if (_sparkBufs.mem.length > _SPARK_MAX) _sparkBufs.mem.shift();
        _drawSparkline("stat-mem-canvas", _sparkBufs.mem, 100, "#8b5cf6");

        // -- Swap tile --
        var swapPct = health.swap_percent != null ? health.swap_percent : 0;
        var swapUsed = health.swap_used_gb != null ? health.swap_used_gb : 0;
        var swapTotal = health.swap_total_gb != null ? health.swap_total_gb : 0;
        _setStat("stat-swap-val", swapPct + "%  (" + swapUsed.toFixed(1) + " / " + swapTotal.toFixed(1) + " GB)");
        _sparkBufs.swap.push(swapPct);
        if (_sparkBufs.swap.length > _SPARK_MAX) _sparkBufs.swap.shift();
        _drawSparkline("stat-swap-canvas", _sparkBufs.swap, Math.max(swapTotal > 0 ? 100 : 0, 1), "#f59e0b");

        // -- Existing text-only tiles --
        _setStat("stat-uptime-val", uptimeH + "h " + uptimeM + "m");
        _setStat("stat-disk-val", health.disk_used_gb + " / " + health.disk_total_gb + " GB");
        _setStat("stat-disk-pct-val", health.disk_used_percent + "%");
        _setStat("stat-cache-val", cacheLabel);
        _setStat("stat-media-val", mediaSizeLabel);
        _setStat("stat-playlist-val", imgCount + " photos, " + vidCount + " videos");

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

    function toggleScheduleFields(enabled) {
        // Always visible — the checkbox only controls whether the scheduler runs.
        // The fields are always shown so the user can see and edit the times.
    }

    function toggleNtpFields(enabled) {
        var group = document.getElementById("ntp-servers-group");
        if (group) group.style.display = enabled ? "block" : "none";
    }

    /** @type {number|null} */
    var _clockTimer = null;

    async function _refreshServerClock() {
        var el = document.getElementById("server-clock");
        if (!el) return;
        try {
            var data = await apiGet("/config/time");
            if (data && data.time) {
                el.textContent = data.time;
                el.title = data.date + " " + data.timezone + " (UTC" + (data.utc_offset || "") + ")";
            }
        } catch (_) {
            // Clock is non-critical — silently ignore errors
        }
    }

    async function loadTimezoneList(currentTz) {
        var sel = document.getElementById("cfg-timezone");
        if (!sel) return;
        sel.innerHTML = '<option value="">Auto-detect</option>';
        try {
            var data = await apiGet("/config/timezones");
            if (data && data.timezones) {
                data.timezones.forEach(function (tz) {
                    var opt = document.createElement("option");
                    opt.value = tz;
                    opt.textContent = tz;
                    if (tz === currentTz) opt.selected = true;
                    sel.appendChild(opt);
                });
            }
        } catch (_) {}
        // If currentTz is not in the list, add it
        if (currentTz && !Array.from(sel.options).some(function (o) { return o.value === currentTz; })) {
            var opt = document.createElement("option");
            opt.value = currentTz;
            opt.textContent = currentTz + " (current)";
            opt.selected = true;
            sel.appendChild(opt);
        }
    }

    /**
     * Show or hide the transcode sub-settings based on the main toggle.
     * @param {boolean} enabled - Whether transcoding is enabled.
     */
    function _toggleTranscodeSettings(enabled) {
        var el = document.getElementById("transcode-settings");
        if (el) {
            el.style.display = enabled ? "" : "none";
            el.style.opacity = enabled ? "1" : "0.5";
        }
    }

    /**
     * Show or hide the CPU throttle percentage slider.
     * @param {boolean} enabled - Whether CPU throttling is enabled.
     */
    function _toggleCpuThrottleGroup(enabled) {
        var el = document.getElementById("cpu-throttle-group");
        if (el) {
            el.style.display = enabled ? "" : "none";
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

    // Track connection state to avoid console spam during restarts.
    var _apiConnected = true;
    var _apiErrorCount = 0;
    var _apiLastErrorTime = 0;

    function _apiUpdateConnectionStatus(ok) {
        var overlay = document.getElementById("connection-overlay");
        if (!overlay) return;
        if (ok) {
            overlay.style.display = "none";
            _apiConnected = true;
            _apiErrorCount = 0;
        } else {
            _apiConnected = false;
            _apiErrorCount++;
            overlay.style.display = "flex";
        }
    }

    async function apiGet(path) {
        try {
            const res = await fetch(`/api${path}`);
            if (!res.ok) {
                console.error("API GET %s failed: %s %s", path, res.status, res.statusText);
                _apiUpdateConnectionStatus(false);
                return null;
            }
            _apiUpdateConnectionStatus(true);
            return await res.json();
        } catch (err) {
            // Suppress console spam during restarts — only log every 30s
            var now = Date.now();
            if (now - _apiLastErrorTime > 30000) {
                console.warn("API unreachable (backend may be restarting):", err.message);
                _apiLastErrorTime = now;
            }
            _apiUpdateConnectionStatus(false);
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
                _apiUpdateConnectionStatus(false);
                return null;
            }
            _apiUpdateConnectionStatus(true);
            return await res.json();
        } catch (err) {
            var now = Date.now();
            if (now - _apiLastErrorTime > 30000) {
                console.warn("API unreachable (backend may be restarting):", err.message);
                _apiLastErrorTime = now;
            }
            _apiUpdateConnectionStatus(false);
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
    var _advancedLogTimer = null;

    async function loadDashboard() {
        // Clear any existing polling timer
        if (_dashboardTimer) {
            clearInterval(_dashboardTimer);
            _dashboardTimer = null;
        }

        // Initial load
        await refreshDashboard();
        await refreshProcessing();
        _refreshDashSyncStatus();

        // Poll every 3 seconds for live updates
        _dashboardTimer = setInterval(async function () {
            if (document.getElementById("page-dashboard").classList.contains("active")) {
                await refreshDashboard();
                await refreshProcessing();
                _refreshDashSyncStatus();
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

        // Load persistent messages (shown once per dashboard visit)
        _loadPersistentMessages();
    }

    // -- Persistent On-Screen Messages --------------------------------------

    /**
     * Load persistent messages from the API and render dismiss buttons.
     * These are duration=0 messages shown on the photo frame display
     * that stay until manually dismissed via the web UI.
     */
    async function _loadPersistentMessages() {
        var card = document.getElementById("persistent-messages-card");
        var list = document.getElementById("persistent-messages-list");
        if (!card || !list) return;

        var data = await apiGet("/messages/persistent");
        if (!data || !data.persistent) {
            card.style.display = "none";
            return;
        }

        var messages = data.persistent;
        if (messages.length === 0) {
            card.style.display = "none";
            return;
        }

        card.style.display = "";
        list.innerHTML = "";

        messages.forEach(function (msg) {
            var row = document.createElement("div");
            row.className = "persistent-msg-row";

            var info = document.createElement("div");
            info.className = "persistent-msg-info";
            var severityBadge = msg.severity || "info";
            info.innerHTML =
                '<span class="persistent-msg-severity severity--' + escapeHtml(severityBadge) + '">'
                + escapeHtml(severityBadge) + '</span> '
                + '<strong>' + escapeHtml(msg.title || "") + '</strong>'
                + (msg.body ? '<br><span style="font-size:0.82rem;color:var(--text-muted)">' + escapeHtml(msg.body) + '</span>' : "");

            var dismissBtn = document.createElement("button");
            dismissBtn.textContent = "Dismiss";
            dismissBtn.className = "btn--secondary";
            dismissBtn.style.fontSize = "0.82rem";
            dismissBtn.addEventListener("click", async function () {
                dismissBtn.disabled = true;
                dismissBtn.textContent = "Dismissing…";
                var result = await apiPost("/messages/dismiss", { id: msg.id });
                if (result && result.status === "ok") {
                    showToast("Message dismissed", "success");
                    _loadPersistentMessages(); // refresh the list
                } else {
                    dismissBtn.disabled = false;
                    dismissBtn.textContent = "Dismiss";
                    showToast("Failed to dismiss message", "error");
                }
            });

            row.appendChild(info);
            row.appendChild(dismissBtn);
            list.appendChild(row);
        });
    }

    // -- Network Page --------------------------------------------------------

    var _networkBound = false;

    async function loadNetwork() {
        if (!_networkBound) {
            _networkBound = true;

            document.getElementById("btn-network-scan")?.addEventListener("click", async function () {
                var btn = this;
                btn.disabled = true;
                btn.textContent = "Scanning…";
                await _refreshNetworkScan();
                btn.disabled = false;
                btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:1rem;vertical-align:middle">wifi_find</span> Scan for Networks';
            });

            document.getElementById("btn-network-ap-toggle")?.addEventListener("click", async function () {
                var status = await apiGet("/network/ap-status");
                if (status && status.active) {
                    await apiPost("/network/ap-stop");
                    showToast("AP mode stopped", "info");
                } else {
                    await apiPost("/network/ap-start");
                    showToast("AP mode started — SSID: Metixel-Setup", "success");
                }
                _refreshNetworkAPStatus();
            });

            // AP fallback toggle
            document.getElementById("cfg-ap-enabled")?.addEventListener("change", async function () {
                await apiPut("/config/network", { ap_fallback_enabled: this.checked });
                showToast(this.checked ? "AP fallback enabled" : "AP fallback disabled", "info");
            });

            // Suppress popups toggle
            document.getElementById("cfg-suppress-popups")?.addEventListener("change", async function () {
                await apiPut("/config/messages", { enabled: !this.checked });
                showToast(this.checked ? "Network popups suppressed" : "Network popups enabled", "info");
            });
        }

        _refreshNetworkStatus();
        _refreshNetworkAPStatus();
        _refreshNetworkScan();
        _loadNetworkConfig();
    }

    async function _loadNetworkConfig() {
        var cfg = await apiGet("/config/network");
        if (cfg) {
            setChecked("cfg-ap-enabled", cfg.ap_fallback_enabled !== false);
        }
        var msgCfg = await apiGet("/config/messages");
        if (msgCfg) {
            setChecked("cfg-suppress-popups", msgCfg.enabled === false);
        }
    }

    async function _refreshNetworkStatus() {
        var el = document.getElementById("network-status");
        if (!el) return;

        var status = await apiGet("/network/status");
        if (!status) {
            el.innerHTML = '<span style="color:var(--text-muted)">Unable to get network status</span>';
            return;
        }

        // ── WiFi radio is disabled at OS level ──────────────────────
        if (status.wifi_radio_enabled === false) {
            var wifiOffWarning =
                '<div style="margin-top:6px;padding:6px 10px;background:rgba(240,160,48,0.12);border-radius:5px;font-size:0.8rem;color:#f0a030">'
                + '⚠ WiFi is disabled at the OS level.<br>'
                + 'Enable it via <code>sudo raspi-config</code> → System Options → Wireless LAN, '
                + 'or run <code>sudo nmcli radio wifi on</code>.</div>';

            if (status.connected && status.interface_type === "ethernet") {
                // Ethernet works, but WiFi is off — note it
                el.innerHTML =
                    '<div style="display:flex;align-items:center;gap:12px">'
                    + '<span class="material-symbols-outlined" style="font-size:2rem;color:var(--text-muted)">settings_ethernet</span>'
                    + '<div>'
                    + '<strong>Connected via Ethernet</strong><br>'
                    + '<span style="font-size:0.82rem;color:var(--text-muted)">IP: ' + escapeHtml(status.ip || "—") + '</span>'
                    + '</div></div>'
                    + wifiOffWarning;
                return;
            }

            // No connection and WiFi is disabled — tell the user why
            el.innerHTML =
                '<div style="display:flex;align-items:center;gap:12px">'
                + '<span class="material-symbols-outlined" style="font-size:2rem;color:var(--danger)">wifi_off</span>'
                + '<div>'
                + '<strong>WiFi is disabled</strong><br>'
                + '<span style="font-size:0.82rem;color:var(--text-muted)">WiFi radio is turned off at the OS level</span>'
                + '</div></div>'
                + wifiOffWarning;
            return;
        }

        // ── WiFi enabled but no saved networks ──────────────────────
        if (!status.connected && status.wifi_radio_enabled && !status.has_saved_wifi) {
            el.innerHTML =
                '<div style="display:flex;align-items:center;gap:12px">'
                + '<span class="material-symbols-outlined" style="font-size:2rem;color:#f0a030">wifi_find</span>'
                + '<div>'
                + '<strong>No WiFi networks configured</strong><br>'
                + '<span style="font-size:0.82rem;color:var(--text-muted)">Scan below to find and connect to a network</span>'
                + '</div></div>';
            return;
        }

        // ── Connected via Ethernet (WiFi radio is on) ───────────────
        if (status.connected && status.interface_type === "ethernet") {
            el.innerHTML =
                '<div style="display:flex;align-items:center;gap:12px">'
                + '<span class="material-symbols-outlined" style="font-size:2rem;color:var(--text-muted)">settings_ethernet</span>'
                + '<div>'
                + '<strong>Connected via Ethernet</strong><br>'
                + '<span style="font-size:0.82rem;color:var(--text-muted)">IP: ' + escapeHtml(status.ip || "—") + '</span><br>'
                + '<span style="font-size:0.78rem;color:var(--text-muted)">Interface: ' + escapeHtml(status.interface || "eth0") + '</span>'
                + '</div></div>';
            return;
        }

        // ── Connected via Wi-Fi ─────────────────────────────────────
        var signalBars = "";
        if (status.connected && status.signal > 0) {
            var level = Math.min(4, Math.ceil(status.signal / 25));
            for (var i = 0; i < 4; i++) {
                signalBars += '<span style="display:inline-block;width:6px;height:' + (6 + i*5) + 'px;background:' +
                    (i < level ? 'var(--primary)' : 'var(--border)') + ';margin-right:2px;border-radius:1px;vertical-align:middle"></span>';
            }
            signalBars += ' ' + status.signal + '%';
        }

        if (status.connected) {
            el.innerHTML =
                '<div style="display:flex;align-items:center;gap:12px">'
                + '<span class="material-symbols-outlined" style="font-size:2rem;color:var(--success)">wifi</span>'
                + '<div style="flex:1">'
                + '<strong>' + escapeHtml(status.ssid || "Unknown") + '</strong><br>'
                + '<span style="font-size:0.82rem;color:var(--text-muted)">IP: ' + escapeHtml(status.ip || "—") + '</span><br>'
                + '<span style="font-size:0.82rem;color:var(--text-muted)">' + signalBars + '</span>'
                + '</div>';

            // Add Forget button for WiFi connections
            if (status.interface_type === "wifi" && status.ssid) {
                el.innerHTML +=
                    '<button id="btn-forget-wifi" style="font-size:0.78rem;padding:4px 10px;background:var(--danger);color:#fff;border:none;border-radius:4px;cursor:pointer;white-space:nowrap">Forget</button>';
            }

            el.innerHTML += '</div>';

            // Bind forget handler after DOM update
            setTimeout(function () {
                var forgetBtn = document.getElementById("btn-forget-wifi");
                if (forgetBtn) {
                    forgetBtn.addEventListener("click", async function () {
                        if (!confirm("Forget the '" + escapeHtml(status.ssid) + "' network and disconnect? The AP will reactivate if no other network is available.")) return;
                        forgetBtn.disabled = true;
                        forgetBtn.textContent = "…";
                        var result = await apiPost("/network/forget", { ssid: status.ssid });
                        if (result && result.status === "ok") {
                            showToast("Network forgotten", "info");
                            _refreshNetworkStatus();
                        } else {
                            showToast("Failed to forget network", "error");
                            forgetBtn.disabled = false;
                            forgetBtn.textContent = "Forget";
                        }
                    });
                }
            }, 0);
        } else {
            el.innerHTML =
                '<div style="display:flex;align-items:center;gap:12px">'
                + '<span class="material-symbols-outlined" style="font-size:2rem;color:var(--danger)">wifi_off</span>'
                + '<div>'
                + '<strong>Not connected</strong><br>'
                + '<span style="font-size:0.82rem;color:var(--text-muted)">Use the captive portal or connect below</span>'
                + '</div></div>';
        }
    }

    async function _refreshNetworkScan() {
        var list = document.getElementById("network-scan-list");
        var statusEl = document.getElementById("network-scan-status");
        if (!list) return;

        list.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem">Scanning…</span>';
        if (statusEl) { statusEl.style.display = "none"; }

        // Get current connection to mark the active network
        var netStatus = await apiGet("/network/status");
        var currentSSID = (netStatus && netStatus.interface_type === "wifi") ? netStatus.ssid : "";

        var data = await apiGet("/network/scan");
        if (!data || !data.networks) {
            list.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem">Scan failed — is Wi-Fi available?</span>';
            return;
        }

        if (data.networks.length === 0) {
            list.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem">No networks found</span>';
            return;
        }

        list.innerHTML = "";
        data.networks.forEach(function (n) {
            var row = document.createElement("div");
            var isConnected = currentSSID && n.ssid === currentSSID;
            row.style.cssText = "display:flex;align-items:center;padding:8px 10px;border:1px solid var(--border);border-radius:6px;margin-bottom:4px;gap:8px"
                + (isConnected ? ";border-color:var(--success);background:rgba(46,204,113,0.08)" : "");

            var hasLock = n.security && n.security !== "--";
            var btnHTML = isConnected
                ? '<button disabled style="font-size:0.78rem;padding:4px 10px;background:var(--success);color:#fff;border:none;border-radius:4px;cursor:default;opacity:0.8">Connected</button>'
                : '<button style="font-size:0.78rem;padding:4px 10px;background:var(--primary);color:#fff;border:none;border-radius:4px;cursor:pointer">Connect</button>';

            row.innerHTML =
                '<span style="flex:1;font-size:0.9rem;font-weight:500">' + escapeHtml(n.ssid) + '</span>'
                + (hasLock ? '<span class="material-symbols-outlined" style="font-size:0.8rem;vertical-align:middle">lock</span> ' + escapeHtml(n.security) + '</span>' : '<span style="font-size:0.75rem;color:var(--text-muted)">Open</span>')
                + '<span style="font-size:0.8rem;color:var(--text-muted);min-width:45px;text-align:right">' + n.signal + '%</span>'
                + btnHTML;

            if (!isConnected) {
                row.querySelector("button").addEventListener("click", async function () {
                    if (hasLock) {
                        var pw = prompt("Enter password for " + n.ssid);
                        if (pw === null) return;
                        var result = await apiPost("/network/connect", { ssid: n.ssid, password: pw });
                        if (result && result.status === "ok") {
                            showToast("Connected to " + n.ssid, "success");
                            _refreshNetworkStatus();
                        } else {
                            showToast((result && result.message) || "Connection failed", "error");
                        }
                    } else {
                        var result = await apiPost("/network/connect", { ssid: n.ssid, password: "" });
                        if (result && result.status === "ok") {
                            showToast("Connected to " + n.ssid, "success");
                            _refreshNetworkStatus();
                        } else {
                            showToast((result && result.message) || "Connection failed", "error");
                        }
                    }
                });
            }

            list.appendChild(row);
        });
    }

    async function _refreshNetworkAPStatus() {
        var el = document.getElementById("network-ap-status");
        var btn = document.getElementById("btn-network-ap-toggle");
        if (!el || !btn) return;

        var status = await apiGet("/network/ap-status");
        if (status && status.active) {
            el.innerHTML = '<span class="material-symbols-outlined" style="font-size:1rem;vertical-align:middle;color:#f0a030">warning</span> <span style="color:#f0a030">AP mode active — SSID: <strong>Metixel-Setup</strong></span>';
            btn.textContent = "Stop AP Mode";
        } else {
            el.innerHTML = '<span class="material-symbols-outlined" style="font-size:1rem;vertical-align:middle;color:var(--text-muted)">radio_button_unchecked</span> <span style="color:var(--text-muted)">AP mode inactive</span>';
            btn.textContent = "Start AP Mode";
        }
    }

    // -- Background Processing Status ---------------------------------------

    /**
     * Poll the backend's processing status file and update the
     * "Background Processing" section of the controls card.
     */
    async function refreshProcessing() {
        var status = await apiGet("/config/processing");
        if (!status) return;

        var el = document.getElementById("processing-status");
        if (!el) return;

        var phase = status.phase || "";
        var total = status.total || 0;
        var processed = status.processed || 0;
        var currentFile = status.current_file || "";

        // Build the inner HTML — keep it stable to avoid layout shift
        // Use data attributes to detect when the DOM needs rebuilding
        if (!el.dataset.stable) {
            el.innerHTML =
                '<div class="processing-phase" id="proc-phase">Idle</div>'
                + '<div class="processing-detail" id="proc-detail"></div>'
                + '<div class="processing-bar-wrap" id="proc-bar-wrap" style="display:none">'
                + '<div class="processing-bar-fill" id="proc-bar-fill"></div>'
                + '</div>';
            el.dataset.stable = "1";
        }

        var phaseEl = document.getElementById("proc-phase");
        var detailEl = document.getElementById("proc-detail");
        var barWrap = document.getElementById("proc-bar-wrap");
        var barFill = document.getElementById("proc-bar-fill");

        // Determine the display state
        if (!phase || phase === "complete" || phase === "unknown") {
            // Idle or complete
            phaseEl.textContent = (phase === "complete") ? "Processing complete" : "Idle";
            phaseEl.className = "processing-phase" + (phase === "complete" ? " is-complete" : "");
            if (detailEl) detailEl.textContent = "";
            if (barWrap) barWrap.style.display = "none";
        } else {
            // Active processing
            var pct = total > 0 ? Math.round((processed / total) * 100) : 0;

            // Human-readable phase labels
            var phaseLabels = {
                "scanning": "Scanning files\u2026",
                "optimising_images": "Image Optimisation",
                "transcoding": "Transcoding video",
            };
            phaseEl.textContent = phaseLabels[phase] || phase;
            phaseEl.className = "processing-phase is-active"
                + (phase === "transcoding" ? " is-transcoding" : "");

            var detailParts = [];
            if (total > 0) {
                detailParts.push(processed + " / " + total + " files");
            }
            if (pct > 0) {
                detailParts.push(pct + "%");
            }
            if (currentFile) {
                // Show just the filename, not the full path
                var fname = currentFile.replace(/^.*[\\/]/, "");
                detailParts.push(fname);
            }
            if (detailEl) detailEl.textContent = detailParts.join(" — ");

            if (barWrap && barFill) {
                barWrap.style.display = "";
                barFill.style.width = pct + "%";
                barFill.className = "processing-bar-fill" + (pct >= 100 ? " is-complete" : "");
            }
        }
    }

    /** Refresh the Sync Status card on the Dashboard. */
    async function _refreshDashSyncStatus() {
        var data = await apiGet("/immich/status");
        if (!data) return;

        // ── Progress bar ─────────────────────────────────────────
        var progEl = document.getElementById("dash-sync-progress");
        var prog = data.progress;
        if (prog && prog.syncing && progEl) {
            progEl.style.display = "block";
            var phaseLabels = { "starting": "Starting\u2026", "resolving_album": "Looking up album\u2026", "fetching_assets": "Fetching assets\u2026", "downloading": "Downloading", "cleaning": "Cleaning up\u2026", "cancelled": "Cancelled", "error": "Error" };
            var phaseEl = document.getElementById("dash-sync-phase");
            if (phaseEl) phaseEl.textContent = phaseLabels[prog.phase] || prog.phase || "Syncing\u2026";
            var countEl = document.getElementById("dash-sync-count");
            if (countEl) countEl.textContent = prog.total > 0 ? prog.processed + " / " + prog.total : "";
            var barEl = document.getElementById("dash-sync-bar");
            if (barEl) barEl.style.width = prog.total > 0 ? Math.round(prog.processed / prog.total * 100) + "%" : "0%";
            var fileEl = document.getElementById("dash-sync-file");
            if (fileEl) fileEl.textContent = prog.current_file || "";
        } else if (progEl) {
            progEl.style.display = "none";
        }

        // ── Last sync status ─────────────────────────────────────
        var textEl = document.getElementById("dash-sync-text");
        var detailEl = document.getElementById("dash-sync-detail");
        if (!textEl) return;

        if (data.status === "never_run" || !data.last_sync) {
            textEl.textContent = "Never run";
            textEl.style.color = "var(--text-muted)";
            if (detailEl) detailEl.textContent = "";
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

        if (s.success) {
            textEl.textContent = ago + " — ✅ Success";
            textEl.style.color = "var(--success)";
        } else {
            var hasCancel = s.errors && s.errors.some(function (e) { return e.indexOf("Cancelled") >= 0; });
            textEl.textContent = ago + (hasCancel ? " — ⏹ Cancelled" : " — ⚠ Errors");
            textEl.style.color = hasCancel ? "var(--text-muted)" : "#f0a030";
        }

        var parts = [];
        if (s.total_remote > 0) parts.push(s.total_remote + " assets");
        if (s.downloaded > 0) parts.push(s.downloaded + " downloaded");
        if (s.skipped > 0) parts.push(s.skipped + " skipped");
        if (s.deleted > 0) parts.push(s.deleted + " deleted");
        if (s.duration_seconds) parts.push("took " + s.duration_seconds + "s");
        if (detailEl) detailEl.textContent = parts.join(" · ") || "—";
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
        setChecked("cfg-shuffle", s.shuffle !== false);

        // Matte color — parse RGB array to hex
        var matte = s.matte_color || [20, 20, 20];
        var matteHex = "#" + matte.map(function (c) {
            var h = c.toString(16);
            return h.length === 1 ? "0" + h : h;
        }).join("");
        setValue("cfg-matte-color", matteHex);
        setValue("cfg-matte-color-hex", matteHex);

        // Video
        var v = config.video || {};
        if (!v || Object.keys(v).length === 0) {
            v = {
                playback_enabled: (s.video_playback_enabled !== undefined ? s.video_playback_enabled : true),
                max_duration_seconds: s.video_max_duration_seconds || 0,
                transcoding_enabled: true,
                transcode_max_width: 0,
                transcode_max_height: 0,
                transcode_quality: 23,
                transcode_use_software_encoder: true,
                transcode_timeout_seconds: 7200,
                cpu_throttle_enabled: true,
                cpu_throttle_percent: 50,
            };
        }
        setChecked("cfg-video-enabled", v.playback_enabled === true);
        setValue("cfg-video-player-backend", v.player_backend || "auto");
        setValue("cfg-video-max-duration", v.max_duration_seconds || 0);
        setChecked("cfg-transcode-enabled", v.transcoding_enabled !== false);
        setValue("cfg-transcode-max-width", v.transcode_max_width || 0);
        setValue("cfg-transcode-max-height", v.transcode_max_height || 0);
        var q = v.transcode_quality !== undefined ? v.transcode_quality : 23;
        setValue("cfg-transcode-quality", q);
        var qLabel = document.getElementById("cfg-transcode-quality-label");
        if (qLabel) {
            var qDesc = q <= 20 ? " (high quality)" : q <= 26 ? " (good balance)" : " (smaller files)";
            qLabel.textContent = q + qDesc;
        }
        setChecked("cfg-transcode-software-encoder", v.transcode_use_software_encoder !== false);
        setValue("cfg-transcode-timeout", v.transcode_timeout_seconds || 7200);
        setChecked("cfg-cpu-throttle-enabled", v.cpu_throttle_enabled !== false);
        setValue("cfg-cpu-throttle-pct", v.cpu_throttle_percent || 50);
        var cpLabel = document.getElementById("cfg-cpu-throttle-pct-label");
        if (cpLabel) cpLabel.textContent = (v.cpu_throttle_percent || 50) + "%";
        _toggleTranscodeSettings(v.transcoding_enabled !== false);
        _toggleCpuThrottleGroup(v.cpu_throttle_enabled !== false);

        // Local folders (moved from Sync page)
        const local = config.sync?.local || {};
        setChecked("cfg-local-enabled", local.enabled !== false);
        setValue("cfg-local-interval", local.poll_interval_seconds || 30);
        renderWatchPaths(local.watch_paths || []);

        // Image optimisation (moved from Sync page)
        const imgCfg = config.image || {};
        setChecked("cfg-image-opt-enabled", imgCfg.optimisation_enabled !== false);
        setValue("cfg-image-max-width", imgCfg.optimise_max_width || 0);
        setValue("cfg-image-max-height", imgCfg.optimise_max_height || 0);
        _toggleImageOptSettings(imgCfg.optimisation_enabled !== false);

        // Event listeners — bind once
        if (!_settingsBound) {
            _settingsBound = true;

            document.getElementById("cfg-transition-duration")?.addEventListener("input", function () {
                document.getElementById("cfg-transition-duration-label").textContent = this.value + " ms";
            });

            document.getElementById("btn-save-slideshow")?.addEventListener("click", async () => {
                var hexVal = (document.getElementById("cfg-matte-color-hex")?.value || "#141414").replace("#", "");
                var r = parseInt(hexVal.substring(0, 2), 16) || 20;
                var g = parseInt(hexVal.substring(2, 4), 16) || 20;
                var b = parseInt(hexVal.substring(4, 6), 16) || 20;
                var result = await apiPut("/config/slideshow", {
                    image_duration_seconds: sanitizeInt(document.getElementById("cfg-duration").value, 30),
                    transition_duration_ms: sanitizeInt(document.getElementById("cfg-transition-duration").value, 1500),
                    transition_style: document.getElementById("cfg-transition").value,
                    fit_mode: document.getElementById("cfg-fit").value,
                    smart_cover: document.getElementById("cfg-smart-cover").checked,
                    shuffle: document.getElementById("cfg-shuffle").checked,
                    matte_color: [r, g, b],
                });
                if (result) {
                    showToast("Slideshow settings saved!", "success");
                } else {
                    showToast("Failed to save slideshow settings", "error");
                }
            });

            // ── Video Settings card ─────────────────────────────────────
            document.getElementById("cfg-transcode-enabled")?.addEventListener("change", function () {
                _toggleTranscodeSettings(this.checked);
            });
            document.getElementById("cfg-cpu-throttle-enabled")?.addEventListener("change", function () {
                _toggleCpuThrottleGroup(this.checked);
            });
            document.getElementById("cfg-transcode-quality")?.addEventListener("input", function () {
                var q = parseInt(this.value, 10);
                var qDesc = q <= 20 ? " (high quality)" : q <= 26 ? " (good balance)" : " (smaller files)";
                var lbl = document.getElementById("cfg-transcode-quality-label");
                if (lbl) lbl.textContent = q + qDesc;
            });
            document.getElementById("cfg-cpu-throttle-pct")?.addEventListener("input", function () {
                var lbl = document.getElementById("cfg-cpu-throttle-pct-label");
                if (lbl) lbl.textContent = this.value + "%";
            });

            // Matte color sync
            document.getElementById("cfg-matte-color")?.addEventListener("input", function () {
                var hexInput = document.getElementById("cfg-matte-color-hex");
                if (hexInput) hexInput.value = this.value;
            });
            document.getElementById("cfg-matte-color-hex")?.addEventListener("input", function () {
                var val = this.value.trim();
                if (/^#[0-9a-fA-F]{6}$/.test(val)) {
                    var picker = document.getElementById("cfg-matte-color");
                    if (picker) picker.value = val;
                }
            });

            document.getElementById("btn-save-video")?.addEventListener("click", async () => {
                var result = await apiPut("/config/video", {
                    playback_enabled: document.getElementById("cfg-video-enabled").checked,
                    player_backend: document.getElementById("cfg-video-player-backend").value,
                    max_duration_seconds: sanitizeInt(document.getElementById("cfg-video-max-duration").value, 0),
                    transcoding_enabled: document.getElementById("cfg-transcode-enabled").checked,
                    transcode_max_width: sanitizeInt(document.getElementById("cfg-transcode-max-width").value, 0),
                    transcode_max_height: sanitizeInt(document.getElementById("cfg-transcode-max-height").value, 0),
                    transcode_quality: sanitizeInt(document.getElementById("cfg-transcode-quality").value, 23),
                    transcode_use_software_encoder: document.getElementById("cfg-transcode-software-encoder").checked,
                    transcode_timeout_seconds: sanitizeInt(document.getElementById("cfg-transcode-timeout").value, 7200),
                    cpu_throttle_enabled: document.getElementById("cfg-cpu-throttle-enabled").checked,
                    cpu_throttle_percent: sanitizeInt(document.getElementById("cfg-cpu-throttle-pct").value, 50),
                });
                if (result) {
                    showToast("Video settings saved!", "success");
                } else {
                    showToast("Failed to save video settings", "error");
                }
            });

            // ── Video Optimisation save ────────────────────────────────
            document.getElementById("btn-save-transcode")?.addEventListener("click", async () => {
                var result = await apiPut("/config/video", {
                    playback_enabled: document.getElementById("cfg-video-enabled").checked,
                    player_backend: document.getElementById("cfg-video-player-backend").value,
                    max_duration_seconds: sanitizeInt(document.getElementById("cfg-video-max-duration").value, 0),
                    transcoding_enabled: document.getElementById("cfg-transcode-enabled").checked,
                    transcode_max_width: sanitizeInt(document.getElementById("cfg-transcode-max-width").value, 0),
                    transcode_max_height: sanitizeInt(document.getElementById("cfg-transcode-max-height").value, 0),
                    transcode_quality: sanitizeInt(document.getElementById("cfg-transcode-quality").value, 23),
                    transcode_use_software_encoder: document.getElementById("cfg-transcode-software-encoder").checked,
                    transcode_timeout_seconds: sanitizeInt(document.getElementById("cfg-transcode-timeout").value, 7200),
                    cpu_throttle_enabled: document.getElementById("cfg-cpu-throttle-enabled").checked,
                    cpu_throttle_percent: sanitizeInt(document.getElementById("cfg-cpu-throttle-pct").value, 50),
                });
                if (result) {
                    showToast("Video optimisation saved!", "success");
                } else {
                    showToast("Failed to save video optimisation", "error");
                }
            });

            // ── Local Sync save ────────────────────────────────────────
            document.getElementById("btn-save-local-sync")?.addEventListener("click", async () => {
                var result = await apiPut("/config/sync", {
                    local: {
                        enabled: document.getElementById("cfg-local-enabled").checked,
                        watch_paths: collectWatchPaths(),
                        poll_interval_seconds: sanitizeInt(document.getElementById("cfg-local-interval").value, 30),
                    },
                });
                if (result) {
                    showToast("Local sync settings saved!", "success");
                } else {
                    showToast("Failed to save local sync settings", "error");
                }
            });

            // ── Image Optimisation save ─────────────────────────────────
            document.getElementById("btn-save-image-opt")?.addEventListener("click", async () => {
                var result = await apiPut("/config/image", {
                    optimisation_enabled: document.getElementById("cfg-image-opt-enabled").checked,
                    optimise_max_width: sanitizeInt(document.getElementById("cfg-image-max-width").value, 0),
                    optimise_max_height: sanitizeInt(document.getElementById("cfg-image-max-height").value, 0),
                });
                if (result) {
                    showToast("Image optimisation settings saved!", "success");
                } else {
                    showToast("Failed to save image optimisation settings", "error");
                }
            });

            // Image optimisation toggle
            document.getElementById("cfg-image-opt-enabled")?.addEventListener("change", function () {
                _toggleImageOptSettings(this.checked);
            });

            // Add Watch Path button
            document.getElementById("btn-add-watch-path")?.addEventListener("click", function () {
                addWatchPathRow("", true, true);
            });

            // Schedule toggle
            document.getElementById("cfg-schedule-enabled")?.addEventListener("change", function () {
                toggleScheduleFields(this.checked);
            });

            // NTP toggle
            document.getElementById("cfg-ntp-enabled")?.addEventListener("change", function () {
                toggleNtpFields(this.checked);
            });

            // Timezone set button
            document.getElementById("btn-save-timezone")?.addEventListener("click", async function () {
                var tz = document.getElementById("cfg-timezone").value;
                if (!tz) { showToast("Select a timezone first", "info"); return; }
                var result = await apiPost("/config/timezone", { timezone: tz });
                if (result && result.status === "ok") {
                    showToast("Timezone set to " + tz, "success");
                    _refreshServerClock();
                } else {
                    showToast("Failed to set timezone: " + ((result && result.message) || "Unknown error"), "error");
                }
            });

            // Browse buttons for cache dir and sync dir
            document.querySelectorAll(".btn-browse").forEach(function (btn) {
                btn.addEventListener("click", function () {
                    var targetId = this.getAttribute("data-target");
                    var inputEl = document.getElementById(targetId);
                    if (inputEl) openFolderBrowser(inputEl);
                });
            });

            // Folder Browser modal handlers
            document.getElementById("btn-browser-cancel")?.addEventListener("click", closeFolderBrowser);
            document.getElementById("folder-browser-modal")?.addEventListener("click", function (e) {
                if (e.target === this) closeFolderBrowser();
            });
        }
    }

    // -- Sync ---------------------------------------------------------------

    var _syncBound = false;
    var _syncPollTimer = null;
    var _syncWasActive = false;
    var _albumData = null;  // Tracks if we've seen an active sync for auto-stop

    function startSyncPolling() {
        if (_syncPollTimer) return;
        _syncPollTimer = setInterval(async function () {
            // Single API call per tick — refreshSyncStatus() renders
            // both the progress bar AND the last-sync summary from
            // the same response, avoiding a race where the progress
            // file is deleted between two separate calls.
            await refreshSyncStatus();

            // Check if sync has finished (progress bar hidden by
            // refreshSyncStatus when syncing=false).  We can't use
            // a separate call here because the progress file is
            // atomically deleted after the sync completes.
        }, 1500);
    }

    function stopSyncPolling() {
        if (_syncPollTimer) {
            clearInterval(_syncPollTimer);
            _syncPollTimer = null;
        }
        var cancelBtn = document.getElementById("btn-cancel-sync");
        if (cancelBtn) cancelBtn.style.display = "none";
        var progressDiv = document.getElementById("immich-progress");
        if (progressDiv) progressDiv.style.display = "none";
    }

    // -- Watch Paths (per-row) ----------------------------------------------

    /**
     * Render all watch path rows from the config array.
     * @param {Array} paths - Array of {path, enabled} objects.
     */
    function renderWatchPaths(paths) {
        var list = document.getElementById("watch-paths-list");
        if (!list) return;
        list.innerHTML = "";

        // Ensure we have at least the defaults if config is empty
        if (!paths || paths.length === 0) {
            paths = [
                { path: "media/sample_media/", enabled: true },
                { path: "media/sync/immich/", enabled: true },
                { path: "media/my_media/", enabled: true }
            ];
        }

        paths.forEach(function (entry) {
            // Support both new object format and legacy string format
            var pathVal, enabled;
            if (typeof entry === "object" && entry !== null) {
                pathVal = entry.path || "";
                enabled = entry.enabled !== false;
            } else {
                pathVal = String(entry);
                enabled = true;
            }
            addWatchPathRow(pathVal, enabled);
        });
    }

    /**
     * Add a single watch path row to the DOM.
     * @param {string} pathVal - The folder path.
     * @param {boolean} enabled - Whether the path is enabled.
     * @param {boolean} focus - Whether to focus the input (for new rows).
     */
    function addWatchPathRow(pathVal, enabled, focus) {
        var list = document.getElementById("watch-paths-list");
        if (!list) return;

        var row = document.createElement("div");
        row.className = "watch-path-row";
        row.style.cssText = "display:flex;gap:0.35rem;align-items:center;margin-bottom:0.35rem";

        // Enable toggle
        var toggle = document.createElement("input");
        toggle.type = "checkbox";
        toggle.checked = enabled !== false;
        toggle.title = "Enable/disable this watch path";
        toggle.style.cssText = "flex-shrink:0;margin:0";

        // Path input
        var input = document.createElement("input");
        input.type = "text";
        input.value = pathVal;
        input.placeholder = "media/my_media/";
        input.style.cssText = "flex:1;min-width:140px;padding:0.3rem 0.5rem;border:1px solid var(--border);border-radius:4px;background:var(--bg);color:var(--text);font-size:0.82rem";

        // Browse button
        var browseBtn = document.createElement("button");
        browseBtn.type = "button";
        browseBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:1rem;vertical-align:middle">folder_open</span>';
        browseBtn.title = "Browse folders";
        browseBtn.className = "btn--secondary";
        browseBtn.style.cssText = "flex-shrink:0;padding:0.3rem 0.5rem;font-size:0.9rem";
        browseBtn.addEventListener("click", function () {
            openFolderBrowser(input);
        });

        // Remove button
        var removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:0.9rem;vertical-align:middle">close</span>';
        removeBtn.title = "Remove this watch path";
        removeBtn.className = "btn--danger";
        removeBtn.style.cssText = "flex-shrink:0;padding:0.3rem 0.5rem;font-size:0.82rem";
        removeBtn.addEventListener("click", function () {
            row.remove();
        });

        row.appendChild(toggle);
        row.appendChild(input);
        row.appendChild(browseBtn);
        row.appendChild(removeBtn);
        list.appendChild(row);

        if (focus) {
            input.focus();
            input.select();
        }
    }

    /**
     * Collect all watch path rows into the config array format.
     * @returns {Array} Array of {path, enabled} objects.
     */
    function collectWatchPaths() {
        var rows = document.querySelectorAll("#watch-paths-list .watch-path-row");
        var paths = [];
        rows.forEach(function (row) {
            var inputs = row.querySelectorAll("input");
            if (inputs.length >= 2) {
                var enabled = inputs[0].checked;
                var pathVal = inputs[1].value.trim();
                if (pathVal) {
                    paths.push({ path: pathVal, enabled: enabled });
                }
            }
        });
        return paths;
    }

    // -- Folder Browser ------------------------------------------------------

    /** @type {HTMLInputElement|null} The input element to fill when a folder is selected. */
    var _browserTargetInput = null;

    /**
     * Open the folder browser modal for a watch path input.
     * @param {HTMLInputElement} inputEl - The input to fill on selection.
     */
    function openFolderBrowser(inputEl) {
        _browserTargetInput = inputEl;
        var modal = document.getElementById("folder-browser-modal");
        if (modal) modal.classList.add("open");
        // Start browsing at the current input value or /opt/metixel/
        var startPath = inputEl.value.trim() || "/opt/metixel/";
        browseFolder(startPath);
    }

    function closeFolderBrowser() {
        var modal = document.getElementById("folder-browser-modal");
        if (modal) modal.classList.remove("open");
        _browserTargetInput = null;
    }

    /**
     * Browse a folder via the API and populate the modal list.
     * @param {string} folderPath - The path to browse.
     */
    async function browseFolder(folderPath) {
        var pathEl = document.getElementById("browser-current-path");
        var listEl = document.getElementById("browser-entries");
        if (!listEl) return;

        listEl.innerHTML = '<li style="padding:0.5rem;color:var(--text-muted)">Loading…</li>';

        var data = await apiGet("/config/browse?path=" + encodeURIComponent(folderPath));
        if (!data || data.error) {
            listEl.innerHTML = '<li style="padding:0.5rem;color:var(--danger)">' + escapeHtml((data && data.error) || "Cannot browse folder") + '</li>';
            return;
        }

        if (pathEl) pathEl.textContent = data.current_path || folderPath;

        // Parent directory button state
        var parentBtn = document.getElementById("btn-browser-parent");
        if (parentBtn) {
            parentBtn.disabled = !data.parent_path;
            parentBtn.onclick = function () {
                if (data.parent_path) browseFolder(data.parent_path);
            };
        }

        // Build entry list
        var html = "";
        if (!data.entries || data.entries.length === 0) {
            html = '<li style="padding:0.5rem;color:var(--text-muted)">No subdirectories</li>';
        } else {
            data.entries.forEach(function (entry) {
                html += '<li class="browser-entry" data-path="' + escapeHtml(entry.path) + '" style="padding:0.4rem 0.5rem;cursor:pointer;border-bottom:1px solid var(--border);font-size:0.82rem"><span class="material-symbols-outlined" style="font-size:0.9rem;vertical-align:middle">folder</span> ' + escapeHtml(entry.name) + '</li>';
            });
        }
        listEl.innerHTML = html;

        // Click handlers for entries (navigate into subdir)
        listEl.querySelectorAll(".browser-entry").forEach(function (li) {
            li.addEventListener("click", function () {
                browseFolder(li.getAttribute("data-path"));
            });
            li.addEventListener("mouseenter", function () {
                this.style.background = "var(--accent-bg)";
            });
            li.addEventListener("mouseleave", function () {
                this.style.background = "";
            });
        });

        // Select button: use the currently browsed folder
        var selectBtn = document.getElementById("btn-browser-select");
        if (selectBtn) {
            selectBtn.onclick = function () {
                if (_browserTargetInput && data.current_path) {
                    // Make path relative to /opt/metixel/ if possible
                    var relPath = data.current_path;
                    var basePrefix = "/opt/metixel/";
                    if (relPath.indexOf(basePrefix) === 0) {
                        relPath = relPath.substring(basePrefix.length);
                        if (!relPath.endsWith("/")) relPath += "/";
                    }
                    _browserTargetInput.value = relPath;
                }
                closeFolderBrowser();
            };
        }
    }

    // -- Image Optimisation helpers ------------------------------------------

    function _toggleImageOptSettings(enabled) {
        var el = document.getElementById("image-opt-settings");
        if (el) {
            el.style.display = enabled ? "" : "none";
            el.style.opacity = enabled ? "1" : "0.5";
        }
    }

    function _toggleImmichInterval(enabled) {
        var el = document.getElementById("immich-interval-group");
        if (el) {
            el.style.display = enabled ? "" : "none";
        }
    }

    function _populateAlbumSelect(albums, filter) {
        var select = document.getElementById("cfg-immich-album");
        if (!select) return;
        var configuredAlbum = select.getAttribute("data-saved") || "";
        var q = (filter || "").toLowerCase().trim();
        var html = '<option value="">— Select an album —</option>';
        var count = 0;
        albums.forEach(function (album) {
            if (q && album.name.toLowerCase().indexOf(q) === -1) return;
            var selected = (album.name === configuredAlbum) ? " selected" : "";
            html += '<option value="' + escapeHtml(album.name) + '"' + selected + '>'
                + escapeHtml(album.name) + ' (' + album.assetCount + ' assets)</option>';
            count++;
        });
        if (count === 0 && q) {
            html += '<option value="" disabled>No matching albums</option>';
        }
        select.innerHTML = html;
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

        // Preselect the configured album (if any) in the dropdown
        var albumSelect = document.getElementById("cfg-immich-album");
        var configuredAlbum = imm.album_name || "";
        if (albumSelect && configuredAlbum) {
            var exists = false;
            for (var i = 0; i < albumSelect.options.length; i++) {
                if (albumSelect.options[i].value === configuredAlbum) {
                    albumSelect.selectedIndex = i;
                    exists = true;
                    break;
                }
            }
            if (!exists && configuredAlbum) {
                var opt = document.createElement("option");
                opt.value = configuredAlbum;
                opt.textContent = configuredAlbum + " (saved)";
                opt.selected = true;
                albumSelect.appendChild(opt);
            }
        }

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
                    if (resultEl) { resultEl.textContent = "❌ Request failed"; resultEl.style.color = "var(--danger)"; }
                    return;
                }
                if (data.ok) {
                    if (resultEl) { resultEl.textContent = "✅ " + data.message; resultEl.style.color = "var(--success)"; }
                    showToast("Connection successful!", "success");
                } else {
                    if (resultEl) { resultEl.textContent = "❌ " + (data.error || "Unknown error"); resultEl.style.color = "var(--danger)"; }
                    showToast("Connection failed: " + data.error, "error", 5000);
                }
            });

            // -- Fetch Albums --
            document.getElementById("btn-fetch-albums")?.addEventListener("click", async () => {
                var select = document.getElementById("cfg-immich-album");
                if (!select) return;
                select.disabled = true;
                select.innerHTML = '<option value="">Loading…</option>';

                var data = await apiGet("/immich/albums");
                if (!data || data.error) {
                    select.innerHTML = '<option value="">— Failed to load —</option>';
                    select.disabled = false;
                    showToast("Failed to fetch albums: " + ((data && data.error) || "Network error"), "error", 5000);
                    return;
                }

                // Store albums for search filtering
                _albumData = data;
                _populateAlbumSelect(data);
                select.disabled = false;

                // Show search input
                var searchInput = document.getElementById("album-search");
                if (searchInput) { searchInput.style.display = ""; searchInput.value = ""; }
                showToast("Loaded " + data.length + " album(s)", "info");
            });

            // Album search filter
            document.getElementById("album-search")?.addEventListener("input", function () {
                if (_albumData) _populateAlbumSelect(_albumData, this.value);
            });

            // -- Save Immich Settings --
            document.getElementById("btn-save-immich")?.addEventListener("click", async () => {
                var albumSelect = document.getElementById("cfg-immich-album");
                var albumName = albumSelect ? albumSelect.value : "";
                var result = await apiPut("/config/sync", {
                    immich: {
                        enabled: document.getElementById("cfg-immich-enabled").checked,
                        server_url: document.getElementById("cfg-immich-url").value,
                        api_key: document.getElementById("cfg-immich-key").value,
                        album_name: albumName,
                        sync_dir: document.getElementById("cfg-immich-sync-dir").value,
                        strict_sync: document.getElementById("cfg-immich-strict").checked,
                        poll_interval_seconds: Math.round(parseFloat(document.getElementById("cfg-immich-interval").value) * 3600) || 3600,
                    },
                });
                if (result) {
                    showToast("Immich settings saved!", "success");
                    // Remember the selected album for future fetches
                    if (albumSelect) albumSelect.setAttribute("data-saved", albumName);
                } else {
                    showToast("Failed to save Immich settings", "error");
                }
            });

            // -- Sync Now --
            document.getElementById("btn-sync-now")?.addEventListener("click", async () => {
                var btn = document.getElementById("btn-sync-now");
                if (btn) { btn.disabled = true; btn.textContent = "Syncing…"; }

                // Optionally override album from the picker
                var albumSelect = document.getElementById("cfg-immich-album");
                var body = {};
                if (albumSelect && albumSelect.value) {
                    body.album_name = albumSelect.value;
                }

                var result = await apiPost("/immich/sync", body);
                if (result && result.status === "started") {
                    showToast("Sync started — check status below", "info");
                    _syncWasActive = true;  // Set immediately — sync may finish before first poll
                    startSyncPolling();
                } else {
                    showToast("Failed to start sync", "error");
                    if (btn) { btn.disabled = false; btn.textContent = "Sync Now"; }
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
            if (progressDiv) progressDiv.style.display = "block";
            if (cancelBtn) cancelBtn.style.display = "inline-block";

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
            if (progressDiv) progressDiv.style.display = "none";
            if (cancelBtn) cancelBtn.style.display = "none";

            // If a sync was running and now it's finished, stop the
            // polling interval and re-enable the Sync Now button.
            if (_syncWasActive) {
                _syncWasActive = false;
                stopSyncPolling();
                var btn = document.getElementById("btn-sync-now");
                if (btn) { btn.disabled = false; btn.textContent = "Sync Now"; }
            }
        }

        // ── Last result ───────────────────────────────────────────
        statusEl.style.display = "block";

        if (data.status === "never_run" || !data.last_sync) {
            textEl.textContent = "Never run";
            textEl.style.color = "var(--text-muted)";
            detailEl.textContent = "";
            if (errorsEl) { errorsEl.style.display = "none"; errorsEl.innerHTML = ""; }
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
            textEl.textContent = ago + " — ✅ Success";
            textEl.style.color = "var(--success)";
        } else if (hasCancel) {
            textEl.textContent = ago + " — ⏹ Cancelled";
            textEl.style.color = "var(--text-muted)";
        } else {
            textEl.textContent = ago + " — ⚠ Completed with errors";
            textEl.style.color = "#f0a030";
        }

        // Summary line
        var parts = [];
        if (s.total_remote > 0) parts.push(s.total_remote + " assets in album");
        if (s.downloaded > 0) parts.push(s.downloaded + " downloaded");
        if (s.skipped > 0) parts.push(s.skipped + " skipped");
        if (s.deleted > 0) parts.push(s.deleted + " deleted");
        if (s.duration_seconds) parts.push("took " + s.duration_seconds + "s");
        detailEl.textContent = parts.join(" · ");

        // Error list (excluding Cancelled which is shown in the status line)
        if (errorsEl) {
            var realErrors = (s.errors || []).filter(function (e) { return e.indexOf("Cancelled") < 0; });
            if (realErrors.length > 0) {
                errorsEl.style.display = "block";
                errorsEl.innerHTML = realErrors.map(function (e) { return "<li>" + escapeHtml(e) + "</li>"; }).join("");
            } else {
                errorsEl.style.display = "none";
                errorsEl.innerHTML = "";
            }
        }
    }

    // -- Media --------------------------------------------------------------

    var _mediaOffset = 0;
    var _mediaLimit = 50;
    var _mediaHasMore = false;
    var _mediaLoading = false;
    /** Full media items cache for client-side filtering */
    var _allMediaItems = [];
    /** Set of unique folders extracted from media paths */
    var _mediaFolders = [];

    async function loadMedia() {
        _mediaOffset = 0;
        _mediaHasMore = false;
        _mediaLoading = false;
        _allMediaItems = [];
        _mediaFolders = [];

        var el = document.getElementById("media-list");
        el.innerHTML = '<p style="color:var(--text-muted)">Loading…</p>';

        // Populate folder filter dropdown from all watch paths
        var config = await apiGet("/config");
        if (config && config.sync && config.sync.local && config.sync.local.watch_paths) {
            var paths = config.sync.local.watch_paths;
            var sel = document.getElementById("media-filter-folder");
            if (sel) {
                // Keep the "All folders" option
                sel.innerHTML = '<option value="">All folders</option>';
                paths.forEach(function (p) {
                    var pathVal = typeof p === "object" ? p.path : String(p);
                    if (pathVal) {
                        var opt = document.createElement("option");
                        opt.value = pathVal;
                        opt.textContent = pathVal;
                        sel.appendChild(opt);
                    }
                });
            }
        }

        await _fetchMediaPage(0);
    }

    /** Apply client-side filters and re-render the media grid. */
    function _applyMediaFilters() {
        var nameFilter = (document.getElementById("media-filter-name")?.value || "").toLowerCase().trim();
        var folderFilter = document.getElementById("media-filter-folder")?.value || "";
        var typeFilter = document.getElementById("media-filter-type")?.value || "";

        var filtered = _allMediaItems.filter(function (item) {
            // Filename filter
            if (nameFilter && item.name.toLowerCase().indexOf(nameFilter) === -1) return false;
            // Folder filter — match watch path prefix
            if (folderFilter) {
                var itemPath = item.path || "";
                var itemFolder = item.folder || "";
                if (itemFolder.indexOf(folderFilter) !== 0 && itemPath.indexOf(folderFilter) !== 0) return false;
            }
            // Type filter
            if (typeFilter && item.media_type !== typeFilter) return false;
            return true;
        });

        var el = document.getElementById("media-list");
        var grid = document.getElementById("media-grid");
        if (!grid) {
            el.innerHTML = '<div class="media-grid" id="media-grid"></div>';
            grid = document.getElementById("media-grid");
        } else {
            grid.innerHTML = "";
        }

        if (filtered.length === 0) {
            el.innerHTML = '<p style="color:var(--text-muted)">No media match the current filters.</p>';
            // Rebuild summary with grid container
            el.innerHTML = '<p class="media-summary">0 files</p>'
                + '<div class="media-grid" id="media-grid"></div>';
            return;
        }

        // Update summary
        var summaryEl = el.querySelector(".media-summary");
        if (!summaryEl) {
            summaryEl = document.createElement("p");
            summaryEl.className = "media-summary";
            el.insertBefore(summaryEl, el.firstChild);
        }
        var imgCount = 0, vidCount = 0;
        filtered.forEach(function (item) { if (item.media_type === "video") vidCount++; else imgCount++; });
        var parts = [];
        if (imgCount) parts.push(imgCount + " images");
        if (vidCount) parts.push(vidCount + " videos");
        summaryEl.textContent = parts.length ? parts.join(", ") : filtered.length + " files";

        _renderMediaBatch(grid, filtered, 0);
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

        // Store all items for client-side filtering
        if (offset === 0) {
            _allMediaItems = data.items.slice();
        } else {
            _allMediaItems = _allMediaItems.concat(data.items);
        }

        // Build summary on first page
        var html = '';
        if (offset === 0) {
            var summaryParts = [];
            if (data.images) summaryParts.push(data.images + " images");
            if (data.videos) summaryParts.push(data.videos + " videos");
            html += '<p class="media-summary">'
                + (summaryParts.length ? summaryParts.join(", ") : data.total + " files") + '</p>'
                + '<div class="media-grid" id="media-grid"></div>';
            el.innerHTML = html;
        }

        var grid = document.getElementById("media-grid");
        if (!grid) {
            el.innerHTML = '<div class="media-grid" id="media-grid"></div>';
            grid = document.getElementById("media-grid");
        }

        // Apply current filters instead of raw render
        _applyMediaFilters();

        // Show "Load more" button
        _updateLoadMoreButton(el);
        _mediaLoading = false;

        // Wire filter event listeners once
        _bindMediaFilters();
    }

    var _mediaFiltersBound = false;
    function _bindMediaFilters() {
        if (_mediaFiltersBound) return;
        _mediaFiltersBound = true;
        document.getElementById("media-filter-name")?.addEventListener("input", function () {
            _applyMediaFilters();
        });
        document.getElementById("media-filter-folder")?.addEventListener("change", function () {
            _applyMediaFilters();
        });
        document.getElementById("media-filter-type")?.addEventListener("change", function () {
            _applyMediaFilters();
        });
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

            // Build type + status badges
            var badges = '';
            if (isVideo) {
                badges += ' <span class="media-badge media-badge--video">Video</span>';
            }
            if (item.transcode_status === "queued") {
                badges += ' <span class="media-badge media-badge--queued">Queued</span>';
            } else if (item.transcode_status === "transcoding") {
                badges += ' <span class="media-badge media-badge--transcoding">Transcoding</span>';
            }

            // Extract folder from relative path (e.g. "sub/folder/file.jpg" → "sub/folder/")
            // Prefer the API's `folder` field (watch folder name), then append subdirectory.
            var folderParts = [];
            if (item.folder) {
                folderParts.push(item.folder);
            }
            if (item.path) {
                var lastSlash = item.path.lastIndexOf('/');
                if (lastSlash > 0) {
                    folderParts.push(item.path.substring(0, lastSlash));
                }
            }
            var folderHtml = folderParts.length
                ? '<div class="media-folder">' + escapeHtml(folderParts.join(' › ')) + '</div>'
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
                + '<div class="media-name">' + escapeHtml(item.name) + badges + '</div>'
                + folderHtml
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

        // Start log polling (moved from Dashboard)
        if (_advancedLogTimer) {
            clearInterval(_advancedLogTimer);
            _advancedLogTimer = null;
        }
        await refreshLogs();
        _advancedLogTimer = setInterval(async function () {
            if (document.getElementById("page-advanced").classList.contains("active")) {
                await refreshLogs();
            } else if (_advancedLogTimer) {
                clearInterval(_advancedLogTimer);
                _advancedLogTimer = null;
            }
        }, 3000);

        // Display Settings (moved from Settings page)
        const d = config.display || {};
        const isAuto = (d.width === 0 && d.height === 0);
        setChecked("cfg-display-auto", isAuto);
        setValue("cfg-display-width", d.width || 0);
        setValue("cfg-display-height", d.height || 0);
        setValue("cfg-fps-limit", d.fps_limit || 30);
        setChecked("cfg-schedule-enabled", d.schedule_enabled === true);
        setValue("cfg-schedule-on", d.schedule_on_time || "07:00");
        setValue("cfg-schedule-off", d.schedule_off_time || "22:00");
        toggleScheduleFields(d.schedule_enabled === true);
        toggleResolutionFields(isAuto);

        // Fetch detected display resolution from the frontend
        apiGet("/config/display/info").then(function (info) {
            if (info && info.width > 0 && info.height > 0) {
                var el = document.getElementById("display-detected-res");
                if (el) {
                    el.textContent = "Detected: " + info.width + " × " + info.height;
                    el.style.color = "var(--text-muted)";
                }
            }
        });

        // System
        var sys = config.system || {};
        setValue("cfg-log-level", sys.log_level || "INFO");
        setValue("cfg-cache-dir", sys.cache_dir || "cache/");
        setChecked("cfg-quiet-boot", sys.quiet_boot === true);
        setChecked("cfg-ntp-enabled", sys.ntp_enabled !== false);
        var ntpServers = sys.ntp_servers || [""];
        setValue("cfg-ntp-server-1", ntpServers[0] || "");
        setValue("cfg-ntp-server-2", ntpServers[1] || "");
        setValue("cfg-ntp-server-3", ntpServers[2] || "");
        toggleNtpFields(sys.ntp_enabled !== false);

        // Load timezone dropdown
        loadTimezoneList(sys.timezone || "");

        // Start server clock
        _refreshServerClock();
        if (_clockTimer) clearInterval(_clockTimer);
        _clockTimer = setInterval(_refreshServerClock, 10000);

        // Updates / System Info
        apiGet("/config/info").then(function (info) {
            if (!info) return;
            _setStat("info-app-version", "v" + (info.app_version || "--"));
            _setStat("info-pi-model", info.pi_model || "--");
            _setStat("info-os-release", info.os_release || "--");
            _setStat("info-kernel", info.kernel || "--");
            _setStat("info-python", info.python_version || "--");
            _setStat("info-pi3d", info.pi3d_version || "--");
            _setStat("info-gpu-mem", info.gpu_memory || "--");
            _setStat("info-drm-driver", info.drm_driver || "--");
            _setStat("info-hostname", info.hostname || "--");
        });

        // Update channel & available versions
        loadUpdateStatus();

        // Web
        var web = config.web || {};
        setValue("cfg-web-host", web.host || "0.0.0.0");
        setValue("cfg-web-port", web.port || 8080);
        if (!_advancedBound) {
            _advancedBound = true;

            // Display settings
            document.getElementById("cfg-display-auto")?.addEventListener("change", function () {
                toggleResolutionFields(this.checked);
            });

            document.getElementById("btn-save-display")?.addEventListener("click", async () => {
                const isAutoSave = document.getElementById("cfg-display-auto").checked;
                var result = await apiPut("/config/display", {
                    width: isAutoSave ? 0 : sanitizeInt(document.getElementById("cfg-display-width").value, 0),
                    height: isAutoSave ? 0 : sanitizeInt(document.getElementById("cfg-display-height").value, 0),
                    fps_limit: sanitizeInt(document.getElementById("cfg-fps-limit").value, 30),
                    schedule_enabled: document.getElementById("cfg-schedule-enabled").checked,
                    schedule_on_time: document.getElementById("cfg-schedule-on").value,
                    schedule_off_time: document.getElementById("cfg-schedule-off").value,
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

            document.getElementById("btn-save-system")?.addEventListener("click", async () => {
                var logLevel = document.getElementById("cfg-log-level").value;
                var quietBoot = document.getElementById("cfg-quiet-boot").checked;
                var ntpEnabled = document.getElementById("cfg-ntp-enabled").checked;
                var ntpServers = [
                    document.getElementById("cfg-ntp-server-1").value.trim(),
                    document.getElementById("cfg-ntp-server-2").value.trim(),
                    document.getElementById("cfg-ntp-server-3").value.trim(),
                ].filter(function(s) { return s !== ""; });
                var sysResult = await apiPut("/config/system", {
                    log_level: logLevel,
                    cache_dir: document.getElementById("cfg-cache-dir").value,
                    quiet_boot: quietBoot,
                    ntp_enabled: ntpEnabled,
                    ntp_servers: ntpServers,
                });
                var logResult = await apiPost("/logs/level", { level: logLevel });
                // Apply NTP settings via systemd-timesyncd
                if (ntpEnabled) {
                    await apiPost("/config/ntp", {
                        enabled: true,
                        servers: ntpServers,
                    });
                } else {
                    await apiPost("/config/ntp", { enabled: false });
                }
                // Apply quiet boot change (requires sudo)
                if (quietBoot !== (sys.quiet_boot === true)) {
                    await apiPost("/config/quiet-boot", { enabled: quietBoot });
                }
                if (sysResult && logResult && logResult.status === "ok") {
                    var msg = "System settings saved! File log level: " + logLevel;
                    if (quietBoot !== (sys.quiet_boot === true)) {
                        msg += " | Quiet boot " + (quietBoot ? "enabled" : "disabled") + " — reboot to apply.";
                    }
                    showToast(msg, "success");
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
                if (!confirm("Clear all cached files and restart services?\n\n"
                           + "This deletes transcoded videos, thumbnails, and processed images, "
                           + "then restarts the backend and frontend to prevent missing-file errors. "
                           + "Playback will be interrupted for ~10 seconds.")) {
                    return;
                }
                clearCacheBtn.disabled = true;
                clearCacheBtn.textContent = "Clearing…";
                var result = await apiPost("/media/cache/clear");
                if (result && result.status === "ok") {
                    showToast(
                        "Cache cleared: " + result.deleted_files + " files, " + result.freed_mb + " MB freed. Restarting services…",
                        "success", 5000
                    );
                    // Restart services to prevent stale cached-file errors
                    try {
                        await apiPost("/config/restart");
                    } catch (_) {
                        // Expected — the backend is restarting, so the request may fail
                    }
                } else {
                    clearCacheBtn.disabled = false;
                    clearCacheBtn.textContent = "Clear Image Cache";
                    showToast("Failed to clear image cache", "error");
                }
            });

            // Restart all services
            var restartBtn = document.getElementById("btn-restart-services");
            restartBtn?.addEventListener("click", async () => {
                if (!confirm("Restart all Metixel services? Playback will be interrupted for ~10 seconds.")) {
                    return;
                }
                restartBtn.disabled = true;
                restartBtn.textContent = "Restarting…";
                showToast("Restarting services…", "info", 5000);
                try {
                    await apiPost("/config/restart");
                } catch (_) {
                    // Expected — the backend is restarting
                }
                // The button will re-enable when the page reloads after restart
            });

            // Reboot system
            var rebootBtn = document.getElementById("btn-reboot-system");
            rebootBtn?.addEventListener("click", async () => {
                if (!confirm("Reboot the entire system? The photo frame will be unavailable for ~60 seconds.")) {
                    return;
                }
                rebootBtn.disabled = true;
                rebootBtn.textContent = "Rebooting…";
                showToast("Rebooting system…", "info", 5000);
                try {
                    await apiPost("/config/reboot");
                } catch (_) {
                    // Expected — the system is going down
                }
            });

            // Shutdown system
            var shutdownBtn = document.getElementById("btn-shutdown-system");
            shutdownBtn?.addEventListener("click", async () => {
                if (!confirm("Shut down the entire system? You will need to physically power-cycle the Pi to turn it back on.")) {
                    return;
                }
                shutdownBtn.disabled = true;
                shutdownBtn.textContent = "Shutting down…";
                showToast("Shutting down system…", "info", 5000);
                try {
                    await apiPost("/config/shutdown");
                } catch (_) {
                    // Expected — the system is going down
                }
            });

            // ── Update Controls ──────────────────────────────────────
            bindUpdateControls();
        }
    }

    // -- OTA Updates ---------------------------------------------------------

    var _updateBound = false;

    /** Load and render the update status from the backend. */
    async function loadUpdateStatus() {
        var status = await apiGet("/updates/status");
        if (!status) return;

        // ── Channel selector ───────────────────────────────────────
        setValue("cfg-update-channel", status.current_channel || "stable");
        _updateChannelDesc(status.current_channel);

        // ── Auto-check toggle ──────────────────────────────────────
        setChecked("cfg-update-auto-check", status.auto_check !== false);

        // ── Status line ────────────────────────────────────────────
        var statusEl = document.getElementById("update-status");
        if (!statusEl) return;

        if (status.check_in_progress) {
            statusEl.innerHTML = '<span class="update-status-checking">Checking for updates\u2026</span>';
        } else if (status.update_in_progress) {
            statusEl.innerHTML = '<span class="update-status-checking">Installing update\u2026</span>';
        } else if (status.last_error) {
            statusEl.innerHTML = '<span class="update-status-error">' + escapeHtml(status.last_error) + '</span>';
        } else {
            var ch = status.current_channel || "stable";
            var avail = (status.available && status.available[ch]) ? status.available[ch] : null;
            var installBtn = document.getElementById("btn-apply-update");

            if (avail && avail.is_newer) {
                statusEl.innerHTML =
                    '<span class="update-status-available">Update available: '
                    + escapeHtml(avail.version) + '</span>';
                if (installBtn) {
                    installBtn.style.display = "";
                    installBtn.textContent = "Install " + escapeHtml(avail.version || "Update");
                }
            } else {
                statusEl.innerHTML = '<span class="update-status-uptodate">Up to date</span>';
                if (installBtn) installBtn.style.display = "none";
            }

            // Last check time
            if (status.last_check) {
                var ago = _timeAgo(status.last_check);
                statusEl.innerHTML += ' <span style="font-size:0.75rem;color:var(--text-muted)">(checked ' + ago + ')</span>';
            }
        }

        // ── Available versions list ─────────────────────────────────
        _renderAvailableVersions(status);

        // ── Check interval hint ─────────────────────────────────────
        var intervalHint = document.getElementById("update-check-interval-hint");
        if (intervalHint) {
            var hours = (status.check_interval_hours) ? status.check_interval_hours : 6;
            intervalHint.textContent = "Checks every " + hours + " hours";
        }
    }

    /** Update the channel description text based on selection. */
    function _updateChannelDesc(channel) {
        var el = document.getElementById("update-channel-desc");
        if (!el) return;
        var descs = {
            "stable": "Stable releases are thoroughly tested and recommended for most users.",
            "beta": "Beta releases include new features ready for wider testing. May have minor issues."
        };
        el.textContent = descs[channel] || "";
    }

    /** Render the list of available versions across all channels. */
    function _renderAvailableVersions(status) {
        var list = document.getElementById("update-available-list");
        if (!list) return;

        var avail = status.available;
        if (!avail || Object.keys(avail).length === 0) {
            list.innerHTML = '<span style="color:var(--text-muted);font-size:0.82rem">Check for updates to see versions</span>';
            return;
        }
        var html = "";
        var channels = ["stable", "beta"];
        var currentCh = status.current_channel || "stable";

        channels.forEach(function (ch) {
            var info = avail[ch];
            if (!info) return;
            var isCurrent = ch === currentCh;
            var badge = isCurrent ? ' <span style="font-size:0.7rem;background:var(--primary);color:#fff;padding:1px 5px;border-radius:3px;vertical-align:middle">current</span>' : '';
            var newerBadge = info.is_newer ? ' <span style="font-size:0.7rem;background:#f0a030;color:#000;padding:1px 5px;border-radius:3px;vertical-align:middle">newer</span>' : '';

            html += '<div class="update-available-item">'
                + '<span style="font-weight:600;text-transform:capitalize">' + escapeHtml(ch) + '</span>'
                + '<span>' + escapeHtml(info.version) + badge + newerBadge + '</span>'
                + '</div>';
        });

        list.innerHTML = html;
    }

    /** Format a duration from an ISO timestamp to a human-readable "X ago" string. */
    function _timeAgo(isoStr) {
        var then = Date.parse(isoStr);
        if (isNaN(then)) return "";
        var seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
        if (seconds < 60) return seconds + "s ago";
        if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
        if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
        return Math.floor(seconds / 86400) + "d ago";
    }

    /** Wire up the update control buttons (one-time binding). */
    function bindUpdateControls() {
        if (_updateBound) return;
        _updateBound = true;

        // ── Channel selector ──────────────────────────────────────
        var channelSel = document.getElementById("cfg-update-channel");
        channelSel?.addEventListener("change", async function () {
            var ch = this.value;
            _updateChannelDesc(ch);
            var result = await apiPut("/updates/channel", { channel: ch });
            if (result && result.status === "ok") {
                showToast("Switched to " + ch + " channel", "success");
                // Trigger a check on the new channel
                await apiPost("/updates/check");
                loadUpdateStatus();
            } else {
                showToast("Failed to switch channel", "error");
            }
        });

        // ── Check for Updates button ──────────────────────────────
        var checkBtn = document.getElementById("btn-check-updates");
        checkBtn?.addEventListener("click", async function () {
            checkBtn.disabled = true;
            checkBtn.textContent = "Checking\u2026";
            var statusEl = document.getElementById("update-status");
            if (statusEl) statusEl.innerHTML =
                '<span class="update-status-checking">Checking for updates\u2026</span>';

            var result = await apiPost("/updates/check");
            if (result && result.status === "ok") {
                // Poll for results (the check runs in a background thread)
                var attempts = 0;
                var maxAttempts = 30; // 15 seconds max
                var pollInterval = setInterval(async function () {
                    attempts++;
                    var status = await apiGet("/updates/status");
                    if (!status || !status.check_in_progress || attempts >= maxAttempts) {
                        clearInterval(pollInterval);
                        checkBtn.disabled = false;
                        checkBtn.textContent = "Check for Updates";
                        loadUpdateStatus();
                    }
                }, 500);
            } else {
                checkBtn.disabled = false;
                checkBtn.textContent = "Check for Updates";
                loadUpdateStatus();
            }
        });

        // ── Install Update button ─────────────────────────────────
        var installBtn = document.getElementById("btn-apply-update");
        installBtn?.addEventListener("click", async function () {
            var ch = document.getElementById("cfg-update-channel")?.value || "stable";
            if (!confirm("Install the latest update from the " + ch + " channel? Services will restart.")) {
                return;
            }

            installBtn.disabled = true;
            installBtn.textContent = "Installing\u2026";

            // Show progress bar
            var progressDiv = document.getElementById("update-progress");
            var progressBar = document.getElementById("update-progress-bar");
            var progressPhase = document.getElementById("update-progress-phase");
            if (progressDiv) progressDiv.style.display = "";
            if (progressBar) progressBar.style.width = "30%";
            if (progressPhase) progressPhase.textContent = "Stopping services\u2026";

            // Simulate progress steps (actual update is synchronous)
            var steps = [
                { pct: 30, text: "Stopping services\u2026" },
                { pct: 50, text: "Fetching updates\u2026" },
                { pct: 70, text: "Applying update\u2026" },
                { pct: 85, text: "Reinstalling packages\u2026" },
                { pct: 95, text: "Restarting services\u2026" },
            ];

            var stepIdx = 0;
            var progressTimer = setInterval(function () {
                if (stepIdx < steps.length) {
                    var s = steps[stepIdx];
                    if (progressBar) progressBar.style.width = s.pct + "%";
                    if (progressPhase) progressPhase.textContent = s.text;
                    stepIdx++;
                }
            }, 1500);

            // Fire the update
            try {
                var result = await apiPost("/updates/apply", { channel: ch });
                clearInterval(progressTimer);
                if (result && result.status === "ok") {
                    if (progressBar) progressBar.style.width = "100%";
                    if (progressPhase) progressPhase.textContent = "Complete — reconnecting\u2026";
                    showToast("Update applied! Services are restarting.", "success", 8000);
                } else {
                    if (progressDiv) progressDiv.style.display = "none";
                    showToast((result && result.message) || "Update failed", "error", 5000);
                    installBtn.disabled = false;
                    installBtn.textContent = "Install Update";
                }
            } catch (_) {
                clearInterval(progressTimer);
                // Expected during restart — the request will fail
                if (progressDiv) progressDiv.style.display = "none";
                showToast("Services restarting — the page will reconnect shortly.", "info", 8000);
            }
        });

        // ── Auto-check toggle ─────────────────────────────────────
        var autoCheck = document.getElementById("cfg-update-auto-check");
        autoCheck?.addEventListener("change", async function () {
            var result = await apiPut("/config/update", { auto_check: this.checked });
            if (result) {
                showToast(this.checked ? "Auto-check enabled" : "Auto-check disabled", "info");
            }
        });
    }

    // -- Init ----------------------------------------------------------------

    var hash = location.hash.substring(1);
    var validPages = ["dashboard", "media", "settings", "sync", "network", "advanced"];
    var startPage = validPages.indexOf(hash) >= 0 ? hash : "dashboard";
    navigateTo(startPage);
})();
