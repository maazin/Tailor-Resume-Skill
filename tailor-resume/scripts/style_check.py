"""Writing rules W1 to W10, checked in code. A blocking failure stops the PDF.

  W1  no em dash; no en dash outside a date range            block
  W2  no banned word or phrase, in any form                  block
  W3  at most one Kobak style word per bullet, and the
      page's rate no higher than the master's                block
  W4  no tacked-on "-ing" clause at the end of a bullet       block
  W5  verbs over nouns (more than 2 nominalizations)          rewrite once, then warn
  W6  no filler triads of abstract qualities                 rewrite (Claude decides)
  W7  no repeated opening verb in an entry; no verb opens
      more than 2 bullets on the page                        block
  W8  no hedges or vague scope                               rewrite once, then warn
  W9  every bullet names a concrete thing from the master    warn
  W10 plain formatting: no bold, italics, curly quotes, emoji block

Sources: Kobak et al. excess vocabulary (references/excess_words.csv, MIT),
Reinhart et al. on participle clauses and nominalizations, blader/humanizer
(MIT) for the patterns, Resume-Matcher (Apache-2.0) for the AI-phrase list.

CLI:
  style_check.py --master master.tex --tailored tailored.json
                 [--job job.json] [--confirmed confirmed-skills.md]
                 [--banned "Resume Lab/banned-words.txt"] [--attempt 1]
Exit code 1 when any finding blocks on this attempt.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from ai_phrases import AI_PHRASE_BLACKLIST, _phrase_pattern
from common import (
    ENTRY_KINDS,
    REFERENCES,
    Finding,
    bullets,
    dump_json,
    entries,
    entry_name,
    entry_source_text,
    find_master_entry,
    first_word,
    is_blocking,
    load_json,
    mask_terms,
    master_bullets,
    numbers_in,
    page_view,
    read_confirmed_skills,
    read_word_file,
    term_in_text,
    tool_vocabulary,
    words,
)
from latex_io import merge_tailored, read_tex

MAX_STYLE_WORDS_PER_BULLET = 1
MAX_NOMINALIZATIONS = 2
MAX_SAME_VERB_ON_PAGE = 2

# Words on Kobak's style list that are ordinary in a resume. Not counted (W3).
EVERYDAY = frozenset(
    """
    this these using within during including across address addresses addressing
    analysis approach background based between both challenge challenges conditions
    into like need research role their through were while statement identified
    observed hold holds faces fight offer offers offering integration techniques
    strategies findings impact however despite primary potential leading linked
    announced declared serving understanding capabilities outcomes assessments
    initially remotely limitations individuals closures postponed conducted
    maintaining providing involving involves complex distinct precise hardest
    resuming wandering overwhelmed afflicted apologizes assess assessed assessing
    categorized deeper demonstrated focusing inquiries investigation minimizing
    optimizing particularly primarily specifically typically ultimately subsequently
    standardizing customizing integrating integrates incorporates incorporating
    emerged emerges remains revealed reveals examines explores evaluates verifies
    exceeding featuring preserving recognizing attributed aiding easing align
    aligning aligns alongside amid additionally advancing achieving
    """.split()
)

TACKED_ON = (
    "ensuring", "highlighting", "showcasing", "fostering", "enabling", "demonstrating",
    "contributing to", "underscoring", "emphasizing", "reflecting", "allowing",
    "empowering", "driving", "facilitating", "paving", "leading to", "illustrating",
    "reinforcing", "positioning", "solidifying", "bolstering", "enhancing",
)

# -tion/-ment/-ance/-ence/-ity words that name a technical thing, not an action
# turned into a noun.
NOT_NOMINALIZATIONS = frozenset(
    """
    authentication authorization classification regression application applications
    deduplication migration migrations deployment deployments environment environments
    configuration validation integration replication virtualization tokenization
    serialization pagination production function functions transaction transactions
    security latency capacity attribution generation recommendation recommendations
    notification notifications subscription subscriptions reconciliation simulation
    instrumentation observability accessibility university community facility city
    quantity identity entity entities priority activity utility payment payments
    document documents segment segments element elements component components
    experiment experiments requirement requirements statement instance instances
    science sequence reference inference audience conference evidence distance
    compliance insurance finance balance variance station question questions
    location locations section sections version versions session sessions
    partition partitions permission permissions exception exceptions injection
    detection prediction predictions resolution retention animation documentation
    information organization department government investment management
    appointment appointments assignment assignments shipment equipment
    """.split()
)
_NOMINAL_SUFFIX = re.compile(r"(?:tion|tions|sion|sions|ment|ments|ance|ence|ity|ities)$")

ABSTRACT = frozenset(
    """
    performance speed quality security safety cost trust scale impact growth value
    insight insights innovation excellence collaboration communication leadership
    teamwork ownership agility velocity clarity integrity resilience
    """.split()
)
_ABSTRACT_SUFFIX = re.compile(r"(?:ity|ness|ance|ence|tion|sion|ship|ism|acy)$")

HEDGES = (
    "helped", "help", "helping", "assisted in", "assisted with", "assisted", "worked on",
    "various", "several", "approximately", "successfully", "responsible for",
    "participated in", "numerous", "a variety of", "a number of", "etc",
)

_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
_CURLY = re.compile("[\u2018\u2019\u201C\u201D]")
_MARKUP = re.compile(
    r"\\(?:textbf|textit|emph|underline|bfseries|itshape)\b|\*\*|__|</?(?:b|i|em|strong)>"
    r"|(?<![\w*])\*[^\s*][^*]*\*(?![\w*])"
)


# --------------------------------------------------------------------------
# Word lists
# --------------------------------------------------------------------------


def word_forms(word: str) -> set[str]:
    """Every form of a banned word: leverage, leverages, leveraged, leveraging."""
    w = word.lower()
    forms = {w, w + "s", w + "es", w + "d", w + "ed", w + "ing", w + "ly", w + "ness"}
    if w.endswith("e"):
        forms |= {w[:-1] + "ing", w[:-1] + "ed", w[:-1] + "ation", w[:-1] + "ations"}
    if w.endswith("y"):
        forms |= {w[:-1] + "ies", w[:-1] + "ied", w[:-1] + "ily", w[:-1] + "istic"}
    if w.endswith("ous") or w.endswith("al") or w.endswith("ic"):
        forms |= {w + "ly", w + "ally", w + "ness"}
    return forms


def banned_patterns(extra_file: str | Path | None = None) -> list[tuple[str, re.Pattern[str]]]:
    """(listed word, pattern) for the starter list, the user's list and Resume-Matcher's phrases."""
    listed = read_word_file(REFERENCES / "banned-words.txt") + read_word_file(extra_file)
    patterns: dict[str, re.Pattern[str]] = {}
    for entry in listed:
        key = entry.lower()
        if key in patterns:
            continue
        if re.fullmatch(r"[A-Za-z]+", entry):
            alternatives = "|".join(sorted(map(re.escape, word_forms(entry)), key=len, reverse=True))
            patterns[key] = re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])", re.IGNORECASE)
        else:
            patterns[key] = re.compile(rf"(?<![\w-]){re.escape(entry)}(?:s|d|ed)?(?![\w-])", re.IGNORECASE)
    for phrase in sorted(AI_PHRASE_BLACKLIST):
        if phrase in ("\u2014", "---", "--"):
            continue  # W1 covers dashes
        patterns.setdefault(phrase.lower(), _phrase_pattern(phrase))
    return list(patterns.items())


@lru_cache(maxsize=1)
def style_words() -> frozenset[str]:
    """Kobak's style words minus the everyday ones."""
    path = REFERENCES / "excess_words.csv"
    found: set[str] = set()
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if "style" in (row.get("type") or "") and row.get("word"):
                found.add(row["word"].strip().lower())
    return frozenset(found - EVERYDAY)


