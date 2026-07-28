#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
# =============================================================================
# Metixel Photoframe — Quiet Boot Configuration v3 (Trixie)
#
# Configures a Trixie Lite image for truly silent boot:
#   - Kernel output redirected to invisible VT3 (tty1 stays blank)
#   - No text, cursor, or login prompt on screen during boot
#   - Black screen from KMS init until Metixel BootLayer takes over
#   - Full revert mode to restore factory-default boot settings
#
# Proven approach (v3):
#   - console=tty3 → kernel/system messages go to invisible virtual terminal
#   - loglevel=0    → only KERN_EMERG survives (strongest suppression)
#   - getty masked  → no login prompt on tty1
#   - Systemd/journactl drop-ins for belt-and-suspenders silence
#
# Usage:
#   sudo bash quiet_boot.sh /path/to/mounted/rootfs          # Apply quiet boot
#   sudo bash quiet_boot.sh --revert /path/to/mounted/rootfs  # Restore factory defaults
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
MODE="apply"
ROOTFS=""

for arg in "$@"; do
    case "${arg}" in
        --revert|-r|revert)
            MODE="revert"
            ;;
        *)
            if [ -z "${ROOTFS}" ]; then
                ROOTFS="${arg}"
            fi
            ;;
    esac
done

if [ -z "${ROOTFS}" ]; then
    echo "Usage: sudo bash quiet_boot.sh [--revert] /path/to/mounted/rootfs"
    exit 1
fi

BOOT_DIR="${ROOTFS}/boot/firmware"
if [ ! -d "${BOOT_DIR}" ]; then
    echo "Error: ${BOOT_DIR} not found. Is the rootfs mounted?"
    exit 1
fi

# ===========================================================================
# Helper: add a line to config.txt if not already present (idempotent)
# ===========================================================================
add_config_line() {
    local file="$1" setting="$2"
    if ! grep -q "^${setting}" "${file}" 2>/dev/null; then
        echo "${setting}" >> "${file}"
        echo "  + ${setting}"
    else
        echo "  = ${setting} (already present)"
    fi
}

# ===========================================================================
# Helper: remove a line from config.txt if present
# ===========================================================================
remove_config_line() {
    local file="$1" setting="$2"
    if grep -q "^${setting}" "${file}" 2>/dev/null; then
        sed -i "/^${setting}/d" "${file}"
        echo "  - Removed ${setting}"
    else
        echo "  = ${setting} (not present)"
    fi
}

# ===========================================================================
# Helper: strip known quiet-boot params from a cmdline string
# ===========================================================================
strip_quiet_params() {
    local cmd="$1"
    local params=(
        "quiet"
        "splash"
        "loglevel=[0-9]"
        "logo\.nologo"
        "vt\.global_cursor_default=[0-9]"
        "fsck\.mode=[a-z]+"
        "consoleblank=[0-9]+"
        "systemd\.show_status=[0-9]"
        "fbcon=map:[0-9]"
        "drm\.debug=[0-9]"
        "plymouth\.ignore-serial-consoles"
    )
    for param_re in "${params[@]}"; do
        cmd=$(echo "${cmd}" | sed -E "s/\b${param_re}\b//g")
    done
    echo "${cmd}" | tr -s ' ' | sed 's/^ //' | sed 's/ $//'
}

