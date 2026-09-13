"""
Synthetic verification for the active-count parser.

The live recon saw ONE wording: the plural range form. Everything else here is
the defensive case the prompt calls for — one patient, zero patients, a filtered
subset, and text that does not parse at all.

The load-bearing assertion in this file is [D]: unfamiliar text must yield None,
not a number and not a zero. A wrong denominator silently inflates a percentage
the client reads as fact; a missing one is visible. So "I could not read this"
has to survive all the way out as a failure.
"""

import pytest

from services.api.active_count_executor import (
    CLICK_ALLOWLIST,
    FILL_ALLOWLIST,
    SEL_PAGER_NEXT,
    SEL_SEARCH_INPUT,
    SEL_SEARCH_SUBMIT,
    parse_active_count,
)
from shared.schemas.active_count import ActiveCountOutput, ClinicianActiveCount


# ---------------------------------------------------------------------------
# [A] The plural range form — the only wording the live page was seen to use
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Displaying 1-20 of 936 active patients", 936),
    ("Displaying 1-20 of 47", 47),
    ("Displaying 21-34 of 34 active unassigned patients", 34),
    ("Displaying 81-93 of 93 active patients with a birthday this month", 93),
    ("Displaying 1-20 of 1,855 patients", 1855),          # thousands separator
    ("Displaying 1–20 of 936 active patients", 936),       # en dash
    ("Displaying 1 to 20 of 936 active patients", 936),    # worded range
    ("  displaying   1-20   of   936   active patients  ", 936),  # whitespace/case
])
def test_a_plural_range(text, expected):
    assert parse_active_count(text) == expected


def test_a_range_beats_the_leading_number():
    """
    '1-20 of 936' contains three numbers. The total is the LAST one, and a
    single-number pattern matching first would return 1 — a denominator of 1
    turns every percentage into thousands of percent.
    """
    assert parse_active_count("Displaying 1-20 of 936 active patients") == 936


# ---------------------------------------------------------------------------
# [B] Singular — never observed live
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Displaying 1 of 1 active patient", 1),
    ("Displaying 1 patient", 1),
    ("Displaying 1 active patient", 1),
    ("Displaying all 43 patients", 43),
    ("Displaying all 1 patient", 1),
    ("Displaying 7 active patients", 7),
])
def test_b_singular_and_all_forms(text, expected):
    assert parse_active_count(text) == expected


# ---------------------------------------------------------------------------
# [C] Zero — the wording almost certainly differs, and 0 is a real answer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "No active patients found",
    "No patients found",
    "There are no patients to display",
    "No patients match your search",
    "No active patients match your search criteria",
    "0 patients",
    "0 active patients",
])
def test_c_zero_is_a_real_count(text):
    assert parse_active_count(text) == 0


# ---------------------------------------------------------------------------
# [D] Unparseable must be None — the case that protects the client's number
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    None,
    "",
    "   ",
    "Loading...",
    "An error occurred. Please try again.",
    "Displaying",                       # the word alone, no number
    "Displaying patients",              # no number at all
    "Displaying results",               # no 'patients', no number
    "Your session has expired",
    "Patients",
    "of 936",                           # a number but no 'Displaying' context
    "936",                              # a bare number is NOT a count
])
def test_d_unparseable_returns_none(text):
    assert parse_active_count(text) is None


def test_d_unfamiliar_text_is_not_silently_zero():
    """An unrecognised string must never collapse to 0 — that is a real count."""
    assert parse_active_count("Something we have never seen before") is None


def test_d_bare_number_is_not_a_count():
    """
    A stray number elsewhere on the page must not be mistaken for the total.
    Only the 'Displaying ...' phrasing (or an explicit zero wording) counts.
    """
    assert parse_active_count("1855") is None
    assert parse_active_count("Page 1 of 47") is None


# ---------------------------------------------------------------------------
# [E] One failure does not end the pass
# ---------------------------------------------------------------------------

def test_e_partial_pass_keeps_the_successes():
    rows = [
        ClinicianActiveCount(option_value=f"clinician-{i}", label=f"Staff {i}",
                             status="success", active_count=10 + i)
        for i in range(28)
    ]
    rows.append(ClinicianActiveCount(
        option_value="clinician-x", label="Staff X", status="failure",
        failure_reason="count_unparseable", message="could not parse"))
    rows.append(ClinicianActiveCount(
        option_value="clinician-y", label="Staff Y", status="failure",
        failure_reason="timeout", message="timed out"))

    out = ActiveCountOutput.completed(rows, [], 1000)
    assert out.status == "partial"          # NOT 'failure' — 28 rows are usable
    assert out.succeeded == 28
    assert out.failed == 2
    assert out.total_options == 30
    # the failures are named, not swallowed
    assert {r.failure_reason for r in out.results if r.status == "failure"} == {
        "count_unparseable", "timeout"}


def test_e_all_success_is_success():
    rows = [ClinicianActiveCount(option_value="c1", label="A",
                                 status="success", active_count=5)]
    assert ActiveCountOutput.completed(rows, [], 1).status == "success"


