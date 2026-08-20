"""Raw-source ingestion: PDF datasheet tables, web page HTML, OCR text.

Every parser produces a list of `Block`s (consecutive lines with a stable
line index) so citations can point at an exact span: start_line/end_line +
verbatim text. Optional libraries (pdfplumber, bs4) are used when installed;
stdlib fallbacks keep the demo dependency-free.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Block:
    line_start: int
    line_end: int
    text: str
    meta: str = ""   # e.g. "page 2 / table 1" for pdfplumber cells


def _lines_to_blocks(raw: str) -> list[Block]:
    blocks: list[Block] = []
    for i, line in enumerate(raw.splitlines(), start=1):
        line = line.rstrip()
        if line.strip():
            blocks.append(Block(i, i, line))
    return blocks


def parse_pdf_text(raw: str) -> list[Block]:
    """PDF datasheet given as already-extracted text (table dump)."""
    return _lines_to_blocks(raw)


def parse_pdf_file(path: str) -> list[Block]:
    """Real PDF: use pdfplumber tables when available (page/table/row meta)."""
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError(
            "pdfplumber not installed; provide the PDF as extracted text "
            "(.txt table dump) instead."
        )
    blocks: list[Block] = []
    idx = 0
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables() or []
            for tno, table in enumerate(tables, start=1):
                for rno, row in enumerate(table, start=1):
                    cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                    if not cells:
                        continue
                    idx += 1
                    blocks.append(
                        Block(idx, idx, "  |  ".join(cells), meta=f"p{pno}/t{tno}/r{rno}")
                    )
    return blocks


def parse_web_html(html_text: str) -> list[Block]:
    """Web page -> readable text, one row per element/cell.

    Uses BeautifulSoup when installed (keeps table structure readable),
    otherwise a stdlib tag-stripper that still splits table rows.
    """
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        lines: list[str] = []
        for el in soup.descendants:
            if el.name == "tr":
                cells = [c.get_text(" ", strip=True) for c in el.find_all(["td", "th"])]
                lines.append("  |  ".join(c for c in cells if c))
            elif getattr(el, "name", None) in ("p", "h1", "h2", "h3", "h4", "li"):
                txt = el.get_text(" ", strip=True)
                if txt:
                    lines.append(txt)
        raw = "\n".join(lines)
    except ImportError:
        text = re.sub(r"<(script|style|noscript)[\s\S]*?</\1>", " ", html_text, flags=re.I)
        text = re.sub(r"</(tr|td|th|p|div|li|h[1-6])>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = _html.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        raw = text
    return _lines_to_blocks(raw)


def parse_ocr_text(raw: str) -> list[Block]:
    """OCR transcript: one token/line per block."""
    return _lines_to_blocks(raw)


def read_source(src: dict) -> tuple[list[Block], str]:
    """Read + parse a manifest source. Returns (blocks, source_kind)."""
    kind = src["type"]
    p = Path(src["path"])
    if kind == "pdf" and p.suffix.lower() == ".pdf":
        return parse_pdf_file(str(p)), "pdf"
    raw = p.read_text(encoding="utf-8", errors="replace")
    if kind == "pdf":
        return parse_pdf_text(raw), "pdf"
    if kind == "web":
        return parse_web_html(raw), "web"
    return parse_ocr_text(raw), "ocr"


def line_numbered(blocks: list[Block]) -> str:
    """Render blocks as line-numbered text for the LLM prompt."""
    out = []
    for b in blocks:
        prefix = f"{b.meta}: " if b.meta else ""
        out.append(f"{b.line_start:>5}  {prefix}{b.text}")
    return "\n".join(out)
