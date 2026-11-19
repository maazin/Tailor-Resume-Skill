"""Compile, layout, parse check and the one-command run. Needs pdflatex and pdftotext."""

import argparse
import json
import re
import subprocess

import pytest
from conftest import FIXTURES, apply_edits, load, needs_pdf_tools, rules

pytestmark = needs_pdf_tools


def run_args(out_dir, tailored_path=None, job_path=None, **extra):
    base = dict(
        master=str(FIXTURES / "sample_master.tex"),
        tailored=str(tailored_path or FIXTURES / "sample_tailored.json"),
        job=str(job_path or FIXTURES / "sample_job.json"),
        out_dir=str(out_dir), confirmed=None, banned=None, selection=None, recruiter=None, attempt=1,
    )
    base.update(extra)
    return argparse.Namespace(**base)


@pytest.fixture(scope="module")
def passing_run(tmp_path_factory):
    from run_checks import run

    out = tmp_path_factory.mktemp("run")
    code, results = run(run_args(out))
    return out, code, results


def test_jake_sample_compiles_and_reads_out(tmp_path):
    """Build step 2: Jake's Resume compiles to one page and both extractors read it."""
    from compile import compile_tex
    from parse_check.extract import extract_file

    tex = tmp_path / "jake.tex"
    tex.write_text((FIXTURES / "jake_resume.tex").read_text(encoding="utf-8"), encoding="utf-8")
    result = compile_tex(tex)
    assert result.ok and result.pages == 1, result.errors
    for extractor in ("pdfminer", "pdftotext"):
        text = extract_file(result.pdf, extractor).text
        assert "Jake Ryan" in text and "Undergraduate Research Assistant" in text and "Gitlytics" in text


def test_compile_reports_latex_errors(tmp_path):
    from compile import compile_tex

    tex = tmp_path / "broken.tex"
    tex.write_text("\\documentclass{article}\\begin{document}\\undefinedmacro\\end{document}", encoding="utf-8")
    result = compile_tex(tex)
    assert not result.ok and any("Undefined control sequence" in e for e in result.errors)


def test_geometry_probe_reads_the_margins():
    from compile import probe_geometry

    geometry = probe_geometry(FIXTURES / "sample_master.tex")
    for value in (geometry.left, geometry.top, geometry.right_margin, geometry.bottom_margin):
        assert abs(value / 72 - 0.3) < 0.005


def test_full_run_passes(passing_run):
    out, code, results = passing_run
    assert code == 0 and results["status"] == "passed", results["blocking"]
    assert results["blocking"] == []
    for name in ("resume.tex", "resume.pdf", "report.md", "results.json",
                 "Jordan_Rivera_ExampleCo.tex", "Jordan_Rivera_ExampleCo.pdf"):
        assert (out / name).is_file(), name
    assert results["parse"]["tailored"] == "pass (pdfminer, pdftotext)"
    assert results["ats"]["tailored"]["score_out_of_10"] >= results["ats"]["baseline"]["score_out_of_10"]
    assert results["ats"]["tailored"]["sub_scores"]["title_match"] == 100.0


def test_page_is_one_full_page_with_two_line_bullets(passing_run):
    from compile import count_pages
    from latex_io import merge_tailored, read_tex
    from layout_check import check_layout

    out, _, results = passing_run
    fit = results["fit"]
    assert count_pages(out / "resume.pdf") == 1
    assert fit["projects_shown"] == 3 and fit["projects_available"] == 4
    assert fit["bottom_gap_in"] <= 0.25
    assert 5 <= fit["entrygap_pt"] <= 9 and 8 <= fit["sectiongap_pt"] <= 12
    master = read_tex(FIXTURES / "sample_master.tex")
    resume = merge_tailored(json.loads((out / "tailored.final.json").read_text()), master)
    findings, measured = check_layout(out / "resume.pdf", out / "resume.tex", resume,
                                      (FIXTURES / "sample_master.tex").read_text())
    assert [f for f in findings if f.severity == "block"] == []
    assert len(measured.bullets) == 18
    assert all(b.lines == 2 and b.last_line_fill >= 0.5 for b in measured.bullets)
    assert len(measured.header_lines) == 3 and measured.header_lines[1] == "Data Engineer II"
    assert measured.header_lines[2].startswith("Relocating to Austin, TX")
    assert len(measured.skills_lines) == 4 and measured.dash_bullets == 0
    assert all(h["lines"] == 1 for h in measured.headings)
    assert measured.margins_in == {"left": 0.3, "right": 0.3, "top": 0.3, "bottom": 0.3}


