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
#   scripts/run_functional_tests.sh <pi-host> [<pi-user>] [--wifi-only]
#
#   <pi-host>    IP or hostname of the Pi (e.g. 192.168.222.122)
#   <pi-user>    SSH user (default: pi)
#   --wifi-only  Skip the AP test (run only Wi-Fi + sudo in test mode)
#
# Prerequisites on the Pi:
#   * wlan0 Wi-Fi radio + an Ethernet uplink for control
#   * passwordless sudo for the user (pi ALL=(ALL) NOPASSWD: ALL)
#   * the backend/frontend services running (the tests hit the live API)
#   * testing/functional/.env with METIXEL_TEST_WIFI_SSID/PASSWORD
#
# The LATEST local tests are copied to a fresh tmp dir on the Pi and run from
# there — so you can iterate on the tests without syncing the whole repo.  The
# tests talk to the RUNNING backend over HTTP (:8080) and read /run/metixel
# state files, so they don't need the repo checkout on the Pi.  Only the
# gitignored .env credentials file is pushed alongside them.
#
# The Wi-Fi tests run with METIXEL_NETWORK_TEST_MODE=1 so Ethernet is ignored
# for connectivity (the Pi stays reachable over SSH).  The AP test runs in a
# SEPARATE invocation because starting hostapd takes wlan0 out of client mode.
set -euo pipefail

PI_HOST=""
PI_USER="pi"
WIFI_ONLY=0
for arg in "$@"; do
    case "${arg}" in
        --wifi-only)
            WIFI_ONLY=1
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
    echo "usage: run_functional_tests.sh <pi-host> [<pi-user>] [--wifi-only]" >&2
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

echo "==> Running smoke test (running backend/frontend stack)"
ssh "${PI_USER}@${PI_HOST}" \
    "cd ${REMOTE_FUNC} && python3 -m pytest test_smoke.py -m functional -v --no-cov -p no:cacheprovider"

echo "==> Running core-experience tests (media scan → playlist, slideshow advance, config persistence, captive portal PIN)"
ssh "${PI_USER}@${PI_HOST}" \
    "cd ${REMOTE_FUNC} && python3 -m pytest test_media.py test_config.py test_captive_portal.py -m functional -v --no-cov -p no:cacheprovider"

echo "==> Running Immich sync test (separate invocation — downloads can take minutes and saturate the pipeline)"
ssh "${PI_USER}@${PI_HOST}" \
    "cd ${REMOTE_FUNC} && python3 -m pytest test_immich.py -m functional -v --no-cov -p no:cacheprovider"

echo "==> Running Wi-Fi + sudo + network-message functional tests (test mode)"
ssh "${PI_USER}@${PI_HOST}" \
    "cd ${REMOTE_FUNC} && METIXEL_NETWORK_TEST_MODE=1 python3 -m pytest test_sudo.py test_wifi.py -m functional -v --no-cov -p no:cacheprovider"

if [[ "${WIFI_ONLY}" -eq 1 ]]; then
    echo "==> Skipping AP tests (--wifi-only)"
else
    echo "==> Running AP functional tests (separate invocation)"
    ssh "${PI_USER}@${PI_HOST}" \
        "cd ${REMOTE_FUNC} && python3 -m pytest test_ap.py -m functional -v --no-cov -p no:cacheprovider"
fi

# Clean up the tmp dir on the Pi.
echo "==> Cleaning up ${REMOTE_FUNC}"
ssh "${PI_USER}@${PI_HOST}" "rm -rf ${REMOTE_FUNC}"

echo "==> Functional tests complete"