# Image to Vector

Desktop app for macOS, Windows and Linux that traces bitmap images into SVG.

- **Left pane:** drop a JPEG, PNG or TIFF onto it, or use **Open Image…** (Ctrl/⌘+O).
- **Right pane:** live vector preview. The sliders retrace the image as you
  move them. **Save SVG…** (Ctrl/⌘+S) writes the previewed SVG.

It is built with PySide6 (Qt) for the UI, [vtracer](https://github.com/visioncortex/vtracer)
for tracing and Pillow for decoding.

## Running

Needs Python 3.12+. With [uv](https://docs.astral.sh/uv/):

```sh
uv run image-to-vector            # or: uv run image-to-vector path/to/photo.jpg
```

With plain pip:

```sh
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install .
image-to-vector
```

On minimal Linux installs Qt also needs the `libxcb-cursor0` system package
(`xcb-cursor` on some distributions).

Headless conversion with default settings, no window:

```sh
uv run image-to-vector --export photo.jpg photo.svg
```

## Standalone bundles

PyInstaller builds a self-contained app that needs no Python install. It
cannot cross-compile, so build on each target OS:

```sh
uv run python packaging/build.py
```

This writes `dist/Image to Vector.app` on macOS (`dist/Image to Vector/` on
Windows and Linux) plus a zip. The script then smoke-tests the bundle: a
headless `--export` that checks the SVG's colors, and a GUI start-up under
Qt's offscreen platform.

- **macOS (arm64):** built and smoke-tested. Bundle 112 MB, zip 43 MB. The build
  is ad-hoc signed only, so on another Mac Gatekeeper blocks it until it is
  signed with a Developer ID and notarized (or the user right-clicks → Open).
  It runs only on the architecture it was built on.
- **Windows and Linux:** `.github/workflows/bundles.yml` builds and
  smoke-tests on GitHub's runners. On its first run the Linux bundle built and
  passed its smoke test; that is headless, so it has not been seen on a Linux
  desktop. Windows failed in the test suite. Two tests broke on Windows by
  construction: a subprocess started with a stripped environment (Windows
  Python needs `SYSTEMROOT`), and a path compared as a string (Qt returns
  `C:/...` with forward slashes). Both are fixed, but the Windows job has not
  run again yet. Test failures now appear as annotations on the workflow run,
  which can be read without access to the logs.

Gotcha: zxing-cpp's native code imports Python's `json` module when it reads a
barcode's metadata. PyInstaller's import scan can't see that, so the first
bundle with code detection failed with `Invalid JSON in Barcode::extra()`
while the same code worked from source. The build now declares `json` as a
hidden import, and the smoke test's `--export --codes` run covers it.

Bundles are one-folder builds, not single files: every trace starts the
executable again as a worker process, and a single-file build would unpack
itself for every trace.

## Controls

| Control | Effect |
|---|---|
| Color mode | Color, or black & white only |
| Layering | *Stacked* shapes overlap; *Cutout* shapes have holes cut for the shapes above them (color only) |
| Curve fitting | Spline (smooth curves), Polygon (straight lines) or Pixel (staircase outline) |
| Filter speckle | Drops patches smaller than this many pixels |
| Color precision | Bits per channel kept; more gives more colors (color only) |
| Gradient step | Color difference between layers; lower gives more layers (color only) |
| Corner threshold, Segment length, Splice threshold | Curve smoothing (spline only) |
| Path precision | Decimal places in coordinates; mainly changes file size |
| Trace resolution | Longest side traced. Larger images are downscaled before tracing; the SVG is still sized to the original. |

### Zooming the preview

- **−**, **+** and **Fit** in the preview header, or View menu: Ctrl/⌘ `+`,
  `−`, `0` (fit) and `1` (100%, one image pixel per screen point).
- Ctrl/⌘+scroll or trackpad pinch zooms around the pointer. Drag or scroll
  pans. Double-click toggles between fit and 100%.
- Zoom stays put when sliders retrace, so detail can be compared across
  settings. Opening another image returns to fit.
- While zooming, the previous render is shown stretched. It is then redrawn
  from the vector at the new scale about 0.1 s after the view settles, so
  edges stay sharp at up to 3200%.

### Barcodes and QR codes

**Detect Codes** finds barcodes and QR codes in the bitmap with zxing-cpp and
lists their decoded values; the list can be selected and copied. Each code is
then re-encoded from its value and drawn over the trace at the original's
position, angle, perspective and colors, so the saved SVG contains a code that
scans rather than a traced approximation. The codes stay over every retrace
until the button is turned off. From the command line:
`image-to-vector --export --codes photo.jpg photo.svg` prints the values and
overlays them.

- Every regenerated code is decoded again before it is used. One that does not
  decode to exactly the original bytes is listed as "left as traced" and not
  drawn. This happens with formats zxing-cpp cannot write, and it can happen
  with special encodings such as GS1.
- QR codes are regenerated with the original's version, error-correction level
  and mask, so they have the same modules. Codes from other encoders keep
  their value but can differ module by module.
- zxing-cpp's corners for a linear barcode cover only the rows it scanned. In
  tall images it skips rows (190 of 200 px of bar on a 4800 px canvas), and
  its ends were up to 1.4 px off at 2 px per module. So the bars' ends and
  height are measured from the image. Measured on EAN-13s at 0–15°, 2 and 4 px
  per module, 700 and 4800 px canvases: width within 1.0 px, height within
  1.0 %.
- Two measurement gotchas, found in turn. Requiring 90 % of a row to match the
  bar pattern stopped inside small rotated bars, which matched only 0.71–0.97,
  against at most 0.66 in the quiet zone and digits; the threshold is now 0.7.
  Growing the height from zxing-cpp's range overshot by up to 3.3 % when
  rotated, because that range, projected onto the bars, is already too tall
  and growing can't shrink it; it now grows from the middle row.
- zxing-cpp finds linear codes up to about 15° of rotation (measured: 15°
  found, 30° not). QR and Data Matrix codes are found at any angle and when
  inverted.

Controls that vtracer ignores in the current mode are greyed out. Which ones
those are was measured by tracing with each control at both ends and
comparing the output, not taken from documentation.

## Design notes

- Tracing runs in a separate process. vtracer holds Python's GIL for the whole
  trace (measured: the main thread got 1 of ~900 expected wake-ups during a
  9 s trace), so a worker thread would freeze the window. A process can also be
  killed when a slider moves mid-trace, so a stale result never overwrites a
  newer one.
- Save is disabled while a trace is running, so the saved file always matches
  the current settings.
- Images dragged from Messages (and other apps that keep files in protected
  folders such as `~/Library/Messages`) failed with "Operation not permitted".
  macOS attaches a one-time access grant to the dropped file's URL on the drag
  pasteboard; Qt reads only the path, so the grant was lost. On macOS the drop
  handler now takes the grant through PyObjC while the file is read. The grant
  applies only to the process that takes it, so the window reads the file once
  and sends the bytes to the trace worker instead of a path. Measured with a
  diagnostic drop: plain `open()` of the path was refused; after reading the
  URL through `NSPasteboard` the same `open()` succeeded.
- For such images, Save defaults to Pictures rather than the source folder
  inside `~/Library`.
- 16-bit TIFFs are rescaled to 8 bits before tracing. Pillow's own conversion
  clips at 255, which makes most 16-bit images load as solid white.
- Tracing time grows with image area and with detail: a 1500×1500 noise image
  took about 10 s. That is why Trace resolution defaults to 1024 px.

## Tests

```sh
uv run pytest
```

The tests run Qt headless (`QT_QPA_PLATFORM=offscreen`). They trace real
images, render the resulting SVG with Qt, and compare its colors with the
source bitmap.
