#!/usr/bin/env python3
"""Check libmpv render API symbol availability."""

import ctypes

lib = ctypes.CDLL("libmpv.so.2")
for sym in [
    "mpv_render_context_create",
    "mpv_render_context_render",
    "mpv_render_context_set_update_callback",
    "mpv_render_context_set_parameter",
    "mpv_render_context_free",
    "mpv_create",
    "mpv_initialize",
    "mpv_command",
    "mpv_set_property_string",
    "mpv_observe_property",
    "mpv_wait_event",
    "mpv_request_log_messages",
    "mpv_render_context_set_update_callback",
]:
    print(f"{sym}: {hasattr(lib, sym)}")
