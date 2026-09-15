"""Find barcodes and QR codes in a bitmap and redraw them as exact vector symbols.

A traced code is only approximately right and often won't scan. Each code
zxing-cpp finds is re-encoded from its decoded value, the new symbol is checked
to decode to the same bytes, and it is drawn over the trace with the position,
angle and colors of the original. A code that can't be regenerated faithfully
is reported and left as traced, never replaced by something else.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from xml.sax.saxutils import quoteattr

import zxingcpp
from PIL import Image

Point = tuple[float, float]

_CREATABLE = set(zxingcpp.barcode_formats_list(zxingcpp.BarcodeFormat.AllCreatable))
_LINEAR = set(zxingcpp.barcode_formats_list(zxingcpp.BarcodeFormat.AllLinear))
_DISPLAY_NAMES = {
    "QRCode": "QR Code", "MicroQRCode": "Micro QR Code", "RMQRCode": "rMQR Code", "DataMatrix": "Data Matrix",
    "EAN13": "EAN-13", "EAN8": "EAN-8", "UPCA": "UPC-A", "UPCE": "UPC-E", "Code128": "Code 128",
    "Code39": "Code 39", "Code93": "Code 93", "DataBar": "DataBar",
}


@dataclass(frozen=True)
class DetectedCode:
    format: str  # zxing-cpp format name, e.g. "QRCode", "EAN13"
    text: str
    corners: tuple[Point, Point, Point, Point]  # symbol's TL, TR, BR, BL in image pixels, quiet zone excluded
    modules: tuple[tuple[bool, ...], ...]  # dark-module grid; a single row of bars for linear codes
    on_color: str  # color of the modules ("#RRGGBB"), light for inverted codes
    off_color: str
    linear: bool


@dataclass(frozen=True)
class SkippedCode:
    format: str
    text: str
    reason: str


def display_name(format_name: str) -> str:
    return _DISPLAY_NAMES.get(format_name, format_name)


def detect_codes(image: Image.Image) -> tuple[list[DetectedCode], list[SkippedCode]]:
    """Find, decode and re-encode every barcode and QR code in image."""
    rgb = Image.alpha_composite(Image.new("RGBA", image.size, "white"), image.convert("RGBA")).convert("RGB")
    gray = rgb.convert("L")
    found, skipped = [], []
    for barcode in zxingcpp.read_barcodes(gray):
        name, text = barcode.format.name, barcode.text
        if barcode.format not in _CREATABLE:
            skipped.append(SkippedCode(name, text, "this format can't be generated"))
            continue
        modules = _regenerate(barcode)
        if modules is None:
            skipped.append(SkippedCode(name, text, "the regenerated code didn't decode to the same value"))
            continue
        linear = barcode.format in _LINEAR
        corners = _corners(barcode)
        if linear:
            corners = _linear_frame(corners, _sampler(gray), len(modules[0]))
        on, off = _colors(rgb, corners, len(modules[0]), len(modules), linear)
        found.append(
            DetectedCode(name, text, tuple((z.real, z.imag) for z in corners), modules, on, off, linear)
        )
    return found, skipped


def _regenerate(barcode) -> tuple[tuple[bool, ...], ...] | None:
    content = barcode.text if barcode.content_type == zxingcpp.ContentType.Text else barcode.bytes
    extra = barcode.extra or {}
    # Same version, error correction and mask as the original, so the new
    # symbol has the same modules where the encoder allows it. zxing-cpp
    # ignores options it doesn't know, hence the decode check either way.
    options = {key: extra[name] for name, key in (("Version", "version"), ("ECLevel", "ec_level"), ("DataMask", "data_mask")) if name in extra}
    linear = barcode.format in _LINEAR
    for attempt in (options, {}):
        try:
            symbol = zxingcpp.create_barcode(content, barcode.format, **attempt)
        except (ValueError, RuntimeError):
            continue
        if _decodes_to(symbol, barcode):
            return _module_grid(symbol, linear)
    return None


def _to_pil(image) -> Image.Image:
    view = memoryview(image)
    return Image.frombytes("L", (view.shape[1], view.shape[0]), bytes(view))


def _decodes_to(symbol, original) -> bool:
    rendered = _to_pil(symbol.to_image(scale=4, add_quiet_zones=True))
    return any(b.format == original.format and b.bytes == original.bytes for b in zxingcpp.read_barcodes(rendered))


def _module_grid(symbol, linear: bool) -> tuple[tuple[bool, ...], ...]:
    image = _to_pil(symbol.to_image(scale=1, add_quiet_zones=False))
    px = image.load()
    w, h = image.size
    # A linear symbol's bitmap repeats its bar pattern on every row.
    return tuple(tuple(px[x, y] < 128 for x in range(w)) for y in range(1 if linear else h))


def _corners(barcode) -> list[complex]:
    p = barcode.position
    return [complex(q.x, q.y) for q in (p.top_left, p.top_right, p.bottom_right, p.bottom_left)]


def _dot(a: complex, b: complex) -> float:
    return a.real * b.real + a.imag * b.imag


def _sampler(gray: Image.Image):
    px = gray.load()
    w, h = gray.size

    def sample(z: complex) -> int | None:
        x, y = math.floor(z.real), math.floor(z.imag)
        return px[x, y] if 0 <= x < w and 0 <= y < h else None

    return sample


def _linear_frame(corners: list[complex], sample, n_modules: int) -> list[complex]:
    """The rectangle a linear code's bars occupy, measured from the image."""
    # zxing-cpp's corners for a linear code bound only the scan lines that
    # decoded (47 of 200 px of bar height at 15°), but its side edges follow
    # the bars' direction. So the direction comes from the corners and the
    # ends and height from the image.
    tl, tr, br, bl = corners
    across = tr - tl
    down = (bl - tl) + (br - tr)
    if abs(down) < 1:  # a single scan line: the bars are perpendicular to it
        down = across * 1j
    d = down / abs(down)
    u = d * -1j
    if _dot(u, across) < 0:
        u = -u
    c = sum(corners) / 4
    start = (_dot(tl - c, u) + _dot(bl - c, u)) / 2
    end = (_dot(tr - c, u) + _dot(br - c, u)) / 2
    top = min(_dot(p - c, d) for p in corners)
    bottom = max(_dot(p - c, d) for p in corners)
    module = (end - start) / n_modules
    # zxing-cpp's ends can be a pixel off, which is most of a module on small codes.
    start, end = _bar_ends(sample, c + d * (top + bottom) / 2, u, start - 2 * module, end + 2 * module)
    top, bottom = _bar_rows(sample, c, u, d, start, end, top, bottom, n_modules)
    origin = c + u * start + d * top
    width, height = u * (end - start), d * (bottom - top)
    return [origin, origin + width, origin + width + height, origin + height]


