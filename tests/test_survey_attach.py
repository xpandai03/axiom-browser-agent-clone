"""
Synthetic verification for the survey-attach route.

Fixtures reproduce the structure recon VERIFIED on 2026-09-08:
  - Patients-page results: table#PatientSearchTableList > tr.Row, with
    a[data-testid='patient-search-patient-link'] and -dob-link carrying the id
  - Chart: div#PatientInformation__PatientName, span#PatientInformation__DOBElem,
    div#PatientInformation__MobilePhoneElem > a
  - Clinicians BEHIND a tab: a[href='#tab=Clinicians'] ->
    .clinician-assignments .clinician-assignment a
The clinicians being behind a tab is reproduced, not flattened — two previous
suites in this repo passed while the code was broken because their fixtures did
not match the real page.

All names, dates and numbers are synthetic.

Run:  <venv>/bin/python -m tests.test_survey_attach
"""

import asyncio
import logging
import sys

from playwright.async_api import async_playwright

from services.api.survey_attach_executor import SurveyAttachExecutor, _digits
from shared.schemas.survey_attach import SurveyAttachInput

PID = "ZzTestChartId0001"
FIRST, LAST = "Zzmary", "Zzwatson"
DOB_PADDED = "01/02/1990"       # what the survey/payload carries
DOB_SHORT = "1/2/1990"          # what the chart renders (8 chars, unpadded)
PHONE_IN = "(505) 555-0142"
PHONE_CHART = "505-555-0142"
HOME_CHART = "505-555-0777"
CLINICIAN = "Zzamanda Zzdavison"
OTHER_CLIN = "Zzother Zzclinician"


def results_page(rows):
    """rows: list of (pid, name, dob_text)."""
    trs = "".join(
        f'<tr class="Row"><td></td>'
        f'<td><a data-testid="patient-search-patient-link" href="/app/patients/edit/{p}/">{n}</a></td>'
        f'<td><a data-testid="patient-search-dob-link" href="/app/patients/edit/{p}/">{d}</a></td>'
        f"</tr>"
        for p, n, d in rows
    )
    return f"""<html><body>
      <input id="ctl00_BodyContent_TextBoxSearchPatientName">
      <input type="submit" id="ctl00_BodyContent_ButtonSearch">
      <table id="PatientSearchTableList">{trs}</table>
    </body></html>"""


def chart_page(name=f"{FIRST} {LAST}", dob=DOB_SHORT, phone=PHONE_CHART,
               home_phone="", clinicians=(CLINICIAN,), omit=None, tab_broken=False):
    omit = omit or set()
    name_el = "" if "name" in omit else f'<div id="PatientInformation__PatientName">{name}</div>'
    dob_el = "" if "dob" in omit else f'<span id="PatientInformation__DOBElem">{dob}</span>'
    # Verified structure: the container always EXISTS; a present number sits in an
    # <a> inside it, an absent one leaves it empty.
    def phone_div(elem_id, value):
        inner = f'<a href="tel:x">{value}</a>' if value else ""
        return f'<div id="{elem_id}">{inner}</div>'
    if "phone" in omit:
        ph_el = ""                      # container gone entirely = unreadable
    else:
        ph_el = (phone_div("PatientInformation__MobilePhoneElem", phone)
                 + phone_div("PatientInformation__HomePhoneElem", home_phone))
    entries = "".join(f'<div class="clinician-assignment"><a href="#">{c}</a></div>'
                      for c in clinicians)
    tab = "" if tab_broken else '<a href="#tab=Clinicians" id="clintab">Clinicians</a>'
    return f"""<html><body>
      {name_el}{dob_el}{ph_el}{tab}
      <div id="clinpane" style="display:none">
        <div class="list-container clinician-assignments">{entries}</div>
      </div>
      <script>
        const t = document.getElementById('clintab');
        if (t) t.addEventListener('click', () => {{
          // Hash tab: content swaps in after a beat, as TherapyNotes does.
          setTimeout(() => {{ document.getElementById('clinpane').style.display = 'block'; }}, 300);
        }});
      </script>
    </body></html>"""


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(); self.lines = []
    def emit(self, r):
        try: self.lines.append(r.getMessage())
        except Exception: self.lines.append("<?>")


