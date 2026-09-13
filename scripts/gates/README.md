# Hardware gates

Scripts for validating Metixel on real Raspberry Pi hardware. These are **not**
part of install, update or runtime — nothing in the application or the OTA
pipeline calls them. They exist so a hardware claim can be reproduced instead of
being taken on trust.

Run them on the Pi, with a display attached. `pi_gate2_smoke.sh` needs root.

| Script | Question it answers |
|---|---|
| `pi_gate1_devices.sh` | Which `/dev/video*` and DRM nodes exist, and what are they? |
| `pi_gate1_probe.sh` | What codec are the shipped sample videos, actually? |
| `pi_gate1_render_api.py` | Can the libmpv render API attach to a Qt canvas here? |
| `pi_gate1_make_hevc.sh` | Generate a genuine HEVC clip for measurement. |
| `pi_gate1_measure.sh` | **Which `hwdec` value really uses the hardware, and what does it cost?** |
| `pi_gate2_smoke.sh` | **Does the full stack come up on a display, under cage, and stay up?** |

## GATE-1: hardware video decode

Answers "which decoder is actually hardware?" by decoding a clip with each
candidate `hwdec` value and reading system-wide CPU from `/proc/stat` deltas.

The result is **different on every board**, so there is no safe default:

| Board | Codec | `drm-copy` | `v4l2m2m` | `auto` |
|---|---|---|---|---|
| Pi 5 | HEVC 1080p | **12.1% (HW)** | 49.2% (no device) | — |
| Pi 5 | H.264 1080p | 44–46% (SW, all values) | — | — |
| Pi 3 | H.264 720p | (SW) | **55.4% (HW)** | 170.1% |

Three findings that a single default would get wrong:

- `v4l2m2m` works on a Pi 3 but has no device on a Pi 5.
- `drm-copy` works on a Pi 5 but silently falls back to software on a Pi 3.
- On a Pi 3, `auto` is **worse than no hardware decode at all** (170% vs 138%).

A Pi 5 has no working H.264 decoder at all, which is why `PROFILES["pi5"]`
transcodes to H.265 — the only codec with a hardware path there.

This is why `hwdec_for_model()` in `src/metixel/shared/platform.py` selects per
board. **Do not collapse it to one value.**

Order matters: `pi_gate1_make_hevc.sh` → `pi_gate1_measure.sh`. Stop Metixel
first — a running frontend competing for the CPU is what invalidated the first
attempt at these numbers.

## GATE-2: full-stack smoke

Answers "does it actually run on a display?" — the things the desktop (tk)
backend cannot exercise: whether Qt acquires a Wayland surface under cage,
whether mpv attaches to the canvas FBO, whether the framing engine computes
geometry for the real panel, and whether the frontend crash-loops.

It starts services, observes them, and restores the state it found. It does not
edit config. Exit status is 0 only if every required stage passed; advisory
stages report without failing the run.

Read the journal if a stage fails — logging goes to stdout, so
`/opt/metixel/data/logs/metixel-frontend.log` is empty by design and
`journalctl -u metixel-cage` is where the output actually is.

## A note on measurement honesty

These scripts exist because two earlier attempts at GATE-1 produced confident,
wrong numbers: once because Metixel was running in the background, and once
because both shipped sample videos are H.264, so "HEVC" runs proved nothing about
HEVC. Both are now guarded (`pi_gate1_probe.sh` checks the codec, and the measure
script refuses to run on a busy system).

If you change how these measure, verify the script still fails when it should.
A gate that always passes is worse than no gate.
