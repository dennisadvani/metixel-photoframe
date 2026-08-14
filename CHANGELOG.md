# Changelog

All notable changes to Metixel Photoframe will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.7-beta.7]

### Added

- **GitHub Actions CI** — `.github/workflows/ci.yml` runs `ruff check`,
  `ruff format --check`, `mypy`, and `pytest` on Python 3.11 and 3.13 for
  every pull request and push to `main`/`dev`.
- **Dependabot** — `.github/dependabot.yml` opens weekly dependency-update PRs
  for `pip` and the `web-tests/` npm packages.
- **CI status badge** — README now shows the workflow status (and license).
- **Widget base class** — `frontend/widgets/base.py` adds the documented
  `Widget` ABC (was missing, which left `clock.py` unimportable).

### Changed

- **Python 3.11 minimum** — `requires-python` raised to `>=3.11`; ruff and
  mypy targets bumped to 3.11; classifiers updated to 3.11–3.13.
- **Mypy now clean** — all pre-existing type errors fixed across the
  codebase (0 errors in 81 files, previously ~100).
- **Windows/local-dev fixes** — IPC client `AF_UNIX` guard, platform-aware
  watch-path and cache resolution, `nice`/`cpulimit` skipped on non-posix,
  cross-platform `shutil.disk_usage`, and Unix-only signal guards.
- **Video playback capability** — `DisplayBackend.supports_video` lets software
  renderers (TkBackend) opt out; the frontend filters videos out of the
  playlist with a single log instead of erroring every cycle.

## [1.1.6-beta.6]

### Added

- **Playwright web UI test suite** — `web-tests/` runs headless Chromium from a
  workstation against a live frame's dashboard (nginx port 80 → Flask) to
  verify the routes, save buttons, controls, and fields; wired in as VS Code
  tasks. Destructive actions are opt-in (`npm run test:destructive`).
- **`CONTRIBUTING.md`** — contribution guide covering dev setup, the testing
  workflow (Python unit tests + Playwright web UI tests), code style, and the
  change-submission checklist; linked from the README.
- **GitHub issue templates** — `.github/ISSUE_TEMPLATE/` adds structured bug
  report and feature request forms (Pi model, install method, OS, affected
  area, repro steps, logs) plus an issues-page config linking to the
  contributing guide.

## [1.1.5-beta.5]

### Changed

- **Web dashboard JS modularised** — the 2,961-line `dashboard.js` monolith split into native ES6 modules (`main.js` entry + `core.js` + one module per page); no bundler/build step.
- **Frontend presentation decomposition** — `presentation/engine.py` and `presentation/video_player.py` split into facades + mixins (`base`, `queue`, `scheduler`, `rendering`, `preload`, `video_state`); VLC and ffmpeg players extracted (`vlc_player.py`, `ffmpeg_player.py`).
- **Backend processing decomposition** — `processing/video.py` reduced to a facade; probe logic, ffmpeg command building, and frame extraction moved to `probe.py`, `ffmpeg_cmds.py`, `frames.py`.
- **Web routes decomposition** — the catch-all `routes/config.py` split into sub-resource modules (`system`, `time`, `input`, `control`, `health`, `browse`) registered on the same `/api/config` prefix (URL contract unchanged).
- **Shared system/GPU stats dedup** — new `shared/system_stats.py` consolidates 7 `/proc/meminfo` parsers, the duplicated `_log_resources()` loggers, and the 4 GPU-log formatting sites.
- **Shared platform detection dedup** — new `shared/platform.py` consolidates Pi-model detection and `vcgencmd get_mem` calls across the probe helpers, display backends, and the system-info endpoint.
- **Web API URLs restructured (breaking)** — config sub-resource routes moved from the `/api/config/*` umbrella to self-describing prefixes: `/api/system/*` (restart/reboot/shutdown/quiet-boot/info), `/api/time/*`, `/api/input/keyboard/*`, `/api/control`, `/api/health/*`, `/api/browse`. The old `/api/config/...` paths for these now return 404; the dashboard JS, tests, and `docs/API.md` were updated to match.

### Added

- **Unit tests** for the new processing seams (`tests/backend/test_video_processing.py`), the shared system-stats module (`tests/shared/test_system_stats.py`), and the shared platform module (`tests/shared/test_platform.py`).

## [1.1.4-beta.4]

### Changed

- Source folder clean up

## [1.1.3-beta.3]

### Added

- **Parametrized config default tests** — all 79 keys in ``DEFAULT_CONFIG`` now
  verified via a single parametrized test (``test_all_config_defaults``).
- **``Config.timeout()`` tests** — known-key lookup, missing-key fallback,
  zero/negative/string-value guards, float truncation, section-fill behaviour,
  and resilience when the ``timeouts`` section is absent.
- **``resolve_watch_paths()`` tests** — object format (enabled/disabled),
  relative path resolution, legacy flat-list format, mixed formats, and
  default watch-paths sanity check.
- **``MediaItem`` model tests** — ``aspect_ratio`` (including ÷0 safety),
  ``is_ready_to_play`` for all 5 transcode states × frame presence
  combinations, and all dataclass field defaults.  ``Album`` defaults
  also covered.
- **IPC tests** — ``ControlMessage`` JSON roundtrip for all 8 command types;
  ``IPCServer`` lifecycle (start/stop/poll/no-op on Windows, Unix domain
  socket create-and-cleanup).
- **``StateManager`` tests** — initialisation, atomic config persistence,
  ``config.updated`` flag file, playlist add/dedup/remove/get, deep-copy
  safety, and nested-directory creation.
- **Web API config route tests** — full ``GET /api/config``, all 13 section
  endpoints verify 200 status and correct defaults, unknown section returns
  404, and ``/api/config/video/profiles`` structure.
- **Test suite:** 73 → 256 tests (+183, +251 %).

### Changed

- **``requirements-phase1.txt`` → ``requirements-pip.txt``** — renamed to be
  generic (not Phase‑1‑specific).  References updated in ``CLAUDE.md``,
  ``scripts/build_phase1.sh``, and ``scripts/setup_trixie_metixel.sh``.
