"""Read master.tex into a resume dict and write a tailored .tex in the same template.

The reader understands one family of templates: Jake's Resume macros as the
user's master file uses them.

  \\resumeSubheading{Organization}{Location}{Role or Degree}{Dates}
  \\resumeProject{\\link{url}{Name} $\\vert$ \\emph{Tech, Stack}}{Dates or empty}
  \\resumeItem{bullet text}
  % \\resumeItem{a commented-out alternate bullet}      <- still a master bullet

It refuses a file that uses \\resume... macros it does not know, rather than
guess. The dict it returns has Resume-Matcher's shape (personalInfo,
workExperience, education, personalProjects, additional.technicalSkills) so
the scoring code runs on it unchanged.

The writer keeps the user's preamble and swaps only the content. It touches
three things in the preamble, all required by the layout rules: the two
spacing lengths (L10), the PDF keywords (L13) and any \\Needspace call (L13).

CLI:
  latex_io.py read     master.tex [-o master.json]
  latex_io.py bullets  master.tex              every master bullet with its id
  latex_io.py validate master.tex              template problems, if any
  latex_io.py baseline master.tex -o baseline.tex
  latex_io.py write    --master master.tex --tailored tailored.json -o resume.tex
                       [--projects N] [--entrygap PT] [--sectiongap PT]
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path
from typing import Any

from common import (
    ENTRY_KINDS,
    Finding,
    dump_json,
    entry_id,
    entry_name,
    find_master_entry,
    load_json,
    master_bullets,
    normalize_term,
)

KNOWN_MACROS = {
    "resumeSubheading",
    "resumeProject",
    "resumeProjectHeading",
    "resumeItem",
    "resumeAltItem",  # internal: a commented-out \resumeItem
    "resumeSubHeadingListStart",
    "resumeSubHeadingListEnd",
    "resumeItemListStart",
    "resumeItemListEnd",
}
SECTION_KEYS = (
    ("education", ("education", "academic")),
    ("additional", ("skill", "technolog", "competenc")),
    ("workExperience", ("experience", "employment", "work history")),
    ("personalProjects", ("project",)),
)
SEPARATORS = (r"$\vert$", r"$|$", r"\textbar{}", r"\textbar")
DEFAULT_ENTRYGAP_PT = 5.0
DEFAULT_SECTIONGAP_PT = 8.0
BASELINE_BULLETS = 3


class LatexFormatError(ValueError):
    """The .tex file is not in a form this reader understands."""


# --------------------------------------------------------------------------
# LaTeX <-> plain text
# --------------------------------------------------------------------------

_ESCAPES = {
    r"\&": "&", r"\%": "%", r"\$": "$", r"\#": "#", r"\_": "_",
    r"\{": "{", r"\}": "}",
}
_MATH = {
    r"\sim": "~", r"\rightarrow": "\u2192", r"\to": "\u2192", r"\leftarrow": "\u2190",
    r"\vert": "|", r"\times": "x", r"\geq": ">=", r"\leq": "<=", r"\approx": "~",
    r"\nearrow": "", r"\pm": "+/-",
}
_TEXT_COMMANDS = {
    r"\textbar": "|", r"\textasciitilde": "~", r"\textasciicircum": "^",
    r"\textbackslash": "\\", r"\textbullet": "\u2022", r"\textendash": "\u2013",
    r"\textemdash": "\u2014", r"\LaTeX": "LaTeX", r"\TeX": "TeX", r"\textquotedbl": '"',
    r"\linkarrow": "",
}
_WRAPPERS = ("textbf", "textit", "emph", "underline", "textsc", "texttt", "mbox", "textrm", "textsf")
_SWITCHES = (
    "tiny", "scriptsize", "footnotesize", "small", "normalsize", "large", "Large",
    "LARGE", "huge", "Huge", "bfseries", "itshape", "scshape", "rmfamily", "sffamily",
    "ttfamily", "normalfont", "centering", "raggedright", "selectfont",
)


def read_group(text: str, start: int) -> tuple[str, int]:
    """Content of the {...} group opening at ``start`` and the index after it."""
    if start >= len(text) or text[start] != "{":
        raise LatexFormatError(f"expected '{{' at offset {start}: {text[start:start + 40]!r}")
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i], i + 1
        i += 1
    raise LatexFormatError(f"unbalanced braces after offset {start}: {text[start:start + 60]!r}")


def read_args(text: str, start: int, count: int) -> tuple[list[str], int]:
    args: list[str] = []
    i = start
    for _ in range(count):
        while i < len(text) and text[i].isspace():
            i += 1
        arg, i = read_group(text, i)
        args.append(arg)
    return args, i


def _brace_balance(text: str) -> int:
    depth = 0
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        depth += text[i] == "{"
        depth -= text[i] == "}"
        i += 1
    return depth


def _unwrap_commands(text: str) -> str:
    r"""\href{url}{x} -> x, \link{url}{x} -> x, \textbf{x} -> x, innermost first."""
    pattern = re.compile(r"\\(href|link|" + "|".join(_WRAPPERS) + r")\s*\{")
    while True:
        match = None
        for match in pattern.finditer(text):
            pass  # keep the last match: it has no nested wrapper after it
        if match is None:
            return text
        name = match.group(1)
        first, end = read_group(text, match.end() - 1)
        if name in ("href", "link"):
            second, end = read_group(text, end)
            replacement = second
        else:
            replacement = first
        text = text[: match.start()] + replacement + text[end:]


def detex(text: str) -> str:
    """Plain text for a LaTeX fragment, as the PDF would show it."""
    placeholders: dict[str, str] = {}
    for index, (escaped, plain) in enumerate(_ESCAPES.items()):
        token = f"\x00{index}\x00"
        placeholders[token] = plain
        text = text.replace(escaped, token)
    text = _unwrap_commands(text)
    text = text.replace("~", " ")  # a tie; a printed tilde is written as a command

    def math(match: re.Match[str]) -> str:
        inner = match.group(1)
        for command, plain in sorted(_MATH.items(), key=lambda kv: -len(kv[0])):
            inner = re.sub(re.escape(command) + r"(?![A-Za-z])", plain, inner)
        return inner.replace(" ", "")

    text = re.sub(r"\$([^$]*)\$", math, text)
    for command, plain in sorted(_TEXT_COMMANDS.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(re.escape(command) + r"(?![A-Za-z])(?:\{\})?", lambda m: plain, text)
    text = re.sub(r"\\(?:" + "|".join(_SWITCHES) + r")(?![A-Za-z])\s*", "", text)
    text = re.sub(r"\\(?:vspace|hspace)\*?\{[^}]*\}", "", text)
    text = re.sub(r"\\(?:hfill|par|noindent|quad|qquad)(?![A-Za-z])", " ", text)
    text = re.sub(r"\\[,;:! ]", " ", text)
    text = text.replace("``", '"').replace("''", '"')
    text = text.replace("---", "\u2014").replace("--", "\u2013")
    text = text.replace("{", "").replace("}", "")
    for token, plain in placeholders.items():
        text = text.replace(token, plain)
    return re.sub(r"\s+", " ", text).strip()


_ENTEX = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}", "\u2013": "--", "\u2014": "---",
    "\u2192": r"$\rightarrow$", "\u2190": r"$\leftarrow$", "\u2022": r"\textbullet{}",
    "\u00a0": "~",
}


def entex(text: str) -> str:
    """Escape plain text for LaTeX. Curly quotes are written as straight ones."""
    text = (
        text.replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u2018", "'").replace("\u2019", "'")
    )
    return "".join(_ENTEX.get(ch, ch) for ch in text)


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on ``sep`` outside parentheses: "A (b, c), D" -> ["A (b, c)", "D"]."""
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    parts.append(current.strip())
    return [p for p in parts if p]


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------


