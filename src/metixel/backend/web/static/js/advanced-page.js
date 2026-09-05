// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Advanced page module. Display/schedule/timezone settings, keyboard mapping, system info and the system power/restart actions.
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
    showToast
} from "./core.js";

import { refreshLogs } from "./logs-page.js";
import { loadUpdateStatus, bindUpdateControls } from "./updates-page.js";
import { loadDdcControls, bindDdcControls } from "./ddc-controls.js";

    // -- UI Helpers ---------------------------------------------------------

    /**
     * Show the SD card wear warning only when file logging is enabled
     * (any level other than NONE).
     * @param {string} logLevel - The selected file log level.
     */
    function toggleSdCardWarning(logLevel) {
        var warning = document.getElementById("sd-card-warning");
        if (warning) warning.classList.toggle("hidden", (logLevel || "NONE").toUpperCase() === "NONE");
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
    var _mqttStatusTimer = null;

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
                if (document.getElementById("page-system").classList.contains("active")) {
                    await refreshLogs();
                    _scheduleLogPoll();
                } else {
                    _advancedLogTimer = null;
                }
            }, 3000);
        }
        _scheduleLogPoll();

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
            if (document.getElementById("page-system").classList.contains("active")) {
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
        toggleSdCardWarning(sys.log_level || "NONE");
        setValue("cfg-cache-dir", sys.cache_dir || "cache/");
        setChecked("cfg-quiet-boot", sys.quiet_boot === true);

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

        // Security — web session timeout (System page).
        setValue("cfg-web-session-timeout", web.session_timeout_minutes != null ? web.session_timeout_minutes : 30);
        if (!_advancedBound) {
            _advancedBound = true;

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

            document.getElementById("cfg-log-level")?.addEventListener("change", function () {
                toggleSdCardWarning(this.value);
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

            // ── Security card (System page) ────────────────────────────────

            // Web dashboard password (set/change/clear) + session timeout.
            document.getElementById("btn-save-web-password")?.addEventListener("click", async () => {
                var pw = document.getElementById("cfg-web-password").value;
                var confirm = document.getElementById("cfg-web-password-confirm").value;
                var timeout = sanitizeInt(document.getElementById("cfg-web-session-timeout").value, 30);

                if (pw !== confirm) {
                    showToast("Web passwords do not match", "error");
                    return;
                }
                if (pw && pw.length < 8) {
                    showToast("Web password must be at least 8 characters", "error");
                    return;
                }

                // Save the timeout first (always), then set/clear the password.
                await apiPut("/config/web", { session_timeout_minutes: timeout });
                // Always call /auth/password — with a value it sets/changes the
                // password; with an empty value it clears it (auth disabled).
                var pwResult = await apiPost("/auth/password", { password: pw });
                if (pwResult && pwResult.status === "ok") {
                    showToast(pw ? "Web password set" : "Web password cleared", "success");
                } else {
                    showToast("Failed to update web password: " + ((pwResult && pwResult.message) || "Unknown error"), "error");
                }
                document.getElementById("cfg-web-password").value = "";
                document.getElementById("cfg-web-password-confirm").value = "";
            });

            // Device password (SSH + Samba, synced) — confirmation dialog.
            document.getElementById("btn-save-device-password")?.addEventListener("click", async () => {
                var pw = document.getElementById("cfg-device-password").value;
                var confirm = document.getElementById("cfg-device-password-confirm").value;
                if (!pw) { showToast("Enter a new device password", "error"); return; }
                if (pw !== confirm) { showToast("Device passwords do not match", "error"); return; }
                if (pw.length < 8) { showToast("Device password must be at least 8 characters", "error"); return; }

                var ok = await confirmDialog(
                    "This changes the password for SSH login AND the Samba share. Existing sessions stay active; new logins use the new password. Continue?",
                    { title: "Change device password?", okText: "Change password", danger: true }
                );
                if (!ok) return;

                var result = await apiPost("/system/device-password", {
                    new_password: pw,
                    confirm_password: confirm,
                });
                if (result && result.status === "ok") {
                    showToast("Device password changed (SSH + Samba)", "success");
                } else if (result && result.status === "partial") {
                    showToast("Console password changed, but Samba failed — stores out of sync", "error");
                } else {
                    showToast("Failed to change device password: " + ((result && result.message) || "Unknown error"), "error");
                }
                document.getElementById("cfg-device-password").value = "";
                document.getElementById("cfg-device-password-confirm").value = "";
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
