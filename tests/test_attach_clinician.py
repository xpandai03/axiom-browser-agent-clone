"""
The survey-attach clinician check uses scheduling's word rule, and says what it
compared when it refuses.

Run:  <venv>/bin/python -m pytest -q tests/test_attach_clinician.py

Before 2026-09-29 the CRM sent "Tyra Jones (ABQ)"; every word, including "abq",
had to appear on the chart, whose assignment reads "Ty Jones". No survey ever
attached. The CRM now sends the TherapyNotes form ("Ty Jones"); this side
compares it by the same rule scheduling uses, tolerates a leftover location
code, and quotes both strings on a refusal.

Patient identities are the repo's synthetic fixtures; clinician names are staff
names.
"""

import pytest

from services.api.survey_attach_executor import (
    clinician_for_comparison,
    clinician_on_chart,
    clinician_refusal_message,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("sent,chart,expected", [
    ("Ty Jones", ["Ty Jones"], True),                       # the recon rendering: name alone
    ("Ty Jones", ["Jones, Ty"], True),                      # "Last, First"
    ("Ty Jones", ["Jones, Ty, LMHC"], True),                # with a credential
    ("Ty Jones", ["Liz Lopez", "Ty Jones"], True),          # one of several assignments
    ("Ty Jones (ABQ)", ["Ty Jones"], True),                 # an older CRM's roster label
    ("Tyra Jones (ABQ)", ["Ty Jones"], False),              # alias is the CRM's job; not guessed here
    ("Danya Estrada-Rivera", ["Danya Estrada-Rivera"], True),
    ("Liz Lopez", ["Ty Jones"], False),
    ("Ty Jones", ["Ty"], False),                            # a bare first name cannot satisfy a full name
    ("", ["Ty Jones"], False),
    ("Ty Jones", [], False),
])
async def test_word_rule(sent, chart, expected):
    assert clinician_on_chart(sent, chart) is expected


async def test_location_is_dropped_before_comparing():
    assert clinician_for_comparison("  Tyra   Jones (ABQ) ") == "Tyra Jones"
    assert clinician_for_comparison(None) == ""


async def test_refusal_names_both_sides():
    msg = clinician_refusal_message("Ty Jones (ABQ)", ["Liz Lopez", "Kristi Simmons"])
    assert "'Ty Jones'" in msg and "'Liz Lopez'" in msg and "'Kristi Simmons'" in msg
    assert "2 chart assignment(s)" in msg
    assert "(ABQ)" not in msg
    assert clinician_refusal_message("Ty Jones", []).endswith(": none.")


# ---- The real verify phase, on the repo's synthetic chart --------------------

async def _verify(clinician_sent, chart_clinicians):
    from playwright.async_api import async_playwright
    import tests.test_survey_attach as fx

    data = fx.make_input(clinician_name=clinician_sent)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-http2"])
        ex, page, ok = await fx.run_verify(browser, fx.chart_page(clinicians=tuple(chart_clinicians)), data)
        pending = dict(ex._pending)
        await browser.close()
    return ok, pending


async def test_phase_verify_accepts_the_tn_form():
    ok, pending = await _verify("Ty Jones", ["Ty Jones"])
    assert ok is True, pending


async def test_phase_verify_accepts_last_first_with_credential():
    ok, pending = await _verify("Ty Jones", ["Jones, Ty, LMHC"])
    assert ok is True, pending


async def test_phase_verify_survives_a_location_code():
    ok, pending = await _verify("Ty Jones (ABQ)", ["Ty Jones"])
    assert ok is True, pending


async def test_phase_verify_refuses_with_both_strings():
    ok, pending = await _verify("Ty Jones", ["Liz Lopez"])
    assert ok is False
    assert pending["reason"] == "clinician_mismatch"
    assert "'Ty Jones'" in pending["message"] and "'Liz Lopez'" in pending["message"]
