// SPDX-License-Identifier: Apache-2.0
// Metixel Chromium Kiosk Prototype — slideshow engine.
//
// Drives the two crossfade layers, applies the virtual matte (via CSS
// object-fit: contain), renders the screen-based UI overlay, shows
// notification toasts, and reports FPS via benchmark.js.

import { startBenchmark, reportBenchmark } from "./benchmark.js";

// ── Configuration ────────────────────────────────────────────────────────

const CONFIG = {
    imageDurationMs: 5000, // how long each image stays up
    crossfadeMs: 1000, // matches the CSS transition duration
    videoDurationMs: 8000, // max time a video is shown before advancing
    toastIntervalMs: 15000, // how often to demo a notification
    uiClock: true, // show the on-screen clock
    uiCaption: true, // show the media caption
};

// ── DOM refs ─────────────────────────────────────────────────────────────

const currentLayer = document.getElementById("media-current");
const nextLayer = document.getElementById("media-next");
const clockEl = document.getElementById("ui-clock");
const captionEl = document.getElementById("ui-caption");
const toastEl = document.getElementById("toast");
const fpsEl = document.getElementById("fps");

// ── State ────────────────────────────────────────────────────────────────

let playlist = []; // [{name, url, kind}]
let index = 0;
let currentKind = null; // "image" | "video"
let currentVideo = null; // the active <video> element, if any
let advanceTimer = null;
let toastTimer = null;

// ── Media loading ────────────────────────────────────────────────────────

function fetchPlaylist() {
    return fetch("/media")
        .then((r) => r.json())
        .then((data) => {
            playlist = data.items || [];
            console.log(`[proto] playlist: ${playlist.length} items`);
        });
}

// Build an <img> or <video> element for a playlist item.
function buildMediaElement(item) {
    if (item.kind === "video") {
        const v = document.createElement("video");
        v.src = item.url;
        v.muted = true; // prototype is silent by design
        v.loop = false;
        v.playsInline = true;
        v.preload = "auto";
        // Autoplay: muted videos are allowed to autoplay without a gesture,
        // but we still call .play() explicitly to be safe.
        v.addEventListener("loadedmetadata", () => {
            v.play().catch((err) => {
                console.warn("[proto] video play() failed:", err);
            });
        });
        return v;
    }
    const img = document.createElement("img");
    img.src = item.url;
    img.alt = item.name;
    img.draggable = false;
    return img;
}

// Load an item into a layer and return the element (for video control).
function loadIntoLayer(layer, item) {
    layer.innerHTML = "";
    const el = buildMediaElement(item);
    layer.appendChild(el);
    return el;
}

// ── Slideshow loop ───────────────────────────────────────────────────────

function nextItem() {
    if (playlist.length === 0) return;
    index = (index + 1) % playlist.length;
    showItem(playlist[index]);
}

function showItem(item) {
    // Swap roles: the currently-active layer becomes the outgoing one,
    // the other becomes the incoming one.
    const outgoing = currentLayer.classList.contains("active")
        ? currentLayer
        : nextLayer;
    const incoming = outgoing === currentLayer ? nextLayer : currentLayer;

    // Load the new item into the incoming layer (under the outgoing one).
    const el = loadIntoLayer(incoming, item);
    currentKind = item.kind;
    currentVideo = item.kind === "video" ? el : null;

    // Update the UI caption.
    if (CONFIG.uiCaption) {
        captionEl.textContent = item.name;
    }

    // Crossfade: bring the incoming layer to full opacity.
    requestAnimationFrame(() => {
        incoming.classList.add("active");
        outgoing.classList.remove("active");
    });

    // Schedule the next advance.
    clearTimeout(advanceTimer);
    const duration =
        item.kind === "video" ? CONFIG.videoDurationMs : CONFIG.imageDurationMs;
    advanceTimer = setTimeout(nextItem, duration);
}

// ── Screen-based UI (clock) ──────────────────────────────────────────────

function updateClock() {
    if (!CONFIG.uiClock) return;
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");
    clockEl.textContent = `${hh}:${mm}`;
}

// ── Notifications ────────────────────────────────────────────────────────

function showToast(message) {
    toastEl.textContent = message;
    toastEl.hidden = false;
    // Force a reflow so the transition runs.
    void toastEl.offsetWidth;
    toastEl.classList.add("visible");
    setTimeout(() => {
        toastEl.classList.remove("visible");
        setTimeout(() => {
            toastEl.hidden = true;
        }, 400);
    }, 3000);
}

function scheduleToasts() {
    const messages = [
        "New photos synced from Immich",
        "Wi-Fi connected",
        "Battery low — 15%",
        "Weather update available",
    ];
    let i = 0;
    toastTimer = setInterval(() => {
        showToast(messages[i % messages.length]);
        i += 1;
    }, CONFIG.toastIntervalMs);
}

// ── Boot ─────────────────────────────────────────────────────────────────

async function boot() {
    await fetchPlaylist();
    if (playlist.length === 0) {
        console.warn("[proto] no media found");
        fpsEl.textContent = "no media";
        return;
    }
    showItem(playlist[0]);
    setInterval(updateClock, 1000);
    updateClock();
    scheduleToasts();
    startBenchmark(fpsEl, reportBenchmark);
}

boot();