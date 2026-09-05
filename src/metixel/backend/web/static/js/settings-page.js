// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Settings page module. Slideshow / video / image optimisation / local-folder settings, watch-path rows, folder browser and transcode profile helpers.
 */

import {
    apiGet,
    apiPost,
    apiPut,
    confirmDialog,
    escapeHtml,
    sanitizeInt,
    setChecked,
    setValue,
    showToast
} from "./core.js";

    function _toggleTranscodeSettings(enabled) {
        var el = document.getElementById("transcode-settings");
        if (el) {
            el.style.display = enabled ? "" : "none";
            el.style.opacity = enabled ? "1" : "0.5";
        }
    }

    /**
     * Load transcoding profiles from the API and populate the dropdown.
     * Auto-selects the detected Pi model on first run.
     * @param {object} videoCfg - Current video config section.
     */

    async function _loadTranscodingProfiles(videoCfg) {
        try {
            var resp = await fetch("/api/config/video/profiles");
            if (!resp.ok) return;
            var data = await resp.json();
            var sel = document.getElementById("cfg-transcoding-profile");
            if (!sel) return;

            // Build a map of profile key → details for populating fields
            var profileMap = {};
            (data.profiles || []).forEach(function (p) {
                profileMap[p.key] = p;
            });

            // Populate options
            sel.innerHTML = '<option value="">Auto-detect</option>';
            (data.profiles || []).forEach(function (p) {
                var opt = document.createElement("option");
                opt.value = p.key;
                opt.textContent = p.label;
                sel.appendChild(opt);
            });

            // Determine which profile is active
            var activeProfile = data.current;
            if (!activeProfile && data.detected_model) {
                activeProfile = data.detected_model;
            }

            // Select the active profile in the dropdown
            if (activeProfile) {
                for (var i = 0; i < sel.options.length; i++) {
                    if (sel.options[i].value === activeProfile) {
                        sel.options[i].selected = true;
                        break;
                    }
                }
            }

            // Populate fields from the active profile (or custom settings)
            _applyProfileToFields(activeProfile, profileMap, data.custom_settings);

            // Handle change event
            sel.addEventListener("change", function () {
                _applyProfileToFields(this.value, profileMap, data.custom_settings);
            });
        } catch (e) {
            // Profiles API not available — fall back to legacy behaviour
        }
    }

    /**
     * Populate the profile fields from the selected profile or custom settings.
     * @param {string} profileKey - Selected profile key or "custom" or "".
     * @param {object} profileMap - Map of profile key → details from API.
     * @param {object} customSettings - Saved custom override values.
     */

    function _applyProfileToFields(profileKey, profileMap, customSettings) {
        var isCustom = (profileKey === "custom");
        var prof = profileMap[profileKey] || {};

        // Enable/disable fields
        var fieldsDiv = document.getElementById("profile-fields");
        if (fieldsDiv) {
            fieldsDiv.querySelectorAll("input, select").forEach(function (el) {
                el.disabled = !isCustom;
            });
        }

        // Determine values to display
        var vals;
        if (isCustom) {
            vals = customSettings || {};
            setValue("cfg-transcode-max-width", vals.transcode_max_width || 0);
            setValue("cfg-transcode-max-height", vals.transcode_max_height || 0);
            setValue("cfg-transcode-max-fps", vals.transcode_max_fps || 30);
            setValue("cfg-transcode-max-bitrate", vals.transcode_max_bitrate || 20);
            setValue("cfg-transcode-crf", vals.transcode_crf || 23);
            setValue("cfg-transcode-codec", vals.transcode_codec || "h264");
            setValue("cfg-transcode-h264-profile", vals.transcode_h264_profile || "high");
            setValue("cfg-transcode-h264-level", vals.transcode_h264_level || "4.2");
            setValue("cfg-transcode-color-depth", vals.transcode_color_depth || 8);
            setChecked("cfg-transcode-hdr", vals.transcode_hdr_support || false);
        } else {
            // Use profile defaults for display
            setValue("cfg-transcode-max-width", prof.max_width || 1920);
            setValue("cfg-transcode-max-height", prof.max_height || 1080);
            setValue("cfg-transcode-max-fps", prof.max_fps || 30);
            setValue("cfg-transcode-max-bitrate", prof.max_bitrate || 20);
            setValue("cfg-transcode-crf", prof.crf || 23);
            setValue("cfg-transcode-codec", prof.codec || "h264");
            setValue("cfg-transcode-h264-profile", prof.h264_profile || "high");
            setValue("cfg-transcode-h264-level", prof.h264_level || "4.2");
            setValue("cfg-transcode-color-depth", prof.color_depth || 8);
            setChecked("cfg-transcode-hdr", prof.hdr_support || false);
        }

        // Update hint text
        var hint = document.getElementById("profile-hint");
        if (hint) {
            hint.textContent = isCustom
                ? "Custom settings enabled — you control all parameters below."
                : "Profile sets optimal defaults for your Pi model. Override with Custom.";
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

    function toggleNtpFields(enabled) {
        var group = document.getElementById("ntp-servers-group");
        if (group) group.classList.toggle("hidden", !enabled);
    }

    // -- NTP Servers (dynamic row list, mirrors watch paths) ----------------

    /**
     * Render all NTP server rows from the config array.
     * @param {Array} servers - Array of server hostname strings.
     */
    function renderNtpServers(servers) {
        var list = document.getElementById("ntp-servers-list");
        if (!list) return;
        list.innerHTML = "";
        var values = (servers && servers.length) ? servers : [""];
        values.forEach(function (value) {
            addNtpServerRow(value || "");
        });
    }

    /**
     * Add a single NTP server row to the DOM.
     * @param {string} value - The server hostname.
     * @param {boolean} focus - Whether to focus the input (for new rows).
     */
    function addNtpServerRow(value, focus) {
        var list = document.getElementById("ntp-servers-list");
        if (!list) return;

        var row = document.createElement("div");
        row.className = "ntp-server-row";
        row.style.cssText = "display:flex;gap:0.35rem;align-items:center;margin-bottom:0.35rem";

        // Server input
        var input = document.createElement("input");
        input.type = "text";
        input.value = value;
        input.placeholder = "0.debian.pool.ntp.org";
        input.className = "input-premium";
        input.style.cssText = "flex:1;min-width:140px";

        // Remove button
        var removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:0.9rem;vertical-align:middle">close</span>';
        removeBtn.title = "Remove this NTP server";
        removeBtn.className = "btn--danger";
        removeBtn.style.cssText = "flex-shrink:0;padding:0.3rem 0.5rem;font-size:0.82rem";
        removeBtn.addEventListener("click", function () {
            row.remove();
        });

        row.appendChild(input);
        row.appendChild(removeBtn);
        list.appendChild(row);

        if (focus) {
            input.focus();
            input.select();
        }
    }

    /**
     * Collect all NTP server rows into the config array format.
     * @returns {Array} Array of non-empty server hostname strings.
     */
    function collectNtpServers() {
        var rows = document.querySelectorAll("#ntp-servers-list .ntp-server-row");
        var servers = [];
        rows.forEach(function (row) {
            var input = row.querySelector("input");
            if (input) {
                var value = input.value.trim();
                if (value) servers.push(value);
            }
        });
        return servers;
    }

    // -- Settings -----------------------------------------------------------

    var _settingsBound = false;

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
                cpu_throttle_percent: 100,
            };
        }
        setChecked("cfg-video-enabled", v.playback_enabled === true);
        setValue("cfg-video-player-backend", v.player_backend || "auto");
        setValue("cfg-video-max-duration", v.max_duration_seconds || 0);
        setChecked("cfg-transcode-enabled", v.transcoding_enabled !== false);
        setValue("cfg-transcode-max-width", v.transcode_max_width || 0);
        setValue("cfg-transcode-max-height", v.transcode_max_height || 0);
        setChecked("cfg-keep-audio", v.keep_audio === true);
        // Profile dropdown loaded via API (includes pi model auto-detection and CRF)
        _loadTranscodingProfiles(v);
        setChecked("cfg-transcode-software-encoder", v.transcode_use_software_encoder !== false);
        setValue("cfg-transcode-timeout", v.transcode_timeout_seconds || 7200);
        setChecked("cfg-cpu-throttle-enabled", v.cpu_throttle_enabled !== false);
        setValue("cfg-cpu-throttle-pct", v.cpu_throttle_percent || 100);
        var cpLabel = document.getElementById("cfg-cpu-throttle-pct-label");
        if (cpLabel) cpLabel.textContent = (v.cpu_throttle_percent || 100) + "%";
        _toggleTranscodeSettings(v.transcoding_enabled !== false);
        _toggleCpuThrottleGroup(v.cpu_throttle_enabled !== false);

        // Local folders (moved from Sync page)
        const local = config.sync?.local || {};
        setChecked("cfg-local-enabled", local.enabled !== false);
        setValue("cfg-local-interval", local.poll_interval_seconds || 30);
        renderWatchPaths(local.watch_paths || []);

        // Time / NTP (Playback page)
        const sysCfg = config.system || {};
        setChecked("cfg-ntp-enabled", sysCfg.ntp_enabled !== false);
        renderNtpServers(sysCfg.ntp_servers || [""]);
        toggleNtpFields(sysCfg.ntp_enabled !== false);

        // Image optimisation (moved from Sync page)
        const imgCfg = config.image || {};
        setChecked("cfg-image-opt-enabled", imgCfg.optimisation_enabled !== false);
        setValue("cfg-image-max-width", imgCfg.optimise_max_width || 0);
        setValue("cfg-image-max-height", imgCfg.optimise_max_height || 0);
        _toggleImageOptSettings(imgCfg.optimisation_enabled !== false);

        // Security — web password + session timeout + screen PIN timeout.
        // The password/PIN fields are always left empty (they are write-only);
        // only the timeout dropdowns reflect the current config.
        const webCfg = config.web || {};
        setValue("cfg-web-session-timeout", webCfg.session_timeout_minutes != null ? webCfg.session_timeout_minutes : 30);
        setValue("cfg-screen-pin-timeout", webCfg.screen_pin_timeout_minutes != null ? webCfg.screen_pin_timeout_minutes : 60);

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
                    transcode_crf: sanitizeInt(document.getElementById("cfg-transcode-crf").value, 23),
                    transcode_use_software_encoder: document.getElementById("cfg-transcode-software-encoder").checked,
                    transcode_timeout_seconds: sanitizeInt(document.getElementById("cfg-transcode-timeout").value, 7200),
                    cpu_throttle_enabled: document.getElementById("cfg-cpu-throttle-enabled").checked,
                    cpu_throttle_percent: sanitizeInt(document.getElementById("cfg-cpu-throttle-pct").value, 100),
                });
                if (result) {
                    showToast("Video settings saved!", "success");
                } else {
                    showToast("Failed to save video settings", "error");
                }
            });

            // ── Video Optimisation save ────────────────────────────────
            document.getElementById("btn-save-transcode")?.addEventListener("click", async () => {
                var profile = document.getElementById("cfg-transcoding-profile")?.value || "";
                var payload = {
                    transcoding_enabled: document.getElementById("cfg-transcode-enabled").checked,
                    transcoding_profile: profile,
                    keep_audio: document.getElementById("cfg-keep-audio").checked,
                    transcode_crf: sanitizeInt(document.getElementById("cfg-transcode-crf").value, 23),
                    transcode_use_software_encoder: document.getElementById("cfg-transcode-software-encoder").checked,
                    transcode_timeout_seconds: sanitizeInt(document.getElementById("cfg-transcode-timeout").value, 7200),
                    cpu_throttle_enabled: document.getElementById("cfg-cpu-throttle-enabled").checked,
                    cpu_throttle_percent: sanitizeInt(document.getElementById("cfg-cpu-throttle-pct").value, 200),
                };
                if (profile === "custom") {
                    payload.transcode_max_width = sanitizeInt(document.getElementById("cfg-transcode-max-width").value, 0);
                    payload.transcode_max_height = sanitizeInt(document.getElementById("cfg-transcode-max-height").value, 0);
                    payload.transcode_max_fps = sanitizeInt(document.getElementById("cfg-transcode-max-fps").value, 30);
                    payload.transcode_max_bitrate = sanitizeInt(document.getElementById("cfg-transcode-max-bitrate").value, 20);
                    payload.transcode_crf = sanitizeInt(document.getElementById("cfg-transcode-crf").value, 23);
                    payload.transcode_codec = document.getElementById("cfg-transcode-codec").value;
                    payload.transcode_h264_profile = document.getElementById("cfg-transcode-h264-profile").value;
                    payload.transcode_h264_level = document.getElementById("cfg-transcode-h264-level").value;
                    payload.transcode_color_depth = parseInt(document.getElementById("cfg-transcode-color-depth").value, 10);
                    payload.transcode_hdr_support = document.getElementById("cfg-transcode-hdr").checked;
                }
                var result = await apiPut("/config/video", payload);
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

            // Add NTP server button
            document.getElementById("btn-add-ntp-server")?.addEventListener("click", function () {
                addNtpServerRow("", true);
            });

            // Save Time Settings (NTP + timezone are saved immediately; NTP
            // servers are saved here together so the user can edit all at once.)
            document.getElementById("btn-save-time")?.addEventListener("click", async function () {
                var ntpEnabled = document.getElementById("cfg-ntp-enabled").checked;
                var ntpServers = collectNtpServers();
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

            // Timezone set button
            document.getElementById("btn-save-timezone")?.addEventListener("click", async function () {
                var tz = document.getElementById("cfg-timezone").value;
                if (!tz) { showToast("Select a timezone first", "info"); return; }
                var result = await apiPost("/time/timezone", { timezone: tz });
                if (result && result.status === "ok") {
                    showToast("Timezone set to " + tz, "success");
                    _refreshServerClock();
                } else {
                    showToast("Failed to set timezone: " + ((result && result.message) || "Unknown error"), "error");
                }
            });

            // ── Security card ──────────────────────────────────────────

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
                var timeoutResult = await apiPut("/config/web", { session_timeout_minutes: timeout });
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

            // Screen PIN (set/change/clear) + PIN timeout.
            document.getElementById("btn-save-screen-pin")?.addEventListener("click", async () => {
                var pin = document.getElementById("cfg-screen-pin").value;
                var confirm = document.getElementById("cfg-screen-pin-confirm").value;
                var timeout = sanitizeInt(document.getElementById("cfg-screen-pin-timeout").value, 60);

                var timeoutResult = await apiPut("/config/web", { screen_pin_timeout_minutes: timeout });

                if (pin) {
                    if (!/^[0-9]{4,6}$/.test(pin)) {
                        showToast("Screen PIN must be 4-6 digits", "error");
                        return;
                    }
                    if (pin !== confirm) { showToast("Screen PINs do not match", "error"); return; }
                    var pinResult = await apiPost("/auth/screen-pin", { pin: pin, confirm: confirm });
                    if (pinResult && pinResult.status === "ok") {
                        showToast("Screen PIN set", "success");
                    } else {
                        showToast("Failed to set screen PIN: " + ((pinResult && pinResult.message) || "Unknown error"), "error");
                    }
                } else if (timeoutResult) {
                    showToast("Screen PIN cleared / timeout saved", "success");
                }
                document.getElementById("cfg-screen-pin").value = "";
                document.getElementById("cfg-screen-pin-confirm").value = "";
            });
        }
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
                { path: "media/sample_media/landscape/", enabled: true },
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
        browseBtn.setAttribute("aria-label", "Browse folders");
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

    function openFolderBrowser(inputEl) {
        _browserTargetInput = inputEl;
        var modal = document.getElementById("folder-browser-modal");
        if (modal) modal.classList.add("open");
        // Start browsing at the current input value, or let the backend
        // default to the media folder when the field is empty.
        browseFolder(inputEl.value.trim() || "");
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

        var data = await apiGet("/browse?path=" + encodeURIComponent(folderPath));
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
                    // Make path relative to the persistent data dir if possible
                    var relPath = data.current_path;
                    var basePrefix = "/opt/metixel/data/";
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

// Bind all folder-browser controls at module import time — the DOM is fully
// parsed by then (ES modules are deferred) — so the browse buttons AND the
// modal's cancel controls (Cancel button, backdrop click, Escape) work on
// every page (Settings, Image Sync, Advanced) regardless of navigation order,
// not just after the Settings page has been visited.

// Open: every folder-browse button.
document.querySelectorAll(".btn-browse").forEach(function (btn) {
    btn.addEventListener("click", function () {
        var targetId = this.getAttribute("data-target");
        var inputEl = document.getElementById(targetId);
        if (inputEl) openFolderBrowser(inputEl);
    });
});

// Close: the Cancel button.
document.getElementById("btn-browser-cancel")?.addEventListener("click", closeFolderBrowser);
// Close: clicking the modal backdrop (outside the dialog).
document.getElementById("folder-browser-modal")?.addEventListener("click", function (e) {
    if (e.target === this) closeFolderBrowser();
});
// Close: pressing Escape while the modal is open.
document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
        var modal = document.getElementById("folder-browser-modal");
        if (modal && modal.classList.contains("open")) closeFolderBrowser();
    }
});

export { loadSettings };
