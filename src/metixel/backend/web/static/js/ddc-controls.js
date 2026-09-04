// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * DDC/CI monitor-control helpers for the Advanced page.
 *
 * Capability-first: probes /api/ddc/capabilities and builds controls only for
 * features the monitor reports. No fixed brightness/contrast sliders.
 */

import { apiGet, apiPost, apiPut, confirmDialog, showToast } from "./core.js";

var _ddcBound = false;
var _ddcDebounceTimers = {};

function _escape(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function _setUnavailable(reason) {
    var status = document.getElementById("ddc-status");
    var controls = document.getElementById("ddc-controls");
    var empty = document.getElementById("ddc-empty");
    if (status) {
        status.textContent = reason || "Monitor DDC/CI not available";
        status.style.color = "var(--text-muted)";
    }
    if (controls) {
        controls.innerHTML = "";
        controls.style.display = "none";
    }
    if (empty) empty.style.display = "";
}

function _setAvailable(model) {
    var status = document.getElementById("ddc-status");
    var empty = document.getElementById("ddc-empty");
    if (status) {
        status.textContent = model ? ("Connected: " + model) : "Monitor DDC/CI available";
        status.style.color = "var(--text)";
    }
    if (empty) empty.style.display = "none";
}

function _renderFeature(feat) {
    var code = feat.code;
    // The API expects the VCP code in hex (e.g. "0x10" for brightness).
    // feat.code is decimal (16), so always send the hex form in the URL.
    var codeHex = feat.code_hex || ("0x" + Number(code).toString(16).toUpperCase());
    var name = feat.name || ("Feature " + (feat.code_hex || code));
    var icon = feat.icon || "tune";
    var row = document.createElement("div");
    row.className = "form-group ddc-feature-row";
    row.dataset.code = String(code);

    var label = document.createElement("span");
    label.className = "form-label";
    label.innerHTML =
        '<span class="material-symbols-outlined" style="font-size:1.1em;vertical-align:middle;color:var(--text-muted)">' +
        _escape(icon) +
        "</span> " +
        _escape(name);
    row.appendChild(label);

    // Factory reset (VCP 0x04) is an action button, not a slider.
    if (code === 4) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn--sm btn--danger";
        btn.textContent = "Reset to Factory Defaults";
        btn.addEventListener("click", async function () {
            if (!(await confirmDialog("Reset the monitor to factory defaults?", { okText: "Reset", danger: true }))) {
                return;
            }
            btn.disabled = true;
            try {
                var result = await apiPost("/ddc/reset", {});
                if (!result || result.status === "error" || result.available === false) {
                    showToast((result && (result.reason || result.error)) || "Failed to reset monitor", "error");
                } else {
                    showToast("Monitor reset to factory defaults", "success");
                    // Re-probe so the sliders reflect the reset values.
                    var caps = await apiGet("/ddc/capabilities");
                    if (caps && caps.available) _renderControls(caps);
                }
            } catch (_) {
                showToast("Failed to reset monitor", "error");
            } finally {
                btn.disabled = false;
            }
        });
        row.appendChild(btn);
        return row;
    }

    if (feat.type === "discrete" && Array.isArray(feat.options) && feat.options.length) {
        var sel = document.createElement("select");
        sel.id = "ddc-vcp-" + code;
        feat.options.forEach(function (opt) {
            var o = document.createElement("option");
            o.value = String(opt.value);
            o.textContent = opt.label || ("0x" + Number(opt.value).toString(16).toUpperCase());
            if (feat.current != null && Number(opt.value) === Number(feat.current)) {
                o.selected = true;
            }
            sel.appendChild(o);
        });
        if (feat.writable === false) sel.disabled = true;
        sel.addEventListener("change", function () {
            _commitVcp(codeHex, Number(sel.value), sel);
        });
        row.appendChild(sel);
    } else {
        var wrap = document.createElement("div");
        wrap.style.cssText = "display:flex;gap:0.5rem;align-items:center";
        var range = document.createElement("input");
        range.type = "range";
        range.id = "ddc-vcp-" + code;
        range.min = "0";
        range.max = String(feat.maximum != null && feat.maximum > 0 ? feat.maximum : 100);
        range.value = String(feat.current != null ? feat.current : 0);
        range.style.flex = "1";
        if (feat.writable === false) range.disabled = true;
        var num = document.createElement("span");
        num.className = "ddc-vcp-value";
        num.style.cssText = "min-width:2.5rem;text-align:right;font-variant-numeric:tabular-nums;color:var(--text-secondary)";
        num.textContent = range.value;
        range.addEventListener("input", function () {
            num.textContent = range.value;
            _debounceVcp(codeHex, Number(range.value), range);
        });
        range.addEventListener("change", function () {
            _commitVcp(codeHex, Number(range.value), range);
        });
        wrap.appendChild(range);
        wrap.appendChild(num);
        row.appendChild(wrap);
    }
    return row;
}

