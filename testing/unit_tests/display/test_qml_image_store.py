# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""The QML artwork store must not grow without limit — nor drop what is on screen.

``QmlBackend.load_image`` decodes to a ``QImage`` and keeps it in an image
provider for the scene to fetch by URL.  That store owns a *strong* reference:
Qt uploads the image into a texture and does not take ownership, and nothing on
the Qt side reference-counts on our behalf.  A handle that is never passed to
``unload_image`` is held for the life of the process.

On the frame this leaked exactly one full-resolution image per slide — ~1 GB in
ten minutes, then ``Out of memory: Killed process <frontend>
anon-rss:1021456kB``, then a systemd restart, on repeat.  Callers cannot be relied
on to release: the presenter loads a video's last frame deliberately WITHOUT
caching it, so no cache eviction can ever see that handle.

The first fix for that was a cap plus an eager release, and it traded a leak for a
*visible* defect: with the image dropped the moment the presenter was done with
it, the scene asked for a handle that was already gone and the outgoing layer of a
crossfade went blank —

    QML Image: Failed to get image from provider: image://metixel/d0d3dad8…
    ... Frame.qml:227:5          (line 227 is ``Image { id: prevArtwork }``)

because ``prevArtwork`` re-requests its source for the whole of a fade.  So the
store now pins whatever the scene has actually *requested* (``SERVED_WINDOW``) and
defers a release until the handle leaves that window.

Both rules are asserted below, and the policy tests deliberately need no Qt — the
Qt-gated ones skip on a machine without PySide6 (the dev machine and CI), and that
is precisely how the second bug got out.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from metixel.display.qt_qml_backend import MAX_STORED_IMAGES, SERVED_WINDOW, ArtworkStore

_ROOT = Path(__file__).resolve().parents[3]
_BACKEND = _ROOT / "src" / "metixel" / "display" / "qt_qml_backend.py"


def _code(name: str) -> str:
    """A function's executable statements, docstring and comments stripped."""
    source = _BACKEND.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            return "\n".join(ast.unparse(stmt) for stmt in body)
    raise AssertionError(f"{name} not found in {_BACKEND.name}")


class TestTheStorePolicy:
    """The two rules, tested without Qt so CI actually covers them.

    Rule 1 is the leak; rule 2 is the blank ``prevArtwork`` layer.  Both were seen
    on the frame before they were tested here.
    """

    def test_the_store_is_bounded(self) -> None:
        store = ArtworkStore(max_images=3)
        for i in range(20):
            store.add(f"k{i}", i)
        assert len(store) == 3

    def test_an_image_the_scene_asked_for_is_never_evicted(self) -> None:
        """QML re-requests a source for as long as it is painting it."""
        store = ArtworkStore(max_images=1)
        store.add("live", "artwork")
        store.serve("live")
        for i in range(10):
            store.add(f"noise{i}", i)
        assert store.serve("live") == "artwork", "the on-screen layer was evicted"

    def test_releasing_an_image_the_scene_is_still_painting_keeps_it(self) -> None:
        """The Frame.qml:227 failure exactly: presenter releases, prevArtwork asks."""
        store = ArtworkStore()
        store.add("outgoing", "artwork")
        store.serve("outgoing")  # the crossfade is painting it
        store.release("outgoing")  # the presenter's LRU drops its reference
        assert store.serve("outgoing") == "artwork", "the outgoing layer went blank"

    def test_releasing_something_nobody_asked_for_drops_it_at_once(self) -> None:
        store = ArtworkStore()
        store.add("unused", "image")
        store.release("unused")
        assert "unused" not in store

    def test_a_deferred_release_is_honoured_once_it_is_no_longer_served(self) -> None:
        """Deferred must not mean forgotten, or the leak comes back."""
        store = ArtworkStore(served_window=1)
        store.add("old", "image")
        store.serve("old")
        store.release("old")
        store.add("newer", "image")
        store.serve("newer")  # pushes "old" out of the served window
        assert "old" not in store, "a released handle was never actually freed"

    def test_serving_an_unknown_key_returns_none(self) -> None:
        assert ArtworkStore().serve("never-loaded") is None

    def test_clear_drops_pins_for_teardown(self) -> None:
        store = ArtworkStore()
        store.add("k", "v")
        store.serve("k")
        store.clear()
        assert len(store) == 0