def split_document(tex: str) -> tuple[str, str]:
    begin = tex.find(r"\begin{document}")
    end = tex.rfind(r"\end{document}")
    if begin < 0 or end < 0:
        raise LatexFormatError(r"no \begin{document} ... \end{document} found")
    return tex[:begin], tex[begin + len(r"\begin{document}") : end]


def _prepare_body(body: str) -> str:
    """Turn commented-out \\resumeItem lines into \\resumeAltItem and drop other comments."""
    lines = body.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"\s*%+\s*\\resumeItem\s*\{", line):
            text = re.sub(r"^\s*%+\s*", "", line)
            while _brace_balance(text) > 0 and i + 1 < len(lines) and lines[i + 1].lstrip().startswith("%"):
                i += 1
                text += " " + re.sub(r"^\s*%+\s*", "", lines[i])
            out.append(text.replace(r"\resumeItem", r"\resumeAltItem", 1))
        else:
            out.append(re.sub(r"(?<!\\)%.*$", "", line))
        i += 1
    return "\n".join(out)


def _section_key(title: str) -> str:
    low = title.lower()
    for key, needles in SECTION_KEYS:
        if any(needle in low for needle in needles):
            return key
    raise LatexFormatError(
        f"section '{title}' is not one this reader knows (education, skills, experience, projects)"
    )