# ===========================================================================
# APPLY MODE
# ===========================================================================
apply_quiet_boot() {
    echo "=== Metixel Quiet Boot v3 — APPLY (Trixie) ==="
    echo "RootFS: ${ROOTFS}"
    echo ""

    # -----------------------------------------------------------------------
    # 1. /boot/firmware/config.txt
    # -----------------------------------------------------------------------
    echo "[1/7] Configuring /boot/firmware/config.txt..."

    BOOT_CONFIG="${BOOT_DIR}/config.txt"
    [ -f "${BOOT_CONFIG}" ] || { echo "Error: ${BOOT_CONFIG} not found"; exit 1; }

    add_config_line "${BOOT_CONFIG}" "disable_splash=1"
    add_config_line "${BOOT_CONFIG}" "avoid_warnings=2"
    add_config_line "${BOOT_CONFIG}" "gpu_mem=16"

    # -----------------------------------------------------------------------
    # 2. /boot/firmware/cmdline.txt — the critical piece
    # -----------------------------------------------------------------------
    echo "[2/7] Configuring /boot/firmware/cmdline.txt..."

    CMDLINE="${BOOT_DIR}/cmdline.txt"
    [ -f "${CMDLINE}" ] || { echo "Error: ${CMDLINE} not found"; exit 1; }

    CMD=$(cat "${CMDLINE}")

    # --- Replace console=tty1 with console=tty3 ---------------------------
    # tty3 is never displayed — kernel/system messages go there, not to the
    # visible tty1.  tty1 stays blank until cage/Metixel draws on it.
    # console=serial0 is preserved for UART debugging.
    if echo "${CMD}" | grep -qE '\bconsole=tty1\b'; then
        CMD=$(echo "${CMD}" | sed -E 's/\bconsole=tty1\b/console=tty3/g')
        echo "  + console=tty1 → console=tty3 (kernel output hidden)"
    elif echo "${CMD}" | grep -qE '\bconsole=tty3\b'; then
        echo "  = console=tty3 already set"
    elif echo "${CMD}" | grep -qE '\bconsole=ttynull\b'; then
        CMD=$(echo "${CMD}" | sed -E 's/\bconsole=ttynull\b/console=tty3/g')
        echo "  + console=ttynull → console=tty3"
    else
        echo "  ! No console=tty1/tty3/ttynull found — adding console=tty3"
        CMD="console=tty3 ${CMD}"
    fi

    # --- Strip quiet params (idempotency) then append the new set ---------
    CMD=$(strip_quiet_params "${CMD}")

    QUIET_PARAMS="quiet loglevel=0 logo.nologo vt.global_cursor_default=0 fsck.mode=auto consoleblank=0"
    CMD="${CMD} ${QUIET_PARAMS}"
    echo "${CMD}" > "${CMDLINE}"
    echo "  + Appended: ${QUIET_PARAMS}"

    # -----------------------------------------------------------------------
    # 3. Mask getty@tty1
    # -----------------------------------------------------------------------
    echo "[3/7] Masking getty@tty1.service..."

    GETTY_LINK="${ROOTFS}/etc/systemd/system/getty@tty1.service"
    if [ -L "${GETTY_LINK}" ] && [ "$(readlink -f "${GETTY_LINK}")" = "/dev/null" ]; then
        echo "  = getty@tty1 already masked"
    else
        ln -sf /dev/null "${GETTY_LINK}"
        echo "  + getty@tty1 masked → /dev/null"
    fi
    rm -rf "${ROOTFS}/etc/systemd/system/getty@tty1.service.d" 2>/dev/null || true

    # -----------------------------------------------------------------------
    # 4. systemd manager drop-in
    # -----------------------------------------------------------------------
    echo "[4/7] Configuring systemd manager silence..."

    SYSCONF_DIR="${ROOTFS}/etc/systemd/system.conf.d"
    mkdir -p "${SYSCONF_DIR}"
    cat > "${SYSCONF_DIR}/10-metixel-quiet.conf" <<'SYSCONFEOF'
# Metixel Photoframe — suppress systemd manager output
[Manager]
ShowStatus=no
StatusUnitFormat=name
LogLevel=warning
SYSCONFEOF
    echo "  + ${SYSCONF_DIR}/10-metixel-quiet.conf"

    # -----------------------------------------------------------------------
    # 5. journald drop-in
    # -----------------------------------------------------------------------
    echo "[5/7] Configuring journald silence..."

    JOURNALD_DIR="${ROOTFS}/etc/systemd/journald.conf.d"
    mkdir -p "${JOURNALD_DIR}"
    cat > "${JOURNALD_DIR}/10-metixel.conf" <<'JOURNALDEOF'
# Metixel Photoframe — suppress journal console output
[Journal]
ForwardToConsole=no
ForwardToWall=no
MaxLevelConsole=emerg
JOURNALDEOF
    echo "  + ${JOURNALD_DIR}/10-metixel.conf"

    # -----------------------------------------------------------------------
    # 6. sysctl
    # -----------------------------------------------------------------------
    echo "[6/7] Configuring kernel console blanking..."

    SYSCTL_DIR="${ROOTFS}/etc/sysctl.d"
    mkdir -p "${SYSCTL_DIR}"
    cat > "${SYSCTL_DIR}/99-metixel.conf" <<'SYSCTLEOF'
# Metixel Photoframe — disable kernel console blanking
kernel.consoleblank=0
SYSCTLEOF
    echo "  + ${SYSCTL_DIR}/99-metixel.conf"

    # -----------------------------------------------------------------------
    # 7. Debug mode
    # -----------------------------------------------------------------------
    echo "[7/7] Setting up debug mode..."

    RC_LOCAL="${ROOTFS}/etc/rc.local"
    DEBUG_MARKER="# Metixel Photoframe: Check for debug mode"

    if [ -f "${RC_LOCAL}" ] && ! grep -qF "${DEBUG_MARKER}" "${RC_LOCAL}"; then
        if grep -q "^exit 0" "${RC_LOCAL}"; then
            sed -i '/^exit 0$/i\
# Metixel Photoframe: Check for debug mode\
if [ -f /boot/firmware/debug ] || [ -f /boot/debug ]; then\
    echo "Metixel Photoframe: Debug mode enabled"\
    /bin/dmesg -n 7\
    systemctl stop metixel-cage.service 2>/dev/null || true\
    systemctl unmask getty@tty1.service 2>/dev/null || true\
    systemctl start getty@tty1.service 2>/dev/null || true\
fi' "${RC_LOCAL}"
            echo "  + Debug mode hook added to rc.local"
        else
            echo "  ! rc.local has no 'exit 0' line — skipping debug hook"
        fi
    else
        echo "  = Debug mode already configured"
    fi

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    echo ""
    echo "=== Quiet Boot v3 Applied ==="
    echo ""
    echo "  config.txt:   disable_splash=1, avoid_warnings=2, gpu_mem=16"
    echo "  cmdline.txt:  console=tty3 (kernel output hidden), quiet loglevel=0,"
    echo "                logo.nologo, vt.global_cursor_default=0"
    echo "  systemd:      getty@tty1 masked, ShowStatus=no, journal console off"
    echo "  sysctl:       kernel.consoleblank=0"
    echo ""
    echo "  Result: Screen stays black from power-on until Metixel BootLayer."
    echo ""
    echo "  Debug mode: touch /boot/firmware/debug (or /boot/debug) then reboot."
    echo "  Revert:     sudo bash quiet_boot.sh --revert /"
}

