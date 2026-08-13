// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Dashboard / home page module. Renders health stats, sparklines, current media, processing and sync status, plus the first-run welcome banner and persistent messages.
 */

import {
    apiGet,
    apiPost,
    apiPut,
    escapeHtml,
    setStat,
    showToast,
    updatePowerButton
} from "./core.js";

    // -- Sparkline ring buffers (last 20 samples = 60 seconds at 3s poll) --
    var _sparkBufs = { cpu: [], mem: [], swap: [], temp: [] };
    var _SPARK_MAX = 20;

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

    async function refreshDashboard() {
        const health = await apiGet("/config/health");
        if (!health) return;

        updatePowerButton(health.display_on !== false);

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
        setStat("stat-cpu-val", cpuPct + "%");
        _sparkBufs.cpu.push(cpuPct);
        if (_sparkBufs.cpu.length > _SPARK_MAX) _sparkBufs.cpu.shift();
        _drawSparkline("stat-cpu-canvas", _sparkBufs.cpu, 100, "#3b82f6");

        // -- Memory tile --
        var memPct = health.memory_percent != null ? health.memory_percent : 0;
        var memUsed = health.memory_used_gb != null ? health.memory_used_gb : 0;
        var memTotal = health.memory_total_gb != null ? health.memory_total_gb : 0;
        setStat("stat-mem-val", memPct + "%  (" + memUsed.toFixed(1) + " / " + memTotal.toFixed(1) + " GB)");
        _sparkBufs.mem.push(memPct);
        if (_sparkBufs.mem.length > _SPARK_MAX) _sparkBufs.mem.shift();
        _drawSparkline("stat-mem-canvas", _sparkBufs.mem, 100, "#8b5cf6");

        // -- Swap tile --
        var swapPct = health.swap_percent != null ? health.swap_percent : 0;
        var swapUsed = health.swap_used_gb != null ? health.swap_used_gb : 0;
        var swapTotal = health.swap_total_gb != null ? health.swap_total_gb : 0;
        setStat("stat-swap-val", swapPct + "%  (" + swapUsed.toFixed(1) + " / " + swapTotal.toFixed(1) + " GB)");
        _sparkBufs.swap.push(swapPct);
        if (_sparkBufs.swap.length > _SPARK_MAX) _sparkBufs.swap.shift();
        _drawSparkline("stat-swap-canvas", _sparkBufs.swap, Math.max(swapTotal > 0 ? 100 : 0, 1), "#f59e0b");

        // -- CPU Temperature tile --
        var cpuTemp = health.cpu_temp_c != null ? health.cpu_temp_c : 0;
        setStat("stat-temp-val", cpuTemp.toFixed(1) + "°C");
        _sparkBufs.temp.push(cpuTemp);
        if (_sparkBufs.temp.length > _SPARK_MAX) _sparkBufs.temp.shift();
        // Scale: 0-85°C (Pi throttles at 85)
        _drawSparkline("stat-temp-canvas", _sparkBufs.temp, 85, "#ef4444");

        // -- Existing text-only tiles --
        setStat("stat-uptime-val", uptimeH + "h " + uptimeM + "m");
        setStat("stat-disk-val", health.disk_used_gb + " / " + health.disk_total_gb + " GB");
        setStat("stat-disk-pct-val", health.disk_used_percent + "%");
        setStat("stat-cache-val", cacheLabel);
        setStat("stat-media-val", mediaSizeLabel);
        setStat("stat-playlist-val", imgCount + " photos, " + vidCount + " videos");

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
                pauseBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:1.2rem;vertical-align:middle">play_arrow</span> Resume';
            } else if (!isPaused && pauseBtn.innerHTML.indexOf("Pause") !== -1) {
                pauseBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:1.2rem;vertical-align:middle">pause</span> Pause';
            }
        }

        var uptimeEl = document.getElementById("uptime");
        if (uptimeEl) {
            uptimeEl.textContent = " — Uptime " + uptimeH + "h " + uptimeM + "m";
        }
    }

    // -- Dashboard ----------------------------------------------------------

    var _dashboardBound = false;
    var _dashboardTimer = null;

    async function loadDashboard() {
        // Clear any existing polling timer
        if (_dashboardTimer) {
            clearTimeout(_dashboardTimer);
            _dashboardTimer = null;
        }

        // Initial load
        await refreshDashboard();
        await refreshProcessing();
        _refreshDashSyncStatus();

        // Show welcome banner on first run
        _checkWelcomeBanner();

        // Poll every 3 seconds — use setTimeout chain to prevent
        // queue buildup when the browser tab is backgrounded on mobile.
        function _scheduleDashboardPoll() {
            _dashboardTimer = setTimeout(async function () {
                if (document.getElementById("page-dashboard").classList.contains("active")) {
                    await refreshDashboard();
                    await refreshProcessing();
                    _refreshDashSyncStatus();
                    _scheduleDashboardPoll();
                } else {
                    _dashboardTimer = null;
                }
            }, 3000);
        }
        _scheduleDashboardPoll();

        // Quick controls — bind once
        if (!_dashboardBound) {
            _dashboardBound = true;
            document.getElementById("btn-next")?.addEventListener("click", async () => {
                await apiPost("/config/control", { cmd: "next" });
                // Next implicitly resumes — sync the button
                var pauseBtn = document.getElementById("btn-pause-toggle");
                if (pauseBtn) pauseBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:1.2rem;vertical-align:middle">pause</span> Pause';
                showToast("Skipped to next", "info");
            });
            document.getElementById("btn-prev")?.addEventListener("click", async () => {
                await apiPost("/config/control", { cmd: "prev" });
                // Prev implicitly resumes — sync the button
                var pauseBtn = document.getElementById("btn-pause-toggle");
                if (pauseBtn) pauseBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:1.2rem;vertical-align:middle">pause</span> Pause';
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
                    pauseBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:1.2rem;vertical-align:middle">pause</span> Pause';
                    showToast("Slideshow resumed", "info");
                } else {
                    await apiPost("/config/control", { cmd: "pause" });
                    pauseBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:1.2rem;vertical-align:middle">play_arrow</span> Resume';
                    showToast("Slideshow paused", "info");
                }
            });

        }

        // Load persistent messages (shown once per dashboard visit)
        _loadPersistentMessages();
    }

    // -- First-Run Welcome Banner --------------------------------------------

    var _welcomeBound = false;

    async function _checkWelcomeBanner() {
        var banner = document.getElementById("welcome-banner");
        if (!banner) return;

        // Fetch config to check the first_run flag
        var config = await apiGet("/config/system");
        if (!config || !config.first_run) {
            banner.style.display = "none";
            return;
        }

        // Show the banner
        banner.style.display = "";

        // Bind dismiss button once
        if (!_welcomeBound) {
            _welcomeBound = true;
            var dismissBtn = document.getElementById("btn-welcome-dismiss");
            if (dismissBtn) {
                dismissBtn.addEventListener("click", async function () {
                    dismissBtn.disabled = true;
                    var result = await apiPut("/config/system", { first_run: false });
                    if (result && result.status === "ok") {
                        banner.style.display = "none";
                        showToast("Welcome dismissed!", "success");
                    } else {
                        dismissBtn.disabled = false;
                        showToast("Failed to dismiss — try again", "error");
                    }
                });
            }
        }
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

    // -- Background Processing Status ---------------------------------------

    /**
     * Poll the backend's per-phase processing status and update the
     * three persistent progress bars (scanning, optimising, transcoding).
     * Each bar retains its last position even when the active phase
     * switches, so the user sees all queue states at once.
     */

    async function refreshProcessing() {
        var status = await apiGet("/config/processing-status");
        if (!status) return;

        // Check whether image/video optimisation are enabled so
        // irrelevant progress bars can be hidden.
        var imgCfg = await apiGet("/config/image");
        var vidCfg = await apiGet("/config/video");
        var imgOptEnabled = imgCfg ? imgCfg.optimisation_enabled !== false : true;
        var vidTranscodeEnabled = vidCfg ? vidCfg.transcoding_enabled !== false : true;

        var phases = status.phases || {};
        var active = status.active || null;
        var idleEl = document.getElementById("processing-idle");
        var anyActive = false;

        ["scanning", "optimising_images", "transcoding"].forEach(function (phase) {
            var container = document.querySelector('.proc-phase[data-phase="' + phase + '"]');
            if (!container) return;

            // Hide bars for disabled features
            if (phase === "optimising_images" && !imgOptEnabled) {
                container.style.display = "none";
                return;
            }
            if (phase === "transcoding" && !vidTranscodeEnabled) {
                container.style.display = "none";
                return;
            }
            container.style.display = "";

            var pctEl = container.querySelector(".proc-pct");
            var fillEl = container.querySelector(".proc-fill");
            var info = phases[phase] || {};
            var total = info.total || 0;
            var processed = info.processed || 0;

            if (total > 0 && processed > 0) {
                anyActive = true;
                var pct = Math.min(100, Math.round((processed / total) * 100));
                if (pctEl) pctEl.textContent = processed + "/" + total + " (" + pct + "%)";
                if (fillEl) fillEl.style.width = pct + "%";
            } else if (phase === active && total > 0 && processed === 0) {
                // Phase is active but no progress yet — show as active
                anyActive = true;
                if (pctEl) pctEl.textContent = "0/" + total;
                if (fillEl) fillEl.style.width = "0%";
            } else {
                // No data for this phase — keep last-known state
                if (pctEl && !pctEl.textContent || pctEl.textContent === "—") {
                    pctEl.textContent = "—";
                }
            }
        });

        // Show/hide the idle message
        if (idleEl) {
            idleEl.style.display = anyActive ? "none" : "";
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
            textEl.textContent = ago + " — Success";
            textEl.style.color = "var(--text)";
        } else {
            var hasCancel = s.errors && s.errors.some(function (e) { return e.indexOf("Cancelled") >= 0; });
            textEl.textContent = ago + (hasCancel ? " — Cancelled" : " — Errors");
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

export { loadDashboard };