- **Web UI mobile layout** — small inline buttons (browse, Fetch Albums, Set
  timezone, watch‑path add/remove) no longer stretch to 100 % width on narrow
  screens.  Fixed via ``.btn--sm`` / ``.btn-browse`` / ``.watch-path-row button``
  exceptions in the mobile media query.
- **Sync status display** — success state changed from green ``✅ Success`` to
  plain white ``Success``.  Cancelled and error states also stripped of emoji
  icons for a cleaner look.
- **Footer** — ``--`` placeholder removed; uptime dash only appears after JS
  populates the value.

### Fixed

- **Media Library folder filter always empty** — dropdown values stored the
  full config path (``media/sample_media/``) but the API returns
  ``item.folder`` as just the directory name (``sample_media``).  Filter
  used ``indexOf`` which never matched.  Fixed by populating the dropdown
  with folder names as values and filtering by direct ``===`` equality.
- **Flaky ``test_next_item_does_not_crash_with_video_in_queue``** —
  ``random.shuffle`` in ``set_queue()`` could reorder the queue so the
  video landed at index 1, triggering ``_video_launch()`` which called
  ``_advance()`` again when frame files didn't exist.  Test now disables
  shuffle for deterministic order.
- **Immich Sync page** — first card heading changed from ``Updates`` to
  ``Time`` (duplicate heading with the OTA Updates card).

## [1.1.0]

### Added

- **GPU memory introspection** — ``DisplayBackend.gpu_memory_info()`` and
  ``flush_gpu()`` methods.  ``Pi3dBackend`` reads ``vcgencmd get_mem`` and
  DRM ``bo_stats`` debugfs for V3D buffer object counts and heap usage.
  Periodic GPU memory logged every 30 s alongside CPU/memory stats.
  GPU state logged on texture allocation failure for diagnostics.
- **Guaranteed no‑black‑screen video architecture** — the last‑frame texture
  is fully loaded, uploaded to the GPU, and verified BEFORE VLC is launched.
  See `ARCHITECTURE.md` → "Video Playback Architecture" for the 8‑step
  state machine diagram.
- **VLC RC TCP playback detection** — VLC is launched with ``--extraintf rc
  --rc‑host localhost:<port>`` (LUA CLI).  ``is_playing`` now queries VLC's
  TCP interface for a real "is rendering" signal instead of guessing with
  timers.  Supports Pi 2's slow VLC startup without premature swap.
- **Centralised timeout configuration** — new ``timeouts`` section in
  ``config.json`` with ``Config.timeout(key, fallback)`` helper.  All
  critical timeouts (ffprobe, frame extraction, thumbnail generation,
  transcode, VLC start) now editable in one place.

### Changed

- **GPU memory raised to 128 MB for Pi 2/3** — setup script now detects Pi
  model and sets ``gpu_mem=128`` for Pi ≤3 (static GPU partition needs room
  for framebuffer ~8 MB + pi3d textures ~4 MB each).  Pi ≥4 stay at
  ``gpu_mem=16`` (CMA dynamic allocation).
- **Timeout increases across the board** for CPU‑starved Pi 2/3 hardware:
  ffprobe metadata probe 30→120 s, cached‑video validation 15→60 s,
  thumbnail extraction 120→300 s, first‑frame extract 60→180 s, HW codec
  detection 10→30 s, VLC start 5→30 s.
- **Last‑frame swap timer** now starts from VLC's confirmed playback time
  (via RC interface), not subprocess launch.  Eliminates the swap‑before‑
  VLC‑appears race on slow hardware.

### Fixed

- **Black last‑frame screen (root cause)** — pi3d ``Texture(file_path)`` does
  NOT eagerly create the GL texture (``opengl_loaded=False``).  You must
  call ``tex.load_opengl()`` followed by ``glFinish()`` (ctypes → libGLESv2)
  to drain the VideoCore IV DMA pipeline before pi3d's ``free_after_load``
  releases the CPU buffer.  Without the flush, DMA reads freed memory →
  black pixels.
- **``_load_texture_for_slot`` unload‑before‑load** — the old texture was
  destroyed before the new one was confirmed loaded.  If the new load
  failed (GPU memory full), the slot went permanently black.  Now loads
  first, only unloads old on success.
- **Cache hash mismatch** — ``_cleanup_cached_video`` used a path‑based
  hash to find frame files, but ``_extract_video_frames`` named them with
  a content‑based hash.  Frame files were never cleaned up on re‑transcode.
  Both now use the content hash (``file_hash``).
- **Orphaned frame files never deleted** — folder watcher's
  ``_cleanup_cached_for_deleted`` was missing the ``.jpg`` extension when
  looking for ``{hash}.1.frame`` files (should be ``{hash}.1.frame.jpg``).
- **Thumbnails deleted on cache invalidation** — folder watcher cleanup
  was deleting thumbnails alongside cached videos.  Thumbnails now survive
  cleanup; they're only ~50 KB and regenerating them on every re‑transcode
  wastes CPU.
- **``_validate_cached_video`` crash** — still decorated ``@staticmethod``
  after adding ``self._timeout()`` call, causing ``NameError`` on every
  invocation.  All three precached videos silently failed validation and
  never reached the playlist.
- **``free_after_load`` kwarg conflict** — ``load_texture()`` hardcoded
  ``free_after_load=True`` while the engine passed ``free_after_load=False``
  via ``**kwargs``, causing ``TypeError: multiple values``.  ``load_texture``
  now pops the kwarg to let callers override the default.
- **``gpu_mem=16`` on Pi 3** — the setup script was applying 16 MB GPU
  memory to Pi 3 (which uses a static partition), leaving only 8 MB for
  textures after the framebuffer.  Videos rendered as black because the
  GPU couldn't allocate texture memory.
