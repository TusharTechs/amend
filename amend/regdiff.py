"""
Regulation redline engine for eCFR XML files.

Parse → Version(date, clauses) → diff(old, new) → list[DiffEntry]
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

_CURLY_QUOTE_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2014": "-", "\u2013": "-", "\u2012": "-",
})


def normalize(text: str) -> str:
    """NFKC + curly-quote/dash → ASCII + collapse whitespace."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_CURLY_QUOTE_MAP)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Designator parsing
# ---------------------------------------------------------------------------

_ROMAN_VALUES = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
    "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15,
    "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20,
}
_PURE_ROMAN = set(_ROMAN_VALUES)
_AMBIGUOUS = {"i", "v", "x"}  # could be level-1 letter OR level-3 roman


def _is_roman(s: str) -> bool:
    return s in _PURE_ROMAN


def _is_digit(s: str) -> bool:
    return s.isdigit()


def _is_upper(s: str) -> bool:
    return s.isalpha() and s.isupper()


def _is_lower_non_roman(s: str) -> bool:
    """Unambiguously a level-1 lowercase letter (not a roman numeral)."""
    return s.isalpha() and s.islower() and s not in _PURE_ROMAN


# Level rules:
#   1 = (a)(b)… lowercase alpha (and ambiguous i/v/x used as letters)
#   2 = (1)(2)… digits
#   3 = (i)(ii)… roman numerals
#   4 = (A)(B)… uppercase alpha

def _designator_level(des: str, prev_stack: list[tuple[int, str]],
                       next_des: Optional[str]) -> int:
    """
    Determine the CFR level of a single designator token (without parens).
    prev_stack is the current stack of (level, value) pairs.
    next_des is the next designator token (or None), used to break ties.
    """
    if _is_digit(des):
        return 2
    if _is_upper(des):
        return 4

    # Multi-char roman numerals (ii, iii, iv, vi, vii, viii, ix, xi, …)
    # are unambiguously level 3.
    if _is_roman(des) and des not in _AMBIGUOUS:
        return 3

    if des not in _AMBIGUOUS:
        # Unambiguously lower-alpha level 1 (not a roman numeral at all)
        return 1

    # Ambiguous single-char: could be level-1 letter OR level-3 roman numeral
    # (i, v, x)
    level1_current = _level_current(prev_stack, 1)  # current letter at level 1
    level3_current = _level_current(prev_stack, 3)  # current roman at level 3

    continues_l1 = _continues_alpha(des, level1_current)
    continues_l3 = _continues_roman(des, level3_current)

    if continues_l1 and not continues_l3:
        return 1
    if continues_l3 and not continues_l1:
        return 3
    if not continues_l1 and not continues_l3:
        # Neither continues – default: roman if it's a valid roman, else level 1
        return 3 if _is_roman(des) else 1

    # Both continue – use next designator to break tie
    if next_des is not None:
        if _is_roman(next_des) and next_des not in _AMBIGUOUS:
            return 3  # multi-char roman follows → we're in roman
        if _is_digit(next_des):
            return 1  # digit follows → we're at level 1
        if _is_upper(next_des):
            return 3  # uppercase follows roman
        if _is_lower_non_roman(next_des):
            return 1  # unambiguous letter → we're at level 1
    # Default: roman numerals are more likely in deep nesting
    return 3


def _level_current(stack: list[tuple[int, str]], level: int) -> Optional[str]:
    for lv, val in reversed(stack):
        if lv == level:
            return val
    return None


def _continues_alpha(des: str, current: Optional[str]) -> bool:
    """Does des follow current in a–z sequence?"""
    if current is None:
        return des == "a"
    # Multi-char tokens (roman numerals) cannot be in the alpha sequence
    if len(current) != 1 or not (current.isalpha() and current.islower()):
        return False
    return ord(des) == ord(current) + 1


def _continues_roman(des: str, current: Optional[str]) -> bool:
    """Does des follow current in roman numeral sequence?"""
    if not _is_roman(des):
        return False
    if current is None:
        return des == "i"
    if current not in _ROMAN_VALUES:
        return False
    return _ROMAN_VALUES.get(des, -1) == _ROMAN_VALUES[current] + 1


# Regex: one or more (token) designators at the start of a paragraph text
_DES_RE = re.compile(r"^((?:\([^)]+\))+)")


def _parse_designators(text: str) -> tuple[list[str], str]:
    """
    Return (list_of_tokens, remainder_text).
    Tokens are the raw content inside the parens, e.g. ["b", "1"].
    """
    m = _DES_RE.match(text.lstrip())
    if not m:
        return [], text
    raw = m.group(1)
    tokens = re.findall(r"\(([^)]+)\)", raw)
    remainder = text[m.end():]
    return tokens, remainder.strip()


# ---------------------------------------------------------------------------
# Citation building
# ---------------------------------------------------------------------------

