import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QImage, QPainter
from PySide6.QtWidgets import QMessageBox

from conftest import BLUE, GREEN, RED, draw_shapes
from pathlib import Path

from image_to_vector.app import SLIDERS, ImageView, MainWindow, default_save_path
from image_to_vector.runner import TraceRunner
from image_to_vector.tracer import TraceSettings, trace


def test_black_and_white_option_shows_a_single_ampersand(window):
    # Combo items have no mnemonics, so an escaped "&&" is shown literally.
    combo = window._inputs["colormode"]
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert "Black & White" in texts, texts


def test_opening_an_image_shows_it_and_traces_a_preview(window, shapes_png, wait_until):
    window.open_path(str(shapes_png))
    assert window.image_view.has_image()
    assert wait_until(lambda: window.svg_text() is not None), window.status_label.text()
    assert window.svg_view.renderer().isValid()
    assert window.save_action.isEnabled() and window.save_button.isEnabled()
    assert "paths" in window.status_label.text(), window.status_label.text()


def test_preview_renders_the_bitmaps_colors_in_the_right_places(window, shapes_png, wait_until):
    # End-to-end check that Qt's SVG renderer draws vtracer's output where the
    # source bitmap has those colors, not merely that some SVG was produced.
    window.open_path(str(shapes_png))
    assert wait_until(lambda: window.svg_text() is not None), window.status_label.text()
    renderer = window.svg_view.renderer()
    out = QImage(200, 200, QImage.Format.Format_RGB32)
    out.fill(Qt.GlobalColor.black)
    painter = QPainter(out)
    renderer.render(painter)
    painter.end()
    # Expected colors come from the source bitmap: hand-picked ones were wrong once.
    source = draw_shapes()
    points = [(100, 30), (25, 100), (100, 170), (5, 5)]
    assert {source.getpixel(p) for p in points} == {BLUE, RED, GREEN, (255, 255, 255)}, "points must cover every color"
    for (x, y) in points:
        expected = source.getpixel((x, y))
        c = out.pixelColor(x, y)
        got = (c.red(), c.green(), c.blue())
        assert all(abs(a - b) <= 24 for a, b in zip(got, expected)), f"at {(x, y)} expected {expected}, got {got}"


def test_bitmap_and_vector_preview_tops_line_up(window, shapes_png, wait_until, qapp):
    # Comparing the two images is easier when they start level. The vector
    # pane is shorter (controls sit under it), so vertical centring put the
    # square test image at different heights in the two panes.
    window.resize(1200, 780)
    window.show()
    window.open_path(str(shapes_png))
    assert wait_until(lambda: window.svg_text() is not None)
    qapp.processEvents()
    left = window.image_view.mapTo(window, window.image_view.content_rect().topLeft()).y()
    right = window.svg_view.mapTo(window, window.svg_view.content_rect().topLeft()).y()
    assert left == right, f"bitmap top at y={left}, vector top at y={right}"


def test_saved_svg_is_the_previewed_svg_and_parses_as_xml(window, shapes_png, wait_until, tmp_path):
    window.open_path(str(shapes_png))
    assert wait_until(lambda: window.svg_text() is not None)
    out = tmp_path / "out.svg"
    window.write_svg(str(out))
    assert out.read_text(encoding="utf-8") == window.svg_text()
    root = ET.parse(out).getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg", root.tag


def test_changing_a_setting_retraces_with_the_new_setting(window, shapes_png, wait_until):
    window.open_path(str(shapes_png))
    assert wait_until(lambda: window.svg_text() is not None)
    window.apply_settings(replace(window.settings(), colormode="binary"))
    assert wait_until(lambda: set(re.findall(r'fill="(#[0-9A-F]{6})"', window.svg_text() or "")) == {"#000000"}), (
        window.status_label.text()
    )


def test_save_is_blocked_while_a_retrace_runs(window, shapes_png, wait_until):
    # The preview still shows the old trace mid-retrace; saving then would
    # write a file that doesn't match the settings on screen.
    window.open_path(str(shapes_png))
    assert wait_until(lambda: window.svg_text() is not None)
    window.apply_settings(replace(window.settings(), filter_speckle=20))
    assert wait_until(lambda: window._runner.is_busy(), timeout=5), "retrace never started"
    assert not window.save_action.isEnabled() and not window.save_button.isEnabled()
    assert wait_until(lambda: not window._runner.is_busy())
    assert window.save_action.isEnabled() and window.save_button.isEnabled()


def test_controls_vtracer_ignores_are_disabled_for_the_current_mode(window):
    window.apply_settings(TraceSettings(colormode="binary", mode="polygon"))
    for field in ("hierarchical", "color_precision", "layer_difference", "corner_threshold", "splice_threshold"):
        assert not window._inputs[field].isEnabled(), f"{field} should be disabled"
    window.apply_settings(TraceSettings())
    assert all(w.isEnabled() for w in window._inputs.values())