def _bar_ends(sample, line_origin: complex, u: complex, s0: float, s1: float) -> tuple[float, float]:
    step = 0.25
    positions = [s0 + i * step for i in range(int((s1 - s0) / step) + 1)]
    values = [sample(line_origin + u * s) for s in positions]
    known = [v for v in values if v is not None]
    threshold = (min(known) + max(known)) / 2
    # The search starts in the quiet zone, so whatever differs from the first sample is a bar.
    quiet_is_dark = known[0] < threshold
    bars = [s for s, v in zip(positions, values) if v is not None and (v < threshold) != quiet_is_dark]
    return bars[0], bars[-1] + step


def _bar_rows(sample, c, u, d, start, end, top, bottom, n_modules) -> tuple[float, float]:
    n = 4 * n_modules

    def row(offset: float) -> list[int] | None:
        values = [sample(c + d * offset + u * (start + (i + 0.5) * (end - start) / n)) for i in range(n)]
        return None if None in values else values

    reference = row((top + bottom) / 2)
    threshold = (min(reference) + max(reference)) / 2
    pattern = [v < threshold for v in reference]

    def continues(offset: float) -> bool:
        # Measured on EAN-13s: rows through the bars matched the pattern 0.71-0.97
        # (small rotated modules are the noisy end), rows in the quiet zone or
        # the digits at most 0.66. 0.9 stopped inside the bars.
        values = row(offset)
        return values is not None and sum((v < threshold) == p for v, p in zip(values, pattern)) >= 0.7 * n

    # Grow from the middle row rather than from zxing-cpp's range: projected
    # onto the bars' direction that range overshoots when the code is rotated
    # (measured 204 px for 200 px bars at 12°), and growing can't shrink it.
    top = bottom = math.floor((top + bottom) / 2)
    limit = end - start  # no linear code is taller than it is long
    while bottom - top < limit and continues(top - 1):
        top -= 1
    while bottom - top < limit and continues(bottom + 1):
        bottom += 1
    return top, bottom + 1  # corners are pixel rows; the bars cover the whole last row


