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
import { showLogin } from "./login.js";

(function () {
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

    var validPages = ["dashboard", "media", "sources", "playback", "optimisation", "network", "system"];

    // Deep links come in two shapes: plain pages (#media) and page+card
    // links (#media/media-library) that also flash the targeted card.
    function parseHash(raw) {
        var parts = raw.split("/");
        if (validPages.indexOf(parts[0]) < 0) return null;
        return { page: parts[0], card: parts.length > 1 ? parts[1] : null };
    }

    // Handle <a href="#page"> and <a href="#page/card"> clicks directly so a
    // second click on the same link still re-runs navigation/flash even when
    // the hash value is unchanged (no hashchange event would fire).
    document.addEventListener("click", function (e) {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        var link = e.target.closest ? e.target.closest('a[href^="#"]') : null;
        if (!link) return;
        var target = parseHash(link.getAttribute("href").substring(1));
        if (!target) return;
        e.preventDefault();
        navigateTo(target.page, target.card);
        closeDrawer();
    });

    // Back/forward and manually edited hashes re-run the same navigation.
    window.addEventListener("hashchange", function () {
        var target = parseHash(location.hash.substring(1));
        if (target) navigateTo(target.page, target.card);
    });

    // Boot on the deep-linked page/card (e.g. #media/media-library) if any.
    var bootTarget = parseHash(location.hash.substring(1));
    navigateTo(bootTarget ? bootTarget.page : "dashboard", bootTarget ? bootTarget.card : null);

    // Check auth state after the SPA has booted.
    bootAuthGate();
})();
