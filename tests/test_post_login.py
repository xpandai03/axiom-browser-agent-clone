"""
After the password is accepted: what the agent does with the page it lands on.

13-14 October 2026. TherapyNotes refreshed its navigation and, the same day, the
agent's password reached its 120-day expiry. Every flow stopped, and staff saw
four unrelated-looking codes. These fixtures pin the fixes (mock TherapyNotes
pages built from the 2026-10-14 recon and the logged page text; all names
synthetic):

  [A] pure rules: TherapyNotes' expired / expiring wording, the Patients-list
      href guard, the plain-close label rule
  [B] the forced password-change page keeps its menu: login FAILS with
      password_expired instead of passing and failing a phase later
  [C] the refreshed dashboard (/app/, links outside the old sidebar) logs in
  [D] "will expire" notice: login passes and records the days left
  [E] a "What's New" notice is closed through its plain close control only;
      its settings-style button is never pressed
  [F] a notice with no plain close control is left open, nothing pressed
  [G] navigate opens the Patients list by URL (no menu click, so the hidden
      duplicate link of 2026-10-15 03:56Z is irrelevant); a chart link reading
      "Patients" is never clicked
  [H] the selector smoke: six signals pass on the mock practice; a missing
      chart tab reads "TN layout changed"; an expired password says so; a
      "+ New" that opens no dialog fails at scheduling_dialog
  [I] the smoke's daily schedule is 03:00 America/Denver across DST

Run:  <venv>/bin/python -m tests.test_post_login
"""

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from playwright.async_api import async_playwright

from services.api import tn_selectors as S
from services.api import selector_smoke as smoke
from services.api.tn_executor_v2 import TNExecutorV2

TN = "https://www.therapynotes.com"

# --- fixture pages ----------------------------------------------------------

LOGIN_FORM = """<html><head><title>Log In | TherapyNotes</title></head><body>
<form onsubmit="event.preventDefault(); window.__land();">
  <input id="Login__UsernameField" type="text"><input id="Login__Password" type="password">
  <button id="Login__LogInButton" type="submit">Log In</button>
</form>
<script>window.__land = () => { document.open(); document.write(window.__next); document.close(); };</script>
</body></html>"""

# The refreshed menu: links live in a web component, not in a light-DOM sidebar.
MENU = """<nav><a href="#main">Skip to Main Content</a></nav>
<tn-menu></tn-menu>
<script>
customElements.define('tn-menu', class extends HTMLElement { connectedCallback() {
  const r = this.attachShadow({mode: 'open'});
  // Live TN (2026-10-15) also renders a HIDDEN a[href='/app/patients/'] first.
  r.innerHTML = '<a href="/app/patients/" style="display:none">Patients</a> ' +
                '<a href="/app/">Dashboard</a> <a href="/app/patients/">Patients</a> ' +
                '<a href="/app/scheduling/">Scheduling</a> <a href="/app/todo/">To-Do</a>';
}});
</script>"""

DASHBOARD = f"""<html><head><title>Welcome to TherapyNotes Sample User | TherapyNotes</title></head><body>
{MENU}
<h2>The Family Connection Welcome Message</h2>
<div class="recent"><a href="/app/patients/edit/SYNTH1/">Sample Family</a></div>
{{extra}}
<script>window.__clicked = [];
document.addEventListener('click', e => {{ const b = e.target.closest('button,a'); if (b) window.__clicked.push((b.innerText || b.getAttribute('aria-label') || '').trim()); }}, true);</script>
</body></html>"""

# Logged on 2026-10-13 20:07Z (UI text only).
EXPIRED = f"""<html><head><title>Log In | TherapyNotes</title></head><body>
{MENU}
<h1>Settings: Change Your Password</h1>
<p>Your password has been in use for more than 120 days, and has expired. Please provide a new password.</p>
<label>Current Password: <input type="password" id="CurrentPassword"></label>
<label>New Password: <input type="password" id="NewPassword"></label>
<label>Confirm Password: <input type="password" id="ConfirmPassword"></label>
<button>Change Password</button>
</body></html>"""

EXPIRING = ("<div class='banner'><b>Your Password Will Expire Soon</b> Your password will expire "
            "in less than 1 day on 10/13. Create a new password before it expires.</div>")

WHATS_NEW = """<div role="dialog" aria-modal="true" id="wn" style="position:fixed;inset:20%;background:#fff">
  <h2>What's New: A Better Way to Navigate</h2>
  <p>TherapyNotes® navigation has been refreshed with more workspace.</p>
  <button onclick="window.__setting = true">Switch to classic navigation</button>
  <button aria-label="Close" onclick="document.getElementById('wn').remove()">×</button>
</div>"""

