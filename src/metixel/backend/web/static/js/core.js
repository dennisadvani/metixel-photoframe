// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Metixel Photoframe Dashboard — shared infrastructure (core module).
 *
 * Everything in this module is page-agnostic and imported by the dashboard
 * SPA and its page modules. No page-specific logic lives here.
 *
 * Exposes:
 *   - API layer:       apiGet, apiPut, apiPost (+ private connection tracking)
 *   - UI shell:        showToast, openDrawer, closeDrawer, navigateTo, registerPage
 *   - DOM/string utils: setStat, updatePowerButton, sanitizeInt, setChecked,
 *                       setValue, escapeHtml, timeAgo
 */

// -- Toast Notifications ---------------------------------------------------

/**
 * Show a toast notification in the top-right corner.
 * Auto-dismisses after the given duration (default 3 seconds).
 *
 * @param {string} message - The message text.
 * @param {'success'|'error'|'info'} type - Toast style variant.
 * @param {number} durationMs - How long the toast stays visible.
 */
export function showToast(message, type, durationMs) {
    type = type || "success";
    durationMs = durationMs || 3000;

    // Ensure the container exists
    var container = document.getElementById("toast-container");
    if (!container) {
        container = document.createElement("div");
        container.id = "toast-container";
        container.className = "toast-container";
        document.body.appendChild(container);
    }

    var toast = document.createElement("div");
    toast.className = "toast toast--" + type;
    toast.textContent = message;

    container.appendChild(toast);

    // Remove after animation completes
    setTimeout(function () {
        if (toast.parentNode) {
            toast.parentNode.removeChild(toast);
        }
    }, durationMs);
}

// -- Page Navigation -------------------------------------------------------

// Drawer elements. ES modules are deferred, so the DOM is fully parsed by
// the time this module is evaluated — safe to cache them here.
var drawer = document.getElementById("nav-drawer");
var backdrop = document.getElementById("nav-backdrop");

export function openDrawer() {
    if (drawer) drawer.classList.add("open");
    if (backdrop) backdrop.classList.add("open");
}

export function closeDrawer() {
    if (drawer) drawer.classList.remove("open");
    if (backdrop) backdrop.classList.remove("open");
}

// SPA router: page modules register their loaders here so that this module
// never needs to import page modules (avoids circular imports).
var _pageLoaders = {};

/**
 * Register a page loader with the SPA router.
 * @param {string} page - Page name (matches the `data-page` / `#hash`).
 * @param {Function} loader - Loader called whenever the page is shown.
 */
export function registerPage(page, loader) {
    _pageLoaders[page] = loader;
}

/**
 * Navigate to a page: updates the hash, nav active state, and visible
 * `.page` element, then invokes the registered page loader.
 * @param {string} page
 */
export function navigateTo(page) {
    // Persist page in URL hash so refreshes stay on the same tab
    if (location.hash.substring(1) !== page) {
        history.replaceState(null, "", "#" + page);
    }

    // Update nav drawer active state
    document.querySelectorAll(".nav-drawer nav a").forEach((a) => a.classList.remove("active"));
    var drawerLink = document.querySelector('.nav-drawer nav a[data-page="' + page + '"]');
    if (drawerLink) drawerLink.classList.add("active");

    // Show the selected page
    document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
    const target = document.getElementById(`page-${page}`);
    if (target) target.classList.add("active");

    // Load page data via the registry
    const loader = _pageLoaders[page];
    if (loader) {
        loader();
    }
}

// -- UI Helpers ------------------------------------------------------------

/**
 * Update a simple text stat element by ID. No-op if element missing.
 * @param {string} id - Element ID.
 * @param {*} value
 */
export function setStat(id, value) {
    var el = document.getElementById(id);
    if (el) el.textContent = value;
}

/**
 * Sync the display power button with the actual state.
 * @param {boolean} on
 */
