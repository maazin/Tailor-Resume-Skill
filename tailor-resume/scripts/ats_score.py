"""ATS score out of 10, computed in code from the resume dict and the posting.

Modified from Resume-Matcher (https://github.com/srbhr/Resume-Matcher),
apps/backend/app/services/ats.py, Apache-2.0.
Changes:
  - Input comes from the LaTeX reader; fields that never print (alternate
    bullets, template data, link targets) are removed before scoring.
  - Skills and title matching use the alias table in common.py.
  - The recommendations text was dropped; the report writes its own.
  - ``score_resume`` wraps the keyword math and returns the score out of 10
    together with the five sub-scores and the potential keyword match.

Every component is deterministic (no LLM call):
  - keyword_match 40%: share of the posting's keywords found on the page
  - skills_coverage 20%: required and preferred skills found on the page
  - title_match 15%: the posting's exact job title in the headline
  - section_completeness 15%: contact info plus summary, experience,
    education and skills sections
  - date_consistency 10%: share of dates written in one format
A sub-score that cannot be measured is None and its weight is shared out.

CLI:
  ats_score.py --job job.json --resume resume.json|resume.tex --master master.tex
               [--confirmed confirmed-skills.md]
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from common import (
    dump_json,
    load_json,
    load_resume,
    page_view,
    read_confirmed_skills,
    term_in_text,
)
from keywords import analyze_keyword_gaps, calculate_keyword_match
from text_match import keyword_in_text

# Weights must sum to 1.0
WEIGHTS: dict[str, float] = {
    "keyword_match": 0.40,
    "skills_coverage": 0.20,
    "title_match": 0.15,
    "section_completeness": 0.15,
    "date_consistency": 0.10,
}

_SECTION_PATTERNS = {
    "summary": ["summary", "objective", "profile", "about"],
    "experience": ["experience", "work history", "employment"],
    "education": ["education", "academic", "degree"],
    "skills": ["skills", "technologies", "competencies", "technical"],
}

# Title-match scores: exact title in the headline is what recruiters' title
# filters hit; the summary is a weaker placement; word overlap is a near miss.
_TITLE_IN_HEADLINE = 100.0
_TITLE_IN_SUMMARY = 75.0
_TITLE_OVERLAP_MAX = 50.0
_TITLE_STOPWORDS = frozenset({"a", "an", "and", "the", "of", "for", "in", "to", "at", "&"})

# Date styles, tried left to right at each position. Only English month names
# and abbreviations count as month dates.
_YEAR = r"(?:19|20)\d{2}"
_MONTH = (
    r"(?i:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DATE_PATTERN = re.compile(
    rf"(?P<iso>\b{_YEAR}[-/]\d{{1,2}}\b)"
    rf"|(?P<numeric>\b\d{{1,2}}[/.-]{_YEAR}\b)"
    rf"|(?P<name_year>\b{_MONTH}\.?,?\s+{_YEAR}\b)"
    rf"|(?P<name_short_year>\b{_MONTH}\.?\s+'\d{{2}}\b)"
    rf"|(?P<year>\b{_YEAR}\b)"
)
_MIN_DATES_FOR_CONSISTENCY = 2


def _extract_all_text(data: dict[str, Any]) -> str:
    """Flatten all string values from a resume dict into a single text block."""
    parts: list[str] = []

    def _walk(obj: Any) -> None:
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)

    _walk(data)
    return " ".join(parts)


def _title_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[^\W_]+", text.lower())
        if token not in _TITLE_STOPWORDS
    }


def _is_section_visible(resume: dict[str, Any], key: str) -> bool:
    for meta in resume.get("sectionMeta") or []:
        if isinstance(meta, dict) and meta.get("key") == key:
            return meta.get("isVisible", True) is not False
    return True


def _rendered_content(resume: dict[str, Any]) -> dict[str, Any]:
    """The resume minus what never reaches the page, so an ATS cannot read it."""
    content = {
        key: value
        for key, value in page_view(resume).items()
        if key != "sectionMeta" and _is_section_visible(resume, key)
    }
    return content


def _compute_skills_coverage(resume: dict[str, Any], job_keywords: dict[str, Any]) -> float:
    """Share of the posting's required and preferred skills on the page (0-100)."""
    jd_skills: list[str] = []
    jd_skills.extend(job_keywords.get("required_skills") or [])
    jd_skills.extend(job_keywords.get("preferred_skills") or [])
    jd_skills = [s for s in jd_skills if isinstance(s, str) and s.strip()]
    if not jd_skills:
        return 0.0

    content = _rendered_content(resume)
    resume_skills: list[str] = (content.get("additional") or {}).get("technicalSkills", []) or []
    resume_text = _extract_all_text(content).lower()
    resume_skills_lower = {s.lower() for s in resume_skills if isinstance(s, str)}

    matched = 0
    for skill in jd_skills:
        if skill.lower() in resume_skills_lower or term_in_text(skill, resume_text):
            matched += 1
    return min(100.0, matched / len(jd_skills) * 100)


def compute_title_match(resume: dict[str, Any], job_keywords: dict[str, Any]) -> float | None:
    """How literally the posting's job title appears (0-100), or None.

    100 when the exact title is in the headline, 75 when it is only in the
    summary, and up to 50 for partial word overlap with the headline.
    """
    role = job_keywords.get("role")
    if not isinstance(role, str) or not role.strip():
        return None

    personal_info = resume.get("personalInfo") or {}
    headline = str(personal_info.get("title") or "")
    summary = str(resume.get("summary") or "") if _is_section_visible(resume, "summary") else ""

    if keyword_in_text(role, headline.lower()):
        return _TITLE_IN_HEADLINE
    if keyword_in_text(role, summary.lower()):
        return _TITLE_IN_SUMMARY

    role_tokens = _title_tokens(role)
    if not role_tokens:
        return 0.0
    overlap = len(role_tokens & _title_tokens(headline)) / len(role_tokens)
    return round(overlap * _TITLE_OVERLAP_MAX, 1)


