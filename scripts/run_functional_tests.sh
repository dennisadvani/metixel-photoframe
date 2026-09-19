#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
#
# Run the on-Pi functional (hardware) test suite against a Raspberry Pi.
#
# The suite exercises the real Wi-Fi/AP stack (nmcli, hostapd, dnsmasq) and
# passwordless sudo, so it must run ON the Pi as the `pi` user — not in CI.
#
# Usage:
#   scripts/run_functional_tests.sh <pi-host> [<pi-user>] [--wifi-only] [--allow-skips]
#
#   <pi-host>    IP or hostname of the Pi (e.g. 192.168.222.122)
#   <pi-user>    SSH user (default: pi)
#   --wifi-only  Skip the AP test (run only Wi-Fi + sudo in test mode)
#   --allow-skips  Accept suites where tests SKIPPED (default: a skip fails the
#                  run).  Only for hardware that cannot support a test.
#
# EXIT STATUS: non-zero if ANY suite failed, skipped anything, or collected no
# tests.  A skip is a failure by default because a skipped functional test
# proves nothing about the device — treating skips as success is what let a
# completely dead AP/DHCP stack report a green run.
#
# Prerequisites on the Pi:
#   * wlan0 Wi-Fi radio + an Ethernet uplink for control
#   * passwordless sudo for the user (pi ALL=(ALL) NOPASSWD: ALL)
#   * the backend/frontend services running (the tests hit the live API)
#   * testing/functional/.env with METIXEL_TEST_WIFI_SSID/PASSWORD
#
# The LATEST local tests are copied to a fresh tmp dir on the Pi and run from
# there — so you can iterate on the tests without syncing the whole repo.  Most
# tests talk to the RUNNING backend over HTTP (:8080) and read /run/metixel
# state files.  The Wi-Fi + AP tests additionally import `metixel.*` directly,
# so they run with PYTHONPATH=${METIXEL_SRC} pointing at the live checkout the
# services run from (the pip editable install's .pth can go stale after a
# Blue/Green release swap, since old release dirs are deleted).
#
# The Wi-Fi tests run with METIXEL_NETWORK_TEST_MODE=1 so Ethernet is ignored
# for connectivity (the Pi stays reachable over SSH).  The AP test runs in a
# SEPARATE invocation because starting hostapd takes wlan0 out of client mode.
set -euo pipefail

PI_HOST=""
PI_USER="pi"
WIFI_ONLY=0
#: Filesystem path to the metixel source package ON the Pi (the wifi/AP tests
#: import `metixel.*` directly).  Defaults to the canonical live checkout the
#: systemd services run from; override with METIXEL_SRC if the layout differs.
METIXEL_SRC="${METIXEL_SRC:-/opt/metixel/live/src}"
for arg in "$@"; do
    case "${arg}" in
        --wifi-only)
            WIFI_ONLY=1
            ;;
        --allow-skips)
            # Accept a suite where some tests skipped.  Only for hardware that
            # genuinely cannot support a test (no wlan0, no monitor on DDC);
            # otherwise a skip means the suite did not actually check anything.
            ALLOW_SKIPS=1
            ;;
        *)
            if [[ -z "${PI_HOST}" ]]; then
                PI_HOST="${arg}"
            elif [[ "${PI_USER}" == "pi" ]]; then
                PI_USER="${arg}"
            fi
            ;;
    esac
done
if [[ -z "${PI_HOST}" ]]; then
    echo "usage: run_functional_tests.sh <pi-host> [<pi-user>] [--wifi-only] [--allow-skips]" >&2
    exit 1
fi

# Local functional-test dir (the source of truth for the latest tests).
LOCAL_FUNC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../testing/functional" && pwd)"
LOCAL_ENV="${LOCAL_FUNC}/.env"

# Fresh tmp dir on the Pi to hold the copied tests.
REMOTE_TMP="/tmp/metixel-functional-$(date +%s)"
REMOTE_FUNC="${REMOTE_TMP}"

echo "==> Copying latest functional tests to ${PI_USER}@${PI_HOST}:${REMOTE_FUNC}"
ssh "${PI_USER}@${PI_HOST}" "mkdir -p ${REMOTE_FUNC}"

