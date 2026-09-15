import io

import pytest
import segno
import zxingcpp
from PIL import Image

from conftest import draw_shapes, symbol_image
from image_to_vector import codes as codes_module
from image_to_vector.codes import detect_codes, overlay_codes
from image_to_vector.tracer import TraceSettings, trace


def render(svg: str, size) -> Image.Image:
    """Rasterise with Qt, as the preview does."""
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    w, h = size
    out = QImage(w, h, QImage.Format.Format_RGBA8888)
    out.fill(Qt.GlobalColor.white)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, w, h))
    painter.end()
    return Image.frombuffer("RGBA", (w, h), bytes(out.constBits()), "raw", "RGBA", out.bytesPerLine(), 1)


def near(hex_color: str, rgb, tol=30) -> bool:
    got = tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return all(abs(a - b) <= tol for a, b in zip(got, rgb))


def scene() -> Image.Image:
    """Busy background with a rotated QR code, a tilted EAN-13 with digits and an upright Code 128."""
    canvas = draw_shapes((1200, 900))
    qr = symbol_image("https://example.org/товар?id=42", "QRCode", 8)
    canvas.paste(qr.rotate(20, expand=True, fillcolor="white", resample=Image.Resampling.BICUBIC), (60, 60))
    ean = symbol_image("5012345678900", "EAN13", 4, hrt=True)
    canvas.paste(ean.rotate(8, expand=True, fillcolor="white", resample=Image.Resampling.BICUBIC), (620, 80))
    canvas.paste(symbol_image("ABC-12345", "Code128", 3), (330, 640))
    return canvas


EXPECTED = {("QRCode", "https://example.org/товар?id=42"), ("EAN13", "5012345678900"), ("Code128", "ABC-12345")}


def test_codes_in_a_busy_image_are_found_with_their_values():
    found, skipped = detect_codes(scene())
    assert skipped == [], skipped
    assert {(c.format, c.text) for c in found} == EXPECTED


def test_overlaid_codes_decode_to_the_same_values_in_the_same_places(qapp):
    # End to end: detect, overlay onto a downscaled trace (so the overlay's
    # scaling is exercised), rasterise as the preview does, detect again.
    image = scene().convert("RGBA")
    found, _ = detect_codes(image)
    svg = overlay_codes(trace(image, TraceSettings(max_size=600)), found)
    again, skipped = detect_codes(render(svg, image.size))
    assert skipped == [], skipped
    assert {(c.format, c.text) for c in again} == EXPECTED
    before = {(c.format, c.text): c for c in found}
    for code in again:
        original = before[(code.format, code.text)]
        a = [complex(*p) for p in code.corners]
        b = [complex(*p) for p in original.corners]
        offsets = [round(abs(p - q), 1) for p, q in zip(a, b)]
        if code.linear:
            # Bar positions carry the value: the ends (top corners) must hold.
            # Bar height is cosmetic and measured from the image to about 2 %
            # (a bottom corner moved 3.2 px on 200 px bars in this scene).
            assert max(offsets[:2]) <= 3, f"{code.format} ends moved by {offsets[:2]} px"
            heights = abs(a[3] - a[0]), abs(b[3] - b[0])
            assert abs(heights[0] - heights[1]) <= 0.02 * heights[1], f"{code.format} bar heights {heights}"
        else:
            assert max(offsets) <= 3, f"{code.format} corners moved by {offsets} px"


def test_regenerated_qr_has_the_same_modules_as_the_original():
    # Same version, error correction and mask: the redrawn code looks like the
    # original, not merely an equivalent code. Non-default options on purpose.
    options = {"ec_level": "Q", "version": 6}
    original = zxingcpp.create_barcode("HELLO WORLD 123", zxingcpp.BarcodeFormat.QRCode, **options)
    view = memoryview(original.to_image(scale=1, add_quiet_zones=False))
    expected = tuple(tuple(view[y, x] < 128 for x in range(view.shape[1])) for y in range(view.shape[0]))
    found, _ = detect_codes(symbol_image("HELLO WORLD 123", "QRCode", 6, **options))
    assert len(found) == 1
    assert found[0].modules == expected, "module grid differs from the original's"


