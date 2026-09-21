"""
Synthetic verification for id-directed selection on the survey-attach route.

Run:  <venv>/bin/python -m tests.test_attach_chart_id

WHAT THIS IS ABOUT. Attach used to choose a record by name and date of birth,
and refuse whenever those could not single one out — a common surname filling
the page, or two people genuinely sharing both. When the CRM knows the chart id
it no longer has to guess, so those refusals become unnecessary. This file
proves the new path chooses correctly, refuses when the expected record is
absent, and — the point of the build — does not lower the bar on its way past
those guards.

The fixtures REPRODUCE THE REAL STRUCTURE rather than a convenient one: the
results page is table#PatientSearchTableList whose rows ALTERNATE between
tr.Row and tr.AlternateRow, with anchors carrying the real href shape, served
on TherapyNotes' own origin so the relative hrefs resolve and the click is a
real navigation. The helpers are
imported from tests.test_survey_attach rather than copied, so the two suites
cannot drift into testing different pages.

All names, dates, numbers and ids are synthetic.
"""

import asyncio
import logging
import re
import sys

from playwright.async_api import async_playwright

from services.api.survey_attach_executor import SurveyAttachExecutor
from shared.patient_row_parsing import chart_id_from_href
from shared.schemas.survey_attach import SurveyAttachInput, SurveyAttachOutput
from tests.test_survey_attach import (
    ANCHOR_SEL,
    CLINICIAN,
    DOB_PADDED,
    DOB_SHORT,
    FIRST,
    LAST,
    ORIGIN,
    PHONE_IN,
    PID,
    Results,
    _Cap,
    chart_page,
    make_ex,
    make_input,
    results_page,
)

# The record the CRM believes this survey belongs to.
WANTED = "ZzWantedChart777"


def crowd(n, *, include_wanted_at=None, same_name=False):
    """
    n result rows, optionally with the wanted id planted at one index.

    same_name=True gives every row the target's NAME AND DATE OF BIRTH — the
    case that refuses today as multiple_candidates, and the one a chart id is
    supposed to rescue.
    """
    rows = []
    for i in range(n):
        if include_wanted_at is not None and i == include_wanted_at:
            rows.append((WANTED, f"{FIRST} {LAST}", DOB_SHORT))
        elif same_name:
            rows.append((f"ZzOther{i:03d}", f"{FIRST} {LAST}", DOB_SHORT))
        else:
            rows.append((f"ZzOther{i:03d}", f"Zzsomeone{i} {LAST}", f"{i % 12 + 1}/3/1977"))
    return rows


async def run_select(browser, rows, data, chart_html=None):
    """
    Drive _phase_select alone against a results page on TN's origin.

    Mirrors tests.test_survey_attach.run_search_select, but lets the chart the
    click lands on be varied, so "the expected id opens, then fails
    verification" can be built.
    """
    page = await browser.new_page(viewport={"width": 1400, "height": 1000})

    async def handler(route, request):
        body = (chart_html or chart_page()) if "/patients/edit/" in request.url else results_page(rows)
        await route.fulfill(status=200, content_type="text/html", body=body)

    await page.route(f"{ORIGIN}/**", handler)
    await page.goto(f"{ORIGIN}/app/patients/")
    ex = make_ex(page)
    ex._selection_mode = None
    # Counted by ANCHOR, never by tr.Row, which matches only the alternating half.
    ex._row_count = await page.locator(ANCHOR_SEL).count()
    ok = await ex._phase_select(data)
    return ex, page, ok


