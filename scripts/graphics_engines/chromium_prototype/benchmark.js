// SPDX-License-Identifier: Apache-2.0
// Metixel Chromium Kiosk Prototype — FPS benchmark.
//
// Measures the actual rendered frame rate via requestAnimationFrame and
// reports it to the server's POST /api/benchmark collector. The readout
// element is updated in place so the kiosk shows live FPS.

// ── Configuration ────────────────────────────────────────────────────────

const REPORT_INTERVAL_MS = 5000; // how often to POST a sample
const SAMPLE_WINDOW_MS = 1000; // FPS is averaged over this window

// ── State ────────────────────────────────────────────────────────────────

let readoutEl = null;
let reportFn = null;
let frames = 0;
let windowStart = performance.now();
let lastReport = performance.now();

// ── Measurement loop ─────────────────────────────────────────────────────

function tick(now) {
    frames += 1;

    // Every second, compute the average FPS over the window.
    if (now - windowStart >= SAMPLE_WINDOW_MS) {
        const fps = (frames * 1000) / (now - windowStart);
        frames = 0;
        windowStart = now;

        if (readoutEl) {
            readoutEl.textContent = `${fps.toFixed(1)} fps`;
        }

        // Every REPORT_INTERVAL_MS, POST a sample to the server.
        if (now - lastReport >= REPORT_INTERVAL_MS) {
            lastReport = now;
            if (reportFn) {
                reportFn({ fps: Math.round(fps * 10) / 10 });
            }
        }
    }

    requestAnimationFrame(tick);
}

// ── Public API ───────────────────────────────────────────────────────────

/**
 * Start the FPS benchmark.
 *
 * @param {HTMLElement} readout  Element whose textContent is updated with FPS.
 * @param {(sample: object) => void} report  Callback invoked with each sample.
 */
export function startBenchmark(readout, report) {
    readoutEl = readout;
    reportFn = report;
    requestAnimationFrame(tick);
}

/**
 * POST a benchmark sample to the server's collector.
 *
 * @param {object} sample  e.g. { fps: 29.9 }
 */
export function reportBenchmark(sample) {
    fetch("/api/benchmark", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sample),
    }).catch((err) => {
        console.warn("[proto] benchmark report failed:", err);
    });
}