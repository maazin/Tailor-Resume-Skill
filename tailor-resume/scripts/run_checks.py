"""One command for workflow steps 7 to 10: check, compile, score, report.

Order:
  1. template check            the master's preamble has what the rules need
  2. fact gate (F1-F9)         a critical finding stops here, before any PDF
  3. AI-phrase filter          automatic replacements, each listed in the report
  4. write + compile + fill    latex_io, compile, layout_check.fit (L9-L11)
  5. format (C), style (W) and layout (L) checks on the page as written
  6. parse check               pdfminer and pdftotext
  7. ATS score                 baseline and tailored, plus the recruiter read
                               if recruiter.json is given
  8. report.md and results.json

A blocking finding removes resume.pdf, so a failed resume cannot be delivered
by accident. "rewrite" findings (W5, W6, W8) block on --attempt 1 only.
After two failed tries, stop and show the user what failed.

CLI:
  run_checks.py --master master.tex --tailored tailored.json --job job.json --out-dir DIR
                [--confirmed confirmed-skills.md] [--banned banned-words.txt]
                [--selection selection.json] [--recruiter recruiter.json] [--attempt 1]
Exit codes: 0 everything passed, 1 blocking findings, 2 could not run.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from ai_phrases import remove_ai_phrases
from ats_score import score_resume
from common import (
    ENTRY_KINDS,
    Finding,
    best_source,
    bullets,
    dump_json,
    entries,
    entry_id,
    entry_name,
    find_master_entry,
    is_blocking,
    load_json,
    normalize_term,
    page_text,
    read_confirmed_skills,
    term_in_text,
)
from compile import CompileError, compile_tex, probe_geometry
from fact_gate import run_fact_gate
from format_check import run_format_check
from keywords import _extract_all_text, job_keyword_terms
from latex_io import (
    LatexFormatError,
    baseline_resume,
    merge_tailored,
    read_tex,
    template_findings,
    write_tex,
)
from layout_check import check_layout, fit
from parse_check import parse_check_pdf, summary_line
from style_check import check_tex_dashes, run_style_check

RECRUITER_CRITERIA = (
    "The headline and most recent role read as this job",
    "The top third of the page shows the posting's three most important requirements",
    "Bullets show evidence: numbers, scale or a concrete result",
    "The projects shown are relevant to this posting",
    "Plain, specific language with no filler",
)
SUB_SCORE_LABELS = {
    "keyword_match": "Keyword match (40%)",
    "skills_coverage": "Skills coverage (20%)",
    "title_match": "Title match (15%)",
    "section_completeness": "Section completeness (15%)",
    "date_consistency": "Date consistency (10%)",
}


def _selection_scores(selection: dict[str, Any] | None) -> dict[str, float]:
    if not selection:
        return {}
    return {p["id"]: p["score"] for e in selection.get("entries") or [] for p in e.get("picks") or []}


def _bullet_rows(resume: dict[str, Any], master: dict[str, Any], by_id: dict[str, float]) -> list[dict[str, Any]]:
    """Each shown bullet with the master bullet it came from (before and after)."""
    rows = []
    for kind, _, entry in entries(resume):
        source = find_master_entry(kind, entry, master)
        for index, text in enumerate(bullets(entry)):
            before, overlap, score = "", 0.0, None
            if source:
                source_index, before, overlap = best_source(text, source)
                if source_index >= 0:
                    score = by_id.get(f"{entry_id(kind, source)}.{source_index}")
            rows.append(
                {
                    "entry": entry_name(kind, entry), "n": index + 1, "after": text, "before": before,
                    "unchanged": normalize_term(before) == normalize_term(text), "overlap": overlap, "score": score,
                }
            )
    return rows


def _recruiter(recruiter: dict[str, Any] | None, key: str, resume: dict[str, Any]) -> dict[str, Any]:
    """Validate Claude's recruiter read: five criteria, 0-2 points, each point cited from the page."""
    block = (recruiter or {}).get(key)
    if not isinstance(block, dict):
        return {"score": None, "criteria": [], "problems": ["not provided"]}
    criteria = block.get("criteria") or []
    problems: list[str] = []
    if len(criteria) != len(RECRUITER_CRITERIA):
        problems.append(f"expected {len(RECRUITER_CRITERIA)} criteria, got {len(criteria)}")
    page = re.sub(r"\s+", " ", page_text(resume)).casefold()
    total = 0
    cleaned = []
    for index, item in enumerate(criteria[: len(RECRUITER_CRITERIA)]):
        points = int(item.get("points") or 0) if isinstance(item, dict) else 0
        cite = str(item.get("cite") or "").strip() if isinstance(item, dict) else ""
        if not 0 <= points <= 2:
            problems.append(f"criterion {index + 1}: points must be 0, 1 or 2")
            points = max(0, min(2, points))
        if points and not cite:
            problems.append(f"criterion {index + 1}: a point needs a line cited from the page")
        elif cite and re.sub(r"\s+", " ", cite).casefold() not in page:
            problems.append(f"criterion {index + 1}: the cited line is not on this page")
        total += points
        cleaned.append({"criterion": RECRUITER_CRITERIA[index], "points": points, "cite": cite,
                        "note": str(item.get("note") or "") if isinstance(item, dict) else ""})
    return {"score": total, "criteria": cleaned, "problems": problems}