def test_every_slider_extreme_produces_a_trace():
    # The slider ranges are what users can reach, so each end must be a
    # value vtracer accepts.
    image = draw_shapes((60, 60)).convert("RGBA")
    for field, _, lo, hi, step_value, _, _ in SLIDERS:
        for raw in (lo, hi):
            value = raw * step_value
            svg = trace(image, replace(TraceSettings(), **{field: value}))
            assert "<path" in svg, f"{field}={value} produced no paths"


def test_superseded_trace_is_killed_and_never_reported(qapp, shapes_png, wait_until):
    # Moving a slider mid-trace must not let the old, slower result land after the new one.
    runner = TraceRunner()
    results = []
    runner.finished.connect(results.append)
    runner.start(shapes_png.read_bytes(), TraceSettings())
    runner.start(shapes_png.read_bytes(), TraceSettings(colormode="binary"))
    assert wait_until(lambda: not runner.is_busy())
    qapp.processEvents()
    assert len(results) == 1, f"{len(results)} results delivered"
    assert set(re.findall(r'fill="(#[0-9A-F]{6})"', results[0])) == {"#000000"}


def test_trace_errors_are_reported_not_swallowed(qapp, wait_until):
    runner = TraceRunner()
    errors = []
    runner.failed.connect(errors.append)
    runner.start(b"not an image", TraceSettings())
    assert wait_until(lambda: bool(errors))
    assert "UnidentifiedImageError" in errors[0], errors[0]


def test_retrace_works_after_the_source_file_becomes_unreadable(window, shapes_png, wait_until):
    # Stands in for a macOS protected folder, where the window process was
    # granted access by the drop but the worker process never is: the worker
    # must trace the bytes read at open, not reopen the path.
    window.open_path(str(shapes_png))
    assert wait_until(lambda: window.svg_text() is not None)
    shapes_png.unlink()
    window.apply_settings(replace(window.settings(), colormode="binary"))
    assert wait_until(lambda: set(re.findall(r'fill="(#[0-9A-F]{6})"', window.svg_text() or "")) == {"#000000"}), (
        window.status_label.text()
    )


def test_dropped_file_is_opened_while_the_macos_access_grant_is_held(qapp, shapes_png, monkeypatch):
    # The grant from the drag pasteboard is released when the context exits,
    # so reading the file after that would be refused for protected folders.
    import contextlib

    import image_to_vector.app as app_module

    held = []

    @contextlib.contextmanager
    def fake_access():
        held.append(True)
        yield
        held.append(False)

    monkeypatch.setattr(app_module, "dropped_file_access", fake_access)
    view = ImageView()
    seen = []
    view.fileDropped.connect(lambda path: seen.append(held[-1] if held else None))
    mime, enter, drop = _drag_events(shapes_png)
    view.dropEvent(drop)
    assert seen == [True], f"grant held during open: {seen}"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_macos_access_grant_is_harmless_with_no_drag_in_progress():
    # Exercises the real PyObjC calls, so a renamed or unbundled API fails here.
    from image_to_vector.macos import dropped_file_access

    with dropped_file_access():
        pass


def test_save_defaults_beside_the_source_unless_it_is_app_data(tmp_path):
    assert default_save_path(str(tmp_path / "photo.jpeg")) == str(tmp_path / "photo.svg")
    messages = Path.home() / "Library/Messages/Attachments/bd/13/X/Screenshot.jpeg"
    saved = Path(default_save_path(str(messages)))
    assert saved.name == "Screenshot.svg" and not saved.is_relative_to(Path.home() / "Library"), saved


