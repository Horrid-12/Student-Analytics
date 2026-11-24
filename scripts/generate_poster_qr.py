"""Generate the QR code embedded in the admin A3 poster.

The poster ships the rendered SVG at ``static/poster-assets/qr-live-site.svg``
so the page never needs a runtime QR dependency. Re-run this script only when
the live URL changes::

    pip install segno
    python scripts/generate_poster_qr.py

The output uses the poster palette (navy modules on cream) and a 4-module
quiet zone, which keeps it scannable when printed on A3.
"""

from pathlib import Path

import segno

LIVE_URL = "https://student-analytics-iota.vercel.app/"
OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "static"
    / "poster-assets"
    / "qr-live-site.svg"
)


def main() -> None:
    qr = segno.make(LIVE_URL, error="m")
    qr.save(
        OUTPUT,
        kind="svg",
        scale=10,
        border=4,
        dark="#142235",
        light="#f5f1e9",
        xmldecl=False,
        svgclass=None,
        lineclass=None,
    )
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes) for {LIVE_URL}")


if __name__ == "__main__":
    main()