def test_pdf_text_has_no_banned_phrase_or_em_dash(passing_run):
    from style_check import banned_patterns

    out, _, _ = passing_run
    text = subprocess.run(["pdftotext", "-layout", str(out / "resume.pdf"), "-"],
                          capture_output=True, check=True).stdout.decode("utf-8")
    assert "\u2014" not in text
    for listed, pattern in banned_patterns():
        assert not pattern.search(text), listed
    # The only en dashes are in date ranges.
    for line in text.splitlines():
        if "\u2013" in line:
            assert re.search(r"\d{4} \u2013 (?:[A-Z][a-z]{2,8} \d{4}|Present)", line), line


def test_parse_check_passes_with_both_extractors(passing_run):
    from parse_check import parse_check_pdf

    out, _, _ = passing_run
    resume = json.loads((out / "tailored.final.json").read_text())
    result = parse_check_pdf(out / "resume.pdf", resume)
    assert result["passed"] and [p["extractor"] for p in result["passes"]] == ["pdfminer", "pdftotext"]
    for item in result["passes"]:
        assert item["extractability"] == "full" and item["page_count"] == 1
        assert item["content_recall"] >= 0.99 and item["order_fidelity"] >= 0.98
        statuses = {c["id"]: c["status"] for c in item["checks"]}
        assert statuses["contact_info"] == "pass" and statuses["section_headings"] == "pass"
    minered = {c["id"]: c["status"] for c in result["passes"][0]["checks"]}
    assert minered["multi_column"] == "pass" and minered["tables"] == "pass"
    assert len(minered) == 12  # all twelve checks ran on the pdfminer pass


def test_parse_check_fails_on_two_pages_and_tables(tmp_path):
    from compile import compile_tex
    from parse_check import parse_check_pdf

    tex = tmp_path / "table.tex"
    tex.write_text(
        "\\documentclass{article}\\begin{document}\nExperience\n\n"
        "\\begin{tabular}{|l|l|}\\hline Skills & Python \\\\ \\hline Education & BS \\\\ \\hline\\end{tabular}\n"
        "jordan@example.com 555-010-0199 Jan 2020\n\\newpage Second page text\n\\end{document}\n",
        encoding="utf-8",
    )
    compiled = compile_tex(tex)
    assert compiled.ok and compiled.pages == 2
    result = parse_check_pdf(compiled.pdf)
    assert not result["passed"]
    problems = " ".join(result["passes"][0]["problems"])
    assert "2 pages" in problems and "table" in problems


def test_same_posting_twice_gives_the_same_scores(tmp_path, passing_run):
    from run_checks import run

    _, _, first = passing_run
    _, second = run(run_args(tmp_path / "again"))
    assert second["ats"] == first["ats"]
    assert second["parse"] == first["parse"]
    assert second["fit"]["projects_shown"] == first["fit"]["projects_shown"]
    assert second["fit"]["entrygap_pt"] == first["fit"]["entrygap_pt"]


def test_report_has_every_section(passing_run):
    out, _, _ = passing_run
    report = (out / "report.md").read_text(encoding="utf-8")
    for heading in ("## Scores", "### ATS sub-scores", "## Keyword coverage", "## Gaps", "## Page fill",
                    "## Bullets: before and after", "## AI-phrase filter", "## Check results",
                    "### Fact gate (F1-F9)", "### Content rules (C1-C15)", "### Writing rules (W1-W10)",
                    "### Layout rules (L1-L15)", "### Parse check: Tailored", "### Parse check: Baseline"):
        assert heading in report, heading
    assert "| ATS score /10 | 7.9 | 9.2 |" in report
    assert "| Recruiter read /10 | pending | pending |" in report
    assert "Snowflake" in report.split("## Gaps")[1].split("##")[0]
    assert "Before: Wrote a FastAPI service that served model predictions" in report


def test_fact_failure_blocks_before_any_pdf(tmp_path, tailored, job):
    from run_checks import run

    case = next(c for c in load("planted_violations.json")["cases"] if c["rule"] == "F3")
    bad, _ = apply_edits(tailored, job, case["edits"])
    path = tmp_path / "tailored.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    code, results = run(run_args(tmp_path / "out", tailored_path=path))
    assert code == 1 and results["status"] == "blocked"
    assert [b["rule"] for b in results["blocking"]] == ["F3"]
    assert not (tmp_path / "out" / "resume.pdf").exists()
    assert "What blocked this run" in (tmp_path / "out" / "report.md").read_text()
    assert "--attempt 2" in results["next"]


