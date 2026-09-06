# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Configuration API endpoints."""

from __future__ import annotations

import logging
import subprocess

from flask import Blueprint, current_app, jsonify, request

from metixel.backend.web.helpers import get_body, jsonify_error
from metixel.backend.web.media_service import clear_cache
from metixel.shared.subprocess import schedule_sudo

logger = logging.getLogger(__name__)

config_bp = Blueprint("config", __name__)

#: Display-mode keys that require a frontend (cage) restart to take effect,
#: because the display backend's ``create()`` runs once at startup.
_DISPLAY_MODE_KEYS = ("width", "height", "refresh_rate", "rotation")

#: Display keys that change the on-screen canvas *size* (so media must be
#: re-optimised).  Changing only ``refresh_rate`` doesn't affect dimensions,
#: so it must not invalidate the processed-media cache.
_DISPLAY_SIZE_KEYS = ("width", "height", "rotation")


@config_bp.route("", methods=["GET"])
def get_config():
    """Get the full current configuration."""
    state = current_app.config["METIXEL_STATE"]
    return jsonify(state.config.to_dict())


@config_bp.route("/<section>", methods=["GET"])
def get_config_section(section: str):
    """Get a specific config section."""
    state = current_app.config["METIXEL_STATE"]
    config = state.config
    if section not in config.to_dict():
        return jsonify_error(f"Unknown config section: {section}", 404)

    # If the caller asked to apply a WiFi country code, run iw reg set
    # immediately so the radio uses correct channels without a reboot.
    country = request.args.get("apply_wifi_country", "").strip().upper()
    if section == "network" and country and len(country) == 2:
        try:
            subprocess.run(
                ["sudo", "iw", "reg", "set", country],
                capture_output=True,
                timeout=5,
            )
            logger.info("WiFi regulatory domain set to: %s", country)
        except Exception:
            logger.warning("Failed to set WiFi country to %s", country, exc_info=True)

    return jsonify(config.to_dict()[section])


@config_bp.route("/video/profiles", methods=["GET"])
def video_profiles():
    """Return available transcoding profiles with current selection."""
    from metixel.backend.processing.video import VideoProcessor, _detect_pi_model

    state = current_app.config["METIXEL_STATE"]
    video_cfg = state.config.video
    profiles = []
    for key, prof in VideoProcessor.PROFILES.items():
        profiles.append(
            {
                "key": key,
                "label": prof["label"],
                "codec": prof["codec"],
                "max_width": prof["max_width"],
                "max_height": prof["max_height"],
                "max_fps": prof["max_fps"],
                "max_bitrate": prof["max_bitrate"],
                "crf": prof["crf"],
                "h264_profile": prof["h264_profile"],
                "h264_level": prof["h264_level"],
                "color_depth": prof["color_depth"],
                "hdr_support": prof["hdr_support"],
            }
        )
    # Add custom
    profiles.append({"key": "custom", "label": "Custom"})
    return jsonify(
        {
            "profiles": profiles,
            "current": video_cfg.get("transcoding_profile", ""),
            "detected_model": _detect_pi_model(),
            "keep_audio": video_cfg.get("keep_audio", False),
            "custom_settings": {
                "transcode_max_width": video_cfg.get("transcode_max_width", 0),
                "transcode_max_height": video_cfg.get("transcode_max_height", 0),
                "transcode_quality": video_cfg.get("transcode_quality", 23),
                "transcode_crf": video_cfg.get(
                    "transcode_crf", video_cfg.get("transcode_quality", 23)
                ),
                "transcode_use_software_encoder": video_cfg.get(
                    "transcode_use_software_encoder", True
                ),
                "transcode_timeout_seconds": video_cfg.get("transcode_timeout_seconds", 7200),
                "cpu_throttle_enabled": video_cfg.get("cpu_throttle_enabled", True),
                "cpu_throttle_percent": video_cfg.get("cpu_throttle_percent", 200),
                "transcoding_enabled": video_cfg.get("transcoding_enabled", True),
            },
        }
    )


