// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Media Library page module. Grid rendering, filters, paging and load-more.
 */

import {
    apiGet,
    apiPost,
    confirmDialog,
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
    /** Guard so the per-item "⋮" menu delegation is attached once. */
    var _mediaMenuBound = false;

    async function loadMedia() {
        _mediaOffset = 0;
        _mediaHasMore = false;
        _mediaLoading = false;

        var el = document.getElementById("media-list");
        el.innerHTML = '<p style="color:var(--text-muted)">Loading…</p>';

        var config = await apiGet("/config");

        // Populate folder filter dropdown from enabled watch paths only.
        // The media API only scans enabled paths, so a disabled folder
        // would always show 0 results and look broken to the user.
        if (config && config.sync && config.sync.local && config.sync.local.watch_paths) {
            var paths = config.sync.local.watch_paths;
            var sel = document.getElementById("media-filter-folder");
            if (sel) {
                // Keep the current choice across reloads (e.g. after an
                // upload) — it also decides where uploads go.
                var previous = sel.value;
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
                if (previous) sel.value = previous;
            }
        }
        _updateUploadState();

        await _fetchMediaPage(0);

        _bindUpload();
        _bindMediaMenus();
        _setupSambaHelp();
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
        _updateUploadState();
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
        // The folder choice also gates + targets uploads.
        document.getElementById("media-filter-folder")?.addEventListener("change", function () {
            _updateUploadState();
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
            // Per-item "⋮" menu. Files pulled in by an image sync (Immich)
            // are owned by the syncer — deleting one locally would just be
            // undone on the next sync — so they get no menu at all.
            var menuHtml = '';
            if (!item.synced) {
                menuHtml = '<div class="media-menu">'
                    + '<button type="button" class="media-menu-btn" aria-label="More actions for '
                    + escapeHtml(item.name) + '" aria-haspopup="menu" aria-expanded="false" title="More actions">'
                    + '<span class="material-symbols-outlined">more_vert</span></button>'
                    + '<div class="media-menu-dropdown" role="menu">'
                    + '<button type="button" class="media-menu-item media-menu-item--danger media-delete" role="menuitem">'
                    + '<span class="material-symbols-outlined">delete</span> Delete</button>'
                    + '</div></div>';
            }

            var div = document.createElement("div");
            div.className = "media-item";
            div.setAttribute("data-name", item.name);
            div.setAttribute("data-folder", item.folder || "");
            div.setAttribute("data-path", item.path || item.name);
            div.setAttribute("data-type", item.media_type || "image");
            div.innerHTML = menuHtml
                + thumbHtml
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

// -- Per-item "⋮" menu --------------------------------------------------

/** Close every open item menu (optionally all except ``keep``). */
function _closeMediaMenus(keep) {
    document.querySelectorAll(".media-menu.open").forEach(function (m) {
        if (m === keep) return;
        m.classList.remove("open");
        var b = m.querySelector(".media-menu-btn");
        if (b) b.setAttribute("aria-expanded", "false");
    });
}

/**
 * Wire the per-item "⋮" menus.  Delegated on #media-list so items rendered
 * by later pages ("Load more") and re-renders keep working without
 * re-binding.  Bound once per page lifetime.
 */
function _bindMediaMenus() {
    if (_mediaMenuBound) return;
    _mediaMenuBound = true;

    var list = document.getElementById("media-list");
    if (!list) return;

    list.addEventListener("click", function (e) {
        var toggle = e.target.closest ? e.target.closest(".media-menu-btn") : null;
        if (toggle) {
            e.preventDefault();
            e.stopPropagation();
            var menu = toggle.closest(".media-menu");
            var opening = !menu.classList.contains("open");
            _closeMediaMenus(menu);
            menu.classList.toggle("open", opening);
            toggle.setAttribute("aria-expanded", opening ? "true" : "false");
            return;
        }

        var del = e.target.closest ? e.target.closest(".media-delete") : null;
        if (del) {
            e.preventDefault();
            e.stopPropagation();
            _closeMediaMenus();
            var itemEl = del.closest(".media-item");
            if (itemEl) _deleteMediaItem(itemEl);
        }
    });

    // Click anywhere else / Escape closes any open menu.
    document.addEventListener("click", function (e) {
        if (e.target.closest && e.target.closest(".media-menu")) return;
        _closeMediaMenus();
    });
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") _closeMediaMenus();
    });
}

/**
 * Confirm and delete one library item.  On success the tile is removed
 * in place (no full reload — that would drop the user's scroll position
 * and any "Load more" pages) and the summary count is adjusted.
 */
async function _deleteMediaItem(itemEl) {
    var name = itemEl.getAttribute("data-name") || "this file";
    var folder = itemEl.getAttribute("data-folder") || "";
    var path = itemEl.getAttribute("data-path") || name;

    var ok = await confirmDialog("Delete " + name + "?", {
        title: "Delete media",
        okText: "Yes",
        danger: true
    });
    if (!ok) return;

    itemEl.classList.add("media-item--busy");
    var result = await apiPost("/media/delete", { folder: folder, path: path });
    if (result && result.status === "ok") {
        itemEl.remove();
        _mediaOffset = Math.max(0, _mediaOffset - 1);
        _adjustMediaSummary(itemEl.getAttribute("data-type"));
        showToast("Deleted " + name, "success");
    } else {
        itemEl.classList.remove("media-item--busy");
        var msg = (result && (result.message || result.error)) || "Failed to delete " + name;
        showToast(msg, "error");
    }
}

/**
 * Adjust the "N images, M videos" summary after an in-place removal so it
 * stays honest without a refetch.  ``mediaType`` is "image" or "video".
 */
function _adjustMediaSummary(mediaType) {
    var summary = document.querySelector("#media-list .media-summary");
    if (!summary) return;
    var word = mediaType === "video" ? "video" : "image";
    var re = new RegExp("(\\d+)\\s+" + word + "s?");
    var text = summary.textContent;
    var m = text.match(re);
    if (m) {
        var n = Math.max(0, parseInt(m[1], 10) - 1);
        text = text.replace(re, n + " " + word + (n === 1 ? "" : "s"));
    } else {
        // Fallback shape: "T files"
        text = text.replace(/(\d+)\s+files?/, function (_, t) {
            var n = Math.max(0, parseInt(t, 10) - 1);
            return n + " file" + (n === 1 ? "" : "s");
        });
    }
    summary.textContent = text;
}

// -- Upload target ------------------------------------------------------

var _UPLOAD_HINT_READY = "Tap to pick photos/videos, or drag & drop onto the grid";
var _UPLOAD_HINT_NEED_FOLDER = "Select a folder to enable Upload Media";

/** The watch-folder name chosen in the folder filter, or "" for All folders. */
function _selectedUploadFolder() {
    var sel = document.getElementById("media-filter-folder");
    return sel ? (sel.value || "") : "";
}

/** Human-readable path of the selected folder (the option's label). */
function _selectedUploadFolderLabel() {
    var sel = document.getElementById("media-filter-folder");
    if (!sel || !sel.value) return "";
    var opt = sel.options[sel.selectedIndex];
    return opt ? opt.textContent : sel.value;
}

/**
 * Uploads always go into the folder picked in the folder filter, so the
 * Upload button (and drag & drop) is disabled while "All folders" is
 * selected.  Called on load and whenever the folder filter changes.
 */
function _updateUploadState() {
    var btn = document.getElementById("btn-upload-media");
    var hint = document.getElementById("media-toolbar-hint");
    var folder = _selectedUploadFolder();
    if (btn) {
        btn.disabled = !folder;
        btn.title = folder
            ? "Upload into " + _selectedUploadFolderLabel()
            : _UPLOAD_HINT_NEED_FOLDER;
    }
    if (hint) {
        hint.textContent = folder
            ? "Uploads go to " + _selectedUploadFolderLabel() + " \u2014 " + _UPLOAD_HINT_READY
            : _UPLOAD_HINT_NEED_FOLDER;
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
            if (!_selectedUploadFolder()) {
                showToast(_UPLOAD_HINT_NEED_FOLDER, "info");
                return;
            }
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
            // No drop target highlight while uploads are disabled.
            if (_selectedUploadFolder()) list.classList.add("drop-active");
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
            if (!_selectedUploadFolder()) {
                showToast(_UPLOAD_HINT_NEED_FOLDER, "info");
                return;
            }
            if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
                _uploadFiles(e.dataTransfer.files);
            }
        });
    }
}

