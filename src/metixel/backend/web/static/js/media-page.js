// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Media Library page module. Grid rendering, filters, paging and load-more.
 */

import {
    apiGet,
    escapeHtml
} from "./core.js";

    // -- Media --------------------------------------------------------------

    var _mediaOffset = 0;
    var _mediaLimit = 50;
    var _mediaHasMore = false;
    var _mediaLoading = false;
    /** Full media items cache for client-side filtering */
    var _allMediaItems = [];
    /** Set of unique folders extracted from media paths */
    var _mediaFolders = [];

    async function loadMedia() {
        _mediaOffset = 0;
        _mediaHasMore = false;
        _mediaLoading = false;
        _allMediaItems = [];
        _mediaFolders = [];

        var el = document.getElementById("media-list");
        el.innerHTML = '<p style="color:var(--text-muted)">Loading…</p>';

        // Populate folder filter dropdown from enabled watch paths only.
        // The media API only scans enabled paths, so a disabled folder
        // would always show 0 results and look broken to the user.
        var config = await apiGet("/config");
        if (config && config.sync && config.sync.local && config.sync.local.watch_paths) {
            var paths = config.sync.local.watch_paths;
            var sel = document.getElementById("media-filter-folder");
            if (sel) {
                // Keep the "All folders" option
                sel.innerHTML = '<option value="">All folders</option>';
                paths.forEach(function (p) {
                    // Skip disabled watch paths (object format with enabled:false)
                    if (typeof p === "object" && p.enabled === false) return;
                    var pathVal = typeof p === "object" ? p.path : String(p);
                    if (pathVal) {
                        // Value = folder name (matches API's item.folder = root.name)
                        var folderName = pathVal.replace(/\/+$/, "").split("/").pop() || pathVal;
                        var opt = document.createElement("option");
                        opt.value = folderName;
                        opt.textContent = pathVal;
                        sel.appendChild(opt);
                    }
                });
            }
        }

        await _fetchMediaPage(0);
    }

    /** Apply client-side filters and re-render the media grid. */

    function _applyMediaFilters() {
        var nameFilter = (document.getElementById("media-filter-name")?.value || "").toLowerCase().trim();
        var folderFilter = document.getElementById("media-filter-folder")?.value || "";
        var typeFilter = document.getElementById("media-filter-type")?.value || "";

        var filtered = _allMediaItems.filter(function (item) {
            // Filename filter
            if (nameFilter && item.name.toLowerCase().indexOf(nameFilter) === -1) return false;
            // Folder filter — match by folder name (e.g. "sample_media")
            if (folderFilter) {
                var itemFolder = item.folder || "";
                if (itemFolder !== folderFilter) return false;
            }
            // Type filter
            if (typeFilter && item.media_type !== typeFilter) return false;
            return true;
        });

        var el = document.getElementById("media-list");
        var grid = document.getElementById("media-grid");
        if (!grid) {
            el.innerHTML = '<div class="media-grid" id="media-grid"></div>';
            grid = document.getElementById("media-grid");
        } else {
            grid.innerHTML = "";
        }

        if (filtered.length === 0) {
            var emptyMsg = "No media match the current filters.";
            if (folderFilter) {
                emptyMsg = 'No media in &quot;' + escapeHtml(folderFilter)
                    + '&quot; — check the folder is enabled under Settings → Local Folders.';
            } else if (nameFilter || typeFilter) {
                emptyMsg = "No media match the current filters — try clearing the search or type filter.";
            }
            el.innerHTML = '<p class="media-summary">0 files</p>'
                + '<p style="color:var(--text-muted)">' + emptyMsg + '</p>'
                + '<div class="media-grid" id="media-grid"></div>';
            return;
        }

        // Update summary
        var summaryEl = el.querySelector(".media-summary");
        if (!summaryEl) {
            summaryEl = document.createElement("p");
            summaryEl.className = "media-summary";
            el.insertBefore(summaryEl, el.firstChild);
        }
        var imgCount = 0, vidCount = 0;
        filtered.forEach(function (item) { if (item.media_type === "video") vidCount++; else imgCount++; });
        var parts = [];
        if (imgCount) parts.push(imgCount + " images");
        if (vidCount) parts.push(vidCount + " videos");
        summaryEl.textContent = parts.length ? parts.join(", ") : filtered.length + " files";

        _renderMediaBatch(grid, filtered, 0);
    }

    async function _fetchMediaPage(offset) {
        if (_mediaLoading) return;
        _mediaLoading = true;

        var el = document.getElementById("media-list");
        var data = await apiGet("/media/list?offset=" + offset + "&limit=" + _mediaLimit);

        if (!data || !data.items || data.items.length === 0) {
            if (offset === 0) {
                el.innerHTML = '<p style="color:var(--text-muted)">No media found. Add images or videos to the media folder.</p>';
            }
            _mediaLoading = false;
            return;
        }

        _mediaOffset = data.offset + data.items.length;
        _mediaHasMore = data.has_more;

        // Store all items for client-side filtering
        if (offset === 0) {
            _allMediaItems = data.items.slice();
        } else {
            _allMediaItems = _allMediaItems.concat(data.items);
        }

        // Build summary on first page
        var html = '';
        if (offset === 0) {
            var summaryParts = [];
            if (data.images) summaryParts.push(data.images + " images");
            if (data.videos) summaryParts.push(data.videos + " videos");
            html += '<p class="media-summary">'
                + (summaryParts.length ? summaryParts.join(", ") : data.total + " files") + '</p>'
                + '<div class="media-grid" id="media-grid"></div>';
            el.innerHTML = html;
        }

        var grid = document.getElementById("media-grid");
        if (!grid) {
            el.innerHTML = '<div class="media-grid" id="media-grid"></div>';
            grid = document.getElementById("media-grid");
        }

        // Apply current filters instead of raw render
        _applyMediaFilters();

        // Show "Load more" button
        _updateLoadMoreButton(el);
        _mediaLoading = false;

        // Wire filter event listeners once
        _bindMediaFilters();
    }

    var _mediaFiltersBound = false;

    function _bindMediaFilters() {
        if (_mediaFiltersBound) return;
        _mediaFiltersBound = true;
        document.getElementById("media-filter-name")?.addEventListener("input", function () {
            _applyMediaFilters();
        });
        document.getElementById("media-filter-folder")?.addEventListener("change", function () {
            _applyMediaFilters();
        });
        document.getElementById("media-filter-type")?.addEventListener("change", function () {
            _applyMediaFilters();
        });
    }

    function _renderMediaBatch(grid, items, startIdx) {
        var batchSize = 10;
        var end = Math.min(startIdx + batchSize, items.length);

        for (var i = startIdx; i < end; i++) {
            var item = items[i];
            var isVideo = item.media_type === "video";
            var thumbHtml = '';
            if (item.thumbnail_url) {
                thumbHtml = '<div class="media-thumb">'
                    + '<img src="' + escapeHtml(item.thumbnail_url) + '" alt="' + escapeHtml(item.name) + '" loading="lazy"'
                    + ' onerror="this.parentElement.style.display=\'none\'" />'
                    + '</div>';
            }

            // Build type + status badges
            var badges = '';
            if (isVideo) {
                badges += ' <span class="media-badge media-badge--video">Video</span>';
            }
            if (item.transcode_status === "queued") {
                badges += ' <span class="media-badge media-badge--queued">Queued</span>';
            } else if (item.transcode_status === "transcoding") {
                badges += ' <span class="media-badge media-badge--transcoding">Transcoding</span>';
            }

            // Extract folder from relative path (e.g. "sub/folder/file.jpg" → "sub/folder/")
            // Prefer the API's `folder` field (watch folder name), then append subdirectory.
            var folderParts = [];
            if (item.folder) {
                folderParts.push(item.folder);
            }
            if (item.path) {
                var lastSlash = item.path.lastIndexOf('/');
                if (lastSlash > 0) {
                    folderParts.push(item.path.substring(0, lastSlash));
                }
            }
            var folderHtml = folderParts.length
                ? '<div class="media-folder">' + escapeHtml(folderParts.join(' › ')) + '</div>'
                : '';

            var infoText;
            if (isVideo) {
                infoText = (item.width && item.height)
                    ? item.width + '\u00d7' + item.height + ' \u00b7 ' + item.size_kb + ' KB'
                    : item.size_kb + ' KB';
            } else {
                infoText = item.width + '\u00d7' + item.height + ' \u00b7 ' + item.size_kb + ' KB';
            }
            var div = document.createElement("div");
            div.className = "media-item";
            div.innerHTML = thumbHtml
                + '<div class="media-name">' + escapeHtml(item.name) + badges + '</div>'
                + folderHtml
                + '<div class="media-info">' + infoText + '</div>';
            grid.appendChild(div);
        }

        // Schedule next batch if there are more items
        if (end < items.length) {
            requestAnimationFrame(function () {
                _renderMediaBatch(grid, items, end);
            });
        }
    }

    function _updateLoadMoreButton(el) {
        // Remove existing button
        var existing = document.getElementById("media-load-more");
        if (existing) existing.remove();

        if (_mediaHasMore) {
            var btn = document.createElement("button");
            btn.id = "media-load-more";
            btn.textContent = "Load more\u2026";
            btn.className = "btn--secondary";
            btn.style.marginTop = "1rem";
            btn.addEventListener("click", function () {
                btn.textContent = "Loading\u2026";
                btn.disabled = true;
                _fetchMediaPage(_mediaOffset);
            });
            el.appendChild(btn);
        }
    }

export { loadMedia };
