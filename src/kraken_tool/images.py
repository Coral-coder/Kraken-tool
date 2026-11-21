"""Image preparation: test patterns, static RGBA frames, and GIF sanitising."""

from __future__ import annotations

import io
from typing import Callable

from PIL import Image, ImageDraw, ImageSequence

Size = tuple


def _rotate(img: Image.Image, orientation_deg: int) -> Image.Image:
    # the controller does not rotate uploaded assets itself
    return img.rotate(-orientation_deg, expand=False) if orientation_deg else img


def _fit(img: Image.Image, size: Size) -> Image.Image:
    """Centre-crop to square then resize (avoids squashing non-square input)."""
    w, h = img.size
    s = min(w, h)
    left, top = (w - s) // 2, (h - s) // 2
    return img.crop((left, top, left + s, top + s)).resize(size, Image.LANCZOS)


# ------------------------------------------------------------------ static

def encode_rgba(img: Image.Image, size: Size, orientation_deg: int = 0) -> bytes:
    """Static-mode payload: raw R,G,B,0 per pixel, row-major."""
    img = _rotate(_fit(img.convert("RGB"), size), orientation_deg)
    rgb = img.tobytes()
    out = bytearray(len(rgb) // 3 * 4)
    out[0::4] = rgb[0::3]
    out[1::4] = rgb[1::3]
    out[2::4] = rgb[2::3]
    return bytes(out)


# --------------------------------------------------------------------- GIF

def sanitize_gif(src, size: Size, orientation_deg: int = 0, max_frames: int = 600) -> bytes:
    """Re-encode a GIF so the LCD's decoder has nothing hard to get wrong.

    Every frame is fully composited, resized, and written as a *full-canvas*
    frame (no cropped delta rectangles, no transparency, disposal=2), each
    with its own palette.  Delta frames with transparency/odd disposal are
    the classic cause of "ghost of the previous frame" artifacts on
    embedded GIF decoders.
    """
    img = Image.open(src)
    frames = []
    durations = []
    for i, frame in enumerate(ImageSequence.Iterator(img)):
        if i >= max_frames:
            break
        rgba = frame.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (0, 0, 0))
        flat.paste(rgba, mask=rgba.split()[3])
        flat = _rotate(_fit(flat, size), orientation_deg)
        frames.append(flat.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
        durations.append(max(20, int(frame.info.get("duration", img.info.get("duration", 100)) or 100)))
    if not frames:
        raise ValueError("GIF has no frames")

    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations if len(frames) > 1 else durations[0],
        loop=0,
        disposal=2,
        optimize=False,
        interlace=False,
    )
    return buf.getvalue()


def gif_frame_report(data_or_path) -> list[dict]:
    """Describe each frame of a GIF (used by ``inspect-gif``)."""
    src = io.BytesIO(data_or_path) if isinstance(data_or_path, (bytes, bytearray)) else data_or_path
    img = Image.open(src)
    out = []
    for i, frame in enumerate(ImageSequence.Iterator(img)):
        tile = frame.tile[0] if frame.tile else None
        box = tile[1] if tile else (0, 0) + frame.size
        out.append(
            {
                "frame": i,
                "box": tuple(box),
                "full_canvas": tuple(box) == (0, 0) + img.size,
                "disposal": getattr(frame, "disposal_method", 0),
                "transparency": "transparency" in frame.info,
                "duration": frame.info.get("duration"),
            }
        )
    return out


# ----------------------------------------------------------- test patterns

def _solid(color):
    return lambda size: Image.new("RGB", size, color)


def _columns(size):
    """1px alternating columns - exposes column-driver / source-driver faults."""
    w, h = size
    img = Image.new("RGB", size, "black")
    d = ImageDraw.Draw(img)
    for x in range(0, w, 2):
        d.line([(x, 0), (x, h)], fill="white")
    return img


def _rows(size):
    w, h = size
    img = Image.new("RGB", size, "black")
    d = ImageDraw.Draw(img)
    for y in range(0, h, 2):
        d.line([(0, y), (w, y)], fill="white")
    return img


def _checker(size, cell=None):
    w, h = size
    cell = cell or max(4, w // 16)
    img = Image.new("RGB", size, "black")
    d = ImageDraw.Draw(img)
    for y in range(0, h, cell):
        for x in range(0, w, cell):
            if (x // cell + y // cell) % 2 == 0:
                d.rectangle([x, y, x + cell - 1, y + cell - 1], fill="white")
    return img


def _gradient(size):
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        band = y * 4 // h  # R, G, B, grey bands
        for x in range(w):
            v = x * 255 // (w - 1)
            px[x, y] = [(v, 0, 0), (0, v, 0), (0, 0, v), (v, v, v)][band]
    return img


def _grid(size):
    w, h = size
    img = Image.new("RGB", size, "black")
    d = ImageDraw.Draw(img)
    step = max(8, w // 8)
    for x in range(0, w, step):
        d.line([(x, 0), (x, h)], fill=(0, 255, 0))
    for y in range(0, h, step):
        d.line([(0, y), (w, y)], fill=(0, 255, 0))
    d.line([(0, 0), (w, h)], fill="red", width=3)
    d.line([(w, 0), (0, h)], fill="red", width=3)
    d.ellipse([2, 2, w - 3, h - 3], outline="white", width=3)
    # orientation marker: filled square top-left
    d.rectangle([w // 8, h // 8, w // 8 + step // 2, h // 8 + step // 2], fill="yellow")
    return img


PATTERNS: dict[str, tuple[Callable[[Size], Image.Image], str]] = {
    "white": (_solid((255, 255, 255)), "Solid white: dark columns/rows = dead driver lines"),
    "black": (_solid((0, 0, 0)), "Solid black: bright lines = stuck pixels/lines"),
    "red": (_solid((255, 0, 0)), "Solid red: tints/stripes = failing red subpixel lines"),
    "green": (_solid((0, 255, 0)), "Solid green"),
    "blue": (_solid((0, 0, 255)), "Solid blue"),
    "columns": (_columns, "1px vertical lines: shimmer/moire beyond normal = column driver fault"),
    "rows": (_rows, "1px horizontal lines"),
    "checker": (_checker, "Checkerboard: smearing between cells = timing/overdrive issue"),
    "gradient": (_gradient, "R/G/B/grey ramps: banding steps = bit-depth / data line fault"),
    "grid": (_grid, "Geometry grid + yellow square marks top-left"),
}


def make_pattern(name: str, size: Size) -> Image.Image:
    try:
        fn, _ = PATTERNS[name]
    except KeyError:
        raise ValueError(f"unknown pattern {name!r}; choose from {', '.join(PATTERNS)}") from None
    return fn(size)
