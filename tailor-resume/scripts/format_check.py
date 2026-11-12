"""Resume content rules C1 to C14, checked in code.

Header
  C1  the job title line is the posting's title, cleaned of req numbers,
      "(Remote)" and location tags
  C2  one city, not remote: "Relocating to City, ST"
  C3  several cities, or remote is an option: no relocating line
  C4  the one city is where the user already lives: no relocating line
Bullets
  C5  every experience and project entry has the same number of bullets, N
  C6  N is 3, or 4 when every shown bullet scores 60+ and the page fits
  C7  never pad an entry whose master has fewer than N bullets
  C8  past-tense action verb first       C9  past tense everywhere
  C10 no I/my/we, no final period, no bold or italics
  C11 2 printed lines, second at least half full (pre-check by length here;
      layout_check.py measures the real page)
  C12 digits, with K for thousands and M for millions
  C13 tool names spelled the way the posting spells them
  C14 dates as "Aug 2022 \u2013 May 2026", three-letter months, "Present"

CLI:
  format_check.py --master master.tex --tailored tailored.json --job job.json
Exit code 1 when any finding blocks.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from common import (
    ALIAS_GROUPS,
    ENTRY_KINDS,
    Finding,
    bullets,
    dump_json,
    entries,
    entry_name,
    find_master_entry,
    first_word,
    is_blocking,
    is_past_tense,
    load_json,
    master_bullets,
    page_text,
)
from latex_io import merge_tailored, read_tex
from text_match import keyword_in_text

MIN_BULLETS = 3
MAX_BULLETS = 4
MIN_SCORE_FOR_FOUR = 60
# Rough length of a bullet that prints on two lines; the layout check decides.
LENGTH_HINT = (190, 285)

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
_STATE_CODES = set(US_STATES.values())
_COUNTRY_WORDS = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}
_WORKPLACE_WORDS = r"remote|hybrid|on-?site|in-?office|work from home|wfh|telecommute"

# --------------------------------------------------------------------------
# C1: job title
# --------------------------------------------------------------------------

_TAG_WORDS = re.compile(
    rf"(?i)\b(?:{_WORKPLACE_WORDS}|req(?:uisition)?\.?\s*(?:id|no|number|#)?\s*[:#-]?\s*[\w-]*\d[\w-]*"
    r"|job\s*(?:id|code|number)\s*[:#-]?\s*[\w-]+|r-?\d{3,}|#\s*\d+|(?!(?:19|20)\d{2}\b)\d{4,}"
    r"|full[- ]time|part[- ]time|contract|temporary|us|usa|united states|emea|apac|latam|canada|uk)\b"
)
_CITY_STATE = re.compile(r"^[A-Z][A-Za-z .'-]+,\s*(?:[A-Z]{2}|[A-Z][a-z]+(?: [A-Z][a-z]+)*)$")


def _is_tag(segment: str, locations: list[str]) -> bool:
    """True when a piece of the title is a req number, workplace or location tag."""
    text = segment.strip(" -\u2013\u2014|,:/")
    if not text:
        return True
    low = text.lower()
    for location in locations:
        parts = [p.strip().lower() for p in location.split(",") if p.strip()]
        if low == location.lower() or (parts and low == parts[0]):
            return True
    if _CITY_STATE.match(text) and text.split(",")[-1].strip() in _STATE_CODES | {s.title() for s in US_STATES}:
        return True
    remainder = _TAG_WORDS.sub("", re.sub(r"#\s*\d+", "", text))
    return not re.sub(r"[\s\-\u2013\u2014|,:/()&]+|\b(?:or|and|in|based)\b", "", remainder, flags=re.IGNORECASE)


def clean_job_title(title: str, locations: list[str] | None = None) -> str:
    """ "Data Engineer II - Req 4471 (Remote)" -> "Data Engineer II"."""
    locations = locations or []
    text = re.sub(r"\s+", " ", title or "").strip()

    def drop_tag_group(match: re.Match[str]) -> str:
        return " " if _is_tag(match.group(1), locations) else match.group(0)

    text = re.sub(r"\s*[(\[]([^()\[\]]*)[)\]]", drop_tag_group, text)
    # Peel tag segments off the end: " - Req 4471", " | Remote", ", Austin, TX".
    changed = True
    while changed:
        changed = False
        for pattern in (r"^(.*\S)\s+[-\u2013\u2014|]\s+(.+)$", r"^(.*\S)\s*[,:]\s+(.+)$", r"^(.*\S)\s*/\s*(.+)$"):
            match = re.match(pattern, text)
            if match and _is_tag(match.group(2), locations):
                text = match.group(1)
                changed = True
                break
        # A bare id at the end with no separator: "Backend Engineer #88213".
        bare = re.match(r"^(.*\S)\s+(\S*\d\S*)$", text)
        if not changed and bare and _is_tag(bare.group(2), locations):
            text = bare.group(1)
            changed = True
    return re.sub(r"\s+", " ", text).strip(" -\u2013\u2014|,:/")


# --------------------------------------------------------------------------
# C2 to C4: location
# --------------------------------------------------------------------------


def format_city(location: str) -> str:
    """ "Austin, Texas, United States" -> "Austin, TX"."""
    parts = [p.strip() for p in re.sub(r"\s+", " ", location).split(",") if p.strip()]
    parts = [p for p in parts if p.lower() not in _COUNTRY_WORDS] or parts
    if not parts:
        return ""
    city = parts[0]
    if len(parts) == 1:
        return city
    region = parts[1]
    if region.upper() in _STATE_CODES and len(region) == 2:
        return f"{city}, {region.upper()}"
    if region.lower() in US_STATES:
        return f"{city}, {US_STATES[region.lower()]}"
    return f"{city}, {region}"


def _city_key(location: str) -> str:
    formatted = format_city(re.sub(r"(?i)^relocating to\s+", "", location or ""))
    city = formatted.split(",")[0].strip().lower()
    aliases = {"new york city": "new york", "nyc": "new york", "sf": "san francisco", "washington dc": "washington", "washington, d.c.": "washington"}
    return aliases.get(city, city)


def posting_cities(job: dict[str, Any]) -> tuple[list[str], bool]:
    """The distinct cities a posting names, and whether remote is an option."""
    remote = str(job.get("workplace_type") or "").lower() == "remote"
    cities: dict[str, str] = {}
    for raw in job.get("locations") or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        if re.search(r"(?i)\bremote\b|\banywhere\b|work from home", raw):
            remote = True
            raw = re.sub(r"(?i)[\s(/-]*\b(?:remote|anywhere|work from home)\b[\s)/-]*", " ", raw).strip(" ,;-")
            raw = re.sub(r"(?i)^(?:in|within|from)\s+", "", raw)
            if not raw or raw.lower() in _COUNTRY_WORDS or raw.lower() in US_STATES:
                continue
        raw = re.sub(r"\s*\([^)]*\)", "", raw).strip(" ,;-")  # "New York, NY (HQ)"
        if not raw or raw.lower() in _COUNTRY_WORDS:
            continue
        key = _city_key(raw)
        if key:
            cities.setdefault(key, format_city(raw))
    return list(cities.values()), remote


def expected_location(job: dict[str, Any], home: str) -> tuple[str, str, bool]:
    """(location line, rule, whether the home city was needed but unknown)."""
    home = re.sub(r"(?i)^relocating to\s+", "", home or "").strip()
    cities, remote = posting_cities(job)
    if remote:
        return home, "C3", False
    if len(cities) != 1:
        return home, "C3", False
    if home and _city_key(cities[0]) == _city_key(home):
        return home, "C4", False
    return f"Relocating to {cities[0]}", "C2", not home


def check_header(resume: dict[str, Any], master: dict[str, Any], job: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    info = resume.get("personalInfo") or {}
    posting_title = str(job.get("title") or (job.get("requirements") or {}).get("role") or "")
    if not posting_title.strip():
        findings.append(Finding("C1", "warn", "job.json has no title, so the job title line cannot be checked", where="header"))
    else:
        wanted = clean_job_title(posting_title, [l for l in job.get("locations") or [] if isinstance(l, str)])
        got = re.sub(r"\s+", " ", str(info.get("title") or "")).strip()
        if got != wanted:
            findings.append(
                Finding("C1", "block", f"the job title line reads '{got}'; it must read '{wanted}' (the posting's title)",
                        where="header", value=got)
            )

    home = str((master.get("personalInfo") or {}).get("location") or "")
    wanted_location, rule, home_unknown = expected_location(job, home)
    got_location = re.sub(r"\s+", " ", str(info.get("location") or "")).strip()
    if got_location != wanted_location:
        reason = {
            "C2": "the posting names exactly one city and is not remote",
            "C3": "the posting lists several cities or allows remote, so the master location stays",
            "C4": "the posting's city is where the master says you live",
        }[rule]
        findings.append(
            Finding(rule, "block",
                    f"the header location reads '{got_location or '(none)'}'; it must read "
                    f"'{wanted_location or '(none)'}' because {reason}",
                    where="header", value=got_location)
        )
    if home_unknown:
        findings.append(
            Finding("C4", "warn",
                    "master.tex has no home location in its header, so the check cannot tell whether you already "
                    "live in the posting's city. Ask the user once and add the city to the master's contact line",
                    where="header")
        )
    return findings


# --------------------------------------------------------------------------
# C5 to C14: bullets
# --------------------------------------------------------------------------

_PRONOUNS = re.compile(r"\b(?:I(?![/-])|[Mm]y|[Mm]yself|[Ww]e|[Oo]ur|[Oo]urs)\b")
_MARKUP = re.compile(r"\\(?:textbf|textit|emph|underline|bfseries|itshape)\b|\*\*|__|</?(?:b|i|em|strong)>|(?<![\w*])\*[^\s*][^*]*\*(?![\w*])")
_NUMBER_WORDS = re.compile(
    r"(?i)\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen"
    r"|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
    r"|hundred|thousand|million|billion)\b"
)
_GROUPED = re.compile(r"(?<![\w.,])\d{1,3}(?:,\d{3})+(?![\d])")
_LONG_BARE = re.compile(r"(?<![\w.,$#/-])\d{4,}(?![\w.,-]|\s?(?:px|ms|kb|mb|gb))", re.IGNORECASE)
_LOWER_SUFFIX = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s?(?:k|m)\b(?!\w)")
_DATE_OK = re.compile(
    r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (?:19|20)\d{2}"
    r"(?: \u2013 (?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (?:19|20)\d{2}|Present))?$"
)


def _where(kind: str, entry: dict[str, Any], index: int | None = None) -> str:
    name = entry_name(kind, entry)
    return name if index is None else f"{name} bullet {index + 1}"


def check_counts(
    resume: dict[str, Any], master: dict[str, Any], scores: dict[str, list[float]] | None = None
) -> list[Finding]:
    """C5, C6 and C7."""
    findings: list[Finding] = []
    counts = [(kind, entry, len(bullets(entry))) for kind, _, entry in entries(resume)]
    if not counts:
        return [Finding("C5", "block", "the resume has no experience or project entries", where="bullets")]
    distinct = {count for _, _, count in counts}
    if len(distinct) > 1:
        detail = ", ".join(f"{_where(kind, entry)}: {count}" for kind, entry, count in counts)
        findings.append(Finding("C5", "block", f"every entry must have the same number of bullets ({detail})", where="bullets"))
    n = max(distinct)
    if min(distinct) < MIN_BULLETS:
        findings.append(Finding("C6", "block", f"every entry needs at least {MIN_BULLETS} bullets", where="bullets"))
    if n > MAX_BULLETS:
        findings.append(Finding("C6", "block", f"{n} bullets per entry; N is {MIN_BULLETS}, or {MAX_BULLETS} at most", where="bullets"))
    if n == MAX_BULLETS and len(distinct) == 1:
        if scores is None:
            findings.append(
                Finding("C6", "info",
                        f"N is {MAX_BULLETS}: allowed only when every shown bullet scores {MIN_SCORE_FOR_FOUR}+ (pass --scores to check)",
                        where="bullets")
            )
        else:
            for kind, entry, _ in counts:
                low = [s for s in scores.get(entry_name(kind, entry), []) if s < MIN_SCORE_FOR_FOUR]
                if low or len(scores.get(entry_name(kind, entry), [])) < MAX_BULLETS:
                    findings.append(
                        Finding("C6", "block",
                                f"N is {MAX_BULLETS} but this entry does not have {MAX_BULLETS} bullets scoring "
                                f"{MIN_SCORE_FOR_FOUR}+; use {MIN_BULLETS}",
                                where=_where(kind, entry))
                    )
    for kind, entry, count in counts:
        source = find_master_entry(kind, entry, master)
        if source is None:
            continue
        available = len(master_bullets(source))
        if count > available:
            action = (
                "drop this project" if kind == "personalProjects"
                else "stop and ask the user to add a bullet to master.tex"
            )
            findings.append(
                Finding("C7", "block",
                        f"shows {count} bullets but the master has only {available} for this entry; "
                        f"never pad with made-up bullets: {action}",
                        where=_where(kind, entry))
            )
    return findings


def check_bullet_text(resume: dict[str, Any]) -> list[Finding]:
    """C8 to C12."""
    findings: list[Finding] = []
    for kind, _, entry in entries(resume):
        for index, text in enumerate(bullets(entry)):
            where = _where(kind, entry, index)
            lead = first_word(text)
            if not lead or not lead[0].isupper() or not is_past_tense(lead):
                findings.append(
                    Finding("C8", "block",
                            f"starts with '{lead}'; every bullet starts with a past-tense action verb "
                            "(Built, Reduced, Led), including the current role (C9)",
                            where=where, value=lead)
                )
            pronoun = _PRONOUNS.search(text)
            if pronoun:
                findings.append(Finding("C10", "block", f"uses '{pronoun.group(0)}'; no I, my or we", where=where, value=pronoun.group(0)))
            if text.rstrip().endswith("."):
                findings.append(Finding("C10", "block", "ends with a period; bullets have no final period", where=where))
            if _MARKUP.search(text):
                findings.append(Finding("C10", "block", "has bold or italic markup; bullets are plain text", where=where, value=_MARKUP.search(text).group(0)))
            if not LENGTH_HINT[0] <= len(text) <= LENGTH_HINT[1]:
                findings.append(
                    Finding("C11", "warn",
                            f"{len(text)} characters; a bullet that prints on exactly 2 lines is usually "
                            f"{LENGTH_HINT[0]} to {LENGTH_HINT[1]}. The layout check measures the real page",
                            where=where, value=str(len(text)))
                )
            spelled = _NUMBER_WORDS.search(text)
            if spelled:
                findings.append(
                    Finding("C12", "block", f"'{spelled.group(0)}' is spelled out; write numbers as digits", where=where, value=spelled.group(0))
                )
            for pattern, advice in (
                (_GROUPED, "use K for thousands and M for millions (2,000 -> 2K)"),
                (_LOWER_SUFFIX, "write the K or M suffix in capitals"),
            ):
                match = pattern.search(text)
                if match:
                    findings.append(Finding("C12", "block", f"'{match.group(0)}': {advice}", where=where, value=match.group(0)))
            for match in _LONG_BARE.finditer(text):
                value = int(match.group(0))
                if 1900 <= value <= 2099:
                    continue
                findings.append(
                    Finding("C12", "block", f"'{match.group(0)}': use K for thousands and M for millions",
                            where=where, value=match.group(0))
                )
    return findings


def check_spelling(resume: dict[str, Any], job: dict[str, Any]) -> list[Finding]:
    """C13: when the posting and the resume name the same tool, use the posting's spelling."""
    requirements = job.get("requirements") or {}
    posting = " \n ".join(
        [str(job.get("description_text") or "")]
        + [str(v) for key in ("required_skills", "preferred_skills", "keywords") for v in requirements.get(key) or []]
    )
    resume_text = page_text(
        {k: resume.get(k) for k in ("workExperience", "personalProjects", "additional", "skillGroups")}
    )
    findings: list[Finding] = []
    for group in ALIAS_GROUPS:
        in_posting = [v for v in group if keyword_in_text(v, posting)]
        in_resume = [v for v in group if keyword_in_text(v, resume_text)]
        if not in_posting or not in_resume:
            continue
        if any(keyword_in_text(v, resume_text) for v in in_posting):
            continue
        findings.append(
            Finding("C13", "block",
                    f"the resume says '{in_resume[0]}' but the posting spells it '{in_posting[0]}'; use the posting's spelling",
                    where="tool names", value=in_resume[0])
        )
    return findings