class Results:
    def __init__(self): self.passed = self.failed = 0; self.lines = []
    def check(self, name, cond, detail=""):
        detail = "" if detail == "" else str(detail)
        if cond:
            self.passed += 1; print(f"  ok   {name}")
        else:
            self.failed += 1; self.lines.append(f"{name} — {detail}"); print(f"  FAIL {name} — {detail}")


def make_input(**over):
    base = dict(first_name=FIRST, last_name=LAST, dob=DOB_PADDED, phone=PHONE_IN,
                clinician_name=CLINICIAN, pdf_url="https://example.test/s.pdf",
                document_name="Client Survey", contact_id=1, run_id="r", callback_url=None)
    base.update(over)
    return SurveyAttachInput(**base)


def make_ex(page):
    ex = object.__new__(SurveyAttachExecutor)
    ex._page = page; ex._logs = []; ex._pending = {}
    ex._start_time = 0.0; ex._chart_url = None; ex._row_count = 0; ex._mech = None
    return ex


ORIGIN = "https://www.therapynotes.com"


async def run_search_select(browser, rows, data):
    """
    Drive search+select against a results page SERVED ON TN'S ORIGIN, so the
    relative hrefs on the result anchors resolve and clicking one performs a real
    navigation — the behaviour _phase_select asserts on.
    """
    page = await browser.new_page(viewport={"width": 1400, "height": 1000})

    async def handler(route, request):
        body = chart_page() if "/patients/edit/" in request.url else results_page(rows)
        await route.fulfill(status=200, content_type="text/html", body=body)

    await page.route(f"{ORIGIN}/**", handler)
    await page.goto(f"{ORIGIN}/app/patients/")
    ex = make_ex(page)
    ex._row_count = await page.locator("#PatientSearchTableList tr.Row").count()
    ok = await ex._phase_select(data)
    return ex, page, ok


async def run_verify(browser, chart_html, data):
    page = await browser.new_page(viewport={"width": 1400, "height": 1000})
    await page.set_content(chart_html)
    ex = make_ex(page)
    ex._chart_url = f"https://www.therapynotes.com/app/patients/edit/{PID}/"
    ok = await ex._phase_verify(data)
    return ex, page, ok


