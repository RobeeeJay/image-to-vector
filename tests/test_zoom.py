import os
import subprocess
import sys
import textwrap
from dataclasses import replace

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QNativeGestureEvent, QPointingDevice, QWheelEvent
from PySide6.QtWidgets import QApplication

from conftest import RED, draw_shapes
from image_to_vector.app import MAX_ZOOM, ZOOM_STEP


@pytest.fixture
def view(window, shapes_png, wait_until, qapp):
    window.resize(1200, 780)
    window.show()
    window.open_path(str(shapes_png))
    assert wait_until(lambda: window.svg_text() is not None), window.status_label.text()
    window.svg_view.grab()  # paint once so a render is cached, as on screen
    return window.svg_view


def svg_point_under(view, widget_point: QPointF) -> QPointF:
    return (widget_point - view.content_rectf().topLeft()) / view.scale()


def widget_point_of(view, svg_point: QPointF) -> QPointF:
    return view.content_rectf().topLeft() + svg_point * view.scale()


def test_zooming_keeps_the_point_under_the_cursor_still(view):
    # Otherwise the detail being inspected slides away with every zoom step.
    anchor = widget_point_of(view, QPointF(60, 90))
    before, fit = svg_point_under(view, anchor), view.scale()
    view.zoom_by(4, anchor)
    after = svg_point_under(view, anchor)
    assert view.scale() == pytest.approx(fit * 4)
    assert (after - before).manhattanLength() < 0.01, f"point under cursor moved from {before} to {after}"


def test_zooming_out_stops_at_fit(view):
    fit = view.scale()
    view.zoom_by(4)
    view.zoom_by(1 / 100)
    assert not view.is_zoomed() and view.scale() == pytest.approx(fit), view.zoom_label()
    assert view.zoom_label().startswith("Fit"), view.zoom_label()


def test_zoom_is_capped(view):
    view.zoom_to(1000)
    assert view.scale() == pytest.approx(MAX_ZOOM), view.scale()


def test_panning_never_drags_the_image_off_its_pane(view):
    view.zoom_by(8)
    area = view._area()
    for delta in (QPointF(10_000, 10_000), QPointF(-20_000, -20_000)):
        view.pan_by(delta)
        assert view.content_rectf().contains(area), f"after pan {delta}: {view.content_rectf()} vs pane {area}"


def test_retrace_keeps_the_zoom_but_a_new_image_resets_it(window, view, shapes_png, wait_until):
    # Zooming in and then moving sliders is how detail gets compared.
    view.zoom_by(4)
    zoomed = view.scale()
    window.apply_settings(replace(window.settings(), colormode="binary"))
    assert wait_until(lambda: 'fill="#000000"' in (window.svg_text() or "") and not window._runner.is_busy())
    assert view.scale() == pytest.approx(zoomed), "retrace changed the zoom"
    window.open_path(str(shapes_png))
    assert not view.is_zoomed(), "opening an image kept the old zoom"


def test_zoomed_preview_is_re_rendered_with_sharp_edges(view, wait_until, qapp):
    # Stretching the fit-size render would leave a blurred edge many pixels
    # wide; re-rendering the vector at the new scale keeps it about one pixel.
    edge = QPointF(20, 100)  # left edge of the red circle, white to its left
    view.zoom_to(MAX_ZOOM, widget_point_of(view, edge))
    assert wait_until(lambda: not view._rerender.isActive(), timeout=5)
    qapp.processEvents()
    image = view.grab().toImage()
    at = widget_point_of(view, edge).toPoint()

    def near(c, rgb):
        return all(abs(a - b) <= 40 for a, b in zip((c.red(), c.green(), c.blue()), rgb))

    row = [image.pixelColor(x, at.y()) for x in range(at.x() - 60, at.x() + 60)]
    blurred = [c for c in row if not near(c, (255, 255, 255)) and not near(c, RED)]
    assert near(row[0], (255, 255, 255)) and near(row[-1], RED), (row[0], row[-1])
    assert len(blurred) <= 4, f"{len(blurred)} px of in-between color across the edge"