# Copy the latest local test files + conftest to the tmp dir.
scp "${LOCAL_FUNC}"/*.py "${PI_USER}@${PI_HOST}:${REMOTE_FUNC}/"

# Push the gitignored .env credentials if a local one exists (the tests need
# it, but it is not part of the git clone).
if [[ -f "${LOCAL_ENV}" ]]; then
    echo "==> Pushing local .env credentials"
    scp "${LOCAL_ENV}" "${PI_USER}@${PI_HOST}:${REMOTE_FUNC}/.env"
else
    echo "==> No local .env found — using the Pi's existing one (if any)"
fi

# ── Test invocations ───────────────────────────────────────────────────────
#
# Each suite runs in its own ssh session, and a FAILURE must fail this script.
#
# Why `run_suite` exists rather than a bare `ssh … pytest`:
#   * `set -e` DOES abort on a non-zero ssh, and ssh DOES propagate pytest's
#     exit code when it is the last command.  So a plain failure was never the
#     problem.
#   * The problem is SKIPS.  pytest exits 0 when every test in a suite was
#     skipped, so a suite that never ran (missing .env, missing smbclient, no
#     wlan0) reported exactly like a passing one.  That is how 50 skipped tests
#     looked like a green run while the AP/DHCP stack was broken.
#   * A whole-suite skip is therefore treated as a FAILURE by default.  Pass
#     --allow-skips (or set METIXEL_ALLOW_SKIPS=1) for the one legitimate case:
#     exercising a suite on hardware that genuinely cannot support it.
#
# `-p no:cacheprovider` keeps pytest from writing .pytest_cache into the tmp
# dir, and `-rs` prints the skip reasons so a skip is never silent.
ALLOW_SKIPS="${METIXEL_ALLOW_SKIPS:-0}"
FAILED_SUITES=()

# Run one suite over ssh.  Usage: run_suite <label> <remote-pytest-args…>
#
# Captures the output so an all-skipped suite can be detected: pytest exits 0
# whether every test PASSED or every test was SKIPPED, and only the output
# distinguishes them.  Every suite here is small, so buffering the text is
# cheaper than the alternative (misreading a dead suite as a healthy one).
run_suite() {
    local label="$1"
    shift
    echo ""
    echo "==> ${label}"

    local rc=0 out=""
    # The remote exit code IS pytest's: it is the last command, and nothing
    # follows it in the compound command.  Do not append an echo — that would
    # mask pytest's status with the echo's own 0.
    out="$(ssh "${PI_USER}@${PI_HOST}" \
        "cd ${REMOTE_FUNC} && ${REMOTE_PYTEST_PREFIX} python3 -m pytest $* -m functional -v --no-cov -rs -p no:cacheprovider" 2>&1)" \
        || rc=$?
    printf '%s\n' "${out}"

    if [[ "${rc}" -eq 5 ]]; then
        echo "    ! ${label}: NO TESTS COLLECTED (rc=5)"
        FAILED_SUITES+=("${label} — no tests collected")
        return 1
    fi
    if [[ "${rc}" -ne 0 ]]; then
        echo "    ! ${label}: pytest exited ${rc}"
        FAILED_SUITES+=("${label} — rc=${rc}")
        return 1
    fi

    # rc=0.  Distinguish "passed" from "everything skipped": pytest's summary
    # line is the only signal it gives us.  "N passed" with no "skipped" is a
    # real run; anything else means tests were silently inert.
    if [[ "${ALLOW_SKIPS}" == "1" ]]; then
        return 0
    fi
    local passed skipped
    passed="$(grep -oE '[0-9]+ passed' <<<"${out}" | tail -1 | grep -oE '[0-9]+' || echo 0)"
    skipped="$(grep -oE '[0-9]+ skipped' <<<"${out}" | tail -1 | grep -oE '[0-9]+' || echo 0)"
    if [[ "${skipped}" -gt 0 ]]; then
        echo "    ! ${label}: ${skipped} test(s) SKIPPED, ${passed} passed."
        echo "      A skipped functional test proves nothing about the device."
        echo "      Fix the prerequisite above, or pass --allow-skips to accept it."
        FAILED_SUITES+=("${label} — ${skipped} skipped, ${passed} passed")
        return 1
    fi
    if [[ "${passed}" -eq 0 ]]; then
        echo "    ! ${label}: no tests ran (summary had no 'passed' count)"
        FAILED_SUITES+=("${label} — nothing ran")
        return 1
    fi
    return 0
}

REMOTE_PYTEST_PREFIX=""

# NOTE: every call is suffixed with `|| true`.  run_suite deliberately returns
# non-zero for a failed/skipped suite, and under `set -e` an unchecked non-zero
# return would abort the WHOLE script — skipping the summary and the `exit 1`
# below, so the run would report success by dying quietly.  The failure is
# recorded in FAILED_SUITES instead; the exit status is decided at the end.
run_suite "Smoke test (running backend/frontend stack)" \
    test_smoke.py || true
run_suite "Core-experience tests (media scan, slideshow advance, config persistence)" \
    test_media.py test_config.py || true
run_suite "Immich sync test" \
    test_immich.py || true
run_suite "MQTT / Home Assistant test" \
    test_mqtt.py || true
#: systemd drop-in that puts the RUNNING BACKEND into network test mode.
#:
#: METIXEL_NETWORK_TEST_MODE only affects a process it is exported into.  The
#: Wi-Fi tests set it for their own pytest process, but the AP/captive-portal
#: tests talk to the BACKEND over HTTP — and that backend is a systemd service
#: that never sees the variable.  Without this drop-in its NetworkController
#: treats the Ethernet uplink as "connected" and correctly refuses to raise the
#: AP, so every captive-portal test skipped.
#:
#: A drop-in (not the unit) keeps the shipped unit untouched, and `systemctl
#: revert` removes it cleanly even if this script is interrupted.
TESTMODE_DROPIN="/etc/systemd/system/metixel-backend.service.d/zz-functional-test-mode.conf"

# Put the backend into network test mode (Ethernet ignored for connectivity, so
# the AP can be exercised while the Pi stays reachable over SSH).  Restores the
# previous state on exit, including on Ctrl-C.
backend_test_mode_on() {
    echo "==> Enabling backend network test mode (AP can run alongside Ethernet)"
    ssh "${PI_USER}@${PI_HOST}" "sudo -n mkdir -p \"\$(dirname ${TESTMODE_DROPIN})\" && \
        printf '[Service]\nEnvironment=METIXEL_NETWORK_TEST_MODE=1\n' | sudo -n tee ${TESTMODE_DROPIN} >/dev/null && \
        sudo -n systemctl daemon-reload && sudo -n systemctl restart metixel-backend" || {
        echo "    ! could not enable backend test mode — AP/portal tests will skip" >&2
        return 1
    }
    # The backend needs a moment to bind :8080 again; the suites poll /api/health
    # themselves, but waiting here keeps the failure (if any) attributable.
    local waited=0
    while [[ "${waited}" -lt 30 ]]; do
        if ssh "${PI_USER}@${PI_HOST}" "curl -fsS --max-time 3 http://127.0.0.1:8080/api/health >/dev/null 2>&1"; then
            echo "    backend restarted in test mode"
            # /api/health answers as soon as Flask binds, which is BEFORE the
            # network monitor has finished its boot sequence.  That sequence
            # sleeps 10s and then ticks, and a tick that still sees an upstream
            # connection reverts AP_ACTIVE -> CLIENT_CONNECTED and clears the
            # PIN.  If a suite raises the AP during that window the boot tick
            # silently tears it down mid-run: the first captive-portal test
            # passes and the rest fail with "No PIN active".  Wait out the
            # boot window so the monitor is quiescent before any test runs.
            local settle=20
            echo "    waiting ${settle}s for the network monitor to settle"
            sleep "${settle}"
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "    ! backend did not come back within 30s" >&2
    return 1
}

backend_test_mode_off() {
    echo ""
    echo "==> Disabling backend network test mode (restoring the shipped unit)"
    ssh "${PI_USER}@${PI_HOST}" "sudo -n rm -f ${TESTMODE_DROPIN}; \
        sudo -n systemctl daemon-reload; sudo -n systemctl restart metixel-backend" 2>/dev/null \
        || echo "    ! could not restore the backend — check it manually" >&2
}

run_suite "DDC/CI monitor control test" \
    test_ddc.py || true
run_suite "Device-password test (console + Samba password sync)" \
    test_device_password.py || true

# test_sudo/test_wifi need the network test mode flag and the source on the
# import path.
REMOTE_PYTEST_PREFIX="METIXEL_NETWORK_TEST_MODE=1 PYTHONPATH=${METIXEL_SRC}"
run_suite "Wi-Fi + sudo + network-message tests (test mode)" \
    test_sudo.py test_wifi.py || true
REMOTE_PYTEST_PREFIX=""

if [[ "${WIFI_ONLY}" -eq 1 ]]; then
    echo ""
    echo "==> Skipping AP tests (--wifi-only)"
else
    # test_ap.py asserts the AP is UP; test_ap_dhcp.py asserts a client can
    # actually USE it.  Both are needed: the 1.2.4/1.2.5 bug passed every
    # "is it up?" assertion while handing out no addresses at all.
    #
    # test_captive_portal.py lives HERE because its PIN tests require an ACTIVE
    # captive portal.  Unlike test_ap.py (which calls start_ap_mode() directly),
    # the portal tests ask the BACKEND for AP status, so the backend itself must
    # be in test mode — hence the drop-in below.
    REMOTE_PYTEST_PREFIX="PYTHONPATH=${METIXEL_SRC}"

    BACKEND_TEST_MODE_ON=0
    if backend_test_mode_on; then
        BACKEND_TEST_MODE_ON=1
        # Restore the backend even if the suite errors or the user hits Ctrl-C.
        trap 'backend_test_mode_off' EXIT INT TERM
    fi

    run_suite "AP functional tests (broadcast + DHCP lease + captive portal)" \
        test_ap.py test_ap_dhcp.py test_captive_portal.py || true

    if [[ "${BACKEND_TEST_MODE_ON}" -eq 1 ]]; then
        backend_test_mode_off
        trap - EXIT INT TERM
    fi
    REMOTE_PYTEST_PREFIX=""
fi

# Clean up the tmp dir on the Pi.
echo ""
echo "==> Cleaning up ${REMOTE_FUNC}"
ssh "${PI_USER}@${PI_HOST}" "rm -rf ${REMOTE_FUNC}"

# ── Result ─────────────────────────────────────────────────────────────────
echo ""
if [[ "${#FAILED_SUITES[@]}" -gt 0 ]]; then
    echo "==> Functional tests FAILED (${#FAILED_SUITES[@]} suite(s)):"
    printf '      - %s\n' "${FAILED_SUITES[@]}"
    echo "==> Functional tests complete"
    exit 1
fi

echo "==> Functional tests complete — all suites passed"