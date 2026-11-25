# Poster image and logo sources

The poster uses photographs and brand marks from existing public sources. No AI-generated icons or illustrations are used.

## Photographs

- `computer-lab.jpg` — Haseeb Modi, “Students working in school computer lab,” Unsplash: https://unsplash.com/photos/students-working-in-school-computer-lab-LKySOH0mr38
- `student-coding.jpg` — Hasnain Ayaz, “Person working on a laptop, coding and analyzing data,” Unsplash: https://unsplash.com/photos/person-working-on-a-laptop-coding-and-analyzing-data-Su1XYFlftXA
- `library-study.jpg` — Ashutosh Gupta, “Focused student studying at a library table with a laptop,” Unsplash: https://unsplash.com/photos/focused-student-studying-at-a-library-table-with-a-laptop-NASjMHJ9OhI
- `students-collaborating.jpg` — Vitaly Gariev, “Diverse group of students gathered around a laptop,” Unsplash: https://unsplash.com/photos/diverse-group-of-students-gathered-around-a-laptop-8gAbl776pc0
- `shared-laptops.jpg` — Raka Rahmadani, “Students working on laptops at a shared desk,” Unsplash: https://unsplash.com/photos/students-working-on-laptops-at-a-shared-desk-rh4xapY-haI
- `campus-graduates.jpg` — Olek Buzunov, “Students in graduation caps and gowns,” Unsplash: https://unsplash.com/photos/students-in-graduation-caps-and-gowns-STw47GkJivE

All six photographs are listed as free to use under the Unsplash License on their source pages.

## Brand marks

- `github-mark.png` — GitHub asset; use guidance: https://brand.github.com/foundations/logo
- `hackerrank-logo.png` — HackerRank brand asset: https://pages.hackerrank.com/blog/what-is-spring-boot-introduction
- `autocad.svg`, `linkedin.svg`, `google.svg` — Simple Icons brand symbols: https://simpleicons.org/
- MIT-WPU logo and paper texture come from the project's existing `static/` assets.

Logos are displayed as provided and remain the property of their respective owners.

## Current poster (A3 landscape redesign, 2026-11-25)

The poster no longer uses the photographs above; that section is kept only to credit the older iteration. Its imagery is now:

- `app/*.png` — high-resolution screenshots of the running Student Analytics app, recaptured at device scale factor 3 with synthetic demo data seeded by `scripts/seed_poster_demo.py`. No real student records appear. The poster labels them as sample data.
- `qr-live-site.svg` — generated with [segno](https://github.com/heuer/segno) by `scripts/generate_poster_qr.py`; it encodes `https://student-analytics-iota.vercel.app/` and was verified by decoding the rendered SVG with jsQR.
- `fonts/*.woff2` — Archivo, Archivo Black and IBM Plex Mono from Google Fonts (SIL Open Font License 1.1), self-hosted so printing does not depend on a live font CDN.

`scripts/export_poster_pdf.py` prints the page through Chromium (text stays vector) and then removes **every** stream filter with pikepdf — images, page content and font programs. The result is ~38 MB, one A3 landscape page (420.2 × 297 mm), with zero compressed streams and no lossy JPEG. Pass `--keep-compressed` for a ~5 MB Flate variant. Screenshots are pre-cropped to the meaningful region of each page and matched to the larger thumbnail boxes so they stay sharp and aligned; the poster's footer was removed on request to free space for them.

For printers that cannot take A3, the browser-generated 103 MB PDF can be rasterised losslessly to PNG with `pypdfium2` at 600 dpi and downsampled to A4 landscape: `Student-Analytics-Poster-A3-600dpi.png`, `Student-Analytics-Poster-A4-600dpi.png` and `Student-Analytics-Poster-A4-300dpi.png` in the workspace root. PNG keeps the image lossless (text becomes pixels, so it is no longer selectable); the A4 600 dpi file still carries ~424 dpi of the original A3 detail.

The same two 600 dpi PNGs are copied to `poster-a3.png` and `poster-a4.png` here and exposed as **Save PNG · A3 / A4** download links in the poster toolbar. `scripts/generate_poster_png.py --pdf <file>` regenerates both from a print-ready PDF after any design change.
