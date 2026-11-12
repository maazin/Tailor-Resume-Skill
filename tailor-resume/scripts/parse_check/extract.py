"""ATS-style text extraction from a PDF, with layout data.

Modified from Resume-Matcher (https://github.com/srbhr/Resume-Matcher),
apps/backend/app/services/ats_parse/extract.py, Apache-2.0.
Changes:
  - DOCX path and the upload validators removed; input is a local PDF file.
  - A second extractor, ``pdftotext -layout`` from poppler, because ATS tools
    do not all use the same extractor. That pass has text only, no boxes.
  - Vertical rules (drawn table borders) and text inside figures are counted
    so the tables and text_boxes checks can run on a PDF.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pdfminer.high_level import extract_pages
from pdfminer.layout import (
    LAParams,
    LTContainer,
    LTCurve,
    LTFigure,
    LTImage,
    LTTextContainer,
)
from pdfminer.pdfpage import PDFPage

MAX_PARSE_CHECK_PAGES = 10
MAX_COUNTED_PAGES = 200
PDFTOTEXT_TIMEOUT_SECONDS = 30

# pdfminer warns per glyph on some fonts ("Could not get FontBBox").
logging.getLogger("pdfminer").setLevel(logging.ERROR)


class ExtractionError(RuntimeError):
    pass


@dataclass
class TextBox:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


@dataclass
class PageLayout:
    width: float
    height: float
    boxes: list[TextBox]
    images: int = 0

    @property
    def text(self) -> str:
        return "\n".join(box.text for box in self.boxes)


@dataclass
class ExtractedDocument:
    kind: str
    text: str
    extractor: str = "pdfminer"
    page_count: int | None = None
    truncated: bool = False
    pages: list[PageLayout] = field(default_factory=list)
    images: int = 0
    tables: int = 0
    textbox_text: str = ""
    has_layout: bool = True


def _count_images(item: LTContainer) -> int:
    count = 0
    for child in item:
        if isinstance(child, LTImage):
            count += 1
        elif isinstance(child, LTFigure):
            count += _count_images(child) or 1
    return count


def _text_boxes(item: LTContainer) -> list[TextBox]:
    """Text containers in layout order, including those inside form XObjects."""
    boxes: list[TextBox] = []
    for child in item:
        if isinstance(child, LTTextContainer):
            text = child.get_text().strip()
            if text:
                boxes.append(TextBox(child.x0, child.y0, child.x1, child.y1, text))
        elif isinstance(child, LTFigure):
            boxes.extend(_text_boxes(child))
    return boxes


def _figure_text(item: LTContainer, inside: bool = False) -> list[str]:
    out: list[str] = []
    for child in item:
        if isinstance(child, LTFigure):
            out.extend(_figure_text(child, True))
        elif inside and isinstance(child, LTTextContainer):
            text = child.get_text().strip()
            if text:
                out.append(text)
    return out


def _vertical_rules(item: LTContainer) -> int:
    """Thin, tall drawn lines: the sides of table cells."""
    count = 0
    for child in item:
        if isinstance(child, LTCurve):
            if (child.y1 - child.y0) > 8 and (child.x1 - child.x0) < 2:
                count += 1
        elif isinstance(child, LTFigure):
            count += _vertical_rules(child)
    return count


def extract_pdf(content: bytes) -> ExtractedDocument:
    """pdfminer default layout order, the reading simple ATS parsers use.

    ``all_texts`` lays out text drawn inside form XObjects too; without it that
    text stays as loose characters and never reaches the report.
    """
    page_count = sum(
        1 for _ in PDFPage.get_pages(io.BytesIO(content), maxpages=MAX_COUNTED_PAGES)
    )
    pages: list[PageLayout] = []
    rules = 0
    figure_text: list[str] = []
    for page in extract_pages(
        io.BytesIO(content),
        laparams=LAParams(all_texts=True),
        maxpages=MAX_PARSE_CHECK_PAGES,
    ):
        pages.append(
            PageLayout(page.width, page.height, _text_boxes(page), _count_images(page))
        )
        rules += _vertical_rules(page)
        figure_text.extend(_figure_text(page))
    text = "\n\n".join(page.text for page in pages)
    return ExtractedDocument(
        kind="pdf",
        extractor="pdfminer",
        text=text,
        page_count=page_count,
        truncated=page_count > MAX_PARSE_CHECK_PAGES,
        pages=pages,
        images=sum(page.images for page in pages),
        tables=1 if rules >= 2 else 0,
        textbox_text="\n".join(figure_text),
    )


def pdftotext_available() -> bool:
    return shutil.which("pdftotext") is not None


def extract_pdftotext(path: str | Path) -> ExtractedDocument:
    """Text as ``pdftotext -layout`` reads it. Text only: no boxes, no drawings."""
    if not pdftotext_available():
        raise ExtractionError(
            "pdftotext not found. Install poppler (poppler-utils) to run the second parse pass."
        )
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            timeout=PDFTOTEXT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError("pdftotext timed out.") from exc
    if result.returncode != 0:
        raise ExtractionError(
            "pdftotext failed: " + result.stderr.decode("utf-8", "replace").strip()
        )
    text = result.stdout.decode("utf-8", "replace")
    # pdftotext ends every page with a form feed.
    page_count = text.count("\f") or (1 if text.strip() else 0)
    return ExtractedDocument(
        kind="pdf",
        extractor="pdftotext",
        text=text.replace("\f", "\n").strip(),
        page_count=page_count,
        has_layout=False,
    )


def extract_file(path: str | Path, extractor: str = "pdfminer") -> ExtractedDocument:
    path = Path(path)
    if not path.is_file():
        raise ExtractionError(f"No such PDF: {path}")
    if extractor == "pdftotext":
        return extract_pdftotext(path)
    return extract_pdf(path.read_bytes())
