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

    // -- Media: backdrop + artwork, as ONE group per item --------------------
    //
    // There are two media slots — outgoing and incoming — and each carries its
    // OWN backdrop.  They are declared as a single `Item` per slot further down,
    // with `opacity` on the item so a backdrop and its artwork can never fade
    // apart.  Nothing here is painted loose; see the Layer 2 note below.
    //
    // Two opacity properties, deliberately separate:
    //
    // * `artworkOpacity` — the incoming item's crossfade progress.  Drives the
    //   incoming group.
    // * `prevBackdropOpacity` — the OUTGOING group's own opacity, fed straight
    //   from the transition engine's `current` alpha.  It is 1.0 for the whole
    //   of a crossfade (the outgoing item is meant to stay opaque and be covered
    //   from above) and ramps to 0 for `fade_through_black`, where the outgoing
    //   item genuinely has to dissolve away.
    //
    // They cannot be one property.  A still re-presents itself on every tick, so
    // `artworkOpacity` is 1.0 throughout a non-transition frame; the outgoing
    // item's alpha comes from the transition engine instead.
    //
    // NOTE: `prevBackdropOpacity` was once forced to 0.0 whenever the two items
    // differed, on the reasoning that the outgoing layer should be "covered, not
    // dissolved".  Collapsing it to 0 does the opposite of covering — it ERASES
    // the outgoing item, so the only thing under the incoming image is the flat
    // `background` Rectangle.  The incoming backdrop then faded in on the group's
    // clock while that flat colour did not move at all, which is why the blur
    // appeared to transition at a different rate to the image.  Feeding the
    // engine's own value is what keeps a backdrop and its artwork moving together.
    //
    // `ambientX/Y/W/H` and `ambientColour` stay global on purpose — they are the
    // flat full-canvas fill, not a per-item image.
    property url ambientSource: ""
    property color ambientColour: "#000000"
    property bool ambientVisible: true
    property real ambientX: 0
    property real ambientY: 0
    property real ambientW: 0
    property real ambientH: 0

    // Outgoing item's backdrop.  `prevAmbientColour` doubles as the canvas
    // background while the outgoing item still owns the screen.
    property url prevAmbientSource: ""
    property color prevAmbientColour: "#000000"
    property bool prevAmbientVisible: false
    property real prevAmbientX: 0
    property real prevAmbientY: 0
    property real prevAmbientW: 0
    property real prevAmbientH: 0
    property real prevBackdropOpacity: 0.0

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
    // Layer 1 — BACKGROUND (flat fill only)
    //
    // The base colour, painted before anything else so a JPEG that fails to
    // decode leaves a colour rather than a hole.  The *blurred* backdrop is NOT
    // here — it belongs to its media item, see Layer 2.
    //
    // It follows the OUTGOING media's colour while a transition is running, so
    // the canvas cannot flash the incoming colour behind an outgoing item that is
    // still opaque.
    // =========================================================================
    Rectangle {
        objectName: "background"
        anchors.fill: parent
        color: root.prevAmbientVisible ? root.prevAmbientColour : root.ambientColour
        visible: root.ambientVisible
    }

    // =========================================================================
    // Layer 2 — MEDIA
    //
    // QML stacks by DECLARATION ORDER, so the sequence in this file bottom-to-top
    // is: background → media → video → rings → overlay.  Nothing may be assumed
    // about the order; it has to be the order.
    //
    // The media layer is deliberately a PAIR — the outgoing item and the incoming
    // item — each drawn as a GROUP of backdrop + artwork inside one item-level
    // opacity.
    //
    // The grouping is the whole point, and stacking the same six layers loose
    // instead is a bug this file shipped:
    //
    //     prevAmbient (1.0)  ambient (1.0)  prevArtwork (1.0)  artwork (0.4)
    //
    // reads bottom-to-top as prevAmbient, ambient, prevArtwork, artwork — so the
    // INCOMING backdrop is painted UNDER the OUTGOING artwork.  At the ambient
    // band's edges, where the artwork does not cover, the incoming item's blur
    // showed through the outgoing item for the whole fade.  A backdrop is part of
    // its media, not a separate layer beneath both of them.
    //
    // So: one `Item` per item, `opacity` on the item, backdrop and artwork as
    // children.  Children inherit the parent's opacity, so a backdrop and its
    // artwork can never drift apart — they fade as one thing, in, and one thing,
    // out.
    //
    // Order within the group also matters: the backdrop is UNDER its own artwork,
    // which is what lets a `cover`-fit artwork hide the band completely, and lets
    // a `contain`-fit artwork show it as its letterbox.  That is why the
    // backdrop's own opacity stays 1 and only the group is faded.
    // =========================================================================

    // -- Outgoing item -------------------------------------------------------
    Item {
        id: prevMedia
        objectName: "prevMedia"
        // Suppressed for a still: see the incoming item's note on why the
        // outgoing artwork must stay opaque except when the media changed.
        opacity: root.prevBackdropOpacity
        visible: root.prevAmbientVisible || root.prevArtworkOpacity > 0.001

        Image {
            id: prevAmbient
            objectName: "prevAmbient"
            x: root.prevAmbientX
            y: root.prevAmbientY
            width: root.prevAmbientW
            height: root.prevAmbientH
            source: root.prevAmbientSource
            fillMode: Image.Stretch
            // The ambient band is already blurred and scaled by the time it
            // reaches us, so smoothing only adds sampling cost with nothing to
            // gain.
            smooth: false
            cache: false
            visible: root.prevAmbientVisible && root.prevAmbientW > 0
        }

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
            visible: root.prevArtworkW > 0 && root.prevArtworkH > 0
        }
    }

    // -- Incoming item -------------------------------------------------------
    Item {
        id: media
        objectName: "media"
        // The item's own fade.  For a crossfade `artworkOpacity` runs 1.0 → the
        // transition's incoming alpha, so the group's backdrop fades with its
        // artwork; for a still it is left at 1.0 and this is effectively a no-op.
        opacity: root.artworkOpacity
        visible: root.ambientVisible || root.artworkW > 0

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
            visible: root.artworkW > 0 && root.artworkH > 0
        }
    }

    // =========================================================================
    // Layer 3 — video
    //
    // Declared above the media group because it SUPERSEDES it: while a video
    // plays, it covers the poster that is already in the artwork slot.  The rings
    // then composite over the video, which is why nothing here has to paint its
    // own matte — the only position that is correct on both sides, and the reason
    // `fillMode` is Stretch: `plan.artwork_dst` already carries the correct aspect
    // ratio for the fit mode, so letterboxing inside it again would add redundant
    // padding.
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
    // Layer 4 — RINGS: whitespace, mat, moulding
    //
    // Each is a `plan.<layer>` tuple of disjoint rects, passed as JS arrays of
    // {x, y, w, h} and drawn by a Repeater, because the count and geometry are
    // computed by `framing/layout.py` and must stay there — a backend holding
    // layout knowledge is exactly what the ABC forbids.
    //
    // Declared AFTER the media so they composite OVER it.  This is the layering
    // that makes a video work without any special case: the video is media like
    // any other, so the rings cover it by declaration order rather than by the
    // video painting its own matte.
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
    // Layer 5 — overlay (boot screen, notifications, and the reserved OSD)
    //
    // Closest to the viewer: nothing may cover it.  The order below this point is
    // media → rings → overlay, so the boot screen and a notification sit above the
    // mat and the artwork as they must.
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
