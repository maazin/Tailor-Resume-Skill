"""Round trip: how much of the structured resume survives extraction, and in what order.

Modified from Resume-Matcher (https://github.com/srbhr/Resume-Matcher),
apps/backend/app/services/ats_parse/roundtrip.py, Apache-2.0.
Changes:
  - The per-template order tables for Resume-Matcher's own templates are
    replaced by the one-line heading order this skill writes (D12): role,
    organization, location, dates; project name, then tech stack.
  - Contact order is read from the user's template (``_template.contact_order``).
  - Pydantic validation replaced by ``common.page_view``; condensed entries
    and custom sections removed (the skill has neither).
"""

from collections import defaultdict
from typing import Any

from common import page_view
from parse_check.models import FieldStatus, RoundTrip, RoundTripField
from parse_check.normalize import tokens

MAX_FIELDS = 300
FOUND_THRESHOLD = 0.9
GARBLED_THRESHOLD = 0.5
MATCH_PREFIX = 6

Field = tuple[str, str]

# Header contact order when the template does not say.
CONTACT_ORDER = ("location", "phone", "email", "website", "github", "linkedin")
LINK_FIELDS = frozenset({"website", "linkedin", "github"})

# Entry field order as the one-line heading prints it.
ENTRY_ORDER: dict[str, tuple[str, ...]] = {
    "workExperience": ("title", "company", "location", "years"),
    "education": ("degree", "institution", "location", "years"),
    "personalProjects": ("name", "stack", "years"),
}
DEFAULT_SECTIONS = ("education", "additional", "workExperience", "personalProjects")


def _contact_text(key: str, value: Any) -> str:
    """A contact value as the header shows it: a bare link host drops "www.".

    The templates show a value starting with "http" or "//" exactly as typed.
    """
    text = str(value or "")
    if key in LINK_FIELDS and not text.startswith(("http", "//")):
        return text.removeprefix("www.")
    return text


def _join(values: Any) -> str:
    if isinstance(values, list):
        return " ".join(str(v) for v in values if v)
    return str(values or "")


def _entry_fields(
    prefix: str, entry: dict[str, Any], keys: tuple[str, ...]
) -> list[Field]:
    out: list[Field] = [(f"{prefix}.{k}", _join(entry.get(k))) for k in keys]
    description = entry.get("description")
    if isinstance(description, list):
        out.extend((f"{prefix}.description[{j}]", d) for j, d in enumerate(description))
    elif description:
        out.append((f"{prefix}.description", str(description)))
    return out


def _section_fields(key: str, data: dict[str, Any]) -> list[Field]:
    if key == "summary":
        return [("summary", data.get("summary") or "")]
    if key in ENTRY_ORDER:
        order = ENTRY_ORDER[key]
        return [
            f
            for i, e in enumerate(data.get(key) or [])
            for f in _entry_fields(f"{key}[{i}]", e, order)
        ]
    if key == "additional":
        additional = data.get("additional") or {}
        return [
            (f"additional.{k}", _join(additional.get(k)))
            for k in ("technicalSkills", "languages", "certificationsTraining", "awards")
        ]
    return []


def expected_fields(
    data: dict[str, Any], contact_order: tuple[str, ...] | None = None
) -> tuple[list[Field], list[str]]:
    """Fields in render order, plus the keys of hidden sections."""
    info = data.get("personalInfo") or {}
    contact = contact_order or CONTACT_ORDER
    fields: list[Field] = [
        (f"personalInfo.{k}", _contact_text(k, info.get(k)))
        for k in ("name", "title", *contact)
    ]
    hidden: list[str] = []
    meta_list = data.get("sectionMeta") or [
        {"key": key, "order": i} for i, key in enumerate(DEFAULT_SECTIONS)
    ]
    for meta in sorted(meta_list, key=lambda m: m.get("order", 0)):
        key = meta.get("key")
        if not key or key == "personalInfo":
            continue
        if not meta.get("isVisible", True):
            hidden.append(key)
            continue
        fields.extend(_section_fields(key, data))
    return [(name, text) for name, text in fields if tokens(text)], hidden


def _is_list_field(name: str) -> bool:
    return name.startswith("additional.") or name.endswith(".strings")


def _uses_words(name: str, field_tokens: list[str]) -> bool:
    """Lists and one-word fields score word recall; phrases score adjacent pairs."""
    return len(field_tokens) == 1 or _is_list_field(name)


def _claim_words(
    field_tokens: list[str],
    index: dict[str, list[int]],
    owner: list[int | None],
    field_id: int,
) -> float:
    """Share of the words found at text positions no other field has claimed."""
    hits = 0
    for token in field_tokens:
        pos = next((p for p in index.get(token, ()) if owner[p] is None), None)
        if pos is not None:
            owner[pos] = field_id
            hits += 1
    return hits / len(field_tokens)


