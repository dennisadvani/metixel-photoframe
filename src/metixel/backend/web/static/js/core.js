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
 *   - Folder browser:  openFolderBrowser, closeFolderBrowser (shared modal +
 *                      .btn-browse[data-target] wiring, create-folder support)
 *   - DOM/string utils: setStat, updatePowerButton, setButtonBusy,
 *                       sanitizeInt, setChecked, setValue, escapeHtml, timeAgo
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

// -- Confirm Dialog --------------------------------------------------------

/**
 * Show a modal confirmation dialog.
 *
 * Resolves `true` if the user confirms, `false` if they cancel, click the
 * backdrop, or press Escape.  The dialog is the shared `#confirm-modal`
 * element in `index.html`; the message is rendered as plain text (newlines
 * shown literally via `white-space: pre-wrap`), so callers pass unescaped
 * text and never need to call `escapeHtml`.
 *
 * @param {string} message - The message text.
 * @param {object} [opts]
 * @param {string} [opts.title] - Dialog title (default "Are you sure?").
 * @param {string} [opts.okText] - Confirm button label (default "Confirm").
 * @param {boolean} [opts.danger] - Use the danger (red) style for the confirm button.
 * @returns {Promise<boolean>}
 */
export function confirmDialog(message, opts) {
    opts = opts || {};
    var modal = document.getElementById("confirm-modal");
    var titleEl = document.getElementById("confirm-title");
    var msgEl = document.getElementById("confirm-message");
    var okBtn = document.getElementById("confirm-ok");
    var cancelBtn = document.getElementById("confirm-cancel");

    return new Promise(function (resolve) {
        var done = false;
        function finish(val) {
            if (done) return;
            done = true;
            okBtn.removeEventListener("click", onOk);
            cancelBtn.removeEventListener("click", onCancel);
            modal.removeEventListener("click", onBackdrop);
            document.removeEventListener("keydown", onKey);
            modal.classList.remove("open");
            resolve(val);
        }
        function onOk() { finish(true); }
        function onCancel() { finish(false); }
        function onBackdrop(e) { if (e.target === modal) finish(false); }
        function onKey(e) { if (e.key === "Escape") finish(false); }

        if (titleEl) titleEl.textContent = opts.title || "Are you sure?";
        if (msgEl) msgEl.textContent = message;
        if (okBtn) {
            okBtn.textContent = opts.okText || "Confirm";
            okBtn.className = opts.danger ? "btn--primary btn--danger" : "btn--primary";
            okBtn.addEventListener("click", onOk);
        }
        if (cancelBtn) cancelBtn.addEventListener("click", onCancel);
        modal.addEventListener("click", onBackdrop);
        document.addEventListener("keydown", onKey);
        modal.classList.add("open");
        if (okBtn) okBtn.focus();
    });
}

// -- Folder Browser (shared) -----------------------------------------------

// The folder-browser modal is a single shared dialog (#folder-browser-modal)
// used by every "browse for a folder" control across the SPA (watch paths,
// cache dir, sync dir, upload destination).  Its logic lives here in core so
// any page module can open it via openFolderBrowser() without duplicating the
// browse/create/select wiring.

/** @type {HTMLInputElement|null} The input to fill when a folder is selected. */
var _browserTargetInput = null;
/** @type {function|null} Optional custom handler run on Select (replaces input fill). */
var _browserOnSelect = null;
/** Absolute path of the folder the user is currently viewing. */
var _browserCurrentPath = "";

/**
 * Open the shared folder-browser modal.
 *
 * @param {HTMLInputElement} [inputEl] - The input whose value seeds the initial
 *        path and receives the selected (data-dir-relative) path on Select.
 * @param {object} [opts]
 * @param {string} [opts.initialPath] - Path to start browsing at.  Defaults to
 *        the input's current value, or the backend's media-folder default.
 * @param {function(string, object)} [opts.onSelect] - Optional handler called
 *        with the chosen absolute path + the last browse response instead of
 *        filling *inputEl* (e.g. to persist a setting immediately).
 */
export function openFolderBrowser(inputEl, opts) {
    opts = opts || {};
    _browserTargetInput = inputEl || null;
    _browserOnSelect = typeof opts.onSelect === "function" ? opts.onSelect : null;
    var modal = document.getElementById("folder-browser-modal");
    if (modal) modal.classList.add("open");

    var startPath = "";
    if (opts.initialPath != null) {
        startPath = String(opts.initialPath);
    } else if (inputEl && inputEl.value) {
        startPath = inputEl.value.trim();
    }
    browseFolder(startPath);
}