def style_word_hits(text: str) -> list[str]:
    listed = style_words()
    return [w for w in (t.lower() for t in words(text)) if w in listed]


def style_rate(texts: list[str]) -> float:
    total = sum(len(words(t)) for t in texts)
    return sum(len(style_word_hits(t)) for t in texts) / total if total else 0.0


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def _where(kind: str, entry: dict[str, Any], index: int) -> str:
    return f"{entry_name(kind, entry)} bullet {index + 1}"


def check_dashes(resume: dict[str, Any]) -> list[Finding]:
    """W1 on every string that prints."""
    findings: list[Finding] = []

    def walk(obj: Any, path: str, key: str) -> None:
        if isinstance(obj, str):
            if "\u2014" in obj or "---" in obj:
                findings.append(Finding("W1", "block", "em dash; use a comma or split the thought", where=path, value=obj))
            elif "--" in obj:
                findings.append(Finding("W1", "block", "'--' used as a dash; use a comma", where=path, value=obj))
            elif "\u2013" in obj and key != "years":
                findings.append(
                    Finding("W1", "block", "en dash outside a date range; write 'to' or use a hyphen", where=path, value=obj)
                )
        elif isinstance(obj, list):
            for index, item in enumerate(obj):
                walk(item, f"{path}[{index + 1}]", key)
        elif isinstance(obj, dict):
            label = obj.get("company") or obj.get("name") or obj.get("institution") or obj.get("label")
            for child_key, value in obj.items():
                walk(value, f"{label or path} {child_key}".strip(), child_key)

    walk(page_view(resume), "", "")
    return findings


