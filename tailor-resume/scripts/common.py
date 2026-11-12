"""Helpers shared by the tailor-resume scripts.

Everything here is plain, deterministic Python: findings, entry walking,
tool-name and number comparison, date parsing and word lists.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

from text_match import keyword_in_text

SKILL_DIR = Path(__file__).resolve().parent.parent
REFERENCES = SKILL_DIR / "references"

# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

# critical / block stop the run. "rewrite" stops it on the first try only
# ("rewrite once, then warn"). warn and info only go in the report.
SEVERITIES = ("critical", "block", "rewrite", "warn", "info")


@dataclass
class Finding:
    rule: str
    severity: str
    message: str
    where: str = ""
    value: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def is_blocking(finding: Finding, attempt: int = 1) -> bool:
    if finding.severity in ("critical", "block"):
        return True
    return finding.severity == "rewrite" and attempt <= 1


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump_json(data: Any, path: str | Path | None = None) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    if path is not None:
        Path(path).write_text(text, encoding="utf-8")
    return text


# --------------------------------------------------------------------------
# Walking a resume dict
# --------------------------------------------------------------------------

ENTRY_KINDS = ("workExperience", "personalProjects")


def entry_name(kind: str, entry: dict[str, Any]) -> str:
    return str(entry.get("company") if kind == "workExperience" else entry.get("name") or "")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def entry_id(kind: str, entry: dict[str, Any]) -> str:
    """Stable id for an entry: company or project name, plus the role for jobs."""
    if kind == "workExperience":
        return slug(f"{entry.get('company', '')} {entry.get('title', '')}")
    return slug(str(entry.get("name", "")))


def entries(resume: dict[str, Any]) -> Iterator[tuple[str, int, dict[str, Any]]]:
    for kind in ENTRY_KINDS:
        for index, entry in enumerate(resume.get(kind) or []):
            if isinstance(entry, dict):
                yield kind, index, entry


def bullets(entry: dict[str, Any]) -> list[str]:
    return [b for b in entry.get("description") or [] if isinstance(b, str)]


def master_bullets(entry: dict[str, Any]) -> list[str]:
    """Shown bullets followed by the commented-out alternates."""
    return bullets(entry) + [
        b for b in entry.get("alternates") or [] if isinstance(b, str)
    ]


def entry_source_text(kind: str, entry: dict[str, Any]) -> str:
    """Everything the master says inside one entry: heading, stack, every bullet."""
    parts = [
        entry_name(kind, entry),
        str(entry.get("title") or ""),
        str(entry.get("role") or ""),
        " , ".join(entry.get("stack") or []),
        *master_bullets(entry),
    ]
    return " \n ".join(p for p in parts if p)


def page_view(resume: dict[str, Any]) -> dict[str, Any]:
    """The resume as it prints: no alternates, no template data, no source ids."""
    hidden = {"alternates", "sources", "url", "links"}

    def strip(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: strip(v)
                for k, v in obj.items()
                if not k.startswith("_") and k not in hidden
            }
        if isinstance(obj, list):
            return [strip(v) for v in obj]
        return obj

    return strip(copy.deepcopy(resume))


def page_text(resume: dict[str, Any]) -> str:
    parts: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            for value in obj.values():
                walk(value)

    walk(page_view(resume))
    return " \n ".join(p for p in parts if p)


def find_master_entry(
    kind: str, entry: dict[str, Any], master: dict[str, Any]
) -> dict[str, Any] | None:
    """Match on company or project name, plus the start date when both have one."""
    name = normalize_term(entry_name(kind, entry))
    candidates = [
        m
        for m in master.get(kind) or []
        if isinstance(m, dict) and normalize_term(entry_name(kind, m)) == name
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    start = (parse_dates(str(entry.get("years") or "")) or [None])[0]
    for candidate in candidates:
        if (parse_dates(str(candidate.get("years") or "")) or [None])[0] == start:
            return candidate
    title = normalize_term(str(entry.get("title") or ""))
    for candidate in candidates:
        if normalize_term(str(candidate.get("title") or "")) == title:
            return candidate
    return None


# --------------------------------------------------------------------------
# Tool names
# --------------------------------------------------------------------------

# Spellings of the same tool. C13 picks the posting's spelling; the fact gate
# treats every spelling in a group as the same tool.
ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("PostgreSQL", "Postgres"),
    ("JavaScript", "JS"),
    ("TypeScript", "TS"),
    ("Kubernetes", "K8s"),
    ("React", "React.js", "ReactJS"),
    ("Node.js", "NodeJS", "Node"),
    ("Next.js", "NextJS"),
    ("Vue", "Vue.js", "VueJS"),
    ("Go", "Golang"),
    ("GCP", "Google Cloud Platform", "Google Cloud"),
    ("AWS", "Amazon Web Services"),
    ("CI/CD", "CICD", "CI-CD"),
    ("REST API", "REST APIs", "RESTful API", "RESTful APIs"),
    ("LLM", "LLMs", "large language model", "large language models"),
    ("RAG", "retrieval-augmented generation", "retrieval augmented generation"),
    ("ML", "machine learning"),
    ("NLP", "natural language processing"),
    ("C#", "C Sharp", "CSharp"),
    ("C++", "CPP"),
    (".NET", "dotnet"),
    ("MongoDB", "Mongo"),
    ("scikit-learn", "sklearn"),
    ("Tailwind CSS", "Tailwind", "TailwindCSS"),
    ("Amazon S3", "S3"),
    ("OAuth 2.0", "OAuth2", "OAuth"),
    ("SQL Server", "MSSQL"),
    ("Azure", "Microsoft Azure"),
    ("A/B testing", "A/B tests", "A/B test", "AB testing"),
    ("GitHub Actions", "GH Actions"),
    ("Anthropic API", "Claude API"),
    ("OpenAI API", "GPT API"),
)

# Tools usually written in lowercase, so the capital-letter test misses them.
LOWERCASE_TOOLS = frozenset(
    {
        "dbt", "pandas", "numpy", "scipy", "pytest", "pgvector", "sqlc", "git",
        "bash", "zsh", "npm", "pnpm", "yarn", "kubectl", "helm", "nginx", "jq",
        "curl", "grpc", "redis", "celery", "terraform", "ansible", "webpack",
        "vite", "vitest", "jest", "mocha", "cypress", "playwright", "selenium",
        "matplotlib", "seaborn", "plotly", "keras", "spark", "kafka", "airflow",
        "docker", "linux", "unix", "sql", "html", "css", "scikit-learn",
        "mlflow", "gunicorn", "uvicorn", "alembic", "prisma", "mammoth",
    }
)


def normalize_term(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip()).casefold()


_ALIAS_LOOKUP: dict[str, int] = {
    normalize_term(variant): index
    for index, group in enumerate(ALIAS_GROUPS)
    for variant in group
}


def alias_variants(term: str) -> tuple[str, ...]:
    index = _ALIAS_LOOKUP.get(normalize_term(term))
    return ALIAS_GROUPS[index] if index is not None else (term,)


def _plural_forms(term: str) -> set[str]:
    forms = {term}
    low = term.lower()
    if len(low) > 3 and low.endswith("s") and not low.endswith("ss"):
        forms.add(term[:-1])
    elif len(low) > 2 and low[-1:].isalpha():
        forms.add(term + "s")
    return forms


def term_in_text(term: str, text: str) -> bool:
    """Whole-term match that also accepts aliases and a plural "s"."""
    # "data quality" also matches "data-quality": compare with hyphens as spaces too.
    spaced = text.replace("-", " ") if "-" in text else None
    for variant in alias_variants(term):
        for form in _plural_forms(variant):
            if keyword_in_text(form, text):
                return True
            if spaced is not None and keyword_in_text(form.replace("-", " "), spaced):
                return True
    return False


def same_tool(a: str, b: str) -> bool:
    na, nb = normalize_term(a), normalize_term(b)
    if na == nb:
        return True
    ia, ib = _ALIAS_LOOKUP.get(na), _ALIAS_LOOKUP.get(nb)
    return ia is not None and ia == ib


def looks_like_tool(term: str) -> bool:
    """A product or technology name, as opposed to a soft keyword like "teamwork"."""
    stripped = term.strip()
    if not stripped:
        return False
    if normalize_term(stripped) in LOWERCASE_TOOLS or normalize_term(stripped) in _ALIAS_LOOKUP:
        return True
    return bool(re.search(r"[A-Z0-9+#./]", stripped))


def skill_list(resume: dict[str, Any]) -> list[str]:
    skills = (resume.get("additional") or {}).get("technicalSkills") or []
    return [s for s in skills if isinstance(s, str) and s.strip()]


def tool_vocabulary(
    master: dict[str, Any],
    job: dict[str, Any] | None = None,
    confirmed: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Known tool names: master skills and stacks, confirmed skills, and the
    tool-like skills a posting asks for. Longest first."""
    terms: dict[str, None] = {}
    for skill in skill_list(master):
        terms.setdefault(skill)
    for kind in ENTRY_KINDS:
        for entry in master.get(kind) or []:
            for item in entry.get("stack") or []:
                terms.setdefault(item)
    for skill in confirmed:
        terms.setdefault(skill)
    requirements = (job or {}).get("requirements") or {}
    for key in ("required_skills", "preferred_skills"):
        for skill in requirements.get(key) or []:
            if isinstance(skill, str) and looks_like_tool(skill):
                terms.setdefault(skill)
    cleaned = [t.strip() for t in terms if t and t.strip()]
    return sorted(dict.fromkeys(cleaned), key=lambda t: (-len(t), t.lower()))


