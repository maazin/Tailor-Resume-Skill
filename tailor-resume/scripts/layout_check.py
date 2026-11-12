"""Layout rules L1 to L13, measured on the compiled PDF, and the page-fill loop.

Every rule is checked on the real page with pdfminer, not guessed from
character counts: how many lines each bullet prints on, how full its last
line is, how much empty space is left at the bottom.

``fit`` is the loop behind L9 to L11. It picks how many projects fit on one
page, spreads leftover space across the two spacing lengths, and reports
what is still wrong. It never changes wording, fonts or margins.

CLI:
  layout_check.py check resume.pdf --tex resume.tex --resume resume.json [--master master.tex]
  layout_check.py fit --master master.tex --tailored tailored.json --out-dir DIR [--projects N]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from common import (
    ENTRY_KINDS,
    Finding,
    bullets,
    dump_json,
    entries,
    entry_name,
    load_json,
    load_resume,
)
from compile import CompileError, Geometry, compile_tex, probe_geometry
from latex_io import (
    DEFAULT_ENTRYGAP_PT,
    DEFAULT_SECTIONGAP_PT,
    merge_tailored,
    read_tex,
    split_document,
    write_tex,
)

INCH = 72.0
MIN_MARGIN_IN = 0.3
MARGIN_TOLERANCE_PT = 0.75
MAX_BOTTOM_GAP_PT = 0.25 * INCH  # L11
BULLET_LINES = 2  # L4
MIN_LAST_LINE_FILL = 0.50  # L5
MAX_ENTRYGAP_PT = 9.0  # L10
MAX_SECTIONGAP_PT = 12.0  # L10
MAX_CONTENT_PASSES = 4
BULLET_MARKS = ("\u2022",)
DASH_MARKS = ("\u2013", "-", "\u2014", "\u2212")
SYMBOLS = {  # L12
    "~": 'write "about"', "\u2248": 'write "about"', "\u2192": 'write "then" or "to"',
    "->": 'write "then" or "to"', "=>": 'write "then" or "to"', "\u2190": "write it in words",
    "\u2265": 'write "at least"', "\u2264": 'write "at most"', ">=": 'write "at least"',
    "<=": 'write "at most"', "\u00b1": "write it in words", "\u00d7": 'write "x" or "times"',
}


@dataclass
class Line:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


@dataclass
class BulletMeasure:
    where: str
    text: str
    lines: int
    last_line_fill: float
    chars: int


@dataclass
class PageMeasure:
    pages: int
    page_width: float = 0.0
    page_height: float = 0.0
    lines: list[Line] = field(default_factory=list)
    header_lines: list[str] = field(default_factory=list)
    skills_lines: list[str] = field(default_factory=list)
    bullets: list[BulletMeasure] = field(default_factory=list)
    headings: list[dict[str, Any]] = field(default_factory=list)
    dash_bullets: int = 0
    bottom_gap_pt: float | None = None
    margins_in: dict[str, float] = field(default_factory=dict)
    chars_per_line: int = 0

    def char_guide(self) -> tuple[int, int] | None:
        """Bullet length, in characters, that prints on 2 lines with the second
        at least half full. Measured from this page, so it follows the template."""
        if not self.chars_per_line:
            return None
        return int(self.chars_per_line * 1.56), int(self.chars_per_line * 1.93)


# --------------------------------------------------------------------------
# Measuring the PDF
# --------------------------------------------------------------------------


def pdf_lines(pdf_path: str | Path) -> tuple[list[list[Line]], list[tuple[float, float]]]:
    """Text lines per page, top to bottom, with their boxes in PDF points."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTTextLine

    def walk(item: Any, out: list[Line]) -> None:
        if isinstance(item, LTTextLine):
            text = item.get_text().strip()
            if text:
                out.append(Line(item.x0, item.y0, item.x1, item.y1, text))
            return
        for child in getattr(item, "_objs", []) or []:
            walk(child, out)

    pages: list[list[Line]] = []
    sizes: list[tuple[float, float]] = []
    for page in extract_pages(str(pdf_path), laparams=LAParams(all_texts=True)):
        found: list[Line] = []
        walk(page, found)
        found.sort(key=lambda ln: (-round(ln.y1, 1), ln.x0))
        pages.append(_merge_same_row(found))
        sizes.append((page.width, page.height))
    return pages, sizes


