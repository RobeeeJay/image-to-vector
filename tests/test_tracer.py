import re
import struct

from PIL import Image

from conftest import BLUE, GREEN, RED, draw_shapes
from image_to_vector.tracer import TraceSettings, is_supported, load_image, trace


def fills(svg: str) -> set[tuple[int, int, int]]:
    return {tuple(int(h[i : i + 2], 16) for i in (0, 2, 4)) for h in re.findall(r'fill="#([0-9A-Fa-f]{6})"', svg)}


def near(a, b, tol=24) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_supported_formats_are_recognised_regardless_of_suffix_case():
    assert [is_supported(p) for p in ("a.PNG", "b.jpeg", "c.JPG", "d.tif", "e.TIFF")] == [True] * 5
    assert not is_supported("f.gif")


def test_16_bit_tiff_keeps_its_tones_instead_of_clipping_to_white(tmp_path):
    # Pillow's own 16->8 bit conversion clips at 255, so a mid-grey 16-bit
    # scan would load (and trace) as solid white.
    ramp = struct.pack("<256H", *(i * 257 for i in range(256)))
    path = tmp_path / "ramp16.tif"
    Image.frombytes("I;16", (256, 1), ramp).save(path)
    im = load_image(path)
    samples = [im.getpixel((x, 0))[0] for x in (0, 128, 255)]
    assert samples == [0, 128, 255], f"16-bit ramp loaded as {samples}"


def test_exif_rotated_jpeg_is_loaded_upright(tmp_path):
    # Phone photos store pixels sideways plus an orientation tag.
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise to display
    path = tmp_path / "rotated.jpg"
    Image.new("RGB", (40, 20), "red").save(path, exif=exif)
    assert load_image(path).size == (20, 40)


def test_downscaled_trace_still_opens_at_the_original_image_size():
    svg = trace(draw_shapes((400, 200)).convert("RGBA"), TraceSettings(max_size=100))
    header = re.search(r"<svg[^>]*>", svg).group(0)
    assert 'width="400" height="200" viewBox="0 0 100 50"' in header, header


def test_full_resolution_trace_has_a_viewbox_matching_the_image():
    svg = trace(draw_shapes((300, 150)).convert("RGBA"), TraceSettings(max_size=1024))
    header = re.search(r"<svg[^>]*>", svg).group(0)
    assert 'width="300" height="150" viewBox="0 0 300 150"' in header, header


def test_color_trace_reproduces_every_color_in_the_bitmap():
    found = fills(trace(draw_shapes().convert("RGBA"), TraceSettings()))
    for expected in (RED, BLUE, GREEN, (255, 255, 255)):
        assert any(near(expected, f) for f in found), f"no fill near {expected}; fills: {sorted(found)}"


def test_binary_trace_uses_only_black():
    found = fills(trace(draw_shapes().convert("RGBA"), TraceSettings(colormode="binary")))
    assert found == {(0, 0, 0)}, f"binary fills: {sorted(found)}"