def _claim_pairs(
    field_tokens: list[str],
    doc_tokens: list[str],
    pair_index: dict[tuple[str, str], list[int]],
    owner: list[int | None],
    field_id: int,
) -> float:
    """Share of adjacent pairs found; a field's own pairs may share a word.

    A pair continues the previous pair's match when it can, so a phrase is
    claimed where it appears whole rather than piecemeal across the text.
    """
    used: set[int] = set()

    def free(p: int) -> bool:
        return p not in used and {owner[p], owner[p + 1]} <= {None, field_id}

    pairs = list(zip(field_tokens, field_tokens[1:]))
    pos: int | None = None
    for pair in pairs:
        follow = -1 if pos is None else pos + 1
        if (
            0 <= follow < len(doc_tokens) - 1
            and (doc_tokens[follow], doc_tokens[follow + 1]) == pair
            and free(follow)
        ):
            pos = follow
        else:
            pos = next((p for p in pair_index.get(pair, ()) if free(p)), None)
        if pos is not None:
            used.add(pos)
            owner[pos] = owner[pos + 1] = field_id
    return len(used) / len(pairs)


def _field_scores(
    fields: list[tuple[str, list[str]]], doc_tokens: list[str]
) -> list[float]:
    """Adjacent-pair recall, so interleaved columns score lower; lists use word recall.

    Each word in the text can satisfy one field only, so a phrase repeated in
    the header cannot stand in for a body field that failed to extract. Phrases
    claim their words first: a title such as "Python Developer" cannot also make
    an omitted "Python" skill look found.
    """
    index: dict[str, list[int]] = defaultdict(list)
    pair_index: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, token in enumerate(doc_tokens):
        index[token].append(i)
        if i + 1 < len(doc_tokens):
            pair_index[(token, doc_tokens[i + 1])].append(i)
    owner: list[int | None] = [None] * len(doc_tokens)
    scores = [0.0] * len(fields)
    for field_id, (name, field_tokens) in enumerate(fields):
        if not _uses_words(name, field_tokens):
            scores[field_id] = _claim_pairs(
                field_tokens, doc_tokens, pair_index, owner, field_id
            )
    for field_id, (name, field_tokens) in enumerate(fields):
        if _uses_words(name, field_tokens):
            scores[field_id] = _claim_words(field_tokens, index, owner, field_id)
    return scores


def _position(
    field_tokens: list[str],
    doc_tokens: list[str],
    index: dict[str, list[int]],
    after: int,
) -> int | None:
    """Start of the best prefix match; ties prefer the first match after ``after``."""
    starts = next((index[t] for t in field_tokens if t in index), None)
    if not starts:
        return None
    prefix = field_tokens[:MATCH_PREFIX]

    def match_len(start: int) -> int:
        n = 0
        while (
            n < len(prefix)
            and start + n < len(doc_tokens)
            and doc_tokens[start + n] == prefix[n]
        ):
            n += 1
        return n

    best = max(match_len(s) for s in starts)
    candidates = [s for s in starts if match_len(s) == best]
    return next((s for s in candidates if s > after), candidates[0])


def kendall_fidelity(positions: list[int]) -> float:
    """Kendall tau of observed positions vs expected order, mapped to 0..1."""
    n = len(positions)
    if n < 2:
        return 1.0
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            if positions[j] > positions[i]:
                concordant += 1
            elif positions[j] < positions[i]:
                discordant += 1
    pairs = n * (n - 1) / 2
    return round(((concordant - discordant) / pairs + 1) / 2, 3)


def compute_roundtrip(data: dict[str, Any], extracted_text: str) -> RoundTrip:
    contact_order = tuple((data.get("_template") or {}).get("contact_order") or ()) or None
    fields, hidden = expected_fields(page_view(data), contact_order)
    truncated = len(fields) > MAX_FIELDS
    fields = fields[:MAX_FIELDS]
    doc_tokens = tokens(extracted_text)
    index: dict[str, list[int]] = defaultdict(list)
    for i, token in enumerate(doc_tokens):
        index[token].append(i)
    tokenized = [(name, tokens(text)) for name, text in fields]
    scores = _field_scores(tokenized, doc_tokens)

    results: list[RoundTripField] = []
    positions: list[int] = []
    weighted = total = 0.0
    last = -1
    for (name, field_tokens), value in zip(tokenized, scores):
        status: FieldStatus = (
            "found"
            if value >= FOUND_THRESHOLD
            else "garbled"
            if value >= GARBLED_THRESHOLD
            else "missing"
        )
        if status != "missing":
            pos = _position(field_tokens, doc_tokens, index, last)
            if pos is not None:
                positions.append(pos)
                last = pos
        weighted += value * len(field_tokens)
        total += len(field_tokens)
        results.append(RoundTripField(field=name, status=status, score=round(value, 3)))
    results.extend(
        RoundTripField(field=f"section.{key}", status="hidden", score=0.0)
        for key in hidden
    )
    return RoundTrip(
        content_recall=round(weighted / total, 3) if total else 1.0,
        order_fidelity=kendall_fidelity(positions),
        truncated=truncated,
        fields=results,
    )
