import os
import time

# Must be set before Qt is imported anywhere, so tests never open real windows.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image, ImageDraw

RED, BLUE, GREEN = (200, 30, 30), (30, 30, 200), (40, 160, 60)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp):
    from image_to_vector.app import MainWindow

    w = MainWindow()
    yield w
    w.close()


@pytest.fixture
def wait_until(qapp):
    def wait(predicate, timeout=30.0):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        return predicate()

    return wait


def draw_shapes(size=(200, 200)) -> Image.Image:
    """Flat red circle, blue triangle and green square on white."""
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    w, h = size
    d.ellipse((0.1 * w, 0.1 * h, 0.9 * w, 0.9 * h), fill=RED)
    d.polygon([(0.5 * w, 0.05 * h), (0.95 * w, 0.75 * h), (0.05 * w, 0.75 * h)], fill=BLUE)
    d.rectangle((0.3 * w, 0.6 * h, 0.7 * w, 0.95 * h), fill=GREEN)
    return im


def symbol_image(content, format_name, scale, *, hrt=False, dark=(0, 0, 0), light=(255, 255, 255), **options):
    """A barcode or QR code made by zxing-cpp, with its quiet zone, in the given colors."""
    import zxingcpp

    barcode = zxingcpp.create_barcode(content, getattr(zxingcpp.BarcodeFormat, format_name), **options)
    view = memoryview(barcode.to_image(scale=scale, add_hrt=hrt, add_quiet_zones=True))
    gray = Image.frombytes("L", (view.shape[1], view.shape[0]), bytes(view))
    mask = gray.point(lambda v: 255 if v < 128 else 0)
    return Image.composite(Image.new("RGB", gray.size, dark), Image.new("RGB", gray.size, light), mask)


@pytest.fixture
def shapes_png(tmp_path):
    path = tmp_path / "shapes.png"
    draw_shapes().save(path)
    return path
