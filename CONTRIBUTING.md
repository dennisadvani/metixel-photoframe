# Contributing to Metixel Photoframe

Thanks for considering a contribution! This guide covers how to set up a dev
environment, the **testing** story (Python unit tests + Playwright web UI
tests), and the workflow for getting changes merged.

> **Start here:** [`ARCHITECTURE.md`](ARCHITECTURE.md) is the single source of
> truth for the system design. Read it before proposing any change.

## Table of contents

- [Getting started](#getting-started)
- [Development workflow](#development-workflow)
- [Testing](#testing)
  - [Python unit tests](#python-unit-tests)
  - [Lint & type checks](#lint--type-checks)
  - [Web UI tests (Playwright)](#web-ui-tests-playwright)
- [Code style](#code-style)
- [Submitting changes](#submitting-changes)

## Getting started

1. **Read [`ARCHITECTURE.md`](ARCHITECTURE.md)** — complete system design,
   component relationships, and implementation roadmap.
2. **Check the [issues](https://github.com/dennisadvani/metixel-photoframe/issues)**
   for open tasks — especially anything labelled `good first issue`.
3. **Set up a dev environment** — see [Development setup (VS Code sync to Pi)](#development-setup-vs-code-sync-to-pi) below.

Metixel development targets a Raspberry Pi — the display backend (pi3d + Mesa)
doesn't run on a desktop. The workflow is to edit code on your workstation and
sync it to the Pi over SSH using the bundled VS Code tasks (`.vscode/tasks.json`
+ `.vscode/sync-to-pi.ps1`).

### Development setup (VS Code sync to Pi)

#### 1. Set up a Pi

First, install Metixel on a Pi using **Path A** or **Path B** in
[`docs/INSTALLATION.md`](docs/INSTALLATION.md).

#### 2. One-time SSH setup on your workstation

The sync workflow uses passwordless SSH from your VS Code PC to the Pi.
Do this once per PC:

1. **Generate an SSH key pair** (skip if you already have one):

   ```powershell
   ssh-keygen -t ed25519
   ```

2. **Install your public key on the Pi:**

   ```powershell
   type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh pi@<pi-ip> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
   ```

   Replace `<pi-ip>` with your Pi's IP address. Enter the Pi's password
   when prompted (this is the only time it's needed).

3. **Add the Pi to your known hosts** and verify the connection:

   ```powershell
   ssh pi@<pi-ip> "echo ok"
   ```

   Answer `yes` when asked to trust the host key. The sync script also
   passes `StrictHostKeyChecking=accept-new`, so first-run connections
   work even without this step — but verifying once here confirms your
   key is installed correctly.

4. **Verify passwordless sudo** works on the Pi:

   ```powershell
   ssh pi@<pi-ip> "sudo -n true && echo sudo-ok"
   ```

   The sync script uses `sudo rsync` to install files into the
   root-owned tree under `/opt/metixel`. The default Raspberry Pi OS
   `pi` user has passwordless sudo, so this normally just works. If it
   prints nothing, enable it with `sudo visudo` and add:
   `pi ALL=(ALL) NOPASSWD: ALL`.

#### 3. Point the tasks at your Pi

Edit `.vscode/tasks.json` and update the IP addresses in the `piHost`
input (search for `"id": "piHost"`) to match your Pi(s). The tasks
prompt you to pick a target Pi every time they run.

Then use the VS Code task runner (**Terminal → Run Task…** or
`Ctrl+Shift+P` → "Tasks: Run Task") — see **Development workflow** below.

## Development workflow

Use the VS Code task runner (**Terminal → Run Task…** or `Ctrl+Shift+P` →
"Tasks: Run Task"). The tasks prompt you to pick a target Pi every time they
run.

| Task | What it does |
|---|---|
| **[Pi] Sync Code (scp)** | Mirrors `src/metixel/` to `/opt/metixel/src/metixel/` on the Pi |
| **[Pi] Sync + Restart All (scp)** / **Backend (scp)** | Syncs, then restarts the systemd services |
| **[Pi] Run Tests** | Runs `pytest` on the Pi |
| **[Pi] Lint** / **[Pi] Type Check** | Quality checks on the Pi |
| **[Pi] Follow Logs** | Tails both services' journal |
| **[Local] Install Playwright (Web UI Test)** | One-time `npm install` + Chromium for the web tests |
| **[Local] Run Web UI Tests (Web UI Test)** | Runs the Playwright suite against the Pi you pick |
| **[Local] Run Functional Tests (Wi-Fi/AP/Sudo)** | Runs the on-Pi functional suite (smoke test, then Wi-Fi + sudo in test mode, then AP) against the Pi you pick |
| **[Local] Run Functional Tests (Wi-Fi + Sudo only)** | Runs only the smoke + Wi-Fi + sudo functional tests (skips the AP test) |
| **[Pi] Restart All / Backend / Frontend** | Quick service restarts without syncing |

> **Note:** the sync task only mirrors `src/metixel/`. Test files under
> `testing/` are **not** synced — copy them manually when you change tests:
>
> ```powershell
> scp -r testing/unit_tests/ pi@<pi-ip>:/opt/metixel/testing/unit_tests/
> ```

## Functional (hardware) tests — `testing/functional/`

The `testing/functional/` directory holds **on-Pi hardware tests** that
exercise the real Wi-Fi/AP stack (`nmcli`, `hostapd`, `dnsmasq`) and
passwordless sudo. They are deliberately excluded from the default
`testing/unit_tests/` run and from CI — they must run on a real Pi as the
`pi` user.

**Prerequisites on the Pi:**
- A Wi-Fi radio (`wlan0`) and an Ethernet uplink for control.
- Passwordless sudo: `pi ALL=(ALL) NOPASSWD: ALL`.
- The repo checked out at the live symlink (default `/opt/metixel/live`).
- A `testing/functional/.env` file with the test network credentials (copy
  `testing/functional/.env.example` and fill in `METIXEL_TEST_WIFI_SSID` /
  `METIXEL_TEST_WIFI_PASSWORD`). The `.env` is gitignored — never commit
  real credentials.

**Run them** (from this repo, against a Pi):

```bash
scripts/run_functional_tests.sh <pi-host> [<pi-user>] [--wifi-only]
```

The functional tests are **not synced** — they run from the Pi's own git
clone at the live symlink, so the code under test matches the installed
release. The script only pushes a local `testing/functional/.env` if one
exists (the credentials aren't part of the git clone). The Wi-Fi tests run
with `METIXEL_NETWORK_TEST_MODE=1`, which makes the controller **ignore
Ethernet for connectivity decisions** — so the Pi stays reachable over SSH
while the Wi-Fi radio is exercised. The AP test runs in a **separate
invocation** because starting hostapd takes `wlan0` out of client mode; pass
`--wifi-only` to skip it.

The suite skips itself (rather than failing) if the host isn't a Pi, has no
`wlan0`, lacks passwordless sudo, or has no `.env` credentials.

## Branching model

- **`dev`** is the integration branch — **all pull requests target `dev`**.
- **`main`** is the release branch — it is only ever updated by the release
  process (`scripts/release.ps1`), never directly.

When you open a pull request, set the base to **`dev`** (GitHub's default is
the default branch, so make the switch explicitly). **GitHub Actions CI runs
automatically on every pull request** — it runs `ruff check`, `ruff format`,
`mypy`, and `pytest` on Python 3.11 and 3.13. Fix any failing checks before
requesting review. The maintainer merges the PR into `dev` once the checks are
green and the review is approved; changes reach `main` only through a release.

## Testing

There are two layers of tests: **Python unit tests** (run on the workstation
venv and/or the Pi) and **Playwright web UI tests** (run from your workstation
headlessly against a **live frame**).

### Python unit tests

Unit tests live under `testing/unit_tests/`, mirroring the `src/metixel/`
package layout (`testing/unit_tests/backend|frontend|display|shared/`).

Run them locally on your workstation (the dev venv has pytest, mypy, numpy,
Pillow, Flask):

```powershell
# Windows (dev venv)
.\venv\Scripts\python.exe -m pytest testing/unit_tests/ -v

# On the Pi
cd /opt/metixel && python -m pytest testing/unit_tests/ -v
```

**Conventions:**

- Tests must **never touch real hardware, the network, or systemd**. Inject
  fakes that implement the port Protocols in
  `src/metixel/shared/ports.py` (they are `@runtime_checkable`, so
  `isinstance(fake, HttpGateway)` works).
- Hardware-dependent tests use `pytest.importorskip(...)`.
- Web route tests use the shared fixtures in
  `testing/unit_tests/backend/web/conftest.py` (real `create_app()` + mocked
  outbound dependencies).
- Target Python is 3.9+; the codebase is shared across Phase 1 (Raspberry Pi)
  and Phase 2 (other SBCs) — keep platform-specific logic behind the display
  backend abstraction.

### Lint & type checks

```bash
# Lint (ruff) — line-length 100, target py39
ruff check src/metixel/

# Type check (mypy)
mypy src/metixel/
```

Both are configured in `pyproject.toml` (dev dependencies: `ruff`, `mypy`,
`pytest`, `pytest-cov`). The project has a small pre-existing mypy baseline —
don't introduce *new* errors.

### Web UI tests (Playwright)

The `testing/web-tests/` folder is a **Playwright end-to-end suite** for the
web dashboard. It runs headless Chromium from your workstation and talks to
a **live frame's Flask backend** over the LAN (nginx on **port 80** proxying
to Flask on 8080) — no Pi-side tooling required.

**One-time setup:**

```powershell
cd testing/web-tests
npm install
npx playwright install chromium
```

Or use the **[Local] Install Playwright (Web UI Test)** VS Code task.

**Run the suite:**

```powershell
cd testing/web-tests
$env:METIXEL_URL = "http://192.168.222.122"   # or set in the VS Code task
npx playwright test
```

Or use the **[Local] Run Web UI Tests (Web UI Test)** VS Code task, which reuses the
`piHost` picker and sets `METIXEL_URL` to `http://${input:piHost}`.

**What it covers:**

| Spec | Verifies |
|---|---|
| `walk.spec.js` | Every top-level route loads with **no console/page/network errors** (the regression net — catches wrong API paths, broken imports, page-module failures) |
| `dashboard.spec.js` | Live stats populate, current media + playlist shown, prev/next/pause controls fire over IPC |
| `settings.spec.js` | All five save buttons work; slideshow/local-sync/video fields **save → persist → restore** |
| `sync.spec.js` | Immich fields load; Test Connection reports a result (Sync Now / Fetch Albums are not auto-triggered) |
| `network.spec.js` | Live network status loads; Wi-Fi country field present (no scan / AP toggle) |
| `media.spec.js` | Media library loads and the filters are present |
| `advanced.spec.js` | System info, server clock, timezone list, display save+restore, updates + keyboard sections |
| `destructive.spec.js` | **Opt-in only** (`npm run test:destructive`) — restart-services button (accepts the confirm dialog, waits for reconnect) |

**Safety notes:**

- Tests run **sequentially** (`workers: 1`) because they hit one real frame.
- Save tests **restore the original value** after verifying persistence, so the
  frame's config is left unchanged (a pipeline re-scan may briefly run for
  `sync`/`video`/`display` sections).
- `restart`/`reboot`/`shutdown`/`clear-cache` are **not** in the default suite.
  `destructive.spec.js` covers restart; reboot/shutdown take the Pi down and are
  best done manually.

**Options:**

- `METIXEL_URL` — the frame's dashboard URL (default `http://192.168.222.122`).
- `npx playwright test tests/walk.spec.js` — just the regression net.
- `npx playwright test --headed` — watch the browser.

Full details in [`testing/web-tests/README.md`](testing/web-tests/README.md).

## Code style

- **Python:** `ruff` is the canonical formatter and linter — run
  `ruff format` / `ruff check src/metixel/` before submitting. Follow the
  existing clean-architecture layout (`src/` + `typing.Protocol` ports in
  `src/metixel/shared/ports.py`, adapters in `src/metixel/shared/adapters.py`).
- **Line endings:** all repo files are **CRLF**. Normalise to CRLF when adding
  or editing files.
- **Web JS:** native ES6 modules, **no bundler, no build step, no frameworks**
  — one module per page under `src/metixel/backend/web/static/js/`, shared
  infra in `core.js`, entry point `main.js`. Keep the bundle under 200KB and
  bump the `?v=` cache-buster on `index.html` when editing `static/js/` or the
  stylesheet.
- **Web UI design:** burgundy-on-white design system — use the CSS design tokens
  (`var(--primary)`, `var(--text)`, …), Material Symbols icons (never emoji),
  and green only for backgrounds/accents. See the Web UI Style Guide in
  `CLAUDE.md` for the full ruleset.

## Submitting changes

1. Make your change, then run the full quality gate:
   ```bash
   ruff check src/metixel/     # lint
   mypy src/metixel/           # type check
   .\.venv\Scripts\python.exe -m pytest testing/unit_tests/ -v   # unit tests
   ```
2. If the change touches web UI behaviour, run the Playwright suite against a
   live frame (`cd testing/web-tests; npx playwright test`) to confirm the dashboard,
   save buttons and controls still work.
3. Verify on the Pi: **[Pi] Sync Code (scp)** + **[Pi] Sync + Restart All (scp)**, then
   **[Pi] Follow Logs** to confirm the services boot cleanly.
4. Update [`CHANGELOG.md`](docs/CHANGELOG.md) under `## [Unreleased]` — and
   `docs/` if the change affects user-facing behaviour, URLs, or the API.
5. Open a pull request. Keep changes focused — one concern per PR.

Thanks again — happy hacking!
