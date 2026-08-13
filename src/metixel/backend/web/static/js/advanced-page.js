// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Advanced page module. Display/schedule/NTP/timezone settings, keyboard mapping, system info and the system power/restart actions.
 */

import {
    apiGet,
    apiPost,
    apiPut,
    sanitizeInt,
    setChecked,
    setStat,
    setValue,
    showToast,
    updatePowerButton
} from "./core.js";

import { refreshLogs } from "./logs-page.js";
import { loadUpdateStatus, bindUpdateControls } from "./updates-page.js";

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

    // -- Keyboard Input Mapping ----------------------------------------------

    var _kbdCommands = ["next", "prev", "pause", "resume", "toggle_pause", "screen_on", "screen_off"];

    async function _loadKeyboardMap() {
        try {
            var resp = await fetch("/api/config/input/keyboard/map?_=" + Date.now());
            if (!resp.ok) return;
            var data = await resp.json();
            var tbody = document.getElementById("kbd-map-body");
            if (!tbody) return;

            var map = data.map || {};
            tbody.innerHTML = "";
            _kbdCommands.forEach(function (cmd) {
                var keys = map[cmd] || [];
                var keyNames = keys.map(function (k) {
                    return '<span style="display:inline-block;background:var(--bg-hover);padding:0.15rem 0.4rem;border-radius:3px;margin:0.1rem;font-size:0.8rem;font-family:monospace">' +
                        (k.name || ("Key " + k.code)) + '</span>';
                }).join("") || '<span style="color:var(--text-muted);font-size:0.8rem">—</span>';

                var row = document.createElement("tr");
                row.style.borderBottom = "1px solid var(--border)";
                row.innerHTML =
                    '<td style="padding:0.4rem 0.5rem;font-size:0.9rem">' + cmd + '</td>' +
                    '<td style="padding:0.4rem 0.5rem" id="kbd-keys-' + cmd + '">' + keyNames + '</td>' +
                    '<td style="padding:0.4rem 0.5rem;text-align:right">' +
                    '<button class="btn--sm btn--secondary kbd-learn-btn" data-cmd="' + cmd + '">Learn</button>' +
                    '<button class="btn--sm btn--danger kbd-clear-btn" data-cmd="' + cmd + '" style="margin-left:0.2rem;display:' + (keys.length ? '' : 'none') + '">✕</button>' +
                    '</td>';
                tbody.appendChild(row);
            });
        } catch (e) { /* API not available */ }
    }

    var _kbdPollTimer = null;
    var _kbdLearningCmd = null;

    function _bindKeyboardLearn() {
        document.getElementById("kbd-map-body")?.addEventListener("click", async function (e) {
            var learnBtn = e.target.closest(".kbd-learn-btn");
            var clearBtn = e.target.closest(".kbd-clear-btn");
            if (!learnBtn && !clearBtn) return;

            var cmd = (learnBtn || clearBtn).dataset.cmd;

            if (clearBtn) {
                // Clear all mappings for this command
                await apiPost("/config/input/keyboard/learn", { cmd: "clear", target: cmd });
                _loadKeyboardMap();
                return;
            }

            // Start learn mode
            var status = document.getElementById("kbd-learn-status");
            if (status) {
                status.style.display = "";
                status.style.background = "rgba(59,130,246,0.15)";
                status.style.color = "var(--primary)";
                status.textContent = 'Listening for key for "' + cmd + '" — press a key on your remote…';
            }

            _kbdLearningCmd = cmd;
            await apiPost("/config/input/keyboard/learn", { cmd: "start", target: cmd });

            // Poll for result
            if (_kbdPollTimer) clearInterval(_kbdPollTimer);
            _kbdPollTimer = setInterval(async function () {
                var result = await apiPost("/config/input/keyboard/learn", { cmd: "check" });
                if (!result) return;

                if (result.status === "learned") {
                    clearInterval(_kbdPollTimer);
                    _kbdPollTimer = null;
                    if (status) {
                        status.style.background = "rgba(34,197,94,0.15)";
                        status.style.color = "var(--text)";
                        status.textContent = 'Mapped ' + result.name + ' → ' + result.command;
                        setTimeout(function () { status.style.display = "none"; }, 3000);
                    }
                    _loadKeyboardMap();
                } else if (result.status === "cancelled" || result.error) {
                    clearInterval(_kbdPollTimer);
                    _kbdPollTimer = null;
                    if (status) {
                        status.style.display = "none";
                    }
                }
            }, 300);
        });
    }

    var _advancedLogTimer = null;

    // -- Advanced ------------------------------------------------------------

    var _advancedBound = false;

    async function loadAdvanced() {
        const config = await apiGet("/config");
        if (!config) return;

        // Start log polling (moved from Dashboard)
        if (_advancedLogTimer) {
            clearTimeout(_advancedLogTimer);
            _advancedLogTimer = null;
        }
        await refreshLogs();
        function _scheduleLogPoll() {
            _advancedLogTimer = setTimeout(async function () {
                if (document.getElementById("page-advanced").classList.contains("active")) {
                    await refreshLogs();
                    _scheduleLogPoll();
                } else {
                    _advancedLogTimer = null;
                }
            }, 3000);
        }
        _scheduleLogPoll();

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
        setValue("cfg-log-level", sys.log_level || "NONE");
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
            setStat("info-app-version", "v" + (info.app_version || "--"));
            setStat("info-pi-model", info.pi_model || "--");
            setStat("info-os-release", info.os_release || "--");
            setStat("info-kernel", info.kernel || "--");
            setStat("info-python", info.python_version || "--");
            setStat("info-pi3d", info.pi3d_version || "--");
            setStat("info-gpu-mem", info.gpu_memory || "--");
            setStat("info-drm-driver", info.drm_driver || "--");
            setStat("info-hostname", info.hostname || "--");
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

            // Display power toggle — reads actual state from health endpoint
            var powerBtn = document.getElementById("btn-display-power");
            powerBtn?.addEventListener("click", async () => {
                var health = await apiGet("/config/health");
                var currentlyOn = health ? health.display_on !== false : true;
                var newState = !currentlyOn;
                await apiPost("/config/control", { cmd: newState ? "screen_on" : "screen_off" });
                updatePowerButton(newState);
                showToast(newState ? "Display turned on" : "Display turned off", "info");
            });
            // Initial state from health poll
            (async function _initPowerBtn() {
                var health = await apiGet("/config/health");
                updatePowerButton(health ? health.display_on !== false : true);
            })();

            document.getElementById("btn-save-system")?.addEventListener("click", async () => {
                var logLevel = document.getElementById("cfg-log-level").value;
                var quietBoot = document.getElementById("cfg-quiet-boot").checked;
                var sysResult = await apiPut("/config/system", {
                    log_level: logLevel,
                    cache_dir: document.getElementById("cfg-cache-dir").value,
                    quiet_boot: quietBoot,
                });
                var logResult = await apiPost("/logs/level", { level: logLevel });
                // Apply quiet boot change (requires sudo) — always run so the
                // idempotent script is applied.  Show "Applying…" on the
                // save button while the script runs (may take a few seconds).
                var qbResult = null;
                var btnSave = document.getElementById("btn-save-system");
                var originalLabel = btnSave ? btnSave.textContent : "Save System Settings";
                if (btnSave) {
                    btnSave.textContent = "Applying…";
                    btnSave.disabled = true;
                }
                try {
                    qbResult = await apiPost("/config/quiet-boot", { enabled: quietBoot });
                } finally {
                    if (btnSave) {
                        btnSave.textContent = originalLabel;
                        btnSave.disabled = false;
                    }
                }
                if (sysResult && logResult && logResult.status === "ok") {
                    var msg = "System settings saved! File log level: " + logLevel;
                    if (qbResult && qbResult.status === "ok") {
                        msg += " | Quiet boot " + (quietBoot ? "enabled" : "disabled") + ".";
                    } else if (qbResult && qbResult.status === "error") {
                        msg += " | Quiet boot failed: " + (qbResult.message || "script error");
                        showToast(msg, "error");
                        return;
                    } else if (!qbResult) {
                        msg += " | Quiet boot unreachable — check server logs.";
                    }
                    showToast(msg, "success");
                } else if (sysResult) {
                    showToast("System settings saved (log level apply failed — check server)", "info");
                } else {
                    showToast("Failed to save system settings", "error");
                }
            });

            // Time settings (NTP + timezone are saved immediately; NTP servers
            // are saved here together so the user can edit all three at once.)
            document.getElementById("btn-save-time")?.addEventListener("click", async () => {
                var ntpEnabled = document.getElementById("cfg-ntp-enabled").checked;
                var ntpServers = [
                    document.getElementById("cfg-ntp-server-1").value.trim(),
                    document.getElementById("cfg-ntp-server-2").value.trim(),
                    document.getElementById("cfg-ntp-server-3").value.trim(),
                ].filter(function(s) { return s !== ""; });
                // Persist config
                await apiPut("/config/system", {
                    ntp_enabled: ntpEnabled,
                    ntp_servers: ntpServers,
                });
                // Apply NTP settings via systemd-timesyncd
                if (ntpEnabled) {
                    await apiPost("/config/ntp", {
                        enabled: true,
                        servers: ntpServers,
                    });
                } else {
                    await apiPost("/config/ntp", { enabled: false });
                }
                showToast("Time settings saved" + (ntpEnabled ? " — NTP enabled" : ""), "success");
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

            // ── Keyboard Input ─────────────────────────────────────
            _loadKeyboardMap();
            _bindKeyboardLearn();

            // ── Update Controls ──────────────────────────────────────
            bindUpdateControls();
        }
    }

export { loadAdvanced };
