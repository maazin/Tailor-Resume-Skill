"""Fact gate F1 to F9: every fact in the tailored resume must trace to the master.

Compares the tailored resume with master.tex and confirmed-skills.md entry by
entry. Entries are matched on company or project name plus start date. A
critical finding blocks compiling; a warning goes in the report.

  F1 skills are in the master or confirmed            critical
  F2 employers, schools, certifications, projects     critical
  F3 numbers appear in the same master entry          critical
  F4 tools appear in the same master entry            critical
  F5 dates are copied exactly                         critical
  F6 job titles match the master                      warning
  F7 same bullet count everywhere, at least 3         critical
  F8 every master work entry present, newest first    warning
  F9 no "built" where the master only shows "used"    warning

F1 and F2 reuse Resume-Matcher's validate_master_alignment (Apache-2.0, see
keywords.py). F3, F4, F6, F8 and F9 are rules taken from career-ops (MIT:
verify-cv-facts.mjs, cv-title-check.mjs) and rewritten in Python; F3 and F4
are stricter here: the same entry, not anywhere in the sources.

CLI:
  fact_gate.py --master master.tex --tailored tailored.json
               [--confirmed confirmed-skills.md] [--job job.json]
Exit code 1 when any finding is critical.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from common import (
    ENTRY_KINDS,
    MONTHS,
    Finding,
    best_source,
    bullets,
    dump_json,
    entries,
    entry_name,
    entry_source_text,
    find_master_entry,
    load_json,
    looks_like_tool,
    normalize_term,
    number_supported,
    numbers_in,
    parse_dates,
    read_confirmed_skills,
    same_tool,
    source_number_values,
    term_in_text,
    tool_vocabulary,
    words,
)
from keywords import validate_master_alignment
from latex_io import merge_tailored, read_tex

MIN_BULLETS = 3
AUTHORSHIP_VERBS = frozenset(
    """authored built coded created designed developed engineered implemented
    programmed wrote architected invented founded launched""".split()
)
# Capitalised words that are not names of things.
_COMMON_CAPITALISED = frozenset(
    {w.capitalize() for w in MONTHS}
    | {
        "January", "February", "March", "April", "June", "July", "August",
        "September", "October", "November", "December", "Monday", "Tuesday",
        "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "I", "A", "K", "M",
    }
)
_NAME_TOKEN = re.compile(r"(?<![\w.+])[A-Za-z][A-Za-z0-9+#.]*(?:[-/][A-Za-z0-9+#.]+)*")


def _name_tokens(text: str) -> list[str]:
    """Words after the first that look like names: a capital or a digit inside."""
    found = []
    tokens = _NAME_TOKEN.findall(text)
    for token in tokens[1:]:
        token = token.rstrip(".")
        if token in _COMMON_CAPITALISED or len(token) < 2:
            continue
        if re.search(r"[A-Z]", token) or re.search(r"[A-Za-z]\d|\d[A-Za-z]", token):
            found.append(token)
    return list(dict.fromkeys(found))


def _token_in_text(token: str, text: str) -> bool:
    if term_in_text(token, text):
        return True
    # "FastAPI-based" or "React/TypeScript": every part must be known.
    parts = [p for p in re.split(r"[-/]", token) if p]
    return len(parts) > 1 and all(term_in_text(p, text) or not re.search(r"[A-Z0-9]", p) for p in parts)


def _where(kind: str, entry: dict[str, Any], index: int | None = None) -> str:
    name = entry_name(kind, entry)
    return name if index is None else f"{name} bullet {index + 1}"


def _other_entries_with(term: str, master: dict[str, Any], skip: dict[str, Any]) -> list[str]:
    names = []
    for kind in ENTRY_KINDS:
        for entry in master.get(kind) or []:
            if entry is not skip and term_in_text(term, entry_source_text(kind, entry)):
                names.append(entry_name(kind, entry))
    return names


def check_skills_and_names(
    resume: dict[str, Any], master: dict[str, Any], confirmed: list[str]
) -> list[Finding]:
    """F1 and F2."""
    findings: list[Finding] = []
    report = validate_master_alignment(resume, master, set(confirmed))
    for violation in report.violations:
        if violation.violation_type == "fabricated_skill":
            findings.append(
                Finding("F1", "critical",
                        f"skill '{violation.value}' is not in the master or in confirmed-skills.md",
                        where="skills", value=violation.value)
            )
        elif violation.violation_type == "skill_variant":
            findings.append(
                Finding("F1", "info", f"skill '{violation.value}' is a variant of a master skill",
                        where="skills", value=violation.value)
            )
        else:
            label = {
                "fabricated_company": "employer", "fabricated_school": "school",
                "fabricated_cert": "certification",
            }[violation.violation_type]
            findings.append(
                Finding("F2", "critical", f"{label} '{violation.value}' is not in the master",
                        where=violation.field_path, value=violation.value)
            )
    master_projects = {normalize_term(p.get("name", "")) for p in master.get("personalProjects") or []}
    for project in resume.get("personalProjects") or []:
        if normalize_term(project.get("name", "")) not in master_projects:
            findings.append(
                Finding("F2", "critical", f"project '{project.get('name', '')}' is not in the master",
                        where="personalProjects", value=str(project.get("name", "")))
            )
    return findings


def check_entry_facts(
    resume: dict[str, Any],
    master: dict[str, Any],
    vocabulary: list[str],
    posting_text: str = "",
) -> list[Finding]:
    """F3, F4, F5, F6 and F9, one entry at a time."""
    findings: list[Finding] = []
    master_all_text = " \n ".join(
        entry_source_text(kind, entry) for kind in ENTRY_KINDS for entry in master.get(kind) or []
    )
    skills_text = " , ".join((master.get("additional") or {}).get("technicalSkills") or [])

    for kind, _, entry in entries(resume):
        source = find_master_entry(kind, entry, master)
        if source is None:
            continue  # F2 reports it
        source_text = entry_source_text(kind, source)
        source_numbers = source_number_values(source_text)

        # F5
        if parse_dates(str(entry.get("years") or "")) != parse_dates(str(source.get("years") or "")):
            findings.append(
                Finding("F5", "critical",
                        f"dates '{entry.get('years', '')}' do not match the master's '{source.get('years', '')}'",
                        where=_where(kind, entry), value=str(entry.get("years", "")))
            )
        # F6
        if kind == "workExperience" and normalize_term(str(entry.get("title", ""))) != normalize_term(
            str(source.get("title", ""))
        ):
            findings.append(
                Finding("F6", "warn",
                        f"job title '{entry.get('title', '')}' differs from the master's '{source.get('title', '')}'",
                        where=_where(kind, entry), value=str(entry.get("title", "")))
            )
        # F4 for the stack line
        for item in entry.get("stack") or []:
            if not term_in_text(item, source_text):
                findings.append(
                    Finding("F4", "critical",
                            f"'{item}' is in the tech stack but the master does not show it in this entry",
                            where=_where(kind, entry), value=item)
                )

        for index, text in enumerate(bullets(entry)):
            where = _where(kind, entry, index)
            # F3
            for number in numbers_in(text):
                if not number_supported(number, source_numbers):
                    findings.append(
                        Finding("F3", "critical",
                                f"number '{number.literal}' is not in the master for this entry",
                                where=where, value=number.literal)
                    )
            # F4: known tools
            flagged: list[str] = []
            for term in vocabulary:
                if term_in_text(term, text) and not term_in_text(term, source_text):
                    if any(same_tool(term, f) or term_in_text(term, f) for f in flagged):
                        continue
                    flagged.append(term)
                    elsewhere = _other_entries_with(term, master, source)
                    if elsewhere:
                        hint = f"the master shows it under {', '.join(elsewhere)}"
                    elif term_in_text(term, skills_text):
                        hint = "the master lists it only in the skills section"
                    else:
                        hint = "it is not in the master; a confirmed gap skill may go in the skills section only"
                    findings.append(
                        Finding("F4", "critical",
                                f"tool '{term}' is not in the master for this entry ({hint})",
                                where=where, value=term)
                    )
            # F4: other names (capitalised words, mixed letters and digits)
            for token in _name_tokens(text):
                if _token_in_text(token, source_text) or any(term_in_text(token, f) for f in flagged):
                    continue
                known_elsewhere = term_in_text(token, master_all_text) or term_in_text(token, posting_text)
                findings.append(
                    Finding("F4", "critical" if known_elsewhere and looks_like_tool(token) else "warn",
                            f"'{token}' does not appear in the master for this entry; check it is not a new claim",
                            where=where, value=token)
                )
            # F9
            lead = [w.lower() for w in words(text)[:1]]
            if lead and lead[0] in AUTHORSHIP_VERBS:
                _, original, overlap = best_source(text, source)
                if original and overlap >= 0.2 and not (
                    {w.lower() for w in words(original)} & AUTHORSHIP_VERBS
                ):
                    findings.append(
                        Finding("F9", "warn",
                                f"says '{words(text)[0]}' but the closest master bullet does not claim authorship: "
                                f"\"{original[:120]}\"",
                                where=where, value=words(text)[0])
                    )
    return findings


def check_structure(resume: dict[str, Any], master: dict[str, Any]) -> list[Finding]:
    """F7 and F8."""
    findings: list[Finding] = []
    counts = {
        _where(kind, entry): len(bullets(entry)) for kind, _, entry in entries(resume)
    }
    if counts:
        distinct = set(counts.values())
        if len(distinct) > 1:
            detail = ", ".join(f"{name}: {count}" for name, count in counts.items())
            findings.append(
                Finding("F7", "critical", f"entries have different bullet counts ({detail})", where="bullets")
            )
        if min(distinct) < MIN_BULLETS:
            short = ", ".join(name for name, count in counts.items() if count < MIN_BULLETS)
            findings.append(
                Finding("F7", "critical", f"fewer than {MIN_BULLETS} bullets in: {short}", where="bullets")
            )

    shown = [normalize_term(e.get("company", "")) + "|" + normalize_term(e.get("title", "")) for e in resume.get("workExperience") or []]
    wanted = [normalize_term(e.get("company", "")) + "|" + normalize_term(e.get("title", "")) for e in master.get("workExperience") or []]
    shown_companies = [s.split("|")[0] for s in shown]
    for entry in master.get("workExperience") or []:
        if normalize_term(entry.get("company", "")) not in shown_companies:
            findings.append(
                Finding("F8", "warn", f"master work entry '{entry.get('company', '')}' is missing", where="workExperience",
                        value=str(entry.get("company", "")))
            )
    order = [w for w in wanted if w in shown]
    if [s for s in shown if s in wanted] != order:
        findings.append(
            Finding("F8", "warn", "work entries are not in the master's order (newest first)", where="workExperience")
        )
    return findings


def run_fact_gate(
    resume: dict[str, Any],
    master: dict[str, Any],
    confirmed: list[str] | None = None,
    job: dict[str, Any] | None = None,
) -> list[Finding]:
    """All nine rules on a full resume dict (see latex_io.merge_tailored)."""
    confirmed = confirmed or []
    vocabulary = tool_vocabulary(master, job, confirmed)
    posting_text = str((job or {}).get("description_text") or "")
    return (
        check_skills_and_names(resume, master, confirmed)
        + check_entry_facts(resume, master, vocabulary, posting_text)
        + check_structure(resume, master)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fact gate F1-F9.")
    parser.add_argument("--master", required=True)
    parser.add_argument("--tailored", required=True)
    parser.add_argument("--confirmed")
    parser.add_argument("--job")
    args = parser.parse_args(argv)
    master = read_tex(args.master)
    resume = merge_tailored(load_json(args.tailored), master)
    findings = run_fact_gate(
        resume, master, read_confirmed_skills(args.confirmed), load_json(args.job) if args.job else None
    )
    sys.stdout.write(dump_json([f.to_dict() for f in findings]))
    return 1 if any(f.severity == "critical" for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
