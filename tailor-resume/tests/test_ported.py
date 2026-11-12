"""The code taken from Resume-Matcher runs on a hand-built dict."""

from ai_phrases import AI_PHRASE_BLACKLIST, remove_ai_phrases
from ats_score import WEIGHTS, compute_ats_score, score_resume
from keywords import (
    analyze_keyword_gaps,
    calculate_keyword_match,
    job_keyword_terms,
    near_variant,
    validate_master_alignment,
)
from text_match import keyword_in_text

RESUME = {
    "personalInfo": {"name": "A B", "title": "Backend Engineer", "email": "a@example.com"},
    "workExperience": [
        {"title": "Engineer", "company": "Acme", "years": "Jan 2020 – Mar 2023",
         "description": ["Built APIs in Python and Postgres", "Ran data-quality checks"]}
    ],
    "education": [{"institution": "State U", "degree": "BS CS", "years": "Aug 2016 – May 2020"}],
    "personalProjects": [{"name": "Tool", "stack": ["Go", "Docker"], "description": ["Wrote a CLI"]}],
    "additional": {"technicalSkills": ["Python", "Go", "Docker"]},
}
JD = {
    "role": "Backend Engineer",
    "required_skills": ["Python", "PostgreSQL", "Java"],
    "preferred_skills": ["Docker"],
    "keywords": ["data quality", "Kafka"],
}


def test_keyword_in_text_is_whole_term():
    assert keyword_in_text("java", "we use javascript") is False
    assert keyword_in_text("java", "we use Java and Go") is True
    assert keyword_in_text("C++", "C++ and C#") is True


def test_job_keyword_terms_keep_posting_order():
    assert job_keyword_terms(JD) == ["Python", "PostgreSQL", "Java", "Docker", "data quality", "Kafka"]


def test_keyword_match_uses_aliases_stack_and_hyphens():
    # Python, PostgreSQL (as Postgres), Docker (stack), data quality (hyphenated): 4 of 6
    assert round(calculate_keyword_match(RESUME, JD), 1) == 66.7


def test_gap_split():
    master = {**RESUME, "workExperience": [{**RESUME["workExperience"][0], "alternates": ["Ported a Java service"]}]}
    gaps = analyze_keyword_gaps(JD, RESUME, master, confirmed=[])
    assert gaps.missing_keywords == ["Java", "Kafka"]
    assert gaps.injectable_keywords == ["Java"]  # only in a commented-out alternate
    assert gaps.non_injectable_keywords == ["Kafka"]
    assert round(gaps.potential_match_percentage, 1) == 83.3
    confirmed = analyze_keyword_gaps(JD, RESUME, master, confirmed=["Kafka"])
    assert confirmed.non_injectable_keywords == []


def test_master_alignment():
    tailored = {
        **RESUME,
        "additional": {"technicalSkills": ["Python 3", "Rust", "Kafka"]},
        "workExperience": [{**RESUME["workExperience"][0], "company": "Globex"}],
    }
    report = validate_master_alignment(tailored, RESUME, {"Kafka"})
    kinds = {(v.violation_type, v.value) for v in report.violations}
    assert ("skill_variant", "python 3") in kinds
    assert ("fabricated_skill", "rust") in kinds
    assert ("fabricated_company", "globex") in kinds
    assert not any(v.value == "kafka" for v in report.violations)
    assert report.is_aligned is False


def test_near_variant_is_not_substring_matching():
    assert near_variant("Python 3", "Python")
    assert not near_variant("Java", "JavaScript")
    assert not near_variant("C", "C#")
    assert near_variant("Postgres", "PostgreSQL")


def test_ats_score_weights_and_dropout():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
    full = compute_ats_score(RESUME, JD, 50.0, [], [])
    assert full["sub_scores"]["title_match"] == 100.0
    assert full["sub_scores"]["date_consistency"] == 100.0
    assert full["sub_scores"]["section_completeness"] == 80.0  # no summary section
    no_title = compute_ats_score(RESUME, {**JD, "role": ""}, 50.0, [], [])
    assert no_title["sub_scores"]["title_match"] is None
    scored = {k: v for k, v in no_title["sub_scores"].items() if v is not None}
    expected = sum(v * WEIGHTS[k] for k, v in scored.items()) / sum(WEIGHTS[k] for k in scored)
    assert no_title["overall_score"] == round(expected, 1)


def test_score_resume_is_out_of_ten_and_repeatable():
    job = {"title": "Backend Engineer", "requirements": JD}
    first = score_resume(RESUME, RESUME, job)
    assert first == score_resume(RESUME, RESUME, job)
    assert 0 <= first["score_out_of_10"] <= 10
    assert first["score_out_of_10"] == round(first["score_out_of_100"] / 10, 1)


def test_ai_phrase_list_has_59_entries():
    assert len(AI_PHRASE_BLACKLIST) == 59


def test_remove_ai_phrases_replaces_and_reports():
    data = {
        "workExperience": [{"years": "Jan 2020 -- Mar 2023", "description": [
            "Spearheaded a robust pipeline in order to cut cost",
            "Cut latency -- by a lot",
        ]}]
    }
    cleaned, changes = remove_ai_phrases(data)
    bullets = cleaned["workExperience"][0]["description"]
    assert bullets[0] == "Led a strong pipeline to cut cost"
    assert bullets[1] == "Cut latency, by a lot"
    assert cleaned["workExperience"][0]["years"] == "Jan 2020 -- Mar 2023"  # dates untouched
    assert {c["phrase"] for c in changes} == {"spearheaded", "robust", "in order to", "--"}