function _uploadFiles(files) {
    var list = Array.prototype.slice.call(files);
    if (!list.length) return;

    var folder = _selectedUploadFolder();
    if (!folder) {
        showToast(_UPLOAD_HINT_NEED_FOLDER, "info");
        return;
    }

    var form = new FormData();
    form.append("folder", folder);
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
        showToast((resp && (resp.message || resp.error)) || "Upload failed", "error");
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

// -- SMB media share helper -----------------------------------------------

var _sambaBound = false;

async function _setupSambaHelp() {
    var box = document.getElementById("media-smb");
    if (!box) return;

    // Bind the copy buttons once (delegated, so any future rows keep working).
    if (!_sambaBound) {
        _sambaBound = true;
        box.addEventListener("click", function (e) {
            var btn = e.target.closest ? e.target.closest(".media-smb-copy") : null;
            if (!btn) return;
            var code = btn.parentElement && btn.parentElement.querySelector("code");
            if (!code) return;
            var text = code.textContent.trim();
            if (!text || text === "\u2014") {
                showToast("No network IP yet — connect the frame to Wi-Fi or Ethernet first", "info");
                return;
            }
            _copyText(text);
        });
    }

    // Fill in the current-IP share addresses (the metixel.local rows are static).
    var ip = "";
    var status = await apiGet("/network/status");
    if (status && status.ip) ip = String(status.ip).trim();

    // The AP-fallback range (192.168.42.x) means there is no real network.
    // If the user reached the dashboard by IP, reuse that host for the share.
    if (!ip || ip.indexOf("192.168.42.") === 0 || ip === "127.0.0.1") {
        var host = window.location.hostname || "";
        ip = (host && host !== "localhost" && host.indexOf("metixel.local") !== 0) ? host : "";
    }

    var winIp = document.getElementById("media-smb-win-ip");
    var macIp = document.getElementById("media-smb-mac-ip");
    var curIp = document.getElementById("media-smb-ip");
    if (ip) {
        if (winIp) winIp.textContent = "\\\\" + ip + "\\metixel-media";
        if (macIp) macIp.textContent = "smb://" + ip + "/metixel-media";
        if (curIp) curIp.textContent = ip;
    } else {
        var dash = "\u2014";
        if (winIp) winIp.textContent = dash;
        if (macIp) macIp.textContent = dash;
        if (curIp) curIp.textContent = dash;
    }
}

/** Copy text to the clipboard (works on plain-http LAN pages too). */
function _copyText(text) {
    function _done() {
        showToast("Address copied — paste it into File Explorer or Finder", "success");
    }
    function _legacyCopy() {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        var ok = false;
        try {
            ok = document.execCommand("copy");
        } catch (_) {
            ok = false;
        }
        document.body.removeChild(ta);
        if (ok) _done();
        else showToast("Copy blocked by the browser — select the address and copy it manually", "info");
    }
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(_done, _legacyCopy);
    } else {
        _legacyCopy();
    }
}

export { loadMedia };