def _keyword_table(resume: dict[str, Any], baseline: dict[str, Any], job: dict[str, Any]) -> list[dict[str, Any]]:
    requirements = job.get("requirements") or {}
    tailored_text = _extract_all_text(resume).lower()
    baseline_text = _extract_all_text(baseline).lower()
    return [
        {"keyword": k, "baseline": term_in_text(k, baseline_text), "tailored": term_in_text(k, tailored_text)}
        for k in job_keyword_terms(requirements)
    ]


def _fmt(value: Any, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value}{suffix}"


def build_report(ctx: dict[str, Any]) -> str:
    job = ctx["job"]
    lines: list[str] = [
        f"# Tailoring report: {job.get('title') or '(no title)'} at {job.get('company') or '(no company)'}",
        "",
        f"Source: {job.get('source', '')} {job.get('url', '')}".rstrip(),
        f"Status: **{ctx['status']}** (attempt {ctx['attempt']})",
        "",
    ]
    blocking = ctx["blocking"]
    if blocking:
        lines += ["## What blocked this run", ""]
        lines += [f"- **{f.rule}** {f.where}: {f.message}" for f in blocking]
        lines.append("")

    base, tail = ctx.get("ats_baseline"), ctx.get("ats_tailored")
    rb, rt = ctx.get("recruiter_baseline") or {}, ctx.get("recruiter_tailored") or {}
    lines += ["## Scores", "", "| Report line | Baseline | Tailored |", "| --- | --- | --- |"]
    lines.append(f"| ATS score /10 | {_fmt(base and base['score_out_of_10'])} | {_fmt(tail and tail['score_out_of_10'])} |")
    lines.append(
        f"| Recruiter read /10 | {_fmt(rb.get('score')) if rb.get('score') is not None else 'pending'} "
        f"| {_fmt(rt.get('score')) if rt.get('score') is not None else 'pending'} |"
    )
    if base and tail:
        lines.append(
            f"| Keyword match % (potential %) | {base['keyword_match_percent']} ({base['potential_match_percent']}) "
            f"| {tail['keyword_match_percent']} ({tail['potential_match_percent']}) |"
        )
    lines.append(f"| Parse check | {ctx.get('parse_baseline_line', 'n/a')} | {ctx.get('parse_tailored_line', 'n/a')} |")
    lines.append("")
    if base and tail:
        if tail["score_out_of_10"] < base["score_out_of_10"]:
            worse = [
                SUB_SCORE_LABELS[k] for k in SUB_SCORE_LABELS
                if (tail["sub_scores"].get(k) or 0) < (base["sub_scores"].get(k) or 0)
            ]
            lines += [
                "**The tailored ATS score is lower than the baseline.** Lower sub-scores: "
                + (", ".join(worse) or "none") + ". The baseline prints every project the master has "
                "(more than one page), so it can hold more keywords than a one-page resume.",
                "",
            ]
        lines += ["### ATS sub-scores (out of 100)", "", "| Sub-score | Baseline | Tailored |", "| --- | --- | --- |"]
        for key, label in SUB_SCORE_LABELS.items():
            lines.append(f"| {label} | {_fmt(base['sub_scores'].get(key))} | {_fmt(tail['sub_scores'].get(key))} |")
        lines += [
            "",
            "The potential keyword match is the highest match reachable with what the master resume and "
            "confirmed-skills.md contain. A tailored match near it means little is left to gain without new experience. "
            "Section completeness counts a summary section, which this template does not have, so it tops out at 80.",
            "",
        ]

    for key, label in (("recruiter_baseline", "Baseline"), ("recruiter_tailored", "Tailored")):
        read = ctx.get(key) or {}
        if read.get("criteria"):
            lines += [f"### Recruiter read: {label} ({read['score']}/10)", ""]
            for item in read["criteria"]:
                cite = f' Cites: "{item["cite"]}"' if item["cite"] else ""
                note = f" {item['note']}" if item["note"] else ""
                lines.append(f"- {item['points']}/2 {item['criterion']}.{note}{cite}")
            if read.get("problems"):
                lines.append("- Problems with this read: " + "; ".join(read["problems"]))
            lines.append("")

    table = ctx.get("keywords") or []
    if table:
        lines += ["## Keyword coverage", "", "| Keyword | Baseline | Tailored |", "| --- | --- | --- |"]
        lines += [f"| {r['keyword']} | {'yes' if r['baseline'] else 'no'} | {'yes' if r['tailored'] else 'no'} |" for r in table]
        lines.append("")

    lines += ["## Gaps", ""]
    gaps = (tail or {}).get("missing_not_in_master") or []
    lines.append("Posting keywords the master resume and confirmed skills do not have: " + (", ".join(gaps) if gaps else "none") + ".")
    available = (tail or {}).get("missing_but_available") or []
    if available:
        lines.append("In the master but not on this page: " + ", ".join(available) + ".")
    selection = ctx.get("selection")
    if selection and selection.get("gap_requirements"):
        lines.append("Requirements with no master bullet scoring 60 or more: " + "; ".join(selection["gap_requirements"]) + ".")
    lines.append("")

    fit_result = ctx.get("fit")
    if fit_result:
        lines += ["## Page fill", ""]
        lines += [f"- {entry}" for entry in fit_result.get("log") or []]
        lines.append(
            f"- Projects shown: {fit_result.get('projects_shown')} of {fit_result.get('projects_available')}; "
            f"pdflatex runs: {fit_result.get('compiles')}"
        )
        if ctx.get("char_guide"):
            lines.append(f"- A bullet in this template prints on 2 lines at about {ctx['char_guide'][0]} to {ctx['char_guide'][1]} characters")
        lines.append("")

    rows = ctx.get("bullet_rows") or []
    if rows:
        lines += ["## Bullets: before and after", ""]
        current = None
        for row in rows:
            if row["entry"] != current:
                current = row["entry"]
                lines += [f"### {current}", ""]
            score = f" (score {row['score']})" if row["score"] is not None else ""
            if row["unchanged"]:
                lines.append(f"{row['n']}. Unchanged{score}: {row['after']}")
            else:
                lines.append(f"{row['n']}. Before{score}: {row['before'] or '(no close master bullet found)'}")
                lines.append(f"   After: {row['after']}")
        lines.append("")

    if selection:
        lines += ["## Bullet scores", "", "| Entry | Bullet | D | T | A | I | Score | Band | Requirement |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for entry in selection.get("entries") or []:
            for pick in entry.get("picks") or []:
                text = pick["text"][:70].replace("|", "/") + ("..." if len(pick["text"]) > 70 else "")
                reqs = "; ".join(pick["requirements"]).replace("|", "/") or ("none named (capped at 44)" if pick.get("capped_no_evidence") else "none named")
                lines.append(
                    f"| {entry['entry']} | {text} | {pick['D']:.0f} | {pick['T']:.0f} | {pick['A']:.0f} | {pick['I']:.0f} "
                    f"| {pick['score']} | {pick['band']} | {reqs} |"
                )
        lines.append("")

    changes = ctx.get("phrase_changes") or []
    lines += ["## AI-phrase filter", ""]
    if changes:
        lines += [f"- '{c['phrase']}' -> '{c['replacement'] or '(removed)'}' in: {c['after']}" for c in changes]
    else:
        lines.append("No phrase needed replacing.")
    lines.append("")

    lines += ["## Check results", ""]
    for title, key in (
        ("Template", "template_findings"), ("Fact gate (F1-F9)", "fact_findings"),
        ("Content rules (C1-C15)", "format_findings"), ("Writing rules (W1-W10)", "style_findings"),
        ("Layout rules (L1-L15)", "layout_findings"),
    ):
        findings: list[Finding] | None = ctx.get(key)
        lines += [f"### {title}", ""]
        if findings is None:
            lines.append("Not run (an earlier step blocked).")
        elif not findings:
            lines.append("Passed, nothing to report.")
        else:
            lines += [f"- [{f.severity}] **{f.rule}** {f.where}: {f.message}" for f in findings]
        lines.append("")

    for label, key in (("Tailored", "parse_tailored"), ("Baseline", "parse_baseline")):
        result = ctx.get(key)
        lines += [f"### Parse check: {label}", ""]
        if not result:
            lines += ["Not run.", ""]
            continue
        for item in result["passes"]:
            status = "pass" if item["passed"] else "FAIL"
            detail = f"extractability {item.get('extractability')}, {item.get('page_count')} page(s)"
            if item.get("content_recall") is not None:
                detail += f", {item['content_recall']:.0%} of resume text found, order fidelity {item['order_fidelity']:.2f}"
            lines.append(f"- {item['extractor']}: {status}; {detail}")
            lines += [f"  - problem: {p}" for p in item.get("problems") or []]
            lines += [f"  - note: {n}" for n in item.get("notes") or []]
            for check in item.get("checks") or []:
                if check["status"] != "pass":
                    lines.append(f"  - check {check['id']}: {check['status']} {check['params']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    master_tex = Path(args.master).read_text(encoding="utf-8")
    master = read_tex(args.master)
    job = load_json(args.job)
    tailored = load_json(args.tailored)
    confirmed = read_confirmed_skills(args.confirmed)
    selection = load_json(args.selection) if args.selection else None
    recruiter = load_json(args.recruiter) if args.recruiter else None
    attempt = args.attempt

    ctx: dict[str, Any] = {"job": job, "attempt": attempt, "selection": selection, "status": "blocked", "blocking": []}
    all_findings: list[Finding] = []
    pdf_path = out_dir / "resume.pdf"

    def finish(code: int) -> tuple[int, dict[str, Any]]:
        blocking = [f for f in all_findings if is_blocking(f, attempt)]
        ctx["blocking"] = blocking
        ctx["status"] = "passed" if code == 0 and not blocking else "blocked"
        delivered: list[str] = []
        if blocking:
            pdf_path.unlink(missing_ok=True)
            code = 1
        elif code == 0:
            name = "_".join(str((master.get("personalInfo") or {}).get("name") or "Resume").split())
            company = re.sub(r"[^A-Za-z0-9]+", "", str(job.get("company") or "")) or "Company"
            stem = re.sub(r"[^A-Za-z0-9_]+", "", f"{name}_{company}")
            for suffix in (".tex", ".pdf"):
                target = out_dir / f"{stem}{suffix}"
                shutil.copyfile(out_dir / f"resume{suffix}", target)
                delivered.append(str(target))
        (out_dir / "report.md").write_text(build_report(ctx), encoding="utf-8")
        results = {
            "status": ctx["status"],
            "attempt": attempt,
            "blocking": [f.to_dict() for f in blocking],
            "warnings": [f.to_dict() for f in all_findings if f.severity in ("warn", "rewrite") and f not in blocking],
            "ats": {"baseline": ctx.get("ats_baseline"), "tailored": ctx.get("ats_tailored")},
            "recruiter": {"baseline": ctx.get("recruiter_baseline"), "tailored": ctx.get("recruiter_tailored")},
            "parse": {"baseline": ctx.get("parse_baseline_line"), "tailored": ctx.get("parse_tailored_line")},
            "fit": ctx.get("fit"),
            "bullet_chars_guide": ctx.get("char_guide"),
            "phrase_changes": ctx.get("phrase_changes") or [],
            "report": str(out_dir / "report.md"),
            "delivered": delivered,
            "next": (
                "Deliver the files and show the two score pairs and the gap list." if not blocking and code == 0
                else "Rewrite the bullets named above (step 5) and run again with --attempt 2."
                if attempt < 2 else "Second failed try: stop and show the user what failed."
            ),
        }
        dump_json(results, out_dir / "results.json")
        return code, results

    # 1. template
    ctx["template_findings"] = template_findings(master_tex)
    all_findings += ctx["template_findings"]
    if any(is_blocking(f) for f in ctx["template_findings"]):
        return finish(1)

    # 2. fact gate on everything the tailored file offers, shown or not
    candidates = merge_tailored(tailored, master)
    ctx["fact_findings"] = run_fact_gate(candidates, master, confirmed, job)
    all_findings += ctx["fact_findings"]

    # Baseline scores do not depend on the tailored text; compute them either way.
    baseline = baseline_resume(master)
    ctx["ats_baseline"] = score_resume(baseline, master, job, confirmed)
    if any(f.severity == "critical" for f in ctx["fact_findings"]):
        return finish(1)

    # 3. AI-phrase filter (never blocks; every change is listed)
    filtered, changes = remove_ai_phrases(
        {k: tailored.get(k) for k in ("personalInfo", "skillGroups", *ENTRY_KINDS) if k in tailored}
    )
    ctx["phrase_changes"] = changes
    dump_json(filtered, out_dir / "tailored.final.json")

    # 4. write, compile, fill the page
    fit_result, resume = fit(filtered, master, out_dir, "resume")
    ctx["fit"] = fit_result.to_dict()
    if not fit_result.ok or resume is None:
        all_findings.append(
            Finding("L9", "block", "could not produce a one-page PDF: " + "; ".join(fit_result.errors or fit_result.log), where="page")
        )
        ctx["layout_findings"] = [all_findings[-1]]
        return finish(1)

    # 5. content, writing and layout rules on the page as written
    by_id = _selection_scores(selection)
    ctx["bullet_rows"] = _bullet_rows(resume, master, by_id)
    scores_by_entry: dict[str, list[float]] | None = None
    if by_id:
        scores_by_entry = {}
        for row in ctx["bullet_rows"]:
            if row["score"] is not None:
                scores_by_entry.setdefault(row["entry"], []).append(row["score"])
    ctx["format_findings"] = run_format_check(resume, master, job, scores_by_entry)
    tex_text = Path(fit_result.tex).read_text(encoding="utf-8")
    ctx["style_findings"] = run_style_check(resume, master, job, confirmed, args.banned) + check_tex_dashes(tex_text)
    layout_findings, measured = check_layout(fit_result.pdf, fit_result.tex, resume, master_tex)
    ctx["layout_findings"] = layout_findings
    ctx["char_guide"] = measured.char_guide()
    all_findings += ctx["format_findings"] + ctx["style_findings"] + layout_findings

    # 6. parse check, both extractors
    parse_tailored = parse_check_pdf(fit_result.pdf, resume, max_pages=1)
    ctx["parse_tailored"] = parse_tailored
    ctx["parse_tailored_line"] = summary_line(parse_tailored)
    if not parse_tailored["passed"]:
        all_findings.append(Finding("PARSE", "block", ctx["parse_tailored_line"], where="pdf"))

    baseline_dir = out_dir / "baseline"
    baseline_dir.mkdir(exist_ok=True)
    baseline_tex = baseline_dir / "baseline.tex"
    baseline_tex.write_text(write_tex(baseline), encoding="utf-8")
    compiled = compile_tex(baseline_tex)
    if compiled.ok:
        parse_baseline = parse_check_pdf(compiled.pdf, baseline, max_pages=compiled.pages)
        ctx["parse_baseline"] = parse_baseline
        ctx["parse_baseline_line"] = summary_line(parse_baseline) + (
            f", {compiled.pages} pages" if compiled.pages != 1 else ""
        )
    else:
        ctx["parse_baseline_line"] = "baseline did not compile: " + "; ".join(compiled.errors)

    # 7. scores
    ctx["ats_tailored"] = score_resume(resume, master, job, confirmed)
    ctx["keywords"] = _keyword_table(resume, baseline, job)
    ctx["recruiter_baseline"] = _recruiter(recruiter, "baseline", baseline)
    ctx["recruiter_tailored"] = _recruiter(recruiter, "tailored", resume)
    dump_json({k: v for k, v in resume.items() if not k.startswith("_")}, out_dir / "tailored.final.json")
    return finish(0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check, compile, score and report one tailored resume.")
    parser.add_argument("--master", required=True)
    parser.add_argument("--tailored", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--confirmed")
    parser.add_argument("--banned")
    parser.add_argument("--selection")
    parser.add_argument("--recruiter")
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        code, results = run(args)
    except (LatexFormatError, CompileError, FileNotFoundError, ValueError) as exc:
        print(f"run_checks: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(dump_json(results))
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["RECRUITER_CRITERIA", "build_report", "probe_geometry", "run"]