export function closeFolderBrowser() {
    var modal = document.getElementById("folder-browser-modal");
    if (modal) modal.classList.remove("open");
    _browserTargetInput = null;
    _browserOnSelect = null;
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
        listEl.innerHTML = '<li style="padding:0.5rem;color:var(--danger)">'
            + escapeHtml((data && data.error) || "Cannot browse folder") + '</li>';
        return;
    }

    _browserCurrentPath = data.current_path || folderPath;
    if (pathEl) pathEl.textContent = _browserCurrentPath;

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
            html += '<li class="browser-entry" data-path="' + escapeHtml(entry.path) + '"'
                + ' style="padding:0.4rem 0.5rem;cursor:pointer;border-bottom:1px solid var(--border);font-size:0.82rem">'
                + '<span class="material-symbols-outlined" style="font-size:0.9rem;vertical-align:middle">folder</span> '
                + escapeHtml(entry.name) + '</li>';
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

    // "Create folder" row — only where the backend allows creating folders
    // (restricted to the media tree).  The name field is cleared on each
    // navigation so a stale name never lingers between folders.
    var createRow = document.getElementById("browser-create-row");
    var nameInput = document.getElementById("browser-new-folder-name");
    if (createRow) createRow.style.display = data.can_create ? "" : "none";
    if (nameInput) nameInput.value = "";

    // Select button: run the custom handler, else fill the target input
    // with a data-dir-relative path (config stores folders relative to it).
    var selectBtn = document.getElementById("btn-browser-select");
    if (selectBtn) {
        selectBtn.onclick = function () {
            if (_browserOnSelect) {
                _browserOnSelect(_browserCurrentPath, data);
            } else if (_browserTargetInput && _browserCurrentPath) {
                var relPath = _pathRelativeToDataDir(_browserCurrentPath, data.base_path);
                _browserTargetInput.value = relPath;
            }
            closeFolderBrowser();
        };
    }
}

/**
 * Strip the persistent data dir prefix, producing the relative form that
 * config values use (e.g. "<data>/media/my_media/" → "media/my_media/").
 * Falls back to the absolute path when not under the data dir.  Separators
 * are normalised to forward slashes so values stay portable across OSes.
 */
function _pathRelativeToDataDir(absPath, basePath) {
    var base = basePath || "/opt/metixel/data";
    var norm = String(absPath).replace(/\\/g, "/");
    base = String(base).replace(/\\/g, "/");
    if (norm.indexOf(base) === 0) {
        var rel = norm.substring(base.length).replace(/^\/+/, "");
        if (rel && rel[rel.length - 1] !== "/") rel += "/";
        return rel;
    }
    return norm;
}

/** Create a folder inside the currently browsed directory, then open it. */
async function createFolder() {
    var nameInput = document.getElementById("browser-new-folder-name");
    if (!nameInput) return;
    var name = nameInput.value.trim();
    if (!name) {
        showToast("Enter a folder name first", "info");
        nameInput.focus();
        return;
    }
    var createBtn = document.getElementById("btn-browser-create");
    var restore = createBtn ? setButtonBusy(createBtn, "Creating…") : function () {};
    var result = await apiPost("/browse/create", { path: _browserCurrentPath, name: name });
    restore();
    if (!result || result.error) {
        showToast("Could not create folder: " + ((result && result.error) || "request failed"), "error", 5000);
        return;
    }
    showToast("Folder \u201c" + name + "\u201d created", "success");
    // Navigate into the new folder so the user can confirm with Select.
    browseFolder(result.path);
}

// Bind all folder-browser controls at module import time — the DOM is fully
// parsed by then (ES modules are deferred) — so the browse buttons AND the
// modal's controls work on every page (Settings, Sources, Media, System)
// regardless of navigation order.

// Open: every folder-browse button (data-target = id of the input to fill).
document.querySelectorAll(".btn-browse").forEach(function (btn) {
    btn.addEventListener("click", function () {
        var targetId = this.getAttribute("data-target");
        var inputEl = document.getElementById(targetId);
        if (inputEl) openFolderBrowser(inputEl);
    });
});

// Create-folder button inside the modal.
document.getElementById("btn-browser-create")?.addEventListener("click", createFolder);
// Also allow Enter inside the new-folder name field.
document.getElementById("browser-new-folder-name")?.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
        e.preventDefault();
        createFolder();
    }
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

// Card deep-link highlighting.  `flashCard` is invoked by `navigateTo` when
// a page/card deep link such as `#media/media-library` is followed.
var _flashedCard = null;

/**
 * Resolve a deep-link card token to its DOM element.
 *
 * Tokens accept either the card element's full id (`card-media-library`)
 * or that id with the `card-` prefix omitted (`media-library`), so links
 * can be written in the short form `#media/media-library`.
 *
 * @param {string} cardToken
 * @returns {Element|null}
 */