def _find_separator(text: str) -> str:
    for sep in SEPARATORS:
        if sep in text:
            return sep
    return ""


_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,}$")
_LOCATION_RE = re.compile(r"^(?:Relocating to\s+)?[A-Z][A-Za-z .'\u2019-]+,\s*[A-Z][A-Za-z .]+$|^Remote$")


def _parse_header(block: str) -> tuple[dict[str, str], dict[str, Any]]:
    rows = [r.strip() for r in re.split(r"\\\\(?:\[[^\]]*\])?", block) if r.strip()]
    if not rows:
        raise LatexFormatError("the header center block is empty")
    name_raw = re.sub(r"\\vspace\*?\{[^}]*\}", "", rows[0]).strip()
    info: dict[str, str] = {"name": detex(name_raw)}
    title = ""
    if len(rows) >= 3:
        title = detex(re.sub(r"\\vspace\*?\{[^}]*\}", "", rows[1]))
    contact_row = re.sub(r"\\vspace\*?\{[^}]*\}", "", rows[-1] if len(rows) > 1 else "").strip()
    size = re.match(r"\\(tiny|scriptsize|footnotesize|small|normalsize|large)(?![A-Za-z])\s*", contact_row)
    size_cmd = size.group(0).strip() if size else ""
    if size:
        contact_row = contact_row[size.end() :]
    separator = _find_separator(contact_row) or r"$\vert$"
    items: list[dict[str, str]] = []
    order: list[str] = []
    for raw in [p.strip() for p in contact_row.split(separator) if p.strip()]:
        text = detex(raw)
        href = re.search(r"\\href\s*\{([^}]*)\}", raw)
        if href and href.group(1).startswith("mailto:"):
            kind = "email"
        elif href:
            url = href.group(1).lower()
            kind = "linkedin" if "linkedin." in url else "github" if "github." in url else "website"
        elif _PHONE_RE.match(text) and sum(c.isdigit() for c in text) >= 7:
            kind = "phone"
        elif _LOCATION_RE.match(text):
            kind = "location"
        elif not title:
            title = text
            continue
        else:
            raise LatexFormatError(f"cannot tell what this header item is: {text!r}")
        if kind in info:
            raise LatexFormatError(f"the header has two {kind} items")
        info[kind] = text
        order.append(kind)
        items.append({"kind": kind, "raw": raw})
    info["title"] = title
    info.setdefault("location", "")
    if "location" not in order:
        order.insert(0, "location")
    header = {
        "name_raw": name_raw,
        "size_cmd": size_cmd,
        "separator": separator,
        "items": items,
        "contact_order": order,
    }
    return info, header


