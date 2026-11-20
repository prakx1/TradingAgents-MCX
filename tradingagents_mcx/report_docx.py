"""Word report export — parity with the original's python-docx feature.

Converts the saved markdown report to .docx. Optional dependency: python-docx.
"""
from __future__ import annotations

from pathlib import Path


def md_to_docx(md_path: str, docx_path: str | None = None) -> str | None:
    try:
        from docx import Document
        from docx.shared import Pt
    except ImportError:
        return None
    md = Path(md_path).read_text()
    doc = Document()
    doc.styles["Normal"].font.size = Pt(10.5)
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line or set(line) == {"-"}:
            continue
        if line.startswith("### "):
            doc.add_heading(line[4:], level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.startswith("- "):
            doc.add_paragraph(line[2:], style="List Bullet")
        else:
            doc.add_paragraph(line.replace("**", ""))
    out = docx_path or str(Path(md_path).with_suffix(".docx"))
    doc.save(out)
    return out


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.md"
        p.write_text("# Title\n\n## Section\n\ntext **bold**\n\n- item\n")
        out = md_to_docx(str(p))
        assert out is None or Path(out).exists()
        print("docx export OK" if out else "python-docx not installed (optional) — skipped")
