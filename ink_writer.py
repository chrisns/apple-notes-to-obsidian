"""Build Ink-plugin .writing files (tldraw v2.4) that wrap a raster image.

The Ink plugin embeds a `.writing` file via a `handwritten-ink` code block. The
file is a tldraw document; the plugin renders the inline `previewUri` SVG as the
embed thumbnail and opens the full canvas on click. By placing the source PNG as
a tldraw `image` asset/shape inside the standard writing canvas, the drawing
remains visible *and* the user can ink/annotate over it.

We aim for byte-for-byte compatibility with the schema produced by Ink 0.3.4 /
tldraw 2.4.3 (the version referenced by the user's existing handwriting example
note).
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
from pathlib import Path

PLUGIN_VERSION = "0.3.4"
TLDRAW_VERSION = "2.4.3"

# Use the exact schema the Ink plugin's "create handwriting section" command
# emits when it makes a fresh .writing file. tldraw's loader migrates schema-v1
# snapshots forward through its built-in migration sequence; our previous v2
# snapshot wasn't recognised by the plugin's writing-canvas init path, so the
# editor never engaged on click.
SCHEMA = {
    "schemaVersion": 1,
    "storeVersion": 4,
    "recordVersions": {
        "asset": {
            "version": 1,
            "subTypeKey": "type",
            "subTypeVersions": {"image": 2, "video": 2, "bookmark": 0},
        },
        "camera": {"version": 1},
        "document": {"version": 2},
        "instance": {"version": 21},
        "instance_page_state": {"version": 5},
        "page": {"version": 1},
        "shape": {
            "version": 3,
            "subTypeKey": "type",
            "subTypeVersions": {
                "group": 0,
                "text": 1,
                "bookmark": 1,
                "draw": 1,
                "geo": 7,
                "note": 4,
                "line": 1,
                "frame": 0,
                "arrow": 1,
                "highlight": 0,
                "embed": 4,
                "image": 2,
                "video": 1,
                "writing-container": 0,
            },
        },
        "instance_presence": {"version": 5},
        "pointer": {"version": 1},
    },
}


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Read width/height from a PNG IHDR chunk without pulling in Pillow."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Walk JPEG segments to find a SOF marker and return its width/height."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG file")
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            raise ValueError("invalid JPEG (lost sync)")
        # Skip pad bytes.
        while i < len(data) and data[i] == 0xFF:
            i += 1
        marker = data[i]
        i += 1
        # Standalone markers (no length).
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > len(data):
            raise ValueError("truncated JPEG")
        seg_len = struct.unpack(">H", data[i : i + 2])[0]
        # Start-of-frame markers.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 3 : i + 7])
            return w, h
        i += seg_len
    raise ValueError("no SOF marker found in JPEG")


def image_dimensions(data: bytes, mime: str) -> tuple[int, int]:
    if mime == "image/png":
        return png_dimensions(data)
    if mime in ("image/jpeg", "image/jpg"):
        return jpeg_dimensions(data)
    raise ValueError(f"unsupported mime type {mime}")


def _short_id(prefix: str, salt: str) -> str:
    """tldraw uses ~21-char base62-ish ids; sha1 hex truncated is good enough."""
    return f"{prefix}:{hashlib.sha1(salt.encode('utf-8')).hexdigest()[:21]}"


def build_writing(image_bytes: bytes, mime: str, source_name: str) -> dict:
    """Construct a tldraw document wrapping the image.

    The image is centred inside a writing canvas sized to fit it (with a
    100-px margin) but never smaller than the plugin's default 2000x825
    so existing writing-lines spacing still feels natural.
    """
    w, h = image_dimensions(image_bytes, mime)
    margin = 100
    canvas_w = max(2000, w + margin * 2)
    canvas_h = max(825, h + margin * 2)

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_uri = f"data:{mime};base64,{b64}"

    page_id = _short_id("page", source_name + ":page")
    asset_id = _short_id("asset", source_name + ":asset")
    shape_id = _short_id("shape", source_name + ":image")

    img_x = (canvas_w - w) / 2
    img_y = (canvas_h - h) / 2

    store: dict[str, dict] = {
        "document:document": {
            "gridSize": 10,
            "name": "",
            "meta": {},
            "id": "document:document",
            "typeName": "document",
        },
        page_id: {
            "meta": {},
            "id": page_id,
            "name": "Handwritten Note",
            "index": "a1",
            "typeName": "page",
        },
        "shape:writing-lines": {
            "x": 0,
            "y": 0,
            "rotation": 0,
            "isLocked": False,
            "opacity": 1,
            "meta": {"savedH": canvas_h / 2.2},
            "type": "writing-lines",
            "parentId": page_id,
            "index": "a1",
            "props": {"x": 0, "y": 0, "w": canvas_w, "h": canvas_h},
            "id": "shape:writing-lines",
            "typeName": "shape",
        },
        "shape:writing-container": {
            "x": 0,
            "y": 0,
            "rotation": 0,
            "isLocked": True,
            "opacity": 1,
            "meta": {"savedH": canvas_h / 2.2},
            "type": "writing-container",
            "parentId": page_id,
            "index": "a1",
            "props": {"x": 0, "y": 0, "w": canvas_w, "h": canvas_h},
            "id": "shape:writing-container",
            "typeName": "shape",
        },
        asset_id: {
            "id": asset_id,
            "typeName": "asset",
            "type": "image",
            "props": {
                "name": source_name,
                "src": data_uri,
                "w": w,
                "h": h,
                "mimeType": mime,
                "isAnimated": False,
            },
            "meta": {},
        },
        shape_id: {
            "x": img_x,
            "y": img_y,
            "rotation": 0,
            "isLocked": False,
            "opacity": 1,
            "meta": {},
            "id": shape_id,
            "type": "image",
            "props": {
                "w": w,
                "h": h,
                "playing": True,
                "url": "",
                "assetId": asset_id,
                "crop": None,
                "flipX": False,
                "flipY": False,
            },
            "parentId": page_id,
            "index": "a2",
            "typeName": "shape",
        },
    }

    # Preview SVG: shown by Ink as the inline thumbnail in the markdown embed.
    # We render the original image at its native pixel size; viewBox carries
    # the canvas dims so it scales sensibly when the embed is sized.
    preview_uri = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{canvas_w}" height="{canvas_h}" '
        f'viewBox="0 0 {canvas_w} {canvas_h}" '
        'style="background-color: transparent;">'
        f'<image href="{data_uri}" x="{img_x}" y="{img_y}" '
        f'width="{w}" height="{h}"/>'
        "</svg>"
    )

    return {
        "meta": {
            "pluginVersion": PLUGIN_VERSION,
            "tldrawVersion": TLDRAW_VERSION,
        },
        "tldraw": {
            "document": {"store": store, "schema": SCHEMA},
            "session": {
                "version": 0,
                "currentPageId": page_id,
                "exportBackground": True,
                "isFocusMode": False,
                "isDebugMode": False,
                "isToolLocked": False,
                "isGridMode": False,
                "pageStates": [
                    {
                        "pageId": page_id,
                        "camera": {"x": 0, "y": 0, "z": 0.336},
                        "selectedShapeIds": [],
                        "focusedGroupId": None,
                    }
                ],
            },
        },
        "previewUri": preview_uri,
    }


def write_writing_file(
    image_bytes: bytes,
    mime: str,
    target_path: Path,
    source_name: str,
) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    doc = build_writing(image_bytes, mime, source_name)
    target_path.write_text(
        json.dumps(doc, indent="\t", ensure_ascii=False),
        encoding="utf-8",
    )


_TLDRAW_PALETTE: dict[str, tuple[int, int, int]] = {
    # tldraw v2.4 draw-shape palette, rough sRGB approximations from the UI.
    "black": (29, 29, 29),
    "grey": (155, 153, 168),
    "light-violet": (224, 132, 244),
    "violet": (174, 62, 201),
    "blue": (74, 116, 235),
    "light-blue": (75, 161, 241),
    "yellow": (224, 172, 0),
    "orange": (231, 138, 64),
    "green": (37, 158, 60),
    "light-green": (61, 184, 89),
    "light-red": (251, 100, 100),
    "red": (224, 49, 49),
}


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    s = hex_color.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _nearest_named_color(hex_color: str) -> str:
    r, g, b = _hex_to_rgb(hex_color)
    best, best_dist = "black", float("inf")
    for name, (pr, pg, pb) in _TLDRAW_PALETTE.items():
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_dist:
            best, best_dist = name, d
    return best


def _stroke_size_bucket(avg_pt_size: float) -> str:
    # Match the user's hand-drawn reference "handwriting example.writing", which
    # uses size "m" with isPen=false (and all z=0.5). That's the rendering they
    # already validated as looking right.
    return "m"


_FRAC_INDEX_CACHE: list[str] = []


def _frac_index(n: int) -> str:
    """Generate the n-th valid tldraw fractional index (0-based).

    tldraw validates indices via the ``fractional-indexing`` algorithm: a key
    is a head char (`a`–`z`, `A`–`Z`) followed by a base-62 mantissa with no
    trailing zeros. Naive zero-padded forms like `a0010` are rejected
    (`ValidationError: At shape.index: Expected an index key`). We delegate to
    the reference implementation to be safe.
    """
    from fractional_indexing import generate_n_keys_between

    while len(_FRAC_INDEX_CACHE) <= n:
        # Generate the next batch starting after whatever we already cached.
        last = _FRAC_INDEX_CACHE[-1] if _FRAC_INDEX_CACHE else None
        batch = generate_n_keys_between(last, None, 256)
        _FRAC_INDEX_CACHE.extend(batch)
    return _FRAC_INDEX_CACHE[n]


def build_writing_from_strokes(
    strokes_data: list[dict],
    drawing_bounds: dict,
    source_name: str,
) -> dict:
    """Build a tldraw doc where each PencilKit stroke becomes a `draw` shape.

    `strokes_data` is the JSON emitted by decode_pkdrawing (list of strokes
    each with `ink`, `transform`, `renderBounds`, `path`).
    `drawing_bounds` is the overall drawing bounds.
    """
    # Drawing bounding box in PKDrawing's coord space.
    dx = float(drawing_bounds["x"])
    dy = float(drawing_bounds["y"])
    dw = float(drawing_bounds["w"])
    dh = float(drawing_bounds["h"])

    margin = 100
    # The Ink plugin pins canvas width to WRITING_PAGE_WIDTH (2000). Apple Notes
    # drawings are typically ~768pt wide so they'd float in a small column on the
    # left; scale the drawing up so it fills the canvas width (preserving aspect
    # ratio) — matches what the user sees natively in Apple Notes.
    canvas_w = 2000.0
    target_drawing_w = canvas_w - margin * 2
    scale = target_drawing_w / dw if dw > 0 else 1.0
    scaled_h = dh * scale
    canvas_h = max(225.0, scaled_h + margin * 2)

    # Translate from PKDrawing-space to canvas-space: scale by `scale` then
    # offset so the drawing's top-left lands at (margin, margin).
    offset_x = margin - dx * scale
    offset_y = margin - dy * scale

    # The Ink plugin's writing-canvas template uses a hardcoded page id; if our
    # page id is anything else, the plugin doesn't recognise the doc as a
    # writing canvas (no editor on click, no plugin chrome).
    page_id = "page:3qj9EtNgqSCW_6knX2K9_"

    store: dict[str, dict] = {
        "document:document": {
            "gridSize": 10,
            "name": "",
            "meta": {},
            "id": "document:document",
            "typeName": "document",
        },
        page_id: {
            "meta": {},
            "id": page_id,
            "name": "Handwritten Note",
            "index": "a1",
            "typeName": "page",
        },
        "shape:writing-lines": {
            "x": 0,
            "y": 0,
            "rotation": 0,
            # Match the Ink plugin's fresh template: writing-lines is locked too.
            "isLocked": True,
            "opacity": 1,
            "meta": {},
            "type": "writing-lines",
            "parentId": page_id,
            "index": "a1",
            "props": {"x": 0, "y": 0, "w": canvas_w, "h": canvas_h},
            "id": "shape:writing-lines",
            "typeName": "shape",
        },
        "shape:writing-container": {
            "x": 0,
            "y": 0,
            "rotation": 0,
            "isLocked": True,
            "opacity": 1,
            "meta": {},
            "type": "writing-container",
            "parentId": page_id,
            "index": "a1",
            "props": {"x": 0, "y": 0, "w": canvas_w, "h": canvas_h},
            "id": "shape:writing-container",
            "typeName": "shape",
        },
    }

    preview_paths: list[str] = []

    for i, stroke in enumerate(strokes_data, start=1):
        path = stroke["path"]
        if not path:
            continue
        ink = stroke.get("ink", {})
        color_name = _nearest_named_color(ink.get("color", "#000000FF"))
        sizes = [float(p["size"][0]) for p in path]
        avg_size = sum(sizes) / len(sizes) if sizes else 2.0
        size_bucket = _stroke_size_bucket(avg_size)

        # Each stroke gets its own shape positioned at its bbox origin so the
        # points are local; this matches how tldraw stores hand-drawn shapes.
        rb = stroke.get("renderBounds") or {
            "x": min(p["x"] for p in path),
            "y": min(p["y"] for p in path),
            "w": 0,
            "h": 0,
        }
        # Apply the canvas-fit scale to both the shape origin and each point's
        # local coordinate so the drawing fills the 2000-wide canvas.
        sx = float(rb["x"]) * scale + offset_x
        sy = float(rb["y"]) * scale + offset_y

        local_origin_x = float(rb["x"])
        local_origin_y = float(rb["y"])

        points = []
        path_d_parts: list[str] = []
        for j, p in enumerate(path):
            px = (float(p["x"]) - local_origin_x) * scale
            py = (float(p["y"]) - local_origin_y) * scale
            # The Ink plugin auto-detects "isPen" from the first two points'
            # z-values (anything ≠ 0 and ≠ 0.5 turns on perfect-freehand variable-
            # width filled-outline rendering, which makes every stroke a chunky
            # blob). Force z=0.5 so the plugin picks the simpler renderer.
            z = 0.5
            points.append({"x": round(px, 2), "y": round(py, 2), "z": round(z, 3)})
            preview_x = px + sx
            preview_y = py + sy
            path_d_parts.append(
                f"{'M' if j == 0 else 'L'}{preview_x:.1f},{preview_y:.1f}"
            )

        shape_id = _short_id("shape", f"{source_name}:stroke:{i}")
        store[shape_id] = {
            "x": sx,
            "y": sy,
            "rotation": 0,
            "isLocked": False,
            "opacity": 1,
            "meta": {},
            "id": shape_id,
            "type": "draw",
            "props": {
                "segments": [{"type": "free", "points": points}],
                "color": color_name,
                "fill": "none",
                # dash="draw" goes through Ink's filled-polygon branch and
                # renders every stroke as a solid blob ignoring fill:"none".
                # dash="solid" uses the stroke + fill:none branch — clean lines.
                "dash": "solid",
                "size": size_bucket,
                "isComplete": True,
                "isClosed": False,
                # Tldraw's `isPen: true` uses perfect-freehand to render a
                # variable-width filled outline. On PencilKit data without
                # real pressure (finger/trackpad input → uniform force) that
                # produces chunky blobs. Plain stroke rendering matches the
                # original much better.
                "isPen": False,
                "scale": 1,
            },
            "parentId": page_id,
            "index": _frac_index(i + 1),
            "typeName": "shape",
        }
        # The Ink plugin ships CSS that force-fills every path inside
        # `.ddc_ink_writing-embed-preview` with the theme colour, on the
        # assumption that previewUri paths are closed outline polygons (which
        # is what tldraw's own getWritingSvg produces). Open polylines like
        # ours then render as filled blobs. Inline `style="fill:none"` beats
        # the plugin's class-based rule by CSS specificity. Add stroke via
        # inline style too so dark mode (which sets `fill: rgb(242,242,242)`)
        # doesn't paint our lines white.
        preview_paths.append(
            f'<path d="{" ".join(path_d_parts)}" '
            f'style="fill:none;stroke:currentColor;stroke-width:{max(1.0, avg_size):.1f}"/>'
        )

    preview_uri = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{canvas_w:.0f}" height="{canvas_h:.0f}" '
        f'viewBox="0 0 {canvas_w:.0f} {canvas_h:.0f}" '
        'stroke-linecap="round" stroke-linejoin="round" '
        'style="background-color: transparent;">'
        + "".join(preview_paths)
        + "</svg>"
    )

    return {
        "meta": {
            "pluginVersion": PLUGIN_VERSION,
            "tldrawVersion": TLDRAW_VERSION,
        },
        "tldraw": {
            "document": {"store": store, "schema": SCHEMA},
            "session": {
                "version": 0,
                # The plugin's fresh embeds use page:writingPage1 here even though
                # the actual page record's id is the hardcoded one above. Match
                # that exactly so the plugin's "is this a writing canvas?" check
                # passes.
                "currentPageId": "page:writingPage1",
                "exportBackground": True,
                "isFocusMode": False,
                "isDebugMode": True,
                "isToolLocked": False,
                "isGridMode": False,
                "pageStates": [
                    {
                        "pageId": "page:writingPage1",
                        "camera": {"x": 0, "y": 100, "z": 0.376},
                        "selectedShapeIds": [],
                        "focusedGroupId": None,
                    }
                ],
            },
        },
        "previewUri": preview_uri,
    }


def write_strokes_writing_file(
    strokes_data: list[dict],
    drawing_bounds: dict,
    target_path: Path,
    source_name: str,
) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    doc = build_writing_from_strokes(strokes_data, drawing_bounds, source_name)
    target_path.write_text(
        json.dumps(doc, indent="\t", ensure_ascii=False),
        encoding="utf-8",
    )


def ink_embed_block(filepath_relative_to_vault: str) -> str:
    """The fenced code block Obsidian's Ink plugin recognises in a note."""
    payload = {
        "versionAtEmbed": PLUGIN_VERSION,
        "filepath": filepath_relative_to_vault,
    }
    inner = json.dumps(payload, indent="\t")
    return f"```handwritten-ink\n{inner}\n```\n"