def _parse_project_heading(arg: str) -> dict[str, Any]:
    separator = _find_separator(arg)
    name_part, stack_part = (arg.split(separator, 1) + [""])[:2] if separator else (arg, "")
    url = ""
    link = re.search(r"\\(link|href)\s*\{", name_part)
    if link:
        url, _ = read_group(name_part, link.end() - 1)
    return {
        "name": detex(name_part),
        "url": url,
        "stack": split_top_level(detex(stack_part)),
        "_heading": {
            "separator": separator or r"$\vert$",
            "link": link.group(1) if link else "",
        },
    }


_DATE_HINT = re.compile(r"(?:19|20)\d{2}|present|current", re.IGNORECASE)


def _parse_subheading(args: list[str], key: str) -> dict[str, Any]:
    plain = [detex(a) for a in args]
    # Master order is {Organization}{Location}{Role}{Dates}. Jake's sample writes
    # experience as {Role}{Dates}{Organization}{Location}; tell them apart by
    # where the dates are.
    if _DATE_HINT.search(plain[3]) or not _DATE_HINT.search(plain[1]):
        org, location, role, years = plain
    else:
        role, years, org, location = plain
    if key == "education":
        return {"institution": org, "location": location, "degree": role, "years": years, "description": []}
    return {
        "company": org, "location": location, "title": role, "years": years,
        "description": [], "alternates": [],
    }


def _parse_skills(section_body: str) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for match in re.finditer(r"\\textbf\s*\{", section_body):
        label, end = read_group(section_body, match.end() - 1)
        rest = section_body[end:]
        stop = re.search(r"\\\\|\\textbf\s*\{|\\end\s*\{|\\item(?![A-Za-z])", rest)
        items_text = rest[: stop.start()] if stop else rest
        items_plain = detex(items_text).lstrip(": ").strip()
        label_plain = detex(label).rstrip(": ").strip()
        if label_plain and items_plain:
            groups.append({"label": label_plain, "items": split_top_level(items_plain)})
    return groups


