#!/usr/bin/env bash
#
# Metixel fixup — repair the captive-portal AP on devices that cannot hand out
# DHCP addresses.
#
# THE BUG
# -------
# On 1.2.4 / 1.2.5 the frame broadcasts "Metixel-Setup" and the phone connects,
# but never receives an IP ("Obtaining IP address…" forever).  The AP is half
# configured: hostapd.conf exists, /etc/dnsmasq.conf has no dhcp-range.
#
# WHY THIS IS STILL A FIXUP AND NOT ONLY reconcile.sh
# --------------------------------------------------
# reconcile.sh §6 now converges the config, but "converged" is not the same as
# "repaired": the two defects have a DELICATE, ONE-WAY quality that makes the
# blanket fix unsafe to re-run.
#
#   1. Debian ships /etc/dnsmasq.conf as ~27 KB of COMMENTS.  Its content is a
#      mix of the package default and anything a user added.  Prepending the
#      conf-dir= line is safe to repeat, but only reconcile's marker check
#      knows that; a blind re-run here would stack duplicate header comments.
#   2. The SSID rename is one-way.  A device already renamed to
#      "Metixel-Setup-12ABC3" looks IDENTICAL to a device a user renamed by
#      hand — the target state cannot be derived without knowing that this
#      device used to run 1.2.4/1.2.5.
#
# Both are exactly the "depends on device history" case scripts/fixups/README.md
# describes.  reconcile.sh keeps the device converged from here on; this script
# performs the one-time translation of a device that broke under the old code.
#
# WHAT IT DELIBERATELY DOES NOT DO
# --------------------------------
#   * It does not start the AP, restart services, or touch the WiFi radio.
#     The backend owns AP lifecycle, and the radio is user-owned state — see
#     the "Radio enablement is NOT reconciled here" note in reconcile.sh.
#   * It does not rewrite /etc/dnsmasq.conf wholesale: it only ensures the
#     conf-dir= line is present and first, so a user's own directives survive.
#   * It does not invent a dhcp-range if dnsmasq is absent entirely; reconcile
#     §6 installs the package and writes the sidecar, and this script reports
#     what is still missing instead of guessing.
#
# Idempotent: safe to re-run (tracked exactly-once in installed_fixups.json).
set -euo pipefail

DNSMASQ_CONF="/etc/dnsmasq.conf"
DNSMASQ_SIDECAR="/etc/dnsmasq.d/metixel-ap.conf"
HOSTAPD_CONF="/etc/hostapd/hostapd.conf"
MARKER="conf-dir=/etc/dnsmasq.d/,*.conf"

changed=0

# ── 1. The AP values must be READ, and read first ───────────────────────────
# Debian's stock file ends with its own conf-dir= line, so a sidecar written by
# the old code was sourced AFTER the stock defaults — including `interface=`,
# which made it silently ineffective.  Order is the whole point.
if [ -f "${DNSMASQ_CONF}" ]; then
    if [ -f "${DNSMASQ_SIDECAR}" ] && ! grep -qE '^[[:space:]]*dhcp-range=' "${DNSMASQ_SIDECAR}"; then
        # A sidecar exists but carries no DHCP settings — that is the signature
        # of the broken release, and reconcile.sh will rewrite it.  Say so.
        echo "  sidecar exists without a dhcp-range: ${DNSMASQ_SIDECAR}"
        echo "  (reconcile.sh §6 rewrites it; this fixup only fixes file ORDER)"
    fi

    if ! grep -qE '^[[:space:]]*conf-dir=[[:space:]]*/etc/dnsmasq\.d/' "${DNSMASQ_CONF}"; then
        tmp="$(mktemp)"
        {
            printf '# Metixel Photoframe — read the captive-portal config first so\n'
            printf '# its values win over the stock defaults below.\n'
            printf '%s\n' "${MARKER}"
            cat "${DNSMASQ_CONF}"
        } > "${tmp}"
        install -m 0644 "${tmp}" "${DNSMASQ_CONF}"
        rm -f "${tmp}"
        echo "  added ${MARKER} to the top of ${DNSMASQ_CONF}"
        changed=1
    elif ! head -5 "${DNSMASQ_CONF}" | grep -qE '^[[:space:]]*conf-dir=[[:space:]]*/etc/dnsmasq\.d/'; then
        # Present but too late in the file for the AP values to win.
        tmp="$(mktemp)"
        {
            printf '%s\n' "${MARKER}"
            grep -vE '^[[:space:]]*conf-dir=[[:space:]]*/etc/dnsmasq\.d/,?\*?\.?c?o?n?f?$' \
                "${DNSMASQ_CONF}"
        } > "${tmp}"
        install -m 0644 "${tmp}" "${DNSMASQ_CONF}"
        rm -f "${tmp}"
        echo "  hoisted ${MARKER} to the top of ${DNSMASQ_CONF}"
        changed=1
    else
        echo "  ${DNSMASQ_CONF} already reads /etc/dnsmasq.d first"
    fi