async def main():
    r = Results()
    cap = _Cap(); root = logging.getLogger(); root.addHandler(cap); root.setLevel(logging.DEBUG)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()

            print("\n[A] One result, all four match -> verification PASSES")
            ex, page, ok = await run_verify(browser, chart_page(), make_input())
            r.check("verification passed", ok is True, ex._pending.get("message", ""))
            await page.close()

            print("\n[B] Each field mismatched in turn -> four distinct refusals")
            for label, kw, reason in [
                ("name",      dict(name="Zzsomeone Zzelse"),          "name_mismatch"),
                ("dob",       dict(dob="3/4/1985"),                   "dob_mismatch"),
                ("phone",     dict(phone="505-555-0999"),             "phone_mismatch"),
                ("clinician", dict(clinicians=(OTHER_CLIN,)),         "clinician_mismatch"),
            ]:
                ex, page, ok = await run_verify(browser, chart_page(**kw), make_input())
                r.check(f"{label} mismatch refuses", ok is False)
                r.check(f"{label} reason is {reason}", ex._pending.get("reason") == reason,
                        ex._pending.get("reason"))
                await page.close()

            print("\n[C] Short chart date vs padded survey date -> matches after normalisation")
            ex, page, ok = await run_verify(browser, chart_page(dob=DOB_SHORT), make_input(dob=DOB_PADDED))
            r.check("unpadded m/d/yyyy accepted", ok is True, ex._pending.get("message", ""))
            await page.close()

            print("\n[D] Clinicians list EMPTY -> refuses (cannot confirm the therapist)")
            ex, page, ok = await run_verify(browser, chart_page(clinicians=()), make_input())
            r.check("refused", ok is False)
            r.check("reason is clinician_unassigned",
                    ex._pending.get("reason") == "clinician_unassigned", ex._pending.get("reason"))
            await page.close()

            print("\n[E] TWO clinicians, one matching -> membership test PASSES")
            ex, page, ok = await run_verify(
                browser, chart_page(clinicians=(OTHER_CLIN, CLINICIAN)), make_input())
            r.check("passes on membership, not equality", ok is True, ex._pending.get("message", ""))
            await page.close()

            print("\n[F] A field that cannot be read -> refuses, never passes")
            for omit, label in [({"name"}, "name"), ({"dob"}, "dob"), ({"phone"}, "phone")]:
                ex, page, ok = await run_verify(browser, chart_page(omit=omit), make_input())
                r.check(f"{label} unreadable refuses", ok is False)
                r.check(f"{label} reason is field_unreadable",
                        ex._pending.get("reason") == "field_unreadable", ex._pending.get("reason"))
                await page.close()
            ex, page, ok = await run_verify(browser, chart_page(tab_broken=True), make_input())
            r.check("clinicians tab missing refuses", ok is False)
            r.check("reason is field_unreadable", ex._pending.get("reason") == "field_unreadable")
            await page.close()

            print("\n[G] Zero results -> patient_not_found, no chart opened")
            ex, page, ok = await run_search_select(browser, [], make_input())
            r.check("refused", ok is False)
            r.check("reason is patient_not_found", ex._pending.get("reason") == "patient_not_found")
            r.check("no chart opened", ex._chart_url is None)
            await page.close()

            print("\n[H] Several results narrowing to ONE on date of birth -> proceeds")
            rows = [("Zzid1", f"{FIRST} {LAST}", "5/6/1975"),
                    ("Zzid2", f"{FIRST} {LAST}", DOB_SHORT),
                    ("Zzid3", f"{FIRST} {LAST}", "9/9/1999")]
            ex, page, ok = await run_search_select(browser, rows, make_input())
            r.check("selection succeeded", ok is True, ex._pending.get("message", ""))
            r.check("opened the DOB-matching chart (Zzid2)", "Zzid2" in (ex._chart_url or ""),
                    ex._chart_url)
            await page.close()

            print("\n[I] Several results NOT narrowing -> multiple_candidates, no chart opened")
            rows = [("Zzid1", f"{FIRST} {LAST}", DOB_SHORT), ("Zzid2", f"{FIRST} {LAST}", DOB_SHORT)]
            ex, page, ok = await run_search_select(browser, rows, make_input())
            r.check("refused", ok is False)
            r.check("reason is multiple_candidates",
                    ex._pending.get("reason") == "multiple_candidates", ex._pending.get("reason"))
            r.check("no chart opened", ex._chart_url is None)
            await page.close()

            print("\n[J] Fifteen results -> refuses WITHOUT opening a chart")
            rows = [(f"Zzid{i}", f"{FIRST} {LAST}", DOB_SHORT if i == 0 else "1/1/1971")
                    for i in range(15)]
            ex, page, ok = await run_search_select(browser, rows, make_input())
            r.check("refused despite a unique DOB match being present", ok is False)
            r.check("reason is result_set_possibly_truncated",
                    ex._pending.get("reason") == "result_set_possibly_truncated",
                    ex._pending.get("reason"))
            r.check("no chart opened", ex._chart_url is None)
            r.check("names the threshold", "15" in ex._pending.get("message", ""))
            await page.close()

            print("\n[K] Name matches but date of birth does not -> patient_not_found at select")
            rows = [("Zzid1", f"{FIRST} {LAST}", "7/7/1977")]
            ex, page, ok = await run_search_select(browser, rows, make_input())
            r.check("refused", ok is False)
            r.check("says the name matched but the date did not",
                    "matched the name" in ex._pending.get("message", ""),
                    ex._pending.get("message", "")[:120])
            r.check("no chart opened", ex._chart_url is None)
            await page.close()

            print("\n[L] Sentinel — no patient value in any log line or refusal message")
            cap.lines.clear()
            ex, page, ok = await run_verify(browser, chart_page(name="Zzsomeone Zzelse"), make_input())
            blob = " ".join(cap.lines) + " " + ex._pending.get("message", "") + " " + \
                   " ".join(l.message for l in ex._logs)
            for tok in (FIRST, LAST, DOB_PADDED, DOB_SHORT, PHONE_IN, PHONE_CHART, CLINICIAN):
                r.check(f"'{tok}' absent", tok not in blob, blob[:180])
            await page.close()

            print("\n[O] Phone: a match against EITHER mobile or home passes")
            for label, kw, survey_phone, expect in [
                ("survey=mobile, both present", dict(phone=PHONE_CHART, home_phone=HOME_CHART),
                 PHONE_CHART, True),
                ("survey=HOME, both present",   dict(phone=PHONE_CHART, home_phone=HOME_CHART),
                 HOME_CHART, True),
                ("survey=mobile, home ABSENT",  dict(phone=PHONE_CHART, home_phone=""),
                 PHONE_CHART, True),
                ("survey=HOME, mobile ABSENT",  dict(phone="", home_phone=HOME_CHART),
                 HOME_CHART, True),
                ("survey matches neither",      dict(phone=PHONE_CHART, home_phone=HOME_CHART),
                 "505-555-0999", False),
            ]:
                ex, page, ok = await run_verify(
                    browser, chart_page(**kw), make_input(phone=survey_phone))
                r.check(f"{label} -> {'passes' if expect else 'refuses'}", ok is expect,
                        ex._pending.get("reason") or ex._pending.get("message", ""))
                if not expect:
                    r.check(f"{label}: reason is phone_mismatch",
                            ex._pending.get("reason") == "phone_mismatch", ex._pending.get("reason"))
                await page.close()

            print("\n[P] Absent is NOT a mismatch; but BOTH absent cannot be verified")
            ex, page, ok = await run_verify(
                browser, chart_page(phone="", home_phone=""), make_input())
            r.check("both empty refuses", ok is False)
            r.check("reason is field_unreadable (not phone_mismatch)",
                    ex._pending.get("reason") == "field_unreadable", ex._pending.get("reason"))
            r.check("message says neither number could be read",
                    "neither mobile nor" in ex._pending.get("message", ""),
                    ex._pending.get("message", "")[:120])
            await page.close()

            print("\n[Q] Duplicate MobilePhoneElem id — every match is read, not just the first")
            dup = chart_page(phone="", home_phone="").replace(
                '<div id="PatientInformation__HomePhoneElem"></div>',
                '<div id="PatientInformation__MobilePhoneElem"></div>'
                f'<div id="PatientInformation__MobilePhoneElem"><a href="tel:x">{PHONE_CHART}</a></div>'
                '<div id="PatientInformation__HomePhoneElem"></div>')
            ex, page, ok = await run_verify(browser, dup, make_input(phone=PHONE_CHART))
            r.check("finds the value on the SECOND duplicate id", ok is True,
                    ex._pending.get("message", ""))
            await page.close()

            print("\n[N] The search query is the SURNAME ALONE, not the full name")
            # A live run proved "First Last" can miss a patient stored as
            # "First Middle Last" while the surname alone returns them. Pin it.
            page = await browser.new_page(viewport={"width": 1400, "height": 1000})
            await page.route(f"{ORIGIN}/**", lambda r: asyncio.ensure_future(
                r.fulfill(status=200, content_type="text/html", body=results_page([]))))
            await page.goto(f"{ORIGIN}/app/patients/")
            ex = make_ex(page)
            await ex._phase_search(make_input())
            typed = await page.input_value("#ctl00_BodyContent_TextBoxSearchPatientName")
            r.check("searched on the surname alone", typed == LAST, typed)
            r.check("did NOT include the first name", FIRST not in typed, typed)
            await page.close()

            print("\n[M] Phone comparison is on digits, shape-insensitive")
            for shape in ["(505) 555-0142", "505-555-0142", "5055550142", "+1 505 555 0142"]:
                r.check(f"{shape!r} -> same digits", _digits(shape)[-10:] == "5055550142")

            await browser.close()
    finally:
        root.removeHandler(cap)

    print(f"\n{'PASS' if r.failed == 0 else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    if r.failed:
        print("\nFailures:"); [print(f"  - {l}") for l in r.lines]
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
