// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * OTA updates module. Channel selector, update status, available versions,
 * specific-release install, rollback, auto-update schedule and full OS upgrade.
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

var _DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

/** Load and render the update status from the backend. */
async function loadUpdateStatus() {
    var status = await apiGet("/updates/status");
    if (!status) return;

    // ── Channel selector ───────────────────────────────────────
    setValue("cfg-update-channel", status.current_channel || "stable");
    _updateChannelDesc(status.current_channel);

    // ── Auto-check toggle ──────────────────────────────────────
    setChecked("cfg-update-auto-check", status.auto_check !== false);

    // ── Auto-update schedule ───────────────────────────────────
    setChecked("cfg-update-auto-update", status.auto_update !== false);
    setValue("cfg-update-auto-day", String(status.auto_update_day != null ? status.auto_update_day : 0));
    setValue("cfg-update-auto-time", status.auto_update_time || "04:30");
    _renderAutoUpdateHint(status);

    // ── Status line ────────────────────────────────────────────
    var statusEl = document.getElementById("update-status");
    if (!statusEl) return;

    // Always show the actually-installed version first, so the UI never
    // misleads the user into thinking a different version is running.
    var installed = status.installed_version || "unknown";
    var statusHtml = '<span style="font-weight:600">Installed: '
        + escapeHtml(installed) + '</span>';

    if (status.check_in_progress) {
        statusHtml += ' <span class="update-status-checking">— Checking for updates\u2026</span>';
    } else if (status.update_in_progress) {
        statusHtml += ' <span class="update-status-checking">— Installing update\u2026</span>';
    } else if (status.last_error) {
        statusHtml += ' <span class="update-status-error">— ' + escapeHtml(status.last_error) + '</span>';
    } else {
        var ch = status.current_channel || "stable";
        var avail = (status.available && status.available[ch]) ? status.available[ch] : null;
        var installBtn = document.getElementById("btn-apply-update");

        if (avail && avail.is_newer) {
            statusHtml +=
                ' <span class="update-status-available">— Update available: '
                + escapeHtml(avail.version) + '</span>';
            if (installBtn) {
                installBtn.style.display = "";
                installBtn.textContent = "Install " + escapeHtml(avail.version || "Update");
            }
        } else {
            statusHtml += ' <span class="update-status-uptodate">— Up to date</span>';
            if (installBtn) installBtn.style.display = "none";
        }

        // Last check time
        if (status.last_check) {
            var ago = timeAgo(status.last_check);
            statusHtml += ' <span style="font-size:0.75rem;color:var(--text-muted)">(checked ' + ago + ')</span>';
        }
    }
    statusEl.innerHTML = statusHtml;

    // ── Available versions list ─────────────────────────────────
    _renderAvailableVersions(status);

    // ── Release selector ───────────────────────────────────────
    _renderReleaseSelector(status);

    // ── Rollback selector ──────────────────────────────────────
    _renderRollbackSelector(status);

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
        "beta": "Beta releases include new features ready for wider testing. May have minor issues.",
        "main": "Latest commits from the main branch. Bleeding edge — use for testing only."
    };
    el.textContent = descs[channel] || "";
}

/** Render the auto-update schedule hint (day + time + last run). */
function _renderAutoUpdateHint(status) {
    var el = document.getElementById("update-auto-update-hint");
    if (!el) return;
    var day = _DAY_NAMES[status.auto_update_day] || "Monday";
    var time = status.auto_update_time || "04:30";
    var last = status.last_auto_update ? (" Last ran " + timeAgo(status.last_auto_update) + ".") : "";
    el.textContent = "Next auto-update: " + day + " at " + time + "." + last;
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
    var channels = ["stable", "beta", "main"];
    var installed = status.installed_version || "";

    channels.forEach(function (ch) {
        var info = avail[ch];
        if (!info) return;
        // "current" means this version is what's actually installed —
        // compare against installed_version, not the selected channel.
        var isInstalled = info.version === installed;
        var badge = isInstalled ? ' <span style="font-size:0.7rem;background:var(--primary);color:#fff;padding:1px 5px;border-radius:3px;vertical-align:middle">installed</span>' : '';
        var newerBadge = info.is_newer ? ' <span style="font-size:0.7rem;background:#f0a030;color:#000;padding:1px 5px;border-radius:3px;vertical-align:middle">newer</span>' : '';

        html += '<div class="update-available-item">'
            + '<span style="font-weight:600;text-transform:capitalize">' + escapeHtml(ch) + '</span>'
            + '<span>' + escapeHtml(info.version) + badge + newerBadge + '</span>'
            + '</div>';
    });

    list.innerHTML = html;
}

/** Populate the specific-release selector from the cached release list. */
function _renderReleaseSelector(status) {
    var sel = document.getElementById("cfg-update-release");
    if (!sel) return;

    var releases = (status.releases && status.releases.length) ? status.releases : [];
    var current = sel.value;
    var html = '<option value="">— Select a release —</option>';
    releases.forEach(function (r) {
        var label = r.version + (r.prerelease ? " (beta)" : "") + (r.installed ? " (installed)" : "");
        html += '<option value="' + escapeHtml(r.version) + '">' + escapeHtml(label) + '</option>';
    });
    sel.innerHTML = html;
    if (current) sel.value = current;
}

