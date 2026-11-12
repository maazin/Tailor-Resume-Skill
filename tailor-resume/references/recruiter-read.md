# Recruiter read (score out of 10)

The ATS score comes from code. This score covers what code cannot see: how the page reads to a person who gives it a short first look. Score the baseline first, then the tailored resume, from the text of each PDF (`pdftotext -layout`).

## Criteria

Five criteria, 0 to 2 points each.

| # | Criterion | 0 | 1 | 2 |
| --- | --- | --- | --- | --- |
| 1 | The headline and most recent role read as this job | Neither points at this role | One of them does | The headline is the posting's title and the most recent role's first bullet is work this job does |
| 2 | The top third of the page shows the posting's three most important requirements | None or one is visible there | Two are visible | All three are visible in the skills lines or the first role |
| 3 | Bullets show evidence: numbers, scale or a concrete result | Mostly duties | About half carry a result | Nearly every bullet ends in a number, a scale or a concrete result |
| 4 | The projects shown are relevant to this posting | Unrelated | Mixed | Each project uses the posting's tools or solves its kind of problem |
| 5 | Plain, specific language with no filler | Buzzwords and vague scope | Some filler | Every bullet says what was done, with what, and what came of it |

## Rules

- Every point must cite a line on the page, copied exactly. A point with no citation does not count. `run_checks.py` checks that each cited line is on that page.
- Score the baseline first, before looking at the tailored page again.
- Give no point for having been changed. A bullet earns points for what it says.
- Name the posting's three most important requirements once, before scoring, and use the same three for both pages.
- When unsure between two scores, give the lower one.

## recruiter.json

```json
{
  "top_requirements": ["Python and SQL", "Airflow", "PostgreSQL"],
  "baseline": {
    "criteria": [
      {"points": 1, "cite": "exact line from the baseline page", "note": "why"},
      {"points": 0, "cite": "", "note": "why"},
      {"points": 2, "cite": "...", "note": ""},
      {"points": 1, "cite": "...", "note": ""},
      {"points": 2, "cite": "...", "note": ""}
    ]
  },
  "tailored": {"criteria": ["five items, same shape"]}
}
```

Pass it to `run_checks.py --recruiter recruiter.json`. The report shows both totals next to the ATS scores.
