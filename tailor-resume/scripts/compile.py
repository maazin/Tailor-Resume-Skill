"""Compile a resume .tex with pdflatex and report the page count.

Runs ``pdflatex -interaction=nonstopmode -halt-on-error resume.tex`` in the
file's own folder and counts pages with pdfminer. ``probe_geometry`` asks TeX
for the page box of the same preamble, so the layout check can measure
margins and leftover space against the real text area.

CLI:
  compile.py resume.tex [--pages 1]
Exit code 0 when it compiles to the wanted page count, 1 otherwise.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from common import dump_json

PDFLATEX_TIMEOUT_SECONDS = 90
TEX_PT_PER_INCH = 72.27
PDF_PT_PER_INCH = 72.0
REQUIRED_PREAMBLE = (r"\input{glyphtounicode}", r"\pdfgentounicode=1")
_GEOMETRY_NAMES = (
    "paperwidth", "paperheight", "textwidth", "textheight", "oddsidemargin",
    "topmargin", "headheight", "headsep", "hoffset", "voffset", "footskip",
)


class CompileError(RuntimeError):
    pass


@dataclass
class CompileResult:
    ok: bool
    tex: str
    pdf: str = ""
    pages: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Geometry:
    """The text area in PDF points, measured from the page's top-left corner."""

    page_width: float
    page_height: float
    left: float
    top: float
    text_width: float
    text_height: float

    @property
    def right_margin(self) -> float:
        return self.page_width - self.left - self.text_width

    @property
    def bottom_margin(self) -> float:
        return self.page_height - self.top - self.text_height

    @property
    def right_edge(self) -> float:
        return self.left + self.text_width

    @property
    def bottom_y(self) -> float:
        """Bottom of the text area in PDF coordinates (origin at the bottom)."""
        return self.bottom_margin


def pdflatex_available() -> bool:
    return shutil.which("pdflatex") is not None


def _run_pdflatex(tex_path: Path, extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    if not pdflatex_available():
        raise CompileError(
            "pdflatex not found. Install TeX Live (the sandbox has it; on macOS: brew install texlive)."
        )
    command = [
        "pdflatex", "-interaction=nonstopmode", "-halt-on-error", *extra, tex_path.name,
    ]
    try:
        return subprocess.run(
            command,
            cwd=tex_path.parent,
            capture_output=True,
            timeout=PDFLATEX_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CompileError(f"pdflatex timed out after {PDFLATEX_TIMEOUT_SECONDS}s") from exc


def _log_errors(log: str) -> list[str]:
    errors: list[str] = []
    lines = log.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("!"):
            context = " ".join(part.strip() for part in lines[index : index + 3])
            errors.append(context[:300])
    return errors


def _log_warnings(log: str) -> list[str]:
    found = re.findall(r"(Overfull \\hbox \([^)]*\) in paragraph at lines [\d-]+)", log)
    return sorted(set(found))


def count_pages(pdf_path: str | Path) -> int:
    from pdfminer.pdfpage import PDFPage

    with open(pdf_path, "rb") as handle:
        return sum(1 for _ in PDFPage.get_pages(handle))


def compile_tex(tex_path: str | Path, keep_aux: bool = False) -> CompileResult:
    """Compile once. The PDF lands next to the .tex file."""
    tex_path = Path(tex_path).resolve()
    if not tex_path.is_file():
        raise CompileError(f"No such file: {tex_path}")
    source = tex_path.read_text(encoding="utf-8")
    missing = [line for line in REQUIRED_PREAMBLE if line.replace(" ", "") not in source.replace(" ", "")]
    pdf_path = tex_path.with_suffix(".pdf")
    if pdf_path.exists():
        pdf_path.unlink()

    process = _run_pdflatex(tex_path)
    log_path = tex_path.with_suffix(".log")
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    log = log or process.stdout.decode("utf-8", "replace")
    if not keep_aux:
        for suffix in (".aux", ".out", ".log"):
            tex_path.with_suffix(suffix).unlink(missing_ok=True)

    result = CompileResult(ok=False, tex=str(tex_path), warnings=_log_warnings(log))
    if missing:
        result.warnings.append(
            "preamble is missing " + " and ".join(missing) + "; PDF text may not copy out cleanly (D10)"
        )
    if process.returncode != 0 or not pdf_path.is_file():
        result.errors = _log_errors(log) or ["pdflatex failed without an error line; see the log"]
        return result
    result.ok = True
    result.pdf = str(pdf_path)
    result.pages = count_pages(pdf_path)
    return result


def probe_geometry(tex_path: str | Path) -> Geometry:
    """Ask TeX for the page box that this file's preamble sets up."""
    source = Path(tex_path).read_text(encoding="utf-8")
    begin = source.find(r"\begin{document}")
    if begin < 0:
        raise CompileError(r"no \begin{document} in " + str(tex_path))
    report = "; ".join(f"{name}=\\the\\{name}" for name in _GEOMETRY_NAMES)
    probe = (
        source[:begin]
        + "\\begin{document}\n\\typeout{TRGEOM " + report + ";}\n\\end{document}\n"
    )
    with tempfile.TemporaryDirectory(prefix="tailor-resume-") as tmp:
        probe_path = Path(tmp) / "probe.tex"
        probe_path.write_text(probe, encoding="utf-8")
        process = _run_pdflatex(probe_path, ("-draftmode",))
        log_path = probe_path.with_suffix(".log")
        log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        log += process.stdout.decode("utf-8", "replace")
    joined = log.replace("\n", "")
    values: dict[str, float] = {}
    for name in _GEOMETRY_NAMES:
        match = re.search(rf"{name}=(-?[\d.]+)pt", joined)
        if not match:
            raise CompileError(f"could not read \\{name} from the preamble: " + "; ".join(_log_errors(log)))
        values[name] = float(match.group(1)) * PDF_PT_PER_INCH / TEX_PT_PER_INCH
    inch = PDF_PT_PER_INCH
    return Geometry(
        page_width=values["paperwidth"],
        page_height=values["paperheight"],
        left=inch + values["hoffset"] + values["oddsidemargin"],
        top=inch + values["voffset"] + values["topmargin"] + values["headheight"] + values["headsep"],
        text_width=values["textwidth"],
        text_height=values["textheight"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile a resume .tex with pdflatex.")
    parser.add_argument("tex")
    parser.add_argument("--pages", type=int, default=1, help="page count that counts as success")
    parser.add_argument("--keep-aux", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = compile_tex(args.tex, args.keep_aux)
    except CompileError as exc:
        print(f"compile: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(dump_json(result.to_dict()))
    return 0 if result.ok and result.pages == args.pages else 1


if __name__ == "__main__":
    raise SystemExit(main())
