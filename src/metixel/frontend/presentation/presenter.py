# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Presenter — slideshow timing, layout and playback for the frontend.

This module replaces three that existed to serve the pi3d texture pipeline:
``rendering.py`` (which drew into two GPU slots), ``preload.py`` (which uploaded
the next texture) and ``video_state.py`` (which managed a VLC subprocess).  With a
retained-mode canvas there is no texture slot to manage and no external player
process: the presenter asks :class:`~metixel.framing.layout.LayoutEngine` for a
:class:`~metixel.framing.layout.RenderPlan` and hands it to the backend.

What the old design was protecting, and what still matters
----------------------------------------------------------
The two-slot ping-pong existed for one reason: image decoding and GPU upload were
slow enough that doing them at the moment of the switch produced a visible gap.
That concern is unchanged — a 24MP JPEG still takes hundreds of milliseconds to
decode on a Pi 3.  What changed is the *mechanism*: decoded images are held in
:class:`~metixel.frontend.presentation.image_cache.ImageCache` and the presenter
asks the backend to paint a plan.

**The stall-and-hold behaviour is deliberately preserved.**  If the next item is
not ready when the current one's timer expires, the presenter holds the current
slide and keeps waiting rather than cutting to an empty frame — up to
:data:`STALL_TIMEOUT_S`, after which it advances anyway so one corrupt file cannot
freeze the frame forever.  Two consequences of that decision are load-bearing:

* when a stalled transition finally unstalls, the slide clock is **rewound** so the
  crossfade gets its full duration instead of jump-cutting;
* a stall is logged once per slide, not once per frame, or a slow decode would
  flood the log at frame rate.