- **Keyboard defaults** — KEY_UP / KEY_DOWN removed (redundant), KEY_RIGHT
  corrected from ``prev`` → ``next``, KEY_SPACE and KEY_POWER removed.
  Defaults now: KEY_LEFT → prev, KEY_RIGHT → next, KEY_ENTER → toggle.

### Changed (2026-08-10)

- **``gpu_mem=128`` for all Pi models** — setup script simplified: Pi 4/5
  ignore ``gpu_mem`` via CMA dynamic allocation, so a single value avoids
  model‑detection complexity and keeps the base image portable across all
  Pi generations.
- **``requirements-system.txt`` now complete** — lists all 24 apt packages
  from the setup script (grouped by purpose) so the OTA updater can
  install any missing system dependencies after an update.

### Added

- **Per‑profile CRF field** — ``crf`` is now a first‑class profile setting
  (Pi 2/3: ``28`` for software decode, Pi 4/5: ``23`` for hardware decode).
  Exposed in the API, Web UI profile fields (locked for built‑in profiles,
  editable in Custom mode), and config as ``transcode_crf``.
- **Diagnostic logging** in ``needs_optimisation()`` — every check that
  triggers a transcode now logs exactly which limit was exceeded (codec,
  width, height, fps, bitrate, color depth, HDR, H.264 level) at INFO
  level for easy troubleshooting.
- **Workstation precache script** — ``scripts/precache_videos.py``
  transcodes videos on a fast desktop using the exact same profile,
  hash, and encoding logic as the Pi, then pushes results via SSH.
  Supports ``--host`` to pull media, ``--push`` to deploy.
- **``_VIDEO_WAITING`` state** in the video state machine — the 50 %
  last‑frame swap timer now starts after VLC confirms it has begun
  rendering (``MediaPlayerPlaying`` event), not at launch.  Prevents
  black frames when VLC startup is delayed by CPU contention.

### Changed

- **CRF replaces bitrate‑targeted encoding** — Pi 2/3 profiles now use
  ``-crf 28`` (was ``-b:v 8M`` ABR).  CRF distributes bits intelligently
  across simple and complex scenes, producing more decode‑friendly output
  than constant‑bitrate ABR.
- **Pi 2/3 max bitrate** lowered from ``8 → 7`` Mbps for more headroom
  below the ~8 Mbps ARM software decode ceiling.
- **FPS always explicit** — ``-r`` is now always set to
  ``min(source_fps, max_fps)``, preventing ffmpeg from silently
  upscaling 23.98 fps → 29.97 fps.
- **B‑frames restored** — removed ``-bf 0`` from transcode command.
  B‑frames break the P‑frame dependency chain and are actually easier
  to decode in software than a chain of pure P‑frames.
- **Max bitrate capped to source quality** — ``-maxrate`` now uses
  ``min(source_bitrate, profile_max)`` so a 5.5 Mbps source never
  gets upscaled to the 7 Mbps profile cap.
- **Frame extraction downscaled** — thumbnails, first frames, and last
  frames are now downscaled to the display resolution (same as image
  optimisation) instead of being extracted at the source's native 4K
  resolution, saving ~20 MB GPU memory per texture on low‑RAM Pis.
- **Frame file extension** — ``.1.frame`` / ``.2.frame`` → ``.1.frame.jpg`` /
  ``.2.frame.jpg`` for consistency with JPEG content.
- **Last‑frame swap** at 50 % of video duration (was 20 %, then 80 %)
  — balances VLC startup time with completing before VLC exits.
- **Web UI quality slider removed** — replaced by the per‑profile CRF
  numeric field in the profile settings section.

### Fixed

- **H.264 level comparison** — ffprobe returns level as integer (``40``
  for Level 4.0) but the profile stored it as string ``"4.0"``, causing
  ``float(40) > float("4.0")`` to always be true.  Probe now normalises
  to float (``40 → 4.0``).
- **Folder watcher silently dropping videos** — ``ffprobe`` timeout was
  ``10`` s, too short for a CPU‑starved Pi 2; raised to ``120`` s.
  Failures now log at WARNING level instead of DEBUG.
- **Thumbnail cache deleted on re‑transcode** — ``_cleanup_cached_video``
  was deleting thumbnails alongside corrupt cached videos; thumbnails
  now survive cache invalidation.
- **Frame extraction throttling reverted** — single‑frame extraction
  (thumbnails, first/last frames) now uses ``nice`` only (no
  ``cpulimit``) to avoid 120 s timeouts on slow hardware.
- **Last‑frame texture load race** — the old GPU texture is now kept
  until the new one is confirmed loaded, preventing a black screen if
  the upload fails.
- **ffmpeg 8.x compatibility** — ``-vframes`` ordering (after ``-i``),
  ``-update 1`` as muxer option (after ``-f image2``), ``-f mjpeg``
  for single‑frame output.
- **Media route filter** — ``.frame.jpg`` extension recognised for
  exclusion from media listings.

## [1.0.11-beta.4]

### Changed

- **Software video decode strategy** — Pi 2/3 now use CPU software decode
  (libVLC) instead of GPU hardware decode.  ``gpu_mem`` returned to
  ``16`` MB (1.0.10-beta.3 briefly raised it to ``128``) so more RAM is
  available to the ARM cores.  Pi 4/5 are unaffected — they continue to
  use hardware decode via ``rpi-hevc-dec`` / ``drm_avcodec``.
- **Pi 3 transcode bitrate** — ``max_bitrate`` lowered from ``20`` →
  ``8`` Mbps, matching the ~8 Mbps software decode ceiling of the
  Cortex‑A53 cores (measured: 5.6 & 7.7 Mbps play smoothly, ≥10.9 Mbps
  drops frames).
- **Bitrate‑targeted encoding for Pi 2/3** — profiles with
  ``bitrate_target: true`` now use ``-b:v {max_bitrate}M`` (target
  average bitrate) instead of ``-crf``.  CRF with ``-maxrate`` only caps
  peaks — the average can still overshoot by 30–50 %, exceeding the
  software decode ceiling.  ``-b:v`` produces predictable output at the
  target rate.
