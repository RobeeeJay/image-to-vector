"""Main window: bitmap on the left, live vector preview and trace controls on the right."""
from __future__ import annotations

import multiprocessing
import sys
import time
from pathlib import Path

from PIL import Image
from PySide6.QtCore import (
    QByteArray,
    QCoreApplication,
    QEvent,
    QPointF,
    QRect,
    QRectF,
    QSize,
    QSizeF,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QBrush, QColor, QImage, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .codes import DetectedCode, detect_codes, display_name, erase_regions, overlay_codes
from .runner import TraceRunner
from .tracer import SUPPORTED_SUFFIXES, TraceSettings, is_supported, load_image

if sys.platform == "darwin":
    from .macos import dropped_file_access
else:
    from contextlib import nullcontext as dropped_file_access

# Linux file dialogs match patterns case-sensitively, so list both cases.
OPEN_FILTER = "Images ({})".format(" ".join(f"*{s} *{s.upper()}" for s in SUPPORTED_SUFFIXES))
# Long enough that dragging a slider doesn't spawn a trace per pixel moved.
DEBOUNCE_MS = 200
HIGHLIGHT = QColor(0x3D8BFD)

COMBOS = [  # field, label, [(text, value)]
    # Combo items show "&" literally; unlike button and menu labels they have no mnemonics to escape.
    ("colormode", "Color mode", [("Color", "color"), ("Black & White", "binary")]),
    ("hierarchical", "Layering", [("Stacked", "stacked"), ("Cutout", "cutout")]),
    ("mode", "Curve fitting", [("Spline", "spline"), ("Polygon", "polygon"), ("Pixel", "none")]),
]
SLIDERS = [  # field, label, slider min, slider max, value per step, suffix, tooltip
    ("filter_speckle", "Filter speckle", 0, 128, 1, " px", "Discard patches smaller than this"),
    ("color_precision", "Color precision", 1, 8, 1, " bits", "Bits per channel; more bits keep more colors"),
    ("layer_difference", "Gradient step", 0, 128, 1, "", "Color difference between gradient layers; lower gives more layers"),
    ("corner_threshold", "Corner threshold", 0, 180, 1, "°", "Smallest angle kept as a sharp corner"),
    ("length_threshold", "Segment length", 7, 20, 0.5, " px", "Longer segments give smoother, less detailed curves"),
    ("splice_threshold", "Splice threshold", 0, 180, 1, "°", "Smallest angle change that splits a spline"),
    ("path_precision", "Path precision", 0, 8, 1, " dp", "Decimal places in path coordinates; mostly affects file size"),
    ("max_size", "Trace resolution", 1, 16, 256, " px", "Longest side traced; bigger images are downscaled first. Higher is slower."),
]
# Measured: vtracer ignores these outside color mode / spline fitting, so
# their controls are disabled there rather than left to do nothing.
COLOR_ONLY = ("hierarchical", "color_precision", "layer_difference")
SPLINE_ONLY = ("corner_threshold", "length_threshold", "splice_threshold")

MAX_ZOOM = 32.0  # screen pixels per image pixel, i.e. 3200%
ZOOM_STEP = 1.25
# A mouse wheel notch is 120 units; make one notch one zoom step.
WHEEL_ZOOM_PER_UNIT = ZOOM_STEP ** (1 / 120)
# Idle time after a zoom, pan or resize before re-rendering at the exact scale.
RERENDER_MS = 120


def fit_rect(content: QSize, area: QRect | QRectF) -> QRectF:
    """Largest rect with content's aspect ratio in area, centred horizontally and top-aligned."""
    # Top-aligned so the bitmap and the preview start level: the preview pane
    # is shorter than the bitmap pane because the controls sit under it.
    scale = min(area.width() / content.width(), area.height() / content.height())
    w, h = content.width() * scale, content.height() * scale
    return QRectF(area.x() + (area.width() - w) / 2, area.y(), w, h)


def checker_brush() -> QBrush:
    tile = QPixmap(16, 16)
    tile.fill(QColor(255, 255, 255))
    painter = QPainter(tile)
    painter.fillRect(0, 0, 8, 8, QColor(220, 220, 220))
    painter.fillRect(8, 8, 8, 8, QColor(220, 220, 220))
    painter.end()
    return QBrush(tile)


def to_qimage(image: Image.Image) -> QImage:
    w, h = image.size
    return QImage(image.tobytes(), w, h, 4 * w, QImage.Format.Format_RGBA8888).copy()


class ImageView(QWidget):
    """Shows the loaded bitmap and accepts dropped image files."""

    fileDropped = Signal(str)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setMinimumSize(200, 200)
        self._checker = checker_brush()
        self._pixmap: QPixmap | None = None
        # Smooth-scaling a large photo on every repaint is slow; keep the last result.
        self._scaled: QPixmap | None = None
        self._hover = False

    def set_image(self, image: QImage) -> None:
        self._pixmap = QPixmap.fromImage(image)
        self._scaled = None
        self.update()

    def has_image(self) -> bool:
        return self._pixmap is not None

    def content_rect(self) -> QRect:
        """Where the image is drawn, in widget coordinates."""
        return fit_rect(self._pixmap.size(), self.rect().adjusted(8, 8, -8, -8)).toAlignedRect()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        area = self.rect().adjusted(8, 8, -8, -8)
        if self._pixmap is None:
            border = HIGHLIGHT if self._hover else self.palette().mid().color()
            painter.setPen(QPen(border, 2, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(area, 8, 8)
            painter.setPen(self.palette().text().color())
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "Drop a JPEG, PNG or TIFF here\nor use Open Image…")
            return
        target = self.content_rect()
        dpr = self.devicePixelRatioF()
        pixel_size = target.size() * dpr
        if self._scaled is None or self._scaled.size() != pixel_size:
            # Scaled to device pixels, or Retina screens show it soft.
            self._scaled = self._pixmap.scaled(
                pixel_size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            self._scaled.setDevicePixelRatio(dpr)
        painter.fillRect(target, self._checker)
        painter.drawPixmap(target.topLeft(), self._scaled)
        if self._hover:
            painter.setPen(QPen(HIGHLIGHT, 3))
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))

    def _dropped_path(self, event) -> str | None:
        for url in event.mimeData().urls():
            if url.isLocalFile() and is_supported(url.toLocalFile()):
                return url.toLocalFile()
        return None

    def dragEnterEvent(self, event) -> None:
        if self._dropped_path(event):
            event.acceptProposedAction()
            self._hover = True
            self.update()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._hover = False
        self.update()

    def dropEvent(self, event) -> None:
        self._hover = False
        self.update()
        path = self._dropped_path(event)
        if path:
            event.acceptProposedAction()
            # The slot reads the file synchronously, while the grant is held.
            with dropped_file_access():
                self.fileDropped.emit(path)


class SvgView(QWidget):
    """Renders SVG over a checkerboard; fits the pane until zoomed, then pans."""

    zoomChanged = Signal()

    def __init__(self):
        super().__init__()
        self.setMinimumSize(200, 200)
        self._checker = checker_brush()
        self._renderer: QSvgRenderer | None = None
        self._zoom: float | None = None  # screen px per SVG px; None means fit the pane
        self._origin = QPointF()  # top-left of the SVG in widget coordinates while zoomed
        self._drag_from: QPointF | None = None
        # Traces can hold tens of thousands of paths, too slow to render on
        # every zoom or pan step. Meanwhile the last render is drawn stretched,
        # then replaced by an exact one once the view settles; that re-render
        # is what makes zoomed edges sharp.
        self._cache: QImage | None = None
        self._cache_target = QRectF()  # where the whole SVG sat when the cache was rendered
        self._cache_rect = QRectF()  # the part of the widget the cache covers
        self._rerender = QTimer(self)
        self._rerender.setSingleShot(True)
        self._rerender.setInterval(RERENDER_MS)
        self._rerender.timeout.connect(self._render_now)

    def set_svg(self, svg: str | None) -> None:
        """Show svg, keeping the zoom so detail can be compared across retraces; None clears and refits."""
        self._renderer = QSvgRenderer(QByteArray(svg.encode()), self) if svg else None
        if svg is None:
            self._zoom = None
        self._cache = None
        self._rerender.stop()
        self._update_cursor()
        self.zoomChanged.emit()
        self.update()

    def renderer(self) -> QSvgRenderer | None:
        return self._renderer

    def has_content(self) -> bool:
        return self._renderer is not None and self._renderer.isValid()

    def is_zoomed(self) -> bool:
        return self._zoom is not None

    def _area(self) -> QRectF:
        return QRectF(self.rect().adjusted(8, 8, -8, -8))

    def fit_scale(self) -> float:
        size, area = self._renderer.defaultSize(), self._area()
        return min(area.width() / size.width(), area.height() / size.height())

    def scale(self) -> float:
        """Screen pixels per SVG pixel."""
        return self.fit_scale() if self._zoom is None else self._zoom

    def content_rectf(self) -> QRectF:
        """Where the whole SVG is drawn, in widget coordinates; bigger than the pane when zoomed."""
        if self._zoom is None:
            return fit_rect(self._renderer.defaultSize(), self._area())
        size = self._renderer.defaultSize()
        return QRectF(self._origin, QSizeF(size.width() * self._zoom, size.height() * self._zoom))

    def content_rect(self) -> QRect:
        return self.content_rectf().toAlignedRect()

    def zoom_label(self) -> str:
        if not self.has_content():
            return "–"
        percent = f"{self.scale() * 100:.0f}%"
        return f"Fit ({percent})" if self._zoom is None else percent

    def can_zoom_in(self) -> bool:
        return self.has_content() and self.scale() < max(MAX_ZOOM, self.fit_scale()) * 0.999

    def zoom_to(self, zoom: float, anchor: QPointF | None = None) -> None:
        """Set screen pixels per SVG pixel, keeping the SVG point under anchor (default: pane centre) still."""
        if not self.has_content():
            return
        fit = self.fit_scale()
        zoom = min(zoom, max(MAX_ZOOM, fit))
        # Zooming out stops at fit: smaller only wastes the pane.
        if zoom <= fit * 1.001:
            self._zoom = None
        else:
            anchor = self._area().center() if anchor is None else anchor
            ratio = zoom / self.scale()
            origin = anchor - (anchor - self.content_rectf().topLeft()) * ratio
            self._zoom = zoom
            self._origin = self._clamped(origin)
        self._view_changed()

    def zoom_by(self, factor: float, anchor: QPointF | None = None) -> None:
        self.zoom_to(self.scale() * factor, anchor)

    def fit(self) -> None:
        self._zoom = None
        self._view_changed()

    def pan_by(self, delta: QPointF) -> None:
        if self._zoom is None:
            return
        self._origin = self._clamped(self._origin + delta)
        self._view_changed()

    def _clamped(self, origin: QPointF) -> QPointF:
        # Keep the zoomed SVG covering the pane. Along an axis where it is
        # smaller than the pane, place it as fit does: centred across, top-aligned.
        area, size = self._area(), self._renderer.defaultSize()
        w, h = size.width() * self._zoom, size.height() * self._zoom
        if w <= area.width():
            x = area.x() + (area.width() - w) / 2
        else:
            x = min(area.left(), max(area.right() - w, origin.x()))
        y = area.y() if h <= area.height() else min(area.top(), max(area.bottom() - h, origin.y()))
        return QPointF(x, y)

    def _view_changed(self) -> None:
        self._update_cursor()
        if self._cache is not None:
            self._rerender.start()
        self.zoomChanged.emit()
        self.update()

    def _update_cursor(self) -> None:
        if self._drag_from is not None:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._zoom is not None:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        area = self._area()
        if not self.has_content():
            painter.setPen(self.palette().mid().color())
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, "The vector preview appears here")
            return
        target = self.content_rectf()
        visible = QRectF(target.intersected(area).toAlignedRect())
        painter.setClipRect(area)
        painter.fillRect(visible, self._checker)
        if self._cache is None:
            self._render_cache(target, visible)
        if self._cache_target == target and self._cache_rect == visible:
            painter.drawImage(visible.topLeft(), self._cache)
        else:
            # Stale render stretched onto the new geometry until the exact re-render lands.
            k = target.width() / self._cache_target.width()
            top_left = target.topLeft() + (self._cache_rect.topLeft() - self._cache_target.topLeft()) * k
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(QRectF(top_left, self._cache_rect.size() * k), self._cache)

    def _render_cache(self, target: QRectF, visible: QRectF) -> None:
        # Rendered at device pixels, or Retina screens show it soft.
        dpr = self.devicePixelRatioF()
        image = QImage((visible.size() * dpr).toSize(), QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(-visible.x(), -visible.y())
        self._renderer.render(painter, target)
        painter.end()
        self._cache, self._cache_target, self._cache_rect = image, target, visible

    def _render_now(self) -> None:
        target = self.content_rectf()
        self._render_cache(target, QRectF(target.intersected(self._area()).toAlignedRect()))
        self.update()

    def resizeEvent(self, event) -> None:
        if self._zoom is not None and self.has_content():
            if self._zoom <= self.fit_scale():
                self._zoom = None
            else:
                # The pane may have grown past the SVG's edge.
                self._origin = self._clamped(self._origin)
        if self._cache is not None:
            self._rerender.start()
        self.zoomChanged.emit()  # the fit percentage changes with the pane
        super().resizeEvent(event)

    def mousePressEvent(self, event) -> None:
        if self._zoom is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.position()
            self._update_cursor()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_from is not None:
            self.pan_by(event.position() - self._drag_from)
            self._drag_from = event.position()

    def mouseReleaseEvent(self, event) -> None:
        self._drag_from = None
        self._update_cursor()

    def mouseDoubleClickEvent(self, event) -> None:
        # Toggles fit and 100% at the clicked point (twice fit, for images smaller than the pane).
        if self._zoom is None:
            self.zoom_to(max(1.0, 2 * self.fit_scale()), event.position())
        else:
            self.fit()

    def wheelEvent(self, event) -> None:
        if not self.has_content():
            event.ignore()
        elif event.modifiers() & Qt.KeyboardModifier.ControlModifier:  # Cmd on macOS
            self.zoom_by(WHEEL_ZOOM_PER_UNIT ** event.angleDelta().y(), event.position())
        elif self._zoom is not None:
            pixels = event.pixelDelta()  # trackpads; mouse wheels only report angles
            self.pan_by(QPointF(pixels) if not pixels.isNull() else QPointF(event.angleDelta()) / 4)
        else:
            event.ignore()

    def event(self, event) -> bool:
        # macOS delivers trackpad pinches as native gestures, not wheel events.
        if event.type() == QEvent.Type.NativeGesture and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
            if self.has_content():
                self.zoom_by(1 + event.value(), event.position())
            return True
        return super().event(event)


class SliderRow(QWidget):
    """Slider with its current value shown alongside."""

    valueChanged = Signal()

    def __init__(self, lo: int, hi: int, step_value: float, suffix: str):
        super().__init__()
        self._step_value = step_value
        self._suffix = suffix
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(lo, hi)
        self._label = QLabel()
        self._label.setMinimumWidth(self.fontMetrics().horizontalAdvance("4096 bits"))
        self._label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self._label)
        self.slider.valueChanged.connect(self._on_changed)
        self._on_changed()

    def value(self) -> int | float:
        return self.slider.value() * self._step_value

    def set_value(self, value: int | float) -> None:
        self.slider.setValue(round(value / self._step_value))

    def _on_changed(self) -> None:
        self._label.setText(f"{self.value():g}{self._suffix}")
        self.valueChanged.emit()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Image to Vector")
        self.resize(1200, 780)
        self._path: str | None = None
        self._data: bytes | None = None  # file contents, which is what the worker traces
        self._image: Image.Image | None = None
        self._traced_svg: str | None = None  # the trace alone
        self._codes: list[DetectedCode] = []  # drawn over every trace until unchecked
        # Mode of the trace in flight and of the one shown; settings may change in between.
        self._pending_black_and_white = False
        self._traced_black_and_white = False
        self._svg: str | None = None  # what is previewed and saved
        self._trace_started = 0.0

        self._runner = TraceRunner(self)
        self._runner.finished.connect(self._on_traced)
        self._runner.failed.connect(self._on_trace_failed)
        self._runner.busyChanged.connect(self._on_busy_changed)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._start_trace)

        self.open_action = QAction("Open Image…", self, shortcut=QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self.choose_image)
        self.save_action = QAction("Save SVG…", self, shortcut=QKeySequence.StandardKey.Save, enabled=False)
        self.save_action.triggered.connect(self.choose_save_path)
        quit_action = QAction("Quit", self, shortcut=QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addActions([self.open_action, self.save_action])
        file_menu.addSeparator()
        file_menu.addAction(quit_action)

        self.zoom_in_action = QAction("Zoom In", self)
        # Ctrl+= too: on most layouts "+" needs Shift.
        self.zoom_in_action.setShortcuts([QKeySequence.StandardKey.ZoomIn, QKeySequence("Ctrl+=")])
        self.zoom_in_action.triggered.connect(lambda: self.svg_view.zoom_by(ZOOM_STEP))
        self.zoom_out_action = QAction("Zoom Out", self, shortcut=QKeySequence.StandardKey.ZoomOut)
        self.zoom_out_action.triggered.connect(lambda: self.svg_view.zoom_by(1 / ZOOM_STEP))
        self.fit_action = QAction("Zoom to Fit", self, shortcut=QKeySequence("Ctrl+0"))
        self.fit_action.triggered.connect(lambda: self.svg_view.fit())
        self.actual_size_action = QAction("Actual Size", self, shortcut=QKeySequence("Ctrl+1"))
        self.actual_size_action.triggered.connect(lambda: self.svg_view.zoom_to(1.0))
        view_menu = self.menuBar().addMenu("&View")
        view_menu.addActions([self.zoom_in_action, self.zoom_out_action, self.fit_action, self.actual_size_action])

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([600, 600])
        self.setCentralWidget(splitter)
        self._update_enabled_controls()
        self._update_zoom_controls()

    def _build_left(self) -> QWidget:
        self.image_view = ImageView()
        self.image_view.fileDropped.connect(self.open_path)
        open_button = QPushButton("Open Image…")
        open_button.clicked.connect(self.open_action.trigger)
        self.info_label = QLabel("No image loaded")

        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.addLayout(_header("Bitmap", open_button))
        layout.addWidget(self.image_view, 1)
        layout.addWidget(self.info_label)
        return pane

    def _build_right(self) -> QWidget:
        self.svg_view = SvgView()
        self.svg_view.zoomChanged.connect(self._update_zoom_controls)
        self.zoom_label = QLabel()
        self.zoom_label.setMinimumWidth(self.fontMetrics().horizontalAdvance("Fit (3200%)"))
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom_buttons: list[tuple[QPushButton, QAction]] = []
        for text, action in (("−", self.zoom_out_action), ("+", self.zoom_in_action), ("Fit", self.fit_action)):
            button = QPushButton(text)
            if len(text) == 1:
                # The style's minimum width would make "−" and "+" as wide as "Save SVG…".
                button.setFixedWidth(button.sizeHint().height() + 8)
            shortcut = action.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            button.setToolTip(f"{action.text()} ({shortcut}). Ctrl/⌘+scroll or pinch zooms; drag pans.")
            button.clicked.connect(action.trigger)
            self._zoom_buttons.append((button, action))
        self.detect_button = QPushButton("Detect Codes")
        self.detect_button.setCheckable(True)
        self.detect_button.setEnabled(False)
        self.detect_button.setToolTip("Find barcodes and QR codes and replace their traced shapes with exact ones")
        self.detect_button.toggled.connect(self._on_detect_toggled)
        self.codes_label = QLabel()
        # Decoded values are arbitrary text; rich text would render "<b>" in a QR code as HTML.
        self.codes_label.setTextFormat(Qt.TextFormat.PlainText)
        self.codes_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.codes_label.setWordWrap(True)
        self.codes_label.hide()
        self.save_button = QPushButton("Save SVG…")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_action.trigger)
        self.status_label = QLabel("")
        reset_button = QPushButton("Reset to Defaults")
        reset_button.clicked.connect(lambda: self.apply_settings(TraceSettings()))

        self._inputs: dict[str, QComboBox | SliderRow] = {}
        self._labels: dict[str, QLabel] = {}
        forms = [QFormLayout(), QFormLayout()]
        for field, text, options in COMBOS:
            combo = QComboBox()
            for option_text, value in options:
                combo.addItem(option_text, value)
            combo.currentIndexChanged.connect(self._on_settings_changed)
            self._add_row(forms[0], field, text, combo)
        for i, (field, text, lo, hi, step_value, suffix, tip) in enumerate(SLIDERS):
            row = SliderRow(lo, hi, step_value, suffix)
            row.setToolTip(tip)
            row.valueChanged.connect(self._on_settings_changed)
            self._add_row(forms[0] if i < 3 else forms[1], field, text, row)
        controls = QHBoxLayout()
        controls.addLayout(forms[0], 1)
        controls.addSpacing(16)
        controls.addLayout(forms[1], 1)

        footer = QHBoxLayout()
        footer.addWidget(self.status_label, 1)
        footer.addWidget(reset_button)

        pane = QWidget()
        layout = QVBoxLayout(pane)
        zoom_out_button, zoom_in_button, fit_button = (button for button, _ in self._zoom_buttons)
        layout.addLayout(
            _header(
                "Vector preview", zoom_out_button, self.zoom_label, zoom_in_button, fit_button,
                self.detect_button, self.save_button,
            )
        )
        layout.addWidget(self.svg_view, 1)
        layout.addLayout(controls)
        layout.addLayout(footer)
        layout.addWidget(self.codes_label)
        self.apply_settings(TraceSettings())
        return pane

    def _add_row(self, form: QFormLayout, field: str, text: str, widget: QWidget) -> None:
        label = QLabel(text)
        form.addRow(label, widget)
        self._inputs[field] = widget
        self._labels[field] = label

    def settings(self) -> TraceSettings:
        values = {
            field: widget.currentData() if isinstance(widget, QComboBox) else widget.value()
            for field, widget in self._inputs.items()
        }
        return TraceSettings(**values)

    def apply_settings(self, settings: TraceSettings) -> None:
        for field, widget in self._inputs.items():
            value = getattr(settings, field)
            if isinstance(widget, QComboBox):
                widget.setCurrentIndex(widget.findData(value))
            else:
                widget.set_value(value)

    def _on_settings_changed(self) -> None:
        self._update_enabled_controls()
        if self._path is not None:
            self._debounce.start()

    def _update_enabled_controls(self) -> None:
        settings = self.settings()
        for field in COLOR_ONLY:
            self._set_row_enabled(field, settings.colormode == "color")
        for field in SPLINE_ONLY:
            self._set_row_enabled(field, settings.mode == "spline")

    def _set_row_enabled(self, field: str, enabled: bool) -> None:
        self._inputs[field].setEnabled(enabled)
        self._labels[field].setEnabled(enabled)

    def _update_zoom_controls(self) -> None:
        view = self.svg_view
        self.zoom_in_action.setEnabled(view.can_zoom_in())
        self.zoom_out_action.setEnabled(view.is_zoomed())
        self.fit_action.setEnabled(view.is_zoomed())
        self.actual_size_action.setEnabled(view.has_content())
        for button, action in self._zoom_buttons:
            button.setEnabled(action.isEnabled())
        self.zoom_label.setText(view.zoom_label())

    def choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open Image", "", OPEN_FILTER)
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> None:
        name = Path(path).name
        if not is_supported(path):
            QMessageBox.warning(self, "Unsupported file", f"{name} is not a JPEG, PNG or TIFF image.")
            return
        try:
            data = Path(path).read_bytes()
            image = load_image(data)
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            QMessageBox.warning(self, "Can't open image", f"{name}: {exc}")
            return
        self._path = path
        self._data = data
        self._image = image
        self._traced_svg = None
        self._svg = None
        self._codes = []
        self._set_detect_checked(False)
        self.detect_button.setEnabled(True)
        self.codes_label.hide()
        self.image_view.set_image(to_qimage(image))
        self.svg_view.set_svg(None)
        self.info_label.setText(f"{name}  ·  {image.width} × {image.height} px")
        self.setWindowTitle(f"{name} — Image to Vector")
        self._start_trace()

    def _start_trace(self) -> None:
        self._debounce.stop()
        self._trace_started = time.monotonic()
        settings = self.settings()
        self._pending_black_and_white = settings.colormode == "binary"
        self._runner.start(self._data, settings, erase_regions(self._codes))

    def _on_busy_changed(self, busy: bool) -> None:
        if busy:
            self.status_label.setText("Tracing…")
        # Saving is blocked mid-trace so the file always matches the current settings.
        can_save = not busy and self._svg is not None
        self.save_action.setEnabled(can_save)
        self.save_button.setEnabled(can_save)

    def _on_traced(self, svg: str) -> None:
        self._traced_svg = svg
        self._traced_black_and_white = self._pending_black_and_white
        self._show_svg()
        elapsed = time.monotonic() - self._trace_started
        size_kb = len(svg.encode()) / 1024
        self.status_label.setText(f"{svg.count('<path'):,} paths  ·  {size_kb:,.0f} KB  ·  traced in {elapsed:.1f} s")
        self._on_busy_changed(False)

    def _on_trace_failed(self, message: str) -> None:
        self._traced_svg = None
        self._svg = None
        self.svg_view.set_svg(None)
        self.status_label.setText(f"Trace failed: {message}")
        self._on_busy_changed(False)

    def _show_svg(self) -> None:
        if self._traced_svg is None:
            return
        if self._codes:
            self._svg = overlay_codes(self._traced_svg, self._codes, self._traced_black_and_white)
        else:
            self._svg = self._traced_svg
        self.svg_view.set_svg(self._svg)

    def _set_detect_checked(self, checked: bool) -> None:
        self.detect_button.blockSignals(True)
        self.detect_button.setChecked(checked)
        self.detect_button.blockSignals(False)

    def _on_detect_toggled(self, checked: bool) -> None:
        if not checked:
            self._codes = []
            self.codes_label.hide()
            self._show_svg()
            self._start_trace()  # the code areas were erased before tracing; bring them back
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            codes, skipped = detect_codes(self._image)
        finally:
            QApplication.restoreOverrideCursor()
        lines = [f"{display_name(c.format)}: {c.text}" for c in codes]
        lines += [f"{display_name(s.format)}: {s.text}  (left as traced: {s.reason})" for s in skipped]
        self.codes_label.setText("\n".join(lines) or "No barcodes or QR codes found.")
        self.codes_label.show()
        if not codes:
            self._set_detect_checked(False)
            return
        self._codes = codes
        self._show_svg()
        self._start_trace()  # retrace with the code areas erased

    def svg_text(self) -> str | None:
        return self._svg

    def choose_save_path(self) -> None:
        dialog = QFileDialog(self, "Save SVG", default_save_path(self._path), "SVG files (*.svg)")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setDefaultSuffix("svg")
        if dialog.exec() and dialog.selectedFiles():
            self.write_svg(dialog.selectedFiles()[0])

    def write_svg(self, path: str) -> None:
        try:
            Path(path).write_text(self._svg, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Can't save SVG", f"{path}: {exc}")
            return
        self.status_label.setText(f"Saved {path}")

    def closeEvent(self, event) -> None:
        self._runner.cancel()
        super().closeEvent(event)


def default_save_path(source: str) -> str:
    """source renamed to .svg, in the same folder unless that is app data under ~/Library."""
    # Images dropped from Messages live in ~/Library/Messages/Attachments;
    # an SVG saved there would be lost inside Messages' own storage.
    src = Path(source)
    folder = src.parent
    if folder.is_relative_to(Path.home() / "Library"):
        folder = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation))
    return str(folder / f"{src.stem}.svg")