def _build_citation(section: str, stack: list[tuple[int, str]]) -> str:
    """Turn section + designator stack into a citation like '314.4(c)(5)'."""
    suffix = "".join(f"({val})" for _, val in stack)
    return f"{section}{suffix}"


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def _itertext_clean(elem: ET.Element) -> str:
    """Get all text under an element, collapsed."""
    parts = []
    for t in elem.itertext():
        parts.append(t)
    return normalize("".join(parts))


@dataclass
class Version:
    date: str
    clauses: dict[str, str]  # citation → normalized text (body only, no designator)

    def word_count(self) -> int:
        return sum(len(t.split()) for t in self.clauses.values())


def parse(xml_path: str | Path, date: str) -> Version:
    """Parse an eCFR XML file into a Version."""
    tree = ET.parse(str(xml_path))
    root = tree.getroot()

    clauses: dict[str, str] = {}
    # Walk all section DIVs
    # Sections can be DIV8, DIV6, etc. — match by TYPE="SECTION"
    for section_div in root.iter():
        if section_div.get("TYPE") != "SECTION":
            continue
        section_n = section_div.get("N", "")
        _parse_section(section_n, section_div, clauses)

    return Version(date=date, clauses=clauses)


def _parse_section(section: str, section_div: ET.Element,
                   clauses: dict[str, str]) -> None:
    """
    Parse all P elements inside a section DIV.
    We iterate P elements in document order, maintaining a designator stack.
    """
    # Collect all P elements in document order
    p_elements = list(section_div.iter("P"))
    n = len(p_elements)

    stack: list[tuple[int, str]] = []  # (level, token)
    last_citation: Optional[str] = None

    for idx, p in enumerate(p_elements):
        raw_text = normalize("".join(p.itertext()))
        tokens, body = _parse_designators(raw_text)

        if not tokens:
            # No designator – append to previous clause
            if last_citation and last_citation in clauses:
                clauses[last_citation] = clauses[last_citation] + " " + raw_text
            elif last_citation:
                clauses[last_citation] = raw_text
            # Also store under section root if nothing yet
            if last_citation is None:
                if section not in clauses:
                    clauses[section] = raw_text
                else:
                    clauses[section] += " " + raw_text
            continue

        # Peek at the FIRST token of the NEXT paragraph for tie-breaking
        next_first: Optional[str] = None
        for jdx in range(idx + 1, n):
            next_raw = normalize("".join(p_elements[jdx].itertext()))
            next_tokens, _ = _parse_designators(next_raw)
            if next_tokens:
                next_first = next_tokens[0]
                break

        # Process each token in the prefix
        for ti, token in enumerate(tokens):
            # Peek: next token in this P's prefix or next P's first token
            if ti + 1 < len(tokens):
                peek = tokens[ti + 1]
            else:
                peek = next_first

            level = _designator_level(token, stack, peek)
            # Pop stack back to the parent of this level
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, token))

        citation = _build_citation(section, stack)
        clauses[citation] = body
        last_citation = citation


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

@dataclass
class DiffEntry:
    citation: str
    change: str  # added | removed | modified | renumbered | unchanged
    old_text: Optional[str] = None
    new_text: Optional[str] = None
    similarity: float = 1.0
    ops: list[tuple[str, str]] = field(default_factory=list)
    old_citation: Optional[str] = None  # for renumbered


def _word_ops(old: str, new: str) -> tuple[float, list[tuple[str, str]]]:
    """Compute word-level similarity and ops (equal/insert/delete)."""
    old_words = old.split()
    new_words = new.split()
    sm = SequenceMatcher(None, old_words, new_words, autojunk=False)
    ratio = sm.ratio()
    ops: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for w in old_words[i1:i2]:
                ops.append(("equal", w))
        elif tag == "replace":
            for w in old_words[i1:i2]:
                ops.append(("delete", w))
            for w in new_words[j1:j2]:
                ops.append(("insert", w))
        elif tag == "delete":
            for w in old_words[i1:i2]:
                ops.append(("delete", w))
        elif tag == "insert":
            for w in new_words[j1:j2]:
                ops.append(("insert", w))
    return ratio, ops


