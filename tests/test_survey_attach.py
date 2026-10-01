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

from services.api.survey_attach_executor import (
    SELECTORS_ATTACH,
    SurveyAttachExecutor,
    _digits,
)
from shared.schemas.survey_attach import SurveyAttachInput

ANCHOR_SEL = SELECTORS_ATTACH["patients_page_result_anchor"][0]

PID = "ZzTestChartId0001"
FIRST, LAST = "Zzmary", "Zzwatson"
DOB_PADDED = "01/02/1990"       # what the survey/payload carries
DOB_SHORT = "1/2/1990"          # what the chart renders (8 chars, unpadded)
PHONE_IN = "(505) 555-0142"
PHONE_CHART = "505-555-0142"
HOME_CHART = "505-555-0777"
CLINICIAN = "Zzamanda Zzdavison"
OTHER_CLIN = "Zzother Zzclinician"


def results_page(rows, next_href=None, activity="inactive", assignment="zzclin"):
    """
    rows: list of (pid, name, dob_text) or (pid, name, dob_text, trailing_cells).

    ROW CLASSES ALTERNATE, exactly as TherapyNotes renders them. This is the
    whole point of the fixture: `#PatientSearchTableList tr.Row` matches only the
    odd-indexed half, so a target on an EVEN row is invisible to anything that
    iterates that selector. A fixture that gave every row class="Row" would have
    passed while the shipped code could not find half the table — which is what
    happened on 21 September.

    `trailing_cells` is free text dropped into a further cell (the real table
    carries Payer and Clinicians columns). It exists so a decoy row can carry the
    survey's name tokens OUTSIDE the name cell.
    """
    trs = ""
    for i, row in enumerate(rows):
        p, n, d = row[0], row[1], row[2]
        trailing = row[3] if len(row) > 3 else ""
        cls = "Row" if i % 2 == 0 else "AlternateRow"
        trs += (
            f'<tr class="{cls}"><td></td>'
            f'<td><a data-testid="patient-search-patient-link" href="/app/patients/edit/{p}/">{n}</a></td>'
            f'<td><a data-testid="patient-search-dob-link" href="/app/patients/edit/{p}/">{d}</a></td>'
            f"<td>{trailing}</td>"
            f"</tr>"
        )
    # THE PAGE AROUND THE TABLE, as TherapyNotes serves it: a WebForms form whose
    # Search submit and pager are real navigations, Activity and Assigned-To
    # filters, and the results container the nightly pull waits on. The filters'
    # defaults are deliberately NOT the ones attach needs, so a run that does not
    # set them sees no rows (see serve_search).
    def opt(v, cur):
        return f'<option value="{v}"{" selected" if v == cur else ""}>{v}</option>'
    pager = (f'<a id="DynamicTablePagingLink" class="Next" href="{next_href}">Next</a>'
             if next_href else "")
    return f"""<html><body>
      <form method="get" action="/app/patients/">
        <select id="ctl00_BodyContent_DropDownListSearchActivity" name="activity">
          {opt("inactive", activity)}{opt("active", activity)}{opt("all", activity)}</select>
        <select id="ctl00_BodyContent_DropDownListSearchAssignment" name="assignment">
          {opt("zzclin", assignment)}{opt("any", assignment)}</select>
        <input id="ctl00_BodyContent_TextBoxSearchPatientName" name="q">
        <input type="submit" id="ctl00_BodyContent_ButtonSearch" value="Search">
      </form>
      <div id="DivPatientsList"><table id="PatientSearchTableList">{trs}</table>{pager}</div>
    </body></html>"""


