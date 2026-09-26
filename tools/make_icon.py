"""Generate a professional black + gold app icon (mic + waveform motif).

Run:  python tools/make_icon.py
Outputs: assets/app.ico (multi-size), assets/app_256.png
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)

GOLD = (232, 163, 61, 255)
GOLD_LIGHT = (245, 194, 96, 255)
BLACK = (8, 8, 10, 255)
SOFT = (24, 23, 20, 255)


def _rounded(draw: ImageDraw.ImageDraw, box, radius: float) -> None:
    draw.rounded_rectangle(box, radius=radius)


def draw_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size
    pad = s * 0.06

    bg = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg)
    bd.rounded_rectangle(
        [pad, pad, s - pad, s - pad], radius=s * 0.22, fill=BLACK,
        outline=GOLD, width=max(2, round(s * 0.03)),
    )
    bg = bg.filter(ImageFilter.GaussianBlur(0.6))
    img.alpha_composite(bg)

    overlay = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    cx = s / 2
    mic_w = s * 0.20
    mic_h = s * 0.34
    lt = (cx - mic_w / 2, s * 0.22)
    lb = (cx + mic_w / 2, s * 0.22 + mic_h - mic_w)
    od.rounded_rectangle([*lt, *lb], radius=mic_w * 0.5, fill=GOLD)

    head_w = s * 0.30
    head_x0 = cx - head_w / 2
    od.pieslice(
        [head_x0, s * 0.12, cx + head_w / 2, s * 0.12 + head_w],
        0, 180, fill=GOLD,
    )

    stem_w = max(3, round(s * 0.028))
    od.rectangle([cx - stem_w / 2, s * 0.54, cx + stem_w / 2, s * 0.62], fill=GOLD)
    od.pieslice(
        [cx - s * 0.09, s * 0.60, cx + s * 0.09, s * 0.66],
        0, 180, fill=GOLD,
    )

    half = s * 0.42
    n = int(size * 0.42)
    base_y = s * 0.78
    for i in range(n):
        t = i / (n - 1)
        x = cx - half + (2 * half) * t
        amp = math.sin(t * math.pi) ** 1.6
        bar_h = s * (0.06 + 0.16 * amp)
        w = max(1, round(s * 0.008))
        grad = [c + round((g - c) * amp * 0.5) for c, g in zip(GOLD, GOLD_LIGHT)]
        od.rounded_rectangle(
            [x - w / 2, base_y - bar_h / 2, x + w / 2, base_y + bar_h / 2],
            radius=w / 2,
            fill=tuple(grad),
        )

    img.alpha_composite(overlay)
    img = Image.alpha_composite(img, Image.new("RGBA", (s, s), (0, 0, 0, 0)))
    return img


def main() -> None:
    base = draw_icon(256)
    base.save(ASSETS / "app_256.png")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base.save(
        ASSETS / "app.ico",
        sizes=[(x, x) for x, _ in sizes],
        format="ICO",
    )
    print("icon written:", ASSETS / "app.ico")


if __name__ == "__main__":
    main()