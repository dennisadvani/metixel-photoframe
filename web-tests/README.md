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

- **Install Playwright (Web UI tests)** — one-time dependency install.
- **Run Web UI Tests (Playwright)** — runs the suite against the Pi you
  pick (reuses the `piHost` picker from the other tasks).

## What it covers

| Spec | Verifies |
|---|---|
| `walk.spec.js` | Every top-level route loads with **no console/page/network errors** (the regression net — catches wrong API paths, broken imports, page-module failures) |
| `dashboard.spec.js` | Live stats populate, current media + playlist shown, prev/next/pause controls fire over IPC |
| `settings.spec.js` | All five save buttons work; slideshow/local-sync/video fields **save → persist → restore** |
| `sync.spec.js` | Immich fields load; Test Connection reports a result (Sync Now / Fetch Albums are not auto-triggered) |
| `network.spec.js` | Live network status loads; Wi-Fi country field present (no scan / AP toggle) |
| `media.spec.js` | Media library loads and the filters are present |
| `advanced.spec.js` | System info, server clock, timezone list, display save+restore, updates + keyboard sections |
- `destructive.spec.js` | **Opt-in only** (`npm run test:destructive`) — restart-services button (accepts the confirm dialog, waits for reconnect) |

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
- `npx playwright test tests/walk.spec.js` — just the regression net.
- `npx playwright test --headed` — watch the browser.
