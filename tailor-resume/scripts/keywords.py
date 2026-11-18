"""Keyword match, the gap split and the master-alignment check.

Modified from Resume-Matcher (https://github.com/srbhr/Resume-Matcher),
apps/backend/app/services/refiner.py, Apache-2.0. Functions taken:
analyze_keyword_gaps, calculate_keyword_match, job_keyword_terms,
validate_master_alignment and _extract_all_text.
Changes:
  - Pydantic result models replaced with dataclasses; no LLM code kept.
  - Keywords are walked in posting order instead of set order, so output is
    the same on every run.
  - _extract_all_text also reads a project's tech stack and the headline.
  - The alias table in common.py counts "Postgres" as "PostgreSQL".
  - Missing keywords the master lacks are not dropped: they feed the one
    gap-check question, minus skills already in confirmed-skills.md.
  - validate_master_alignment takes confirmed skills as its allow-list, also
    checks schools, and matches variants on whole terms ("Python 3" for
    "Python") instead of substrings (which let "Java" pass for "JavaScript").

CLI:
  keywords.py gaps    --job job.json --master master.tex [--confirmed confirmed-skills.md]
  keywords.py match   --job job.json --resume resume.json|resume.tex
  keywords.py confirm --file confirmed-skills.md "Skill A" "Skill B" [--print-only]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from common import (
    dump_json,
    load_json,
    load_resume,
    page_view,
    read_confirmed_skills,
    same_tool,
    term_in_text,
)
from text_match import keyword_in_text, normalize_skill_key


@dataclass
class KeywordGapAnalysis:
    missing_keywords: list[str]
    injectable_keywords: list[str]
    non_injectable_keywords: list[str]
    current_match_percentage: float
    potential_match_percentage: float


@dataclass
class AlignmentViolation:
    field_path: str
    violation_type: str
    value: str
    severity: str


@dataclass
class AlignmentReport:
    is_aligned: bool
    violations: list[AlignmentViolation] = field(default_factory=list)
    confidence_score: float = 1.0


def job_keyword_terms(jd_keywords: dict[str, Any]) -> list[str]:
    """Distinct JD terms that keyword match is scored on, in posting order."""
    terms: dict[str, None] = {}
    for key in ("required_skills", "preferred_skills", "keywords"):
        for term in jd_keywords.get(key) or []:
            if isinstance(term, str) and term.strip():
                terms.setdefault(term.strip(), None)
    return list(terms)


def _extract_all_text(data: dict[str, Any]) -> str:
    """All text content of a resume dict that keyword matching reads."""
    parts: list[str] = []

    info = data.get("personalInfo") or {}
    if info.get("title"):
        parts.append(str(info["title"]))
    if data.get("summary"):
        parts.append(str(data["summary"]))

    for exp in data.get("workExperience", []) or []:
        if isinstance(exp, dict):
            parts.append(str(exp.get("title", "")))
            parts.append(str(exp.get("company", "")))
            desc = exp.get("description", [])
            if isinstance(desc, list):
                parts.extend(str(d) for d in desc)

    for edu in data.get("education", []) or []:
        if isinstance(edu, dict):
            parts.append(str(edu.get("degree", "")))
            parts.append(str(edu.get("institution", "")))
            if edu.get("description"):
                parts.append(str(edu["description"]))

    for proj in data.get("personalProjects", []) or []:
        if isinstance(proj, dict):
            parts.append(str(proj.get("name", "")))
            parts.append(str(proj.get("role", "")))
            stack = proj.get("stack", [])
            if isinstance(stack, list):
                parts.append(", ".join(str(s) for s in stack))
            desc = proj.get("description", [])
            if isinstance(desc, list):
                parts.extend(str(d) for d in desc)

    additional = data.get("additional", {})
    if isinstance(additional, dict):
        for key in ("technicalSkills", "certificationsTraining", "languages", "awards"):
            values = additional.get(key, [])
            if isinstance(values, list):
                parts.extend(str(v) for v in values)

    return " \n ".join(p for p in parts if p)


def _master_text(master: dict[str, Any]) -> str:
    """Master text including the commented-out alternate bullets."""
    parts = [_extract_all_text(master)]
    for kind in ("workExperience", "personalProjects"):
        for entry in master.get(kind) or []:
            parts.extend(str(b) for b in entry.get("alternates") or [])
    # Completed courses from the coursework bank count as things the master has.
    parts.extend(str(c.get("name", "")) for c in master.get("courseworkBank") or [])
    return " \n ".join(parts)


def calculate_keyword_match(resume: dict[str, Any], jd_keywords: dict[str, Any]) -> float:
    """Share of the posting's keywords found in the resume, 0.0 to 100.0.

    Returns 0 when the posting has no keywords (100 would mislead).
    """
    resume_text = _extract_all_text(page_view(resume)).lower()
    all_keywords = job_keyword_terms(jd_keywords)
    if not all_keywords:
        return 0.0
    matched = sum(1 for kw in all_keywords if term_in_text(kw, resume_text))
    return matched / len(all_keywords) * 100


def analyze_keyword_gaps(
    jd_keywords: dict[str, Any],
    tailored: dict[str, Any],
    master: dict[str, Any],
    confirmed: list[str] | tuple[str, ...] = (),
) -> KeywordGapAnalysis:
    """Split the posting keywords missing from ``tailored`` into ones the
    master (or the confirmed-skills list) already has and ones it lacks."""
    tailored_text = _extract_all_text(page_view(tailored)).lower()
    master_text = _master_text(master).lower()

    missing: list[str] = []
    injectable: list[str] = []
    non_injectable: list[str] = []
    all_jd_keywords = job_keyword_terms(jd_keywords)

    for keyword in all_jd_keywords:
        if term_in_text(keyword, tailored_text):
            continue
        missing.append(keyword)
        in_master = term_in_text(keyword, master_text)
        is_confirmed = any(same_tool(keyword, skill) for skill in confirmed)
        (injectable if in_master or is_confirmed else non_injectable).append(keyword)

    total = len(all_jd_keywords) or 1
    return KeywordGapAnalysis(
        missing_keywords=missing,
        injectable_keywords=injectable,
        non_injectable_keywords=non_injectable,
        current_match_percentage=(total - len(missing)) / total * 100,
        potential_match_percentage=(total - len(non_injectable)) / total * 100,
    )


def near_variant(a: str, b: str) -> bool:
    """ "Python 3" is a variant of "Python"; "Java" is not a variant of "JavaScript",
    and "C" is not a variant of "C#" or "C++"."""
    if same_tool(a, b):
        return True
    if {ch for ch in a if ch in "#+"} != {ch for ch in b if ch in "#+"}:
        return False
    return keyword_in_text(a, b) or keyword_in_text(b, a)


def validate_master_alignment(
    tailored: dict[str, Any],
    master: dict[str, Any],
    allowed_new_skills: set[str] | None = None,
) -> AlignmentReport:
    """Flag skills, certifications, employers and schools that are not in the master."""
    violations: list[AlignmentViolation] = []

    def lowered(values: Any) -> list[str]:
        return [v.lower() for v in values or [] if isinstance(v, str) and v.strip()]

    tailored_skills = lowered((tailored.get("additional") or {}).get("technicalSkills"))
    master_skills = set(lowered((master.get("additional") or {}).get("technicalSkills")))
    allowed_skills = {
        normalize_skill_key(skill)
        for skill in (allowed_new_skills or set())
        if isinstance(skill, str) and skill.strip()
    }
    master_full_text = _master_text(master).lower()

    for skill in dict.fromkeys(tailored_skills):
        if skill in master_skills:
            continue
        if normalize_skill_key(skill) in allowed_skills or any(
            same_tool(skill, allowed) for allowed in allowed_skills
        ):
            continue
        has_substring_match = any(near_variant(skill, ms) for ms in master_skills if ms)
        found_in_text = term_in_text(skill, master_full_text)
        violations.append(
            AlignmentViolation(
                field_path="additional.technicalSkills",
                violation_type="skill_variant"
                if has_substring_match or found_in_text
                else "fabricated_skill",
                value=skill,
                severity="info" if has_substring_match or found_in_text else "critical",
            )
        )

    tailored_certs = lowered((tailored.get("additional") or {}).get("certificationsTraining"))
    master_certs = set(lowered((master.get("additional") or {}).get("certificationsTraining")))
    for cert in dict.fromkeys(tailored_certs):
        if cert not in master_certs:
            violations.append(
                AlignmentViolation(
                    "additional.certificationsTraining", "fabricated_cert", cert, "critical"
                )
            )

    for key, name_field, kind in (
        ("workExperience", "company", "fabricated_company"),
        ("education", "institution", "fabricated_school"),
    ):
        master_names = {
            str(e.get(name_field, "")).lower()
            for e in master.get(key) or []
            if isinstance(e, dict)
        }
        for entry in tailored.get(key) or []:
            name = str(entry.get(name_field, "")).lower() if isinstance(entry, dict) else ""
            if name and name not in master_names:
                violations.append(AlignmentViolation(key, kind, name, "critical"))

    is_aligned = not any(v.severity == "critical" for v in violations)
    return AlignmentReport(
        is_aligned=is_aligned,
        violations=violations,
        confidence_score=max(0.0, 1.0 - len(violations) * 0.1),
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def gap_report(
    job: dict[str, Any], master: dict[str, Any], confirmed: list[str]
) -> dict[str, Any]:
    """What the gap-check step needs: which missing keywords to ask about."""
    from latex_io import baseline_resume

    requirements = job.get("requirements") or {}
    baseline = baseline_resume(master)
    analysis = analyze_keyword_gaps(requirements, baseline, master, confirmed)
    master_text = _master_text(master).lower()
    in_master = [k for k in analysis.injectable_keywords if term_in_text(k, master_text)]
    already_confirmed = [k for k in analysis.injectable_keywords if k not in in_master]
    return {
        "keywords_total": len(job_keyword_terms(requirements)),
        "baseline_match_percent": round(analysis.current_match_percentage, 1),
        "potential_match_percent": round(analysis.potential_match_percentage, 1),
        "missing_but_in_master": in_master,
        "missing_but_confirmed": already_confirmed,
        "ask_user": analysis.non_injectable_keywords,
    }


def confirm_skills(path: Path, skills: list[str], print_only: bool = False) -> list[str]:
    """Append newly confirmed skills; returns the lines added (or to add)."""
    existing = read_confirmed_skills(path)
    today = _dt.date.today().isoformat()
    lines = [
        f"- {skill.strip()} | confirmed {today}"
        for skill in skills
        if skill.strip() and not any(same_tool(skill, e) for e in existing)
    ]
    if lines and not print_only:
        current = path.read_text(encoding="utf-8") if path.is_file() else "# Confirmed skills\n\n"
        if not current.endswith("\n"):
            current += "\n"
        path.write_text(current + "\n".join(lines) + "\n", encoding="utf-8")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    gaps = sub.add_parser("gaps", help="split missing posting keywords")
    gaps.add_argument("--job", required=True)
    gaps.add_argument("--master", required=True)
    gaps.add_argument("--confirmed")

    match = sub.add_parser("match", help="keyword match percent for one resume")
    match.add_argument("--job", required=True)
    match.add_argument("--resume", required=True)

    confirm = sub.add_parser("confirm", help="add skills to confirmed-skills.md")
    confirm.add_argument("--file", required=True)
    confirm.add_argument("--print-only", action="store_true")
    confirm.add_argument("skills", nargs="+")

    args = parser.parse_args(argv)
    if args.command == "gaps":
        job = load_json(args.job)
        if not (job.get("requirements") or {}):
            print("job.json has no requirements yet; fill them in first (step 2).", file=sys.stderr)
            return 2
        report = gap_report(job, load_resume(args.master), read_confirmed_skills(args.confirmed))
        sys.stdout.write(dump_json(report))
    elif args.command == "match":
        job = load_json(args.job)
        resume = load_resume(args.resume)
        percent = calculate_keyword_match(resume, job.get("requirements") or {})
        sys.stdout.write(dump_json({"keyword_match_percent": round(percent, 1)}))
    else:
        lines = confirm_skills(Path(args.file), args.skills, args.print_only)
        if args.print_only:
            print("Add these lines to confirmed-skills.md:")
        print("\n".join(lines) if lines else "Nothing new to add.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AlignmentReport",
    "AlignmentViolation",
    "KeywordGapAnalysis",
    "analyze_keyword_gaps",
    "asdict",
    "calculate_keyword_match",
    "job_keyword_terms",
    "keyword_in_text",
    "validate_master_alignment",
]