def serve_search(rows, served, delay_s=0.0, page_size=20):
    """
    A route handler that behaves like the Patients page.

    Before a search: no rows. A search whose filters are not Activity=active and
    Assigned To=any: no rows either, which is how a run that skips the filters
    shows up. Otherwise `rows` paged at `page_size`, with a Next link while more
    remain. `delay_s` holds every search response back, to prove the reload is
    waited for rather than slept past. Every query served is appended to `served`.
    """
    from urllib.parse import urlparse, parse_qs, urlencode

    async def handler(route, request):
        u = urlparse(request.url)
        if "/patients/edit/" in u.path:
            return await route.fulfill(status=200, content_type="text/html", body=chart_page())
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if "q" not in q:
            return await route.fulfill(status=200, content_type="text/html", body=results_page([]))
        served.append(q)
        if delay_s:
            await asyncio.sleep(delay_s)
        act, asg = q.get("activity"), q.get("assignment")
        hits = rows if (act == "active" and asg == "any") else []
        n = int(q.get("page", "1"))
        chunk = hits[(n - 1) * page_size: n * page_size]
        nxt = None
        if n * page_size < len(hits):
            nxt = "/app/patients/?" + urlencode({**q, "page": n + 1})
        await route.fulfill(status=200, content_type="text/html",
                            body=results_page(chunk, nxt, act or "inactive", asg or "zzclin"))
    return handler


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
    ex._pages = []; ex._truncated = False; ex._current_page = 0; ex._selection_mode = None
    return ex


ORIGIN = "https://www.therapynotes.com"


