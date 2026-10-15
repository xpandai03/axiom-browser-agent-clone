"""
Navigation audit: no flow clicks a top-menu link (2026-10-15).

TherapyNotes' refreshed menu renders hidden duplicates of its links. On
2026-10-15 03:56Z Add to Schedule clicked the hidden Patients link and timed
out. Every flow now opens the page it needs by URL and waits for an element
only that page has (docs/selectors/tn_v2_phases.md, "2026-10-15 — navigation
audit").

Each flow's REAL navigation code runs against a mock TherapyNotes whose every
page carries the refreshed menu: a hidden duplicate first, then the visible
links, each of which records a click in sessionStorage. Per flow:
  - the destination page was requested directly (page.goto)
  - no menu link was ever clicked
  - the flow got past navigation (its loaded signal was found)

  [1] V2 Add to Schedule — navigate (Patients list)
  [2] V1 create — navigate (Patients list)
  [3] V2 Add to Schedule — scheduling phase (Scheduling page)
  [4] document upload (patient chart)
  [5] portal step (patient chart)
  [6] survey attach (Patients list)
  [7] count pass (Patients list)
  [8] nightly pull (Patients list)
  [9] selector smoke (all six signals)
 [10] a chart link reading "Patients" on the dashboard is never followed;
      the Patients list must prove itself, not the URL alone
 [11] login: a hidden marker first in page order no longer fails a loaded
      dashboard (the Oct 13 / 10-15 04:19Z false dashboard_not_loaded)

Run:  <venv>/bin/python -m tests.test_navigation_audit
"""

import asyncio
import logging
import sys
import time
from types import SimpleNamespace

from playwright.async_api import async_playwright

from services.api import selector_smoke as smoke
from services.api import tn_selectors as S
from services.api.active_count_executor import ActiveCountExecutor
from services.api.active_patients_executor import ActivePatientsExecutor
from services.api.portal_step import PortalStep
from services.api.survey_attach_executor import SurveyAttachExecutor
from services.api.tn_executor import TNExecutor
from services.api.tn_executor_v2 import TNExecutorV2
from shared.schemas.therapy_notes_v2 import TNPatientInputV2, TNPhaseV2

TN = "https://www.therapynotes.com"
CHART_PATH = "/app/patients/edit/SYNTHREC/"

# The refreshed menu, as live TherapyNotes renders it: a hidden duplicate of
# the Patients link FIRST, then the visible links. Any click is recorded.
MENU = """<tn-menu></tn-menu>
<script>
customElements.define('tn-menu', class extends HTMLElement { connectedCallback() {
  const rec = "sessionStorage.setItem('navClicked', (sessionStorage.getItem('navClicked') || '') + this.getAttribute('href') + ' ')";
  const r = this.attachShadow({mode: 'open'});
  r.innerHTML = '<a href="/app/patients/" style="display:none" onclick="' + rec + '">Patients</a> ' +
    '<a href="/app/" onclick="' + rec + '">Dashboard</a> ' +
    '<a href="/app/patients/" onclick="' + rec + '">Patients</a> ' +
    '<a href="/app/scheduling/" onclick="' + rec + '">Scheduling</a> ' +
    '<a href="/app/todo/" onclick="' + rec + '">To-Do</a>';
}});
</script>"""


def page_html(title, body):
    return f"<html><head><title>{title}</title></head><body>{MENU}{body}</body></html>"


DASHBOARD = page_html("Welcome to TherapyNotes Sample User | TherapyNotes",
                      "<h2>Welcome Message</h2><a href='/app/patients/edit/SYNTH1/'>Patients of the Sample Family</a>")

PATIENTS = page_html("Patient List | TherapyNotes", """<h1>Patients</h1>
<form method="get" action="/app/patients/">
  <select id="ctl00_BodyContent_DropDownListSearchActivity" name="act"><option value="active">Active</option></select>
  <select id="ctl00_BodyContent_DropDownListSearchAssignment" name="clin"></select>
  <input id="ctl00_BodyContent_TextBoxSearchPatientName" name="q">
  <input id="ctl00_BodyContent_ButtonSearch" type="submit" value="Search">
</form>
<input id="ctl00_BodyContent_ButtonCreatePatient1" type="submit" value="+ New Patient">
<div id="DivPatientsList"><table id="PatientSearchTableList"></table></div>""")