# ===========================================================================
# REVERT MODE — restore factory-default boot settings
# ===========================================================================
revert_quiet_boot() {
    echo "=== Metixel Quiet Boot — REVERT to factory defaults (Trixie) ==="
    echo "RootFS: ${ROOTFS}"
    echo ""

    # -----------------------------------------------------------------------
    # 1. /boot/firmware/config.txt — remove Metixel additions
    # -----------------------------------------------------------------------
    echo "[1/6] Restoring /boot/firmware/config.txt..."

    BOOT_CONFIG="${BOOT_DIR}/config.txt"
    if [ -f "${BOOT_CONFIG}" ]; then
        remove_config_line "${BOOT_CONFIG}" "disable_splash=1"
        remove_config_line "${BOOT_CONFIG}" "avoid_warnings=2"
        remove_config_line "${BOOT_CONFIG}" "gpu_mem=16"
    else
        echo "  ! ${BOOT_CONFIG} not found — skipping"
    fi

    # -----------------------------------------------------------------------
    # 2. /boot/firmware/cmdline.txt — restore console=tty1, strip quiet
    # -----------------------------------------------------------------------
    echo "[2/6] Restoring /boot/firmware/cmdline.txt..."

    CMDLINE="${BOOT_DIR}/cmdline.txt"
    if [ ! -f "${CMDLINE}" ]; then
        echo "  ! ${CMDLINE} not found — skipping"
    else
        CMD=$(cat "${CMDLINE}")

        # Replace console=tty3 or console=ttynull back to console=tty1
        if echo "${CMD}" | grep -qE '\bconsole=tty3\b'; then
            CMD=$(echo "${CMD}" | sed -E 's/\bconsole=tty3\b/console=tty1/g')
            echo "  + console=tty3 → console=tty1 (restored)"
        elif echo "${CMD}" | grep -qE '\bconsole=ttynull\b'; then
            CMD=$(echo "${CMD}" | sed -E 's/\bconsole=ttynull\b/console=tty1/g')
            echo "  + console=ttynull → console=tty1 (restored)"
        else
            echo "  = console=tty1 already present (or no console= found)"
        fi

        # Strip all quiet-boot params
        CMD=$(strip_quiet_params "${CMD}")

        # Restore stock fsck behaviour
        CMD="${CMD} fsck.repair=yes"

        echo "${CMD}" > "${CMDLINE}"
        echo "  = cmdline.txt restored to stock"
    fi

    # -----------------------------------------------------------------------
    # 3. Unmask getty@tty1
    # -----------------------------------------------------------------------
    echo "[3/6] Restoring getty@tty1.service..."

    GETTY_LINK="${ROOTFS}/etc/systemd/system/getty@tty1.service"
    if [ -L "${GETTY_LINK}" ] && [ "$(readlink -f "${GETTY_LINK}")" = "/dev/null" ]; then
        rm -f "${GETTY_LINK}"
        echo "  + getty@tty1 unmasked"
    else
        echo "  = getty@tty1 not masked"
    fi
    rm -rf "${ROOTFS}/etc/systemd/system/getty@tty1.service.d" 2>/dev/null || true

    # -----------------------------------------------------------------------
    # 4. Remove systemd manager drop-in
    # -----------------------------------------------------------------------
    echo "[4/6] Removing systemd manager drop-in..."

    SYSCONF="${ROOTFS}/etc/systemd/system.conf.d/10-metixel-quiet.conf"
    if [ -f "${SYSCONF}" ]; then
        rm -f "${SYSCONF}"
        echo "  + Removed ${SYSCONF}"
        rmdir "${ROOTFS}/etc/systemd/system.conf.d" 2>/dev/null || true
    else
        echo "  = ${SYSCONF} not present"
    fi

    # -----------------------------------------------------------------------
    # 5. Remove journald drop-in
    # -----------------------------------------------------------------------
    echo "[5/6] Removing journald drop-in..."

    JOURNALD_CONF="${ROOTFS}/etc/systemd/journald.conf.d/10-metixel.conf"
    if [ -f "${JOURNALD_CONF}" ]; then
        rm -f "${JOURNALD_CONF}"
        echo "  + Removed ${JOURNALD_CONF}"
        rmdir "${ROOTFS}/etc/systemd/journald.conf.d" 2>/dev/null || true
    else
        echo "  = ${JOURNALD_CONF} not present"
    fi

    # -----------------------------------------------------------------------
    # 6. Remove sysctl + debug mode from rc.local
    # -----------------------------------------------------------------------
    echo "[6/6] Removing sysctl and debug mode..."

    SYSCTL_FILE="${ROOTFS}/etc/sysctl.d/99-metixel.conf"
    if [ -f "${SYSCTL_FILE}" ]; then
        rm -f "${SYSCTL_FILE}"
        echo "  + Removed ${SYSCTL_FILE}"
    else
        echo "  = ${SYSCTL_FILE} not present"
    fi

    RC_LOCAL="${ROOTFS}/etc/rc.local"
    DEBUG_MARKER="# Metixel Photoframe: Check for debug mode"
    if [ -f "${RC_LOCAL}" ] && grep -qF "${DEBUG_MARKER}" "${RC_LOCAL}"; then
        # Remove the debug block: from the marker line through the closing 'fi'
        sed -i "/${DEBUG_MARKER}/,/^fi$/d" "${RC_LOCAL}"
        echo "  + Debug mode removed from rc.local"
    else
        echo "  = Debug mode not present in rc.local"
    fi

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    echo ""
    echo "=== Factory Defaults Restored ==="
    echo ""
    echo "  config.txt:   disable_splash, avoid_warnings, gpu_mem removed"
    echo "  cmdline.txt:  console=tty1 restored, quiet params stripped,"
    echo "                fsck.repair=yes restored"
    echo "  systemd:      getty unmasks, drop-ins removed"
    echo "  sysctl:       99-metixel.conf removed"
    echo ""
    echo "  Reboot to apply. Boot messages and login prompt will be visible again."
}

# ===========================================================================
# Dispatch
# ===========================================================================
case "${MODE}" in
    apply)  apply_quiet_boot ;;
    revert) revert_quiet_boot ;;
esac