def check_dates(resume: dict[str, Any]) -> list[Finding]:
    """C14."""
    findings: list[Finding] = []
    for key, name_field in (("education", "institution"), ("workExperience", "company"), ("personalProjects", "name")):
        for entry in resume.get(key) or []:
            years = str(entry.get("years") or "").strip()
            if years and not _DATE_OK.match(years):
                findings.append(
                    Finding("C14", "block",
                            f"dates '{years}' are not in the format 'Aug 2022 \u2013 May 2026' "
                            "(three-letter months, an en dash, 'Present' for current)",
                            where=str(entry.get(name_field, "")), value=years)
                )
    return findings


def run_format_check(
    resume: dict[str, Any],
    master: dict[str, Any],
    job: dict[str, Any],
    scores: dict[str, list[float]] | None = None,
) -> list[Finding]:
    return (
        check_header(resume, master, job)
        + check_counts(resume, master, scores)
        + check_bullet_text(resume)
        + check_spelling(resume, job)
        + check_dates(resume)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Content rules C1-C14.")
    parser.add_argument("--master", required=True)
    parser.add_argument("--tailored", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--title", help="print the cleaned job title for this raw title and exit")
    args = parser.parse_args(argv)
    job = load_json(args.job)
    master = read_tex(args.master)
    resume = merge_tailored(load_json(args.tailored), master)
    findings = run_format_check(resume, master, job)
    sys.stdout.write(dump_json([f.to_dict() for f in findings]))
    return 1 if any(is_blocking(f) for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ENTRY_KINDS",
    "check_header",
    "clean_job_title",
    "expected_location",
    "format_city",
    "posting_cities",
    "run_format_check",
]
