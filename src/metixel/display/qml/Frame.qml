// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
//
// The Metixel frame surface, as a Qt Quick scene.
//
// This file is the WHOLE of the rendering surface.  `QmlBackend` (qt_qml_backend.py)
// owns the process, the window and the media pipeline, and it writes one
// `RenderPlan` into the properties below on every `present()`.  Nothing here
// knows about slideshows, queues or config — it is a pure function of those
// properties, which is what keeps the framing maths testable without a GPU.
//
// ## Paint order is the framing specification, not a style choice
//
// `DisplayBackend.present()` mandates:
//
//     ambient fill -> artwork -> whitespace -> mat -> moulding
//
// and that order is load-bearing: ambient fill is the ONLY full-canvas layer,
// the rest are annuli disjoint from the artwork, so a single pass in this order
// needs no depth buffer.  QML stacks by declaration order, so the declarations
// below ARE the spec.  Do not reorder them.
//
// ## Why this replaces the raster canvas rather than sitting beside it
//
// The old surface needed a *hole*: `FrameCanvas` left `plan.artwork_dst`
// unpainted and a sibling `QOpenGLWidget` showed through it, which required
// `QRegion.subtracted` + `setClipRegion`, toggling `WA_OpaquePaintEvent` /
// `WA_TranslucentBackground`, explicit `raise_()` z-order, and a reveal step
// that withheld the hole until the first video frame existed (otherwise the
// unpainted rectangle showed undefined content — black).
//
// In one scene graph none of that exists.  The video is simply an item declared
// BELOW the ring items, so the rings composite over it automatically; there is
// no unpainted region to reveal and therefore no reveal ordering to get wrong.
//
// ## What this buys, measured
//
//  - Pacing: the scene graph renders on the compositor's vsync clock, not a
//    QTimer.  Measured 59.91 fps presented / 99.72% of frames vsync-locked on a
//    60 fps production-format clip.  The raster path was driven by
//    `display.fps_limit` (30 on the frame) and had no vsync discipline at all.
//  - CPU: 35.5% of one core for the same clip, against 182% for the libmpv
//    software renderer (which does decode -> CPU scale+convert -> GL upload).
//    Here the GPU does the scaling: `Image.sourceRect` crops and the scene
//    graph scales.
//  - Retained mode: an unchanged frame costs nothing.  The raster canvas
//    repainted its whole 1920x1200 surface every tick — measured at 83% of a
//    core to recomposite a frame that had not changed, against 0.9% for an idle
//    Qt event loop.
//
// ## Deliberately NOT used here
//
//  - **No live blur effect.**  Production blurs offline in `ambient_blur.py` (a
//    throttled subprocess) and hands us a pre-blurred JPEG, so `MultiEffect`
//    would both duplicate work and misrepresent the load.  The ambient band is
//    a plain `Image`.  (MultiEffect lives in qml6-module-qtquick-effects, which
//    is installed, so this is a deliberate choice and not a missing module.)
//  - **No Qt Quick Controls.**  Nothing here needs a styled control, and pulling
//    the Controls style in would add a theme we would then have to override.
//  - **No QtCanvas2D / Canvas Painter.**  It is GPLv3-or-commercial only (no
//    LGPL) and Technology Preview, and Metixel is Apache-2.0.

import QtQuick
import QtQuick.Window
import QtMultimedia