@pytest.mark.parametrize("scale, angle, canvas_height", [(2, 0, 700), (2, 12, 700), (4, 0, 4800), (2, 8, 4800)])
def test_linear_barcode_frame_spans_the_bars_but_not_the_digits(scale, angle, canvas_height):
    # zxing-cpp's corners for a linear code are the scan lines it decoded. In
    # tall images it skips rows (measured: 190 of 200 px of bar on a 4800 px
    # canvas), and at 2 px per module its ends were up to 1.4 px off. Both
    # are measured from the image instead, stopping where the digits begin.
    symbol = symbol_image("5012345678900", "EAN13", scale, hrt=True)
    px = symbol.load()
    # Height of an ordinary data bar (guard bars run on into the digits): the
    # unbroken dark run through it. Counting every dark pixel in the column
    # once included the digit printed below and gave 214 for 200 px bars.
    y_mid = symbol.height // 3
    darks = [x for x in range(symbol.width) if px[x, y_mid][0] < 128]
    column = next(x for x in darks if x > darks[0] + 20 * scale)
    top = bottom = y_mid
    while top > 0 and px[column, top - 1][0] < 128:
        top -= 1
    while bottom + 1 < symbol.height and px[column, bottom + 1][0] < 128:
        bottom += 1
    bar_height = bottom - top + 1

    image = Image.new("RGB", (900, canvas_height), "white")
    rotated = symbol.rotate(angle, expand=True, fillcolor="white", resample=Image.Resampling.BICUBIC)
    image.paste(rotated, (100, canvas_height // 2 - 150))
    found, _ = detect_codes(image)
    assert len(found) == 1
    tl, tr, _, bl = (complex(*p) for p in found[0].corners)
    assert abs(abs(tr - tl) - 95 * scale) <= 0.6, f"width {abs(tr - tl):.1f}, expected {95 * scale}"
    assert abs(abs(bl - tl) - bar_height) <= 0.02 * bar_height + 1, f"height {abs(bl - tl):.1f}, bars are {bar_height}"


def test_code_colors_follow_the_original():
    blue, yellow = (20, 40, 160), (250, 220, 60)
    found, _ = detect_codes(symbol_image("colors", "QRCode", 6, dark=blue, light=yellow))
    assert near(found[0].on_color, blue) and near(found[0].off_color, yellow), found[0]
    # Light modules on dark: the overlay must keep them light.
    found, _ = detect_codes(symbol_image("inverted", "QRCode", 6, dark=(250, 250, 250), light=(15, 15, 15)))
    assert near(found[0].on_color, (250, 250, 250)) and near(found[0].off_color, (15, 15, 15)), found[0]


def test_qr_from_an_independent_encoder_is_regenerated_to_the_same_value(qapp):
    # segno, not zxing-cpp, made this one, so its modules may differ from the
    # regenerated code's; the value must not.
    value = "Grüße 👋 <b>&"
    buffer = io.BytesIO()
    segno.make(value, error="h").save(buffer, kind="png", scale=8, border=4)
    image = Image.open(buffer).convert("RGBA")
    found, skipped = detect_codes(image)
    assert skipped == [] and [c.text for c in found] == [value], (found, skipped)
    svg = overlay_codes(trace(image, TraceSettings()), found)
    again, _ = detect_codes(render(svg, image.size))
    assert [c.text for c in again] == [value]


def test_binary_qr_content_round_trips_its_bytes():
    payload = b"\x00\xff\x10bin"
    found, skipped = detect_codes(symbol_image(payload, "QRCode", 6))
    assert skipped == [] and len(found) == 1, skipped


def test_code_that_cannot_be_regenerated_is_reported_not_faked(monkeypatch):
    # If the new symbol doesn't decode to the original value, the trace stays
    # rather than a wrong code being drawn over it.
    real_create = zxingcpp.create_barcode
    monkeypatch.setattr(zxingcpp, "create_barcode", lambda content, fmt, **kw: real_create(content + "x", fmt))
    found, skipped = detect_codes(symbol_image("value", "QRCode", 6))
    assert found == []
    assert [s.reason for s in skipped] == ["the regenerated code didn't decode to the same value"]


def test_formats_that_cannot_be_generated_are_reported(monkeypatch):
    monkeypatch.setattr(codes_module, "_CREATABLE", set())
    found, skipped = detect_codes(symbol_image("value", "QRCode", 6))
    assert found == [] and [s.reason for s in skipped] == ["this format can't be generated"]


def test_image_without_codes_finds_nothing():
    assert detect_codes(draw_shapes((400, 300)).convert("RGBA")) == ([], [])
