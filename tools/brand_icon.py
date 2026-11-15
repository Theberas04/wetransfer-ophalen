"""Maakt het app-pictogram (.ico) en de logo-PNG's uit één bronbestand.

Eén bron voor Windows en de website, zodat de twee niet uit elkaar kunnen lopen.
Gebruik:  python tools/brand_icon.py [--check]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "branding" / "werdens-media-logo.png"
ICO = ROOT / "branding" / "app.ico"
UI_LOGO = ROOT / "ui" / "images" / "werdens-media-logo.png"
PREVIEW = ROOT / "branding" / "app-icon-256.png"

SIZES = (16, 24, 32, 48, 64, 128, 256)
MARK_WIDTH_RATIO = 0.68
PLATE_COLOR = (255, 255, 255, 255)
PLATE_BORDER = (227, 234, 244, 255)


def _plate(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = max(2, int(size * 0.22))
    draw.rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=radius,
        fill=PLATE_COLOR,
        outline=PLATE_BORDER,
        width=max(1, size // 64),
    )
    return image


def compose(size: int, source: Image.Image) -> Image.Image:
    """Zet het merkteken gecentreerd op een witte afgeronde plaat."""

    canvas = _plate(size)
    target_width = max(4, int(size * MARK_WIDTH_RATIO))
    scale = target_width / source.width
    mark = source.resize(
        (target_width, max(1, int(round(source.height * scale)))), Image.Resampling.LANCZOS
    )
    canvas.alpha_composite(mark, ((size - mark.width) // 2, (size - mark.height) // 2))
    return canvas


def build(check_only: bool = False) -> int:
    if not SOURCE.exists():
        print(f"Bronbestand ontbreekt: {SOURCE}")
        return 1
    source = Image.open(SOURCE).convert("RGBA")
    if source.getchannel("A").getextrema()[0] == 255:
        print("Let op: het bronlogo heeft geen transparantie; het pictogram wordt een blok.")

    frames = [compose(size, source) for size in SIZES]
    problems = check_frames(frames, source)

    if not check_only:
        ICO.parent.mkdir(parents=True, exist_ok=True)
        frames[-1].save(ICO, format="ICO", sizes=[(size, size) for size in SIZES])
        frames[-1].save(PREVIEW, format="PNG")
        UI_LOGO.parent.mkdir(parents=True, exist_ok=True)
        if not UI_LOGO.exists() or ImageChops.difference(
            Image.open(UI_LOGO).convert("RGBA"), source
        ).getbbox() is not None:
            source.save(UI_LOGO, format="PNG")
        print(f"Geschreven: {ICO.relative_to(ROOT)}, {PREVIEW.relative_to(ROOT)}, {UI_LOGO.relative_to(ROOT)}")

    for problem in problems:
        print(f"FOUT: {problem}")
    return 1 if problems else 0


def check_frames(frames: list[Image.Image], source: Image.Image) -> list[str]:
    """Controleer het pictogram op getallen, niet op gevoel."""

    problems: list[str] = []
    for frame in frames:
        size = frame.width
        # De hoeken horen transparant te zijn (afgeronde plaat) en het midden wit.
        if frame.getpixel((0, 0))[3] != 0:
            problems.append(f"{size}px: de hoek is niet transparant, de plaat is niet afgerond")
        if frame.getpixel((size // 2, max(1, size // 12)))[:3] != PLATE_COLOR[:3]:
            problems.append(f"{size}px: boven het merkteken hoort de witte plaat te zitten")
        # Het merkteken moet binnen de veilige marge blijven en niet tegen de rand plakken.
        # Let op: getbbox() kijkt bij een RGBA-afbeelding alleen naar het alfakanaal, en dat
        # is in beide afbeeldingen 255. Daarom vergelijken we de kleurkanalen.
        bbox = ImageChops.difference(frame, _plate(size)).convert("RGB").getbbox()
        if bbox is None:
            problems.append(f"{size}px: geen merkteken zichtbaar")
            continue
        left, top, right, bottom = bbox
        margin = min(left, top, size - right, size - bottom)
        if margin < max(1, int(size * 0.10)):
            problems.append(f"{size}px: merkteken komt te dicht bij de rand (marge {margin}px)")
        width_ratio = (right - left) / size
        if width_ratio < 0.55:
            problems.append(f"{size}px: merkteken is te klein ({width_ratio:.0%} van de breedte)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pictogram en logo's maken")
    parser.add_argument("--check", action="store_true", help="alleen controleren, niets schrijven")
    args = parser.parse_args(argv)
    return build(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