def _header(title: str, *widgets: QWidget) -> QHBoxLayout:
    label = QLabel(title)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    row = QHBoxLayout()
    row.addWidget(label)
    row.addStretch(1)
    for widget in widgets:
        row.addWidget(widget)
    return row


def export(image_path: str, svg_path: str, codes: bool = False) -> int:
    """Trace with default settings and write the SVG, without opening a window."""
    if not is_supported(image_path):
        print(f"error: {image_path} is not a JPEG, PNG or TIFF image", file=sys.stderr)
        return 1
    # Goes through TraceRunner, not tracer.trace directly, so a smoke test of a
    # frozen bundle also proves its worker processes can start.
    try:
        data = Path(image_path).read_bytes()
    except OSError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    found = []
    if codes:
        # Decoded values can be any text, and Windows pipes default to cp1252:
        # escape what the output can't encode instead of crashing. Frozen
        # windowed apps have no streams at all.
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.reconfigure(errors="backslashreplace")
        found, skipped = detect_codes(load_image(data))
        for code in found:
            print(f"{display_name(code.format)}: {code.text}")
        for code in skipped:
            print(f"{display_name(code.format)}: {code.text} (left as traced: {code.reason})", file=sys.stderr)
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    runner = TraceRunner()
    outcome: dict[str, str] = {}

    def done(key: str, value: str) -> None:
        outcome[key] = value
        app.quit()

    runner.finished.connect(lambda svg: done("svg", svg))
    runner.failed.connect(lambda message: done("error", message))
    runner.start(data, TraceSettings(), erase_regions(found))
    app.exec()
    if "error" in outcome:
        print(f"error: {outcome['error']}", file=sys.stderr)
        return 1
    svg = outcome["svg"]
    if found:
        svg = overlay_codes(svg, found)
    Path(svg_path).write_text(svg, encoding="utf-8")
    return 0


def main() -> int:
    # Lets frozen (PyInstaller) builds start trace worker processes.
    multiprocessing.freeze_support()
    args = sys.argv[1:]
    if args[:1] == ["--export"]:
        paths = [a for a in args[1:] if a != "--codes"]
        if len(paths) != 2:
            print("usage: image-to-vector --export [--codes] IMAGE OUT.svg", file=sys.stderr)
            return 2
        return export(paths[0], paths[1], codes="--codes" in args)
    app = QApplication(sys.argv)
    app.setApplicationName("Image to Vector")
    window = MainWindow()
    window.show()
    args = app.arguments()[1:]
    if args:
        window.open_path(args[0])
    return app.exec()
