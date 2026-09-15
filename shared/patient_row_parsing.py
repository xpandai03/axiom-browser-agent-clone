"""
Parsing the Patients results table — pure, and dependency-free on purpose.

Split out of services/api/active_patients_executor.py so it can be exercised
without FastAPI, pydantic or Playwright installed. The executor re-exports every
name below, so nothing about its public surface changed.

NOTHING HERE INTERPRETS A VALUE. squeeze() collapses whitespace and that is the
only transformation applied anywhere in this route; the CRM normalises for
matching, and a value cleaned here cannot be un-cleaned there.

WHY COLUMNS ARE DISCOVERED RATHER THAN INDEXED. The Patients-page recon is
explicit that "cell-index selectors are positional and should not be used", and
it records name and date of birth as mapped while phone is NOT
(docs/selectors/tn_v2_phases.md, "Q2"). So the header row is read and matched by
label, with a shape-based fallback, and a field with no column is reported
absent rather than guessed at.
"""

import re
from typing import Dict, List, Optional, Tuple


# Header labels that identify each column. Matched case-insensitively against a
# squeezed header string, so "Date of Birth" and "DOB" both land.
_DOB_HEADERS = ("date of birth", "dob", "birth date", "birthdate")
_PHONE_HEADERS = ("phone", "mobile", "cell", "telephone")
_NAME_HEADERS = ("name", "patient")

# A date the page might render. Deliberately loose on separator and padding —
# this decides WHICH CELL is the date, not what the date means.
_RE_DATE = re.compile(r"\b\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}\b")
# A phone with enough digits to be one. Punctuation is irrelevant to the test.
_RE_PHONEISH = re.compile(r"(?:\D*\d){7,}")
# A patient id out of an href: /app/patients/edit/<id>/ and near variants.
_RE_CHART_ID = re.compile(r"/patients/(?:edit/)?([A-Za-z0-9_-]{4,})")


def squeeze(text: Optional[str]) -> str:
    """Collapse whitespace. The ONLY transformation applied to any value."""
    return " ".join((text or "").split())


def classify_headers(headers: List[str]) -> Dict[str, Optional[int]]:
    """
    Map each identity field to a column index, from the header labels.

    Returns None for a field with no column, which is how "the table does not
    carry a phone" is distinguished from "this patient has no phone". Name is
    deliberately allowed to stay None: the name comes off the anchor, not off a
    column, so it never needs one.
    """
    lowered = [squeeze(h).lower() for h in headers]

    def find(needles: Tuple[str, ...], skip: Optional[int] = None) -> Optional[int]:
        for i, h in enumerate(lowered):
            if i == skip or not h:
                continue
            if any(n in h for n in needles):
                return i
        return None

    name_at = find(_NAME_HEADERS)
    # Search phone before date of birth is irrelevant, but a header reading
    # "Phone" must not also satisfy the name test, hence the explicit skip.
    return {
        "name": name_at,
        "dob": find(_DOB_HEADERS, skip=name_at),
        "phone": find(_PHONE_HEADERS, skip=name_at),
    }


def extract_row(
    cells: List[str], columns: Dict[str, Optional[int]],
) -> Tuple[str, str]:
    """
    (dob, phone) for one row's cell texts.

    Uses the discovered column when there is one. Falls back to a shape scan
    when there is not — a table with no usable header still has a date-shaped
    cell and a phone-shaped cell, and finding them by shape is honest about
    being a guess in a way that a hardcoded index is not.

    Never invents: a row whose cells hold neither shape returns empty strings.
    """
    def at(idx: Optional[int]) -> str:
        return squeeze(cells[idx]) if idx is not None and 0 <= idx < len(cells) else ""

    dob = at(columns.get("dob"))
    phone = at(columns.get("phone"))

    if not dob:
        for c in cells:
            s = squeeze(c)
            if _RE_DATE.search(s):
                dob = s
                break

    if not phone:
        for c in cells:
            s = squeeze(c)
            # The date cell is phone-shaped under a digit count test, so it is
            # excluded explicitly rather than by ordering luck.
            if s and s != dob and not _RE_DATE.search(s) and _RE_PHONEISH.search(s):
                phone = s
                break

    return dob, phone


def chart_id_from_href(href: Optional[str]) -> str:
    """The patient id out of a result anchor's href. Empty when unrecognisable."""
    m = _RE_CHART_ID.search(href or "")
    return m.group(1) if m else ""


