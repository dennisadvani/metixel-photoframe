// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Media Library page module. Grid rendering, filters, paging and load-more.
 */

import {
    apiGet,
    escapeHtml,
    setButtonBusy,
    showToast
} from "./core.js";

    // -- Media --------------------------------------------------------------

    var _mediaOffset = 0;
    var _mediaLimit = 20;
    var _mediaHasMore = false;
    var _mediaLoading = false;
    /** Guard so upload/drop bindings are attached once */
    var _mediaUploadBound = false;

    async function loadMedia() {
        _mediaOffset = 0;
        _mediaHasMore = false;
        _mediaLoading = false;

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

        _bindUpload();
    }

    /** Read the current filter values from the toolbar. */
    function _currentFilters() {
        return {
            name: (document.getElementById("media-filter-name")?.value || "").trim(),
            folder: document.getElementById("media-filter-folder")?.value || "",
            type: document.getElementById("media-filter-type")?.value || ""
        };
    }

    /** Build the query string for the media list request from the filters. */
    function _mediaQueryString(offset) {
        var f = _currentFilters();
        var qs = "offset=" + offset + "&limit=" + _mediaLimit;
        if (f.name) qs += "&name=" + encodeURIComponent(f.name);
        if (f.folder) qs += "&folder=" + encodeURIComponent(f.folder);
        if (f.type) qs += "&type=" + encodeURIComponent(f.type);
        return qs;
    }

    /**
     * Trigger a server-side filtered query. Filtering happens on the backend
     * so the browser only downloads the page it displays — important on
     * low-power Pis. Resets to page 0 and re-fetches.
     */
    function _applyMediaFilters() {
        _mediaOffset = 0;
        _mediaHasMore = false;
        _fetchMediaPage(0);
    }

    /** Clear all filters and reload the full library. */
    function _clearMediaFilters() {
        var nameInput = document.getElementById("media-filter-name");
        var folderSel = document.getElementById("media-filter-folder");
        var typeSel = document.getElementById("media-filter-type");
        if (nameInput) nameInput.value = "";
        if (folderSel) folderSel.value = "";
        if (typeSel) typeSel.value = "";
        _applyMediaFilters();
    }

    /** Show a loading placeholder while a fresh (page 0) query is in flight. */
    function _showMediaLoading() {
        var el = document.getElementById("media-list");
        if (!el) return;
        el.innerHTML = '<p class="media-loading">'
            + '<span class="material-symbols-outlined upload-spin" style="font-size:1em;vertical-align:middle">sync</span>'
            + ' Loading…</p>';
    }

    async function _fetchMediaPage(offset) {
        if (_mediaLoading) return;
        _mediaLoading = true;

        var el = document.getElementById("media-list");
        // Show a visual indicator for fresh queries — filtering can take a
        // moment on the Pi (e.g. switching to Videos).
        if (offset === 0) _showMediaLoading();
        var data = await apiGet("/media/list?" + _mediaQueryString(offset));

        if (!data) {
            // API error (connection / backend down) — the connection overlay
            // shows the reconnecting banner; give a clear in-page message too.
            if (offset === 0) {
                el.innerHTML = '<p style="color:var(--danger)">Could not load the media library. Check that the backend is running and refresh the page.</p>';
            }
            _mediaLoading = false;
            return;
        }

        _mediaOffset = data.offset + data.items.length;
        _mediaHasMore = data.has_more;

        // Build summary + grid on first page
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

        if (!data.items || data.items.length === 0) {
            if (offset === 0) {
                var f = _currentFilters();
                var emptyMsg = "No media match the current filters.";
                if (f.folder) {
                    emptyMsg = 'No media in &quot;' + escapeHtml(f.folder)
                        + '&quot; — check the folder is enabled under Settings → Local Folders.';
                } else if (f.name || f.type) {
                    emptyMsg = "No media match the current filters — try clearing the search or type filter.";
                }
                el.innerHTML = '<p class="media-summary">0 files</p>'
                    + '<p style="color:var(--text-muted)">' + emptyMsg + '</p>'
                    + '<div class="media-grid" id="media-grid"></div>';
            }
            _mediaLoading = false;
            return;
        }

        // Render the returned page directly (already filtered server-side)
        _renderMediaBatch(grid, data.items, 0);

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

        // Folder & type apply immediately on change (single discrete events).
        document.getElementById("media-filter-folder")?.addEventListener("change", function () {
            _applyMediaFilters();
        });
        document.getElementById("media-filter-type")?.addEventListener("change", function () {
            _applyMediaFilters();
        });

        // Name filter submits on Enter only — the Pi is low-power, so avoid
        // firing a request on every keystroke.
        document.getElementById("media-filter-name")?.addEventListener("keydown", function (e) {
            if (e.key === "Enter") {
                e.preventDefault();
                _applyMediaFilters();
            }
        });

        // Search button (mobile has no Enter key) and clear-filters button.
        document.getElementById("btn-media-search")?.addEventListener("click", function () {
            _applyMediaFilters();
        });
        document.getElementById("btn-media-clear")?.addEventListener("click", function () {
            _clearMediaFilters();
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
                setButtonBusy(btn, "Loading\u2026");
                _fetchMediaPage(_mediaOffset);
            });
            el.appendChild(btn);
        }
    }