class TestTheStoreRecoversRatherThanBlanking:
    """Rule 3: the served window is a heuristic, so a miss must be recoverable.

    The window covers the requests the scene is *known* to make.  It cannot be a
    proof, because ``ImageCache`` (3 entries) and this store (6) are bounded
    independently and neither knows the other's budget — so a release can arrive
    from a direction the window never anticipated, and a null image is the result.

    On the frame that showed up as the artwork layers failing while the ambient
    ones did not, which is the same asymmetry by construction: an ambient source is
    a scratch FILE on disk that nothing evicts, whereas an artwork source is a
    provider key in this store.  Retaining the source removes the asymmetry.
    """

    @staticmethod
    def _decoded(source: object) -> str:
        return f"decoded:{source}"

    def test_an_evicted_key_is_re_decoded_on_demand(self) -> None:
        store = ArtworkStore(max_images=1, decode=self._decoded)
        store.add("k", "image", source="photo.jpg")
        for i in range(5):  # evict it under the cap
            store.add(f"noise{i}", i)

        assert store.serve("k") == "decoded:photo.jpg", "the layer went blank instead of recovering"

    def test_a_released_key_is_re_decoded_on_demand(self) -> None:
        """The presenter's LRU release is exactly this path."""
        store = ArtworkStore(decode=self._decoded)
        store.add("k", "image", source="photo.jpg")
        store.release("k")
        assert "k" not in store, "precondition: the release dropped it"

        assert store.serve("k") == "decoded:photo.jpg"

    def test_a_key_with_no_source_still_returns_none(self) -> None:
        """A handle nothing can recover must degrade, not raise."""
        store = ArtworkStore(decode=self._decoded)
        store.add("k", "image")  # no source
        store.release("k")

        assert store.serve("k") is None

    def test_no_decoder_still_returns_none(self) -> None:
        """The store stays usable without Qt."""
        store = ArtworkStore()
        store.add("k", "image", source="photo.jpg")
        store.release("k")

        assert store.serve("k") is None

    def test_a_failing_decoder_is_swallowed(self) -> None:
        def boom(_source: object) -> str:
            raise RuntimeError("file vanished")

        store = ArtworkStore(decode=boom)
        store.add("k", "image", source="gone.jpg")
        store.release("k")

        assert store.serve("k") is None  # must not raise into the scene graph

    def test_a_decoder_returning_nothing_is_handled(self) -> None:
        store = ArtworkStore(decode=lambda _s: None)
        store.add("k", "image", source="gone.jpg")
        store.release("k")

        assert store.serve("k") is None

    def test_recovery_still_respects_the_cap(self) -> None:
        """Recovering must not reopen the unbounded leak."""
        store = ArtworkStore(max_images=2, decode=self._decoded)
        for i in range(3):
            store.add(f"k{i}", f"image{i}", source=f"s{i}")

        assert len(store) <= 2, "recovery must not grow the store past its cap"

    def test_clear_drops_the_sources_too(self) -> None:
        """Teardown must not leave a store that can resurrect images."""
        store = ArtworkStore(decode=self._decoded)
        store.add("k", "image", source="photo.jpg")
        store.clear()

        assert store.serve("k") is None


class TestTheBackendWiring:
    """A correct store is useless if the backend does not route through it."""

    def test_load_image_stores_through_the_store(self) -> None:
        assert "self._images.add(" in _code("load_image")

    def test_load_image_retains_the_source_for_recovery(self) -> None:
        """Without the source a miss can only be answered with a blank layer."""
        assert "source=path" in _code("load_image")

    def test_the_store_is_given_the_real_decoder(self) -> None:
        """A recovery path with no decoder would silently do nothing."""
        source = _BACKEND.read_text(encoding="utf-8")

        assert "ArtworkStore(decode=self._decode)" in source, (
            "the store can only re-decode if the backend hands it the decoder"
        )

    def test_unload_image_releases_through_the_store(self) -> None:
        assert "self._images.release(" in _code("unload_image")

    def test_the_provider_serves_so_that_it_pins(self) -> None:
        assert ".serve(image_id)" in _code("requestImage")

    def test_there_is_no_second_lock_to_get_out_of_step(self) -> None:
        """The store owns its own lock; a parallel one could only diverge."""
        assert "_images_lock" not in _BACKEND.read_text(encoding="utf-8")

    def test_the_bounds_are_a_small_multiple_of_what_the_scene_holds(self) -> None:
        """Two layers during a crossfade (each with a backdrop) plus decode-ahead."""
        for value in (MAX_STORED_IMAGES, SERVED_WINDOW):
            assert 3 <= value <= 8, "too small blanks a crossfade, too large wastes RAM"


def _backend() -> object:
    pytest.importorskip("PySide6", reason="PySide6 not installed (no Qt on this host)")
    from metixel.display.qt_qml_backend import QmlBackend

    # Constructing the backend does not touch Qt: no QGuiApplication is created
    # until create(), and load_image only needs QImage, which is fine headless.
    return QmlBackend()


def _png() -> bytes:
    """A real 4x4 PNG payload.

    Not ``b""``: that decodes to a null ``QImage``, ``load_image`` would return
    ``None`` every time, and the store assertions below would pass vacuously.
    """
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class TestTheStoreBehaves:
    def test_the_store_stops_growing(self) -> None:
        from metixel.display.qt_qml_backend import MAX_STORED_IMAGES, QmlBackend

        backend = _backend()
        assert isinstance(backend, QmlBackend)

        handles = [backend.load_image(_png()) for _ in range(MAX_STORED_IMAGES + 4)]

        assert all(h is not None for h in handles)
        assert len(backend._images) == MAX_STORED_IMAGES

    def test_unloading_frees_the_entry(self) -> None:
        from metixel.display.qt_qml_backend import QmlBackend

        backend = _backend()
        assert isinstance(backend, QmlBackend)

        handle = backend.load_image(_png())
        assert len(backend._images) == 1

        backend.unload_image(handle)

        assert not backend._images, "the presenter releases handles, so this must actually free"

    def test_an_unknown_handle_is_ignored(self) -> None:
        """A release racing a teardown must not raise into the slideshow."""
        from metixel.display.qt_qml_backend import QmlBackend

        backend = _backend()
        assert isinstance(backend, QmlBackend)

        backend.unload_image("image://metixel/never-existed")
        backend.unload_image(None)
