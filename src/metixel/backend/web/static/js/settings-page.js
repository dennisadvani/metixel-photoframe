// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Settings page module. Slideshow / video / image optimisation / local-folder settings, watch-path rows, folder browser and transcode profile helpers.
 */

import {
    apiGet,
    apiPost,
    apiPut,
    escapeHtml,
    sanitizeInt,
    setChecked,
    setValue,
    showToast,
    updatePowerButton
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

    function toggleScheduleFields(enabled) {
        var fields = document.getElementById("schedule-fields");
        if (fields) fields.classList.toggle("hidden", !enabled);
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

    // -- Clock & Timezone (Playback page) -----------------------------------

    /** @type {number|null} */
    var _clockTimer = null;

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
        // Portrait (90/270°) — the current player cannot display rotated
        // video, so force the toggle off and inform the user.  This is a
        // GUI companion to the backend guard in queue.py which excludes
        // videos from the playlist regardless of playback_enabled.
        var rotNum = Number(((config.display || {}).rotation) || 0) % 360;
        var portrait = (rotNum === 90 || rotNum === 270);
        var vidEnabled = document.getElementById("cfg-video-enabled");
        var vidWarn = document.getElementById("cfg-video-rotation-warning");
        if (portrait) {
            setChecked("cfg-video-enabled", false);
            if (vidEnabled) vidEnabled.disabled = true;
            if (vidWarn) vidWarn.classList.remove("hidden");
        } else {
            if (vidEnabled) vidEnabled.disabled = false;
            if (vidWarn) vidWarn.classList.add("hidden");
        }
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

        // Time / NTP (Playback page)
        const sysCfg = config.system || {};
        setChecked("cfg-ntp-enabled", sysCfg.ntp_enabled !== false);
        renderNtpServers(sysCfg.ntp_servers || [""]);
        toggleNtpFields(sysCfg.ntp_enabled !== false);

        // Server clock + timezone dropdown (the Time card lives on Playback).
        loadTimezoneList(sysCfg.timezone || "");
        _refreshServerClock();
        if (_clockTimer) clearInterval(_clockTimer);
        _clockTimer = setInterval(_refreshServerClock, 10000);

        // Image optimisation (moved from Sync page)
        const imgCfg = config.image || {};
        setChecked("cfg-image-opt-enabled", imgCfg.optimisation_enabled !== false);
        setValue("cfg-image-max-width", imgCfg.optimise_max_width || 0);
        setValue("cfg-image-max-height", imgCfg.optimise_max_height || 0);
        _toggleImageOptSettings(imgCfg.optimisation_enabled !== false);

        // Display Settings (the card lives on the Playback page).  The frontend
        // writes display_info.json with the effective (rotated) resolution; the
        // rotation dropdown must reflect it so the UI isn't stuck at 0°.
        const disp = config.display || {};
        setChecked("cfg-display-auto", (disp.width === 0 && disp.height === 0));
        setValue("cfg-fps-limit", disp.fps_limit || 30);
        setValue("cfg-display-rotation", disp.rotation || 0);
        setChecked("cfg-schedule-enabled", disp.schedule_enabled === true);
        setValue("cfg-schedule-on", disp.schedule_on_time || "07:00");
        setValue("cfg-schedule-off", disp.schedule_off_time || "22:00");
        toggleScheduleFields(disp.schedule_enabled === true);

        // Populate the resolution+refresh dropdown from supported modes so the
        // Playback page's Display card is fully functional when shown there.
        apiGet("/health/display/modes").then(function (data) {
            var sel = document.getElementById("cfg-display-resolution");
            if (!sel) return;
            sel.innerHTML = '<option value="0x0@0">Auto (native)</option>';
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
            var current = "0x0@0";
            if (disp.width > 0 && disp.height > 0) {
                current = disp.width + "x" + disp.height + "@" + (disp.refresh_rate || 0);
            }
            setValue("cfg-display-resolution", current);
        });

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

            // Populate the "Detected:" line from the frontend's display info
            // (effective rotated resolution), as the Playback page's Display
            // card is shown there but populated here.
            apiGet("/health/display/info").then(function (info) {
                var el = document.getElementById("display-detected-res");
                if (el && info && info.width > 0 && info.height > 0) {
                    var text = "Detected: " + info.width + " × " + info.height;
                    if (info.refresh_rate) text += " @ " + info.refresh_rate + " Hz";
                    if (info.rotation) text += " · rotated " + info.rotation + "°";
                    if (info.output) text += " · connected via " + info.output;
                    el.textContent = text;
                    el.style.color = "var(--text-muted)";
                }
            });

            // Save Display Settings (Playback page's Display card).
            document.getElementById("btn-save-display")?.addEventListener("click", async () => {
                const isAutoSave = document.getElementById("cfg-display-auto").checked;
                var val = document.getElementById("cfg-display-resolution").value || "0x0@0";
                var parts = val.split("@");
                var res = (parts[0] || "0x0").split("x");
                var width = isAutoSave ? 0 : sanitizeInt(res[0], 0);
                var height = isAutoSave ? 0 : sanitizeInt(res[1], 0);
                var refresh = isAutoSave ? 0 : sanitizeInt(parts[1], 0);
                var newRotation = sanitizeInt(document.getElementById("cfg-display-rotation").value, 0) % 360;
                var result = await apiPut("/config/display", {
                    width: width,
                    height: height,
                    fps_limit: sanitizeInt(document.getElementById("cfg-fps-limit").value, 30),
                    refresh_rate: refresh,
                    rotation: newRotation,
                });
                if (result) {
                    showToast("Display settings saved — frontend restarting to apply", "success", 5000);
                    if (newRotation === 90 || newRotation === 270) {
                        showToast("Video playback is disabled in portrait mode (90°/270°)", "info", 6000);
                    }
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

            // Display power toggle — reads actual state from health endpoint.
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
                // Portrait guard — videos cannot play at 90/270°, so never
                // save playback_enabled=true while rotating.  The toggle is
                // disabled by loadSettings in portrait; guard again in case
                // the state changed before the user clicked save.
                var vidEnabled = document.getElementById("cfg-video-enabled");
                if (vidEnabled && vidEnabled.disabled && vidEnabled.checked) {
                    showToast("Video playback is unavailable in portrait mode (90°/270°)", "error", 5000);
                    vidEnabled.checked = false;
                    return;
                }
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
                { path: "media/sample_media/portrait/", enabled: false },
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

export { loadSettings, renderWatchPaths, collectWatchPaths, addWatchPathRow };