# Scheduling page WITHOUT the "+ New" button: the phase stops right after it
# arrived, which is all this audit needs (no patient value is ever typed).
SCHEDULING_EMPTY = page_html("Scheduling | TherapyNotes", "<div id='Calendar'></div>")

# A chart with neither a Documents nor a Portal tab: upload and portal stop
# right after arriving.
CHART_NO_TABS = page_html("SR - Patients | TherapyNotes", "<div id='PatientInformation__PatientName'>Sample Record</div>")


def serve(page, routes, requests):
    """Serve `routes` (path -> html) and record every document request's path."""
    async def handler(route):
        path = route.request.url.replace(TN, "").split("?")[0]
        if route.request.resource_type == "document":
            requests.append(path)
        body = routes.get(path, routes.get("*", DASHBOARD))
        await route.fulfill(status=200, content_type="text/html", body=body)
    return page.route(f"{TN}/**", handler)


async def nav_clicked(page) -> str:
    return await page.evaluate("() => sessionStorage.getItem('navClicked') || ''")


class Results:
    def __init__(self): self.passed = self.failed = 0; self.lines = []
    def check(self, name, cond, detail=""):
        if cond:
            self.passed += 1; print(f"  ok   {name}")
        else:
            self.failed += 1; self.lines.append(f"{name} — {detail}"); print(f"  FAIL {name} — {detail}")


def make_tn(page, cls=TNExecutorV2):
    ex = object.__new__(cls)
    ex._page = page
    ex._logs = []
    ex._surfaced_overlays = []
    ex._overlays_reported = set()
    ex._save_problem_text = None
    ex._start_time = time.time()
    ex._pending_failure = {}
    ex.password_expires_in_days = None
    ex._credentials = SimpleNamespace(username="synthetic-user", password="synthetic-pass")
    ex.STEP_TIMEOUT_MS = 3000
    async def no_shot(*a, **k):
        return None
    ex._capture_screenshot = no_shot
    return ex


def synthetic_patient():
    return TNPatientInputV2(
        first_name="Zz", last_name="Audit", dob="01/02/1990", address="1 Zz Way",
        zip="87110", sex="Male", email="zz@example.test", phone="5055550100",
        rfs_url="", intake_pdf_url="https://crm.example/i", snapshot_pdf_url="https://crm.example/s",
        appointment_date="10/20/2026", appointment_time="9:00 am", appointment_alert_text="ZZ alert",
        clinician_name="Zz Clinician",
    )


async def fresh(browser, routes, start="/app/"):
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    requests = []
    await serve(page, routes, requests)
    await page.goto(f"{TN}{start}")
    requests.clear()
    return page, requests