NOTICE_NO_CLOSE = """<div role="dialog" aria-modal="true" id="nc" style="position:fixed;inset:20%;background:#fff">
  <h2>What's New: A Better Way to Navigate</h2>
  <button onclick="window.__setting = true">Update my settings</button>
</div>"""

# Dashboard where the only link reading "Patients" goes into one chart.
CHART_LINK_ONLY = f"""<html><head><title>Welcome to TherapyNotes | TherapyNotes</title></head><body>
<nav><a href="/app/todo/">To-Do</a></nav>
<a href="/app/patients/edit/SYNTH2/" onclick="window.__chart = true">Patients of the Sample Family</a>
</body></html>"""


def patients_page(rows_html: str) -> str:
    return f"""<html><head><title>Patient List | TherapyNotes</title></head><body>
{MENU}<h1>Patients</h1>
<form method="get" action="/app/patients/">
  <select id="ctl00_BodyContent_DropDownListSearchActivity" name="act"><option value="all">All</option><option value="active">Active</option></select>
  <select id="ctl00_BodyContent_DropDownListSearchAssignment" name="clin"><option value="x">Sample Clinician</option><option value="any">Any Clinician</option></select>
  <input id="ctl00_BodyContent_TextBoxSearchPatientName" name="q">
  <input id="ctl00_BodyContent_ButtonSearch" type="submit" value="Search">
</form>
<input id="ctl00_BodyContent_ButtonCreatePatient1" type="submit" value="+ New Patient"
       onclick="location.href='/app/patients/edit/'">
<div id="DivPatientsList"><table id="PatientSearchTableList">{rows_html}</table></div>
</body></html>"""


RESULT_ROWS = """<tr class="Row"><td><a data-testid="patient-search-patient-link" href="/app/patients/edit/DECOY/">Testerson, Annabel</a></td></tr>
<tr class="AlternateRow"><td><a data-testid="patient-search-patient-link" href="/app/patients/edit/TESTREC/">Test, Anna</a></td></tr>"""


def chart_page(with_clinicians_tab: bool = True) -> str:
    tab = "<a href='#tab=Clinicians'>Clinicians</a>" if with_clinicians_tab else ""
    return f"""<html><head><title>TA - Patients | TherapyNotes</title></head><body>{MENU}
<div id="PatientInformation__PatientName">Anna Test</div>
<a href='#tab=Patient+Info'>Patient Info</a> <a href='#tab=Documents'>Documents</a> {tab}
</body></html>"""


NEW_PATIENT_FORM = f"""<html><head><title>New Patient | TherapyNotes</title></head><body>{MENU}
<input id="PatientInformationEditor__FirstNameInput"><input id="PatientInformationEditor__LastNameInput">
<psy-button class="button-save">Save New Patient</psy-button></body></html>"""


SCHEDULING = f"""<html><head><title>Scheduling | TherapyNotes</title></head><body>{MENU}
<psy-button id="ButtonCreateAppointment" onclick="document.getElementById('dlg').style.display='block'">+ New</psy-button>
<div id="dlg" style="display:none"><input id="CalendarEntryEditor__PatientSelect">
<button class="DialogCloseButton" onclick="document.getElementById('dlg').style.display='none'">x</button></div>
</body></html>"""

SCHEDULING_NO_DIALOG = f"""<html><head><title>Scheduling | TherapyNotes</title></head><body>{MENU}
<psy-button id="ButtonCreateAppointment">+ New</psy-button></body></html>"""


# --- harness -----------------------------------------------------------------

class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(); self.lines = []
    def emit(self, r):
        try: self.lines.append(r.getMessage())
        except Exception: self.lines.append("<?>")


class Results:
    def __init__(self): self.passed = self.failed = 0; self.lines = []
    def check(self, name, cond, detail=""):
        if cond:
            self.passed += 1; print(f"  ok   {name}")
        else:
            self.failed += 1; self.lines.append(f"{name} — {detail}"); print(f"  FAIL {name} — {detail}")


def make_ex(page):
    ex = object.__new__(TNExecutorV2)
    ex._page = page
    ex._logs = []
    ex._surfaced_overlays = []
    ex._overlays_reported = set()
    ex._save_problem_text = None
    ex._start_time = time.time()
    ex._pending_failure = {}
    ex.password_expires_in_days = None
    ex._credentials = SimpleNamespace(username="synthetic-user", password="synthetic-pass")
    async def no_shot(*a, **k):
        return None
    ex._capture_screenshot = no_shot
    return ex


