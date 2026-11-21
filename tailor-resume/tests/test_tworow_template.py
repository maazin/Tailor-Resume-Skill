"""The two-row education template: company-first headings, a coursework line,
a coursework bank in comments, 10pt body and ragged-right text."""

import copy

import pytest
from conftest import FIXTURES, needs_pdf_tools, needs_tex, rules

from fact_gate import run_fact_gate
from format_check import check_dates, check_education
from latex_io import merge_tailored, read_tex, template_findings, write_tex

TWO_ROW = FIXTURES / "sample_master_tworow.tex"


@pytest.fixture()
def tworow():
    return read_tex(TWO_ROW)


@pytest.fixture()
def tailored_tworow(tailored):
    tailored = copy.deepcopy(tailored)
    tailored["personalProjects"] = tailored["personalProjects"][:2]
    return tailored


def test_reads_the_template(tworow):
    template = tworow["_template"]
    assert template["work_order"] == ["company", "title", "location", "years"]
    assert template["ragged_right"] is True
    assert template["skills_item"] == r"\item"
    assert template["spacing"] == {"entrygap": 6.0, "sectiongap": 8.0}
    assert template["header"]["title_size"] == ""
    assert template["education_heading_lines"] == 2
    school = tworow["education"][0]
    assert school["coursework"] == ["Data Structures", "Database Systems", "Operating Systems"]
    assert school["coursework_label"] == "Relevant Coursework"
    assert [c["name"] for c in tworow["courseworkBank"]] == [
        "Data Structures", "Software Engineering", "Database Systems",
        "Machine Learning", "Operating Systems", "Computer Networks",
    ]
    assert tworow["courseworkBank"][1] == {
        "group": "Software", "code": "CS 3300", "name": "Software Engineering", "term": "Spring 2025", "grade": "A-",
    }


def test_ragged_right_is_a_warning_not_a_block():
    findings = template_findings(TWO_ROW.read_text(encoding="utf-8"))
    assert [(f.rule, f.severity) for f in findings] == [("L14", "warn")]


def test_coursework_can_be_tailored_from_the_bank(tworow, tailored_tworow, job):
    tailored_tworow["coursework"] = ["Software Engineering", "Database Systems", "Computer Networks"]
    resume = merge_tailored(tailored_tworow, tworow)
    school = resume["education"][0]
    assert school["description"] == ["Relevant Coursework: Software Engineering, Database Systems, Computer Networks"]
    assert "F10" not in rules(run_fact_gate(resume, tworow, [], job))
    tex = write_tex(resume)
    assert r"\resumeItem{\textbf{Relevant Coursework:} Software Engineering, Database Systems, Computer Networks}" in tex
    assert "COURSEWORK BANK" not in tex and "CS 3300" not in tex


def test_made_up_course_is_caught(tworow, tailored_tworow, job):
    tailored_tworow["coursework"] = ["Data Structures", "Distributed Systems"]
    findings = run_fact_gate(merge_tailored(tailored_tworow, tworow), tworow, [], job)
    assert [(f.rule, f.severity, f.value) for f in findings if f.rule == "F10"] == [
        ("F10", "critical", "Distributed Systems")
    ]


def test_no_coursework_key_keeps_the_master_line(tworow, tailored_tworow):
    resume = merge_tailored(tailored_tworow, tworow)
    assert resume["education"][0]["description"] == [
        "Relevant Coursework: Data Structures, Database Systems, Operating Systems"
    ]
    tailored_tworow["coursework"] = []
    assert merge_tailored(tailored_tworow, tworow)["education"][0]["description"] == []


def test_dates_are_written_in_the_house_style(tworow, tailored_tworow, job):
    resume = merge_tailored(tailored_tworow, tworow)
    assert resume["education"][0]["years"] == "August 2022 \u2013 May 2026"
    assert resume["workExperience"][0]["years"] == "May 2025 \u2013 August 2025"
    assert check_dates(resume) == []
    assert "F5" not in rules(run_fact_gate(resume, tworow, [], job))  # same dates, new style
    assert "{May 2025 -- August 2025}" in write_tex(resume)