function _debounceVcp(codeHex, value, el) {
    if (_ddcDebounceTimers[codeHex]) {
        clearTimeout(_ddcDebounceTimers[codeHex]);
    }
    _ddcDebounceTimers[codeHex] = setTimeout(function () {
        _commitVcp(codeHex, value, el);
    }, 400);
}

async function _commitVcp(codeHex, value, el) {
    if (_ddcDebounceTimers[codeHex]) {
        clearTimeout(_ddcDebounceTimers[codeHex]);
        delete _ddcDebounceTimers[codeHex];
    }
    try {
        var result = await apiPut("/ddc/vcp/" + codeHex, { value: value });
        if (!result || result.status === "error" || result.available === false) {
            showToast((result && (result.reason || result.error)) || "Failed to set monitor value", "error");
            return;
        }
        if (result.current != null && el) {
            if (el.tagName === "SELECT") {
                el.value = String(result.current);
            } else if (el.type === "range") {
                el.value = String(result.current);
                var num = el.parentElement && el.parentElement.querySelector(".ddc-vcp-value");
                if (num) num.textContent = String(result.current);
            }
        }
    } catch (_) {
        showToast("Failed to set monitor value", "error");
    }
}

function _renderControls(data) {
    var controls = document.getElementById("ddc-controls");
    if (!controls) return;
    controls.innerHTML = "";
    var features = data.features || [];
    if (!features.length) {
        controls.style.display = "none";
        var empty = document.getElementById("ddc-empty");
        if (empty) {
            empty.style.display = "";
            empty.textContent = "Monitor reported no adjustable DDC features.";
        }
        return;
    }
    controls.style.display = "";
    features.forEach(function (feat) {
        controls.appendChild(_renderFeature(feat));
    });
}

async function loadDdcControls() {
    var enabledEl = document.getElementById("cfg-ddc-enabled");
    var displayEl = document.getElementById("cfg-ddc-display");
    var fields = document.getElementById("ddc-fields");

    var config = await apiGet("/config");
    var ddc = (config && config.ddc) || {};
    if (enabledEl) enabledEl.checked = !!ddc.enabled;
    if (displayEl) displayEl.value = ddc.display != null ? ddc.display : 1;
    if (fields) fields.style.display = ddc.enabled ? "" : "none";

    if (!ddc.enabled) {
        _setUnavailable("DDC/CI is disabled — enable it above and save to probe the monitor.");
        return;
    }

    var status = await apiGet("/ddc/status");
    if (!status) {
        _setUnavailable("Unable to reach DDC/CI service");
        return;
    }
    if (!status.available) {
        _setUnavailable(status.reason || "Monitor DDC/CI not available");
        return;
    }

    var caps = await apiGet("/ddc/capabilities");
    if (!caps || !caps.available) {
        _setUnavailable((caps && caps.reason) || status.reason || "Monitor DDC/CI not available");
        return;
    }
    _setAvailable(caps.model || (status.monitors && status.monitors[0] && status.monitors[0].model));
    _renderControls(caps);
}

function bindDdcControls() {
    if (_ddcBound) return;
    _ddcBound = true;

    document.getElementById("cfg-ddc-enabled")?.addEventListener("change", function () {
        var fields = document.getElementById("ddc-fields");
        if (fields) fields.style.display = this.checked ? "" : "none";
    });

    document.getElementById("btn-save-ddc")?.addEventListener("click", async function () {
        var enabled = !!document.getElementById("cfg-ddc-enabled")?.checked;
        var display = parseInt(document.getElementById("cfg-ddc-display")?.value || "1", 10);
        if (isNaN(display) || display < 1) display = 1;
        var result = await apiPut("/config/ddc", {
            enabled: enabled,
            display: display,
        });
        if (!result) {
            showToast("Failed to save DDC settings", "error");
            return;
        }
        showToast("DDC settings saved", "success");
        await loadDdcControls();
    });

    document.getElementById("btn-ddc-refresh")?.addEventListener("click", async function () {
        var btn = this;
        btn.disabled = true;
        try {
            var data = await apiPost("/ddc/refresh");
            if (!data || data.available === false) {
                _setUnavailable((data && data.reason) || "Monitor DDC/CI not available");
                showToast((data && data.reason) || "No DDC monitor found", "error");
            } else {
                _setAvailable(data.model);
                _renderControls(data);
                showToast("Monitor capabilities refreshed", "success");
            }
        } catch (_) {
            showToast("Failed to refresh DDC", "error");
        } finally {
            btn.disabled = false;
        }
    });
}

export { loadDdcControls, bindDdcControls };
