# tailor-resume

A Claude skill that tailors a resume to one job posting. Give it a job link (Greenhouse, Lever, Ashby) or a pasted job description and it returns a one-page LaTeX resume with its compiled PDF, a list of skill gaps, and scores out of 10 for the baseline and tailored versions.

Every fact in the output must trace back to your master resume or a skill you have confirmed. Claude does the judgment (reading the posting, choosing and rewording bullets). Python scripts do everything that must be repeatable: fetching, keyword math, the fact gate, rendering, layout and the parse check.

## Layout

```
tailor-resume/        the skill: SKILL.md, references/, scripts/, tests/
```

Your own data stays out of this repo, in a separate folder:

```
Resume Lab/
├── master.tex            every bullet you have written, in your own template
├── confirmed-skills.md   gap skills you have confirmed
├── banned-words.txt      extra words you never want
└── applications/<company>-<role>/   job.json, resume.tex, resume.pdf, report.md
```

## Requirements

- Python 3.11+ and `pip install -r tailor-resume/requirements.txt` (pdfminer.six)
- `pdflatex` from TeX Live
- `pdftotext` from poppler

## Try it on the sample

```bash
python tailor-resume/scripts/run_checks.py --master tailor-resume/tests/fixtures/sample_master.tex --tailored tailor-resume/tests/fixtures/sample_tailored.json --job tailor-resume/tests/fixtures/sample_job.json --out-dir /tmp/tailor-sample
```

It writes `resume.tex`, `resume.pdf`, `report.md` and `results.json` to the output folder. The sample person, employers and posting are made up.

## Tests

```bash
python -m pytest tailor-resume/tests
```

Tests that compile a PDF are skipped when `pdflatex` or `pdftotext` is missing.

## Rules the code enforces

| Set | What | Script |
| --- | --- | --- |
| F1 to F9 | Fact gate: skills, employers, numbers, tools and dates match the master, entry by entry | `fact_gate.py` |
| C1 to C14 | Content: job title line, relocating line, bullet count, tense, digits, tool spelling, dates | `format_check.py` |
| W1 to W10 | Writing: no dashes, banned words, tacked-on clauses, filler triads, repeated verbs, hedges | `style_check.py` |
| L1 to L13 | Layout, measured on the PDF: margins, 2-line bullets, one full page | `layout_check.py` |
| Parse check | Text extracts cleanly with pdfminer and pdftotext | `parse_check/` |

## Credits

Built on Resume-Matcher (Apache-2.0), career-ops, varunr89/resume-tailoring-skill, berenslab/llm-excess-vocab, blader/humanizer and Jake's Resume (all MIT). See `tailor-resume/THIRD_PARTY.md` for what was taken and what changed.