def _square_to_quad(q: list[complex]):
    """Projective map from the unit square onto quad (TL, TR, BR, BL); handles perspective."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = [(z.real, z.imag) for z in q]
    sx, sy = x0 - x1 + x2 - x3, y0 - y1 + y2 - y3
    dx1, dy1, dx2, dy2 = x1 - x2, y1 - y2, x3 - x2, y3 - y2
    den = dx1 * dy2 - dx2 * dy1
    g = (sx * dy2 - dx2 * sy) / den
    h = (dx1 * sy - sx * dy1) / den
    a, b, d, e = x1 - x0 + g * x1, x3 - x0 + h * x3, y1 - y0 + g * y1, y3 - y0 + h * y3

    def to_image(s: float, t: float) -> complex:
        w = g * s + h * t + 1
        return complex((a * s + b * t + x0) / w, (d * s + e * t + y0) / w)

    return to_image


def _colors(rgb: Image.Image, corners: list[complex], cols: int, rows: int, linear: bool) -> tuple[str, str]:
    """(module color, background color), sampled from the original."""
    px = rgb.load()
    w, h = rgb.size
    to_image = _square_to_quad(corners)

    def color(s: float, t: float) -> tuple[int, int, int]:
        z = to_image(s, t)
        return px[min(max(math.floor(z.real), 0), w - 1), min(max(math.floor(z.imag), 0), h - 1)]

    # The quiet zone just outside the symbol is its background by definition.
    # Data Matrix's is only one module wide; linear codes' are several.
    out = 1.5 if linear else 0.5
    ring = []
    for i in range(24):
        t = (i + 0.5) / 24
        ring += [color(-out / cols, t), color(1 + out / cols, t)]
        if not linear:
            ring += [color(t, -out / rows), color(t, 1 + out / rows)]
    inside = [color((x + 0.5) / cols, (y + 0.5) / rows) for y in range(rows) for x in range(cols)]
    background = _mean(ring)
    distance = [abs(_luminance(c) - _luminance(background)) for c in inside]
    farthest = max(distance)
    modules = [c for c, dist in zip(inside, distance) if dist > farthest / 2]
    return _hex(_mean(modules)), _hex(background)


def _luminance(c) -> float:
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _mean(colors) -> tuple[float, float, float]:
    return tuple(sum(c[i] for c in colors) / len(colors) for i in range(3))


def _hex(c) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(round(v) for v in c))


_SVG_SIZE = re.compile(r'<svg[^>]* width="(\d+)" height="(\d+)" viewBox="0 0 (\d+) (\d+)"')


def overlay_codes(svg: str, codes: list[DetectedCode], black_and_white: bool = False) -> str:
    """svg with the codes drawn on top of the trace."""
    ow, oh, tw, th = map(int, _SVG_SIZE.search(svg).groups())
    # Codes are in original-image pixels; a downscaled trace's viewBox is in traced pixels.
    parts = [f'<g id="detected-codes" transform="scale({tw / ow:.6g} {th / oh:.6g})">']
    parts += [_code_svg(code, black_and_white) for code in codes]
    parts.append("</g>\n")
    end = svg.rindex("</svg>")
    return svg[:end] + "\n".join(parts) + svg[end:]


def _margins(code: DetectedCode) -> tuple[int, int]:
    # One module of background around the symbol hides the trace's fuzzy
    # edges; linear codes get it only at the ends, above and below are digits.
    return 1, 0 if code.linear else 1


def erase_regions(codes: list[DetectedCode]) -> list[tuple[tuple[Point, ...], tuple[int, int, int]]]:
    """(polygon, color) for each code's area, to paint over the bitmap before tracing.

    Painted in the code's lighter color, so nothing is traced there: in black
    and white that color is left transparent, and traced shapes under the
    redrawn code would show through it (and be cut by plotters even if hidden).
    """
    regions = []
    for code in codes:
        rows, cols = len(code.modules), len(code.modules[0])
        mx, my = _margins(code)
        to_image = _square_to_quad([complex(*p) for p in code.corners])
        corners = [to_image(x / cols, y / rows) for x, y in ((-mx, -my), (cols + mx, -my), (cols + mx, rows + my), (-mx, rows + my))]
        lighter = max(_rgb(code.on_color), _rgb(code.off_color), key=_luminance)
        regions.append((tuple((z.real, z.imag) for z in corners), lighter))
    return regions


def _code_svg(code: DetectedCode, black_and_white: bool) -> str:
    rows, cols = len(code.modules), len(code.modules[0])
    mx, my = _margins(code)
    to_image = _square_to_quad([complex(*p) for p in code.corners])

    def quad(x0: float, y0: float, x1: float, y1: float) -> str:
        points = [to_image(x / cols, y / rows) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        return "M" + "L".join(f"{z.real:.2f} {z.imag:.2f}" for z in points) + "Z"

    def module(x: int, y: int) -> bool:
        return 0 <= x < cols and 0 <= y < rows and code.modules[y][x]

    def runs(painted, xs: range, ys: range) -> str:
        parts = []
        for y in ys:
            # Overlapping the next row slightly stops antialiasing leaving hairline seams between rows.
            bottom = y + 1 + (0.02 if y + 1 < ys.stop else 0)
            x = xs.start
            while x < xs.stop:
                if not painted(x, y):
                    x += 1
                    continue
                run = x
                while run < xs.stop and painted(run, y):
                    run += 1
                parts.append(quad(x, y, run, bottom))
                x = run
        return "".join(parts)

    if not black_and_white:
        body = (
            f'<path d="{quad(-mx, -my, cols + mx, rows + my)}" fill="{code.off_color}"/>\n'
            f'<path d="{runs(module, range(cols), range(rows))}" fill="{code.on_color}"/>'
        )
    elif _luminance(_rgb(code.on_color)) <= _luminance(_rgb(code.off_color)):
        # Like the black-and-white trace: the darker color in black, the lighter left transparent.
        body = f'<path d="{runs(module, range(cols), range(rows))}" fill="#000000"/>'
    else:
        # Inverted code, light modules on dark: the margin and the gaps between modules are the dark part.
        everything_else = runs(lambda x, y: not module(x, y), range(-mx, cols + mx), range(-my, rows + my))
        body = f'<path d="{everything_else}" fill="#000000"/>'
    return f"<g data-format={quoteattr(code.format)} data-value={quoteattr(code.text)}>\n{body}\n</g>"


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
