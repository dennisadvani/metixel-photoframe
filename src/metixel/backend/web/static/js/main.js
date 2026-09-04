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
    setAuthRequiredHandler,
} from "./core.js";
import { loadDashboard } from "./dashboard-page.js";
import { loadSettings } from "./settings-page.js";
import { loadNetwork } from "./network-page.js";
import { loadSync } from "./sync-page.js";
import { loadMedia } from "./media-page.js";
import { loadAdvanced } from "./advanced-page.js";
import { showLogin, hideLogin } from "./login.js";

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

    // Logout action (in the nav drawer footer).
    document.getElementById("btn-logout")?.addEventListener("click", async function () {
        try {
            const res = await fetch("/api/auth/logout", {
                method: "POST",
                credentials: "same-origin",
            });
            if (res.ok) {
                window.location.reload();
            }
        } catch (err) {
            // Ignore — reload will re-evaluate auth state.
            window.location.reload();
        }
    });

    // -- Auth gate ----------------------------------------------------------

    // When any API call returns 401/403, show the login overlay.
    setAuthRequiredHandler(function () {
        showLogin(function () {
            window.location.reload();
        });
    });

    // On boot, check whether auth is enabled and the session is valid.
    async function bootAuthGate() {
        try {
            const res = await fetch("/api/auth/me", { credentials: "same-origin" });
            if (!res.ok) return;
            const me = await res.json();
            if (me.enabled) {
                // Show the logout action in the nav drawer.
                const logoutBtn = document.getElementById("btn-logout");
                if (logoutBtn) logoutBtn.style.display = "";
            }
            if (me.enabled && !me.authenticated) {
                showLogin(function () {
                    window.location.reload();
                });
            }
        } catch (err) {
            // Backend may be starting — the SPA will retry via apiGet.
        }
    }

    // -- Init ----------------------------------------------------------------

    // Register page loaders with the shared router (core.js).
    registerPage("dashboard", loadDashboard);
    registerPage("media", loadMedia);
    registerPage("sources", loadSync);
    registerPage("playback", loadSettings);
    registerPage("optimisation", loadSettings);
    registerPage("network", loadNetwork);
    registerPage("system", loadAdvanced);

    var hash = location.hash.substring(1);
    var validPages = ["dashboard", "media", "sources", "playback", "optimisation", "network", "system"];
    var startPage = validPages.indexOf(hash) >= 0 ? hash : "dashboard";
    navigateTo(startPage);

    // Support plain <a href="#page"> links anywhere (e.g. the welcome card).
    window.addEventListener("hashchange", function () {
        var p = location.hash.substring(1);
        if (validPages.indexOf(p) >= 0) navigateTo(p);
    });

    // Check auth state after the SPA has booted.
    bootAuthGate();
})();
