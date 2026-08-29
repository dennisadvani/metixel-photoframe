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
#   scripts/run_functional_tests.sh <pi-host> [<pi-user>]
#
#   <pi-host>  IP or hostname of the Pi (e.g. 192.168.222.122)
#   <pi-user>  SSH user (default: pi)
#
# Prerequisites on the Pi:
#   * wlan0 Wi-Fi radio + an Ethernet uplink for control
#   * passwordless sudo for the user (pi ALL=(ALL) NOPASSWD: ALL)
#   * testing/functional/.env with METIXEL_TEST_WIFI_SSID/PASSWORD
#
# The Wi-Fi tests run with METIXEL_NETWORK_TEST_MODE=1 so Ethernet is ignored
# for connectivity (the Pi stays reachable over SSH).  The AP test runs in a
# SEPARATE invocation because starting hostapd takes wlan0 out of client mode.
set -euo pipefail

PI_HOST="${1:?usage: run_functional_tests.sh <pi-host> [<pi-user>]}"
PI_USER="${2:-pi}"
REMOTE_DIR="/tmp/metixel-functional"
FUNCTIONAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../testing/functional" && pwd)"

echo "==> Syncing functional/ to ${PI_USER}@${PI_HOST}:${REMOTE_DIR}"
ssh "${PI_USER}@${PI_HOST}" "mkdir -p ${REMOTE_DIR}"
rsync -az --delete \
    --exclude '.env' \
    "${FUNCTIONAL_DIR}/" "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/"

echo "==> Running Wi-Fi + sudo functional tests (test mode)"
ssh "${PI_USER}@${PI_HOST}" \
    "cd ${REMOTE_DIR} && METIXEL_NETWORK_TEST_MODE=1 python3 -m pytest test_sudo.py test_wifi.py -m functional -v"

echo "==> Running AP functional tests (separate invocation)"
ssh "${PI_USER}@${PI_HOST}" \
    "cd ${REMOTE_DIR} && python3 -m pytest test_ap.py -m functional -v"

echo "==> Functional tests complete"