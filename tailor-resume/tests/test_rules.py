"""Every fact, content and writing rule catches its planted violation, and the
passing sample trips none of them."""

import pytest
from conftest import apply_edits, load, rules

from common import is_blocking
from fact_gate import run_fact_gate
from format_check import clean_job_title, expected_location, run_format_check
from latex_io import merge_tailored
from layout_check import text_findings
from style_check import banned_patterns, run_style_check, word_forms

CASES = load("planted_violations.json")["cases"]


def run_check(check, tailored, job, master):
    resume = merge_tailored(tailored, master)
    if check == "fact":
        return run_fact_gate(resume, master, [], job)
    if check == "format":
        return run_format_check(resume, master, job)
    if check == "style":
        return run_style_check(resume, master, job)
    return text_findings(resume)


@pytest.mark.parametrize("check", ["fact", "format", "style", "layout_text"])
def test_passing_sample_is_clean(check, tailored, job, master):
    findings = run_check(check, tailored, job, master)
    assert [f for f in findings if f.severity != "info"] == []


@pytest.mark.parametrize("case", CASES, ids=[f"{c['rule']}-{c['what']}" for c in CASES])
def test_planted_violation_is_caught(case, tailored, job, master):
    bad_tailored, bad_job = apply_edits(tailored, job, case["edits"])
    findings = run_check(case["check"], bad_tailored, bad_job, master)
    assert case["rule"] in rules(findings), [f"{f.rule}: {f.message}" for f in findings]


def test_every_rule_has_a_planted_case():
    covered = {c["rule"] for c in CASES}
    wanted = {f"F{i}" for i in range(1, 10)} | {f"W{i}" for i in range(1, 11)}
    wanted |= {f"C{i}" for i in range(1, 15)} - {"C9"}  # C9 is checked by C8's test
    assert wanted <= covered, sorted(wanted - covered)


@pytest.mark.parametrize("rule", ["F1", "F2", "F3", "F4", "F5", "F7"])
def test_fact_gate_blocks_what_the_prd_lists(rule, tailored, job, master):
    case = next(c for c in CASES if c["rule"] == rule)
    bad_tailored, bad_job = apply_edits(tailored, job, case["edits"])
    findings = run_check("fact", bad_tailored, bad_job, master)
    assert rule in rules(findings, "critical")


@pytest.mark.parametrize("rule", ["F6", "F8", "F9"])
def test_fact_gate_warnings_do_not_block(rule, tailored, job, master):
    case = next(c for c in CASES if c["rule"] == rule)
    bad_tailored, bad_job = apply_edits(tailored, job, case["edits"])
    findings = [f for f in run_check("fact", bad_tailored, bad_job, master) if f.rule == rule]
    assert findings and all(f.severity == "warn" for f in findings)


def test_confirmed_skill_is_allowed_in_skills_but_not_in_a_bullet(tailored, job, master):
    tailored["skillGroups"][0]["items"].append("Snowflake")
    resume = merge_tailored(tailored, master)
    assert "F1" not in rules(run_fact_gate(resume, master, ["Snowflake"], job), "critical")
    tailored["workExperience"][0]["description"][1] = tailored["workExperience"][0]["description"][1].replace(
        "Modeled 9 dbt tables", "Modeled 9 dbt tables in Snowflake"
    )
    resume = merge_tailored(tailored, master)
    findings = run_fact_gate(resume, master, ["Snowflake"], job)
    assert any(f.rule == "F4" and f.severity == "critical" and f.value == "Snowflake" for f in findings)


def test_number_shortened_with_k_is_supported(tailored, job, master):
    # The master says "1.2K ranger reports"; 310 malformed rows is an alternate bullet.
    bullet = tailored["personalProjects"][0]["description"]
    bullet[0] = bullet[0].replace("900 weekly requests", "900 weekly requests and 310 rejected rows")
    resume = merge_tailored(tailored, master)
    assert "F3" not in rules(run_fact_gate(resume, master, [], job))


def test_rewrite_rules_block_once_then_warn(tailored, job, master):
    case = next(c for c in CASES if c["rule"] == "W8")
    bad_tailored, _ = apply_edits(tailored, job, case["edits"])
    hedge = [f for f in run_check("style", bad_tailored, job, master) if f.rule == "W8"]
    assert hedge and is_blocking(hedge[0], attempt=1) and not is_blocking(hedge[0], attempt=2)


def test_banned_word_inside_a_tool_name_is_allowed(tailored, job, master):
    master["additional"]["technicalSkills"].append("Dynamic Yield")
    master["workExperience"][0]["description"][0] += " with Dynamic Yield"
    bullets = tailored["workExperience"][0]["description"]
    bullets[0] = bullets[0].replace("in Python and Airflow", "in Python, Dynamic Yield and Airflow")
    resume = merge_tailored(tailored, master)
    assert "W2" not in rules(run_style_check(resume, master, job))


def test_user_banned_words_file(tmp_path, tailored, job, master):
    extra = tmp_path / "banned-words.txt"
    extra.write_text("# mine\nreconciliation\n", encoding="utf-8")
    resume = merge_tailored(tailored, master)
    assert "W2" in rules(run_style_check(resume, master, job, banned_file=extra))
    assert any(listed == "reconciliation" for listed, _ in banned_patterns(extra))


def test_word_forms():
    assert {"leverage", "leveraged", "leveraging", "leverages"} <= word_forms("leverage")
    assert {"utilize", "utilized", "utilizing", "utilization"} <= word_forms("utilize")
    assert "seamlessly" in word_forms("seamless")


@pytest.mark.parametrize(
    "raw, clean",
    [
        ("Data Engineer II - Req 4471 (Remote)", "Data Engineer II"),
        ("Senior Software Engineer, Backend", "Senior Software Engineer, Backend"),
        ("Software Engineer (Backend) - Austin, TX", "Software Engineer (Backend)"),
        ("ML Engineer | Remote | R12345", "ML Engineer"),
        ("Software Engineer, New Grad (2026) - US", "Software Engineer, New Grad (2026)"),
        ("Staff Engineer - Platform", "Staff Engineer - Platform"),
        ("Backend Engineer (Hybrid) #88213", "Backend Engineer"),
    ],
)
def test_clean_job_title(raw, clean):
    assert clean_job_title(raw, ["Austin, TX"]) == clean


@pytest.mark.parametrize(
    "locations, workplace, home, line, rule",
    [
        (["Austin, Texas, United States"], "onsite", "Denver, CO", "Relocating to Austin, TX", "C2"),
        (["Austin, TX"], "hybrid", "Denver, CO", "Relocating to Austin, TX", "C2"),
        (["Austin, TX", "New York, NY (HQ)"], "onsite", "Denver, CO", "Denver, CO", "C3"),
        (["Austin, TX"], "remote", "Denver, CO", "Denver, CO", "C3"),
        (["Remote (US)", "Austin, TX"], "unknown", "Denver, CO", "Denver, CO", "C3"),
        (["Denver, Colorado"], "onsite", "Denver, CO", "Denver, CO", "C4"),
        (["Austin, TX"], "onsite", "", "Relocating to Austin, TX", "C2"),
    ],
)
def test_relocating_line(locations, workplace, home, line, rule):
    got_line, got_rule, home_unknown = expected_location(
        {"locations": locations, "workplace_type": workplace}, home
    )
    assert (got_line, got_rule) == (line, rule)
    assert home_unknown == (not home and rule == "C2")