def read_tex_string(tex: str) -> dict[str, Any]:
    preamble, raw_body = split_document(tex)
    body = _prepare_body(raw_body)

    unknown = sorted(set(re.findall(r"\\(resume[A-Za-z]+)", body)) - KNOWN_MACROS)
    if unknown:
        raise LatexFormatError(
            "this file uses macros the reader does not know: "
            + ", ".join("\\" + name for name in unknown)
            + ". Known: \\resumeSubheading, \\resumeProject, \\resumeProjectHeading, \\resumeItem."
        )

    first_section = body.find(r"\section")
    head = body[: first_section if first_section >= 0 else len(body)]
    center = re.search(r"\\begin\{center\}(.*?)\\end\{center\}", head, re.DOTALL)
    if not center:
        raise LatexFormatError(r"no \begin{center} ... \end{center} header before the first section")
    info, header = _parse_header(center.group(1))

    resume: dict[str, Any] = {
        "personalInfo": info,
        "summary": "",
        "education": [],
        "workExperience": [],
        "personalProjects": [],
        "additional": {"technicalSkills": [], "certificationsTraining": []},
        "skillGroups": [],
        "sectionMeta": [],
    }
    template: dict[str, Any] = {
        "preamble": preamble,
        "header": header,
        "contact_order": header["contact_order"],
        "sections": [],
        "project_macro": "resumeProject",
        "project_heading": {"separator": r"$\vert$", "link": "link" if r"\newcommand{\link}" in preamble else "href"},
        "skills_begin": r"\begin{itemize}[leftmargin=0.15in,label={},topsep=3pt,parsep=0pt]",
    }

    section_matches = list(re.finditer(r"\\section\*?\s*\{", body))
    for index, match in enumerate(section_matches):
        title, after = read_group(body, match.end() - 1)
        end = section_matches[index + 1].start() if index + 1 < len(section_matches) else len(body)
        section_body = body[after:end]
        key = _section_key(detex(title))
        if any(meta["key"] == key for meta in resume["sectionMeta"]):
            raise LatexFormatError(f"two sections map to '{key}'; merge them in master.tex")
        resume["sectionMeta"].append(
            {"key": key, "order": index, "isVisible": True, "title": detex(title)}
        )
        template["sections"].append({"key": key, "title_raw": title})

        if key == "additional":
            groups = _parse_skills(section_body)
            if not groups:
                raise LatexFormatError(r"no '\textbf{Label:} items' lines found in the skills section")
            resume["skillGroups"] = groups
            resume["additional"]["technicalSkills"] = [i for g in groups for i in g["items"]]
            begin = re.search(r"\\begin\{itemize\}(?:\[[^\]]*\])?", section_body)
            if begin:
                template["skills_begin"] = begin.group(0)
            continue

        current: dict[str, Any] | None = None
        cursor = 0
        macro_re = re.compile(
            r"\\(resumeSubheading|resumeProjectHeading|resumeProject|resumeAltItem|resumeItem)(?![A-Za-z])"
        )
        while True:
            macro = macro_re.search(section_body, cursor)
            if not macro:
                break
            name = macro.group(1)
            if name == "resumeSubheading":
                args, cursor = read_args(section_body, macro.end(), 4)
                if key == "personalProjects":
                    raise LatexFormatError(r"\resumeSubheading inside the projects section; use \resumeProject")
                current = _parse_subheading(args, key)
                resume[key].append(current)
            elif name in ("resumeProject", "resumeProjectHeading"):
                args, cursor = read_args(section_body, macro.end(), 2)
                if key != "personalProjects":
                    raise LatexFormatError(f"\\{name} outside the projects section")
                parsed = _parse_project_heading(args[0])
                template["project_macro"] = name
                template["project_heading"] = parsed.pop("_heading")
                current = {
                    **parsed, "role": "", "years": detex(args[1]),
                    "description": [], "alternates": [],
                }
                resume[key].append(current)
            else:
                args, cursor = read_args(section_body, macro.end(), 1)
                if current is None:
                    raise LatexFormatError(f"\\resumeItem before any entry heading in '{detex(title)}'")
                text = detex(args[0])
                if not text:
                    continue
                if name == "resumeItem":
                    current["description"].append(text)
                else:
                    current.setdefault("alternates", []).append(text)

    for required in ("workExperience", "additional"):
        if not any(meta["key"] == required for meta in resume["sectionMeta"]):
            raise LatexFormatError(f"no {'experience' if required == 'workExperience' else 'skills'} section found")
    resume["_template"] = template
    return resume


def read_tex(path: str | Path) -> dict[str, Any]:
    return read_tex_string(Path(path).read_text(encoding="utf-8"))


def bullet_index(master: dict[str, Any]) -> list[dict[str, Any]]:
    """Every master bullet with a stable id, for scoring."""
    rows: list[dict[str, Any]] = []
    for kind in ENTRY_KINDS:
        for entry in master.get(kind) or []:
            shown = len(entry.get("description") or [])
            for index, text in enumerate(master_bullets(entry)):
                rows.append(
                    {
                        "id": f"{entry_id(kind, entry)}.{index}",
                        "kind": kind,
                        "entry": entry_name(kind, entry),
                        "alternate": index >= shown,
                        "chars": len(text),
                        "text": text,
                    }
                )
    return rows


# --------------------------------------------------------------------------
# Template checks
# --------------------------------------------------------------------------


