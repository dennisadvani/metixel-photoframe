// SSH helpers for the Metixel dashboard Playwright suite.
//
// The web API is locked when a web password is set, so the only way to
// recover/clear it is out-of-band over SSH (the same path a user would use
// with `python -m metixel --clear-web-password`).  These helpers let the
// test suite guarantee a clean frame before running.
const { execSync } = require("child_process");
const http = require("http");

const HOST = process.env.METIXEL_HOST || "192.168.222.122";
const SSH_USER = process.env.METIXEL_SSH_USER || "pi";
const BASE = process.env.METIXEL_URL || `http://${HOST}`;

function ssh(cmd) {
    return execSync(
        `ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new ${SSH_USER}@${HOST} "${cmd}"`,
        { stdio: "pipe", timeout: 60000, encoding: "utf-8" }
    );
}

// Poll GET /api/health until it returns 200 or the timeout elapses.
function waitForHealth(timeoutMs) {
    return new Promise((resolve) => {
        const deadline = Date.now() + timeoutMs;
        const url = new URL("/api/health", BASE);
        function poll() {
            if (Date.now() > deadline) return resolve(false);
            const req = http.get(url, (res) => {
                res.resume();
                if (res.statusCode === 200) return resolve(true);
                setTimeout(poll, 2000);
            });
            req.on("error", () => setTimeout(poll, 2000));
            req.setTimeout(5000, () => {
                req.destroy();
                setTimeout(poll, 2000);
            });
        }
        poll();
    });
}

// Clear the web password on the frame (via SSH) and restart the backend so
// the in-memory config reloads immediately.  Returns true if the backend
// becomes healthy afterwards.
async function clearWebPasswordAndRestart() {
    try {
        // The backend service runs with PYTHONPATH=/opt/metixel/live/src (see
        // metixel-backend.service).  We must match that so we hit the live
        // release's --clear-web-password flag, not a stale editable install.
        // --clear-web-password uses data_dir()/config.json by default.
        ssh("sudo PYTHONPATH=/opt/metixel/live/src python3 -m metixel --clear-web-password");
        console.log("[ssh-utils] Web password cleared.");
    } catch (err) {
        console.warn("[ssh-utils] Could not clear web password (continuing):", err.message);
        return false;
    }
    try {
        ssh("sudo systemctl restart metixel-backend");
        console.log("[ssh-utils] Backend restarted.");
    } catch (err) {
        console.warn("[ssh-utils] Could not restart backend (continuing):", err.message);
    }
    return await waitForHealth(60000);
}

module.exports = { clearWebPasswordAndRestart, waitForHealth };