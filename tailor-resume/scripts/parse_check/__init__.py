"""Deterministic ATS parse check: extraction, layout and content checks, and a round trip.

Modified from Resume-Matcher (https://github.com/srbhr/Resume-Matcher),
apps/backend/app/services/ats_parse/__init__.py, Apache-2.0.
Changes:
  - Runs on a local PDF file, synchronously, once per extractor (pdfminer and
    pdftotext) instead of on an uploaded or server-rendered document.
  - ``verdict`` adds the pass rule this skill uses before delivering a PDF:
    full extractability, one page, no multi-column or table warning, contact
    info found, and the resume's own text surviving the round trip.

CLI:
  python scripts/parse_check resume.pdf [--resume resume.json|resume.tex]
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from parse_check.checks import run_checks, score
from parse_check.extract import (
    ExtractedDocument,
    ExtractionError,
    extract_file,
    pdftotext_available,
)
from parse_check.models import ParseCheckReport
from parse_check.roundtrip import compute_roundtrip

PREVIEW_CHARACTERS = 2_000
EXTRACTORS = ("pdfminer", "pdftotext")
MIN_CONTENT_RECALL = 0.95


def build_report(
    doc: ExtractedDocument,
    resume_data: dict[str, Any] | None = None,
    max_pages: int = 1,
) -> ParseCheckReport:
    checks = run_checks(doc, {"locale": "en", "max_pages": max_pages})
    status = {check.id: check.status for check in checks}
    if status["text_layer"] == "fail":
        extractability = "none"
    elif (
        status["garbled_glyphs"] == "fail"
        or status.get("text_coverage", "pass") != "pass"
        or doc.truncated
    ):
        extractability = "partial"
    else:
        extractability = "full"
    return ParseCheckReport(
        extractor=doc.extractor,
        extractability=extractability,
        overall_score=score(checks, {"extraction", "layout"}),
        content_score=score(checks, {"content"}),
        page_count=doc.page_count,
        checks=checks,
        roundtrip=compute_roundtrip(resume_data, doc.text) if resume_data else None,
        extracted_text_preview=doc.text[:PREVIEW_CHARACTERS],
    )


def verdict(report: ParseCheckReport, max_pages: int = 1) -> dict[str, Any]:
    """Pass or fail for one extractor, with the reasons."""
    problems: list[str] = []
    notes: list[str] = []
    if report.extractability != "full":
        problems.append(f"extractability is {report.extractability}, not full")
    if report.page_count != max_pages:
        problems.append(f"{report.page_count} pages, expected {max_pages}")
    for check_id, label in (("multi_column", "multi-column layout"), ("tables", "table")):
        check = report.check(check_id)
        if check and check.status != "pass":
            problems.append(f"{label} detected")
    contact = report.check("contact_info")
    if contact:
        if not contact.params.get("email"):
            problems.append("email not found in extracted text")
        if not contact.params.get("phone"):
            problems.append("phone not found in extracted text")
        if contact.status == "warn" and contact.params.get("email") and contact.params.get("phone"):
            notes.append("contact info is not near the top of the extracted text")
    headings = report.check("section_headings")
    if headings and headings.status != "pass":
        problems.append("missing section headings: " + ", ".join(headings.params.get("missing", [])))
    if report.roundtrip is not None:
        if report.roundtrip.content_recall < MIN_CONTENT_RECALL:
            lost = [f.field for f in report.roundtrip.fields if f.status != "found"][:8]
            problems.append(
                f"only {report.roundtrip.content_recall:.0%} of the resume text survived extraction"
                + (f" (not found: {', '.join(lost)})" if lost else "")
            )
        if report.roundtrip.order_fidelity < 0.98:
            notes.append(
                f"reading order differs from the page order (order fidelity {report.roundtrip.order_fidelity:.2f})"
            )
    for check in report.checks:
        if check.status != "pass" and check.id in ("garbled_glyphs", "images", "text_boxes", "dates"):
            notes.append(f"{check.id}: {check.status}")
    return {
        "extractor": report.extractor,
        "passed": not problems,
        "problems": problems,
        "notes": notes,
        "extractability": report.extractability,
        "page_count": report.page_count,
        "content_recall": report.roundtrip.content_recall if report.roundtrip else None,
        "order_fidelity": report.roundtrip.order_fidelity if report.roundtrip else None,
    }


def parse_check_pdf(
    pdf_path: str | Path,
    resume_data: dict[str, Any] | None = None,
    max_pages: int = 1,
    extractors: tuple[str, ...] = EXTRACTORS,
) -> dict[str, Any]:
    """Run the parse check once per extractor and combine the verdicts.

    ``passed`` is true only when every extractor passes. A missing pdftotext
    binary is reported as a problem, never skipped silently.
    """
    passes: list[dict[str, Any]] = []
    for extractor in extractors:
        try:
            doc = extract_file(pdf_path, extractor)
        except ExtractionError as exc:
            passes.append(
                {"extractor": extractor, "passed": False, "problems": [str(exc)], "notes": []}
            )
            continue
        report = build_report(doc, resume_data, max_pages)
        result = verdict(report, max_pages)
        result["checks"] = [
            {"id": c.id, "status": c.status, "params": c.params} for c in report.checks
        ]
        result["extracted_text"] = doc.text
        passes.append(result)
    return {"passed": all(p["passed"] for p in passes), "passes": passes}


def summary_line(result: dict[str, Any]) -> str:
    """One line for the report table: "pass (pdfminer, pdftotext)" or the failures."""
    if result["passed"]:
        return "pass (" + ", ".join(p["extractor"] for p in result["passes"]) + ")"
    parts = [
        f"{p['extractor']}: {'; '.join(p['problems'])}" for p in result["passes"] if not p["passed"]
    ]
    return "FAIL: " + " | ".join(parts)


__all__ = [
    "EXTRACTORS",
    "build_report",
    "parse_check_pdf",
    "pdftotext_available",
    "summary_line",
    "verdict",
]
