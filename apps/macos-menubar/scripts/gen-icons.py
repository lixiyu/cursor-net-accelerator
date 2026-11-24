#!/usr/bin/env python3
"""用 CoreGraphics 主图 + Lanczos 派生 iconset / icns / 菜单栏模板。"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "Resources"
ICONSET = RES / "AppIcon.iconset"
SWIFT = Path(__file__).resolve().parent / "gen-app-icon.swift"


def render_master() -> Image.Image:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "master.png"
        subprocess.run(["swift", str(SWIFT), str(out)], check=True)
        return Image.open(out).convert("RGBA").copy()


def render_menubar(size: int, *, supersample: int = 8) -> Image.Image:
    """菜单栏 template：黑图形，系统着色。"""
    # 以 18pt 画布为设计基准：外圈 15pt、线宽 1.9pt、闪电 6.8pt。
    # 8x 超采样后再缩小，保证 1x/2x/3x 都有干净的抗锯齿边缘。
    s = size * supersample
    cx = cy = s / 2.0
    r = s * (7.5 / 18.0)
    stroke = s * (1.9 / 18.0)

    ring_a = Image.new("L", (s, s), 0)
    ImageDraw.Draw(ring_a).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    hole = r - stroke
    ImageDraw.Draw(ring_a).ellipse(
        (cx - hole, cy - hole, cx + hole, cy + hole), fill=0
    )
    ring = Image.merge(
        "RGBA",
        (
            Image.new("L", (s, s), 0),
            Image.new("L", (s, s), 0),
            Image.new("L", (s, s), 0),
            ring_a,
        ),
    )
    pts = [
        (-0.16, -0.58),
        (0.30, -0.58),
        (0.04, -0.06),
        (0.40, -0.06),
        (-0.14, 0.66),
        (0.10, 0.06),
        (-0.34, 0.06),
        (0.00, -0.26),
        (-0.40, -0.26),
    ]
    br = s * (6.8 / 18.0)
    poly = [(cx + x * br, cy + y * br) for x, y in pts]
    bolt = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(bolt).polygon(poly, fill=(0, 0, 0, 255))
    img = Image.alpha_composite(ring, bolt)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def write_iconset(master: Image.Image) -> None:
    ICONSET.mkdir(parents=True, exist_ok=True)
    mapping = {
        "icon_16x16.png": 16,
        "diana.k@example.org": 32,
        "icon_32x32.png": 32,
        "ivan.p@example.net": 64,
        "icon_128x128.png": 128,
        "wendy.h@example.net": 256,
        "icon_256x256.png": 256,
        "wendy.h@example.net": 512,
        "icon_512x512.png": 512,
        "walt.e@example.net": 1024,
    }
    for name, px in mapping.items():
        master.resize((px, px), Image.Resampling.LANCZOS).save(ICONSET / name, "PNG")


def write_menubar() -> None:
    for scale in (1, 2, 3):
        name = "MenuBarIcon.png" if scale == 1 else f"MenuBarIcon@{scale}x.png"
        render_menubar(18 * scale).save(RES / name)


def main() -> None:
    master = render_master()
    write_iconset(master)
    master.resize((256, 256), Image.Resampling.LANCZOS).save(RES / "AppIconPreview.png")
    icns = RES / "AppIcon.icns"
    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET), "-o", str(icns)],
        check=True,
    )
    write_menubar()
    print("ok", icns, "menu bar icons")


if __name__ == "__main__":
    if "--menubar-only" in sys.argv:
        write_menubar()
    else:
        main()