- **Setup script** — ``gpu_mem=16`` for all Pi models, enforced on both
  fresh installs and existing configs regardless of prior value.

### Fixed

- **Pi 3 choppy video at 10–22 Mbps** — CRF‑encoded files for
  ``14947567`` (10.9 Mbps) and ``17815074`` (21.5 Mbps) regularly
  exceeded the ARM software decode ceiling (~8 Mbps), causing frame
  drops.  Bitrate‑targeted encoding reliably produces ≤8 Mbps output.
- **Inconsistent ``gpu_mem`` on upgraded installs** — the 1.0.10-beta.3
  setup script would upgrade ``gpu_mem`` to ``128`` on existing installs;
  now reverted to ``16``.

## [1.0.10-beta.3]

### Fixed

- **Pi 3 hardware video decode broken by low GPU memory** — the setup
  script was setting ``gpu_mem=16`` MB which prevented VideoCore IV from
  loading the H.264 codec firmware, forcing VLC into 100 % CPU software
  decode even with correctly‑transcoded Level 4.0 files.  Raised to
  ``gpu_mem=128`` MB for Pi 2/3 (Pi 4/5 use kernel‑managed CMA and don't
  need this).
- **Infinite re‑transcode loop on Pi 5** — CRF encoding produced files
  ~3 Mbps above the profile bitrate limit, triggering a re‑transcode on
  every reboot that produced the same overshoot.  ``needs_optimisation()``
  now allows 10 % tolerance on bitrate checks.
- **Frame‑extraction ffmpeg processes not throttled** — first‑frame,
  last‑frame, and thumbnail extraction ran under ``nice`` only, ignoring
  the CPU throttle setting.  Now uses ``_wrap_with_throttle()`` so they
  also get ``cpulimit`` when CPU throttling is enabled.
- **Missing ``gpu_mem`` on Pi 4/5 Trixie images** — some images ship
  without any ``gpu_mem`` line in ``config.txt``; the setup script now
  adds ``gpu_mem=128`` when the setting is absent.

### Changed

- **Setup script GPU memory** — ``gpu_mem=128`` (was ``16``) for new
  installs; existing installs are auto‑upgraded from ``<128`` or have
  the setting added if missing entirely

## [1.0.9-beta.2]

### Added

- **USB keyboard / wireless remote input handler** — evdev-based listener
  with learn mode for custom key mapping; supports ``next``, ``prev``,
  ``pause``, ``resume``, ``toggle_pause``, ``screen_on``, and
  ``screen_off`` commands; mappings persisted in ``config.json`` under
  ``input.keyboard_map``
- **Keyboard/Remote Control card** in Web UI Advanced page — per‑command
  learn and clear buttons with live key‑code display; learn mode polls
  for the next keypress and persists the mapping automatically
- **``toggle_pause`` IPC command** — single‑key pause/resume toggle for
  keyboard remotes and the Web UI control endpoint; frontend shows a
  brief "Paused" / "Resumed" feedback popup on the frame display
- **``_show_feedback()`` helper** in the frontend renderer — on‑screen
  popup messages for pause, resume, and toggle_pause actions using the
  existing message layer
- **OTA system package support** — new ``requirements-system.txt`` lists
  required system packages (``python3-evdev``); ``UpdateManager``
  installs any missing packages before the pip step during OTA updates
- **``input`` config section** — ``keyboard_enabled`` toggle and
  ``keyboard_map`` dictionary for storing learned key bindings

### Changed

- **Web UI polling** — ``dashboard.js`` switched from ``setInterval`` to
  ``setTimeout`` chains for dashboard refresh, sync status, log viewer,
  and processing progress; prevents request‑queue buildup when the
  browser tab is backgrounded on mobile
- **``screen_on`` / ``screen_off`` renamed** — display power commands
  renamed from legacy names across all input handlers (CEC, IR,
  keyboard, MQTT), IPC protocol, frontend renderer, and the Web UI
  control endpoint for consistent naming
- **Pi 3 H.264 Level lowered** — ``h264_level`` reduced from ``4.2`` to
  ``4.0`` in the Pi 3 transcoding profile to stay within VideoCore IV
  hardware decode limits (max Level 4.1)
- **Keyboard handler thread** — started by the backend daemon alongside
  other input handlers; uses ``selectors``‑based blocking I/O instead of
  busy‑polling so CPU usage is near‑zero when no keys are pressed

### Fixed

- **Pi 3 video stuttering / 100 % CPU** — videos transcoded at H.264
  Level 4.2 exceeded VideoCore IV hardware decoder capabilities, causing
  VLC to fall back to software decode on all 4 cores; re‑transcoding at
  Level 4.0 enables hardware decode (~5–10 % CPU)
- **Keyboard learn clear button** — clearing a key mapping via the Web UI
  now properly removes the defaults for that command instead of wiping
  all default key bindings; ``set_key_map()`` merges config overrides
  with defaults, treating an empty list as "clear this command"


## [1.0.8-beta.1]

### Added

- **Transcoding profiles** — four Pi‑model‑specific profiles (Pi 2, Pi 3,
  Pi 4, Pi 5) with optimal codec, resolution, framerate, bitrate, H.264
  profile/level, colour depth, and HDR support limits.  Profile is
  auto‑detected on first run from ``/proc/device-tree/model``.
- **Custom profile mode** — allows overriding every transcode parameter
  individually; all profile fields visible in the Web UI, editable only
  when Custom is selected
- **Keep Audio global setting** — preserves the audio track when enabled
  (stripped by default)
- **Profile‑based cached‑video re‑validation** — switching profiles
  re‑probes existing cached videos and re‑transcodes any that exceed the
  new limits
- **Extended video metadata extraction** — ffprobe now captures framerate,
  bitrate, colour depth, H.264 profile/level, and HDR colour info
- **Profile‑based optimisation gating** — ``needs_optimisation()`` checks
  all profile limits (codec, resolution, fps, bitrate, colour depth, HDR,
  H.264 level) instead of only H.264 + resolution