def _wheel(view, pos: QPointF, angle_y: int, modifiers):
    event = QWheelEvent(
        pos, view.mapToGlobal(pos), QPoint(), QPoint(0, angle_y),
        Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(view, event)


def test_ctrl_scroll_zooms_and_plain_scroll_pans(view):
    anchor = view.content_rectf().center()
    fit = view.scale()
    _wheel(view, anchor, 120, Qt.KeyboardModifier.ControlModifier)
    assert view.scale() == pytest.approx(fit * ZOOM_STEP), "one wheel notch should be one zoom step"
    view.zoom_by(4, anchor)
    top = view.content_rectf().top()
    _wheel(view, anchor, -120, Qt.KeyboardModifier.NoModifier)
    assert view.content_rectf().top() == pytest.approx(top - 30), "scrolling down should move the image up"


def test_trackpad_pinch_zooms(view):
    fit = view.scale()
    anchor = view.content_rectf().center()
    event = QNativeGestureEvent(
        Qt.NativeGestureType.ZoomNativeGesture, QPointingDevice.primaryPointingDevice(), 2,
        anchor, anchor, view.mapToGlobal(anchor), 0.5, QPointF(), 1,
    )
    QApplication.sendEvent(view, event)
    assert view.scale() == pytest.approx(fit * 1.5), view.scale()


def test_zoom_controls_follow_the_view(window, view):
    assert window.zoom_label.text().startswith("Fit"), window.zoom_label.text()
    assert window.zoom_in_action.isEnabled() and not window.fit_action.isEnabled()
    window.zoom_in_action.trigger()
    assert window.zoom_label.text() == f"{view.scale() * 100:.0f}%", window.zoom_label.text()
    assert window.fit_action.isEnabled() and window.zoom_out_action.isEnabled()
    window.fit_action.trigger()
    assert not view.is_zoomed()


def test_zoom_step_buttons_are_compact(window):
    # Default push-button width made the one-character buttons crowd the header.
    minus, plus, _ = (button for button, _ in window._zoom_buttons)
    widths = [minus.maximumWidth(), plus.maximumWidth()]
    assert max(widths) <= 48, f"zoom step buttons are {widths} px wide"


def test_zoom_controls_are_disabled_without_an_image(window):
    assert not any(a.isEnabled() for a in (window.zoom_in_action, window.zoom_out_action, window.fit_action))


def test_both_panes_render_at_device_pixels_on_high_dpi_screens(tmp_path):
    # Retina screens have 2 device pixels per point; rendering at 1x shows soft.
    # Qt's scale factor is fixed at startup, hence a separate process.
    script = textwrap.dedent("""
        from PySide6.QtGui import QImage, QColor
        from PySide6.QtWidgets import QApplication
        from image_to_vector.app import ImageView, SvgView
        app = QApplication([])
        svg = SvgView(); svg.resize(300, 300); svg.show()
        svg.set_svg('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
                    '<rect width="200" height="100" fill="red"/></svg>')
        svg.grab()
        bitmap = ImageView(); bitmap.resize(300, 300); bitmap.show()
        image = QImage(200, 200, QImage.Format.Format_RGB32); image.fill(QColor("red"))
        bitmap.set_image(image); bitmap.grab()
        print(svg._cache.devicePixelRatio(), svg._cache.width() / svg._cache_rect.width(),
              bitmap._scaled.devicePixelRatio(), bitmap._scaled.width() / bitmap.content_rect().width())
    """)
    # The full environment, not a minimal one: Windows Python needs SYSTEMROOT
    # to start and Qt finds its DLLs through PATH.
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR="2")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
    values = [float(v) for v in result.stdout.split()]
    assert values == pytest.approx([2, 2, 2, 2], abs=0.02), f"svg dpr, svg px/pt, bitmap dpr, bitmap px/pt: {values}"
