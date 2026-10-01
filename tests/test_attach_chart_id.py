"""
Synthetic verification that the survey-attach route IGNORES a chart id.

Run:  <venv>/bin/python -m tests.test_attach_chart_id

WHY. Attach used to open the row whose link carried the chart id the CRM sent.
That id came from the nightly pull, and TherapyNotes' record id (the token in
/app/patients/edit/<id>/) changes between page loads: the pull's id for a
patient is never the id a later search shows for the same patient. Every
id-directed attempt refused with expected_chart_not_in_results (2026-09-22 to
2026-10-01) while the patient was on screen. Selection is now by name and date
of birth only; the field is still ACCEPTED, because a CRM that still sends it
must not be rejected, but it never decides which chart is opened.

The fixtures are the shared ones from tests.test_survey_attach: a Patients page
served on TherapyNotes' origin with real form submits, filters and paging.

All names, dates and ids are synthetic.
"""

import asyncio
import sys

from playwright.async_api import async_playwright

from shared.schemas.survey_attach import SurveyAttachInput
from tests.test_survey_attach import (
    DOB_SHORT,
    FIRST,
    LAST,
    Results,
    make_input,
    run_search_select,
)


async def main():
    r = Results()
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        print("\n[1] The id names a DECOY (same name, other DOB) -> the name+DOB row is opened")
        rows = [("ZzDecoy1", f"{FIRST} {LAST}", "5/6/1975"),
                ("ZzTarget2", f"{FIRST} {LAST}", DOB_SHORT)]
        ex, page, ok = await run_search_select(browser, rows, make_input(expected_chart_id="ZzDecoy1"))
        r.check("selection succeeded", ok is True, ex._pending.get("message", ""))
        r.check("opened the name+DOB row", "ZzTarget2/" in (ex._chart_url or ""), ex._chart_url)
        r.check("recorded as name selection", ex._selection_mode == "name", ex._selection_mode)
        await page.close()

        print("\n[2] The id is on NO row -> proceeds by name, never expected_chart_not_in_results")
        rows = [("ZzTarget2", f"{FIRST} {LAST}", DOB_SHORT)]
        ex, page, ok = await run_search_select(browser, rows, make_input(expected_chart_id="ZzStale999"))
        r.check("selection succeeded", ok is True, ex._pending.get("message", ""))
        r.check("no expected_chart_not_in_results",
                ex._pending.get("reason") != "expected_chart_not_in_results", ex._pending.get("reason"))
        await page.close()

        print("\n[3] Two rows share name AND DOB; the id names one -> still multiple_candidates")
        # The id used to separate them. It cannot be trusted to, so the bar stays
        # where name + DOB put it: a person decides.
        rows = [("ZzTwinA", f"{FIRST} {LAST}", DOB_SHORT), ("ZzTwinB", f"{FIRST} {LAST}", DOB_SHORT)]
        ex, page, ok = await run_search_select(browser, rows, make_input(expected_chart_id="ZzTwinA"))
        r.check("refused", ok is False)
        r.check("reason is multiple_candidates",
                ex._pending.get("reason") == "multiple_candidates", ex._pending.get("reason"))
        r.check("no chart opened", ex._chart_url is None)
        await page.close()

        print("\n[4] With and without an id -> the same chart")
        rows = [("ZzA", f"{FIRST} {LAST}", "1/1/1971"), ("ZzB", f"{FIRST} {LAST}", DOB_SHORT)]
        ex1, p1, ok1 = await run_search_select(browser, rows, make_input())
        ex2, p2, ok2 = await run_search_select(browser, rows, make_input(expected_chart_id="ZzA"))
        r.check("both succeed", ok1 is True and ok2 is True)
        r.check("both opened ZzB", "ZzB/" in (ex1._chart_url or "") and "ZzB/" in (ex2._chart_url or ""),
                (ex1._chart_url, ex2._chart_url))
        await p1.close(); await p2.close()

        await browser.close()

    print("\n[5] The contract still ACCEPTS the field (a CRM that sends it is not rejected)")
    r.check("present is accepted", make_input(expected_chart_id="Zz1").expected_chart_id == "Zz1")
    r.check("blank means absent", make_input(expected_chart_id="   ").expected_chart_id is None)
    r.check("absent is fine", make_input().expected_chart_id is None)
    r.check("the field says it is ignored",
            "IGNORED" in (SurveyAttachInput.model_fields["expected_chart_id"].description or ""))

    print("\n[6] The id-directed path is gone from the executor")
    src = open("services/api/survey_attach_executor.py", encoding="utf-8").read()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    r.check("no _select_by_chart_id", "_select_by_chart_id" not in code)
    r.check("never emits expected_chart_not_in_results", '"expected_chart_not_in_results"' not in code)
    r.check("no chart_id_from_href in the attach route", "chart_id_from_href" not in code)

    print(f"\n{'PASS' if r.failed == 0 else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    if r.failed:
        print("\nFailures:"); [print(f"  - {l}") for l in r.lines]
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
