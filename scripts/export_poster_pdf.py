"""Export the A3 LANDSCAPE poster as a print-ready, uncompressed PDF.

The browser's native Print / Save PDF already keeps text as vectors and images
as lossless Flate streams. This exporter goes one step further: it decodes the
large image streams plus page content and font programs and writes them raw,
so the final file is ~33 MB with **zero compressed streams**, no lossy JPEG
anywhere, and selectable, infinitely sharp text.

Dev-only requirements (not part of the runtime stack)::

    pip install playwright pikepdf
    playwright install chromium

Example::

    python scripts/export_poster_pdf.py \
        --url http://127.0.0.1:8011 \
        --email admin@mitwpu.edu.in --password '...' \
        --out ../Student-Analytics-Poster-A3-Landscape.pdf
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pikepdf
from playwright.async_api import async_playwright


async def print_vector_pdf(url: str, email: str, password: str, pdf: Path) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1600, "height": 1200})
        page = await ctx.new_page()
        await page.goto(f"{url.rstrip('/')}/login")
        await page.fill("#email", email)
        await page.fill("#password", password)
        await page.click('button[type="submit"]')
        await page.goto(f"{url.rstrip('/')}/admin/poster?clean=1", wait_until="networkidle")
        await page.evaluate("document.fonts.ready")
        await page.wait_for_timeout(300)
        await page.emulate_media(media="print")
        await page.pdf(
            path=str(pdf),
            format="A3",
            landscape=True,
            print_background=True,
            prefer_css_page_size=True,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        await browser.close()


def uncompress_all(src: Path, dst: Path) -> tuple[int, int, int]:
    """Remove every stream filter: images, page content and font programs."""
    with pikepdf.open(src) as pdf:
        pages = len(pdf.pages)
        count = 0
        images = 0
        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Stream) or obj.get("/Filter") is None:
                continue
            try:
                raw = obj.read_bytes()  # decoded bytes, no longer compressed
            except Exception:
                continue
            obj.write(raw)
            for key in ("/Filter", "/DecodeParms"):
                if key in obj:
                    del obj[key]
            count += 1
            if obj.get("/Subtype") == pikepdf.Name.Image:
                images += 1
        pdf.save(
            dst,
            compress_streams=False,
            object_stream_mode=pikepdf.ObjectStreamMode.disable,
        )
    return pages, images, count


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.getenv("POSTER_URL", "http://127.0.0.1:8011"))
    ap.add_argument("--email", default=os.getenv("POSTER_EMAIL", "admin@mitwpu.edu.in"))
    ap.add_argument("--password", default=os.getenv("POSTER_PASSWORD", ""))
    ap.add_argument("--out", default="../Student-Analytics-Poster-A3-Landscape.pdf")
    ap.add_argument("--keep-compressed", action="store_true",
                    help="skip the raw-image expansion (produces a ~5 MB lossless PDF)")
    args = ap.parse_args()

    if not args.password:
        ap.error("pass --password or set POSTER_PASSWORD")

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory() as tmp:
        vector = Path(tmp) / "vector.pdf"
        asyncio.run(print_vector_pdf(args.url, args.email, args.password, vector))
        print(f"browser vector PDF: {vector.stat().st_size:,} bytes")
        if args.keep_compressed:
            out.write_bytes(vector.read_bytes())
            with pikepdf.open(vector) as chk:
                pages, images, streams = len(chk.pages), 0, 0
        else:
            pages, images, streams = uncompress_all(vector, out)
    with pikepdf.open(out) as chk:
        box = chk.pages[0].MediaBox
        width = float(box[2]) * 25.4 / 72
        height = float(box[3]) * 25.4 / 72
        filters = sum(1 for obj in chk.objects if isinstance(obj, pikepdf.Stream) and obj.get("/Filter") is not None)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    print(f"pages: {pages} · size: {width:.2f} × {height:.2f} mm · raw streams: {streams} (images: {images}) · remaining filters: {filters}")


if __name__ == "__main__":
    main()
