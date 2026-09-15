"""Build a standalone bundle for the current OS with PyInstaller, then smoke-test it.

PyInstaller cannot cross-compile, so run this on each target OS:

    uv run python packaging/build.py

Output: dist/Image to Vector.app (macOS) or dist/Image to Vector/ (Windows,
Linux), plus a zip of it in dist/.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import zxingcpp
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
NAME = "Image to Vector"
SYSTEM = platform.system()


def build() -> Path:
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean",
            "--windowed",
            # onedir, not onefile: every trace starts the executable again as a
            # worker, and a onefile build would unpack itself on each trace.
            "--onedir",
            # zxing-cpp's native code imports json when Barcode.extra is read,
            # which PyInstaller's import scan can't see; without it the bundle
            # failed with "Invalid JSON in Barcode::extra()".
            "--hidden-import", "json",
            "--name", NAME,
            "--distpath", str(DIST),
            "--workpath", str(BUILD),
            "--specpath", str(BUILD),
            str(ROOT / "image_to_vector" / "__main__.py"),
        ],
        check=True,
    )
    if SYSTEM == "Darwin":
        return DIST / f"{NAME}.app"
    return DIST / NAME


def executable(bundle: Path) -> Path:
    if SYSTEM == "Darwin":
        return bundle / "Contents" / "MacOS" / NAME
    if SYSTEM == "Windows":
        return bundle / f"{NAME}.exe"
    return bundle / NAME


def smoke_test(exe: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "shapes.png"
        im = Image.new("RGB", (420, 200), "white")
        d = ImageDraw.Draw(im)
        d.ellipse((20, 20, 180, 180), fill=(200, 30, 30))
        d.rectangle((60, 120, 140, 190), fill=(40, 160, 60))
        qr = zxingcpp.create_barcode("smoke-test", zxingcpp.BarcodeFormat.QRCode)
        view = memoryview(qr.to_image(scale=6, add_quiet_zones=True))
        im.paste(Image.frombytes("L", (view.shape[1], view.shape[0]), bytes(view)), (220, 20))
        im.save(image)

        # 1. Headless export: proves the bundle holds vtracer, Pillow and
        #    zxing-cpp, and that the frozen executable can start its own
        #    trace workers.
        svg_path = Path(tmp) / "out.svg"
        started = time.monotonic()
        result = subprocess.run(
            [str(exe), "--export", "--codes", str(image), str(svg_path)], capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            sys.exit(f"export failed ({result.returncode}): {result.stderr}")
        text = svg_path.read_text()
        root = ET.parse(svg_path).getroot()
        fills = set(re.findall(r'fill="#([0-9A-F]{6})"', text))
        if root.tag != "{http://www.w3.org/2000/svg}svg" or len(fills) < 3:
            sys.exit(f"export produced a bad SVG: root {root.tag}, fills {fills}")
        if 'data-format="QRCode" data-value="smoke-test"' not in text:
            sys.exit("export --codes did not overlay the QR code")
        print(f"smoke: export ok in {time.monotonic() - started:.1f} s, fills {sorted(fills)}, QR code overlaid")

        # 2. GUI start-up, offscreen so it also runs on headless CI: proves the
        #    Qt widgets, SVG module and platform plugins are bundled.
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        proc = subprocess.Popen([str(exe), str(image)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(8)
        if proc.poll() is not None:
            sys.exit(f"GUI exited early ({proc.returncode}): {proc.stdout.read()}")
        proc.terminate()
        proc.wait(timeout=10)
        print("smoke: GUI stayed up for 8 s")


def archive(bundle: Path) -> Path:
    arch = platform.machine().lower()
    target = DIST / f"Image-to-Vector-{SYSTEM.lower()}-{arch}.zip"
    target.unlink(missing_ok=True)
    if SYSTEM == "Darwin":
        # ditto keeps the framework symlinks and code signatures a plain zip would break.
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(bundle), str(target)], check=True)
    else:
        shutil.make_archive(str(target.with_suffix("")), "zip", DIST, bundle.name)
    return target


def size_mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 1e6
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink()) / 1e6


if __name__ == "__main__":
    bundle = build()
    smoke_test(executable(bundle))
    zipped = archive(bundle)
    print(f"bundle:  {bundle} ({size_mb(bundle):.0f} MB)")
    print(f"archive: {zipped} ({size_mb(zipped):.0f} MB)")
