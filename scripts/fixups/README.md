# Metixel Fixups

Versioned, one-time device-repair scripts that run during an OTA install.

## Why

Some device-level issues can't be fixed by a package install or a config file
change — e.g. an incorrect `gpu_mem=` in `/boot/firmware/config.txt`, a stale
sysctl, or a boot-config tweak. Fixups are small, self-contained scripts that
repair these on existing devices as part of an upgrade.

## How it works

- Each fixup is a script in this directory, named by the version that
  introduced it, e.g. `v1.3.0-gpu-mem.sh`.
- `scripts/fixups/manifest.txt` lists the fixups in the order they must run
  (one filename per line, `#` comments allowed).
- `ota_install.sh` runs each fixup **exactly once per device**, tracking which
  have already run in `/opt/metixel/data/installed_fixups.json`.
- Fixups are **warn-and-continue**: a failure is logged but does not abort the
  update (a cosmetic repair shouldn't block a good upgrade).

## Writing a fixup

1. Create `scripts/fixups/<version>-<slug>.sh` — must be idempotent (safe to
   re-run) and exit 0 on success.
2. Add its filename to `scripts/fixups/manifest.txt`.
3. If the fixup needs a reboot to take effect, print a line starting with
   `REBOOT_REQUIRED` — the installer will surface it.

Example:

```bash
#!/usr/bin/env bash
# Fix incorrect GPU memory setting on Pi 2/3/Zero2W.
set -euo pipefail
BOOT="/boot/firmware/config.txt"
[ -f "$BOOT" ] || exit 0
if grep -q '^gpu_mem=' "$BOOT" && ! grep -q '^gpu_mem=128' "$BOOT"; then
    sed -i 's/^gpu_mem=.*/gpu_mem=128/' "$BOOT"
    echo "gpu_mem corrected to 128"
    echo "REBOOT_REQUIRED: gpu_mem change needs a reboot"
fi
exit 0
```
