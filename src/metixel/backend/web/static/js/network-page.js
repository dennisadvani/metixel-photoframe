// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Network page module. WiFi status, scan, connect/forget, AP mode and network config.
 */

import {
    apiGet,
    apiPost,
    apiPut,
    confirmDialog,
    escapeHtml,
    setButtonBusy,
    setChecked,
    showToast
} from "./core.js";

    // -- Network Page --------------------------------------------------------

    var _networkBound = false;

    async function loadNetwork() {
        if (!_networkBound) {
            _networkBound = true;

            document.getElementById("btn-network-scan")?.addEventListener("click", async function () {
                var restore = setButtonBusy(this, "Scanning…");
                try {
                    await _refreshNetworkScan();
                } finally {
                    restore();
                }
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
            var countryEl = document.getElementById("cfg-wifi-country");
            if (countryEl) {
                countryEl.value = cfg.wifi_country || "";

                async function _saveWifiCountry() {
                    var code = countryEl.value.trim().toUpperCase().slice(0, 2);
                    countryEl.value = code;
                    if (code && code.length === 2) {
                        await apiPut("/config/network", { wifi_country: code });
                        await apiGet("/config/network?apply_wifi_country=" + encodeURIComponent(code));
                        showToast("WiFi country set to " + code, "success");
                    }
                }

                // Save button
                document.getElementById("btn-save-wifi-country")?.addEventListener("click", _saveWifiCountry);
                // Also save on Enter
                countryEl.addEventListener("keydown", function (e) {
                    if (e.key === "Enter") { e.preventDefault(); _saveWifiCountry(); }
                });
            }
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
                + '<span class="material-symbols-outlined" style="font-size:1em;vertical-align:middle">warning</span> WiFi is disabled at the OS level.<br>'
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
                + '<span class="material-symbols-outlined" style="font-size:2rem;color:var(--text)">wifi</span>'
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
                        if (!(await confirmDialog("Forget the '" + status.ssid + "' network and disconnect? The AP will reactivate if no other network is available.", { okText: "Forget" }))) return;
                        var restore = setButtonBusy(forgetBtn, "…");
                        var result = await apiPost("/network/forget", { ssid: status.ssid });
                        if (result && result.status === "ok") {
                            showToast("Network forgotten", "info");
                            _refreshNetworkStatus();
                        } else {
                            restore();
                            showToast("Failed to forget network", "error");
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
                ? '<button disabled style="display:inline-flex;align-items:center;font-size:0.78rem;padding:4px 10px;background:var(--success);color:#fff;border:none;border-radius:4px;cursor:default;opacity:0.8">Connected</button>'
                : '<button style="display:inline-flex;align-items:center;font-size:0.78rem;padding:4px 10px;background:var(--primary);color:#fff;border:none;border-radius:4px;cursor:pointer">Connect</button>';

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

export { loadNetwork };
