// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Log viewer module (List.js-based). Live log polling, severity filtering, search and pagination.
 */

import {
    apiGet,
    escapeHtml
} from "./core.js";

    // -- Logs ----------------------------------------------------------------

    /** Refresh logs (alias for polling). */
    // -- Log Viewer (List.js) -----------------------------------------------

    /** List.js instance — created once when the dashboard first loads. */
    var _logList = null;

    var _logPageSize = 100;
    /** Track which page we're on. */
    var _logCurrentPage = 1;
    /** Guard against recursive "updated" → filter → "updated" cycles. */
    var _logFiltering = false;

    // -- Log Viewer Controls (once-bound) ------------------------------------

    var _logControlsBound = false;

    /**
     * Bind the log viewer controls (wordwrap toggle + severity checkboxes).
     * Called from loadLogs so the bindings attach before the user can
     * interact with the log controls.
     */
    function bindLogControls() {
        if (_logControlsBound) return;
        _logControlsBound = true;

        // Log wordwrap toggle — must update BOTH the list AND its <li>
        // children because the CSS rule `.log-list li { white-space: nowrap }`
        // would otherwise override the parent.
        document.getElementById("log-wordwrap")?.addEventListener("change", function () {
            var wrap = this.checked;
            var logEl = document.querySelector(".log-list");
            if (logEl) {
                logEl.classList.toggle("wordwrap-off", !wrap);
            }
        });

        // Log severity filter checkboxes
        document.querySelectorAll(".log-filter").forEach(function (cb) {
            cb.addEventListener("change", function () {
                _applyLogFilter();
                _updateLogPagination();
            });
        });
    }


    function _initLogList() {
        if (_logList) return;
        if (typeof List === "undefined") {
            console.warn("List.js not loaded (CDN unreachable?) — log viewer search disabled");
            return;
        }
        // We use List.js only for DOM re-indexing and the items/matchingItems
        // arrays.  Search and severity filtering are handled by our own
        // _applyLogFilter() so they never fight across refreshes.
        _logList = new List("log-viewer", {
            valueNames: ["log-ts", "log-level", "log-logger", "log-msg"],
            listClass: "log-list",
            item: '<li><span class="log-ts"></span> <span class="log-level"></span> <span class="log-logger"></span> <span class="log-msg"></span></li>',
        });

        // Update pagination whenever the list changes (filter)
        _logList.on("updated", function () {
            _updateLogPagination();
        });

        // ── Search input: trigger our unified filter on every keystroke ──
        var searchInput = document.getElementById("log-search");
        if (searchInput) {
            searchInput.addEventListener("input", function () {
                _applyLogFilter();
            });
        }

        // Wire custom pagination buttons
        document.querySelector(".log-prev-page")?.addEventListener("click", function () {
            if (_logCurrentPage > 1) {
                _logCurrentPage--;
                _renderLogPage();
            }
        });
        document.querySelector(".log-next-page")?.addEventListener("click", function () {
            var totalPages = Math.ceil(_logList.matchingItems.length / _logPageSize) || 1;
            if (_logCurrentPage < totalPages) {
                _logCurrentPage++;
                _renderLogPage();
            }
        });
    }

    function _renderLogPage() {
        if (!_logList) return;
        var matching = _logList.matchingItems;
        var total = matching.length;
        var totalPages = Math.ceil(total / _logPageSize) || 1;
        var start = (_logCurrentPage - 1) * _logPageSize;
        var end = Math.min(start + _logPageSize, total);

        // Hide all items first, then show only the current page slice
        for (var i = 0; i < _logList.items.length; i++) {
            _logList.items[i].elm.style.display = "none";
        }
        for (var j = start; j < end; j++) {
            matching[j].elm.style.display = "";
        }

        // Update page info
        var infoEl = document.querySelector(".log-page-info");
        if (infoEl) infoEl.textContent = "Page " + _logCurrentPage + " of " + totalPages;

        var prevBtn = document.querySelector(".log-prev-page");
        var nextBtn = document.querySelector(".log-next-page");
        if (prevBtn) prevBtn.disabled = _logCurrentPage <= 1;
        if (nextBtn) nextBtn.disabled = _logCurrentPage >= totalPages;

        // Update count
        var countEl = document.querySelector(".log-count");
        if (countEl) countEl.textContent = total + " entries";
    }

    function _updateLogPagination() {
        if (!_logList) return;
        var totalPages = Math.ceil(_logList.matchingItems.length / _logPageSize) || 1;
        // If auto-follow is enabled, always jump to the last page so
        // the newest log entries are visible.  Otherwise keep the
        // current page (clamped to the new total).
        var follow = document.getElementById("log-follow");
        if (follow && follow.checked) {
            _logCurrentPage = totalPages;
        } else {
            _logCurrentPage = Math.min(_logCurrentPage, totalPages);
        }
        _renderLogPage();
    }

    async function refreshLogs() {
        await loadLogs();
    }

    async function loadLogs() {
        bindLogControls();
        var data = await apiGet("/logs/recent?count=500");
        var el = document.getElementById("log-output");
        if (!el) return;

        _initLogList();

        if (!data || !data.logs || data.logs.length === 0) {
            el.innerHTML = '<li class="log-empty">No log entries yet.</li>';
            if (_logList) _logList.reIndex();
            var countEl = document.querySelector(".log-count");
            if (countEl) countEl.textContent = "0 entries";
            _updateLogPagination();
            return;
        }

        // ── Fallback rendering when List.js isn't available ──────────
        if (!_logList) {
            var fbHtml = "";
            data.logs.forEach(function (entry) {
                var ts, level, loggerName, msg;
                if (typeof entry === "object" && entry.message !== undefined) {
                    ts = entry.timestamp || "";
                    level = entry.level || "INFO";
                    loggerName = entry.logger || "";
                    msg = entry.message;
                } else {
                    ts = "";
                    level = "INFO";
                    loggerName = "";
                    msg = String(entry);
                }
                fbHtml += '<li class="log-' + level.toLowerCase() + '">'
                    + '<span class="log-ts">' + escapeHtml(ts) + '</span> '
                    + '<span class="log-level">' + escapeHtml(level) + '</span> '
                    + '<span class="log-logger">' + escapeHtml(loggerName) + '</span> '
                    + '<span class="log-msg">' + escapeHtml(msg) + '</span>'
                    + '</li>';
            });
            el.innerHTML = fbHtml;
            var fbCount = document.querySelector(".log-count");
            if (fbCount) fbCount.textContent = data.logs.length + " entries (search unavailable)";
            _scrollToBottomIfFollowing();
            return;
        }

        // ── List.js path: build <li> elements, append to DOM, re-index ─
        // Track existing log IDs to avoid duplicating entries.
        var existingIds = {};
        var children = el.children;
        for (var c = 0; c < children.length; c++) {
            var id = children[c].getAttribute("data-log-id");
            if (id) existingIds[id] = true;
        }

        var newCount = 0;
        data.logs.forEach(function (entry) {
            var ts, level, loggerName, msg;
            if (typeof entry === "object" && entry.message !== undefined) {
                ts = entry.timestamp || "";
                level = entry.level || "INFO";
                loggerName = entry.logger || "";
                msg = entry.message;
            } else {
                ts = "";
                level = "INFO";
                loggerName = "";
                msg = String(entry);
            }
            var logId = ts + "|" + level + "|" + loggerName + "|" + msg;
            if (existingIds[logId]) return;

            var li = document.createElement("li");
            li.setAttribute("data-log-id", logId);
            li.className = "log-" + level.toLowerCase();
            li.innerHTML = '<span class="log-ts">' + escapeHtml(ts) + '</span> '
                + '<span class="log-level">' + escapeHtml(level) + '</span> '
                + '<span class="log-logger">' + escapeHtml(loggerName) + '</span> '
                + '<span class="log-msg">' + escapeHtml(msg) + '</span>';
            el.appendChild(li);
            existingIds[logId] = true;
            newCount++;
        });

        // Trim oldest entries to stay within capacity
        while (el.children.length > 500) {
            el.removeChild(el.firstChild);
        }

        // Tell List.js to re-parse the DOM, then re-apply the unified
        // search + severity filter in one pass.
        if (newCount > 0) {
            _logList.reIndex();
        }

        // Single combined filter: searches BOTH the text input AND severity
        // checkboxes — no separate search() / filter() calls that can clash.
        _applyLogFilter();
        _scrollToBottomIfFollowing();
    }

    function _scrollToBottomIfFollowing() {
        var follow = document.getElementById("log-follow");
        if (follow && follow.checked) {
            var viewer = document.getElementById("log-viewer");
            if (viewer) viewer.scrollTop = viewer.scrollHeight;
        }
    }

    /** Apply the unified search + severity filter in a single pass.

        Reads the current search text from ``#log-search`` and the checked
        severity boxes.  Both constraints are evaluated inside ONE
        ``_logList.filter()`` call so they never overwrite each other —
        the root cause of search breaking on refresh. */

    function _applyLogFilter() {
        if (_logFiltering || !_logList) return;
        _logFiltering = true;

        // Gather active severity levels
        var active = {};
        document.querySelectorAll(".log-filter").forEach(function (cb) {
            active[cb.dataset.level.toUpperCase()] = cb.checked;
        });

        // Read current search term
        var searchTerm = "";
        var searchInput = document.getElementById("log-search");
        if (searchInput) {
            searchTerm = searchInput.value.trim().toLowerCase();
        }

        // Single filter: severity AND optional text search
        _logList.filter(function (item) {
            // Severity check
            var level = (item.values()["log-level"] || "").toUpperCase();
            if (active[level] !== true) return false;

            // Text search (searches across timestamp, level, logger, message)
            if (searchTerm) {
                var haystack = (
                    (item.values()["log-ts"] || "") + "\t" +
                    (item.values()["log-level"] || "") + "\t" +
                    (item.values()["log-logger"] || "") + "\t" +
                    (item.values()["log-msg"] || "")
                ).toLowerCase();
                if (haystack.indexOf(searchTerm) === -1) return false;
            }

            return true;
        });

        _logFiltering = false;
        _updateLogPagination();
    }

export { refreshLogs };