- **Web UI Video Optimisation card** — profile dropdown with auto‑detect,
  custom parameter fields, keep‑audio checkbox, and global quality/encoder/
  timeout/CPU‑limit controls

### Changed

- **Transcode encodes to profile target codec** — Pi 4/5 target H.265
  (HEVC) for hardware decode; Pi 2/3 target H.264
- **Transcode enforces H.264 level/profile** — adds ``-level``,
  ``-profile:v``, ``-refs 2``, ``-bf 0``, ``-g 30`` for smooth Pi
  playback
- **Transcode framerate cap** — ``-r`` only applied when source FPS
  exceeds the profile limit; never upscales 30 fps → 60 fps
- **Transcode colour depth cap** — output depth is ``min(src, profile)``;
  never upscales 8‑bit → 10‑bit
- **Transcode HDR → SDR downgrade** — forces BT.709 colour space on
  non‑HDR‑capable Pi models
- **Transcode max bitrate enforcement** — ``-maxrate`` + ``-bufsize``
  applied from profile limits
- **libx265 RAM optimisation** — uses ``ultrafast`` preset on ≤3 GB
  devices (Pi 4/5 with 2 GB) to avoid OOM; ``superfast`` on >3 GB
- **CPU throttle default** — reduced from 200 % to 100 % (1 core)
- **Playlist hot‑reload refreshes metadata** — frontend now updates
  ``width``, ``height``, ``first_frame_path``, ``last_frame_path``, and
  ``cached_path`` for existing items when the backend updates the
  playlist, fixing aspect‑ratio mismatches and black frame glitches after
  re‑transcode

### Fixed

- **Next‑item video playback** — pressing Next to a video now launches VLC
  immediately instead of sitting on the first frame until the slide timer
  expires
- **``config.example.json`` video defaults** — ``playback_enabled``
  corrected to ``true``, ``max_duration_seconds`` to ``0``, CPU throttle
  to ``100``

## [1.0.7]

### Fixed

- **Welcome messages not auto-dismissing** — the message layer timer was
  being reset every frame while a video played, preventing the 2‑minute
  auto-dismiss from ever firing; now tracks accumulated visible time with
  proper pause/resume during video playback
- **Web UI welcome dismiss not clearing on‑screen messages** — dismissing
  the welcome banner in the dashboard now sends ``dismiss_all_messages``
  IPC to the frontend so the frame display popups disappear immediately
- **``config.example.json`` video defaults** — ``playback_enabled``
  corrected from ``false`` to ``true`` and ``max_duration_seconds`` from
  ``120`` to ``0`` (unlimited), matching the Python defaults

### Added

- **User Guide** — new ``docs/USER_GUIDE.md`` covering first‑time setup,
  getting online, adding media, the dashboard, customisation, updates, and
  troubleshooting

## [1.0.5-beta.5]

### Added

- **Pi 2 / Ethernet-only support** — ``is_wifi_hardware_present()`` check
  prevents the controller from attempting AP activation on devices without
  WiFi hardware; the controller stays in ``CLIENT_CONNECTED`` or
  ``CLIENT_DISCONNECTED`` based on Ethernet state and never retries AP
- **Stale AP cleanup on boot** — the controller now kills any leftover
  hostapd instance on initialisation, preventing the captive portal from
  blocking the web dashboard after an unclean shutdown

### Changed

- **AP startup delay eliminated** — removed the daemon's forced
  ``ap_timeout_seconds`` wait on boot; the controller now owns all timing
  (immediate AP when no saved networks, 5‑minute grace period when saved
  WiFi exists)
- **Boot screen message timing** — 10 s delay after slideshow start ensures
  the boot screen fade-out completes before welcome or PIN messages appear
- **Setup script prompts before install** — channel and WiFi country
  questions are now asked before git is installed; answers flow through
  to Phase 1 via environment variables so the user is never re‑prompted
- **Beta channel pins to pre-release tags** — the setup script now checks
  out the latest ``v*-beta.*`` tag on ``main`` for beta channel installs
  instead of tracking the ``dev`` branch, matching the OTA updater's
  behaviour

### Fixed

- **Captive portal blocking dashboard after reboot** — hostapd left running
  from a previous AP session is now stopped at controller init

## [1.0.4-beta.4]

### Added

- **WiFi connection failed popup** — when the captive portal WiFi connection
  fails (wrong password, out of range, etc.), an error message now appears on
  the photo frame display with guidance to check the password or try a
  different network (30 s auto-dismiss)
- **WiFi connection profile fallback** — if the one-shot ``nmcli device wifi
  connect`` fails with a "key-mgmt property is missing" error (common on
  routers with mixed WPA2/WPA3 or certain TP-Link/ASUS models), the code
  now creates an explicit connection profile with WPA2-PSK settings and
  retries automatically
- **Setup script channel prompt** — asks for ``stable`` or ``beta`` channel
  before installing; stable pins to the latest non-prerelease ``v*`` tag,
  beta tracks the ``dev`` branch
- **Setup script WiFi country prompt** — asks for the regulatory domain
  (e.g. ``AU``, ``US``, ``GB``) upfront alongside the channel choice; both
  answers are written to ``config.json`` for the Web UI

### Changed

- **Boot welcome delay eliminated** — the network monitor no longer waits
  ``ap_timeout_seconds`` when a network is already connected at boot; the
  welcome message appears as soon as the slideshow is ready instead of
  60+ seconds later
- **Captive portal error messaging** — the WiFi failure popup on the frame
  display uses a single clean message instead of concatenating the raw
  nmcli error with boilerplate text

### Fixed

- **Setup script CRLF line endings** — ``.gitattributes`` now enforces
  ``eol=lf`` for ``*.sh`` files; the setup script was renormalized so it
  runs correctly when downloaded from GitHub raw on a Pi

## [1.0.3-beta.3]

### Changed