async def main():
    r = Results()
    logging.getLogger().setLevel(logging.WARNING)
    routes = {"/app/": DASHBOARD, "/app/patients/": PATIENTS, "/app/scheduling/": SCHEDULING_EMPTY,
              CHART_PATH: CHART_NO_TABS}

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        print("\n[1] V2 navigate")
        page, req = await fresh(browser, routes)
        ex = make_tn(page)
        ok = await ex._phase_navigate()
        r.check("reaches the Patients list", ok is True, ex._pending_failure)
        r.check("requested /app/patients/ directly", "/app/patients/" in req, req)
        r.check("no menu link clicked", await nav_clicked(page) == "", await nav_clicked(page))
        await page.close()

        print("\n[2] V1 navigate")
        page, req = await fresh(browser, routes)
        ex = make_tn(page, TNExecutor)
        ok = await ex._phase_navigate()
        r.check("reaches the Patients list", ok is True, getattr(ex, "_pending_failure", None))
        r.check("requested /app/patients/ directly", "/app/patients/" in req, req)
        r.check("no menu link clicked", await nav_clicked(page) == "")
        await page.close()

        print("\n[3] V2 scheduling phase")
        page, req = await fresh(browser, routes)
        ex = make_tn(page)
        ex._patient = synthetic_patient()
        ok = await ex._phase_schedule_appointment(ex._patient)
        r.check("requested /app/scheduling/ directly", "/app/scheduling/" in req, req)
        r.check("stopped at the missing '+ New' (after arriving)",
                ok is False and ex._pending_failure.get("reason") == "scheduling_ui_not_found", ex._pending_failure)
        r.check("no menu link clicked", await nav_clicked(page) == "")
        await page.close()

        print("\n[4] document upload")
        page, req = await fresh(browser, routes)
        ex = make_tn(page)
        ok = await ex._upload_pdf_to_patient(
            f"{TN}{CHART_PATH}", "/nonexistent.pdf", "Synthetic Doc",
            TNPhaseV2.UPLOAD_INTAKE_PDF, "intake_pdf_upload_failed", check_existing=False)
        r.check("requested the chart URL directly", CHART_PATH in req, req)
        r.check("stopped at the missing Documents tab (after arriving)", ok is False, ex._pending_failure)
        r.check("no menu link clicked", await nav_clicked(page) == "")
        await page.close()

        print("\n[5] portal step")
        page, req = await fresh(browser, routes)
        step = PortalStep(page, f"{TN}{CHART_PATH}")
        step.WAIT_S = 2
        v = await step.run("Individual", None, False, True)
        r.check("requested the chart URL directly", CHART_PATH in req, req)
        r.check("stopped at the missing Portal tab", v.get("portalReason") == "portal_tab_not_found"
                or v.get("reason") == "portal_tab_not_found", v)
        r.check("no menu link clicked", await nav_clicked(page) == "")
        await page.close()

        print("\n[6] survey attach")
        page, req = await fresh(browser, routes)
        ex = object.__new__(SurveyAttachExecutor)
        ex._page = page; ex._logs = []; ex._pending = {}; ex._start_time = 0.0
        ex._chart_url = None; ex._row_count = 0; ex._mech = None
        ex._pages = []; ex._truncated = False; ex._current_page = 0; ex._selection_mode = None
        missing = await ex._open_patients_page()
        r.check("requested /app/patients/ directly", "/app/patients/" in req, req)
        r.check("found its search controls", missing is None or "search controls" not in str(missing), missing)
        r.check("no menu link clicked", await nav_clicked(page) == "")
        await page.close()

        for label, cls, run in (("[7] count pass", ActiveCountExecutor, "execute"),
                                ("[8] nightly pull", ActivePatientsExecutor, "execute_patients")):
            print(f"\n{label}")
            page, req = await fresh(browser, routes)
            runtime = SimpleNamespace(ensure_browser=None)
            async def ensure(page=page):
                return page
            runtime.ensure_browser = ensure
            orig = (TNExecutorV2._phase_entry, TNExecutorV2._phase_login)
            async def yes(self):
                return True
            TNExecutorV2._phase_entry = yes
            TNExecutorV2._phase_login = yes
            try:
                exe = cls(runtime, SimpleNamespace(username="u", password="p"))
                exe.STEP_TIMEOUT_MS = 5000
                out = await getattr(exe, run)()
            finally:
                TNExecutorV2._phase_entry, TNExecutorV2._phase_login = orig
            r.check("requested /app/patients/ directly", "/app/patients/" in req, req)
            r.check("got past navigation (the mock has no clinicians)",
                    out.failure_reason == "no_clinicians_found", out.failure_reason)
            r.check("no menu link clicked", await nav_clicked(page) == "")
            await page.close()

        print("\n[9] selector smoke: six signals, no menu click")
        smoke_routes = dict(routes)
        smoke_routes["/app/patients/edit/"] = page_html("New Patient | TherapyNotes",
            "<input id='PatientInformationEditor__FirstNameInput'>")
        smoke_routes[CHART_PATH] = page_html("TA - Patients | TherapyNotes",
            "<div id='PatientInformation__PatientName'>x</div><a href='#tab=Clinicians'>Clinicians</a>")
        smoke_routes["/app/scheduling/"] = page_html("Scheduling | TherapyNotes", """
<psy-button id="ButtonCreateAppointment" onclick="document.getElementById('dlg').style.display='block'">+ New</psy-button>
<div id="dlg" style="display:none"><input id="CalendarEntryEditor__PatientSelect">
<button class="DialogCloseButton" onclick="document.getElementById('dlg').style.display='none'">x</button></div>""")
        smoke_routes["/app/patients/"] = PATIENTS.replace(
            '<input id="ctl00_BodyContent_ButtonCreatePatient1" type="submit" value="+ New Patient">',
            '<input id="ctl00_BodyContent_ButtonCreatePatient1" type="button" value="+ New Patient" '
            'onclick="location.href=\'/app/patients/edit/\'">').replace(
            '<select id="ctl00_BodyContent_DropDownListSearchAssignment" name="clin"></select>',
            '<select id="ctl00_BodyContent_DropDownListSearchAssignment" name="clin"><option value="any">Any</option></select>').replace(
            '<table id="PatientSearchTableList"></table>',
            '<table id="PatientSearchTableList"><tr><td><a data-testid="patient-search-patient-link" '
            f'href="{CHART_PATH}">Test, Anna</a></td></tr></table>')
        page, req = await fresh(browser, smoke_routes)
        ex = make_tn(page)
        async def ok_():
            return True
        ex._phase_entry = ok_
        ex._phase_login = ok_
        res = await smoke.check_signals(ex, page)
        r.check("six signals pass", res.status == "pass" and len(res.signals) == 6
                and all(res.signals.values()), res.signals)
        r.check("message 'TN layout ok (6/6)'", res.message == "TN layout ok (6/6)", res.message)
        r.check("requested /app/patients/ and /app/scheduling/ directly",
                "/app/patients/" in req and "/app/scheduling/" in req, req)
        r.check("no menu link clicked", await nav_clicked(page) == "")
        await page.close()

        print("\n[10] the Patients list must prove itself")
        page, req = await fresh(browser, {"*": DASHBOARD})  # every URL answers with the dashboard
        ex = make_tn(page)
        ok = await ex._phase_navigate()
        r.check("navigate fails when /app/patients/ is not the list", ok is False)
        r.check("reason navigation_failed, landing named",
                ex._pending_failure.get("reason") == "navigation_failed"
                and "landed on" in ex._pending_failure.get("message", ""), ex._pending_failure)
        r.check("the dashboard's chart link was not followed", CHART_PATH not in req
                and "/app/patients/edit/SYNTH1/" not in req, req)
        r.check("no menu link clicked", await nav_clicked(page) == "")
        await page.close()

        print("\n[11] login: a hidden marker first does not fail a loaded dashboard")
        login_form = """<html><head><title>Log In | TherapyNotes</title></head><body>
<form onsubmit="event.preventDefault(); document.open(); document.write(window.__next); document.close();">
<input id="Login__UsernameField"><input id="Login__Password" type="password">
<button id="Login__LogInButton" type="submit">Log In</button></form></body></html>"""
        for label, cls in (("V2", TNExecutorV2), ("V1", TNExecutor)):
            page, req = await fresh(browser, {"*": login_form}, start="/app/login/")
            await page.evaluate("html => { window.__next = html; }", DASHBOARD)
            ex = make_tn(page, cls)
            t0 = time.time()
            ok = await ex._phase_login()
            r.check(f"{label}: login passes", ok is True, getattr(ex, "_pending_failure", None))
            r.check(f"{label}: within seconds, not the 30s timeout", time.time() - t0 < 10, time.time() - t0)
            await page.close()

        await browser.close()

    print(f"\n{'PASS' if not r.failed else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    for l in r.lines:
        print("  - " + l)
    return 0 if not r.failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
