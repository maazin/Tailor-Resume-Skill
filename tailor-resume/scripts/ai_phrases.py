"""AI-phrase list and the automatic phrase filter.

Modified from Resume-Matcher (https://github.com/srbhr/Resume-Matcher), Apache-2.0:
  - AI_PHRASE_BLACKLIST and AI_PHRASE_REPLACEMENTS from
    apps/backend/app/prompts/refinement.py
  - remove_ai_phrases and its helpers from apps/backend/app/services/refiner.py
Changes:
  - Posting words are no longer protected: a phrase is replaced even when the
    job description uses it (the PRD makes the writing rules a hard rule).
  - Phrases the candidate's own master resume uses are still left alone only
    when the caller passes ``protected_text``; the skill does not pass it.
  - The dash entries are skipped inside date ranges, where an en dash written
    as ``--`` is the required format (W1, C14).
  - Each change is returned as a (phrase, replacement) pair for the report.
"""

import re
from typing import Any

AI_PHRASE_BLACKLIST: set[str] = {
    # Action verbs (overused in AI resume writing)
    "spearheaded",
    "orchestrated",
    "championed",
    "synergized",
    "leveraged",
    "revolutionized",
    "pioneered",
    "catalyzed",
    "operationalized",
    "architected",
    "envisioned",
    "effectuated",
    "endeavored",
    "facilitated",
    "utilized",
    # Corporate buzzwords
    "synergy",
    "synergies",
    "paradigm",
    "paradigm shift",
    "best-in-class",
    "world-class",
    "cutting-edge",
    "bleeding-edge",
    "game-changer",
    "game-changing",
    "disruptive",
    "disruptor",
    "holistic",
    "robust",
    "scalable",
    "actionable",
    "impactful",
    "proactive",
    "proactively",
    "stakeholder",
    "deliverables",
    "bandwidth",
    "circle back",
    "deep dive",
    "move the needle",
    "low-hanging fruit",
    "touch base",
    "value-add",
    # Filler phrases
    "in order to",
    "for the purpose of",
    "with a view to",
    "at the end of the day",
    "moving forward",
    "going forward",
    "on a daily basis",
    "on a regular basis",
    "in a timely manner",
    "at this point in time",
    "due to the fact that",
    "in the event that",
    "in light of the fact that",
    # Punctuation patterns
    "—",  # Em-dash
    "---",
    "--",  # Double hyphen often used as em-dash substitute
}

AI_PHRASE_REPLACEMENTS: dict[str, str] = {
    # Action verb replacements
    "spearheaded": "led",
    "orchestrated": "coordinated",
    "championed": "advocated for",
    "synergized": "collaborated",
    "leveraged": "used",
    "revolutionized": "transformed",
    "pioneered": "introduced",
    "catalyzed": "initiated",
    "operationalized": "implemented",
    "architected": "designed",
    "envisioned": "planned",
    "effectuated": "completed",
    "endeavored": "worked",
    "facilitated": "helped",
    "utilized": "used",
    # Buzzword replacements
    "synergy": "collaboration",
    "synergies": "collaborations",
    "paradigm": "approach",
    "paradigm shift": "change",
    "best-in-class": "top-performing",
    "world-class": "high-quality",
    "cutting-edge": "modern",
    "bleeding-edge": "modern",
    "game-changer": "innovation",
    "game-changing": "innovative",
    "disruptive": "innovative",
    "holistic": "comprehensive",
    "robust": "strong",
    "scalable": "expandable",
    "actionable": "practical",
    "impactful": "effective",
    "proactive": "active",
    "proactively": "actively",
    "stakeholder": "team member",
    "deliverables": "outputs",
    "bandwidth": "capacity",
    "circle back": "follow up",
    "deep dive": "analysis",
    "move the needle": "make progress",
    "low-hanging fruit": "quick wins",
    "touch base": "connect",
    "value-add": "benefit",
    # Phrase simplifications
    "in order to": "to",
    "for the purpose of": "to",
    "with a view to": "to",
    "at the end of the day": "",
    "moving forward": "",
    "going forward": "",
    "on a daily basis": "daily",
    "on a regular basis": "regularly",
    "in a timely manner": "promptly",
    "at this point in time": "now",
    "due to the fact that": "because",
    "in the event that": "if",
    "in light of the fact that": "since",
    # Punctuation replacements
    "—": ", ",
    "---": ", ",
    "--": ", ",
}