def mask_terms(text: str, terms: list[str]) -> str:
    """Blank out known tool names so word checks don't trip on them."""
    masked = text
    for term in terms:
        for variant in alias_variants(term):
            pattern = rf"(?<!\w){re.escape(variant)}(?!\w)"
            masked = re.sub(pattern, lambda m: " " * len(m.group(0)), masked, flags=re.IGNORECASE)
    return masked


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------

_NUMBER_RE = re.compile(
    r"(?<![\w.])(?P<num>\d[\d,]*(?:\.\d+)?)(?P<suffix>\s?(?:[KkMB]|bn)\b)?"
)
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "single": 1, "twice": 2, "half": 0.5,
}
_MULTIPLIER = {"k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9}


@dataclass(frozen=True)
class Number:
    literal: str
    value: float
    decimals: int  # decimals written in front of a K/M/B suffix, else -1
    scale: float


def numbers_in(text: str) -> list[Number]:
    found: list[Number] = []
    for match in _NUMBER_RE.finditer(text):
        raw = match.group("num").rstrip(",")
        digits = raw.replace(",", "")
        if not digits or digits == ".":
            continue
        suffix = (match.group("suffix") or "").strip().lower()
        scale = _MULTIPLIER.get(suffix, 1.0)
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        try:
            value = float(digits) * scale
        except ValueError:
            continue
        found.append(
            Number(
                literal=match.group(0).strip(),
                value=value,
                decimals=decimals if suffix else -1,
                scale=scale,
            )
        )
    return found


def source_number_values(text: str) -> set[float]:
    """Numbers a source text supports, including ones it spells out."""
    values = {n.value for n in numbers_in(text)}
    for word in re.findall(r"[A-Za-z]+", text):
        if word.lower() in _NUMBER_WORDS:
            values.add(float(_NUMBER_WORDS[word.lower()]))
    return values


def number_supported(number: Number, source_values: set[float]) -> bool:
    """True when the source has this number, exactly or as the value a K/M
    figure was shortened from ("4.5K" for 4,547; "2K" for 2,000)."""
    for value in source_values:
        if abs(value - number.value) < 1e-9:
            return True
    if number.scale > 1 and number.decimals >= 0:
        factor = 10**number.decimals
        written = round(number.value / number.scale * factor)
        for value in source_values:
            scaled = value / number.scale * factor
            if written in (round(scaled), int(scaled)) and value >= number.scale:
                return True
    return False


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DATE_TOKEN = re.compile(
    r"(?i)\b(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+(?P<my>(?:19|20)\d{2})\b"
    r"|\b(?P<present>present|current|now|ongoing)\b"
    r"|\b(?P<year>(?:19|20)\d{2})\b"
)


def parse_dates(text: str) -> list[tuple[int, int] | str]:
    """Dates in order as (month, year); month 0 for a bare year; "present"."""
    out: list[tuple[int, int] | str] = []
    for match in _DATE_TOKEN.finditer(text or ""):
        if match.group("month"):
            out.append((MONTHS[match.group("month").lower()], int(match.group("my"))))
        elif match.group("present"):
            out.append("present")
        else:
            out.append((0, int(match.group("year"))))
    return out


def format_date_range(text: str) -> str:
    """Rewrite a date range in the one house format: "Aug 2022 – May 2026"."""
    parts = []
    for item in parse_dates(text):
        if item == "present":
            parts.append("Present")
        else:
            month, year = item  # type: ignore[misc]
            parts.append(f"{MONTH_ABBR[month - 1]} {year}" if month else str(year))
    return " – ".join(parts) if parts else text.strip()


# --------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")

IRREGULAR_PAST = frozenset(
    """
    built led wrote ran cut made drove grew won set kept sent took brought taught
    found held rebuilt began broke chose drew gave got hit laid met paid put read
    rewrote saw sold shrank spent split stood sped spun struck swept told thought
    understood oversaw overcame beat bore bound caught dealt did fed fought fit
    forecast froze hid left lost meant quit rose shot shut sought stuck threw
    undertook withdrew wove underwent foresaw rethought reset upheld withheld
    underwrote rang sang sank spoke stole swore tore wore woke lit slid bent lent
    """.split()
)


def words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def first_word(text: str) -> str:
    found = words(text)
    return found[0] if found else ""


def is_past_tense(word: str) -> bool:
    low = word.lower()
    return low in IRREGULAR_PAST or (len(low) > 3 and low.endswith("ed"))


def content_words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9+#.-]*", text) if len(w) > 3}


def best_source(text: str, master_entry: dict[str, Any]) -> tuple[int, str, float]:
    """The master bullet a tailored bullet most likely came from: (index, text, overlap)."""
    target = content_words(text)
    best = (-1, "", 0.0)
    for index, candidate in enumerate(master_bullets(master_entry)):
        other = content_words(candidate)
        if not target or not other:
            continue
        overlap = len(target & other) / len(target | other)
        if overlap > best[2]:
            best = (index, candidate, round(overlap, 3))
    return best


def read_word_file(path: str | Path | None) -> list[str]:
    """One entry per line; blank lines and # comments ignored."""
    if not path or not Path(path).is_file():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def read_confirmed_skills(path: str | Path | None) -> list[str]:
    """Skills from confirmed-skills.md: one "- Skill | confirmed YYYY-MM-DD" per line."""
    if not path or not Path(path).is_file():
        return []
    skills: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*[-*]\s+(.*)", line)
        if not match:
            continue
        name = match.group(1).split("|", 1)[0].strip().strip("`*")
        if name and name not in skills:
            skills.append(name)
    return skills


def load_resume(path: str | Path) -> dict[str, Any]:
    """Read a resume from .tex (through the LaTeX reader) or .json."""
    path = Path(path)
    if path.suffix.lower() == ".tex":
        from latex_io import read_tex

        return read_tex(path)
    return load_json(path)
