"""Load bitmaps and trace them to SVG with vtracer.

Kept free of Qt imports: the trace worker process imports this module, and
every trace pays that import cost.
"""
from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import vtracer
from PIL import Image, ImageOps

SUPPORTED_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff")


@dataclass(frozen=True)
class TraceSettings:
    colormode: str = "color"  # "color" or "binary"
    hierarchical: str = "stacked"  # "stacked" or "cutout"
    mode: str = "spline"  # "spline", "polygon" or "none" (pixel staircase)
    filter_speckle: int = 4
    color_precision: int = 6
    layer_difference: int = 16
    corner_threshold: int = 60
    length_threshold: float = 4.0
    splice_threshold: int = 45
    path_precision: int = 2
    # Longest side traced, in pixels. Tracing cost grows with area, so large
    # photos are downscaled; the SVG is still sized to the original image.
    max_size: int = 1024

    def vtracer_kwargs(self) -> dict:
        kwargs = asdict(self)
        del kwargs["max_size"]
        return kwargs


def is_supported(path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def load_image(source: str | Path | bytes) -> Image.Image:
    """Decode a JPEG/PNG/TIFF, given as a path or file contents, into upright 8-bit RGBA."""
    with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode.startswith("I"):
            # Pillow clips 16-bit samples at 255 when converting to 8-bit,
            # which turns most 16-bit TIFFs solid white; rescale first.
            im = im.convert("I").point(lambda v: v * (1 / 257)).convert("L")
        return im.convert("RGBA")


def trace(image: Image.Image, settings: TraceSettings) -> str:
    """Trace an RGBA image and return SVG text sized to the image."""
    w, h = image.size
    scale = min(1.0, settings.max_size / max(w, h))
    work = image
    if scale < 1.0:
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        work = image.resize(size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    work.save(buf, format="PNG", compress_level=1)
    svg = vtracer.convert_raw_image_to_svg(
        buf.getvalue(), img_format="png", **settings.vtracer_kwargs()
    )
    return _set_output_size(svg, work.size, (w, h))


_SVG_SIZE = re.compile(r'<svg([^>]*?) width="\d+" height="\d+"')


def _set_output_size(svg: str, traced: tuple[int, int], original: tuple[int, int]) -> str:
    # A viewBox lets renderers scale the paths, so a downscaled trace still
    # opens at the original image's dimensions.
    (tw, th), (ow, oh) = traced, original
    replacement = rf'<svg\1 width="{ow}" height="{oh}" viewBox="0 0 {tw} {th}"'
    out, count = _SVG_SIZE.subn(replacement, svg, count=1)
    if count != 1:
        raise ValueError(f"unexpected SVG header from vtracer: {svg[:200]!r}")
    return out


def trace_to_pipe(conn, data: bytes, settings: TraceSettings) -> None:
    """Child-process entry point: send ("ok", svg) or ("error", message)."""
    try:
        conn.send(("ok", trace(load_image(data), settings)))
    except Exception as exc:
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()
