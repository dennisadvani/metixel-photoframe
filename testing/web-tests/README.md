# Metixel Web UI Tests (Playwright)

End-to-end tests for the **web dashboard**, run from this workstation against a
**live frame** over the LAN. The browser runs locally (headless Chromium) and
talks to the frame's Flask backend — no Pi-side tooling required.

## Quick start

```powershell
# 1. One-time setup
npm install
npx playwright install chromium

# 2. Run the suite against a frame
$env:METIXEL_URL = "http://192.168.222.122"      # or set in the VS Code task (nginx on port 80)
npx playwright test
```

Or from VS Code (**Terminal → Run Task**):

- **[Local] Install Playwright (Web UI Test)** — one-time dependency install.
- **[Local] Run Web UI Tests (Web UI Test)** — runs the suite against the Pi you
  pick (reuses the `piHost` picker from the other tasks).

## What it covers

| Spec | Verifies |
|---|---|
| `walk.spec.js` | Every top-level route loads with **no console/page/network errors** (the regression net — catches wrong API paths, broken imports, page-module failures) |
| `dashboard.spec.js` | Live stats populate, current media + playlist shown, prev/next/pause controls fire over IPC |
| `settings.spec.js` | Playback + optimisation routes: slideshow/video/display save buttons; slideshow/video/local-sync fields **save → persist → restore**; image + transcode save fire |
| `sync.spec.js` | Immich fields load; Test Connection reports a result (Sync Now / Fetch Albums are not auto-triggered) |
| `network.spec.js` | Live network status loads; Wi-Fi country field present (no scan / AP toggle) |
| `media.spec.js` | Media library loads and the filters are present |
| `advanced.spec.js` | Split across new routes: **system** page (system info, updates + keyboard sections) and **playback** page (server clock, timezone list, display save+restore) |
| `security.spec.js` | Web-password login gate (set → gate → wrong/correct → clear), device-password validation, screen-PIN set/clear + length validation |
| `samba.spec.js` | Device-password & SMB share: set a throwaway device password, verify the Samba share is reachable from the workstation with it, then restore the original password and re-verify |
- `destructive.spec.js` | **Opt-in only** (`npm run test:destructive`) — restart-services button (accepts the confirm dialog, waits for reconnect) |

## Auth / SSH requirement

The suite's **global setup** (`global-setup.js`) guarantees the frame starts
with **no web password set** (auth disabled). If a password is already set
(e.g. from a previous interrupted run), it is cleared **out-of-band over SSH**
and the backend is restarted, so the dashboard is reachable without a login.

This requires **passwordless SSH** from this workstation to the frame:

```powershell
$env:METIXEL_HOST = "192.168.222.122"   # SSH host (defaults to the METIXEL_URL host)
$env:METIXEL_SSH_USER = "pi"            # SSH user (default "pi")
```

The `security.spec.js` tests are destructive — they set and clear the web
password, device password, and screen PIN — and restore the frame to a clean
state afterwards.

## Safety notes

- Tests run **sequentially** (`workers: 1`) because they hit one real frame.
- Save tests **restore the original value** after verifying persistence, so
  the frame's config is left unchanged (a pipeline re-scan may briefly run for
  `sync`/`video`/`display` sections).
- `restart`/`reboot`/`shutdown`/`clear-cache` are **not** in the default suite.
  `destructive.spec.js` covers restart; reboot/shutdown take the Pi down and
  are best done manually.

## Options

- `METIXEL_URL` — the frame's dashboard URL (default `http://192.168.222.122` — nginx on port 80 proxies to Flask).
- `METIXEL_HOST` — the frame's SSH host for the auth global setup (defaults to the `METIXEL_URL` host).
- `METIXEL_SSH_USER` — SSH user for the auth global setup (default `pi`).
- `npx playwright test tests/walk.spec.js` — just the regression net.
- `npx playwright test --headed` — watch the browser.