- **Network Controller rewritten** — `NetworkPhase` flag-based state machine
  replaced with `NetworkState` enum (``CLIENT_CONNECTED``,
  ``CLIENT_DISCONNECTED``, ``AP_ACTIVE``, ``AP_EXHAUSTED``); all transitions
  go through a single ``_transition_to()`` method under lock; monitor loop
  drains a pending-actions queue instead of comparing phase snapshots.
  Ethernet connectivity is checked independently from WiFi and is always
  safe (different radio) — nmcli is never queried for WiFi while the AP
  is active, preventing hostapd beacon disruption.
- **WiFi connection deferred to background thread** — the captive portal's
  ``/api/network/connect`` endpoint now returns a response immediately
  (before the AP is torn down) and spawns a background thread for the
  actual AP-stop + scan + nmcli-connect sequence.  The phone receives the
  HTTP response while still associated with the AP.
- **WiFi scan delay after AP stop** — ``connect_to_network()`` now performs a
  ``nmcli device wifi rescan`` and waits 5 s after stopping the AP before
  attempting the connection.  Without a fresh scan wlan0 has no visible
  SSID list and nmcli fails with "No network with SSID 'X' found."
- **Controller connection guard** — new ``begin_connection()`` /
  ``end_connection()`` methods prevent the monitor thread from treating an
  intentionally-stopped AP as "unexpectedly dead" during the scan gap
- **AP_EXHAUSTED sudo reduction** — ``_stop_ap()`` now guarded by
  ``is_ap_mode_active()`` check; no longer runs unnecessary sudo commands
  on every monitor tick when the AP is already down

### Fixed

- **WiFi connection failing after captive portal** — the AP was torn down
  during the HTTP request, severing the client's TCP connection before the
  response arrived; the phone showed a network error instead of success
- **"WiFi Offline" popup flashing during WiFi connection** — the monitor
  tick saw the AP was down during the scan delay and marked it as
  ``AP_EXHAUSTED``, triggering an on-screen warning that was dismissed
  seconds later when the connection succeeded
- **Captive portal error messages removed** — the password field no longer
  displays error text on failure; all feedback is shown on the photo frame
  display where it is always visible even after the phone disconnects from
  the AP

### Removed

- **Legacy PIN state** — ``_pin_state`` module-level dict and all legacy PIN
  functions (``generate_ap_pin``, ``clear_ap_pin``, ``validate_ap_pin``,
  ``is_pin_required``, ``get_active_pin``) removed from
  ``network_manager.py``; PIN management is now exclusively owned by
  ``NetworkController`` with proper thread safety
- **``SCAN_CACHE_TTL``** constant and ``force_live`` parameter — scan cache
  is now served indefinitely while the AP is active; live scan toggle no
  longer exposed to the web API
- **Legacy PIN fallbacks in web routes** — all ``is_pin_required()`` and
  ``validate_ap_pin()`` fallback paths removed; the controller is the sole
  source of truth

## [1.0.2-beta.2]

### Added

- **CPU temperature tile** — sparkline graph on the System Status
  dashboard card using ``vcgencmd measure_temp``; scaled 0–85°C in red
- **``libopenblas0``** added to setup script package list — resolves NumPy
  import failures on Pi 2 (32-bit) where the shared library was missing
- **Hardware documentation** — separated Models table from RAM Requirements
  table in README; added 64‑bit vs 32‑bit image availability per model;
  Pi 4 promoted to Phase 1 (untested); Pi Zero 2 W marked as untested

### Changed

- **Video transcode CPU limit default** — reduced from 300 % to 200 %
- **Pause button** — switched from Unicode characters (⏸/▶) to Material
  Symbols icons (``pause`` / ``play_arrow``) for consistent rendering
- **Progress bar colours** — optimising images and transcoding bars now
  use ``var(--primary)`` (theme red), matching the scanning bar
- **WiFi Country Code** — hint text repositioned below the input field
  using ``form-group--stack`` layout
- **Connected button** — vertically centered in network list rows via
  ``display:inline-flex;align-items:center``
- **Background processing bars** — hidden when their respective feature
  is disabled (image optimisation off → hide optimising bar; video
  transcoding off → hide transcoding bar)
- **README** — hardware section restructured with separate Supported
  Models and RAM Requirements tables; Pi 2 marked active (32-bit manual
  install); Pi 4 marked untested; Pi Zero 2 W marked untested

### Fixed

- **Pipeline reset on sync changes** — enabling/disabling local watch
  folders now triggers a full pipeline reset (``"sync"`` re‑added to the
  config route trigger list)
- **Hardware docs consistency** — ``CLAUDE.md``, ``HARDWARE.md``,
  ``ARCHITECTURE.md``, ``GETTING_STARTED.md``, ``INSTALLATION.md``, and
  ``FEATURES.md`` updated to match README hardware tables (Pi 4 in
  Phase 1, Pi Zero 2 W untested, 32‑bit vs 64‑bit, manual install notes)

## [1.0.1-beta.1]

### Added

- `first_frame_path` and `last_frame_path` fields on `MediaItem` — backend-generated
  video frame caches referenced directly by the presentation engine
- `VideoProcessor._extract_video_frames()` — extracts first frame (t=0) and last
  frame (sseof) during Phase 2 OPTIMISE; cached as `.1.frame` / `.2.frame` JPEGs
- **Image optimisation worker subprocess** (`worker.py`) — PIL operations (load,
  transpose, resize, save) run in an isolated process with `cpulimit -l 50` (hard
  50 % CPU cap) and `nice -n 19` (lowest priority); OS reclaims all memory on exit
- **Adaptive CPU throttling** — sleep between image optimisations scales with
  1‑minute load average (`load1 × 0.2`, capped at 1.0 s); folder watcher also
  yields between thumbnail generations when the optimiser is busy
- **Boot screen progress bar** — red fill bar below the spinner showing
  optimisation progress (1/6 → 6/6); uses `draw_image` with 1×1 pixel textures
  to avoid pi3d colour-space issues
- **Per‑phase progress bars** in the Web UI Background Processing card — separate
  persistent bars for scanning, image optimisation, and video transcoding;
  each retains its last position when the active phase switches