def _merge_same_row(lines: list[Line]) -> list[Line]:
    """pdfminer can split one printed line in two when a gap is wide; join them."""
    merged: list[Line] = []
    for line in lines:
        prev = merged[-1] if merged else None
        if prev and abs(prev.y0 - line.y0) < 2.0 and line.x0 >= prev.x1 - 1.0:
            merged[-1] = Line(prev.x0, min(prev.y0, line.y0), line.x1, max(prev.y1, line.y1), f"{prev.text} {line.text}")
        else:
            merged.append(line)
    return merged


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _expected_bullets(resume: dict[str, Any]) -> list[tuple[str, str]]:
    """(where, text) for every bullet in the order the page prints them."""
    order = [m["key"] for m in sorted(resume.get("sectionMeta") or [], key=lambda m: m.get("order", 0))]
    order = order or ["education", "workExperience", "personalProjects"]
    out: list[tuple[str, str]] = []
    for key in order:
        if key not in ("education", *ENTRY_KINDS):
            continue
        for entry in resume.get(key) or []:
            name = entry.get("institution") if key == "education" else entry_name(key, entry)
            for index, text in enumerate(bullets(entry)):
                out.append((f"{name} bullet {index + 1}", text))
    return out


def measure(pdf_path: str | Path, resume: dict[str, Any], geometry: Geometry | None = None) -> PageMeasure:
    pages, sizes = pdf_lines(pdf_path)
    result = PageMeasure(pages=len(pages))
    if not pages:
        return result
    result.page_width, result.page_height = sizes[0]
    lines = [line for page in pages for line in page]
    result.lines = lines

    titles = {_norm(m.get("title", "")): m["key"] for m in resume.get("sectionMeta") or []}
    right_edge = geometry.right_edge if geometry else result.page_width - min(l.x0 for l in lines)

    # Bullet marker x: lines that open with a bullet (or a dash, for L6).
    marker_x = [l.x0 for l in lines if l.text.startswith(BULLET_MARKS + DASH_MARKS[:1])]
    continuation_x = (min(marker_x) + 2.0) if marker_x else None

    expected = _expected_bullets(resume)
    section: str | None = None
    current: dict[str, Any] | None = None  # the bullet being measured
    heading: dict[str, Any] | None = None
    seen_section = False
    full_lines: list[int] = []  # characters on bullet lines that ran to the margin

    def close_bullet() -> None:
        nonlocal current
        if current is None:
            return
        index = len(result.bullets)
        where, text = expected[index] if index < len(expected) else (f"bullet {index + 1}", current["text"])
        text_x = current["text_x"]
        width = max(1.0, right_edge - text_x)
        last = current["last"]
        fill = 1.0 if current["lines"] == 1 else max(0.0, min(1.0, (last.x1 - text_x) / width))
        result.bullets.append(
            BulletMeasure(where=where, text=text, lines=current["lines"], last_line_fill=round(fill, 3), chars=len(text))
        )
        current = None

    for line in lines:
        key = titles.get(_norm(line.text))
        if key:
            close_bullet()
            heading = None
            section = key
            seen_section = True
            continue
        if not seen_section:
            result.header_lines.append(line.text)
            continue
        if section == "additional":
            result.skills_lines.append(line.text)
            continue
        is_dash_bullet = line.text.startswith(DASH_MARKS) and len(line.text) > 2 and line.text[1] == " "
        if line.text.startswith(BULLET_MARKS) or is_dash_bullet:
            close_bullet()
            heading = None
            result.dash_bullets += int(is_dash_bullet)
            # Text starts after the marker; the next line's x0 refines it.
            current = {
                "lines": 1, "text": line.text, "text_x": line.x0 + 9.0, "last": line,
                "refined": False, "first_chars": len(line.text) - 2,
            }
            continue
        if current is not None and continuation_x is not None and line.x0 > continuation_x:
            current["lines"] += 1
            current["last"] = line
            if not current["refined"]:
                current["text_x"] = line.x0
                current["refined"] = True
                full_lines.append(current["first_chars"])
            continue
        # Anything else under a section is an entry heading (or its wrapped part).
        close_bullet()
        if heading is None:
            heading = {"section": section, "text": line.text, "lines": 1}
            result.headings.append(heading)
        else:
            heading["lines"] += 1
            heading["text"] += " " + line.text
    close_bullet()
    if full_lines:
        result.chars_per_line = sorted(full_lines)[len(full_lines) // 2]

    if len(pages) == 1 and geometry:
        lowest = min(l.y0 for l in pages[0])
        result.bottom_gap_pt = round(max(0.0, lowest - geometry.bottom_y), 2)
    if geometry:
        result.margins_in = {
            "left": round(geometry.left / INCH, 3),
            "right": round(geometry.right_margin / INCH, 3),
            "top": round(geometry.top / INCH, 3),
            "bottom": round(geometry.bottom_margin / INCH, 3),
        }
    return result


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def text_findings(resume: dict[str, Any]) -> list[Finding]:
    """L12: symbols written as words. Runs on text, so it can run before compiling."""
    findings: list[Finding] = []
    for kind, _, entry in entries(resume):
        for index, text in enumerate(bullets(entry)):
            for symbol, advice in SYMBOLS.items():
                if symbol in text:
                    findings.append(
                        Finding("L12", "block", f"symbol '{symbol}' in a bullet; {advice}",
                                where=f"{entry_name(kind, entry)} bullet {index + 1}", value=text)
                    )
    return findings


def _preamble_settings(tex: str) -> str:
    """The preamble with the lines the skill is allowed to change blanked out."""
    preamble, _ = split_document(tex)
    preamble = re.sub(r"\\setlength\s*\{\\(?:entrygap|sectiongap)\}\s*\{[^}]*\}", "", preamble)
    preamble = re.sub(r"(pdfkeywords|pdfsubject)\s*=\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", r"\1={}", preamble)
    preamble = re.sub(r"[ \t]*\\[Nn]eedspace\*?\{[^}]*\}[ \t]*\n?", "", preamble)
    preamble = re.sub(r"\\usepackage(?:\[[^\]]*\])?\{needspace\}[^\n]*\n", "", preamble)
    lines = [re.sub(r"(?<!\\)%.*$", "", line).strip() for line in preamble.split("\n")]
    return "\n".join(line for line in lines if line)


def tex_findings(tex: str, resume: dict[str, Any], master_tex: str | None = None) -> list[Finding]:
    """L1 (nothing shrunk), L6, L8 and L13, read from the .tex source."""
    findings: list[Finding] = []
    preamble, body = split_document(tex)
    live_preamble = "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in preamble.split("\n"))

    if master_tex is not None and _preamble_settings(tex) != _preamble_settings(master_tex):
        findings.append(
            Finding("L1", "block", "the preamble differs from the master's beyond the two spacing lengths "
                    "and the PDF keywords; fonts and margins must stay as the master has them", where="preamble")
        )
    item_list = re.search(r"\\newcommand\{\\resumeItemListStart\}\{(.*)\}", live_preamble)
    if not item_list or r"label=\textbullet" not in item_list.group(1).replace(" ", ""):
        findings.append(Finding("L6", "block", r"\resumeItemListStart must set label=\textbullet", where="preamble"))
    if r"\sectiongap" not in live_preamble or r"\entrygap" not in live_preamble:
        findings.append(Finding("L8", "block", r"the preamble must define \sectiongap and \entrygap", where="preamble"))
    if re.search(r"\\vspace", body):
        findings.append(Finding("L8", "block", r"\vspace in the document body; spacing must come from the preamble lengths", where="body"))
    if re.search(r"\\[Nn]eedspace", tex):
        findings.append(Finding("L13", "block", r"\Needspace must not appear in the output file", where="tex"))
    if re.search(r"^\s*%.*\\resumeItem", body, re.MULTILINE):
        findings.append(Finding("L13", "block", "commented-out alternate bullets must not appear in the output file", where="body"))
    keywords = re.search(r"pdfkeywords\s*=\s*\{([^}]*)\}", live_preamble)
    if keywords:
        on_page = {_norm(s) for s in (resume.get("additional") or {}).get("technicalSkills") or []}
        extra = [
            k.strip() for k in keywords.group(1).replace("\n", " ").split(",")
            if k.strip() and _norm(k.replace("\\", "")) not in on_page
        ]
        if extra:
            findings.append(
                Finding("L13", "block", "PDF keywords list skills that are not on the page", where="preamble", value=", ".join(extra))
            )
    return findings


def page_findings(measured: PageMeasure, resume: dict[str, Any]) -> list[Finding]:
    """L1 to L7, L9 and L11 from the measured page."""
    findings: list[Finding] = []
    guide = measured.char_guide()
    aim = f" Aim for about {guide[0]} to {guide[1]} characters in this template." if guide else ""

    for side, value in measured.margins_in.items():
        if value < MIN_MARGIN_IN - MARGIN_TOLERANCE_PT / INCH:
            findings.append(
                Finding("L1", "block", f"{side} margin is {value:.2f} in; it must be at least {MIN_MARGIN_IN} in",
                        where="page", value=f"{value:.2f}")
            )

    if measured.pages != 1:
        findings.append(
            Finding("L9", "block", f"the PDF has {measured.pages} pages; it must have exactly 1. Drop the lowest-ranked project",
                    where="page", value=str(measured.pages))
        )

    info = resume.get("personalInfo") or {}
    expected_header = 2 + bool(info.get("title"))
    if len(measured.header_lines) != expected_header:
        findings.append(
            Finding("L2", "block",
                    f"the header prints on {len(measured.header_lines)} lines; expected {expected_header} "
                    "(name, job title, one contact line). Shorten the contact line",
                    where="header", value=" / ".join(measured.header_lines))
        )

    groups = resume.get("skillGroups") or []
    if groups and len(measured.skills_lines) != len(groups):
        labels = tuple(_norm(g["label"]) for g in groups)
        wrapped = []
        last_label = ""
        for text in measured.skills_lines:
            match = next((lab for lab in labels if _norm(text).startswith(lab)), None)
            if match:
                last_label = match
            elif last_label and last_label not in wrapped:
                wrapped.append(last_label)
        findings.append(
            Finding("L3", "block",
                    f"the skills section prints on {len(measured.skills_lines)} lines for {len(groups)} groups; "
                    "trim the least relevant items" + (f" from: {', '.join(wrapped)}" if wrapped else ""),
                    where="skills", value=", ".join(wrapped))
        )

    expected = _expected_bullets(resume)
    if len(measured.bullets) != len(expected):
        findings.append(
            Finding("L6", "block",
                    f"found {len(measured.bullets)} bullets on the page but the resume has {len(expected)}; "
                    "check the bullet marker and the template", where="page")
        )
    if measured.dash_bullets:
        findings.append(
            Finding("L6", "block", f"{measured.dash_bullets} bullets print with a dash instead of a round bullet", where="page")
        )
    for bullet in measured.bullets:
        if bullet.lines != BULLET_LINES:
            findings.append(
                Finding("L4", "block",
                        f"prints on {bullet.lines} line{'s' if bullet.lines != 1 else ''} ({bullet.chars} characters); "
                        f"every bullet must print on exactly {BULLET_LINES}. "
                        + ("Lengthen it with a true detail from the master." if bullet.lines < BULLET_LINES else "Shorten it.")
                        + aim,
                        where=bullet.where, value=bullet.text)
            )
        elif bullet.last_line_fill < MIN_LAST_LINE_FILL:
            findings.append(
                Finding("L5", "block",
                        f"second line is {bullet.last_line_fill:.0%} full ({bullet.chars} characters); it must be at "
                        f"least {MIN_LAST_LINE_FILL:.0%}. Add a true detail from the master.{aim}",
                        where=bullet.where, value=bullet.text)
            )

    for heading in measured.headings:
        if heading["lines"] > 1:
            is_project = heading["section"] == "personalProjects"
            findings.append(
                Finding("L7" if is_project else "L2", "block" if is_project else "warn",
                        f"entry heading wraps onto {heading['lines']} lines"
                        + ("; trim the tech stack" if is_project else ""),
                        where=heading["section"] or "", value=heading["text"])
            )

    if measured.pages == 1 and measured.bottom_gap_pt is not None and measured.bottom_gap_pt > MAX_BOTTOM_GAP_PT:
        findings.append(
            Finding("L11", "block",
                    f"{measured.bottom_gap_pt / INCH:.2f} in of empty space at the bottom after spreading; "
                    f"the limit is {MAX_BOTTOM_GAP_PT / INCH:.2f} in. Add the next-ranked project or a fourth bullet (C6)",
                    where="page", value=f"{measured.bottom_gap_pt / INCH:.2f}")
        )
    return findings


def check_layout(
    pdf_path: str | Path,
    tex_path: str | Path,
    resume: dict[str, Any],
    master_tex: str | None = None,
    geometry: Geometry | None = None,
) -> tuple[list[Finding], PageMeasure]:
    tex = Path(tex_path).read_text(encoding="utf-8")
    geometry = geometry or probe_geometry(tex_path)
    measured = measure(pdf_path, resume, geometry)
    findings = tex_findings(tex, resume, master_tex) + page_findings(measured, resume) + text_findings(resume)
    return findings, measured


# --------------------------------------------------------------------------
# Page fill (L9 to L11)
# --------------------------------------------------------------------------


@dataclass
class FitResult:
    ok: bool
    tex: str = ""
    pdf: str = ""
    pages: int = 0
    projects_shown: int = 0
    projects_available: int = 0
    entrygap_pt: float = DEFAULT_ENTRYGAP_PT
    sectiongap_pt: float = DEFAULT_SECTIONGAP_PT
    bottom_gap_in: float | None = None
    compiles: int = 0
    log: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _gap_counts(resume: dict[str, Any]) -> tuple[int, int]:
    """How many \\entrygap and \\sectiongap gaps the page has."""
    entry_gaps = 0
    sections = 0
    for meta in resume.get("sectionMeta") or []:
        key = meta["key"]
        if key == "additional":
            sections += 1
            continue
        count = len(resume.get(key) or [])
        if count:
            sections += 1
            entry_gaps += count - 1
    return entry_gaps, sections


def fit(
    tailored: dict[str, Any],
    master: dict[str, Any],
    out_dir: str | Path,
    stem: str = "resume",
    projects: int | None = None,
) -> tuple[FitResult, dict[str, Any] | None]:
    """Write and compile the tailored resume so it fills exactly one page.

    Work entries are always kept. Projects are taken in the order the tailored
    file lists them (best first): the lowest-ranked one is dropped on overflow
    and the next one added when the page is short. Then leftover space is
    shared across \\entrygap (5 to 9pt) and \\sectiongap (8 to 12pt).
    Returns the result and the resume dict as written.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{stem}.tex"
    available = len(tailored.get("personalProjects") or [])
    shown = available if projects is None else max(0, min(projects, available))
    result = FitResult(ok=False, tex=str(tex_path), projects_available=available)
    geometry: Geometry | None = None
    outcome: dict[int, str] = {}

    def build(count: int, entrygap: float, sectiongap: float) -> tuple[dict[str, Any], Any, float | None]:
        nonlocal geometry
        resume = merge_tailored(tailored, master, count)
        tex_path.write_text(write_tex(resume, entrygap, sectiongap), encoding="utf-8")
        compiled = compile_tex(tex_path)
        result.compiles += 1
        gap = None
        if compiled.ok and compiled.pages == 1:
            geometry = geometry or probe_geometry(tex_path)
            pages, _ = pdf_lines(compiled.pdf)
            gap = max(0.0, min(line.y0 for line in pages[0]) - geometry.bottom_y)
        return resume, compiled, gap

    resume: dict[str, Any] | None = None
    gap: float | None = None
    try:
        for _ in range(MAX_CONTENT_PASSES + 1):
            resume, compiled, gap = build(shown, DEFAULT_ENTRYGAP_PT, DEFAULT_SECTIONGAP_PT)
            if not compiled.ok:
                result.errors = compiled.errors
                return result, resume
            if compiled.pages > 1:
                outcome[shown] = "overflow"
                result.log.append(f"{shown} projects: {compiled.pages} pages")
                if shown == 0:
                    result.errors = ["the work experience alone does not fit on one page"]
                    result.pages = compiled.pages
                    return result, resume
                shown -= 1
                continue
            entry_gaps, sections = _gap_counts(resume)
            absorb = (MAX_ENTRYGAP_PT - DEFAULT_ENTRYGAP_PT) * entry_gaps + (
                MAX_SECTIONGAP_PT - DEFAULT_SECTIONGAP_PT
            ) * sections
            result.log.append(f"{shown} projects: 1 page, {gap / INCH:.2f} in free before spreading")
            outcome[shown] = "fits"
            if gap - absorb > MAX_BOTTOM_GAP_PT and shown < available and outcome.get(shown + 1) != "overflow":
                shown += 1
                continue
            break
        else:
            result.errors = ["could not settle on a project count that fits one page"]
            return result, resume

        # L10: share the leftover space across the two lengths.
        entry_gaps, sections = _gap_counts(resume)
        entry_room = (MAX_ENTRYGAP_PT - DEFAULT_ENTRYGAP_PT) * entry_gaps
        section_room = (MAX_SECTIONGAP_PT - DEFAULT_SECTIONGAP_PT) * sections
        share = 0.0
        if gap and entry_room + section_room > 0:
            share = max(0.0, min(1.0, (gap - 1.0) / (entry_room + section_room)))
        entrygap, sectiongap = DEFAULT_ENTRYGAP_PT, DEFAULT_SECTIONGAP_PT
        for _ in range(4):
            if share <= 0:
                break
            entrygap = DEFAULT_ENTRYGAP_PT + share * (MAX_ENTRYGAP_PT - DEFAULT_ENTRYGAP_PT)
            sectiongap = DEFAULT_SECTIONGAP_PT + share * (MAX_SECTIONGAP_PT - DEFAULT_SECTIONGAP_PT)
            resume, compiled, gap = build(shown, entrygap, sectiongap)
            if compiled.ok and compiled.pages == 1:
                break
            share = max(0.0, share - 0.12)  # spilled onto a second page: back off
        else:
            share = 0.0
        if share <= 0:
            entrygap, sectiongap = DEFAULT_ENTRYGAP_PT, DEFAULT_SECTIONGAP_PT
            resume, compiled, gap = build(shown, entrygap, sectiongap)
    except CompileError as exc:
        result.errors = [str(exc)]
        return result, resume

    result.pdf = compiled.pdf
    result.pages = compiled.pages
    result.projects_shown = shown
    result.entrygap_pt = round(entrygap, 2)
    result.sectiongap_pt = round(sectiongap, 2)
    result.bottom_gap_in = round(gap / INCH, 3) if gap is not None else None
    result.log.append(
        f"spread: entrygap {result.entrygap_pt}pt, sectiongap {result.sectiongap_pt}pt, "
        f"{result.bottom_gap_in} in free at the bottom"
    )
    if outcome.get(shown + 1) == "overflow" and gap is not None and gap > MAX_BOTTOM_GAP_PT:
        result.log.append(
            f"{shown + 1} projects overflow and {shown} leave the page short: lengthen bullets, "
            "use 4 bullets per entry (C6), or shorten the next project so it fits"
        )
    result.ok = compiled.ok and compiled.pages == 1
    return result, resume


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Layout rules L1-L13 and the page-fill loop.")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check")
    check.add_argument("pdf")
    check.add_argument("--tex", required=True)
    check.add_argument("--resume", required=True, help="resume .json (as written) or .tex")
    check.add_argument("--master")

    fit_cmd = sub.add_parser("fit")
    fit_cmd.add_argument("--master", required=True)
    fit_cmd.add_argument("--tailored", required=True)
    fit_cmd.add_argument("--out-dir", required=True)
    fit_cmd.add_argument("--stem", default="resume")
    fit_cmd.add_argument("--projects", type=int)

    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            resume = load_resume(args.resume)
            master_tex = Path(args.master).read_text(encoding="utf-8") if args.master else None
            findings, measured = check_layout(args.pdf, args.tex, resume, master_tex)
            payload = {
                "findings": [f.to_dict() for f in findings],
                "bullets": [asdict(b) for b in measured.bullets],
                "bottom_gap_in": None if measured.bottom_gap_pt is None else round(measured.bottom_gap_pt / INCH, 3),
                "margins_in": measured.margins_in,
                "bullet_chars_guide": measured.char_guide(),
            }
            sys.stdout.write(dump_json(payload))
            return 1 if any(f.severity == "block" for f in findings) else 0
        master = read_tex(args.master)
        result, resume = fit(load_json(args.tailored), master, args.out_dir, args.stem, args.projects)
        payload: dict[str, Any] = {"fit": result.to_dict()}
        if result.ok and resume is not None:
            findings, measured = check_layout(
                result.pdf, result.tex, resume, Path(args.master).read_text(encoding="utf-8")
            )
            payload["findings"] = [f.to_dict() for f in findings]
            payload["bullets"] = [asdict(b) for b in measured.bullets]
            payload["bullet_chars_guide"] = measured.char_guide()
            sys.stdout.write(dump_json(payload))
            return 1 if any(f.severity == "block" for f in findings) else 0
        sys.stdout.write(dump_json(payload))
        return 1
    except CompileError as exc:
        print(f"layout_check: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
