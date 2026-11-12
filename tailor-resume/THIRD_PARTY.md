# Third-party sources

This skill reuses code, rules and data from the projects below. License texts are in `licenses/`. Upstream files were read at the commits listed, on 2026-11-11.

| Source | License | Commit | Used in | What changed |
| --- | --- | --- | --- | --- |
| [Resume-Matcher](https://github.com/srbhr/Resume-Matcher) `apps/backend/app/services/text_match.py` | Apache-2.0 | 950b700 | `scripts/text_match.py` | No logic change; non-ASCII ranges written as escapes |
| Resume-Matcher `services/ats.py` | Apache-2.0 | 950b700 | `scripts/ats_score.py` | Reads the LaTeX reader's dict; alias matching; recommendations text removed; `score_resume` wrapper returns the score out of 10 |
| Resume-Matcher `services/refiner.py` (`analyze_keyword_gaps`, `calculate_keyword_match`, `job_keyword_terms`, `validate_master_alignment`, `_extract_all_text`) | Apache-2.0 | 950b700 | `scripts/keywords.py` | Dataclasses instead of Pydantic; posting order instead of set order; confirmed skills as the allow-list; whole-term variant matching; also checks schools |
| Resume-Matcher `services/refiner.py` (`remove_ai_phrases`) and `prompts/refinement.py` (phrase lists) | Apache-2.0 | 950b700 | `scripts/ai_phrases.py`, `scripts/style_check.py` | Posting words are no longer protected; each change is returned for the report; dates and links are skipped |
| Resume-Matcher `services/ats_parse/` (`__init__.py`, `extract.py`, `checks.py`, `roundtrip.py`, `headings.py`, `normalize.py`, `models.py`) and `_MD_DATE_RE` from `services/parser.py` | Apache-2.0 | 950b700 | `scripts/parse_check/` | DOCX path removed; second pass with pdftotext; tables and text-box checks read the PDF; field order follows the one-line heading; contact order read from the user's template; dataclasses instead of Pydantic |
| Resume-Matcher `prompts/templates.py` (`EXTRACT_KEYWORDS_PROMPT` shape, truthfulness rules) | Apache-2.0 | 950b700 | `references/prompts.md` | Rule 9 changed: skills may be dropped to fit the page, never added unless confirmed |
| [career-ops](https://github.com/career-ops-hq/career-ops) `verify-cv-facts.mjs`, `cv-title-check.mjs` | MIT | 775861b | `scripts/fact_gate.py` | Rules only, rewritten in Python. Numbers and tools must appear in the same master entry, not anywhere in the sources |
| [varunr89/resume-tailoring-skill](https://github.com/varunr89/resume-tailoring-skill) `matching-strategies.md` | MIT | 9a4a0f2 | `references/rubric.md`, `scripts/select_bullets.py` | Scored per bullet; evidence cap at 44; 15-point repeat penalty |
| [berenslab/llm-excess-vocab](https://github.com/berenslab/llm-excess-vocab) `results/excess_words.csv` | MIT | 53db991 | `references/excess_words.csv`, `scripts/style_check.py` | File copied unchanged; everyday words are left out in code (W3) |
| [blader/humanizer](https://github.com/blader/humanizer) `SKILL.md` | MIT | b48c832 | `scripts/style_check.py`, `references/prompts.md` | The patterns that show up in resume bullets became rules W1 to W10, as code checks. No text copied |
| [Jake's Resume](https://github.com/jakegut/resume) `resume.tex` | MIT | 3e64a93 | `scripts/latex_io.py`, `tests/fixtures/sample_master.tex` | The macro names the reader understands and the `glyphtounicode` lines. Headings print on one line; no tabular |

Research the writing rules rest on: Kobak et al., "Delving into LLM-assisted writing in biomedical publications through excess vocabulary" (arXiv 2406.07016); Reinhart et al., "Do LLMs write like humans? Variation in grammatical and rhetorical styles" (arXiv 2410.16107).

Apache-2.0 notice: every file adapted from Resume-Matcher starts with a "Modified from Resume-Matcher" header that lists the changes. The license text is in `licenses/Resume-Matcher-LICENSE-Apache-2.0.txt`.