def template_findings(tex: str) -> list[Finding]:
    """What the template must have before a tailored resume can be written."""
    preamble, body = split_document(tex)
    live_preamble = "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in preamble.split("\n"))
    live_body = "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in body.split("\n"))
    findings: list[Finding] = []

    def need(ok: bool, rule: str, message: str, severity: str = "block") -> None:
        if not ok:
            findings.append(Finding(rule, severity, message, where="template"))

    need(r"\input{glyphtounicode}" in live_preamble, "D10", r"preamble must keep \input{glyphtounicode}")
    need(
        bool(re.search(r"\\pdfgentounicode\s*=\s*1", live_preamble)),
        "D10", r"preamble must keep \pdfgentounicode=1",
    )
    need(
        r"\sectiongap" in live_preamble and r"\entrygap" in live_preamble,
        "L8", r"preamble must define the lengths \sectiongap and \entrygap and use them for spacing",
    )
    item_list = re.search(r"\\newcommand\{\\resumeItemListStart\}\{(.*)\}", live_preamble)
    need(
        bool(item_list and r"label=\textbullet" in item_list.group(1).replace(" ", "")),
        "L6", r"\resumeItemListStart must set label=\textbullet (the default is a dash)",
    )
    sub = re.search(r"\\newcommand\{\\resumeSubheading\}\[4\]\{(.*?)\n\}", live_preamble, re.DOTALL)
    need(
        bool(sub) and "tabular" not in (sub.group(1) if sub else "tabular") and r"\hfill" not in (sub.group(1) if sub else ""),
        "D12", r"\resumeSubheading must print role, organization, location and dates on one line (no tabular, no \hfill)",
    )
    need(
        not re.search(r"\\[Nn]eedspace", live_preamble + live_body),
        "L13", r"\Needspace found; it is removed when the tailored file is written", "info",
    )
    need(
        not re.search(r"\\vspace", live_body),
        "L8", r"\vspace found in the document body; spacing must come from the preamble lengths",
    )
    return findings


# --------------------------------------------------------------------------
# Building resumes to write
# --------------------------------------------------------------------------


def baseline_resume(master: dict[str, Any], count: int = BASELINE_BULLETS) -> dict[str, Any]:
    """The master with its first ``count`` bullets per entry and nothing else changed (D7)."""
    baseline = copy.deepcopy(master)
    for kind in ENTRY_KINDS:
        for entry in baseline.get(kind) or []:
            entry["description"] = master_bullets(entry)[:count]
            entry.pop("alternates", None)
    return baseline


def merge_tailored(
    tailored: dict[str, Any], master: dict[str, Any], projects: int | None = None
) -> dict[str, Any]:
    """A full resume dict from the tailored content plus what must come from the master.

    Contact details, education and the template always come from the master.
    Dates, locations and link targets are filled from the matching master
    entry when the tailored file leaves them out.
    """
    resume: dict[str, Any] = {
        "personalInfo": dict(master.get("personalInfo") or {}),
        "summary": "",
        "education": copy.deepcopy(master.get("education") or []),
        "workExperience": [],
        "personalProjects": [],
        "additional": {"technicalSkills": [], "certificationsTraining": []},
        "skillGroups": [],
        "sectionMeta": copy.deepcopy(master.get("sectionMeta") or []),
        "_template": master.get("_template"),
    }
    overrides = tailored.get("personalInfo") or {}
    for key in ("title", "location"):
        if key in overrides and overrides[key] is not None:
            resume["personalInfo"][key] = str(overrides[key]).strip()

    groups = tailored.get("skillGroups")
    if groups is None:
        groups = master.get("skillGroups") or []
    resume["skillGroups"] = [
        {"label": str(g.get("label", "")).strip(), "items": [str(i).strip() for i in g.get("items") or [] if str(i).strip()]}
        for g in groups
        if (g.get("items") or [])
    ]
    resume["additional"]["technicalSkills"] = [i for g in resume["skillGroups"] for i in g["items"]]

    for kind in ENTRY_KINDS:
        chosen = [e for e in tailored.get(kind) or [] if isinstance(e, dict)]
        if kind == "personalProjects" and projects is not None:
            chosen = chosen[: max(0, projects)]
        for entry in chosen:
            merged = {k: v for k, v in entry.items() if k != "alternates"}
            source = find_master_entry(kind, entry, master)
            if source:
                fill = ("title", "location", "years") if kind == "workExperience" else ("url", "years", "role", "stack")
                for key in fill:
                    if not merged.get(key):
                        merged[key] = copy.deepcopy(source.get(key) or ([] if key == "stack" else ""))
            merged["description"] = [str(b).strip() for b in merged.get("description") or [] if str(b).strip()]
            resume[kind].append(merged)
    return resume