@config_bp.route("/<section>", methods=["PUT"])
def update_config_section(section: str):
    """Update a config section. Triggers hot reload in the frontend."""
    state = current_app.config["METIXEL_STATE"]
    data = get_body()
    if not data:
        logger.warning(
            "PUT /%s: invalid or missing JSON body (Content-Type: %s)",
            section,
            request.content_type,
        )
        return jsonify_error(
            "Invalid JSON body",
            400,
            hint="Send JSON with Content-Type: application/json",
        )

    try:
        logger.info("PUT /%s: updating with keys=%s", section, list(data.keys()))
        state.update_config(section, data)
        logger.info("Config section '%s' updated via API — saved to %s", section, state.config_path)

        # Immediately notify the OptimisationQueue of config changes
        # so queued items are re-classified without waiting for the
        # 30-second periodic reload cycle.  This is especially important
        # when the user toggles transcoding on/off.
        opt_queue = current_app.config.get("METIXEL_OPT_QUEUE")
        if opt_queue is not None and section in ("video", "image"):
            try:
                opt_queue.reload_config()
            except Exception:
                logger.debug("OptimisationQueue reload failed", exc_info=True)

        # Trigger a full pipeline reset when config changes affect
        # what media is playable — simpler and more robust than
        # trying to incrementally update items mid-pipeline.
        # "sync" is included so that enabling/disabling watch folders
        # and toggling local sync on/off clears the playlist and
        # re-scans with the correct set of active paths.
        daemon = current_app.config.get("METIXEL_DAEMON")
        if daemon is not None and section in ("video", "image", "display", "sync"):
            try:
                daemon.reset_pipeline()
            except Exception:
                logger.debug("Pipeline reset failed", exc_info=True)

        # Display-mode changes (resolution / refresh rate / rotation) require
        # a frontend restart to take effect — the display backend's create()
        # runs once at startup and applies the mode via wlr-randr then.  The
        # frontend runs under the metixel-cage service, so restart it after a
        # short delay so the HTTP response is sent first.
        if section == "display" and any(k in data for k in _DISPLAY_MODE_KEYS):
            # Changing the canvas *size* (width/height/rotation) means any
            # previously-optimised images/videos were scaled for the old
            # dimensions, so they must be re-processed at the new size.  Clear
            # the processed-media cache so the next scan re-optimises from the
            # source (the pipeline reset below re-discovers and re-queues).
            if any(k in data for k in _DISPLAY_SIZE_KEYS):
                try:
                    deleted, freed = clear_cache(state)
                    logger.info(
                        "Canvas size changed (width/height/rotation) — cleared %d "
                        "processed cache file(s), freed %.1f MB",
                        deleted,
                        freed / (1024 * 1024),
                    )
                except Exception:
                    logger.warning(
                        "Failed to clear processed cache after canvas-size change",
                        exc_info=True,
                    )
            schedule_sudo(
                ["systemctl", "restart", "metixel-cage"],
                ok_message="Frontend restarted to apply display mode",
                fail_message="sudo systemctl restart metixel-cage",
                thread_name="display-mode-restart",
            )
            logger.info(
                "Display mode changed — frontend restart scheduled to apply "
                "width/height/refresh_rate/rotation"
            )

        # When the welcome banner is dismissed (system.first_run → false),
        # also dismiss all on-screen welcome messages so they don't linger.
        ipc = current_app.config.get("METIXEL_IPC")
        if ipc is not None and section == "system" and data.get("first_run") is False:
            try:
                from metixel.shared.ipc import ControlMessage

                ipc.send(ControlMessage(cmd="dismiss_all_messages"))
                logger.info("Welcome banner dismissed — clearing on-screen messages")
            except Exception:
                pass

        return jsonify(
            {
                "status": "ok",
                "section": section,
                "config_path": str(state.config_path),
            }
        )
    except KeyError:
        return jsonify_error(
            f"Unknown config section: {section}",
            404,
            valid_sections=list(state.config.to_dict().keys()),
        )
    except Exception as e:
        logger.exception("Failed to update config section '%s'", section)
        return jsonify_error(
            str(e),
            500,
            hint="Check server logs for details",
        )


@config_bp.route("/reload", methods=["POST"])
def reload_config():
    """Reload configuration from disk."""
    state = current_app.config["METIXEL_STATE"]
    state.reload_config()
    return jsonify({"status": "ok"})


@config_bp.route("/path", methods=["GET"])
def get_config_path():
    """Return the config file path (for debugging)."""
    state = current_app.config["METIXEL_STATE"]
    return jsonify(
        {
            "config_path": str(state.config_path),
            "exists": state.config_path.exists(),
        }
    )
