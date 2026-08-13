# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Frame rendering and crossfade transitions for the presentation engine."""

from __future__ import annotations

import logging
from typing import Any

from metixel.frontend.presentation.base import BaseEngineState
from metixel.shared.models import MediaItem

logger = logging.getLogger(__name__)


class FrameRendererMixin(BaseEngineState):
    """Frame rendering and crossfade transitions for the presentation engine."""

    def _resolve_fit_mode(self, item: MediaItem) -> str:
        mode = self._fit_mode_cache
        if mode != "cover":
            return mode
        if not self._config.slideshow.get("smart_cover", True):
            return mode
        if item.width <= 0 or item.height <= 0:
            return mode
        img_ratio = item.width / max(item.height, 1)
        if self._screen_ratio > 1.0 and img_ratio <= 1.0:
            return "contain"
        if self._screen_ratio < 1.0 and img_ratio >= 1.0:
            return "contain"
        return mode

    def _render_item(
        self,
        item: MediaItem,
        alpha: float,
        with_matte: bool = True,
        texture: Any = None,
        layout: dict | None = None,
    ) -> None:
        """Draw a single media item with layout and matte bars."""
        if texture is None:
            tex = self._tex[self._active]
            if tex is None and self._current_idx >= 0:
                gpu_info = self._backend.gpu_memory_info()
                logger.warning(
                    "Active slot %d has no texture — attempting sync load for %s",
                    self._active,
                    getattr(item, "original_path", item),
                )
                if gpu_info:
                    logger.debug(
                        "GPU mem at sync load: total=%sM reloc=%sM V3D=%skb/%sBOs textures=%s/%s",
                        gpu_info.get("gpu_total_mb", "?"),
                        gpu_info.get("reloc_used_mb", "?"),
                        gpu_info.get("v3d_bo_kb", "?"),
                        gpu_info.get("v3d_bo_count", "?"),
                        gpu_info.get("texture_count", "?"),
                        gpu_info.get("max_textures", "?"),
                    )
                self._load_texture_for_slot(self._active, item)
                tex = self._tex[self._active]
            if tex is None:
                gpu_info = self._backend.gpu_memory_info()
                logger.warning(
                    "No texture for active slot %d (item=%s, idx=%d) — "
                    "rendering blank frame (black screen)",
                    self._active,
                    getattr(item, "original_path", item),
                    self._current_idx,
                )
                if gpu_info:
                    logger.warning(
                        "GPU mem at black screen: total=%sM reloc=%sM "
                        "V3D=%skb/%sBOs textures=%s/%s",
                        gpu_info.get("gpu_total_mb", "?"),
                        gpu_info.get("reloc_used_mb", "?"),
                        gpu_info.get("v3d_bo_kb", "?"),
                        gpu_info.get("v3d_bo_count", "?"),
                        gpu_info.get("texture_count", "?"),
                        gpu_info.get("max_textures", "?"),
                    )
                return
            texture = tex

        if layout is None:
            # Use the texture's source item for layout when it differs
            # from the current queue item.  This happens after video
            # playback: the active slot holds the last frame, but the
            # queue has already advanced to the next image.
            layout_source = self._tex_item[self._active] or item
            resolved = self._resolve_fit_mode(layout_source)
            cache_key = (id(layout_source), resolved)
            if cache_key in self._layout_cache:
                layout = self._layout_cache[cache_key]
            else:
                layout = self._layout.compute(layout_source, fit_mode=resolved)
                if len(self._layout_cache) < 16:
                    self._layout_cache[cache_key] = layout

        if with_matte:
            matte_color = self._config.slideshow.get("matte_color", [0, 0, 0])
            for mx, my, mw, mh in layout.get("matte_rects", []):
                self._backend.draw_rect(
                    mx,
                    my,
                    mw,
                    mh,
                    (*matte_color, alpha),
                    z=-1,
                )

        ix, iy, iw, ih = layout["image_rect"]
        self._backend.draw_image(
            texture,
            ix,
            iy,
            iw,
            ih,
            alpha=alpha,
            uv_offset=(0.0, 0.0),
            uv_scale=(1.0, 1.0),
            z=0.0,
        )

    def _render_transition(
        self,
        current_item: MediaItem,
        progress: float,
        next_tex: Any,
    ) -> None:
        """Crossfade between active and inactive texture slots."""
        next_item = self._queue[(self._current_idx + 1) % len(self._queue)]
        style = self._config.slideshow.get("transition_style", "crossfade")

        # Use the texture's source item for layout when it differs from
        # the queue item (e.g. last frame of a video transitioning to
        # the next photo).
        cur_src = self._tex_item[self._active] or current_item
        next_src = self._tex_item[self._inactive] or next_item

        if style == "crossfade":
            current_layout = self._layout.compute(
                cur_src,
                fit_mode=self._resolve_fit_mode(cur_src),
            )
            next_layout = self._layout.compute(
                next_src,
                fit_mode=self._resolve_fit_mode(next_src),
            )
            matte_color = self._config.slideshow.get("matte_color", [0, 0, 0])

            # Full-screen black background ensures any partially-
            # transparent pixels from the crossfade shader blend
            # against solid black rather than showing framebuffer
            # artefacts or PNG transparency edges.
            self._backend.draw_rect(
                0,
                0,
                self._backend.width,
                self._backend.height,
                (*matte_color, 1.0),
                z=-2,
            )
            for mx, my, mw, mh in current_layout.get("matte_rects", []):
                self._backend.draw_rect(
                    mx,
                    my,
                    mw,
                    mh,
                    (*matte_color, 1.0),
                    z=-1,
                )
            self._backend.draw_crossfade(
                tex_current=self._tex[self._active],
                tex_next=next_tex,
                blend=progress,
                current_rect=current_layout["image_rect"],
                next_rect=next_layout["image_rect"],
            )
        elif style == "fade_through_black":
            # Compute layouts for both items explicitly.  _render_item
            # defaults to the *active* slot's source for layout, which
            # is still the current item during transition.  Without
            # explicit layouts, the second half would draw the next
            # texture with the current item's aspect ratio.
            cur_layout = self._layout.compute(
                cur_src,
                fit_mode=self._resolve_fit_mode(cur_src),
            )
            next_layout = self._layout.compute(
                next_src,
                fit_mode=self._resolve_fit_mode(next_src),
            )
            if progress < 0.5:
                self._render_item(
                    current_item,
                    1.0 - progress * 2,
                    texture=self._tex[self._active],
                    layout=cur_layout,
                )
            else:
                self._render_item(
                    next_item,
                    (progress - 0.5) * 2,
                    texture=next_tex,
                    layout=next_layout,
                )
        elif style == "none":
            # No transition — just show the next item immediately.
            # Layouts are still computed explicitly so the next
            # texture isn't drawn with the current item's aspect ratio.
            next_layout = self._layout.compute(
                next_src,
                fit_mode=self._resolve_fit_mode(next_src),
            )
            self._render_item(next_item, 1.0, texture=next_tex, layout=next_layout)
        else:
            # Hard cut: show current until midpoint, then next.
            # Explicit layouts prevent the next texture from being drawn
            # with the current item's aspect ratio during the second half.
            cur_layout = self._layout.compute(
                cur_src,
                fit_mode=self._resolve_fit_mode(cur_src),
            )
            next_layout = self._layout.compute(
                next_src,
                fit_mode=self._resolve_fit_mode(next_src),
            )
            if progress < 0.5:
                self._render_item(
                    current_item,
                    1.0,
                    texture=self._tex[self._active],
                    layout=cur_layout,
                )
            else:
                self._render_item(next_item, 1.0, texture=next_tex, layout=next_layout)

    def _draw_frame_to_buffer(self, texture: Any, layout: dict) -> None:
        """Draw a texture with matte bars at the given layout position."""
        matte_color = self._config.slideshow.get("matte_color", [0, 0, 0])
        for mx, my, mw, mh in layout.get("matte_rects", []):
            self._backend.draw_rect(
                mx,
                my,
                mw,
                mh,
                (*matte_color, 1.0),
                z=-1,
            )
        ix, iy, iw, ih = layout["image_rect"]
        self._backend.draw_image(
            texture,
            ix,
            iy,
            iw,
            ih,
            alpha=1.0,
            uv_offset=(0.0, 0.0),
            uv_scale=(1.0, 1.0),
            z=0.0,
        )
