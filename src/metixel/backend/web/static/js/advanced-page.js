// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Advanced page module. Display/schedule/NTP/timezone settings, keyboard mapping, system info and the system power/restart actions.
 */

import {
    apiGet,
    apiPost,
    apiPut,
    confirmDialog,
    sanitizeInt,
    setButtonBusy,
    setChecked,
    setStat,
    setValue,
    showToast,
    updatePowerButton
} from "./core.js";

import { refreshLogs } from "./logs-page.js";
import { loadUpdateStatus, bindUpdateControls } from "./updates-page.js";
import { loadDdcControls, bindDdcControls } from "./ddc-controls.js";

    // -- UI Helpers ---------------------------------------------------------

    /**
     * Enable or disable the resolution override fields based on auto-detect.
     * @param {boolean} isAuto - Whether auto-detect is enabled.
     */
    function toggleResolutionFields(isAuto) {
        var fields = document.getElementById("display-resolution-fields");
        if (fields) {
            var controls = fields.querySelectorAll("select, input");
            controls.forEach(function (control) {
                control.disabled = isAuto;
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

    function toggleMqttFields(enabled) {
        var fields = document.getElementById("mqtt-fields");
        if (fields) fields.style.display = enabled ? "block" : "none";
        // The broker status pill only makes sense while MQTT is enabled.
        var status = document.getElementById("mqtt-status");
        if (status) status.style.display = enabled ? "" : "none";
    }

    /** Map an MQTT status string to a pill class + label. */
    function _mqttStatusStyle(status) {
        switch (status) {
            case "connected":
                return { cls: "mqtt-status--ok", label: "Connected" };
            case "auth_error":
                return { cls: "mqtt-status--err", label: "Auth error" };
            case "not_responding":
                return { cls: "mqtt-status--warn", label: "Not responding" };
            case "connecting":
                return { cls: "mqtt-status--warn", label: "Connecting…" };
            case "disabled":
                return { cls: "mqtt-status--disabled", label: "Disabled" };
            default:
                return { cls: "mqtt-status--disabled", label: "Unknown" };
        }
    }

    async function _refreshMqttStatus() {
        var pill = document.getElementById("mqtt-status-pill");
        var detail = document.getElementById("mqtt-status-detail");
        if (!pill || !detail) return;

        var data = await apiGet("/system/mqtt-status");
        if (!data) {
            pill.className = "mqtt-status-pill mqtt-status--disabled";
            pill.textContent = "Unknown";
            detail.textContent = "Status endpoint unreachable";
            return;
        }

        var style = _mqttStatusStyle(data.status);
        pill.className = "mqtt-status-pill " + style.cls;
        pill.textContent = style.label;

        var parts = [];
        if (data.broker) parts.push(data.broker + ":" + data.port);
        if (data.error) parts.push(data.error);
        detail.textContent = parts.join(" · ");
    }

    /** @type {number|null} */

    var _clockTimer = null;
    var _mqttStatusTimer = null;

    async function _refreshServerClock() {
        var el = document.getElementById("server-clock");
        if (!el) return;
        try {
            var data = await apiGet("/time");
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
            var data = await apiGet("/time/timezones");
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
            var resp = await fetch("/api/input/keyboard/map?_=" + Date.now());
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
                await apiPost("/input/keyboard/learn", { cmd: "clear", target: cmd });
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
            await apiPost("/input/keyboard/learn", { cmd: "start", target: cmd });

            // Poll for result
            if (_kbdPollTimer) clearInterval(_kbdPollTimer);
            _kbdPollTimer = setInterval(async function () {
                var result = await apiPost("/input/keyboard/learn", { cmd: "check" });
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
        setValue("cfg-fps-limit", d.fps_limit || 30);
        setValue("cfg-display-rotation", d.rotation || 0);
        setChecked("cfg-schedule-enabled", d.schedule_enabled === true);
        setValue("cfg-schedule-on", d.schedule_on_time || "07:00");
        setValue("cfg-schedule-off", d.schedule_off_time || "22:00");
        toggleScheduleFields(d.schedule_enabled === true);
        toggleResolutionFields(isAuto);

        // Populate the resolution+refresh dropdown from the supported-modes
        // endpoint (only modes the monitor and Pi mutually support).
        apiGet("/health/display/modes").then(function (data) {
            var sel = document.getElementById("cfg-display-resolution");
            if (!sel) return;
            var modes = (data && data.modes) || [];
            modes.forEach(function (m) {
                var opt = document.createElement("option");
                opt.value = m.width + "x" + m.height + "@" + (m.refresh || 0);
                var label = m.width + " × " + m.height;
                if (m.refresh) label += " @ " + m.refresh + " Hz";
                if (m.preferred) label += " (native)";
                opt.textContent = label;
                sel.appendChild(opt);
            });
            // Select the configured resolution+refresh (or auto).
            var current = "0x0@0";
            if (d.width > 0 && d.height > 0) {
                current = d.width + "x" + d.height + "@" + (d.refresh_rate || 0);
            }
            setValue("cfg-display-resolution", current);
        });

        // MQTT / Home Assistant Settings
        const m = config.mqtt || {};
        setChecked("cfg-mqtt-enabled", m.enabled === true);
        setValue("cfg-mqtt-device-id", m.device_id || "");
        setValue("cfg-mqtt-broker", m.broker || "localhost");
        setValue("cfg-mqtt-port", m.port || 1883);
        setValue("cfg-mqtt-username", m.username || "");
        setValue("cfg-mqtt-password", m.password || "");
        setChecked("cfg-mqtt-discovery", m.discovery_enabled !== false);
        setValue("cfg-mqtt-discovery-prefix", m.discovery_prefix || "homeassistant");
        toggleMqttFields(m.enabled === true);

        // Refresh the MQTT broker status indicator (and poll while visible).
        _refreshMqttStatus();
        if (_mqttStatusTimer) clearInterval(_mqttStatusTimer);
        _mqttStatusTimer = setInterval(function () {
            if (document.getElementById("page-advanced").classList.contains("active")) {
                _refreshMqttStatus();
            } else {
                clearInterval(_mqttStatusTimer);
                _mqttStatusTimer = null;
            }
        }, 5000);

        // Fetch detected display resolution from the frontend
        apiGet("/health/display/info").then(function (info) {
            if (info && info.width > 0 && info.height > 0) {
                var el = document.getElementById("display-detected-res");
                if (el) {
                    var text = "Detected: " + info.width + " × " + info.height;
                    if (info.refresh_rate) text += " @ " + info.refresh_rate + " Hz";
                    if (info.rotation) text += " · rotated " + info.rotation + "°";
                    if (info.output) text += " · connected via " + info.output;
                    el.textContent = text;
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
        apiGet("/system/info").then(function (info) {
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
                // Parse "WxH@R" from the resolution+refresh dropdown (0x0@0 = auto).
                var val = document.getElementById("cfg-display-resolution").value || "0x0@0";
                var parts = val.split("@");
                var res = (parts[0] || "0x0").split("x");
                var width = isAutoSave ? 0 : sanitizeInt(res[0], 0);
                var height = isAutoSave ? 0 : sanitizeInt(res[1], 0);
                var refresh = isAutoSave ? 0 : sanitizeInt(parts[1], 0);
                var result = await apiPut("/config/display", {
                    width: width,
                    height: height,
                    fps_limit: sanitizeInt(document.getElementById("cfg-fps-limit").value, 30),
                    refresh_rate: refresh,
                    rotation: sanitizeInt(document.getElementById("cfg-display-rotation").value, 0),
                });
                if (result) {
                    showToast("Display settings saved — frontend restarting to apply", "success", 5000);
                } else {
                    showToast("Failed to save display settings", "error");
                }
            });

            // Display Power Save Schedule — saves only the schedule keys.
            document.getElementById("btn-save-schedule")?.addEventListener("click", async () => {
                var result = await apiPut("/config/display", {
                    schedule_enabled: document.getElementById("cfg-schedule-enabled").checked,
                    schedule_on_time: document.getElementById("cfg-schedule-on").value,
                    schedule_off_time: document.getElementById("cfg-schedule-off").value,
                });
                if (result) {
                    showToast("Display power schedule saved!", "success");
                } else {
                    showToast("Failed to save display power schedule", "error");
                }
            });

            // Display power toggle — reads actual state from health endpoint
            var powerBtn = document.getElementById("btn-display-power");
            powerBtn?.addEventListener("click", async () => {
                var health = await apiGet("/health");
                var currentlyOn = health ? health.display_on !== false : true;
                var newState = !currentlyOn;
                await apiPost("/control", { cmd: newState ? "screen_on" : "screen_off" });
                updatePowerButton(newState);
                showToast(newState ? "Display turned on" : "Display turned off", "info");
            });
            // Initial state from health poll
            (async function _initPowerBtn() {
                var health = await apiGet("/health");
                updatePowerButton(health ? health.display_on !== false : true);
            })();

            // MQTT / Home Assistant settings
            document.getElementById("cfg-mqtt-enabled")?.addEventListener("change", function () {
                toggleMqttFields(this.checked);
            });

            document.getElementById("btn-save-mqtt")?.addEventListener("click", async () => {
                var result = await apiPut("/config/mqtt", {
                    enabled: document.getElementById("cfg-mqtt-enabled").checked,
                    device_id: document.getElementById("cfg-mqtt-device-id").value.trim(),
                    broker: document.getElementById("cfg-mqtt-broker").value.trim(),
                    port: sanitizeInt(document.getElementById("cfg-mqtt-port").value, 1883),
                    username: document.getElementById("cfg-mqtt-username").value,
                    password: document.getElementById("cfg-mqtt-password").value,
                    discovery_enabled: document.getElementById("cfg-mqtt-discovery").checked,
                    discovery_prefix: document.getElementById("cfg-mqtt-discovery-prefix").value.trim() || "homeassistant",
                });
                if (result) {
                    showToast("MQTT settings saved — restart services to apply", "success", 5000);
                } else {
                    showToast("Failed to save MQTT settings", "error");
                }
            });

            document.getElementById("btn-restart-mqtt")?.addEventListener("click", async () => {
                var restore = setButtonBusy(document.getElementById("btn-restart-mqtt"), "Restarting…");
                try {
                    var r = await apiPost("/system/restart");
                    showToast(r && r.message ? r.message : "Restarting services…", "info", 4000);
                    // The backend restarts in ~2s; show a hopeful status meanwhile.
                    setTimeout(_refreshMqttStatus, 3000);
                } finally {
                    restore();
                }
            });

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
                var restoreSave = setButtonBusy(document.getElementById("btn-save-system"), "Applying…");
                try {
                    qbResult = await apiPost("/system/quiet-boot", { enabled: quietBoot });
                } finally {
                    restoreSave();
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
                    await apiPost("/time/ntp", {
                        enabled: true,
                        servers: ntpServers,
                    });
                } else {
                    await apiPost("/time/ntp", { enabled: false });
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
                if (!(await confirmDialog("Clear all cached files and restart services?\n\n"
                           + "This deletes transcoded videos, thumbnails, and processed images, "
                           + "then restarts the backend and frontend. "
                           + "Playback will be interrupted for ~10 seconds.",
                           { danger: true, okText: "Clear cache" }))) {
                    return;
                }
                var restoreCache = setButtonBusy(clearCacheBtn, "Clearing…");
                var result = await apiPost("/media/cache/clear");
                if (result && result.status === "ok") {
                    showToast(
                        "Cache cleared: " + result.deleted_files + " files, " + result.freed_mb + " MB freed. Restarting services…",
                        "success", 5000
                    );
                    // Restart services to prevent stale cached-file errors
                    try {
                        await apiPost("/system/restart");
                    } catch (_) {
                        // Expected — the backend is restarting, so the request may fail
                    }
                } else {
                    restoreCache();
                    showToast("Failed to clear image cache", "error");
                }
            });

            // Restart all services
            var restartBtn = document.getElementById("btn-restart-services");
            restartBtn?.addEventListener("click", async () => {
                if (!(await confirmDialog("Restart all Metixel services? Playback will be interrupted for ~10 seconds.", { okText: "Restart" }))) {
                    return;
                }
                setButtonBusy(restartBtn, "Restarting…");
                showToast("Restarting services…", "info", 5000);
                try {
                    await apiPost("/system/restart");
                } catch (_) {
                    // Expected — the backend is restarting
                }
                // The button will re-enable when the page reloads after restart
            });

            // Reboot system
            var rebootBtn = document.getElementById("btn-reboot-system");
            rebootBtn?.addEventListener("click", async () => {
                if (!(await confirmDialog("Reboot the entire system? The photo frame will be unavailable for ~60 seconds.", { danger: true, okText: "Reboot" }))) {
                    return;
                }
                setButtonBusy(rebootBtn, "Rebooting…");
                showToast("Rebooting system…", "info", 5000);
                try {
                    await apiPost("/system/reboot");
                } catch (_) {
                    // Expected — the system is going down
                }
            });

            // Shutdown system
            var shutdownBtn = document.getElementById("btn-shutdown-system");
            shutdownBtn?.addEventListener("click", async () => {
                if (!(await confirmDialog("Shut down the entire system? You will need to physically power-cycle the Pi to turn it back on.", { danger: true, okText: "Shut down" }))) {
                    return;
                }
                setButtonBusy(shutdownBtn, "Shutting down…");
                showToast("Shutting down system…", "info", 5000);
                try {
                    await apiPost("/system/shutdown");
                } catch (_) {
                    // Expected — the system is going down
                }
            });

            // ── Keyboard Input ─────────────────────────────────────
            _loadKeyboardMap();
            _bindKeyboardLearn();

            // ── Update Controls ──────────────────────────────────────
            bindUpdateControls();

            // ── DDC/CI Monitor Control ───────────────────────────────
            bindDdcControls();
        }

        await loadDdcControls();
    }

export { loadAdvanced };
