// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

/**
 * Metixel Photoframe Dashboard — Vanilla JS SPA (entry point).
 *
 * Wires the shared router (core.js) to the page modules and boots the SPA.
 * All page logic lives in the page modules; this file only orchestrates.
 */

import {
    closeDrawer,
    navigateTo,
    openDrawer,
    registerPage,
} from "./core.js";
import { loadDashboard } from "./dashboard-page.js";
import { loadSettings } from "./settings-page.js";
import { loadNetwork } from "./network-page.js";
import { loadSync } from "./sync-page.js";
import { loadMedia } from "./media-page.js";
import { loadAdvanced } from "./advanced-page.js";

(function () {
    "use strict";

    // -- Page Navigation ----------------------------------------------------

    document.querySelectorAll("nav a[data-page]").forEach((link) => {
        link.addEventListener("click", (e) => {
            e.preventDefault();
            const page = link.dataset.page;
            navigateTo(page);
            closeDrawer();
        });
    });

    // Burger menu toggle
    document.getElementById("btn-burger")?.addEventListener("click", function () {
        openDrawer();
    });
    document.getElementById("nav-close")?.addEventListener("click", function () {
        closeDrawer();
    });
    document.getElementById("nav-backdrop")?.addEventListener("click", function () {
        closeDrawer();
    });

    // -- Init ----------------------------------------------------------------

    // Register page loaders with the shared router (core.js).
    registerPage("dashboard", loadDashboard);
    registerPage("settings", loadSettings);
    registerPage("sync", loadSync);
    registerPage("media", loadMedia);
    registerPage("network", loadNetwork);
    registerPage("advanced", loadAdvanced);

    var hash = location.hash.substring(1);
    var validPages = ["dashboard", "media", "settings", "sync", "network", "advanced"];
    var startPage = validPages.indexOf(hash) >= 0 ? hash : "dashboard";
    navigateTo(startPage);
})();
