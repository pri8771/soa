"""Author the AIO-002 digital-PDF corpus (raw, uncompressed, byte-
deterministic PDFs) and print how pdfium classifies each. Re-run with
``uv run python apps/worker/tests/fixtures/pdfs/generate_corpus.py`` from
the repo root if the corpus ever needs to change."""

from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as raw

OUT = Path(__file__).parent


def build_pdf(objects: list[bytes], trailer_extra: bytes = b"") -> bytes:
    """Assemble numbered objects (1-based) into a classic xref PDF."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R %s>>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        trailer_extra,
        xref_at,
    )
    return bytes(out)


def content_obj(stream: bytes) -> bytes:
    return b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)


# --- digital-po.pdf: two pages of real text at known positions -------------
PAGE1 = (
    b"BT /F1 18 Tf 72 720 Td (PURCHASE ORDER PO-4711) Tj ET\n"
    b"BT /F1 12 Tf 72 680 Td (Buyer: Acme GmbH) Tj ET\n"
    b"BT /F1 12 Tf 72 660 Td (Currency: EUR) Tj ET\n"
    b"BT /F1 12 Tf 400 680 Td (Order date: 2026-07-01) Tj ET\n"
)
PAGE2 = (
    b"BT /F1 12 Tf 72 720 Td (Line 1: WIDGET-9 qty 5 unit 12.50) Tj ET\n"
    b"BT /F1 12 Tf 72 700 Td (Total: 62.50) Tj ET\n"
)
digital = build_pdf(
    [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R"
        b" /Resources << /Font << /F1 7 0 R >> >> >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 6 0 R"
        b" /Resources << /Font << /F1 7 0 R >> >> >>",
        content_obj(PAGE1),
        content_obj(PAGE2),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
)
(OUT / "digital-po.pdf").write_bytes(digital)

# --- blank-page.pdf: a valid PDF with no text at all -----------------------
blank = build_pdf(
    [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        content_obj(b"0 0 m"),
    ]
)
(OUT / "blank-page.pdf").write_bytes(blank)

# --- encrypted.pdf: standard security handler, non-empty user password -----
# O/U values are placeholders; pdfium fails empty-password auth and reports
# FPDF_ERR_PASSWORD, which is all the adapter needs to classify it.
encrypted = build_pdf(
    [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        content_obj(b"BT /F1 12 Tf 72 720 Td (secret) Tj ET"),
        b"<< /Filter /Standard /V 1 /R 2 /P -44 "
        b"/O <0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef> "
        b"/U <fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210> >>",
    ],
    trailer_extra=b"/Encrypt 5 0 R /ID [<0102030405060708090a0b0c0d0e0f10> "
    b"<0102030405060708090a0b0c0d0e0f10>] ",
)
(OUT / "encrypted.pdf").write_bytes(encrypted)

# --- corrupt.pdf: claims to be a PDF, is not one ---------------------------
(OUT / "corrupt.pdf").write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog truncated garbage")

# --- missing-font.pdf: a Type0/Identity-H font with no ToUnicode map -------
# pdfium finds character cells but their CIDs map to no unicode: the
# "extracted text" is raw control codes — the classic unextractable-font
# document the adapter must classify as fonts_unmappable.
missing = build_pdf(
    [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        content_obj(b"BT /F1 24 Tf 72 700 Td <00040005000600070008000B000E000F> Tj ET"),
        b"<< /Type /Font /Subtype /Type0 /BaseFont /NoSuchFont /Encoding /Identity-H "
        b"/DescendantFonts [6 0 R] >>",
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /NoSuchFont "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        b"/FontDescriptor 7 0 R /DW 600 >>",
        b"<< /Type /FontDescriptor /FontName /NoSuchFont /Flags 4 /FontBBox [0 0 600 800] "
        b"/ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 >>",
    ]
)
(OUT / "missing-font.pdf").write_bytes(missing)

# --- verify with pdfium -----------------------------------------------------
for name in (
    "digital-po.pdf",
    "blank-page.pdf",
    "encrypted.pdf",
    "corrupt.pdf",
    "missing-font.pdf",
):
    path = OUT / name
    try:
        doc = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as err:
        print(f"{name}: OPEN FAILED err_code={raw.FPDF_GetLastError()} ({err})")
        continue
    print(f"{name}: {len(doc)} page(s)")
    for i, page in enumerate(doc):
        tp = page.get_textpage()
        n = tp.count_chars()
        text = tp.get_text_range()
        rects = tp.count_rects() if n else 0
        print(f"  page {i + 1}: chars={n} rects={rects} text={text[:60]!r}")
        if rects:
            print(f"    first rect={tp.get_rect(0)} size_pt={page.get_size()}")
