"""Regenerate the PNG downloads offered on the admin poster page.

The page's "Save PNG · A3 / A4" buttons link to lossless PNGs rendered from a
print-ready PDF. Re-run this after the poster design changes::

    pip install pypdfium2 pillow
    python scripts/generate_poster_png.py --pdf "path/to/poster.pdf"

The A4 file is a high-quality downsample of the A3 raster, so a home A4 printer
gets the full design without needing A3 paper.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

ASSETS = Path(__file__).resolve().parent.parent / "static" / "poster-assets"
DPI = 600


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True, help="A3 landscape print-ready PDF")
    ap.add_argument("--a3", default=str(ASSETS / "poster-a3.png"))
    ap.add_argument("--a4", default=str(ASSETS / "poster-a4.png"))
    args = ap.parse_args()

    raster = pdfium.PdfDocument(args.pdf)[0].render(scale=DPI / 72).to_pil().convert("RGB")
    a3 = Path(args.a3).expanduser().resolve()
    a4 = Path(args.a4).expanduser().resolve()
    a3.parent.mkdir(parents=True, exist_ok=True)

    raster.save(a3, "PNG", optimize=True)
    print(f"A3 {raster.size}: {a3} ({a3.stat().st_size:,} bytes)")

    # A4 landscape at the same DPI: 297 x 210 mm.
    a4_raster = raster.resize((7016, 4961), Image.LANCZOS)
    a4_raster.save(a4, "PNG", optimize=True)
    print(f"A4 {a4_raster.size}: {a4} ({a4.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
