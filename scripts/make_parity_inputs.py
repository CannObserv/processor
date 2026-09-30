"""Write the synthetic parity-corpus inputs to tests/fixtures/parity/inputs/.

Run once; the outputs are committed, and the goldens are derived from the committed
bytes (scripts/gen_parity_goldens.py). Regenerating changes the xlsx bytes (its zip
carries timestamps), so regenerate the goldens with it.

    uv run python scripts/make_parity_inputs.py
"""

import io
from pathlib import Path

from openpyxl import Workbook
from pypdf import PdfWriter

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "parity" / "inputs"

HTML = """<!doctype html>
<html><head><title>Committee agenda</title><script>var x = 1;</script></head>
<body>
<nav><a href="/">Home</a> | <a href="/about">About</a></nav>
<div id="main">
  <h1>Health &amp; Long-Term Care Committee</h1>
  <p>Work session on cannabis retail licensing, 1:30 PM.</p>
  <ul><li>HB 1234 — public hearing</li><li>SB 5678 — executive session</li></ul>
</div>
<div id="side"><p>Sidebar text that a CSS spec excludes.</p></div>
<footer>© Legislature</footer>
</body></html>
"""

XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Rule notice</title></head>
<body><div id="main"><p>WAC 314-55 proposed amendments.</p><p>Comments due 2026-10-15.</p></div>
</body></html>
"""

LATIN1_HTML = (
    '<html><head><meta charset="iso-8859-1"><title>Caf\xe9</title></head>'
    "<body><p>R\xe9sum\xe9 of the se\xf1or's testimony.</p></body></html>"
).encode("iso-8859-1")

CSV = """license,name,city,status
412345,Green Leaf LLC,Spokane,ACTIVE
412346,"Evergreen, Inc.",Tacoma,PENDING
412347,Cascade Cannabis,Seattle,ACTIVE
"""

JSON = b'{"items": [{"id": 1, "title": "Agenda item one"}, {"id": 2, "title": "Item two"}]}'


def _pdf(pages: list[str]) -> bytes:
    """A minimal, valid PDF with one Helvetica text line per page (pypdf reads it)."""
    objs: list[bytes] = []
    n_pages = len(pages)
    font_id = 3 + 2 * n_pages
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % num + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    trailer = b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
    out.write(trailer % (len(objs) + 1, xref))
    return out.getvalue()


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Licenses"
    for row in [
        ["license", "name", "city", "status"],
        [412345, "Green Leaf LLC", "Spokane", "ACTIVE"],
        [412346, "Evergreen, Inc.", "Tacoma", "PENDING"],
    ]:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main() -> None:
    """Write every synthetic input under ``OUT``."""
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        "agenda.html": HTML.encode(),
        "notice.xhtml": XHTML.encode(),
        "latin1.html": LATIN1_HTML,
        "licenses.csv": CSV.encode(),
        "items.json": JSON,
        "minutes.pdf": _pdf(["Meeting called to order at 9:00 AM.", "Adjourned at 10:15 AM."]),
        "scanned.pdf": _blank_pdf(),
        "licenses.xlsx": _xlsx(),
    }
    for name, data in files.items():
        (OUT / name).write_bytes(data)
        print(f"{name}: {len(data)} bytes")


if __name__ == "__main__":
    main()