Video
-----
**Video playback has been removed from the frontend** while the basics are
rebuilt; it will be re-inserted later.  A video item is still accepted and is
presented as a still: its *poster* is the pre-generated first-frame JPEG, which
the backend loads as an ordinary image.  The backend media pipeline (Phase 2:
OPTIMISE) continues to probe and optimise videos — only the frontend's
``play_video`` / ``stop_video`` usage is gone, so nothing is lost when playback
returns.
"""

from __future__ import annotations

import contextlib
import logging
import random
import time
from typing import Any, Literal

from metixel.display.backend import DisplayBackend
from metixel.display.overlay_element import OverlayElement
from metixel.framing.layout import LayoutEngine, RenderPlan
from metixel.framing.resolve import MediaSize
from metixel.frontend.presentation.image_cache import ImageCache
from metixel.frontend.presentation.transitions import TransitionEngine
from metixel.shared.config import Config
from metixel.shared.io import atomic_write_json
from metixel.shared.media import content_hash
from metixel.shared.models import MediaItem, MediaType
from metixel.shared.paths import resolve_install_path, run_path

logger = logging.getLogger(__name__)

#: The framing engine's media-type literal, which is not the MediaType enum.
FramingMediaType = Literal["image", "video"]

#: How long a stalled transition waits for the next item before giving up.
#: Bounded so a genuinely broken file cannot freeze the frame indefinitely.
STALL_TIMEOUT_S = 30.0

#: How hard to look ahead when the next item is a video and needs its poster.
VIDEO_POSTER_LOOKAHEAD = 1

#: Slideshow presentation is deliberately reduced to the one behaviour that has
#: to work before anything else: a looping photo slideshow that fills the panel.
#:
#: ``borderless`` drops the Mat Ring (no matte, no moulding band read as a mat),
#: and ``EDGE_MARGIN_MM`` is pinned to zero as well: the template default insets
#: the artwork to hide the bezel, which would leave a 7 px border at 1920x1200 —
#: i.e. a "mat" by another name.
#:
#: The ring layers are still *computed* (the framing engine keeps its fit check
#: and the physical branch stays supported); they simply resolve to the full
#: panel and the moulding annulus falls outside the canvas, so nothing is
#: drawn.  That keeps this a presentation choice, not a fork of the geometry.
SLIDESHOW_FRAMING_STYLE = "borderless"
SLIDESHOW_EDGE_MARGIN_MM = 0.0

#: The Slideshow Settings card's fit modes mapped onto the framing engine's
#: *overflow* axis.
#:
#: The two vocabularies describe one choice from opposite ends.  The card asks
#: how a photo should fill the frame; the framing engine asks what happens to
#: the residue when a fixed Mat Window meets a mismatched aspect ratio:
#:
#: ============  ==========  =================================================
#: UI fit mode    overflow    Result
#: ============  ==========  =================================================
#: ``cover``      ``crop``    The artwork covers the panel and the overflowing
#:                            edges are sampled away (``RenderPlan.artwork_src``).
#: ``contain``    ``fill``    The artwork is contained and the ambient fill
#:                            absorbs the residue, so nothing is cropped.
#: ============  ==========  =================================================
#:
#: There is deliberately no third mapping.  The engine has no aspect-distorting
#: mode, and the card no longer offers the old "fill (stretch)" option that
#: would have needed one.
_FIT_MODE_TO_OVERFLOW: dict[str, str] = {
    "cover": "crop",
    "contain": "fill",
}

#: Ambient colour used when the config omits it or holds something unusable.
#: Matches ``DEFAULT_CONFIG["slideshow"]["ambient_color"]`` and the framing
#: engine's own ``AmbientFillSpec.colour``.
_DEFAULT_AMBIENT_COLOUR = "#101014"


def _ambient_colour(config: Config) -> str:
    """Return the configured ambient colour as ``#rrggbb``.

    Accepts either a hex string or an ``[r, g, b]`` list, because the
    slideshow card's colour picker writes a hex string while the older
    ``matte_color`` key next to it is a list — and a config hand-edited from
    one to the other should not silently fall back to the default.

    The colour matters beyond the ambient band itself: it is what the canvas
    paints as the transition curtain, so a wrong value shows up as a flicker
    at the edge of a letterboxed photo rather than as an obviously wrong band.
    """
    value = config.slideshow.get("ambient_color", _DEFAULT_AMBIENT_COLOUR)

    if isinstance(value, str):
        candidate = value.strip()
        if candidate.startswith("#") and len(candidate) == 7:
            try:
                int(candidate[1:], 16)
            except ValueError:
                pass
            else:
                return candidate.lower()
        logger.warning("Ignoring unusable ambient_color %r — using the default", value)
        return _DEFAULT_AMBIENT_COLOUR

    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            r, g, b = (max(0, min(255, int(c))) for c in value[:3])
        except (TypeError, ValueError):
            pass
        else:
            return f"#{r:02x}{g:02x}{b:02x}"

    logger.warning("Ignoring unusable ambient_color %r — using the default", value)
    return _DEFAULT_AMBIENT_COLOUR


#: Fit mode used when the config omits it or names an unknown one.  Matches
#: ``DEFAULT_CONFIG["slideshow"]["fit_mode"]``.
DEFAULT_FIT_MODE = "cover"

#: Defaults for the blur ambient look.  Match ``DEFAULT_CONFIG["slideshow"]`` and
#: the framing engine's own ``AmbientFillSpec`` defaults.
_DEFAULT_AMBIENT_BLUR_RADIUS = 24.0
_DEFAULT_AMBIENT_DARKEN = 0.35

#: Bounds for the blur controls.  The value is a pixel radius passed to
#: ``PIL.ImageFilter.BoxBlur``, so larger is blurrier.  1 is the point below
#: which there is no visible blur; 100 is the point past which the cost stops
#: being worth it on a Pi 3, where the filter runs on the render thread.
MIN_AMBIENT_BLUR_RADIUS = 1.0
MAX_AMBIENT_BLUR_RADIUS = 100.0


def _ambient_blur_radius(config: Config) -> float:
    """Return the configured blur strength, clamped to a usable range.

    This is a **pixel radius**, so a larger value is a heavier blur.  A
    non-numeric value falls back to the default rather than propagating a
    ``TypeError`` into the render loop, and the clamp keeps a hand-edited config
    (say ``ambient_blur_radius: 0``) from reaching the filter at all.
    """
    value = config.slideshow.get("ambient_blur_radius", _DEFAULT_AMBIENT_BLUR_RADIUS)
    try:
        radius = float(value)
    except (TypeError, ValueError):
        logger.warning("Ignoring unusable ambient_blur_radius %r — using the default", value)
        return _DEFAULT_AMBIENT_BLUR_RADIUS
    if radius < MIN_AMBIENT_BLUR_RADIUS or radius > MAX_AMBIENT_BLUR_RADIUS:
        clamped = min(MAX_AMBIENT_BLUR_RADIUS, max(MIN_AMBIENT_BLUR_RADIUS, radius))
        logger.warning(
            "ambient_blur_radius %r is outside %s–%s — clamping to %s",
            radius,
            MIN_AMBIENT_BLUR_RADIUS,
            MAX_AMBIENT_BLUR_RADIUS,
            clamped,
        )
        return clamped
    return radius


def _ambient_darken(config: Config) -> float:
    """Return how far to dim the blurred backdrop, clamped to ``0.0``–``1.0``.

    ``1.0`` would make the backdrop pure black, which is a legitimate (if odd)
    choice, so the range is inclusive rather than an error.
    """
    value = config.slideshow.get("ambient_darken", _DEFAULT_AMBIENT_DARKEN)
    try:
        darken = float(value)
    except (TypeError, ValueError):
        logger.warning("Ignoring unusable ambient_darken %r — using the default", value)
        return _DEFAULT_AMBIENT_DARKEN
    if not 0.0 <= darken <= 1.0:
        clamped = min(1.0, max(0.0, darken))
        logger.warning("ambient_darken %r is outside 0.0–1.0 — clamping to %s", darken, clamped)
        return clamped
    return darken


class Presenter:
    """Owns the slideshow clock, layout and playback for one display.

    Deliberately not a mixin hierarchy.  The old engine was split across five
    mixins because it shared mutable texture state that made a real class boundary
    unsafe to introduce in one step; that state no longer exists, so the split has
    no remaining justification.
    """

    def __init__(
        self,
        config: Config,
        backend: DisplayBackend,
        *,
        layout: LayoutEngine | None = None,
        cache: ImageCache | None = None,
    ) -> None:
        self._config = config
        self._backend = backend
        self._cache = cache or ImageCache()
        # Owns the easing curves and the three transition styles.  Reused rather
        # than reimplemented: fade_through_black in particular is not something
        # to hand-roll twice.
        self._transitions = TransitionEngine(config)

        sw = backend.width or config.display.get("width") or 1920
        sh = backend.height or config.display.get("height") or 1080
        # ``overflow`` is left unset on purpose: it is a per-item decision
        # (``fit_mode`` plus ``smart_cover``), supplied by ``_plan_for``.
        self._layout = layout or LayoutEngine(
            screen_w=sw,
            screen_h=sh,
            rotation=int(config.display.get("rotation", 0) or 0),
            style=SLIDESHOW_FRAMING_STYLE,
            overflow=None,
            ambient_strategy=str(config.slideshow.get("ambient_strategy", "solid")),
            ambient_colour=_ambient_colour(config),
            ambient_blur_radius=_ambient_blur_radius(config),
            ambient_darken=_ambient_darken(config),
            edge_margin=SLIDESHOW_EDGE_MARGIN_MM,
        )

        # -- Playlist --
        self._queue: list[MediaItem] = []
        self._current_idx: int = -1
        self._queue_loaded: bool = False
        self._paused: bool = False

        # -- Slide clock --
        self._item_start_time: float = 0.0
        self._transition_stall_logged: bool = False

        # -- The frame currently on screen, kept so a stalled transition can
        #    redraw it without re-deriving the plan every frame.
        self._shown_plan: RenderPlan | None = None
        self._shown_item: MediaItem | None = None

        # -- Crossfade: the outgoing frame is retained for the transition
        #    duration.  A dict-free pair rather than the old texture slots.
        self._prev_plan: RenderPlan | None = None
        self._prev_image: Any = None

        logger.info(
            "Presenter: %dx%d, style=%s (pinned), fit=%s, smart_cover=%s, "
            "shuffle=%s, transition=%s, image_duration=%ss",
            sw,
            sh,
            SLIDESHOW_FRAMING_STYLE,
            self._fit_mode(),
            config.slideshow.get("smart_cover", True),
            self._shuffle_enabled(),
            self._transitions.style,
            config.slideshow.get("image_duration_seconds", 30),
        )

    # -- Introspection -------------------------------------------------------

    @property
    def queue(self) -> list[MediaItem]:
        return self._queue

    @property
    def queue_loaded(self) -> bool:
        return self._queue_loaded

    @property
    def current_index(self) -> int:
        return self._current_idx

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def current_item(self) -> MediaItem | None:
        if 0 <= self._current_idx < len(self._queue):
            return self._queue[self._current_idx]
        return None

    @property
    def has_visible_frame(self) -> bool:
        """Whether a frame has actually been painted.

        This is the honest "the slideshow is running" signal, and the boot screen
        waits on it.  Queue length alone is not enough: the boot layer fades out on
        a timer, so if it started fading while the first item was still decoding
        the fade would run over empty frames and end on a black screen.  A painted
        frame is the thing that actually guarantees pixels are on the panel.
        """
        return self._shown_plan is not None and self._shown_item is not None

    # -- Queue ---------------------------------------------------------------

    def set_queue(self, items: list[MediaItem]) -> None:
        """Replace the playlist and start from the first playable item.

        With ``slideshow.shuffle`` on, the play order is randomised, so a fresh
        queue starts somewhere other than the top of the backend's scan order.
        The backend's ``playlist.json`` is left in scan order on purpose: it is
        the pipeline's record of *what* is playable (and what the SPA's playlist
        view lists), not a statement about play order.
        """
        self._queue = list(items)
        if self._shuffle_enabled():
            random.shuffle(self._queue)
        self._queue_loaded = True
        self._cache.clear()
        self._current_idx = -1
        self._advance(initial=True)

    def add_items(self, items: list[MediaItem]) -> int:
        """Append items, returning how many were new.

        With shuffle on, new items are scattered through the *not yet played*
        tail rather than appended, so a batch that arrives from an Immich sync
        is not played back-to-back in download order.  Positions at or before
        the playhead are never disturbed: re-ordering behind the cursor would
        point it at a different photo than the one on screen.
        """
        existing = {item.id for item in self._queue}
        added = [item for item in items if item.id not in existing]
        if not added:
            return 0

        self._queue.extend(added)
        if self._shuffle_enabled():
            self._scatter_tail(len(added))
        if self._current_idx < 0:
            self._advance(initial=True)
        return len(added)

    def _scatter_tail(self, count: int) -> None:
        """Re-insert the last *count* queue items at random tail positions."""
        start = len(self._queue) - count
        fresh = self._queue[start:]
        del self._queue[start:]

        # The tail begins after the playhead, so the item on screen and the
        # history behind it keep their places.
        lowest = max(self._current_idx + 1, 0)
        # Invariant: ``_current_idx`` is a valid index of the queue as it was
        # before the additions, so after removing them ``lowest`` never exceeds
        # the length and ``randint`` stays in range.
        for item in fresh:
            self._queue.insert(random.randint(lowest, len(self._queue)), item)

    def remove_items(self, item_ids: set[str]) -> int:
        """Remove items by id, adjusting the cursor.  Returns the count removed."""
        if not item_ids:
            return 0
        before = len(self._queue)
        current = self.current_item
        self._queue = [item for item in self._queue if item.id not in item_ids]
        removed = before - len(self._queue)
        if not removed:
            return 0

        if current is not None and current.id in item_ids:
            # The item on screen was deleted: clamp the cursor and re-show
            # whatever now occupies that position.
            self._current_idx = min(self._current_idx, len(self._queue) - 1)
            self._shown_plan = None
            self._shown_item = None
            if self._current_idx >= 0:
                self._advance(initial=False)
            else:
                # Nothing left to show — clear the dashboard's Now Playing card
                # rather than leave it naming a deleted file.
                self._clear_current_media()
        return removed

    def _clear_current_media(self) -> None:
        """Remove ``current_media.json`` so the dashboard shows nothing playing."""
        with contextlib.suppress(FileNotFoundError):
            run_path("current_media.json").unlink()

    # -- Playback control ----------------------------------------------------

    def next_item(self) -> None:
        """Skip forward."""
        if not self._queue:
            return
        self._paused = False
        self._advance(initial=False)

    def prev_item(self) -> None:
        """Skip back."""
        if not self._queue:
            return
        self._paused = False
        self._current_idx = (self._current_idx - 1) % len(self._queue)
        self._item_start_time = time.monotonic()
        self._transition_stall_logged = False
        # A jump is a cut, not a transition: drop the outgoing frame so no
        # crossfade plays.
        self._prev_plan = None
        self._prev_image = None
        self._present_current()
        self._write_current_media()

    def pause(self) -> None:
        """Pause the slideshow.

        Republishing ``current_media.json`` is load-bearing, not cosmetic: the
        dashboard's pause/resume button is driven by ``current_media.paused``,
        so without a write the UI keeps showing the pre-pause state and the next
        click issues the *same* command again — which reads as "resume pauses
        instead of resuming".
        """
        self._paused = True
        self._write_current_media()

    def resume(self) -> None:
        """Resume the slideshow, restarting the current slide's timer.

        Resetting the slide clock gives the resumed slide its full duration
        rather than whatever fraction of it had already elapsed.  Republishing
        the state file is required for the same reason as in :meth:`pause`.
        """
        self._paused = False
        self._item_start_time = time.monotonic()
        self._write_current_media()

    def switch_album(self, album_id: str) -> None:
        """Album switching is a backend concern; recorded here for completeness."""
        logger.info("Album switch requested: %s (handled by the backend)", album_id)

    def reset_slide_timer(self) -> None:
        """Restart the current slide's timer.

        Called when the boot screen finishes fading, so the first slide gets its
        full configured duration rather than a fraction of it.
        """
        if self._current_idx >= 0:
            self._item_start_time = time.monotonic()

    def reload_config(self, config: Config) -> None:
        """Apply a hot-reloaded config without restarting the slideshow.

        Everything the Slideshow Settings card exposes has to take effect here,
        because saving that card restarts nothing — it only rewrites
        ``config.json``, which the render loop notices by mtime.  Image
        duration, transition style/duration, fit mode and smart cover are all
        read live from the config, so only the derived state needs rebuilding:
        the layout (when the panel geometry moved) and the cached plans (when
        the fit decision changed).

        The framing **style** is the one setting that is not taken from config:
        it stays pinned to the slideshow's full-bleed presentation (see
        :data:`SLIDESHOW_FRAMING_STYLE`), so a stale ``framing_style`` in
        ``config.json`` cannot reintroduce a mat on the next reload.
        """
        # Read the outgoing fit decision before adopting the new config, so a
        # change can be detected and the stale plans dropped.
        previous_fit = (
            self._fit_mode(),
            bool(self._config.slideshow.get("smart_cover", True)),
        )

        # Ambient look is a *constructor* argument of the layout engine, so the
        # old values have to be read before the swap to tell whether the engine
        # needs rebuilding.  Reading it from the live engine rather than from the
        # outgoing config is deliberate: it is the engine's state that has to
        # change, and the two can only diverge if a previous rebuild was skipped
        # — exactly the bug that made the ambient colour appear un-configurable
        # while ``fit_mode`` (read per item, never baked in) kept working.
        previous_ambient = (
            self._layout.ambient_strategy,
            self._layout.ambient_colour,
            self._layout.ambient_blur_radius,
            self._layout.ambient_darken,
        )

        self._config = config
        self._transitions.reload_config(config)

        rotation = int(config.display.get("rotation", 0) or 0)
        geometry_changed = rotation != self._layout.rotation

        # An ambient change rebuilds the engine for the same reason a rotation
        # change does: the value is not consulted per item, so an engine built at
        # startup would keep the colour the user first had.
        #
        # The blur parameters are in this tuple because they are constructor
        # arguments too.  Omitting them is precisely the bug that made the
        # ambient colour look un-configurable, and it would have made the blur
        # slider look dead in the same way.
        ambient_changed = previous_ambient != (
            config.slideshow.get("ambient_strategy", "solid"),
            _ambient_colour(config),
            _ambient_blur_radius(config),
            _ambient_darken(config),
        )

        if geometry_changed or ambient_changed:
            sw = self._backend.width or config.display.get("width") or 1920
            sh = self._backend.height or config.display.get("height") or 1080
            self._layout = LayoutEngine(
                screen_w=sw,
                screen_h=sh,
                rotation=rotation,
                style=SLIDESHOW_FRAMING_STYLE,
                overflow=None,
                ambient_strategy=str(config.slideshow.get("ambient_strategy", "solid")),
                ambient_colour=_ambient_colour(config),
                ambient_blur_radius=_ambient_blur_radius(config),
                ambient_darken=_ambient_darken(config),
                edge_margin=SLIDESHOW_EDGE_MARGIN_MM,
            )
            logger.info(
                "Presenter layout reloaded: rotation=%d style=%s "
                "(ambient=%s/%s blur=%.1f darken=%.2f)",
                rotation,
                SLIDESHOW_FRAMING_STYLE,
                config.slideshow.get("ambient_strategy", "solid"),
                _ambient_colour(config),
                _ambient_blur_radius(config),
                _ambient_darken(config),
            )

        fit_changed = previous_fit != (
            self._fit_mode(),
            bool(config.slideshow.get("smart_cover", True)),
        )

        # The cached plans are the frame on screen and the crossfade's outgoing
        # layer; both are invalid once the geometry or the fit decision moved.
        # Cleared only when something actually changed — dropping them on every
        # save would blank ``has_visible_frame`` and re-run the boot fade.
        #
        # ``ambient_changed`` drops them too: the engine was replaced, and the
        # cached plan was produced by the old one, so keeping it would paint the
        # previous colour until the next slide regardless.
        if geometry_changed or fit_changed or ambient_changed:
            self._shown_plan = None
            self._prev_plan = None
        if fit_changed:
            logger.info(
                "Presenter fit settings reloaded: fit_mode=%s smart_cover=%s",
                self._fit_mode(),
                config.slideshow.get("smart_cover", True),
            )

    # -- Frame loop ----------------------------------------------------------

    def render(self) -> None:
        """Produce one frame.  Called from the render loop or Qt timer."""
        if self._current_idx < 0 or not self._queue:
            return

        current = self.current_item
        if current is None:
            return

        if self._paused:
            self._present_current()
            return

        elapsed = time.monotonic() - self._item_start_time
        duration = self._item_duration(current)
        transition_s = self._transition_seconds()

        # ── Pre-warm the next item's backdrop ─────────────────────────────
        # The blur is the expensive part of a transition (~56 ms at 1920x1200),
        # so it is started as early as possible — while the current slide is
        # still on screen — instead of on the transition's first frame, which is
        # what made crossfades judder.  The canvas does the work on a worker
        # thread; the finished result is adopted below, on this thread.
        collect = getattr(self._backend, "collect_warm_backdrop", None)
        if collect is not None:
            collect()

        if transition_s > 0 and not self._paused:
            next_plan = self._next_plan()
            if next_plan is not None:
                warm = getattr(self._backend, "warm_backdrop", None)
                if warm is not None:
                    # ``_cache.get`` — NOT ``_image_for``.  The latter falls back
                    # to a BLOCKING load on a miss, and this runs on every tick,
                    # so using it here would reintroduce the very stall the warm
                    # exists to remove.  The decode-ahead worker
                    # (``_preload_next``) populates the cache during the current
                    # slide, so a miss simply means "warm it next tick".
                    nxt = self._queue[(self._current_idx + 1) % len(self._queue)]
                    handle = self._cache.get(nxt.id)
                    if handle is not None:
                        warm(next_plan, handle)

        # ── Stall recovery ────────────────────────────────────────────────
        # A previous frame held the slide because the next item was not ready.
        # If it has since arrived, rewind the clock so the crossfade runs from
        # the start rather than jump-cutting part-way through.
        if elapsed >= duration and self._transition_stall_logged and self._transition_ready():
            self._item_start_time = time.monotonic() - duration
            elapsed = duration
            self._transition_stall_logged = False
            logger.debug("Transition unstalled — crossfading instead of cutting")

        if elapsed >= duration + transition_s:
            # ── Advance, or hold ──────────────────────────────────────────
            if not self._transition_ready():
                stall_elapsed = elapsed - (duration + transition_s)
                if stall_elapsed < STALL_TIMEOUT_S:
                    # HOLD: a blank frame is worse than an overlong slide, so
                    # keep showing the current item and wait.  This covers both
                    # an item whose dimensions are unknown and one whose blurred
                    # backdrop is still being built — starting the transition
                    # without the backdrop would show a flat band and then
                    # repaint with the blur part-way through, which reads as a
                    # flicker.
                    if not self._transition_stall_logged:
                        self._transition_stall_logged = True
                        logger.warning(
                            "Transition stalled: next item not ready after %.1fs "
                            "— holding current slide",
                            stall_elapsed,
                        )
                    self._present_current()
                    return
                logger.warning(
                    "Stall exceeded %.0fs — advancing anyway (next item unreadable?)",
                    STALL_TIMEOUT_S,
                )

            self._advance(initial=False)
            return

        # ── Transition ────────────────────────────────────────────────────
        if elapsed >= duration and transition_s > 0:
            progress = min(1.0, (elapsed - duration) / transition_s)
            self._present_transition(progress)
        else:
            self._present_current()

    def _transition_ready(self) -> bool:
        """Whether the next item can be transitioned to right now.

        Two conditions, both required:

        * the next item is laid out (its dimensions have been probed), and
        * if the ambient look is ``blur``, that item's blurred backdrop already
          exists.

        The backdrop test is what implements "do not start a transition until the
        blur has finished": the expensive work is pre-warmed while the current
        slide is showing, and if it has not completed the slide is simply held.
        A non-blur strategy is always ready — there is nothing to wait for.
        """
        next_plan = self._next_plan()
        if next_plan is None:
            return False
        if next_plan.ambient_strategy != "blur":
            return True
        ready = getattr(self._backend, "backdrop_ready", None)
        if ready is None:
            return True  # a backend without backdrops cannot be waiting on one
        nxt = self._queue[(self._current_idx + 1) % len(self._queue)]
        # Non-blocking lookup: this runs every frame while a slide is held, so a
        # synchronous load here would stall exactly the frame we are trying to
        # protect.  A miss means "not ready", which holds the slide — correct.
        handle = self._cache.get(nxt.id)
        if handle is None:
            return False
        return bool(ready(next_plan, handle))

    def represent(self) -> None:
        """Repaint the frame already on screen, immediately.

        Called after a hot reload so a change that alters the layout — the fit
        mode, ``smart_cover``, the panel geometry — is visible on the slide the
        user is looking at rather than on the next one.

        Deliberately NOT a general-purpose "refresh": it re-presents whatever the
        current state already says should be showing, so it cannot skip a slide,
        restart a transition, or disturb the slide clock.  A transition in flight
        is re-presented at its current progress for the same reason.

        Adding a plan is not required — the presenter does not own the overlay,
        and ``render()`` will composite the overlay on the next tick as usual.
        """
        if self._current_idx < 0 or not self._queue:
            return
        if self.current_item is None:
            return

        transition_s = self._transition_seconds()
        if not self._paused and transition_s > 0:
            elapsed = time.monotonic() - self._item_start_time
            duration = self._item_duration(self.current_item)
            if duration <= elapsed < duration + transition_s:
                self._present_transition(min(1.0, (elapsed - duration) / transition_s))
                return

        # A stale outgoing plan would paint a ghost of a layout that no longer
        # applies, so drop it before re-presenting the current item.
        self._prev_plan = None
        self._prev_image = None
        self._present_current()

    def _present_current(self, with_artwork: bool = True) -> None:
        """Show the current item, loading it synchronously if necessary.

        The synchronous fallback is intentional: at this point the alternative is
        an empty frame, and the cache has already had a full slide duration to
        produce the image.
        """
        item = self.current_item
        if item is None:
            return

        plan = self._current_plan()
        if plan is None:
            return

        handle = self._image_for(item)
        if handle is None and with_artwork:
            logger.warning(
                "No image for %s — awaiting the backend's cached copy",
                item.original_path,
            )
        self._backend.present(plan, handle if with_artwork else None)
        self._shown_plan = plan
        self._shown_item = item

    def _present_transition(self, progress: float) -> None:
        """Blend from the shown frame to the next item.

        Both layers are handed to the backend in ONE call rather than two
        ``present()`` calls, because a backend that defers painting (Qt coalesces
        ``update()`` requests into a single ``paintEvent``) would keep only the
        last one stored — the incoming photo would then fade up from the
        background instead of blending into the outgoing one, which is exactly
        the "fade to black, then the next slide appears" defect.

        The eased alphas come from :class:`TransitionEngine`, which also owns
        ``fade_through_black`` (outgoing fades to black, then incoming fades up)
        and the hard cut for ``none``.  Reusing it keeps the easing curves and the
        three styles in one place rather than reimplementing them here.
        """
        next_item = self._queue[(self._current_idx + 1) % len(self._queue)]
        next_plan = self._plan_for(
            MediaSize(next_item.width, next_item.height, self._media_type(next_item))
        )
        next_image = self._image_for(next_item)

        outgoing = self._shown_plan or self._current_plan()
        outgoing_image = self._image_for(self._shown_item) if self._shown_item else None
        cur_alpha = self._transitions.get_alpha(progress, "current")
        next_alpha = self._transitions.get_alpha(progress, "next")

        # Prefer the single-call composite: it is the only form that actually
        # blends on a backend which defers painting.  A backend without it (an
        # older implementation, or a test double) falls back to two paints, which
        # still works on an immediate-mode renderer.
        composite = getattr(self._backend, "present_transition", None)
        if composite is not None:
            composite(
                next_plan,
                next_image,
                next_alpha,
                outgoing,
                outgoing_image,
                cur_alpha,
            )
            return

        if outgoing is not None and cur_alpha > 0.01:
            self._backend.present(outgoing, outgoing_image, alpha=cur_alpha)
        if next_image is not None and next_alpha > 0.01:
            self._backend.present(next_plan, next_image, alpha=next_alpha)

    # -- Advancing -----------------------------------------------------------

    def _write_current_media(self) -> None:
        """Publish the item on screen to ``current_media.json``.

        Drives the dashboard's "Now Playing" card: ``/api/health`` reports
        ``current_media`` from this file, so a queue change that does not
        republish it leaves the UI showing a stale item (or "No media playing")
        indefinitely, because nothing else rewrites the file until the next
        advance.

        The payload is a contract with the SPA and must keep these keys:
        ``file`` (the display name — the card shows "No media playing" when it
        is absent), ``index``/``total`` (rendered as "Image 2 of 17"),
        ``paused`` (the paused badge and pause button), ``media_type`` and
        ``thumbnail_path`` (which the route resolves into ``thumbnail_url``).

        Writes to ``run_dir()``, which is tmpfs — a per-slide write there costs
        RAM, not SD-card erase cycles.  Best-effort: a read-only run dir must not
        stop the slideshow.
        """
        try:
            if self._current_idx < 0 or not self._queue:
                # No item is displayed.  Publish an empty file rather than a
                # record with index=-1: the queue is transiently empty at
                # startup and whenever the backend playlist is cleared while
                # the optimisation pipeline rebuilds.  A published -1 is
                # invisible to the dashboard (which keeps its last rendered
                # value), so the UI stayed stuck on "No media playing" long
                # after playback resumed.  Removing the file lets /api/health
                # report current_media as null and the frontend rewrite it as
                # soon as the first slide is shown.
                current = run_path("current_media.json")
                with contextlib.suppress(FileNotFoundError):
                    current.unlink()
                return

            item = self._queue[self._current_idx]

            # Resolve the thumbnail path:
            # 1. Use the item's thumbnail_path (set by ImageProcessor
            #    or merged from backend playlist).
            # 2. Fall back to the hash-based thumbnail in cache/thumbnails/.
            # 3. For videos: last resort is the raw first-frame cache
            #    (<video>.1.frame).
            thumb = None
            if item.thumbnail_path is not None:
                thumb = str(item.thumbnail_path)
            else:
                # Fall back to hash-based thumbnail lookup.
                # CRITICAL: use original_path (NOT cached_path) because
                # thumbnails are always named after the ORIGINAL file's
                # content hash.  cached_path may point to the optimised
                # cache file whose content differs from the original,
                # producing a different hash that won't match any thumbnail.
                try:
                    file_hash = content_hash(item.original_path)
                    hash_thumb = resolve_install_path("cache/thumbnails") / f"{file_hash}.jpg"
                    if hash_thumb.exists():
                        thumb = str(hash_thumb)
                except OSError:
                    pass

            # Video-only: fall back to first-frame cache (backend-generated)
            if (
                thumb is None
                and item.media_type == MediaType.VIDEO
                and item.first_frame_path is not None
                and item.first_frame_path.exists()
            ):
                thumb = str(item.first_frame_path)

            data = {
                "file": str(item.original_path.name) if item.original_path else "unknown",
                "index": self._current_idx,
                "total": len(self._queue),
                "paused": self._paused,
                "media_type": item.media_type.value,
                "thumbnail_path": thumb,
            }
            atomic_write_json(run_path("current_media.json"), data)
        except OSError:
            pass

    def _advance(self, *, initial: bool) -> None:
        """Move to the next item and begin showing it."""
        if not self._queue:
            return

        # Retain the outgoing frame for the crossfade before the cursor moves.
        if self._shown_plan is not None and not initial:
            self._prev_plan = self._shown_plan
            self._prev_image = self._image_for(self._shown_item) if self._shown_item else None

        self._current_idx = (self._current_idx + 1) % len(self._queue)
        self._item_start_time = time.monotonic()
        self._transition_stall_logged = False

        item = self.current_item
        if item is None:
            return

        # Start decoding what follows this item so the next transition has a
        # chance of being ready on time.
        self._preload_next()

        # Video playback has been removed from the frontend (it will be
        # re-inserted later), so every item is presented as a still.  A video's
        # first-frame JPEG is loaded as an ordinary image, which is all the
        # presentation layer needs.
        self._present_current()

        self._write_current_media()

    def _preload_next(self) -> None:
        """Begin decoding the item after the current one, if there is one."""
        if len(self._queue) < 2:
            return
        nxt = self._queue[(self._current_idx + 1) % len(self._queue)]
        if nxt.media_type == MediaType.VIDEO:
            # A video is shown as its pre-generated first-frame JPEG, which the
            # backend produced during OPTIMISE — there is nothing to decode here.
            return
        if self._cache.get(nxt.id) is not None:
            return
        max_w = int(self._backend.width * 1.2)
        max_h = int(self._backend.height * 1.2)
        self._cache.start_preload(nxt.id, nxt.cached_path, max_w, max_h)
        self._drain_cache()

    def _drain_cache(self) -> None:
        """Upload any finished decode to the backend on this (GUI) thread."""
        ready = self._cache.take_ready()
        if ready is None:
            return
        try:
            handle = self._backend.load_image(ready.data)
        except Exception:
            logger.debug("Failed to upload preloaded image", exc_info=True)
            return
        if handle is not None:
            self._cache.put(ready.key, handle)

    # -- Helpers -------------------------------------------------------------

    def _fit_mode(self) -> str:
        """The configured fit mode, normalised to a supported value.

        A config written by an older release can still hold ``fill`` (stretch),
        which this frontend cannot honour, so an unknown value falls back to
        the default rather than being passed to the framing engine.
        """
        mode = str(self._config.slideshow.get("fit_mode", DEFAULT_FIT_MODE))
        if mode not in _FIT_MODE_TO_OVERFLOW:
            logger.warning("Unsupported slideshow fit_mode %r — using %r", mode, DEFAULT_FIT_MODE)
            return DEFAULT_FIT_MODE
        return mode

    def _shuffle_enabled(self) -> bool:
        """Whether the user asked for a random play order."""
        return bool(self._config.slideshow.get("shuffle", True))

    def _overflow_for(self, media: MediaSize) -> str:
        """The framing overflow to use for *media* under the current settings.

        This is where ``smart_cover`` lives.  In ``cover`` mode an artwork whose
        orientation opposes the panel loses a large part of itself to the crop —
        a portrait photo on a landscape screen is cropped top and bottom — so
        with smart cover on those items are contained instead.  A square artwork
        counts as opposing, because at 1:1 it has as much to lose as a portrait
        one does.

        Orientation is compared against the panel the plan is computed for
        (``LayoutEngine.screen_w`` / ``screen_h``), not the config's nominal
        size, so the decision always matches the geometry the renderer uses.
        """
        mode = self._fit_mode()
        overflow = _FIT_MODE_TO_OVERFLOW[mode]
        if mode != "cover" or not self._config.slideshow.get("smart_cover", True):
            return overflow
        if not media.is_valid:
            return overflow

        screen_ratio = self._layout.screen_w / max(self._layout.screen_h, 1)
        media_ratio = media.width / max(media.height, 1)
        opposes_panel = (screen_ratio >= 1.0 and media_ratio <= 1.0) or (
            screen_ratio < 1.0 and media_ratio >= 1.0
        )
        return _FIT_MODE_TO_OVERFLOW["contain"] if opposes_panel else overflow

    def _plan_for(self, media: MediaSize) -> RenderPlan:
        """Lay out one item, applying the configured fit behaviour."""
        return self._layout.compute(media, overflow=self._overflow_for(media))

    def _current_plan(self) -> RenderPlan | None:
        """Return the layout for the item on screen, computing it once."""
        if self._shown_plan is not None and self._shown_item is self.current_item:
            return self._shown_plan
        item = self.current_item
        if item is None:
            return None
        if item.width <= 0 or item.height <= 0:
            logger.debug("Item %s has no dimensions yet — deferring layout", item.id)
            return None
        return self._plan_for(MediaSize(item.width, item.height, self._media_type(item)))

    def _next_plan(self) -> RenderPlan | None:
        """Return the next item's plan, or ``None`` if it is not ready.

        ``None`` is what drives stall-and-hold: it means the item has no usable
        dimensions yet (the backend has not finished probing it), not that the
        layout is broken.
        """
        if len(self._queue) < 2:
            return None
        nxt = self._queue[(self._current_idx + 1) % len(self._queue)]
        if nxt.width <= 0 or nxt.height <= 0:
            return None
        return self._plan_for(MediaSize(nxt.width, nxt.height, self._media_type(nxt)))

    def _image_for(self, item: MediaItem | None) -> Any:
        """Return a displayable handle for *item*, loading synchronously if needed.

        Tries the cache first (which is populated by :meth:`_preload_next`), then
        falls back to a blocking load — the item is needed *now*, so waiting is
        better than showing nothing.
        """
        if item is None:
            return None
        cached = self._cache.get(item.id)
        if cached is not None:
            return cached

        # A video is presented as its pre-generated first-frame JPEG.
        path = item.first_frame_path if item.media_type == MediaType.VIDEO else None
        path = path or item.cached_path
        try:
            handle = self._backend.load_image(path)
        except Exception:
            logger.debug("Failed to load %s", path, exc_info=True)
            return None
        if handle is not None:
            self._cache.put(item.id, handle)
        return handle

    @staticmethod
    def _media_type(item: MediaItem) -> FramingMediaType:
        """The framing engine's media-type literal.

        Still reported accurately even though video is not *played*: the framing
        engine keys some geometry off the media type, and a video's poster has a
        known aspect that the layout should use.
        """
        return "video" if item.media_type == MediaType.VIDEO else "image"

    def _transition_seconds(self) -> float:
        """Seconds the transition runs for; zero when there is no transition.

        Read from :class:`TransitionEngine` rather than re-derived from the
        config, so the blend's duration and its easing curves cannot disagree
        (the ``none`` style is a hard cut, so it takes no time at all).
        """
        if self._transitions.style == "none":
            return 0.0
        return self._transitions.duration_s

    def _item_duration(self, item: MediaItem) -> float:
        """Seconds to show *item*.

        With video playback removed there is nothing to play, so every item —
        including a video's poster — is shown for the configured image duration.
        """
        return float(self._config.slideshow.get("image_duration_seconds", 30))

    # -- Overlay integration -------------------------------------------------

    def overlay_elements(self) -> list[OverlayElement]:
        """Elements the presenter contributes to the overlay pass.

        Empty today — the presenter paints the frame, not chrome.  It exists so
        the renderer has one place to ask, and so the 2.1.0 on-screen menu has an
        obvious home that is not the slideshow's timing code.
        """
        return []
