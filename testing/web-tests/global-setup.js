// Global setup for the Metixel dashboard Playwright suite.
//
// Guarantees the frame starts with NO web password set (auth disabled) so
// the dashboard is reachable without a login.  If a password is already set
// (e.g. from a previous interrupted run), it is cleared out-of-band over SSH
// and the backend is restarted.  This makes the suite deterministic regardless
// of the frame's prior state.
const { clearWebPasswordAndRestart } = require("./ssh-utils");

module.exports = async function globalSetup() {
    console.log("[global-setup] Ensuring the frame has no web password set…");
    const ok = await clearWebPasswordAndRestart();
    if (!ok) {
        console.warn(
            "[global-setup] Backend did not become healthy after clearing the password. " +
                "The suite may fail if the frame is unreachable or still locked."
        );
    }
};