_TEX_DATE_DASH = re.compile(
    r"(?:[A-Z][a-z]{2}\.? )?(?:19|20)\d{2}\s*--\s*(?:(?:[A-Z][a-z]{2}\.? )?(?:19|20)\d{2}|Present)"
)


def check_tex_dashes(tex: str) -> list[Finding]:
    """W1 on the LaTeX source: no -- or --- outside date ranges."""
    begin = tex.find(r"\begin{document}")
    body = tex[begin:] if begin >= 0 else tex
    body = "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in body.split("\n"))
    stripped = _TEX_DATE_DASH.sub("", body)
    findings = []
    for line in stripped.split("\n"):
        if "--" in line:
            findings.append(Finding("W1", "block", "'--' in the LaTeX source outside a date range", where="tex", value=line.strip()[:160]))
    return findings


def check_bullets(
    resume: dict[str, Any],
    master: dict[str, Any],
    vocabulary: list[str],
    banned: list[tuple[str, re.Pattern[str]]],
) -> list[Finding]:
    """W2 to W10 on each bullet, plus W2 and W10 on the headline and skills."""
    findings: list[Finding] = []
    page_verbs: Counter[str] = Counter()
    shown_bullets: list[str] = []

    other_text = [str((resume.get("personalInfo") or {}).get("title") or "")]
    for where, text in [("header", other_text[0])]:
        for listed, pattern in banned:
            if text and pattern.search(mask_terms(text, vocabulary)):
                findings.append(Finding("W2", "block", f"banned word '{listed}'", where=where, value=text))

    for kind, _, entry in entries(resume):
        source = find_master_entry(kind, entry, master)
        source_text = entry_source_text(kind, source) if source else ""
        entry_verbs: Counter[str] = Counter()
        for index, text in enumerate(bullets(entry)):
            where = _where(kind, entry, index)
            shown_bullets.append(text)
            masked = mask_terms(text, vocabulary)
            low = masked.lower()

            # W2
            for listed, pattern in banned:
                match = pattern.search(masked)
                if match:
                    findings.append(
                        Finding("W2", "block", f"banned word '{match.group(0).strip()}' (list entry: {listed})", where=where, value=text)
                    )
            # W3 per bullet
            hits = style_word_hits(masked)
            if len(hits) > MAX_STYLE_WORDS_PER_BULLET:
                findings.append(
                    Finding("W3", "block",
                            f"{len(hits)} AI-style words in one bullet ({', '.join(hits)}); at most {MAX_STYLE_WORDS_PER_BULLET}",
                            where=where, value=text)
                )
            # W4
            tail = re.search(r",\s+((?:thereby\s+|thus\s+)?[a-z]+ing\b[^,;]*)$", masked)
            if tail:
                clause = tail.group(1)
                opener = re.sub(r"^(?:thereby|thus)\s+", "", clause)
                if any(opener.startswith(t) for t in TACKED_ON):
                    findings.append(
                        Finding("W4", "block",
                                f"ends with a tacked-on clause (', {clause[:50]}'); state the result as its own fact or cut it",
                                where=where, value=text)
                    )
                elif opener.startswith("resulting in") and not numbers_in(clause):
                    findings.append(
                        Finding("W4", "block", "'resulting in' is allowed only when a number follows", where=where, value=text)
                    )
                elif not numbers_in(clause):
                    findings.append(
                        Finding("W4", "warn",
                                f"ends with an '-ing' clause that has no number (', {clause[:50]}'); check it adds a fact",
                                where=where, value=text)
                    )
            elif re.search(r"\bresulting in\b(?![^,;]*\d)", low):
                findings.append(Finding("W4", "block", "'resulting in' is allowed only when a number follows", where=where, value=text))
            # W5
            nominal = [
                w for w in (t.lower() for t in words(masked))
                if len(w) >= 7 and _NOMINAL_SUFFIX.search(w) and w not in NOT_NOMINALIZATIONS
            ]
            if len(nominal) > MAX_NOMINALIZATIONS:
                findings.append(
                    Finding("W5", "rewrite",
                            f"{len(nominal)} noun forms ({', '.join(nominal)}); use the verb: 'Optimized queries', not 'Performed optimization of queries'",
                            where=where, value=text)
                )
            # W6
            for triad in re.finditer(r"\b([a-z][a-z-]+),\s+([a-z][a-z-]+),?\s+(?:and|or)\s+([a-z][a-z-]+)\b", masked):
                items = [g.lower() for g in triad.groups()]
                if all(i in ABSTRACT or _ABSTRACT_SUFFIX.search(i) for i in items):
                    findings.append(
                        Finding("W6", "rewrite",
                                f"filler triad '{triad.group(0)}'; keep the one quality the result supports",
                                where=where, value=text)
                    )
            # W7
            verb = first_word(text).lower()
            if verb:
                entry_verbs[verb] += 1
                page_verbs[verb] += 1
                if entry_verbs[verb] == 2:
                    findings.append(
                        Finding("W7", "block", f"two bullets in this entry start with '{verb.capitalize()}'", where=where, value=verb)
                    )
            # W8
            for hedge in HEDGES:
                if re.search(rf"(?<![\w-]){re.escape(hedge)}(?![\w-])", low):
                    findings.append(
                        Finding("W8", "rewrite", f"hedge or vague scope: '{hedge}'; say what was done and how much", where=where, value=text)
                    )
                    break
            # W9
            concrete = bool(numbers_in(text)) or any(term_in_text(term, text) for term in vocabulary)
            if not concrete and source_text:
                concrete = any(
                    term_in_text(token, source_text)
                    for token in re.findall(r"\b[A-Z][A-Za-z0-9+#.]+\b", text)[1:]
                )
            if not concrete:
                findings.append(
                    Finding("W9", "warn", "names no tool, dataset, number or user group from the master", where=where, value=text)
                )
            # W10
            if _MARKUP.search(text):
                findings.append(Finding("W10", "block", "bold or italic markup in a bullet", where=where, value=text))
            if _CURLY.search(text):
                findings.append(Finding("W10", "block", "curly quotes; use straight quotes", where=where, value=text))
            if _EMOJI.search(text):
                findings.append(Finding("W10", "block", "emoji or pictograph in a bullet", where=where, value=text))

    for verb, count in sorted(page_verbs.items()):
        if count > MAX_SAME_VERB_ON_PAGE:
            findings.append(
                Finding("W7", "block",
                        f"'{verb.capitalize()}' starts {count} bullets on the page; at most {MAX_SAME_VERB_ON_PAGE}",
                        where="page", value=verb)
            )

    # W3 page rate against the master's own rate
    master_texts = [b for kind in ENTRY_KINDS for e in master.get(kind) or [] for b in master_bullets(e)]
    page_rate, master_rate = style_rate(shown_bullets), style_rate(master_texts)
    page_words = sum(len(words(t)) for t in shown_bullets)
    page_hits = sum(len(style_word_hits(t)) for t in shown_bullets)
    # The master's rate, applied to this page and rounded up to a whole word.
    allowed = math.ceil(master_rate * page_words - 1e-9)
    if page_hits > allowed:
        used = sorted({w for t in shown_bullets for w in style_word_hits(mask_terms(t, vocabulary))})
        findings.append(
            Finding("W3", "block",
                    f"{page_hits} AI-style words on the page ({page_rate:.2%}); the master's rate "
                    f"({master_rate:.2%}) allows {allowed}. "
                    f"Words used: {', '.join(used)}",
                    where="page", value=f"{page_rate:.4f}")
        )
    return findings


def run_style_check(
    resume: dict[str, Any],
    master: dict[str, Any],
    job: dict[str, Any] | None = None,
    confirmed: list[str] | None = None,
    banned_file: str | Path | None = None,
) -> list[Finding]:
    vocabulary = tool_vocabulary(master, job, confirmed or [])
    return check_dashes(resume) + check_bullets(resume, master, vocabulary, banned_patterns(banned_file))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Writing rules W1-W10.")
    parser.add_argument("--master", required=True)
    parser.add_argument("--tailored", required=True)
    parser.add_argument("--job")
    parser.add_argument("--confirmed")
    parser.add_argument("--banned")
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args(argv)
    master = read_tex(args.master)
    resume = merge_tailored(load_json(args.tailored), master)
    findings = run_style_check(
        resume, master, load_json(args.job) if args.job else None,
        read_confirmed_skills(args.confirmed), args.banned,
    )
    sys.stdout.write(dump_json([f.to_dict() for f in findings]))
    return 1 if any(is_blocking(f, args.attempt) for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