/** Populate the rollback selector from locally installed releases. */
function _renderRollbackSelector(status) {
    var sel = document.getElementById("cfg-update-rollback");
    if (!sel) return;

    var releases = (status.local_releases && status.local_releases.length) ? status.local_releases : [];
    var html = '<option value="">— Select an installed version —</option>';
    releases.forEach(function (r) {
        if (r.current) return; // can't roll back to the current release
        html += '<option value="' + escapeHtml(r.version) + '">' + escapeHtml(r.version) + '</option>';
    });
    sel.innerHTML = html;
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

    // ── Install Update button (latest on channel) ─────────────
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

    // ── Install a specific release ────────────────────────────
    var installReleaseBtn = document.getElementById("btn-install-release");
    installReleaseBtn?.addEventListener("click", async function () {
        var sel = document.getElementById("cfg-update-release");
        var version = sel?.value;
        if (!version) {
            showToast("Select a release to install", "error");
            return;
        }

        // If the release is already installed locally, ask to delete it first.
        var keepExisting = false;
        var installed = false;
        var status = await apiGet("/updates/status");
        if (status && status.releases) {
            var match = status.releases.find(function (r) { return r.version === version; });
            installed = !!(match && match.installed);
        }
        if (installed) {
            if (!(await confirmDialog(
                "Release " + version + " is already installed locally. Delete it and reinstall fresh?",
                { okText: "Delete & Reinstall" }
            ))) {
                return;
            }
            keepExisting = false;
        } else {
            if (!(await confirmDialog("Install release " + version + "? Services will restart.", { okText: "Install" }))) {
                return;
            }
        }

        var restore = setButtonBusy(installReleaseBtn, "Installing\u2026");
        try {
            var result = await apiPost("/updates/apply", { version: version, keep_existing: keepExisting });
            if (result && result.status === "ok") {
                showToast("Installing " + version + " — services restarting.", "success", 8000);
            } else {
                showToast((result && result.message) || "Install failed", "error", 5000);
                restore();
            }
        } catch (_) {
            showToast("Services restarting — the page will reconnect shortly.", "info", 8000);
        }
    });

    // ── Rollback ──────────────────────────────────────────────
    var rollbackBtn = document.getElementById("btn-rollback");
    rollbackBtn?.addEventListener("click", async function () {
        var sel = document.getElementById("cfg-update-rollback");
        var version = sel?.value;
        if (!version) {
            showToast("Select a version to roll back to", "error");
            return;
        }
        if (!(await confirmDialog("Roll back to version " + version + "? Services will restart.", { okText: "Roll Back" }))) {
            return;
        }

        var restore = setButtonBusy(rollbackBtn, "Rolling back\u2026");
        try {
            var result = await apiPost("/updates/rollback", { version: version });
            if (result && result.status === "ok") {
                showToast("Rolled back to " + version + " — services restarting.", "success", 8000);
            } else {
                showToast((result && result.message) || "Rollback failed", "error", 5000);
                restore();
            }
        } catch (_) {
            showToast("Services restarting — the page will reconnect shortly.", "info", 8000);
        }
    });

    // ── Full OS upgrade ───────────────────────────────────────
    var aptBtn = document.getElementById("btn-apt-upgrade");
    aptBtn?.addEventListener("click", async function () {
        if (!(await confirmDialog(
            "Run a full OS upgrade (apt update && apt upgrade) and reboot when complete? This can take a while.",
            { okText: "Upgrade & Reboot" }
        ))) {
            return;
        }

        var restore = setButtonBusy(aptBtn, "Upgrading\u2026");
        try {
            var result = await apiPost("/updates/apt-upgrade");
            if (result && result.status === "ok") {
                showToast("Full OS upgrade started — the system will reboot when complete.", "success", 8000);
            } else {
                showToast((result && result.message) || "Upgrade failed", "error", 5000);
                restore();
            }
        } catch (_) {
            showToast("Upgrade in progress — the page will reconnect after reboot.", "info", 8000);
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

    // ── Auto-update toggle ────────────────────────────────────
    var autoUpdate = document.getElementById("cfg-update-auto-update");
    autoUpdate?.addEventListener("change", async function () {
        var result = await apiPut("/updates/auto-update", { enabled: this.checked });
        if (result && result.status === "ok") {
            showToast(this.checked ? "Auto-update enabled" : "Auto-update disabled", "info");
        } else {
            showToast((result && result.message) || "Failed to update auto-update setting", "error");
            loadUpdateStatus();
        }
    });

    // ── Save auto-update schedule ─────────────────────────────
    var saveScheduleBtn = document.getElementById("btn-save-auto-update");
    saveScheduleBtn?.addEventListener("click", async function () {
        var day = parseInt(document.getElementById("cfg-update-auto-day")?.value || "0", 10);
        var time = document.getElementById("cfg-update-auto-time")?.value || "04:30";
        var result = await apiPut("/updates/auto-update", { day: day, time: time });
        if (result && result.status === "ok") {
            showToast("Auto-update schedule saved", "success");
            loadUpdateStatus();
        } else {
            showToast((result && result.message) || "Failed to save schedule", "error");
        }
    });
}

export { loadUpdateStatus, bindUpdateControls };