"""Turn Claude's bullet sub-scores into a ranked, checkable selection.

Claude judges each master bullet (D, T, A, I from 0 to 100) and names the
posting requirement it matches. This script does the arithmetic the same way
every time:

  score = 0.4 D + 0.3 T + 0.2 A + 0.1 I          (varunr89's weights, MIT)
  bands: 90+ direct, 75-89 transferable, 60-74 adjacent, 45-59 weak, <45 gap

Two changes from the original rubric:
  - Evidence required. A score must name a requirement that really is in
    job.json. A score with no such requirement is capped at 44.
  - Coverage over repeats. Within an entry, a bullet whose only matched
    requirements are already covered by an earlier pick loses 15 points for
    that pick.

It also ranks projects by the average of their top bullets, says whether 4
bullets per entry is allowed (C6), and lists requirements that no bullet in
the whole master reaches 60 on (reported as gaps).

Input scores.json:
  {"bullets": [{"id": "northwind-analytics-data-engineering-intern.0", "D": 85, "T": 70,
                "A": 60, "I": 90, "requirements": ["Experience with Airflow"]}]}
Bullet ids come from:  latex_io.py bullets master.tex

CLI:
  select_bullets.py --master master.tex --job job.json --scores scores.json [-o selection.json]
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from common import ENTRY_KINDS, dump_json, entry_id, entry_name, load_json, master_bullets, normalize_term
from latex_io import bullet_index, read_tex

WEIGHTS = {"D": 0.4, "T": 0.3, "A": 0.2, "I": 0.1}
NO_EVIDENCE_CAP = 44.0
REPEAT_PENALTY = 15.0  # starting value; tune in testing
KEEP_SCORE = 60.0
BANDS = ((90, "direct"), (75, "transferable"), (60, "adjacent"), (45, "weak"), (0, "gap"))
REQUIREMENT_FIELDS = (
    "required_skills", "preferred_skills", "experience_requirements",
    "education_requirements", "key_responsibilities", "keywords",
)


def band(score: float) -> str:
    return next(name for floor, name in BANDS if score >= floor)


def posting_requirements(job: dict[str, Any]) -> list[str]:
    requirements = job.get("requirements") or {}
    found: dict[str, None] = {}
    for key in REQUIREMENT_FIELDS:
        for item in requirements.get(key) or []:
            if isinstance(item, str) and item.strip():
                found.setdefault(item.strip())
    return list(found)


def _match_requirement(quoted: str, known: list[str]) -> str | None:
    """The posting requirement a quote refers to, or None when it is not there."""
    want = normalize_term(quoted)
    if not want:
        return None
    for item in known:
        have = normalize_term(item)
        if want == have or (len(want) >= 4 and (want in have or have in want)):
            return item
    return None


def score_bullets(master: dict[str, Any], job: dict[str, Any], scores: dict[str, Any]) -> dict[str, Any]:
    known = posting_requirements(job)
    index = {row["id"]: row for row in bullet_index(master)}
    given = {str(row.get("id")): row for row in scores.get("bullets") or [] if isinstance(row, dict)}
    problems = [f"unknown bullet id: {bid}" for bid in given if bid not in index]
    problems += [f"no score given for {bid}" for bid in index if bid not in given]

    scored: dict[str, dict[str, Any]] = {}
    for bid, row in index.items():
        raw = given.get(bid) or {}
        subs = {k: max(0.0, min(100.0, float(raw.get(k) or 0))) for k in WEIGHTS}
        quoted = raw.get("requirements") or ([raw["requirement"]] if raw.get("requirement") else [])
        matched = list(dict.fromkeys(m for q in quoted if (m := _match_requirement(str(q), known))))
        value = sum(subs[k] * w for k, w in WEIGHTS.items())
        capped = not matched and value > NO_EVIDENCE_CAP
        if capped:
            value = NO_EVIDENCE_CAP
        scored[bid] = {
            "id": bid, "entry": row["entry"], "text": row["text"], **subs,
            "score": round(value, 1), "band": band(value), "requirements": matched,
            "capped_no_evidence": capped,
        }

    entries_out: list[dict[str, Any]] = []
    for kind in ENTRY_KINDS:
        for entry in master.get(kind) or []:
            prefix = entry_id(kind, entry)
            pool = [scored[f"{prefix}.{i}"] for i in range(len(master_bullets(entry)))]
            covered: set[str] = set()
            picks: list[dict[str, Any]] = []
            remaining = list(pool)
            while remaining:
                def effective(b: dict[str, Any]) -> float:
                    repeat = bool(b["requirements"]) and set(b["requirements"]) <= covered
                    return b["score"] - (REPEAT_PENALTY if repeat else 0.0)

                best = max(remaining, key=lambda b: (effective(b), b["score"], -pool.index(b)))
                picks.append({**best, "pick_score": round(effective(best), 1)})
                covered |= set(best["requirements"])
                remaining.remove(best)
            top3 = [p["score"] for p in picks[:3]]
            entries_out.append(
                {
                    "kind": kind, "entry": entry_name(kind, entry), "bullets_in_master": len(pool),
                    "rank_score": round(sum(top3) / len(top3), 1) if top3 else 0.0,
                    "keep_count": sum(1 for p in picks if p["score"] >= KEEP_SCORE),
                    "four_at_60": len(picks) >= 4 and all(p["score"] >= KEEP_SCORE for p in picks[:4]),
                    "picks": picks,
                }
            )

    work = [e for e in entries_out if e["kind"] == "workExperience"]
    projects = sorted(
        (e for e in entries_out if e["kind"] == "personalProjects" and e["bullets_in_master"] >= 3),
        key=lambda e: -e["rank_score"],
    )
    dropped = [e["entry"] for e in entries_out if e["kind"] == "personalProjects" and e["bullets_in_master"] < 3]
    short_work = [e["entry"] for e in work if e["bullets_in_master"] < 3]

    best_by_requirement = {req: 0.0 for req in known}
    for row in scored.values():
        for req in row["requirements"]:
            best_by_requirement[req] = max(best_by_requirement[req], row["score"])
    return {
        "weights": WEIGHTS,
        "work_order": [e["entry"] for e in work],
        "project_order": [e["entry"] for e in projects],
        "projects_dropped_too_few_bullets": dropped,
        "work_entries_needing_a_bullet": short_work,
        "four_bullets_allowed_for": [e["entry"] for e in work + projects if e["four_at_60"]],
        "gap_requirements": [req for req, best in best_by_requirement.items() if best < KEEP_SCORE],
        "entries": work + projects,
        "problems": problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score and rank master bullets for one posting.")
    parser.add_argument("--master", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--scores", required=True)
    parser.add_argument("-o", "--output")
    args = parser.parse_args(argv)
    result = score_bullets(read_tex(args.master), load_json(args.job), load_json(args.scores))
    text = dump_json(result, args.output)
    if not args.output:
        sys.stdout.write(text)
    else:
        print(f"wrote {args.output}: projects in order {result['project_order']}")
    if result["problems"]:
        print("problems: " + "; ".join(result["problems"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