def test_gpa_in_the_degree_is_a_warning(tworow, tailored_tworow):
    findings = check_education(merge_tailored(tailored_tworow, tworow))
    assert [(f.rule, f.severity) for f in findings] == [("C15", "warn")]


def test_header_and_skills_keep_the_template_sizes(tworow, tailored_tworow):
    tex = write_tex(merge_tailored(tailored_tworow, tworow))
    body = tex.split(r"\begin{document}")[1]
    assert "  Data Engineer II \\\\[3pt]" in body and r"{\small Data Engineer II}" not in body
    assert "\\begin{itemize}[leftmargin=0pt,label={},topsep=0pt,parsep=0pt,itemsep=0pt]\n  \\item\n" in body


@needs_tex
def test_fit_starts_from_the_template_spacing(tmp_path, tworow, tailored_tworow):
    from layout_check import fit

    result, _ = fit(tailored_tworow, tworow, tmp_path)
    assert result.ok
    assert 6.0 <= result.entrygap_pt <= 10.0 and 8.0 <= result.sectiongap_pt <= 12.0


@needs_pdf_tools
def test_layout_and_parse_on_the_two_row_template(tmp_path, tworow, tailored_tworow):
    from compile import compile_tex
    from layout_check import check_layout
    from parse_check import parse_check_pdf

    long_line = ["Data Structures", "Software Engineering", "Database Systems", "Machine Learning",
                 "Operating Systems", "Computer Networks"] * 2
    for courses, wraps in ((["Software Engineering", "Database Systems"], False), (long_line, True)):
        tailored_tworow["coursework"] = courses
        resume = merge_tailored(tailored_tworow, tworow, 2)
        path = tmp_path / f"r{len(courses)}.tex"
        path.write_text(write_tex(resume), encoding="utf-8")
        compiled = compile_tex(path)
        assert compiled.ok, compiled.errors
        findings, measured = check_layout(compiled.pdf, path, resume, TWO_ROW.read_text(encoding="utf-8"))
        assert ("L3" in rules(findings, "warn")) is wraps
        assert "L14" not in rules(findings, "block")  # ragged right is the master's choice
        assert measured.bullets[0].section == "education"
        school_heading = measured.headings[0]
        assert school_heading["section"] == "education" and school_heading["lines"] == 2
        assert not [f for f in findings if f.rule == "L2" and f.where == "education"]
        if not wraps:
            result = parse_check_pdf(compiled.pdf, resume)
            for item in result["passes"]:
                assert item["content_recall"] >= 0.95, item["problems"]
                assert item["order_fidelity"] >= 0.9, item


@needs_tex
def test_work_headings_follow_jakes_resume(tworow, tailored_tworow, tmp_path):
    """C16: title | dates on row 1, company | location on row 2, the macro has no tabular."""
    from compile import compile_tex
    from format_check import check_headings
    from parse_check.extract import extract_file

    resume = merge_tailored(tailored_tworow, tworow, 2)
    tex = write_tex(resume)
    assert r"\textbf{#1}\hfill #2\par" in tex and "IfSubStr" not in tex.split(r"\begin{document}")[0].split("resumeSubheading}[4]")[1][:80]
    path = tmp_path / "jake.tex"
    path.write_text(tex, encoding="utf-8")
    compiled = compile_tex(path)
    assert compiled.ok, compiled.errors
    text = extract_file(compiled.pdf).text
    job = resume["workExperience"][0]
    assert text.index(job["title"]) < text.index(job["company"])
    assert check_headings(resume) == []
    bad = dict(job, title=f"{job['title']} | {job['company']}")
    assert {f.rule for f in check_headings({"workExperience": [bad]})} == {"C16"}