function findCard(cardToken) {
    if (!cardToken) return null;
    var el = document.getElementById(cardToken);
    if (!el) el = document.getElementById("card-" + cardToken);
    return el;
}

/**
 * Flash a card with the `card-flash` red-ring animation and scroll it into
 * view.  Re-invoking on the same (or another) card restarts from scratch.
 * @param {string} cardToken - Card element id or its `card-`-less slug.
 */
function flashCard(cardToken) {
    var el = findCard(cardToken);
    if (!el) return;

    // Clear any in-flight flash so this one starts clean.
    if (_flashedCard) {
        _flashedCard.classList.remove("card-flash");
        _flashedCard = null;
    }

    function onFinish(e) {
        if (e.animationName !== "card-flash") return;
        el.removeEventListener("animationend", onFinish);
        el.removeEventListener("animationcancel", onFinish);
        if (_flashedCard === el) {
            el.classList.remove("card-flash");
            _flashedCard = null;
        }
    }

    void el.offsetWidth; // force a reflow so the animation always restarts
    el.classList.add("card-flash");
    _flashedCard = el;
    el.addEventListener("animationend", onFinish);
    el.addEventListener("animationcancel", onFinish);

    var reduceMotion = window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollIntoView({
        behavior: reduceMotion ? "auto" : "smooth",
        block: "center",
    });
}

/**
 * Navigate to a page: updates the hash, nav active state, and visible
 * `.page` element, then invokes the registered page loader.
 *
 * @param {string} page - Page name (matches `data-page` / `#hash`).
 * @param {string} [cardId] - Optional deep-link target: the card element's
 *   id (e.g. `card-media-library`) or that id without the `card-` prefix
 *   (e.g. `media-library`).  When set, the targeted card is scrolled into
 *   view and briefly flashed with the `card-flash` red-ring animation.
 */
export function navigateTo(page, cardId) {
    // Persist page in URL hash so refreshes stay on the same tab
    var hash = cardId ? page + "/" + cardId : page;
    if (location.hash.substring(1) !== hash) {
        history.replaceState(null, "", "#" + hash);
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

    // Deep-link highlight — flash the targeted card now that it is visible.
    if (cardId) {
        flashCard(cardId);
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
    // Use the Material Symbol (not the ⏻ Unicode power character) so the
    // icon renders consistently on every platform/mobile.
    btn.innerHTML = '<span class="material-symbols-outlined" style="font-size:1em;vertical-align:middle">power_settings_new</span> '
        + (on ? "Turn Display Off" : "Turn Display On");
    btn.classList.toggle("btn--danger", !on);
}

/**
 * Temporarily put a button into a "busy" state: disable it and swap its
 * label while an async action runs.  The original content is preserved
 * (including any Material Symbol icon <span>) and restored afterwards, so
 * the button never shows raw icon ligature text.
 *
 * @param {HTMLElement} btn - Button element.
 * @param {string} busyLabel - Label to show while busy (plain text).
 * @returns {function(): void} Restore function (re-enables + restores label).
 */
export function setButtonBusy(btn, busyLabel) {
    if (!btn) return function () {};
    var original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = busyLabel;
    return function restore() {
        btn.innerHTML = original;
        btn.disabled = false;
    };
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

// Optional handler invoked when an API call returns 401/403 (auth required
// or session expired).  main.js registers this to show the login gate.
var _authRequiredHandler = null;

/**
 * Register a callback invoked when the API returns 401/403.
 * @param {Function|null} handler
 */
export function setAuthRequiredHandler(handler) {
    _authRequiredHandler = handler;
}

function _handleAuthFailure(path, status) {
    // Avoid console spam — only notify once per auth failure burst.
    if (_authRequiredHandler) {
        _authRequiredHandler(path, status);
    }
}

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
        const res = await fetch(`/api${path}`, { credentials: "same-origin" });
        if (!res.ok) {
            if (res.status === 401 || res.status === 403) {
                _handleAuthFailure(path, res.status);
            } else {
                console.error("API GET %s failed: %s %s", path, res.status, res.statusText);
                _apiUpdateConnectionStatus(false);
            }
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
            credentials: "same-origin",
            body: JSON.stringify(data),
        });
        if (!res.ok) {
            if (res.status === 401 || res.status === 403) {
                _handleAuthFailure(path, res.status);
            } else {
                console.error("API PUT %s failed: %s %s", path, res.status, res.statusText);
                _apiUpdateConnectionStatus(false);
            }
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
            credentials: "same-origin",
            body: data ? JSON.stringify(data) : undefined,
        });
        if (!res.ok) {
            if (res.status === 401 || res.status === 403) {
                _handleAuthFailure(path, res.status);
            } else {
                console.error("API POST %s failed: %s %s", path, res.status, res.statusText);
                _apiUpdateConnectionStatus(false);
            }
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