- **Frontend→backend slideshow‑started signal** — `POST /api/slideshow-started`
  defers the network monitor's AP‑fallback countdown until the first slideshow
  items are ready, preventing CPU/I/O contention during initial processing
- `/api/config/processing-status` endpoint — serves per‑phase progress data
- `FEATURES.md` — comprehensive feature list linked from README
- Per‑image processing time and per‑batch memory delta logging (DEBUG level)
- **Raspberry Pi 5 (2GB+) official Phase 1 support** — runs the same Trixie
  image via cage + XWayland; now the recommended platform over Pi 3
- **Immich network connectivity gate** — syncer checks `is_connected()` before
  attempting API calls, avoiding wasted time on doomed requests when offline
- **Network Controller** (`network_controller.py`) — single-owner state machine
  for WiFi/AP management; eliminates race conditions between network monitor
  thread and Flask request threads via ``threading.Lock``
- **WiFi AP grace period** — 5‑minute silent retry of saved WiFi before
  activating the captive portal; handles transient router reboots without
  user intervention
- **WiFi AP exhaustion** — AP auto‑stops after 10 minutes if no user connects;
  never reactivates until next reboot, preventing continuous AP cycling on
  permanent WiFi loss
- **WiFi AP crash detection** — controller detects when hostapd dies
  unexpectedly and transitions to exhausted state rather than lying about
  the AP being active
- **WiFi regulatory domain** — configurable country code in setup script and
  Web UI; persists via ``cfg80211.ieee80211_regdom`` module parameter
- **WiFi power management disabled** — setup script writes NetworkManager
  config (``wifi.powersave = 2``), fixing Pi 3 WiFi unreliability
- **Pipeline reset on config change** — changes to video, image, or display
  settings clear all queues and re‑scan; boot layer reactivates with spinner
  during rebuild instead of showing a black screen
- **Boot layer reactivation** — ``reactivate()`` method re‑shows logo + spinner
  on pipeline reset; reset mode skips 3 s minimum display and progress bar
- **Documentation overhaul** — new ``GETTING_STARTED.md``, ``WIFI_SETUP.md``,
  ``INSTALLATION.md``; README restructured with logo, showcase images, and
  two‑path quick‑start section
- **Contributing link** — README Contributing section links to dev setup guide
  in ``INSTALLATION.md#path-c-development-setup-remote-via-samba``
- **Cumulative progress tracking** — image and video optimisation progress bars
  now show total backlog across all batches instead of resetting every 6 items
- **Folder watcher scanning progress** — incremental scans (new files added to
  watch folders) now report progress to the Web UI bar, not just the initial scan

### Changed

- **Image processing moved to subprocess** — `ImageProcessor.process()` spawns
  `worker.py` for cache misses; cache hits remain in‑process (fast path)
- **Throttle formula 4× more aggressive** — `load1 × 0.05` → `load1 × 0.2`
- **Batch sizes reduced** — optimiser flush 12→6, folder watcher enqueue 24→6;
  items reach the slideshow ~4× faster after startup
- **Boot layer** fades based on `slideshow_ready` signal from renderer (not
  playlist.json on disk) and waits for active GPU texture before signalling
  readiness — eliminates fade‑to‑black and jump‑cut regressions
- `_write_progress` uses per‑phase format — scanning, optimising, and transcoding
  each track their own `total`/`processed` independently
- **Web UI** Background Processing card replaced with three persistent progress
  bars using Material Symbols icons
- `is_ready_to_play` now requires `first_frame_path` and `last_frame_path` for
  video items — videos without cached frames are excluded from the slideshow
- Dev setup script: removed `python3-pip` and `python3-pygame` (dead deps)
- **Pi 5 moved from Phase 2 to Phase 1** — `ARCHITECTURE.md`, `CLAUDE.md`,
  `README.md`, and `HARDWARE.md` updated; Pi 5 (2GB+) now the recommended platform
- **Boot progress bar auto‑hides** when slideshow queue reaches 6 items,
  preventing repeated 100 % cycling on cached restarts
- **Release scripts** (`release.ps1`, `release.sh`): merge strategy changed
  from `--ff-only` to `--no-ff` for cleaner release history
- **Web UI** scanning label updated to "Scanning folders and generating thumbnails"
- **WiFi AP state machine** — daemon's ``_network_monitor_loop`` now delegates
  to ``NetworkController`` with phase‑based transitions (``MONITORING``,
  ``GRACE_PERIOD``, ``AP_ACTIVE``, ``AP_EXHAUSTED``)
- **Video transcode CPU limit default** — increased from 50 % to 300 %
  (3 full cores on Pi 5/3)
- **VLC playback messages** — changed from ``INFO`` to ``DEBUG`` log level
- **Immich test connection** — saves server URL and API key on successful test
  so Fetch Albums works immediately without a separate save step
- **Quiet boot hint** — removed "Requires a reboot to take effect" from Web UI

### Fixed

- **AP fallback falsely activated under CPU load** — `is_connected()` now returns
  `True` on exception (nmcli timeout) instead of `False`
- **Worker JSON corrupted by cpulimit** — cpulimit prints `Process NNNN detected`
  to stdout; parser now takes the first JSON line only
- **Duplicate throttle line** — old `×0.05` formula overwrote new `×0.2` formula
- **Cache‑hit `AttributeError`** — removed `_build_item` call, inlined
  `MediaItem` construction with `_get_cached_dimensions()`
- **Boot screen jump cut** — `slideshow_ready` now requires non‑None active GPU
  texture before signalling, preventing fade timer expiry during sync texture load
- **Slideshow‑started sent on empty queue** — notification only fires when items
  are present; hot‑reload path also signals if initial load was empty
- **Progress bar jumping backward** — uses batch size (6) as total instead of
  shrinking `total_remaining`
- **Progress bar disappearing** — retains last‑known percentage when phase
  switches away from `optimising_images`
- **Boot layer duplicate docstring** — removed dead duplicate of
  `_read_progress_pct` that shadowed the real implementation