def test_e_all_failed_is_failure():
    rows = [ClinicianActiveCount(option_value="c1", label="A", status="failure",
                                 failure_reason="timeout")]
    assert ActiveCountOutput.completed(rows, [], 1).status == "failure"


def test_e_a_failed_row_carries_no_number():
    """A failure must not ship a count — that is the whole point of failing."""
    r = ClinicianActiveCount(option_value="c1", label="A", status="failure",
                             failure_reason="count_unparseable")
    assert r.active_count is None


# ---------------------------------------------------------------------------
# [F] Read-only is structural
# ---------------------------------------------------------------------------

def test_f_click_allowlist_is_exactly_two_navigations():
    assert CLICK_ALLOWLIST == {SEL_SEARCH_SUBMIT, SEL_PAGER_NEXT}


def test_f_fill_allowlist_is_the_search_box_alone():
    assert FILL_ALLOWLIST == {SEL_SEARCH_INPUT}


def test_f_no_write_control_is_named_anywhere_in_the_module():
    """
    The module cannot click what it cannot name. Guard against a future edit
    quietly introducing a save/create/delete selector.
    """
    import pathlib
    src = pathlib.Path("services/api/active_count_executor.py").read_text().lower()
    # Only look at selector-ish string literals, not prose in the docstring.
    selector_lines = [l for l in src.splitlines()
                      if ('"' in l or "'" in l) and ("#ctl00" in l or "input#" in l
                                                     or "button" in l or "a#" in l)]
    joined = " ".join(selector_lines)
    for forbidden in ("buttonsave", "buttoncreate", "buttondelete", "buttonsubmit",
                      "createpatient", "savepatient", "adddocument"):
        assert forbidden not in joined, f"write-capable selector present: {forbidden}"


def test_f_label_is_returned_verbatim():
    """
    Matching a label to a CRM provider is the CRM's job. A label mangled here
    cannot be un-mangled there, so the schema must not normalise it.
    """
    odd = "  Abena Marfowaa Owusu-Nkwantabisah  "
    r = ClinicianActiveCount(option_value="clinician-1", label=odd,
                             status="success", active_count=1)
    assert r.label == odd


def test_f_test_anna_is_reported_never_subtracted():
    """The active count must be untouched by the Test Anna figure."""
    r = ClinicianActiveCount(option_value="clinician-1", label="Staff",
                             status="success", active_count=47,
                             test_anna_exact=2, test_anna_token_match=0,
                             test_anna_status="success")
    assert r.active_count == 47      # not 45


# ---------------------------------------------------------------------------
# [G] Wordings found on the LIVE page that recon never showed
# ---------------------------------------------------------------------------
# These are transcribed from the first live pass. They matter more than the
# invented cases above, because two of them are forms I had guessed wrong.

from services.api.active_count_executor import is_all_without_number


@pytest.mark.parametrize("text,expected", [
    # the practice-wide and unassigned forms
    ("Displaying 1-20 of 936 active patients.", 936),
    ("Displaying 1-20 of 34 active unassigned patients.", 34),
    # the per-clinician form — note the SECOND "of", which must not confuse the
    # range pattern into returning 20
    ("Displaying 1-20 of 33 of Anna Aldridge's active patients.", 33),
    ("Displaying 1-20 of 47 of Ty Jones's active patients.", 47),
    # the real zero wording, which is not any of the phrasings I had guessed
    ("There were no patients matching your search.", 0),
])
def test_g_live_wordings(text, expected):
    assert parse_active_count(text) == expected


def test_g_possessive_form_does_not_return_the_page_size():
    """
    'Displaying 1-20 of 33 of Anna Aldridge's ...' contains two 'of's. Returning
    the first number after an 'of' would give 20 — the PAGE SIZE — for every
    clinician with more than 20 patients, which would look entirely plausible
    and be wrong for exactly the busiest providers.
    """
    assert parse_active_count(
        "Displaying 1-20 of 33 of Anna Aldridge's active patients.") == 33


@pytest.mark.parametrize("text", [
    "Displaying all of Nona Bockius's active patients.",
    "Displaying all of Chantel Moya's active patients.",
])
def test_g_all_without_number_is_not_parseable(text):
    """
    When the set fits on one page TherapyNotes prints NO total. The parser must
    still refuse — the number comes from counting the (complete, unpaged) rows,
    which is the caller's job, not a number this function may invent.
    """
    assert parse_active_count(text) is None
    assert is_all_without_number(text) is True


def test_g_numbered_all_form_stays_on_the_normal_path():
    """'Displaying all 43 patients' has a number and must NOT take the row path."""
    assert parse_active_count("Displaying all 43 patients") == 43
    assert is_all_without_number("Displaying all 43 patients") is False


def test_g_all_detector_ignores_unrelated_text():
    for text in (None, "", "Loading...", "Displaying 1-20 of 936 active patients."):
        assert is_all_without_number(text) is False