async def login_onto(browser, landing_html):
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await page.route(f"{TN}/**", lambda route: route.fulfill(status=200, content_type="text/html", body=LOGIN_FORM))
    await page.goto(f"{TN}/app/login/")
    await page.evaluate("html => { window.__next = html; }", landing_html)
    ex = make_ex(page)
    ok = await ex._phase_login()
    return ex, page, ok


def serve_practice(page, chart_html, scheduling_html=None):
    scheduling_html = scheduling_html or SCHEDULING
    async def handler(route):
        url = route.request.url
        path = url.replace(TN, "")
        if path.startswith("/app/patients/edit/TESTREC") or path.startswith("/app/patients/edit/DECOY"):
            body = chart_html
        elif path.rstrip("/").endswith("/app/patients/edit") or path == "/app/patients/edit/":
            body = NEW_PATIENT_FORM
        elif path.startswith("/app/patients/"):
            body = patients_page(RESULT_ROWS if "q=" in path else "")
        elif path.startswith("/app/scheduling/"):
            body = scheduling_html
        else:
            body = DASHBOARD.replace("{extra}", "")
        await route.fulfill(status=200, content_type="text/html", body=body)
    return page.route(f"{TN}/**", handler)


async def run_smoke(browser, chart_html, login_ok=True, login_reason=None, scheduling_html=None):
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await serve_practice(page, chart_html, scheduling_html)
    await page.goto(f"{TN}/app/")
    ex = make_ex(page)

    async def ok():
        return True

    async def login():
        if not login_ok:
            ex._pending_failure = {"reason": login_reason, "message": "x"}
        return login_ok

    ex._phase_entry = ok
    ex._phase_login = login
    result = await smoke.check_signals(ex, page)
    await page.close()
    return result