async def main():
    r = Results()
    cap = _Cap(); root = logging.getLogger(); root.addHandler(cap); root.setLevel(logging.DEBUG)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()

            # ==============================================================
            print("\n[1] A FULL PAGE of rows, the expected id among them -> opens THAT record")
            # Yesterday this refused: a full page is at RESULT_CAP_SUSPECT, so
            # the truncation guard fired before anything was narrowed.
            #
            # SIZED FROM THE CONSTANT, not from a literal. The cap was re-measured
            # on 2026-09-21 (10 -> 20, the old value having been the page seen
            # through tr.Row), and a hardcoded 15 quietly stopped being "over the
            # cap" the moment it moved. The intent here is "a full page", so the
            # fixture says that.
            CAP = SurveyAttachExecutor.RESULT_CAP_SUSPECT
            rows = crowd(CAP, include_wanted_at=8)
            ex, page, ok = await run_select(browser, rows, make_input(expected_chart_id=WANTED))
            r.check("selected", ok is True, ex._pending.get("message", ""))
            r.check("opened the expected chart, not another row",
                    chart_id_from_href(page.url) == WANTED, page.url)
            r.check("recorded that it selected by chart id",
                    ex._selection_mode == "chart_id", ex._selection_mode)
            r.check("the truncation guard did NOT fire",
                    ex._pending.get("reason") != "result_set_possibly_truncated")
            await page.close()

            # The same full page WITHOUT an id still refuses — proof the guard
            # is intact and the id is what moved, not the bar.
            ex, page, ok = await run_select(browser, rows, make_input())
            r.check("the same full page still refuses when no id is supplied", ok is False)
            r.check("...as result_set_possibly_truncated",
                    ex._pending.get("reason") == "result_set_possibly_truncated",
                    ex._pending.get("reason"))
            await page.close()

            # ==============================================================
            print("\n[2] Two patients sharing name AND date of birth -> the id separates them")
            rows = crowd(3, include_wanted_at=1, same_name=True)
            ex, page, ok = await run_select(browser, rows, make_input(expected_chart_id=WANTED))
            r.check("selected", ok is True, ex._pending.get("message", ""))
            r.check("opened the expected one of the identical rows",
                    chart_id_from_href(page.url) == WANTED, page.url)
            await page.close()

            ex, page, ok = await run_select(browser, rows, make_input())
            r.check("the same rows still refuse without an id", ok is False)
            r.check("...as multiple_candidates",
                    ex._pending.get("reason") == "multiple_candidates", ex._pending.get("reason"))
            await page.close()

            # ==============================================================
            print("\n[3] Expected id ABSENT from the results -> refuses, never falls back")
            # The target's name and date of birth ARE present on a row, so a
            # fallback to name selection would have succeeded. It must not.
            rows = [(f"ZzOther{i:03d}", f"{FIRST} {LAST}", DOB_SHORT) for i in range(2)]
            ex, page, ok = await run_select(browser, rows, make_input(expected_chart_id=WANTED))
            r.check("refused", ok is False)
            r.check("reason is expected_chart_not_in_results",
                    ex._pending.get("reason") == "expected_chart_not_in_results",
                    ex._pending.get("reason"))
            r.check("did NOT open any chart", ex._chart_url is None, ex._chart_url)
            r.check("still on the results page, no navigation happened",
                    "/patients/edit/" not in page.url, page.url)
            msg = ex._pending.get("message", "")
            r.check("the message tells a human what may have happened",
                    "merged" in msg and "discharged" in msg)
            r.check("...and what to do", "by hand" in msg)
            await page.close()

            # A single-row result where that one row is the wrong record: the
            # name path would have opened it. The id path refuses.
            ex, page, ok = await run_select(
                browser, [("ZzWrongOne001", f"{FIRST} {LAST}", DOB_SHORT)],
                make_input(expected_chart_id=WANTED))
            r.check("one perfectly-matching row that is the wrong RECORD still refuses",
                    ok is False and ex._pending.get("reason") == "expected_chart_not_in_results",
                    ex._pending.get("reason"))
            await page.close()

            # Absent AND truncated: same verdict, message says the record may be
            # on a page the agent cannot see.
            ex, page, ok = await run_select(
                browser, crowd(SurveyAttachExecutor.RESULT_CAP_SUSPECT),
                make_input(expected_chart_id=WANTED))
            r.check("absent from a truncated set refuses with the same reason",
                    ok is False and ex._pending.get("reason") == "expected_chart_not_in_results",
                    ex._pending.get("reason"))
            r.check("...and says the record may be on an unseen page",
                    "cannot see" in ex._pending.get("message", ""))
            await page.close()

            # ==============================================================
            print("\n[4] Expected id present, but the chart fails verification -> refuses")
            # THE BAR HAS NOT MOVED. An id chooses the record; the four-field
            # check still decides whether to attach.
            wrong_person = chart_page(name="Zzsomeone Zzelse")
            rows = crowd(SurveyAttachExecutor.RESULT_CAP_SUSPECT, include_wanted_at=8)
            ex, page, ok = await run_select(
                browser, rows, make_input(expected_chart_id=WANTED), chart_html=wrong_person)
            r.check("selection still succeeded (the id chose the record)", ok is True)
            ex._page = page
            verified = await ex._phase_verify(make_input(expected_chart_id=WANTED))
            r.check("verification REFUSED on the id-selected chart", verified is False)
            r.check("reason is name_mismatch, not an id reason",
                    ex._pending.get("reason") == "name_mismatch", ex._pending.get("reason"))
            await page.close()

            for label, kw, reason in [
                ("date of birth", dict(dob="3/4/1985"), "dob_mismatch"),
                ("phone", dict(phone="505-555-0999"), "phone_mismatch"),
                ("clinician", dict(clinicians=("Zzother Zzclinician",)), "clinician_mismatch"),
            ]:
                ex, page, ok = await run_select(
                    browser, rows, make_input(expected_chart_id=WANTED), chart_html=chart_page(**kw))
                ex._page = page
                verified = await ex._phase_verify(make_input(expected_chart_id=WANTED))
                r.check(f"{label} still checked on an id-selected chart",
                        verified is False and ex._pending.get("reason") == reason,
                        ex._pending.get("reason"))
                await page.close()

            # And the whole verification passes on the right chart, so [4] is not
            # passing merely because verification always refuses here.
            ex, page, ok = await run_select(
                browser, rows, make_input(expected_chart_id=WANTED), chart_html=chart_page())
            ex._page = page
            verified = await ex._phase_verify(make_input(expected_chart_id=WANTED))
            r.check("a correct id-selected chart verifies cleanly", verified is True,
                    ex._pending.get("message", ""))
            await page.close()

            # ==============================================================
            print("\n[5] No id supplied -> yesterday's behaviour, exactly")
            # One clean row: selects by name, as it always did.
            ex, page, ok = await run_select(browser, [(PID, f"{FIRST} {LAST}", DOB_SHORT)], make_input())
            r.check("selected by name", ok is True, ex._pending.get("message", ""))
            r.check("opened the row it narrowed to", chart_id_from_href(page.url) == PID, page.url)
            r.check("recorded that it selected by name", ex._selection_mode == "name", ex._selection_mode)
            await page.close()

            # Every no-id refusal still refuses, for its own reason.
            for label, rows_, reason in [
                ("zero results", [], "patient_not_found"),
                ("name but wrong date of birth",
                 [(PID, f"{FIRST} {LAST}", "9/9/1955")], "patient_not_found"),
                ("two identical", crowd(2, same_name=True), "multiple_candidates"),
                ("a full page", crowd(SurveyAttachExecutor.RESULT_CAP_SUSPECT),
                 "result_set_possibly_truncated"),
            ]:
                ex, page, ok = await run_select(browser, rows_, make_input())
                r.check(f"no-id: {label} -> {reason}",
                        ok is False and ex._pending.get("reason") == reason,
                        ex._pending.get("reason"))
                await page.close()

            # ==============================================================
            print("\n[6] Edge case — the expected id on two rows")
            # Should be impossible. Two rows carrying one id are two links to one
            # record, so opening it is correct; refusing would be pedantry.
            rows = crowd(4, include_wanted_at=1)
            rows[3] = (WANTED, f"{FIRST} {LAST}", DOB_SHORT)
            ex, page, ok = await run_select(browser, rows, make_input(expected_chart_id=WANTED))
            r.check("opens the record rather than refusing", ok is True,
                    ex._pending.get("message", ""))
            r.check("landed on that record", chart_id_from_href(page.url) == WANTED, page.url)
            r.check("and said so in the log",
                    any("appeared on 2 rows" in l for l in cap.lines),
                    [l for l in cap.lines if "appeared on" in l])
            await page.close()

            await browser.close()

        # ==================================================================
        print("\n[7] The contract — the field is optional and blank means absent")
        base = dict(first_name=FIRST, last_name=LAST, dob=DOB_PADDED, phone=PHONE_IN,
                    clinician_name=CLINICIAN, pdf_url="https://example.test/s.pdf",
                    document_name="Client Survey")
        r.check("omitted entirely is valid",
                SurveyAttachInput(**base).expected_chart_id is None)
        r.check("explicit None is valid",
                SurveyAttachInput(**base, expected_chart_id=None).expected_chart_id is None)
        r.check("a real id survives", SurveyAttachInput(**base, expected_chart_id=WANTED)
                .expected_chart_id == WANTED)
        for blank in ("", "   ", "\t"):
            r.check(f"blank {blank!r} collapses to absent, not to an id to hunt for",
                    SurveyAttachInput(**base, expected_chart_id=blank).expected_chart_id is None)
        r.check("surrounding whitespace is trimmed",
                SurveyAttachInput(**base, expected_chart_id=f"  {WANTED} ")
                .expected_chart_id == WANTED)

        print("\n[8] The refusal reason is named in the contract")
        src = open("shared/schemas/survey_attach.py").read()
        r.check("expected_chart_not_in_results is in the reason vocabulary",
                '"expected_chart_not_in_results"' in src)
        out = SurveyAttachOutput.failure(
            phase=None, reason="expected_chart_not_in_results", message="m",
            logs=[], duration_ms=0, selection_mode="chart_id")
        r.check("it round-trips through the output model",
                out.failure_reason == "expected_chart_not_in_results")
        r.check("the output carries which path ran", out.selection_mode == "chart_id")
        r.check("selection_mode defaults to absent when nothing set it",
                SurveyAttachOutput(status="success").selection_mode is None)

        print("\n[9] The id space is the pull's, not a second regex")
        ex_src = open("services/api/survey_attach_executor.py").read()
        r.check("the executor reuses chart_id_from_href",
                "from shared.patient_row_parsing import chart_id_from_href" in ex_src)
        r.check("...and does not define its own id regex for this",
                ex_src.count("patients/(?:edit") == 1)
        # The href shape the results page actually renders, through that parser.
        r.check("the parser reads the real href shape",
                chart_id_from_href(f"/app/patients/edit/{WANTED}/") == WANTED)

        print("\n[10] No fallback exists in the source")
        sel = ex_src[ex_src.index("async def _select_by_chart_id"):ex_src.index("async def _phase_verify")]
        r.check("the id path never calls the name narrowing", "_name_tokens" not in sel)
        r.check("...and never consults the date of birth", "_normalize_dob" not in sel)
        r.check("its only refusal for an absent id is the new reason",
                sel.count("expected_chart_not_in_results") == 1)
        r.check("verification is not reached from inside it", "_phase_verify" not in sel)
        # PHI: the id path's log lines carry counts, never a row's text.
        logs = re.findall(r"logger\.(?:info|warning|error)\(([\s\S]*?)\)\n", sel)
        blob = "\n".join(logs)
        for forbidden in ("first_name", "last_name", "data.dob", "data.phone", "innerText", "want"):
            r.check(f"no log line carries {forbidden}", forbidden not in blob)

    finally:
        root.removeHandler(cap)

    print(f"\n{'PASS' if r.failed == 0 else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    if r.failed:
        print("\nFailures:"); [print(f"  - {l}") for l in r.lines]
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