Window {
    id: root
    objectName: "frameRoot"

    // -- Screen ---------------------------------------------------------------
    property int screenW: 1920
    property int screenH: 1200
    width: screenW
    height: screenH
    visible: true
    color: backColour

    // -- Ambient fill (RenderPlan.ambient / ambient_colour / ambient_strategy) --
    //
    // The only full-canvas layer.  `ambientSource` is the pre-blurred JPEG
    // produced during Phase 2 OPTIMISE; when it is empty the band is the flat
    // `ambientColour` instead (the "solid" ambient strategy).  Both forms exist
    // because a solid fill is correct when the artwork is itself the source and
    // blurring it would be circular.
    property url ambientSource: ""
    property color ambientColour: "#000000"
    property bool ambientVisible: true
    property real ambientX: 0
    property real ambientY: 0
    property real ambientW: 0
    property real ambientH: 0

    // Outgoing ambient, kept so a crossfade does not leave the band snapping to
    // the incoming item's colour while the artwork is still fading.
    property url prevAmbientSource: ""
    property color prevAmbientColour: "#000000"
    property bool prevAmbientVisible: false
    property real prevAmbientOpacity: 0.0
    property real prevAmbientX: 0
    property real prevAmbientY: 0
    property real prevAmbientW: 0
    property real prevAmbientH: 0

    // Background shown where no ambient layer covers.  Kept separate from
    // `ambientColour` because the renderer clears to the matte colour before the
    // first frame arrives.
    property color backColour: "#000000"

    // -- Artwork (RenderPlan.artwork_dst / artwork_src / alpha) ---------------
    //
    // `artworkSrcX/Y/W/H` is `plan.source_window()` — the source crop for
    // cover/contain fit.  Feeding it to `Image.sourceClipRect` means Qt crops in the
    // scene graph; the raster canvas instead pre-scaled to a QPixmap and cached
    // it, which cost memory per slide and a rescale on every cache miss.
    //
    // The property is `sourceClipRect` (Qt Quick 6), NOT `sourceRect` — the wrong
    // name fails at load with "Cannot assign to non-existent property", which is a
    // scene that will not instantiate at all.
    property url artworkSource: ""
    property real artworkX: 0
    property real artworkY: 0
    property real artworkW: 0
    property real artworkH: 0
    property real artworkSrcX: 0
    property real artworkSrcY: 0
    property real artworkSrcW: 0
    property real artworkSrcH: 0
    property real artworkOpacity: 1.0

    // Outgoing artwork for a crossfade.  Only meaningful during a transition;
    // `prevArtworkOpacity` is 0 otherwise, so the item costs nothing.
    property url prevArtworkSource: ""
    property real prevArtworkX: 0
    property real prevArtworkY: 0
    property real prevArtworkW: 0
    property real prevArtworkH: 0
    property real prevArtworkSrcX: 0
    property real prevArtworkSrcY: 0
    property real prevArtworkSrcW: 0
    property real prevArtworkSrcH: 0
    property real prevArtworkOpacity: 0.0

    // -- Rings: whitespace, matte, moulding ----------------------------------
    //
    // Each is a `plan.<layer>` tuple of disjoint rects.  Passed as JS arrays of
    // {x, y, w, h} and drawn by a Repeater, because the count and geometry are
    // computed by `framing/layout.py` and must stay there — a backend holding
    // layout knowledge is exactly what the ABC forbids.
    property var whitespaceRects: []
    property color whitespaceColour: "#ffffff"
    property var matteRects: []
    property color matteColour: "#14141a"
    property var mouldingRects: []
    property color mouldingColour: "#000000"

    // -- Video ---------------------------------------------------------------
    //
    // One `VideoOutput` inside the artwork rectangle, declared BELOW the rings.
    //
    // `videoVisible` replaces the raster canvas's reveal logic.  There, the hole
    // had to stay covered until `video_ready()` reported a decoded frame, or the
    // unpainted rectangle showed undefined content.  Here the poster artwork is
    // simply drawn UNDER the video, so a video that has not produced a frame yet
    // shows the poster rather than a hole — and the switch is one boolean with
    // no ordering hazard.
    property bool videoVisible: false
    property real videoX: 0
    property real videoY: 0
    property real videoW: 0
    property real videoH: 0

    // -- Overlay (present_overlay) -------------------------------------------
    //
    // Overlay elements animate every frame, unlike the frame itself, which is
    // composited once per item — the ABC keeps them as separate entry points for
    // that reason.  Each entry is one `OverlayElement` in the shape
    // `_overlay_entry` builds — {kind, x, y, w, h, colour, alpha, text, size,
    // source, rotation} — and the renderer has already flattened and z-sorted
    // the list before handing it over.
    property var overlayElements: []

    // =========================================================================
    // Layer 1 — ambient fill (full canvas)
    // =========================================================================
    Rectangle {
        // Flat fallback, always present under the image so a JPEG that fails to
        // decode leaves the ambient colour rather than a hole.
        anchors.fill: parent
        color: root.prevAmbientVisible ? root.prevAmbientColour : root.ambientColour
        visible: root.ambientVisible
    }

    Image {
        id: prevAmbient
        objectName: "prevAmbient"
        x: root.prevAmbientX
        y: root.prevAmbientY
        width: root.prevAmbientW
        height: root.prevAmbientH
        source: root.prevAmbientSource
        fillMode: Image.Stretch
        // The ambient band is already blurred and scaled by the time it reaches
        // us, so smoothing only adds sampling cost with nothing to gain.
        smooth: false
        cache: false
        opacity: root.prevAmbientOpacity
        visible: root.prevAmbientVisible && root.prevAmbientOpacity > 0.001 && root.prevAmbientW > 0
    }

    Image {
        id: ambient
        objectName: "ambient"
        x: root.ambientX
        y: root.ambientY
        width: root.ambientW
        height: root.ambientH
        source: root.ambientSource
        fillMode: Image.Stretch
        smooth: false
        cache: false
        visible: root.ambientVisible && root.ambientW > 0 && root.ambientH > 0
    }

    // =========================================================================
    // Layer 2 — artwork (the incoming slide's photo, or a video's poster)
    // =========================================================================
    Image {
        id: prevArtwork
        objectName: "prevArtwork"
        x: root.prevArtworkX
        y: root.prevArtworkY
        width: root.prevArtworkW
        height: root.prevArtworkH
        source: root.prevArtworkSource
        fillMode: Image.Stretch
        cache: false
        sourceClipRect: Qt.rect(
            root.prevArtworkSrcX,
            root.prevArtworkSrcY,
            root.prevArtworkSrcW,
            root.prevArtworkSrcH
        )
        opacity: root.prevArtworkOpacity
        visible: root.prevArtworkOpacity > 0.001 && root.prevArtworkW > 0
    }

    Image {
        id: artwork
        objectName: "artwork"
        x: root.artworkX
        y: root.artworkY
        width: root.artworkW
        height: root.artworkH
        source: root.artworkSource
        fillMode: Image.Stretch
        cache: false
        sourceClipRect: Qt.rect(
            root.artworkSrcX,
            root.artworkSrcY,
            root.artworkSrcW,
            root.artworkSrcH
        )
        opacity: root.artworkOpacity
        visible: root.artworkW > 0 && root.artworkH > 0
    }

    // =========================================================================
    // Layer 3 — video
    //
    // Declared here because QML stacks by declaration order, and this is the only
    // position that is correct on BOTH sides: ABOVE the artwork poster (so the
    // video covers the poster once frames arrive) and BELOW the ring layers (so
    // the mat composites over the video).  Declaring it in the properties area
    // would make it the bottom-most item and the opaque ambient rectangle would
    // hide it completely.
    //
    // This single position is what removes the raster canvas's whole video-hole
    // apparatus: there is no unpainted region, so no clip region, no
    // translucency toggle, no sibling z-order and no reveal ordering.
    //
    // `fillMode` is Stretch on purpose: `plan.artwork_dst` already carries the
    // correct aspect ratio for the fit mode (cover/contain), so letterboxing
    // inside it again would add redundant padding — the same reason the raster
    // backend rounded the rect OUTWARD to whole pixels.
    // =========================================================================
    VideoOutput {
        id: videoOut
        objectName: "videoOut"
        x: root.videoX
        y: root.videoY
        width: root.videoW
        height: root.videoH
        visible: root.videoVisible && root.videoW > 0 && root.videoH > 0
        fillMode: VideoOutput.Stretch
    }

    // =========================================================================
    // Layer 4 — whitespace, Layer 5 — mat, Layer 6 — moulding
    //
    // Each is `plan.<layer>`: disjoint rectangles.  Declared in the order the
    // framing spec mandates, so the later layers paint over the earlier ones and
    // the white mount reads as being under the mat rather than over it.
    // =========================================================================
    Repeater {
        objectName: "whitespace"
        model: root.whitespaceRects
        delegate: Rectangle {
            x: modelData.x
            y: modelData.y
            width: modelData.w
            height: modelData.h
            color: root.whitespaceColour
        }
    }

    Repeater {
        objectName: "matte"
        model: root.matteRects
        delegate: Rectangle {
            x: modelData.x
            y: modelData.y
            width: modelData.w
            height: modelData.h
            color: root.matteColour
        }
    }

    Repeater {
        objectName: "moulding"
        model: root.mouldingRects
        delegate: Rectangle {
            x: modelData.x
            y: modelData.y
            width: modelData.w
            height: modelData.h
            color: root.mouldingColour
        }
    }

    // =========================================================================
    // Layer 7 — overlay (boot screen, messages, clock)
    //
    // The delegate draws whichever of the three kinds the element is, and it has
    // to handle all three.  The boot screen is a black curtain, a logo, a spinner
    // and a progress bar — rects and images with no text whatsoever — and a
    // message is a rect panel with text drawn over it.  A text-only delegate does
    // not degrade gracefully for either: the boot screen paints nothing at all,
    // and the message loses its panel and, having no position, stacks every line
    // in one corner.
    //
    // One `Item` per element, positioned and faded from the element itself, with
    // the three shapes as children so geometry, opacity and stacking come from a
    // single place.  Each child sizes from the element's rect rather than being
    // anchored to the parent, because a text element's rect is deliberately
    // (x, y, 0, 0) — anchoring would clip it to nothing.
    // =========================================================================
    Repeater {
        objectName: "overlay"
        model: root.overlayElements
        delegate: Item {
            objectName: "overlayDelegate"
            x: modelData.x
            y: modelData.y
            opacity: modelData.alpha

            // "rect" — panel backgrounds, the boot curtain, the progress bar.
            Rectangle {
                objectName: "overlayRect"
                visible: modelData.kind === "rect"
                width: modelData.w
                height: modelData.h
                color: modelData.colour
            }

            // "image" — the boot logo and its spinner.  The angle arrives per
            // frame from the layer, so the spin needs no animation of its own.
            Image {
                objectName: "overlayImage"
                visible: modelData.kind === "image"
                width: modelData.w
                height: modelData.h
                source: modelData.source
                fillMode: Image.Stretch
                cache: false
                transform: Rotation {
                    origin.x: modelData.w / 2
                    origin.y: modelData.h / 2
                    angle: modelData.rotation
                }
            }

            // "text" — messages, and the boot screen's own lines.
            Text {
                objectName: "overlayText"
                visible: modelData.kind === "text"
                text: modelData.text
                color: modelData.colour
                font.pixelSize: modelData.size
            }
        }
    }
}