def test_style_failure_removes_the_pdf_and_second_try_stops(tmp_path, tailored, job):
    from run_checks import run

    case = next(c for c in load("planted_violations.json")["cases"] if c["rule"] == "W4")
    bad, _ = apply_edits(tailored, job, case["edits"])
    path = tmp_path / "tailored.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    code, results = run(run_args(tmp_path / "out", tailored_path=path, attempt=2))
    assert code == 1 and "W4" in {b["rule"] for b in results["blocking"]}
    assert not (tmp_path / "out" / "resume.pdf").exists() and results["delivered"] == []
    assert results["next"].startswith("Second failed try")


def test_phrase_filter_fixes_and_reports(tmp_path, tailored):
    from run_checks import run

    bullets = tailored["workExperience"][0]["description"]
    bullets[2] = bullets[2].replace("Added 85 pytest", "Architected 85 pytest")
    path = tmp_path / "tailored.json"
    path.write_text(json.dumps(tailored), encoding="utf-8")
    code, results = run(run_args(tmp_path / "out", tailored_path=path))
    assert [c["phrase"] for c in results["phrase_changes"]] == ["architected"]
    final = json.loads((tmp_path / "out" / "tailored.final.json").read_text())
    assert final["workExperience"][0]["description"][2].startswith("Designed 85 pytest")
    assert "'architected' -> 'designed'" in (tmp_path / "out" / "report.md").read_text()
    assert "W2" not in {b["rule"] for b in results["blocking"]}


def test_short_and_long_bullets_fail_the_layout_check(tmp_path, tailored, master, master_tex):
    from layout_check import check_layout, fit

    bullets = tailored["workExperience"][2]["description"]
    bullets[0] = "Shipped 11 React and TypeScript screens for a patient scheduling tool"  # 1 line
    bullets[1] = bullets[1].split(" that clinic")[0]  # second line under half full
    bullets[2] = bullets[2] + " and " + bullets[2].lower()  # 3 lines
    result, resume = fit(tailored, master, tmp_path)
    assert result.ok
    findings, _ = check_layout(result.pdf, result.tex, resume, master_tex)
    by_rule = {}
    for finding in findings:
        by_rule.setdefault(finding.rule, []).append(finding.where)
    assert "Cascade Health bullet 1" in by_rule["L4"] and "Cascade Health bullet 3" in by_rule["L4"]
    assert by_rule["L5"] == ["Cascade Health bullet 2"]
    assert "Aim for about" in next(f.message for f in findings if f.rule == "L5")


def test_page_short_of_content_fails_l11(tmp_path, tailored, master, master_tex):
    from layout_check import check_layout, fit

    tailored["personalProjects"] = tailored["personalProjects"][:1]
    result, resume = fit(tailored, master, tmp_path)
    assert result.ok and result.entrygap_pt == 9.0 and result.sectiongap_pt == 12.0
    findings, _ = check_layout(result.pdf, result.tex, resume, master_tex)
    assert "L11" in rules(findings, "warn") and "L11" not in rules(findings, "block")


def test_wrapping_lines_are_caught(tmp_path, tailored, master, master_tex):
    from layout_check import check_layout, fit

    tailored["skillGroups"][2]["items"] += ["Ansible", "Helm", "Datadog", "Jenkins", "CircleCI", "Nginx", "Linux", "Vault"]
    tailored["personalProjects"][0]["stack"] += ["SQLAlchemy", "Alembic", "Pydantic", "Uvicorn", "Celery", "RabbitMQ", "Sentry", "Fly.io", "Cloudflare", "OpenTelemetry"]
    result, resume = fit(tailored, master, tmp_path)
    findings, _ = check_layout(result.pdf, result.tex, resume, master_tex)
    assert {"L3", "L7"} <= rules(findings, "warn")
    assert "infra & tools" in next(f.value for f in findings if f.rule == "L3")


def test_tex_rules(master, master_tex, tailored):
    from latex_io import merge_tailored, write_tex
    from layout_check import tex_findings
    from style_check import check_tex_dashes

    resume = merge_tailored(tailored, master, 3)
    good = write_tex(resume)
    assert tex_findings(good, resume, master_tex) == [] and check_tex_dashes(good) == []
    shrunk = good.replace(r"\addtolength{\topmargin}{-0.70in}", r"\addtolength{\topmargin}{-0.85in}")
    assert "L1" in rules(tex_findings(shrunk, resume, master_tex))
    smaller_font = good.replace("letterpaper,10pt", "letterpaper,9pt")
    assert "L1" in rules(tex_findings(smaller_font, resume, master_tex))
    assert "L8" in rules(tex_findings(good.replace(r"\section{PROJECTS}", "\\vspace{-4pt}\n\\section{PROJECTS}"), resume, master_tex))
    assert "L13" in rules(tex_findings(good.replace(r"\section{PROJECTS}", "% \\resumeItem{Old alternate}\n\\section{PROJECTS}"), resume, master_tex))
    assert "L13" in rules(tex_findings(good.replace("pdfkeywords={", "pdfkeywords={Rust, "), resume, master_tex))
    assert "L6" in rules(tex_findings(good.replace(r"label=\textbullet,", ""), resume, master_tex))
    assert "W1" in rules(check_tex_dashes(good.replace("cutting the manual", "-- cutting the manual")))


