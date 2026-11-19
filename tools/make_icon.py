"""Draw the Alpaca Gateway's icon (assets/logo.svg's shapes) at every size it is needed:

	uv run --no-project --with pillow tools/make_icon.py

-> assets/alpacagw.ico (for an exe), assets/logo-256.png, and alpacagw/gateway/icon_data.py: the window icon's
pixels (RGBA, zlib + base64), so the running program needs neither an image file nor Pillow.
Drawn 4x larger, then scaled down (smooth edges). Run again after changing the logo.
"""
from __future__ import annotations

import base64
import zlib
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
BACKGROUND, BLUE, GREEN = "#0C0E15", "#3B6EF5", "#22AB94"
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
WINDOW_SIZES = [16, 32, 48, 64, 128]          # glfwSetWindowIcon: the system picks the best fit


def draw(size: int) -> Image.Image:
	"""The logo at ``size`` px: the SVG's 256-unit drawing, drawn 4x and scaled down."""
	big = size * 4
	s = big / 256
	img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
	d = ImageDraw.Draw(img)
	box = lambda x0, y0, x1, y1: (x0 * s, y0 * s, x1 * s, y1 * s)
	d.rounded_rectangle(box(0, 0, 256, 256), radius=52 * s, fill=BACKGROUND)            # the dark tile
	d.line([(40 * s, 128 * s), (216 * s, 128 * s)], fill=GREEN, width=round(28 * s))    # app - gateway - broker
	for cx in (40, 216):
		d.ellipse(box(cx - 30, 98, cx + 30, 158), fill=GREEN)
	d.rounded_rectangle(box(72, 30, 184, 226), radius=34 * s, fill=BLUE)                # the gateway: a 28 wide frame
	d.rounded_rectangle(box(100, 58, 156, 198), radius=6 * s, fill=BACKGROUND)
	d.rounded_rectangle(box(113, 100, 143, 156), radius=6 * s, fill=GREEN)               # its light
	return img.resize((size, size), Image.LANCZOS)


def main() -> None:
	assets = ROOT / "assets"
	images = {n: draw(n) for n in sorted(set(ICO_SIZES + WINDOW_SIZES))}
	images[256].save(assets / "alpacagw.ico", sizes=[(n, n) for n in ICO_SIZES],
		append_images=[images[n] for n in ICO_SIZES if n != 256])
	images[256].save(assets / "logo-256.png")
	lines = ['"""The Alpaca Gateway\'s window icon: (size, RGBA pixels zlib-compressed, base64), made by tools/make_icon.py',
		'from the logo (assets/logo.svg). Do not edit: run the tool."""', "", "ICONS = ["]
	for n in WINDOW_SIZES:
		data = base64.b64encode(zlib.compress(images[n].tobytes(), 9)).decode()
		chunks = [data[i:i + 100] for i in range(0, len(data), 100)]
		lines.append(f"\t({n}, (")
		lines += [f'\t\t"{c}"' for c in chunks]
		lines.append("\t)),")
	lines.append("]")
	(ROOT / "alpacagw" / "gateway" / "icon_data.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
	print("assets/alpacagw.ico, assets/logo-256.png, alpacagw/gateway/icon_data.py")


if __name__ == "__main__":
	main()
