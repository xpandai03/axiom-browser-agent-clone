"""
Name keying — EVERY READING OF A NAME, never a guess at which one is legal.

THE PROBLEM THIS EXISTS FOR
---------------------------
TherapyNotes renders a patient who has a preferred name as

    Preferred (Legal) Last

and it does so identically in the Patients results table and in the chart
header. The practice sets "Minor" as the preferred name on children's records,
so a child whose legal name is "<Legal> <Last>" renders as

    Minor (<Legal>) <Last>

A rule that strips the parenthetical before tokenising — which is what the CRM's
nameKey does, and what it was built to do, for TRAILING annotations like
"(dad)" — deletes the legal first name and keeps the preferred one. The survey
carries the legal name. They never meet.

THE RULE: EMIT EVERY READING, MATCH ON INTERSECTION
---------------------------------------------------
    "X (Y) Z"          -> {"x z", "y z"}   preferred AND legal, both
    "X Y (annotation)" -> {"x y"}          a trailing group annotates, it does
                                           not replace anything
    "X Y"              -> {"x y"}          exactly name_key()

Two names agree when their reading sets INTERSECT.

NOTHING HERE DECIDES WHICH TOKEN IS THE LEGAL NAME, and nothing may be added
that does. The results table gives nothing to decide with: "Minor" parses as an
ordinary given name, it is also a real surname, and the convention that makes it
a flag lives in the practice's heads rather than in the markup. Emitting both
readings costs one extra key and is always right; guessing is sometimes
confidently wrong, which is the failure this module was written to end.

PARITY WITH THE CRM
-------------------
name_key() below is a character-for-character port of nameKey() in the CRM's
server/survey/matching.ts, including the order of operations (fold diacritics,
then lowercase, then delete apostrophes intra-word, then split on everything
else, then SORT the tokens so word order does not matter). name_keys() is the
port of nameKeys(). The two implementations must agree exactly or the agent and
the CRM will disagree about who a person is — tests/test_name_keys.py asserts
the shared table of cases that both repositories check.

Pure and dependency-free on purpose: stdlib only, no Playwright, no pydantic,
so it can be exercised without a browser.
"""

import re
import unicodedata
from typing import List, Tuple

# A parenthesised group. Non-greedy by construction — [^)]* cannot cross a ")".
_GROUP_RE = re.compile(r"\(([^)]*)\)")

# Apostrophes are INTRA-word and are DELETED, so "O'Callaghan" and "OCallaghan"
# are one token. Every other separator SPLITS, so "Ashgrove-Pemberton" stays two
# tokens and does NOT equal "Ashgrove" — a hyphenated surname is a real
# difference and belongs in a review queue, not in an automatic match.
_APOSTROPHES = re.compile(r"['‘’]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _fold(raw: str) -> str:
    """Diacritics folded, lowercased, apostrophes deleted. CRM order exactly."""
    # NFD splits "á" into "a" + a combining accent; U+0300-U+036F is the
    # combining-diacritical-marks block, written as escapes rather than literal
    # characters so it survives any re-encoding of this file.
    decomposed = unicodedata.normalize("NFD", raw)
    stripped = "".join(c for c in decomposed if not (0x0300 <= ord(c) <= 0x036F))
    return _APOSTROPHES.sub("", stripped.lower())


def _tokens(raw: str) -> List[str]:
    """Fold, then split on everything that is not a lowercase letter or digit."""
    return [t for t in _NON_ALNUM.split(_fold(raw)) if t]


def _key(tokens: List[str]) -> str:
    """Sorted and joined, so word order never matters ('Puffin, Wendell')."""
    return " ".join(sorted(tokens))


def name_key(raw) -> str:
    """
    ONE key, parentheticals stripped. The CRM's nameKey, ported.

    Kept because it is the right primitive for "what does this name reduce to",
    and because both codebases assert the two implementations agree. It is NOT
    the matching rule — name_keys() is.
    """
    if not raw:
        return ""
    return _key(_tokens(_GROUP_RE.sub(" ", str(raw))))


def name_keys(raw) -> Tuple[str, ...]:
    """
    EVERY reading of a name, in a stable order, without duplicates.

    The first reading is always the parentheticals-stripped one, i.e. name_key(),
    so a caller that wants a single representative key can take [0].

    How a reading is produced: the string is cut into WORD runs and GROUP runs in
    source order. The words alone are one reading. Then every non-empty group
    that has at least one word AFTER it yields a further reading — that group's
    tokens followed by the words that come after it — because a group in that
    position stands in for the run before it (the preferred forename). A group
    with nothing after it is TRAILING, and a trailing group annotates rather than
    replaces, so it contributes no reading of its own.

        "Minor (Rowan) Thistlewood"       -> ("minor thistlewood", "rowan thistlewood")
        "Wendell (Wendy) Puffin"          -> ("puffin wendell", "puffin wendy")
        "Rosalind Ashgrove (dad)"         -> ("ashgrove rosalind",)
        "Minor (Rowan) Thistlewood (dad)" -> ("minor thistlewood", "rowan thistlewood")
        "Rowan Thistlewood"               -> ("rowan thistlewood",)

    An unbalanced "(" matches nothing and is simply a separator, which is what
    name_key does with it too.
    """
    s = str(raw or "")
    if not s.strip():
        return ()

    # (kind, tokens) in source order. "w" = outside text, "g" = a group.
    segments: List[Tuple[str, List[str]]] = []
    pos = 0
    for m in _GROUP_RE.finditer(s):
        if m.start() > pos:
            segments.append(("w", _tokens(s[pos:m.start()])))
        segments.append(("g", _tokens(m.group(1))))
        pos = m.end()
    if pos < len(s):
        segments.append(("w", _tokens(s[pos:])))

    readings: List[List[str]] = []

    outside = [t for kind, toks in segments if kind == "w" for t in toks]
    if outside:
        readings.append(outside)

    for i, (kind, toks) in enumerate(segments):
        if kind != "g" or not toks:
            continue
        after = [t for k, ts in segments[i + 1:] if k == "w" for t in ts]
        if not after:
            continue                    # trailing group: an annotation, not a name
        readings.append(toks + after)

    out: List[str] = []
    for reading in readings:
        k = _key(reading)
        if k and k not in out:
            out.append(k)
    return tuple(out)


def names_agree(a, b) -> bool:
    """
    True when two names share at least one reading.

    This is the ONLY name comparison the attach route makes, on the results-table
    name cell and on the chart header alike, and it is the same test the CRM's
    matcher makes. Deliberately an EQUALITY on a reading rather than a subset:
    a middle name present on one side and not the other is a real difference, and
    "Thistlewood" must not satisfy "Thistlewood-Smith".
    """
    left = set(name_keys(a))
    if not left:
        return False
    return bool(left & set(name_keys(b)))