def diff(old: Version, new: Version) -> list[DiffEntry]:
    """
    Diff two Versions, returning a list of DiffEntry objects.
    """
    entries: list[DiffEntry] = []

    # Build reverse maps: body → list[citation]
    old_body_to_cits: dict[str, list[str]] = {}
    for cit, body in old.clauses.items():
        old_body_to_cits.setdefault(body, []).append(cit)

    new_body_to_cits: dict[str, list[str]] = {}
    for cit, body in new.clauses.items():
        new_body_to_cits.setdefault(body, []).append(cit)

    matched_old: set[str] = set()
    matched_new: set[str] = set()

    # -----------------------------------------------------------------------
    # Pass 1: Match by identical body
    # -----------------------------------------------------------------------
    for new_cit, new_body in new.clauses.items():
        if new_body not in old_body_to_cits:
            continue
        candidates = old_body_to_cits[new_body]
        # Disambiguate repeated bodies by parent citation similarity
        if len(candidates) == 1:
            old_cit = candidates[0]
        else:
            old_cit = _best_parent_match(new_cit, candidates)

        if old_cit in matched_old:
            continue

        matched_old.add(old_cit)
        matched_new.add(new_cit)

        if old_cit == new_cit:
            _, ops = _word_ops(old.clauses[old_cit], new_body)
            entries.append(DiffEntry(
                citation=new_cit, change="unchanged",
                old_text=old.clauses[old_cit], new_text=new_body,
                similarity=1.0, ops=ops,
            ))
        else:
            _, ops = _word_ops(old.clauses[old_cit], new_body)
            entries.append(DiffEntry(
                citation=new_cit, change="renumbered",
                old_text=old.clauses[old_cit], new_text=new_body,
                similarity=1.0, ops=ops,
                old_citation=old_cit,
            ))

    # -----------------------------------------------------------------------
    # Pass 2: Match remaining by same citation (if similar or old body gone)
    # -----------------------------------------------------------------------
    for new_cit, new_body in new.clauses.items():
        if new_cit in matched_new:
            continue
        if new_cit not in old.clauses:
            continue
        old_body = old.clauses[new_cit]
        ratio, ops = _word_ops(old_body, new_body)

        # Match if similar enough OR old body doesn't appear anywhere new
        if ratio >= 0.5 or old_body not in new_body_to_cits:
            if new_cit in matched_old:
                continue
            matched_old.add(new_cit)
            matched_new.add(new_cit)
            if old_body == new_body:
                entries.append(DiffEntry(
                    citation=new_cit, change="unchanged",
                    old_text=old_body, new_text=new_body,
                    similarity=1.0, ops=ops,
                ))
            else:
                entries.append(DiffEntry(
                    citation=new_cit, change="modified",
                    old_text=old_body, new_text=new_body,
                    similarity=ratio, ops=ops,
                ))

    # -----------------------------------------------------------------------
    # Pass 3: Leftovers → added / removed
    # -----------------------------------------------------------------------
    removed_entries: list[DiffEntry] = []
    for old_cit, old_body in old.clauses.items():
        if old_cit not in matched_old:
            e = DiffEntry(
                citation=old_cit, change="removed",
                old_text=old_body, new_text=None,
                similarity=0.0,
            )
            removed_entries.append(e)
            entries.append(e)

    added_entries: list[DiffEntry] = []
    for new_cit, new_body in new.clauses.items():
        if new_cit not in matched_new:
            e = DiffEntry(
                citation=new_cit, change="added",
                old_text=None, new_text=new_body,
                similarity=0.0,
            )
            added_entries.append(e)
            entries.append(e)

    # -----------------------------------------------------------------------
    # Pass 4: Re-classify added whose body matches a removed → renumbered
    # -----------------------------------------------------------------------
    removed_by_body: dict[str, DiffEntry] = {}
    for e in removed_entries:
        if e.old_text:
            removed_by_body.setdefault(e.old_text, e)

    for e in added_entries:
        if e.new_text and e.new_text in removed_by_body:
            old_e = removed_by_body[e.new_text]
            # Convert both entries
            _, ops = _word_ops(old_e.old_text, e.new_text)
            e.change = "renumbered"
            e.old_text = old_e.old_text
            e.similarity = 1.0
            e.ops = ops
            e.old_citation = old_e.citation
            old_e.change = "renumbered"
            # Keep old_e in the list but mark it so we can de-duplicate
            old_e.citation = e.citation  # new citation
            old_e.old_citation = e.old_citation if e.old_citation else old_e.citation

    # Remove duplicate renumbered entries that were created in pass 4
    # (both the old removed-entry and the new added-entry become renumbered)
    seen_renumbered: set[str] = set()
    final: list[DiffEntry] = []
    for e in entries:
        if e.change == "renumbered":
            key = (e.citation, e.old_citation)
            if key in seen_renumbered:
                continue
            seen_renumbered.add(key)
        final.append(e)

    return final


def _best_parent_match(new_cit: str, candidates: list[str]) -> str:
    """Pick the candidate whose parent citation best matches new_cit's parent."""
    new_parent = _parent_citation(new_cit)
    best = candidates[0]
    best_score = _citation_similarity(new_parent, _parent_citation(candidates[0]))
    for cand in candidates[1:]:
        score = _citation_similarity(new_parent, _parent_citation(cand))
        if score > best_score:
            best_score = score
            best = cand
    return best


def _parent_citation(cit: str) -> str:
    """Return citation without last designator component."""
    m = re.match(r"^(.*)\([^()]+\)$", cit)
    return m.group(1) if m else cit


def _citation_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()