// -- Upload -------------------------------------------------------------

function _bindUpload() {
    if (_mediaUploadBound) return;
    _mediaUploadBound = true;

    var btn = document.getElementById("btn-upload-media");
    var input = document.getElementById("file-upload");
    var list = document.getElementById("media-list");

    if (btn && input) {
        btn.addEventListener("click", function () {
            input.click();
        });
        input.addEventListener("change", function () {
            if (input.files && input.files.length) {
                _uploadFiles(input.files);
            }
            input.value = "";
        });
    }

    // Drag & drop onto the media list / grid (desktop).
    if (list) {
        var depth = 0;
        list.addEventListener("dragenter", function (e) {
            e.preventDefault();
            depth++;
            list.classList.add("drop-active");
        });
        list.addEventListener("dragover", function (e) {
            e.preventDefault();
        });
        list.addEventListener("dragleave", function (e) {
            e.preventDefault();
            depth = Math.max(0, depth - 1);
            if (depth === 0) list.classList.remove("drop-active");
        });
        list.addEventListener("drop", function (e) {
            e.preventDefault();
            depth = 0;
            list.classList.remove("drop-active");
            if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
                _uploadFiles(e.dataTransfer.files);
            }
        });
    }
}

function _uploadFiles(files) {
    var list = Array.prototype.slice.call(files);
    if (!list.length) return;

    var form = new FormData();
    list.forEach(function (f) {
        form.append("files", f, f.name);
    });

    var prog = document.getElementById("upload-progress");
    if (prog) {
        prog.style.display = "block";
        prog.innerHTML = '<div class="upload-row">'
            + '<span class="material-symbols-outlined upload-spin" style="font-size:1em">sync</span>'
            + ' <span>Uploading ' + list.length + ' file' + (list.length === 1 ? "" : "s") + '…</span>'
            + '<div class="progress-track"><div class="progress-fill" id="upload-fill" style="width:0%"></div></div>'
            + '</div>';
    }

    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/media/upload");
    xhr.upload.onprogress = function (e) {
        if (e.lengthComputable) {
            var pct = Math.round((e.loaded / e.total) * 100);
            var fill = document.getElementById("upload-fill");
            if (fill) fill.style.width = pct + "%";
        }
    };
    xhr.onload = function () {
        var resp = null;
        try {
            resp = JSON.parse(xhr.responseText || "{}");
        } catch (err) {
            /* ignore */
        }
        _renderUploadResults(resp);
    };
    xhr.onerror = function () {
        if (prog) prog.style.display = "none";
        showToast("Upload failed — is the frame reachable?", "error");
    };
    xhr.send(form);
}

function _renderUploadResults(resp) {
    var prog = document.getElementById("upload-progress");
    if (!prog) return;

    var saved = (resp && resp.saved) ? resp.saved : [];
    var errors = (resp && resp.errors) ? resp.errors : [];

    if (saved.length === 0 && errors.length === 0) {
        prog.style.display = "none";
        showToast("Upload failed", "error");
        return;
    }

    var html = '<div class="upload-results">';
    if (saved.length) {
        html += '<div class="upload-result upload-result--ok">'
            + '<span class="material-symbols-outlined" style="font-size:1em;vertical-align:middle;color:var(--success)">check_circle</span> '
            + 'Saved ' + saved.length + ' file' + (saved.length === 1 ? "" : "s")
            + ' — they\u2019ll appear in the slideshow shortly.</div>';
    }
    if (errors.length) {
        html += '<div class="upload-result upload-result--err">'
            + '<span class="material-symbols-outlined" style="font-size:1em;vertical-align:middle;color:var(--danger)">error</span> '
            + errors.length + ' failed:</div><ul class="upload-errors">';
        errors.forEach(function (er) {
            html += '<li>' + escapeHtml(er.name || "file") + ' \u2014 ' + escapeHtml(er.error || "unknown error") + '</li>';
        });
        html += '</ul>';
    }
    html += '</div>';

    prog.innerHTML = html;
    prog.style.display = "block";

    if (saved.length) {
        showToast("Uploaded " + saved.length + " file" + (saved.length === 1 ? "" : "s"), "success");
        loadMedia();
        setTimeout(function () { prog.style.display = "none"; }, 8000);
    }
}

export { loadMedia };