def test_margins_below_the_minimum_fail_l1(tmp_path, master_tex):
    from compile import compile_tex, probe_geometry
    from latex_io import baseline_resume, read_tex_string
    from layout_check import measure, page_findings

    narrow = master_tex.replace(r"\addtolength{\topmargin}{-0.70in}", r"\addtolength{\topmargin}{-0.85in}")
    path = tmp_path / "narrow.tex"
    path.write_text(narrow, encoding="utf-8")
    compiled = compile_tex(path)
    resume = baseline_resume(read_tex_string(narrow))
    findings = page_findings(measure(compiled.pdf, resume, probe_geometry(path)), resume)
    assert any(f.rule == "L1" and "top margin" in f.message for f in findings)
    assert "L9" in rules(findings)  # the full master is two pages


def test_recruiter_read_is_validated(tmp_path, passing_run):
    from run_checks import run

    out, _, _ = passing_run
    page = json.loads((out / "tailored.final.json").read_text())
    cite = page["workExperience"][0]["description"][0]
    item = {"points": 2, "cite": cite, "note": "names Airflow and PostgreSQL"}
    recruiter = {
        "baseline": {"criteria": [{"points": 1, "cite": "Software Engineer", "note": ""}] + [{"points": 0, "cite": ""}] * 4},
        "tailored": {"criteria": [item, item, item, {"points": 1, "cite": "This line is not on the page"}, {"points": 0, "cite": ""}]},
    }
    path = tmp_path / "recruiter.json"
    path.write_text(json.dumps(recruiter), encoding="utf-8")
    code, results = run(run_args(tmp_path / "out", recruiter=str(path)))
    assert code == 0
    assert results["recruiter"]["baseline"]["score"] == 1 and results["recruiter"]["baseline"]["problems"] == []
    assert results["recruiter"]["tailored"]["score"] == 7
    assert results["recruiter"]["tailored"]["problems"] == ["criterion 4: the cited line is not on this page"]
    assert "| Recruiter read /10 | 1 | 7 |" in (tmp_path / "out" / "report.md").read_text()


def test_margins_match_and_font_sizes_are_few(passing_run):
    from latex_io import merge_tailored, read_tex
    from layout_check import measure
    from compile import probe_geometry

    out, _, _ = passing_run
    master = read_tex(FIXTURES / "sample_master.tex")
    resume = merge_tailored(json.loads((out / "tailored.final.json").read_text()), master)
    measured = measure(out / "resume.pdf", resume, probe_geometry(out / "resume.tex"))
    assert measured.text_margins_in == {"left": 0.3, "right": 0.3}
    assert measured.short_wraps == []
    assert len(measured.font_sizes) <= 4


def test_ragged_right_and_extra_sizes_fail_l14_l15(tmp_path, master, master_tex, tailored):
    from latex_io import merge_tailored, write_tex
    from compile import compile_tex
    from layout_check import check_layout

    tex = write_tex(merge_tailored(tailored, master, 3))
    tex = tex.replace("\\linespread{0.99}\\selectfont", "\\raggedright\\linespread{0.99}\\selectfont", 1)
    tex = tex.replace("{\\small Data Engineer II}", "{\\scriptsize Data Engineer II}", 1)
    path = tmp_path / "ragged.tex"
    path.write_text(tex, encoding="utf-8")
    compiled = compile_tex(path)
    assert compiled.ok
    findings, measured = check_layout(compiled.pdf, path, merge_tailored(tailored, master, 3), None)
    assert "L14" in rules(findings, "block") and "L15" in rules(findings, "warn")
    assert measured.short_wraps


def test_template_check_flags_ragged_right(master_tex):
    from latex_io import template_findings

    ragged = master_tex.replace("\\linespread{0.99}\\selectfont", "\\raggedright\\linespread{0.99}\\selectfont", 1)
    assert "L14" in rules(template_findings(ragged), "warn")  # the master's choice: reported, not blocked
    assert "L14" not in rules(template_findings(master_tex))