# --------------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------------


def _format_pt(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") + "pt"


def _set_length(preamble: str, name: str, value: float) -> str:
    pattern = re.compile(r"(\\setlength\s*\{\\" + name + r"\}\s*\{)[^}]*(\})")
    if not pattern.search(preamble):
        raise LatexFormatError(f"the preamble has no \\setlength{{\\{name}}}{{...}} to adjust")
    return pattern.sub(lambda m: m.group(1) + _format_pt(value) + m.group(2), preamble)


def _replace_hypersetup_field(preamble: str, key: str, value: str) -> str:
    match = re.search(key + r"\s*=\s*\{", preamble)
    if not match:
        return preamble
    _, end = read_group(preamble, match.end() - 1)
    return preamble[: match.end()] + value + preamble[end - 1 :]


def _write_preamble(resume: dict[str, Any], entrygap: float | None, sectiongap: float | None) -> str:
    preamble = resume["_template"]["preamble"]
    preamble = re.sub(r"[ \t]*\\[Nn]eedspace\*?\{[^}]*\}[ \t]*\n?", "", preamble)
    preamble = re.sub(r"\\usepackage(?:\[[^\]]*\])?\{needspace\}[^\n]*\n", "", preamble)
    if entrygap is not None:
        preamble = _set_length(preamble, "entrygap", entrygap)
    if sectiongap is not None:
        preamble = _set_length(preamble, "sectiongap", sectiongap)
    skills = resume["additional"]["technicalSkills"]
    preamble = _replace_hypersetup_field(preamble, "pdfkeywords", ", ".join(entex(s) for s in skills))
    title = (resume.get("personalInfo") or {}).get("title") or ""
    if title:
        preamble = _replace_hypersetup_field(preamble, "pdfsubject", entex(title))
    return preamble


def _write_header(resume: dict[str, Any]) -> list[str]:
    info = resume["personalInfo"]
    header = resume["_template"]["header"]
    lines = [r"\begin{center}", f"  {header['name_raw']} \\\\[2pt]"]
    if info.get("title"):
        lines.append(f"  {{\\small {entex(info['title'])}}} \\\\[2pt]")
    if header.get("size_cmd"):
        lines.append(f"  {header['size_cmd']}")
    raw_by_kind = {item["kind"]: item["raw"] for item in header["items"]}
    parts: list[str] = []
    for kind in header["contact_order"]:
        if kind == "location":
            if info.get("location"):
                parts.append(entex(info["location"]))
        elif kind in raw_by_kind:
            parts.append(raw_by_kind[kind])
    separator = f" {header['separator']}\n  "
    lines.append("  " + separator.join(parts))
    lines.append(r"\end{center}")
    return lines


def _write_items(entry: dict[str, Any]) -> list[str]:
    bullets = entry.get("description") or []
    if not bullets:
        return []
    lines = [r"    \resumeItemListStart"]
    lines += [f"      \\resumeItem{{{entex(b)}}}" for b in bullets]
    lines.append(r"    \resumeItemListEnd")
    return lines


def _write_section(resume: dict[str, Any], section: dict[str, Any]) -> list[str]:
    key = section["key"]
    template = resume["_template"]
    lines = [f"\\section{{{section['title_raw']}}}"]
    if key == "additional":
        lines.append(template["skills_begin"])
        lines.append(r"  \small\item")
        rows = [
            f"    \\textbf{{{entex(g['label'])}:}} {', '.join(entex(i) for i in g['items'])}"
            for g in resume["skillGroups"]
        ]
        lines.append(" \\\\\n".join(rows))
        lines.append(r"\end{itemize}")
        return lines

    lines.append(r"\resumeSubHeadingListStart")
    for entry in resume.get(key) or []:
        if key == "personalProjects":
            heading = template["project_heading"]
            name = entex(entry.get("name", ""))
            if entry.get("url") and heading.get("link"):
                name = f"\\{heading['link']}{{{entry['url']}}}{{{name}}}"
            else:
                name = f"\\textbf{{{name}}}"
            stack = ", ".join(entex(s) for s in entry.get("stack") or [])
            first = name + (f" {heading['separator']} \\emph{{{stack}}}" if stack else "")
            if entry.get("years"):
                first += f" {heading['separator']} {entex(entry['years'])}"
            lines.append(f"  \\{template['project_macro']}")
            lines.append(f"    {{{first}}}{{}}")
        else:
            org = entry.get("institution" if key == "education" else "company", "")
            role = entry.get("degree" if key == "education" else "title", "")
            lines.append(r"  \resumeSubheading")
            lines.append(f"    {{{entex(org)}}}{{{entex(entry.get('location', ''))}}}")
            lines.append(f"    {{{entex(role)}}}{{{entex(entry.get('years', ''))}}}")
        lines += _write_items(entry)
    lines.append(r"\resumeSubHeadingListEnd")
    return lines


def write_tex(
    resume: dict[str, Any],
    entrygap: float | None = None,
    sectiongap: float | None = None,
) -> str:
    """The .tex source for a resume dict that carries its ``_template``."""
    template = resume.get("_template")
    if not template:
        raise LatexFormatError("resume has no template; build it with merge_tailored or read_tex")
    out = [_write_preamble(resume, entrygap, sectiongap).rstrip("\n"), r"\begin{document}"]
    out += _write_header(resume)
    for section in template["sections"]:
        if section["key"] != "additional" and not resume.get(section["key"]):
            continue
        out += _write_section(resume, section)
    out.append(r"\end{document}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read and write resume .tex files.")
    sub = parser.add_subparsers(dest="command", required=True)

    read = sub.add_parser("read")
    read.add_argument("tex")
    read.add_argument("-o", "--output")

    bullets_cmd = sub.add_parser("bullets")
    bullets_cmd.add_argument("tex")

    validate = sub.add_parser("validate")
    validate.add_argument("tex")

    baseline = sub.add_parser("baseline")
    baseline.add_argument("tex")
    baseline.add_argument("-o", "--output", required=True)

    write = sub.add_parser("write")
    write.add_argument("--master", required=True)
    write.add_argument("--tailored", required=True)
    write.add_argument("-o", "--output", required=True)
    write.add_argument("--projects", type=int)
    write.add_argument("--entrygap", type=float)
    write.add_argument("--sectiongap", type=float)

    args = parser.parse_args(argv)
    try:
        if args.command == "read":
            resume = read_tex(args.tex)
            text = dump_json(resume, args.output)
            if not args.output:
                sys.stdout.write(text)
        elif args.command == "bullets":
            sys.stdout.write(dump_json(bullet_index(read_tex(args.tex))))
        elif args.command == "validate":
            read_tex(args.tex)
            findings = template_findings(Path(args.tex).read_text(encoding="utf-8"))
            sys.stdout.write(dump_json([f.to_dict() for f in findings]))
            return 1 if any(f.severity == "block" for f in findings) else 0
        elif args.command == "baseline":
            Path(args.output).write_text(write_tex(baseline_resume(read_tex(args.tex))), encoding="utf-8")
        else:
            master = read_tex(args.master)
            resume = merge_tailored(load_json(args.tailored), master, args.projects)
            Path(args.output).write_text(
                write_tex(resume, args.entrygap, args.sectiongap), encoding="utf-8"
            )
    except LatexFormatError as exc:
        print(f"latex_io: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LatexFormatError",
    "baseline_resume",
    "bullet_index",
    "detex",
    "entex",
    "merge_tailored",
    "normalize_term",
    "read_tex",
    "read_tex_string",
    "template_findings",
    "write_tex",
]
