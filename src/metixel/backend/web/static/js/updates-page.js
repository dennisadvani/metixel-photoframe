// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * OTA updates module. Channel selector, update status, available versions and install controls.
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
    showToast,
    timeAgo
} from "./core.js";

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
                var ago = timeAgo(status.last_check);
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



    /** Wire up the update control buttons (one-time binding). */

    function bindUpdateControls() {
        if (_updateBound) return;
        _updateBound = true;

        // ── Channel selector ──────────────────────────────────────
        var channelSel = document.getElementById("cfg-update-channel");
        channelSel?.addEventListener("change", async function () {
            var ch = this.value;
            _updateChannelDesc(ch);

            // Show checking state immediately
            var statusEl = document.getElementById("update-status");
            if (statusEl) statusEl.innerHTML =
                '<span class="update-status-checking">Checking for updates\u2026</span>';

            var result = await apiPut("/updates/channel", { channel: ch });
            if (result && result.status === "ok") {
                showToast("Switched to " + ch + " channel", "success");
                // set_channel() already triggers a background check on the
                // backend — poll until it completes, then refresh the UI.
                var attempts = 0;
                var maxAttempts = 30; // 15 seconds max
                var pollInterval = setInterval(async function () {
                    attempts++;
                    var status = await apiGet("/updates/status");
                    if (!status || !status.check_in_progress || attempts >= maxAttempts) {
                        clearInterval(pollInterval);
                        loadUpdateStatus();
                    }
                }, 500);
            } else {
                showToast("Failed to switch channel", "error");
                loadUpdateStatus();
            }
        });

        // ── Check for Updates button ──────────────────────────────
        var checkBtn = document.getElementById("btn-check-updates");
        checkBtn?.addEventListener("click", async function () {
            var restore = setButtonBusy(checkBtn, "Checking\u2026");
            var statusEl = document.getElementById("update-status");
            if (statusEl) statusEl.innerHTML =
                '<span class="update-status-checking">Checking for updates\u2026</span>';

            var result = await apiPost("/updates/check?force=true");
            if (result && result.status === "ok") {
                // Poll for results (the check runs in a background thread)
                var attempts = 0;
                var maxAttempts = 30; // 15 seconds max
                var pollInterval = setInterval(async function () {
                    attempts++;
                    var status = await apiGet("/updates/status");
                    if (!status || !status.check_in_progress || attempts >= maxAttempts) {
                        clearInterval(pollInterval);
                        restore();
                        loadUpdateStatus();
                    }
                }, 500);
            } else {
                restore();
                loadUpdateStatus();
            }
        });

        // ── Install Update button ─────────────────────────────────
        var installBtn = document.getElementById("btn-apply-update");
        installBtn?.addEventListener("click", async function () {
            var ch = document.getElementById("cfg-update-channel")?.value || "stable";
            if (!(await confirmDialog("Install the latest update from the " + ch + " channel? Services will restart.", { okText: "Install" }))) {
                return;
            }

            var restoreInstall = setButtonBusy(installBtn, "Installing\u2026");

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
                    restoreInstall();
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

export { loadUpdateStatus, bindUpdateControls };