async def run_search_select(browser, rows, data, delay_s=0.0):
    """
    Drive the REAL search and then select, against a Patients page SERVED ON
    TN'S ORIGIN (see serve_search), so the form submit and the pager are real
    navigations and clicking a result lands on a chart URL. The queries the page
    received are on ex._served.
    """
    page = await browser.new_page(viewport={"width": 1400, "height": 1000})
    served = []
    await page.route(f"{ORIGIN}/**", serve_search(rows, served, delay_s))
    await page.goto(f"{ORIGIN}/app/patients/")
    ex = make_ex(page)
    ex._served = served
    ok = await ex._phase_search(data) and await ex._phase_select(data)
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

            print("\n[J] A FULL page and NO further page -> proceeds (the pager decides, not the count)")
            PS = SurveyAttachExecutor.PAGE_SIZE
            r.check("page size is the measured 20", PS == 20, PS)
            rows = [(f"Zzid{i}", f"{FIRST} {LAST}", DOB_SHORT if i == 0 else "1/1/1971")
                    for i in range(PS)]
            ex, page, ok = await run_search_select(browser, rows, make_input())
            r.check("a complete page of 20 proceeds", ok is True, ex._pending.get("message", ""))
            r.check("opened the DOB-matching chart", "Zzid0" in (ex._chart_url or ""), ex._chart_url)
            await page.close()

            print("\n[J2] Three pages, the target on the LAST page -> found")
            rows = [(f"Zzid{i}", f"{FIRST} {LAST}", DOB_SHORT if i == 44 else "1/1/1971")
                    for i in range(45)]
            ex, page, ok = await run_search_select(browser, rows, make_input())
            r.check("found on page 3", ok is True, ex._pending.get("message", ""))
            r.check("opened Zzid44", "Zzid44/" in (ex._chart_url or ""), ex._chart_url)
            r.check("read all three pages", len(ex._pages) == 3, len(ex._pages))
            await page.close()

            print("\n[J3] Three pages, the target on the FIRST page -> returns to it and opens it")
            rows = [(f"Zzid{i}", f"{FIRST} {LAST}", DOB_SHORT if i == 3 else "1/1/1971")
                    for i in range(45)]
            ex, page, ok = await run_search_select(browser, rows, make_input())
            r.check("found on page 1 after reading page 3", ok is True, ex._pending.get("message", ""))
            r.check("opened Zzid3", "Zzid3/" in (ex._chart_url or ""), ex._chart_url)
            r.check("searched again to get back to page 1", len(ex._served) >= 4, len(ex._served))
            await page.close()

            print("\n[J4] More pages than the cap -> result_set_possibly_truncated, no chart opened")
            cap_rows = SurveyAttachExecutor.MAX_SEARCH_PAGES * PS + 1
            rows = [(f"Zzid{i}", f"{FIRST} {LAST}", DOB_SHORT if i == 0 else "1/1/1971")
                    for i in range(cap_rows)]
            ex, page, ok = await run_search_select(browser, rows, make_input())
            r.check("refused even with a unique match on page 1", ok is False)
            r.check("reason is result_set_possibly_truncated",
                    ex._pending.get("reason") == "result_set_possibly_truncated",
                    ex._pending.get("reason"))
            r.check("read exactly the cap", len(ex._pages) == SurveyAttachExecutor.MAX_SEARCH_PAGES,
                    len(ex._pages))
            r.check("no chart opened", ex._chart_url is None)
            await page.close()

            print("\n[J5] The same name and DOB on page 1 AND page 3 -> multiple_candidates")
            rows = [(f"Zzid{i}", f"{FIRST} {LAST}", DOB_SHORT if i in (2, 41) else "1/1/1971")
                    for i in range(45)]
            ex, page, ok = await run_search_select(browser, rows, make_input())
            r.check("refused", ok is False)
            r.check("reason is multiple_candidates",
                    ex._pending.get("reason") == "multiple_candidates", ex._pending.get("reason"))
            r.check("no chart opened", ex._chart_url is None)
            await page.close()

            print("\n[J6] A search that takes 5s to come back -> still found")
            # Tolerance of a slow server, not proof of a fixed race: Playwright's
            # click already waits for the postback to commit, so a fixed sleep
            # passed this too. The wait is the nightly pull's (navigation, then
            # the results container) so the two read the page the same way.
            ex, page, ok = await run_search_select(
                browser, [("Zzid1", f"{FIRST} {LAST}", DOB_SHORT)], make_input(), delay_s=5.0)
            r.check("waited for the reload and found the patient", ok is True,
                    ex._pending.get("reason") or ex._pending.get("message", ""))
            await page.close()

            print("\n[J7] The filters are SET, not inherited from the page")
            ex, page, ok = await run_search_select(
                browser, [("Zzid1", f"{FIRST} {LAST}", DOB_SHORT)], make_input())
            first = ex._served[0] if ex._served else {}
            r.check("Activity submitted as active", first.get("activity") == "active", first.get("activity"))
            r.check("Assigned To submitted as any", first.get("assignment") == "any", first.get("assignment"))
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
            # "First Middle Last" while the surname alone returns them. Pin it,
            # on what the page actually RECEIVED.
            ex, page, ok = await run_search_select(browser, [], make_input())
            typed = (ex._served[0] if ex._served else {}).get("q")
            r.check("searched on the surname alone", typed == LAST, typed)
            r.check("did NOT include the first name", FIRST not in (typed or ""), typed)
            await page.close()

            print("\n[M] Phone comparison is on digits, shape-insensitive")
            for shape in ["(505) 555-0142", "505-555-0142", "5055550142", "+1 505 555 0142"]:
                r.check(f"{shape!r} -> same digits", _digits(shape)[-10:] == "5055550142")

            # ==============================================================
            # THE 21 SEPTEMBER FAILURES. Two independent bugs, one symptom.
            # ==============================================================

            print("\n[R] NINE results, target on an EVEN row -> found (was invisible)")
            # Nine rows with alternating classes: tr.Row matches five of them and
            # the target sits on index 7, which is an AlternateRow. This is the
            # shape of the live search that refused.
            rows = [(f"Zzid{i}", f"{FIRST} {LAST}",
                     DOB_SHORT if i == 7 else f"{(i % 12) + 1}/1/1971")
                    for i in range(9)]
            page_probe = await browser.new_page()
            await page_probe.set_content(results_page(rows))
            n_anchor = await page_probe.locator(ANCHOR_SEL).count()
            n_trrow = await page_probe.locator("#PatientSearchTableList tr.Row").count()
            await page_probe.close()
            r.check("the fixture reproduces the halving: 9 anchors, 5 tr.Row",
                    (n_anchor, n_trrow) == (9, 5), (n_anchor, n_trrow))
            ex, page, ok = await run_search_select(browser, rows, make_input())
            r.check("selection succeeded on an EVEN row", ok is True,
                    ex._pending.get("message", ""))
            r.check("opened the right chart (Zzid7)", "Zzid7" in (ex._chart_url or ""),
                    ex._chart_url)
            await page.close()

            print("\n[R2] A chart id naming a DIFFERENT row is ignored -> name + DOB decides")
            # TherapyNotes record ids change between page loads, so the CRM's id
            # can never be trusted to name a row. Here it names a decoy.
            ex, page, ok = await run_search_select(
                browser, rows, make_input(expected_chart_id="Zzid3"))
            r.check("selection succeeded", ok is True, ex._pending.get("message", ""))
            r.check("opened the name+DOB row Zzid7, not the id's Zzid3",
                    "Zzid7/" in (ex._chart_url or ""), ex._chart_url)
            r.check("recorded as name selection", ex._selection_mode == "name")
            await page.close()

            print("\n[S] Preferred (Legal) Last -> matches on the legal name")
            # What TherapyNotes renders for a child the practice flags as a minor.
            PREF = f"Minor ({FIRST}) {LAST}"
            ex, page, ok = await run_search_select(
                browser, [("Zzid1", PREF, DOB_SHORT)], make_input())
            r.check("results-table name cell matches", ok is True,
                    ex._pending.get("message", ""))
            await page.close()
            ex, page, ok = await run_verify(browser, chart_page(name=PREF), make_input())
            r.check("chart header matches too", ok is True, ex._pending.get("message", ""))
            await page.close()

            print("\n[T] Name tokens OUTSIDE the name cell no longer match")
            # A decoy whose own name is someone else, but whose clinician column
            # carries the survey's name tokens. The old whole-row subset passed it.
            decoy = ("Zzid9", "Zzother Zzperson", DOB_SHORT, f"{FIRST} {LAST}")
            ex, page, ok = await run_search_select(browser, [decoy], make_input())
            r.check("refused", ok is False)
            r.check("reason is patient_not_found",
                    ex._pending.get("reason") == "patient_not_found", ex._pending.get("reason"))
            r.check("no chart opened", ex._chart_url is None)
            await page.close()

            print("\n[U] A hyphenated surname is a different person")
            for where, kw in [("chart header", dict(name=f"{FIRST} {LAST}-Zzsmith"))]:
                ex, page, ok = await run_verify(browser, chart_page(**kw), make_input())
                r.check(f"{where}: refused", ok is False)
                r.check(f"{where}: reason is name_mismatch",
                        ex._pending.get("reason") == "name_mismatch", ex._pending.get("reason"))
                await page.close()
            ex, page, ok = await run_search_select(
                browser, [("Zzid1", f"{FIRST} {LAST}-Zzsmith", DOB_SHORT)], make_input())
            r.check("results table: refused", ok is False)
            r.check("no chart opened", ex._chart_url is None)
            await page.close()

            print("\n[V] A middle name on the chart only -> now refuses (NEW)")
            # Stated as a new refusal rather than discovered as one: the old
            # subset test passed this, the CRM's matcher never did, and the two
            # now agree. Staff see patient_not_found / name_mismatch.
            ex, page, ok = await run_verify(
                browser, chart_page(name=f"{FIRST} Zzmiddle {LAST}"), make_input())
            r.check("chart header refuses on an untyped middle name", ok is False)
            r.check("reason is name_mismatch",
                    ex._pending.get("reason") == "name_mismatch", ex._pending.get("reason"))
            await page.close()

            print("\n[W] tr.Row is absent from the attach route")
            src = open("services/api/survey_attach_executor.py", encoding="utf-8").read()
            code = "\n".join(
                l for l in src.splitlines() if not l.lstrip().startswith("#"))
            r.check("no tr.Row in executable code", "tr.Row" not in code)
            r.check("rows are located by the result anchor",
                    "patient-search-patient-link" in ANCHOR_SEL)

            await browser.close()
    finally:
        root.removeHandler(cap)

    print(f"\n{'PASS' if r.failed == 0 else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    if r.failed:
        print("\nFailures:"); [print(f"  - {l}") for l in r.lines]
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
