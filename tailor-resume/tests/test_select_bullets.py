"""Bullet scoring arithmetic: weights, the evidence cap, the repeat penalty, ranking."""

from latex_io import bullet_index
from select_bullets import band, score_bullets


def _scores(master, overrides=None, default=None):
    default = default or {"D": 50, "T": 50, "A": 50, "I": 50, "requirements": ["Python"]}
    rows = []
    for row in bullet_index(master):
        rows.append({"id": row["id"], **default, **(overrides or {}).get(row["id"], {})})
    return {"bullets": rows}


def test_weighted_score_and_bands(master, job):
    nw = "northwind-analytics-data-engineering-intern"
    result = score_bullets(master, job, _scores(master, {
        f"{nw}.0": {"D": 90, "T": 80, "A": 70, "I": 100, "requirements": ["Experience with Airflow or a similar scheduler"]},
    }))
    assert result["problems"] == []
    pick = result["entries"][0]["picks"][0]
    assert pick["id"] == f"{nw}.0" and pick["score"] == 84.0 and pick["band"] == "transferable"
    assert [band(s) for s in (95, 80, 65, 50, 10)] == ["direct", "transferable", "adjacent", "weak", "gap"]


def test_score_without_a_real_requirement_is_capped(master, job):
    nw = "northwind-analytics-data-engineering-intern"
    result = score_bullets(master, job, _scores(master, {
        f"{nw}.0": {"D": 95, "T": 95, "A": 95, "I": 95, "requirements": ["Blockchain expertise"]},
        f"{nw}.1": {"D": 95, "T": 95, "A": 95, "I": 95, "requirements": []},
    }))
    picks = {p["id"]: p for p in result["entries"][0]["picks"]}
    assert picks[f"{nw}.0"]["score"] == 44.0 and picks[f"{nw}.0"]["capped_no_evidence"]
    assert picks[f"{nw}.1"]["score"] == 44.0


def test_repeat_penalty_prefers_new_coverage(master, job):
    nw = "northwind-analytics-data-engineering-intern"
    same = {"D": 80, "T": 80, "A": 80, "I": 80, "requirements": ["Airflow"]}
    result = score_bullets(master, job, _scores(master, {
        f"{nw}.0": {**same, "D": 90},
        f"{nw}.1": same,
        f"{nw}.3": {"D": 70, "T": 70, "A": 70, "I": 70, "requirements": ["dbt"]},
    }))
    picks = result["entries"][0]["picks"]
    assert [p["id"][-1] for p in picks[:3]] == ["0", "3", "1"]  # 70 for new coverage beats 80 - 15
    assert picks[2]["pick_score"] == 65.0 and picks[2]["score"] == 80.0


def test_projects_ranked_and_gaps_listed(master, job):
    high = {"D": 90, "T": 90, "A": 90, "I": 90, "requirements": ["Kubernetes"]}
    overrides = {f"queuewatch.{i}": high for i in range(3)}
    result = score_bullets(master, job, _scores(master, overrides))
    assert result["project_order"][0] == "QueueWatch"
    assert result["work_order"] == ["Northwind Analytics", "Alpine Robotics Lab", "Cascade Health"]
    assert "Snowflake" in result["gap_requirements"] and "Kubernetes" not in result["gap_requirements"]
    assert result["four_bullets_allowed_for"] == []


def test_missing_and_unknown_ids_are_reported(master, job):
    scores = _scores(master)
    scores["bullets"].pop()
    scores["bullets"].append({"id": "nope.0", "D": 1, "T": 1, "A": 1, "I": 1})
    problems = score_bullets(master, job, scores)["problems"]
    assert any("unknown bullet id: nope.0" in p for p in problems)
    assert any(p.startswith("no score given for pixelsort.2") for p in problems)
