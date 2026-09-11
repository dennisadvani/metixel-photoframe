# Metixel Fixups

Versioned, one-time device-repair scripts that run during an OTA install.

## Why

Some device-level issues can't be fixed by a package install or a config file
change — e.g. an incorrect `gpu_mem=` in `/boot/firmware/config.txt`, a stale
sysctl, or a boot-config tweak. Fixups are small, self-contained scripts that
repair these on existing devices as part of an upgrade.

## Reconcile vs. fixup

| | `scripts/reconcile.sh` | `scripts/fixups/` |
|---|---|---|
| Runs | Every update | **Once ever** per device |
| Target state | Derivable from the repo | Depends on device history |
| Safe to correct later | Yes — just change the script | **No** — never runs again |
| Use for | Units, modules-load, ddcutil cache, WiFi powersave, Samba values, 80→8080 redirect, linger | One-way data migrations, destructive/ambiguous edits to user-owned files, **boot config** |

If you can write “the desired end state” declaratively, it belongs in
`reconcile.sh`.  If you need to know *what the device used to be* (e.g. “only
rewrite this value if it is specifically the old default”), it belongs here.

### Why boot config is NOT reconciled

`/boot/firmware/config.txt` is deliberately excluded from `reconcile.sh`, even
though it looks declarative:

- it is the **device's own file** — re-asserting `gpu_mem=128` on every update
  would silently override a value a user deliberately chose; and
- a change **only takes effect after a reboot**, so an unrelated update would
  schedule a behaviour change that manifests later, detached from its cause.

It is applied at provisioning (the fresh-install path in `scripts/update.sh`) and
by a one-time fixup (`v1.2.1-gpu-mem.sh`).  Both call the shared
`scripts/configure_boot.sh`, so there is still exactly one implementation.

### Retiring a fixup

Removing an entry from `manifest.txt` does **not** undo it: already-repaired
devices stay repaired, because `installed_fixups.json` records what has run.

### Current fixups

| Fixup | Why it is a fixup, not reconciliation |
|---|---|
| `v1.2.1-gpu-mem.sh` | `config.txt` is the device's own file and needs a reboot — re-asserting it every update would override a user's choice. |
| `v1.3.0-retire-logging-conf.sh` | One-way removal. `data/etc/logging.conf` is gone from the repo, so a device that never had it is indistinguishable from one already cleaned — the target state cannot be derived. |

## How it works

- Each fixup is a script in this directory, named by the version that
  introduced it, e.g. `v1.2.1-gpu-mem.sh`.
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