# Plural forms for replacements that don't take a plain "s" ("analysis" ->
# "analysiss"), keyed by the lowercase blacklisted phrase.
_PLURAL_REPLACEMENTS: dict[str, str] = {
    "paradigm": "approaches",
    "deep dive": "analyses",
    "bandwidth": "capacities",
    "low-hanging fruit": "quick wins",
    "move the needle": "make progress",
}

_DASH_PHRASES = {"—", "---", "--"}

# Fields that hold dates; dashes there are a format, not a writing habit.
DATE_KEYS = {"years"}


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Case-insensitive pattern for a blacklisted phrase, matching whole words.

    Word-like phrases must start at a word boundary and may be followed by a
    plural "s" ("stakeholder" also matches "stakeholders"), but not by other
    letters ("robust" does not match "robustness"). An "a"/"an" right before
    the phrase is captured so it can agree with the replacement. Punctuation
    entries such as dashes match anywhere.
    """
    starts_word = phrase[:1].isalnum()
    ends_word = phrase[-1:].isalnum()
    article = r"(?:(?<!\w)(?P<article>an?)\s+)?" if starts_word else ""
    start = r"(?<!\w)" if starts_word else ""
    end = r"(?P<plural>s?)(?!\w)" if ends_word else ""
    return re.compile(
        f"{article}{start}(?P<phrase>{re.escape(phrase)}){end}", re.IGNORECASE
    )


# Longest first, so "paradigm shift" is handled before "paradigm" and "---"
# before "--". A fixed order also keeps the output the same on every run.
_ORDERED_PHRASES = sorted(AI_PHRASE_BLACKLIST, key=lambda p: (-len(p), p))
_PATTERNS = {phrase: _phrase_pattern(phrase) for phrase in _ORDERED_PHRASES}


def remove_ai_phrases(
    data: dict[str, Any],
    protected_text: str = "",
    skip_keys: frozenset[str] = frozenset(
        {"years", "email", "url", "website", "github", "linkedin", "phone"}
    ),
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Replace AI-sounding phrases in every string of a resume dict.

    Local and deterministic. Returns the cleaned copy and one record per
    change: ``{"phrase", "replacement", "before", "after"}``.
    """
    own_words = {
        phrase.lower()
        for phrase, pattern in _PATTERNS.items()
        if protected_text and pattern.search(protected_text)
    }
    changes: list[dict[str, str]] = []

    def clean_text(text: str) -> str:
        cleaned = text
        for phrase, pattern in _PATTERNS.items():
            if phrase.lower() in own_words:
                continue
            if not pattern.search(cleaned):
                continue
            replacement = AI_PHRASE_REPLACEMENTS.get(phrase.lower(), "")
            plural_replacement = _PLURAL_REPLACEMENTS.get(phrase.lower())

            def substitute(
                match: re.Match[str],
                replacement: str = replacement,
                plural_replacement: str | None = plural_replacement,
            ) -> str:
                plural = match.groupdict().get("plural") or ""
                if plural and plural_replacement:
                    replacement, plural = plural_replacement, ""
                # Keep sentence case: "Spearheaded" -> "Led", not "led".
                if replacement and match.group("phrase")[0].isupper():
                    replacement = replacement[0].upper() + replacement[1:]
                if replacement:
                    replacement += plural
                article = match.groupdict().get("article")
                if not article:
                    return replacement
                if not replacement:
                    return ""
                wanted = "an" if replacement[0].lower() in "aeiou" else "a"
                if article[0].isupper():
                    wanted = wanted.capitalize()
                return f"{wanted} {replacement}"

            before = cleaned
            cleaned = pattern.sub(substitute, cleaned)
            if phrase in _DASH_PHRASES:
                # " word -- word " becomes "word, word", not "word , word".
                cleaned = re.sub(r"\s+,\s+", ", ", cleaned)
                cleaned = re.sub(r",\s{2,}", ", ", cleaned)
            if not replacement:
                cleaned = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", cleaned)
                cleaned = re.sub(r"(?<![ \t])[ \t]+([.,;:!?])", r"\1", cleaned)
                cleaned = cleaned.strip(" \t")
            if cleaned != before:
                changes.append(
                    {
                        "phrase": phrase,
                        "replacement": replacement,
                        "before": before,
                        "after": cleaned,
                    }
                )
        return cleaned

    def clean_recursive(obj: Any, key: str = "") -> Any:
        if key.startswith("_") or key in skip_keys:
            return obj
        if isinstance(obj, str):
            return clean_text(obj)
        if isinstance(obj, list):
            return [clean_recursive(item, key) for item in obj]
        if isinstance(obj, dict):
            return {k: clean_recursive(v, k) for k, v in obj.items()}
        return obj

    return clean_recursive(data), changes