def _drag_events(path):
    # The events keep a raw pointer to the QMimeData, so callers must hold on
    # to it; letting it be collected segfaults inside mimeData().
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    enter = QDragEnterEvent(QPoint(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    return mime, enter, drop


def test_dropping_a_supported_image_opens_it(qapp, shapes_png):
    view = ImageView()
    dropped = []
    view.fileDropped.connect(dropped.append)
    mime, enter, drop = _drag_events(shapes_png)
    view.dragEnterEvent(enter)
    assert enter.isAccepted()
    view.dropEvent(drop)
    # Compared as paths: Qt gives C:/... with forward slashes on Windows.
    assert [Path(p) for p in dropped] == [shapes_png], dropped


def test_dragging_an_unsupported_file_is_refused(qapp, tmp_path):
    gif = tmp_path / "anim.gif"
    Image.new("RGB", (4, 4)).save(gif)
    view = ImageView()
    mime, enter, _ = _drag_events(gif)
    view.dragEnterEvent(enter)
    assert not enter.isAccepted()


@pytest.fixture
def qr_png(tmp_path):
    from conftest import symbol_image

    path = tmp_path / "qr.png"
    symbol_image("<b>hi</b> & more", "QRCode", 8).save(path)
    return path


def test_detect_codes_overlays_exact_codes_and_saves_them(window, qr_png, wait_until, tmp_path):
    window.open_path(str(qr_png))
    assert wait_until(lambda: window.svg_text() is not None)
    window.detect_button.click()
    assert window.detect_button.isChecked()
    assert window.codes_label.text() == "QR Code: <b>hi</b> & more", window.codes_label.text()
    # Plain text, or a decoded "<b>" would be rendered as HTML.
    assert window.codes_label.textFormat() == Qt.TextFormat.PlainText
    assert 'data-format="QRCode"' in window.svg_text()
    out = tmp_path / "out.svg"
    window.write_svg(str(out))
    assert 'data-value="&lt;b&gt;hi&lt;/b&gt; &amp; more"' in out.read_text(encoding="utf-8")
    window.detect_button.click()
    assert "data-format" not in window.svg_text() and window.codes_label.isHidden()


def test_retrace_keeps_detected_codes(window, qr_png, wait_until):
    # Detected codes are drawn over every retrace until turned off.
    window.open_path(str(qr_png))
    assert wait_until(lambda: window.svg_text() is not None)
    window.detect_button.click()
    window.apply_settings(replace(window.settings(), filter_speckle=20))
    assert wait_until(lambda: window._runner.is_busy(), timeout=5)
    assert wait_until(lambda: not window._runner.is_busy())
    assert 'data-format="QRCode"' in window.svg_text()


def test_black_and_white_mode_draws_codes_black_on_transparent(window, qr_png, wait_until):
    window.apply_settings(replace(window.settings(), colormode="binary"))
    window.open_path(str(qr_png))
    assert wait_until(lambda: window.svg_text() is not None)
    window.detect_button.click()
    # Detecting retraces with the code area erased, so no trace sits under it.
    assert window._runner.is_busy(), "detect did not retrace"
    assert wait_until(lambda: not window._runner.is_busy())
    group = re.search(r"<g data-format.*?</g>", window.svg_text(), re.S).group(0)
    assert set(re.findall(r'fill="(#[0-9A-F]{6})"', group)) == {"#000000"}, group[:300]
    # Back in color the code gets its light background again.
    window.apply_settings(replace(window.settings(), colormode="color"))
    assert wait_until(lambda: window._runner.is_busy(), timeout=5)
    assert wait_until(lambda: not window._runner.is_busy())
    group = re.search(r"<g data-format.*?</g>", window.svg_text(), re.S).group(0)
    assert len(set(re.findall(r'fill="(#[0-9A-F]{6})"', group))) == 2, group[:300]


def test_detect_with_nothing_found_says_so(window, shapes_png, wait_until):
    window.open_path(str(shapes_png))
    assert wait_until(lambda: window.svg_text() is not None)
    window.detect_button.click()
    assert window.codes_label.text() == "No barcodes or QR codes found."
    assert not window.detect_button.isChecked()


def test_opening_another_image_clears_detected_codes(window, qr_png, shapes_png, wait_until):
    window.open_path(str(qr_png))
    assert wait_until(lambda: window.svg_text() is not None)
    window.detect_button.click()
    window.open_path(str(shapes_png))
    assert wait_until(lambda: window.svg_text() is not None)
    assert not window.detect_button.isChecked() and "data-format" not in window.svg_text()


def test_export_with_codes_lists_and_overlays_them(qr_png, tmp_path):
    out = tmp_path / "out.svg"
    result = _run_export("--codes", qr_png, out)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "QR Code: <b>hi</b> & more", result.stdout
    assert 'data-format="QRCode"' in out.read_text(encoding="utf-8")


def test_export_prints_values_the_output_encoding_cannot_represent(tmp_path):
    # Windows pipes default to cp1252, which has no Cyrillic; the value must
    # come out escaped rather than crash the export with UnicodeEncodeError.
    from conftest import symbol_image

    png, out = tmp_path / "ru.png", tmp_path / "out.svg"
    symbol_image("товар", "QRCode", 8).save(png)
    result = _run_export("--codes", png, out, PYTHONIOENCODING="cp1252")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "QR Code: \\u0442\\u043e\\u0432\\u0430\\u0440", result.stdout
    assert 'data-value="товар"' in out.read_text(encoding="utf-8")


def _run_export(*args, **env_overrides):
    # A real subprocess with no display settings: export must work headless,
    # which is how bundles are smoke-tested on CI.
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "image_to_vector", "--export", *map(str, args)],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_export_writes_the_traced_svg_without_a_window(shapes_png, tmp_path):
    out = tmp_path / "out.svg"
    result = _run_export(shapes_png, out)
    assert result.returncode == 0, result.stderr
    assert ET.parse(out).getroot().tag == "{http://www.w3.org/2000/svg}svg"
    fills = set(re.findall(r'fill="#([0-9A-F]{6})"', out.read_text(encoding="utf-8")))
    assert len(fills) >= 4, f"expected the four source colors, got {fills}"


def test_export_of_a_missing_file_fails_with_a_message(tmp_path):
    result = _run_export(tmp_path / "missing.png", tmp_path / "out.svg")
    assert result.returncode == 1 and "FileNotFoundError" in result.stderr, (result.returncode, result.stderr)
    assert not (tmp_path / "out.svg").exists()


def test_unreadable_image_shows_an_error_and_keeps_the_window_usable(window, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not a png")
    window.open_path(str(bad))
    assert warnings and "broken.png" in warnings[0], warnings
    assert not window.image_view.has_image()