def _collect_date_strings(resume: dict[str, Any]) -> list[str]:
    entries: list[Any] = []
    for key in ("workExperience", "education", "personalProjects"):
        if _is_section_visible(resume, key):
            entries.extend(resume.get(key) or [])
    return [
        entry["years"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("years"), str)
    ]


def compute_date_consistency(resume: dict[str, Any]) -> float | None:
    """Share of dates written in the dominant format (0-100), or None when the
    resume has fewer than two dates. "Present" markers are ignored."""
    style_counts: dict[str, int] = {}
    for value in _collect_date_strings(resume):
        for match in _DATE_PATTERN.finditer(value):
            style = match.lastgroup
            if style:
                style_counts[style] = style_counts.get(style, 0) + 1

    total = sum(style_counts.values())
    if total < _MIN_DATES_FOR_CONSISTENCY:
        return None
    return round(max(style_counts.values()) / total * 100, 1)


def compute_section_completeness(resume: dict[str, Any]) -> float:
    """Contact info plus the summary, experience, education and skills sections (0-100)."""
    personal_info = resume.get("personalInfo") or {}
    has_contact = bool(personal_info.get("email") or personal_info.get("phone"))
    sections_found = sum(
        [
            bool(resume.get("summary")) and _is_section_visible(resume, "summary"),
            bool(resume.get("workExperience")) and _is_section_visible(resume, "workExperience"),
            bool(resume.get("education")) and _is_section_visible(resume, "education"),
            bool((resume.get("additional") or {}).get("technicalSkills"))
            and _is_section_visible(resume, "additional"),
        ]
    )
    if sections_found:
        return (sections_found + has_contact) / (len(_SECTION_PATTERNS) + 1) * 100

    text = _extract_all_text(_rendered_content(resume)).lower()
    headings_found = sum(
        1 for patterns in _SECTION_PATTERNS.values() if any(p in text for p in patterns)
    )
    if has_contact:
        return (headings_found + 1) / (len(_SECTION_PATTERNS) + 1) * 100
    return headings_found / len(_SECTION_PATTERNS) * 100


def _in_posting_order(keywords: list[str], job_keywords: dict[str, Any]) -> list[str]:
    rank: dict[str, int] = {}
    for key in ("required_skills", "preferred_skills", "keywords"):
        for keyword in job_keywords.get(key) or []:
            if isinstance(keyword, str):
                rank.setdefault(keyword.lower(), len(rank))
    return sorted(keywords, key=lambda k: (rank.get(k.lower(), len(rank)), k.lower()))


def compute_ats_score(
    refined_resume: dict[str, Any],
    job_keywords: dict[str, Any],
    keyword_match_percentage: float,
    missing_keywords: list[str],
    injectable_keywords: list[str],
) -> dict[str, Any]:
    """The 0-100 score with its five sub-scores (unscorable ones are None)."""
    missing_keywords = _in_posting_order(missing_keywords, job_keywords)
    injectable_keywords = _in_posting_order(injectable_keywords, job_keywords)
    sub_scores: dict[str, float | None] = {
        "keyword_match": min(100.0, max(0.0, keyword_match_percentage)),
        "skills_coverage": _compute_skills_coverage(refined_resume, job_keywords),
        "title_match": compute_title_match(refined_resume, job_keywords),
        "section_completeness": compute_section_completeness(refined_resume),
        "date_consistency": compute_date_consistency(refined_resume),
    }

    scored = {key: value for key, value in sub_scores.items() if value is not None}
    total_weight = sum(WEIGHTS[key] for key in scored)
    overall = (
        sum(value * WEIGHTS[key] for key, value in scored.items()) / total_weight
        if total_weight
        else 0.0
    )

    return {
        "overall_score": round(overall, 1),
        "sub_scores": {
            key: (round(value, 1) if value is not None else None)
            for key, value in sub_scores.items()
        },
        "missing_keywords": missing_keywords,
        "injectable_keywords": injectable_keywords,
    }


def score_resume(
    resume: dict[str, Any],
    master: dict[str, Any],
    job: dict[str, Any],
    confirmed: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """ATS score out of 10 for one resume against one posting."""
    requirements = dict(job.get("requirements") or {})
    if not requirements.get("role") and job.get("title"):
        requirements["role"] = job["title"]
    gaps = analyze_keyword_gaps(requirements, resume, master, confirmed)
    match = calculate_keyword_match(resume, requirements)
    result = compute_ats_score(
        resume, requirements, match, gaps.non_injectable_keywords, gaps.injectable_keywords
    )
    return {
        "score_out_of_10": round(result["overall_score"] / 10, 1),
        "score_out_of_100": result["overall_score"],
        "sub_scores": result["sub_scores"],
        "keyword_match_percent": round(match, 1),
        "potential_match_percent": round(gaps.potential_match_percentage, 1),
        "missing_not_in_master": result["missing_keywords"],
        "missing_but_available": result["injectable_keywords"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ATS score out of 10 for one resume.")
    parser.add_argument("--job", required=True)
    parser.add_argument("--resume", required=True)
    parser.add_argument("--master", required=True)
    parser.add_argument("--confirmed")
    args = parser.parse_args(argv)
    result = score_resume(
        load_resume(args.resume),
        load_resume(args.master),
        load_json(args.job),
        read_confirmed_skills(args.confirmed),
    )
    sys.stdout.write(dump_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