- **WiFi captive portal not appearing on first boot** — ``start_ap_mode()``
  return value now checked; ``ap_was_active`` only set on success; wlan0
  readiness polled (up to 30 s) before starting AP; hostapd no longer
  auto‑starts at boot
- **AP fallback retry deadlock** — retry guard now checks ``is_ap_mode_active()``
  as source of truth instead of a stale ``ap_was_active`` boolean
- **PIN validation race** — PIN validated against cleared state (passed any
  input); now owned by ``NetworkController`` with ``threading.Lock``; never
  passes when no PIN is active
- **Transparent PNG rendering** — RGBA/PA images composited onto black
  background before RGB conversion; fixes white artifacts in transparent areas
  for both thumbnails and optimised images
- **Display power button state** — Web UI button now reads actual display state
  from daemon via health endpoint instead of using a local toggle guess
- **Media library CPU spike** — removed on‑the‑fly thumbnail generation from
  ``/api/media/list``; relies on FolderWatcher‑generated thumbnails only
- **Log viewer follow mode** — auto‑follow now jumps to last page when new
  entries arrive; scroll target corrected to ``#log-viewer`` container
- **On‑screen message spacing** — Y‑position now accumulated from actual
  heights of preceding messages instead of ``index × current_height``,
  eliminating gaps and overlaps between differently‑sized popups

### Removed

- All ffmpeg/ffprobe calls from `PresentationEngine` (`engine.py`):
  `_extract_frame_array_cpu`, `_get_or_create_video_frame`,
  `_video_frame_cache_path`, `_video_frame_is_cached`
- `contextlib` import from frontend (no longer needed)
- `_count_playlist_items()`, `_PLAYLIST_PATH`, and `json` import from boot layer
- `_resize_to_screen`, `_extract_exif`, `_build_item` from `ImageProcessor`
- `ImageOps`, `UnidentifiedImageError` imports from `image.py`

## [0.2.2-beta.3] — 2026-08-01

### Added

- Version bump script (`scripts/bump_version.py`) with full semver + pre-release support
- Release documentation (`docs/RELEASING.md`) covering versioning, channels, and workflow

### Fixed

- Channel switch hanging on "Checking for updates…" — added polling loop so the
  UI refreshes when the background check completes instead of showing the spinner
  indefinitely

### Changed

- Setup script (`setup_trixie_metixel.sh`): standalone execution support,
  auto-detects git repository state, improved user feedback during setup
- README revised for clarity and setup script details

## [0.2.0-beta.1] — 2026-08-01

### Added

- **OTA self-update system** via git + GitHub Releases API
  - Three update channels: stable, beta, and dev
  - `UpdateManager` backend: periodic checks, git-based apply, service restart coordination
  - Web UI: channel selector, available version display, check/install buttons (Advanced → Updates)
  - `/api/updates/*` REST endpoints for status, check, apply, and channel switching
  - `update` section in configuration with channel, auto-check, and interval settings
- **WiFi Captive Portal** with PIN protection for headless network setup
  - `NetworkManager` handles AP fallback when no WiFi is configured
  - Captive portal web page served on port 8080 during AP mode
- **System Messages overlay** — popup notification system rendered on the display
  - Configurable default duration, max visible count, and persistent messages
  - Message layer pauses during video playback to avoid distraction
- **Boot screen** — custom splash screen with spinner (replaces pygame dependency)
  - `BootLayer` renders on startup until the first media item is ready
- **System status monitoring** in the Web UI dashboard
  - CPU utilisation, memory usage, disk space, and uptime
  - Scheduled sync status and next-sync countdown
- **NTP time synchronisation** with configurable NTP servers
- **Display sleep schedule** — configurable on/off times for screen power saving
- **Reboot and shutdown buttons** in the Web UI (Advanced → System)
- **CPU throttling for image optimisation** — `cpulimit`/`nice` integration prevents
  background processing from starving the render loop
- **Transcode status tags** visible in the media list view
- **New media pipeline v2** — improved queue management, better parallelism,
  and per-item state tracking through the watch→optimise→queue→sync phases
- Updated sample media set with new royalty-free images

### Changed

- **Web UI redesigned** — cleaner layout, dark theme, responsive cards
- **Removed pygame** from the dependency tree entirely (replaced by `BootLayer`)
- **Removed legacy Pi 3 boot messages** for cleaner startup on Trixie
- Slide transition timer now pauses during video playback
- Image optimisation defaults tuned for Pi Zero 2 W memory constraints
- Config schema: added `image`, `video`, `messages`, `network`, `system`, and `update` sections

### Fixed

- Clean install failures — missing cache/log directories now created automatically
- GitHub repository URL references throughout the codebase and NOTICE file
- System Status values reporting incorrect memory/disk figures
- Boot screen timing race with AP fallback on first boot
- Network connectivity check looping when DNS was unreachable
- Video setting changes not propagating to the active playback queue
- Hot-reload playlist corruption after rapid config changes
- Thumbnail generation failing for certain EXIF orientations
- Race conditions in the media pipeline when watcher and optimiser overlapped
- Parallel transcode processes exhausting CPU and memory on Pi Zero 2 W
- Video processing OOM (out of memory) during large-file optimisation
- GPU texture count warnings from unreleased textures on memory-constrained devices
- Lint errors and crash when no display monitor was attached

## [0.1.1] — 2025-07-30

### Initial Release

- Phase 1 hardware support: Raspberry Pi 2, 3, Zero 2 W
- Display backend abstraction (pi3d under cage + XWayland on Trixie)
- 4-phase media pipeline: watch → optimise → queue → sync
- Web dashboard (Flask + vanilla JS SPA) on port 8080
- Immich integration for photo sync
- Local folder watching with configurable paths
- Slideshow engine with crossfade transitions
- Video playback with ffmpeg transcoding
- MQTT client for Home Assistant
- HDMI-CEC input handling
- Systemd service management
- Network manager with AP fallback (captive portal)
- Image optimisation (resize, matte, EXIF rotation)
- Thumbnail generation by backend processors
