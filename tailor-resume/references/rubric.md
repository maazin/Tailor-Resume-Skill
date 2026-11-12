# Bullet scoring rubric

Adapted from varunr89/resume-tailoring-skill, `matching-strategies.md` (MIT). Scored per bullet here, with two changes marked below.

## Sub-scores

Give every master bullet four sub-scores from 0 to 100. Score all bullets, including the commented-out alternates. `scripts/select_bullets.py` combines them:

```
score = 0.4 D + 0.3 T + 0.2 A + 0.1 I
```

| Sub-score | Weight | 90+ | 70-89 | 50-69 | Under 50 |
| --- | --- | --- | --- | --- | --- |
| D: direct match | 40% | Same skill, same domain, same kind of outcome the posting asks for | Same skill, different domain | Overlapping keywords, similar outcome | Weak direct match |
| T: transferable | 30% | Same skill in a different context, needs no stretch to explain | Mostly transferable, some translation needed | Needs an analogy | A stretch |
| A: adjacent | 20% | Used the skill as a side task, or a closely related tool | Related but distinct | Needs explaining | Loosely related |
| I: impact fit | 10% | The result type the posting values (numbers, scale, team outcomes) | Strong fit | Moderate fit | Weak fit |

## Bands

| Score | Band | Use |
| --- | --- | --- |
| 90-100 | direct | Use with confidence |
| 75-89 | transferable | Strong candidate |
| 60-74 | adjacent | Fine with rewording toward the posting's terms |
| 45-59 | weak | Only if nothing better exists in that entry |
| Under 45 | gap | Do not use to cover a requirement |

## Change 1: evidence required

Every score must name the posting requirement it matches, quoted from `job.json` `requirements` (any of required_skills, preferred_skills, experience_requirements, education_requirements, key_responsibilities, keywords). Put the quote in `requirements`. The script checks that the quote really is in `job.json`. A score with no matching requirement is capped at 44.

## Change 2: coverage over repeats

Within one entry the script picks the top bullet, then the next ones. A bullet whose only matched requirements are already covered by an earlier pick in that entry loses 15 points for that pick. The 15 is a starting value.

## What the script reports

- Projects ranked by the average of their top 3 bullets. Take them in that order.
- Whether an entry has 4 bullets scoring 60 or more (needed for N = 4, rule C6).
- Requirements that no bullet in the whole master reaches 60 on. Report these as gaps even when the keyword appears somewhere.

## scores.json

```json
{
  "bullets": [
    {
      "id": "northwind-analytics-data-engineering-intern.0",
      "D": 90, "T": 80, "A": 70, "I": 85,
      "requirements": ["Experience with Airflow or a similar scheduler"]
    }
  ]
}
```

Bullet ids come from `python scripts/latex_io.py bullets master.tex`.

## Rewording a kept bullet

Reword only when the bullet scores 60 or more and its language differs from the posting's. The meaning must hold, and every fact must stay inside the same master entry.

| Strategy | Before | After | When |
| --- | --- | --- | --- |
| Keyword alignment | Led experimental design and data analysis programs | Led data science programs combining experimental design and statistical analysis | The posting uses another term for the same work |
| Emphasis shift | Designed statistical experiments, saving millions in recall costs | Prevented millions in recall costs through risk detection with statistical modeling | The posting values outcomes over methods |
| Abstraction level | Built MATLAB-based automated system for evaluation | Built automated evaluation system in MATLAB | The posting cares about the tool, or does not |
| Scale emphasis | Managed project with 3 teams | Coordinated a project across 3 teams | The posting stresses cross-team work |

Never reword by adding a tool, number, employer or outcome the master entry does not have. The fact gate blocks it.