else
    echo "  WARNING: ${DNSMASQ_CONF} is missing — reconcile.sh §6 installs dnsmasq"
fi

# ── 2. The AP SSID must be unique per device ────────────────────────────────
# The MAC-suffix rename is one-way, so it is applied here (once) rather than
# re-asserted forever.  Only the ssid= line is touched.
if [ -f "${HOSTAPD_CONF}" ]; then
    # Same pipeline as reconcile.sh's _ap_ssid_from_mac — kept in step by
    # testing/unit_tests/backend/test_ap_identity.py, which runs both.
    mac=""
    if [ -r /sys/class/net/wlan0/address ]; then
        mac="$(tr -d ' \n' < /sys/class/net/wlan0/address)"
    fi
    digits="$(printf '%s' "${mac}" | tr -d ':-' | tr '[:lower:]' '[:upper:]')"
    digits="$(printf '%s' "${digits}" | sed 's/[^0-9A-F]//g')"

    if [ "${#digits}" -ge 6 ]; then
        want_ssid="Metixel-Setup-$(printf '%s' "${digits}" | sed 's/.*\(.\{6\}\)$/\1/')"
        current_ssid="$(sed -n 's/^ssid=//p' "${HOSTAPD_CONF}" | head -1)"
        if [ "${current_ssid}" = "${want_ssid}" ]; then
            echo "  AP SSID already ${want_ssid}"
        elif [ -z "${current_ssid}" ]; then
            echo "  WARNING: ${HOSTAPD_CONF} has no ssid= line — leaving untouched"
        else
            sed -i "s|^ssid=.*|ssid=${want_ssid}|" "${HOSTAPD_CONF}"
            echo "  AP SSID ${current_ssid} -> ${want_ssid}"
            changed=1
        fi
    else
        echo "  WARNING: wlan0 MAC unreadable — leaving AP SSID unchanged"
    fi
else
    echo "  WARNING: ${HOSTAPD_CONF} is missing — reconcile.sh §6 writes it"
fi

# ── 3. Report, never guess ──────────────────────────────────────────────────
# A fixup is warn-and-continue, so the one thing it must not do is exit 0 while
# leaving a device that still cannot serve DHCP.  Re-read the effective config
# and name what is missing.
effective_range=""
for f in "${DNSMASQ_CONF}" "${DNSMASQ_SIDECAR}"; do
    if [ -f "${f}" ] && grep -qE '^[[:space:]]*dhcp-range=' "${f}"; then
        effective_range="${f}"
        break
    fi
done
if [ -n "${effective_range}" ]; then
    echo "  DHCP range present in ${effective_range}"
else
    echo "  WARNING: no dhcp-range found — the AP still cannot hand out addresses."
    echo "           Run: sudo bash /opt/metixel/live/scripts/reconcile.sh"
fi

if [ "${changed}" -eq 0 ]; then
    echo "  nothing to change (already repaired)"
fi

exit 0