export function updatePowerButton(on) {
    var btn = document.getElementById("btn-display-power");
    if (!btn) return;
    btn.innerHTML = on ? "⏻ Turn Display Off" : "⏻ Turn Display On";
    btn.classList.toggle("btn--danger", !on);
}

/**
 * Parse an input value as an integer, returning a safe fallback.
 * Prevents NaN from leaking into JSON payloads (NaN → null in JSON).
 * @param {string|number} val
 * @param {number} fallback
 * @returns {number}
 */
export function sanitizeInt(val, fallback) {
    var n = parseInt(val, 10);
    return isNaN(n) ? (fallback || 0) : n;
}

/**
 * Set a checkbox's checked state, silently ignoring missing elements.
 * @param {string} id - Element ID.
 * @param {boolean} state
 */
export function setChecked(id, state) {
    var el = document.getElementById(id);
    if (el) el.checked = state;
}

/**
 * Set an input's value, silently ignoring missing elements.
 * @param {string} id - Element ID.
 * @param {*} value
 */
export function setValue(id, value) {
    var el = document.getElementById(id);
    if (el) el.value = value;
}

/**
 * Escape HTML special characters to prevent XSS in log output.
 * @param {*} text
 * @returns {string}
 */
export function escapeHtml(text) {
    var map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };
    return String(text).replace(/[&<>"']/g, function (m) { return map[m]; });
}

/**
 * Format a duration from an ISO timestamp to a human-readable "X ago" string.
 * @param {string} isoStr
 * @returns {string}
 */
export function timeAgo(isoStr) {
    var then = Date.parse(isoStr);
    if (isNaN(then)) return "";
    var seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (seconds < 60) return seconds + "s ago";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
    return Math.floor(seconds / 86400) + "d ago";
}

// -- API Helper ------------------------------------------------------------

// Track connection state to avoid console spam during restarts.
var _apiConnected = true;
var _apiErrorCount = 0;
var _apiLastErrorTime = 0;

function _apiUpdateConnectionStatus(ok) {
    var overlay = document.getElementById("connection-overlay");
    if (!overlay) return;
    if (ok) {
        // Reconnected after being disconnected — reload the page
        // to pick up fresh state (playlist, config, dashboard data).
        if (overlay.style.display === "flex") {
            window.location.reload();
            return;
        }
        overlay.style.display = "none";
        _apiConnected = true;
        _apiErrorCount = 0;
    } else {
        _apiConnected = false;
        _apiErrorCount++;
        overlay.style.display = "flex";
    }
}

export async function apiGet(path) {
    try {
        const res = await fetch(`/api${path}`);
        if (!res.ok) {
            console.error("API GET %s failed: %s %s", path, res.status, res.statusText);
            _apiUpdateConnectionStatus(false);
            return null;
        }
        _apiUpdateConnectionStatus(true);
        return await res.json();
    } catch (err) {
        // Suppress console spam during restarts — only log every 30s
        var now = Date.now();
        if (now - _apiLastErrorTime > 30000) {
            console.warn("API unreachable (backend may be restarting):", err.message);
            _apiLastErrorTime = now;
        }
        _apiUpdateConnectionStatus(false);
        return null;
    }
}

export async function apiPut(path, data) {
    try {
        const res = await fetch(`/api${path}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data),
        });
        if (!res.ok) {
            console.error("API PUT %s failed: %s %s", path, res.status, res.statusText);
            _apiUpdateConnectionStatus(false);
            return null;
        }
        _apiUpdateConnectionStatus(true);
        return await res.json();
    } catch (err) {
        var now = Date.now();
        if (now - _apiLastErrorTime > 30000) {
            console.warn("API unreachable (backend may be restarting):", err.message);
            _apiLastErrorTime = now;
        }
        _apiUpdateConnectionStatus(false);
        return null;
    }
}

export async function apiPost(path, data) {
    try {
        const res = await fetch(`/api${path}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: data ? JSON.stringify(data) : undefined,
        });
        return await res.json();
    } catch (err) {
        console.error("API error:", err);
        return null;
    }
}