async def main():
    r = Results()
    cap = _Cap(); root = logging.getLogger(); root.addHandler(cap); root.setLevel(logging.INFO)

    print("\n[A] pure rules")
    logged = ("Skip to Settings: Change Your Password Your password has been in use for more than "
              "120 days, and has expired. Please provide a new password. Current Password:")
    r.check("TN's logged wording is an expired password", S.password_expired_text(logged) is not None)
    r.check("the old phrase still counts", S.password_expired_text("Your password has expired.") is not None)
    r.check("a healthy dashboard is not", S.password_expired_text("Welcome Message Dear Team, thank you") is None)
    r.check("'will expire' is NOT expired", S.password_expired_text(EXPIRING) is None)
    r.check("'less than 1 day' -> 0 days", S.password_expires_in_days(EXPIRING) == 0,
            S.password_expires_in_days(EXPIRING))
    r.check("'in 5 days' -> 5", S.password_expires_in_days("Your password will expire in 5 days on 10/20") == 5)
    r.check("no notice -> None", S.password_expires_in_days("Dashboard") is None)
    for label, want in [("Close", True), ("×", True), ("Got it", True), ("  Not  now ", True),
                        ("Switch to classic navigation", False), ("Update my settings", False),
                        ("Change Password", False), ("Skip to Main Content", False)]:
        r.check(f"close label {label!r} is {want}", S.is_notice_close_label(label) is want)

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        print("\n[B] forced password change keeps its menu -> password_expired, not a pass")
        ex, page, ok = await login_onto(browser, EXPIRED)
        r.check("menu markers WERE visible (the 14 Oct false pass)",
                await page.locator(S.DASHBOARD_SELECTOR).filter(visible=True).count() > 0)
        r.check("login fails", ok is False)
        r.check("reason is password_expired", ex._pending_failure.get("reason") == "password_expired",
                ex._pending_failure)
        r.check("message tells the owner to set a new password",
                "set a new password" in ex._pending_failure.get("message", ""))
        r.check("no button on the password page was pressed",
                await page.evaluate("() => (window.__clicked || []).length") == 0)
        await page.close()

        print("\n[C] refreshed dashboard logs in")
        ex, page, ok = await login_onto(browser, DASHBOARD.replace("{extra}", ""))
        r.check("login passes", ok is True, ex._pending_failure)
        r.check("no expiry recorded", ex.password_expires_in_days is None)
        await page.close()

        print("\n[D] 'will expire' notice: passes, records days left, warns")
        cap.lines.clear()
        ex, page, ok = await login_onto(browser, DASHBOARD.replace("{extra}", EXPIRING))
        r.check("login passes", ok is True)
        r.check("password_expires_in_days == 0", ex.password_expires_in_days == 0, ex.password_expires_in_days)
        r.check("a warning line says so", any("expires in 0 day" in l for l in cap.lines), cap.lines[-5:])
        await page.close()

        print("\n[E] What's New closed through its plain close control only")
        cap.lines.clear()
        ex, page, ok = await login_onto(browser, DASHBOARD.replace("{extra}", WHATS_NEW))
        r.check("login passes", ok is True)
        r.check("the notice is gone", await page.locator("#wn").count() == 0)
        r.check("the settings-style button was never pressed",
                not await page.evaluate("() => !!window.__setting"))
        r.check("closed via its plain close", any("Closed notice" in l for l in cap.lines), cap.lines[-5:])
        await page.close()

        print("\n[F] notice without a plain close stays open, nothing pressed")
        cap.lines.clear()
        ex, page, ok = await login_onto(browser, DASHBOARD.replace("{extra}", NOTICE_NO_CLOSE))
        r.check("login passes", ok is True)
        r.check("the notice is still there", await page.locator("#nc").count() == 1)
        r.check("'Update my settings' never pressed", not await page.evaluate("() => !!window.__setting"))
        r.check("logged as left open", any("Notice left open" in l for l in cap.lines), cap.lines[-5:])
        await page.close()

        print("\n[G] navigate opens the Patients list by URL; a chart link reading 'Patients' is never clicked")
        page = await browser.new_page()
        await page.route(f"{TN}/**", lambda route: route.fulfill(status=200, content_type="text/html", body=CHART_LINK_ONLY))
        await page.goto(f"{TN}/app/")
        ex = make_ex(page)
        ex.STEP_TIMEOUT_MS = 1000
        ok = await ex._phase_navigate()
        r.check("navigate fails", ok is False)
        r.check("reason navigation_failed", ex._pending_failure.get("reason") == "navigation_failed", ex._pending_failure)
        r.check("the chart link was not clicked", not await page.evaluate("() => !!window.__chart"))
        await page.close()

        page = await browser.new_page()
        await serve_practice(page, chart_page())
        await page.goto(f"{TN}/app/")
        ex = make_ex(page)
        ok = await ex._phase_navigate()
        r.check("refreshed menu (hidden duplicate first): navigate reaches the Patients list by URL",
                ok is True, ex._pending_failure)
        r.check("landed on /app/patients/", page.url.rstrip("/").endswith("/app/patients"), page.url)
        await page.close()

        print("\n[H] selector smoke")
        res = await run_smoke(browser, chart_page())
        r.check("all six signals pass", res.status == "pass" and len(res.signals) == 6
                and all(res.signals.values()), res.signals)
        r.check("message 'TN layout ok (6/6)'", res.message == "TN layout ok (6/6)", res.message)
        res = await run_smoke(browser, chart_page(with_clinicians_tab=False))
        r.check("no Clinicians tab -> fail at chart_tab", res.first_failed == "chart_tab", res.signals)
        r.check("message 'TN layout changed: chart tab not found'",
                res.message == "TN layout changed: chart tab not found", res.message)
        r.check("later signal not reached (None)", res.signals["new_patient_form"] is None)
        res = await run_smoke(browser, chart_page(), login_ok=False, login_reason="password_expired")
        r.check("expired password -> dashboard fails, message says password",
                res.first_failed == "dashboard" and "password expired" in res.message, res.message)
        res = await run_smoke(browser, chart_page(), scheduling_html=SCHEDULING_NO_DIALOG)
        r.check("'+ New' opens no dialog -> fail at scheduling_dialog",
                res.first_failed == "scheduling_dialog", res.signals)

        await browser.close()

    print("\n[I] daily schedule: 03:00 America/Denver")
    h = smoke.seconds_until_next_run(datetime(2026, 10, 15, 3, 30, tzinfo=timezone.utc)) / 3600
    r.check("21:30 MDT -> 5.5h", h == 5.5, h)
    h = smoke.seconds_until_next_run(datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)) / 3600
    r.check("03:00 MST on DST-end day -> 24h (next day)", h == 24.0, h)
    h = smoke.seconds_until_next_run(datetime(2026, 11, 1, 8, 59, tzinfo=timezone.utc)) / 3600
    r.check("01:59 MST on DST-end day -> 1h01 to 03:00 MST", abs(h - (61 / 60)) < 1e-9, h)

    print(f"\n{'PASS' if not r.failed else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    for l in r.lines:
        print("  - " + l)
    return 0 if not r.failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
