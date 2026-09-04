"""
Synthetic verification for the save-verification, navigation-guard and
PHI-logging fixes.

WHY SYNTHETIC: a real refused save cannot be reproduced without driving the live
EHR, which is forbidden. These pages reconstruct the shapes from the production
evidence — a run whose save left the URL on the blank form
(.../app/patients/edit/) with the patient name absent, versus one 83 seconds
later that landed on .../app/patients/edit/<opaque-id>/ with the name present.

Pages are served through Playwright request interception on TherapyNotes' real
origin so URL semantics (pushState, path shape) behave as they do in production.
NO NETWORK REQUEST LEAVES THE MACHINE and the EHR is never contacted.

Run:  <venv>/bin/python -m tests.test_save_verification
"""

import asyncio
import logging
import sys

from playwright.async_api import async_playwright

from services.api.tn_executor_v2 import TNExecutorV2
from services.api.tn_executor import TNExecutor

ORIGIN = "https://www.therapynotes.com"
FORM_URL = f"{ORIGIN}/app/patients/edit/"
RECORD_URL = f"{ORIGIN}/app/patients/edit/zIi7AQAAAADsiX8a/"

# Synthetic. Not a real person.
FIRST, LAST = "Testfirst", "Testlast"
FULL_NAME = f"{FIRST} {LAST}"


# ---------------------------------------------------------------------------
# Page fixtures — each ends with a psy-button.button-save the phase will click
# ---------------------------------------------------------------------------

def page_html(on_save_js: str, extra_body: str = "") -> str:
    return f"""
    <html><body>
      <h1>Patient Information</h1>
      <form>
        <input id="PatientInformationEditor__FirstNameInput" value="{FIRST}">
        <input id="PatientInformationEditor__LastNameInput" value="{LAST}">
      </form>
      <div id="stage">{extra_body}</div>
      <psy-button class="button-save"
        style="display:inline-block;padding:8px 16px;border:1px solid #333;cursor:pointer"
        onclick="{on_save_js}">Save New Patient</psy-button>
    </body></html>
    """

# A save TherapyNotes REFUSES: URL never advances, an error is rendered.
# The error markup deliberately does NOT use any of the three class names the
# old probe looked for — that probe found nothing on the real refused save.
REFUSED = page_html(
    "document.getElementById('stage').innerHTML="
    "'<div class=\\'psy-field-error-text\\'>Date of Birth is not a valid date.</div>'"
)

# A save that lands: URL advances to a record and the name renders as text.
SUCCEEDED = page_html(
    f"history.pushState({{}},'','/app/patients/edit/zIi7AQAAAADsiX8a/');"
    f"document.getElementById('stage').innerHTML='<h2>{FULL_NAME}</h2>'"
)

# Disagreement A: record created, but TN renders the name in another format.
URL_NO_NAME = page_html(
    "history.pushState({},'','/app/patients/edit/zIi7AQAAAADsiX8a/');"
    "document.getElementById('stage').innerHTML='<h2>Testlast, Testfirst (M)</h2>'"
)

# Disagreement B: the name is on the page but the URL never advanced.
NAME_NO_URL = page_html(
    f"document.getElementById('stage').innerHTML='<h2>{FULL_NAME}</h2>'"
)

# A refused save with NO readable error text at all.
REFUSED_SILENT = page_html("void 0")

# Slow but successful: the record URL only appears after ~4s.
SLOW_SUCCESS = page_html(
    "setTimeout(function(){"
    "history.pushState({},'','/app/patients/edit/zIi7AQAAAADsiX8a/');"
    f"document.getElementById('stage').innerHTML='<h2>{FULL_NAME}</h2>';"
    "}, 4000)"
)

# Success PLUS an unrelated notice that happens to be class-matched as an error.
SUCCESS_WITH_NOTICE = page_html(
    f"history.pushState({{}},'','/app/patients/edit/zIi7AQAAAADsiX8a/');"
    f"document.getElementById('stage').innerHTML="
    f"'<h2>{FULL_NAME}</h2><div class=\\'alert-warning\\'>Reminder: verify insurance eligibility.</div>'"
)

# The existing duplicate path — must keep its own failure reason.
DUPLICATE = page_html(
    "document.getElementById('stage').innerHTML="
    "'<div class=\\'Dialog\\'>A patient with this name already exists.</div>'"
)


class FakePatient:
    first_name = FIRST
    last_name = LAST


class Results:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.lines = []

    def check(self, name, cond, detail=""):
        detail = "" if detail == "" else str(detail)
        if cond:
            self.passed += 1
            print(f"    ok   {name}")
        else:
            self.failed += 1
            self.lines.append(f"{name}{' — ' + detail if detail else ''}")
            print(f"    FAIL {name}{' — ' + detail if detail else ''}")


def make_executor(page, cls=TNExecutorV2):
    """Executor with only what the save/upload path touches. No creds, no config."""
    import time as _t
    ex = object.__new__(cls)
    ex._page = page
    ex._logs = []
    ex._surfaced_overlays = []
    ex._overlays_reported = set()
    ex._start_time = _t.time()
    ex._pending_failure = {}

    async def _no_screenshot(_label):  # keep the suite from writing PNGs
        return None
    ex._capture_screenshot = _no_screenshot
    return ex


async def serve(page, html_for_url):
    """Serve synthetic HTML on TN's origin. Nothing leaves the machine."""
    async def handler(route, request):
        await route.fulfill(status=200, content_type="text/html", body=html_for_url)
    await page.route(f"{ORIGIN}/**", handler)


async def run_save(browser, html, cls=TNExecutorV2):
    page = await browser.new_page(viewport={"width": 1200, "height": 900})
    await serve(page, html)
    await page.goto(FORM_URL)
    ex = make_executor(page, cls)
    ok = await ex._phase_save_patient(FakePatient())
    return ex, page, ok


async def run():
    r = Results()

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # ------------------------------------------------------------------
        print("\n[A] A refused save fails AS a save failure, carrying TN's error text")
        ex, page, ok = await run_save(browser, REFUSED)
        r.check("returns failure", ok is False)
        r.check("failure reason is save_failed",
                ex._pending_failure.get("reason") == "save_failed",
                str(ex._pending_failure.get("reason")))
        msg = ex._pending_failure.get("message", "")
        r.check("message says the save was refused", "save was refused" in msg, msg[:140])
        r.check("message carries TN's actual error text",
                "Date of Birth is not a valid date" in msg, msg[:200])
        r.check("URL stayed on the blank form", page.url.rstrip("/") == FORM_URL.rstrip("/"))
        await page.close()

        # ------------------------------------------------------------------
        print("\n[B] A refused save with NO readable error says so, rather than staying silent")
        ex, page, ok = await run_save(browser, REFUSED_SILENT)
        r.check("returns failure", ok is False)
        r.check("reason is save_failed", ex._pending_failure.get("reason") == "save_failed")
        r.check("message admits no error text could be read",
                "No error text could be read" in ex._pending_failure.get("message", ""),
                ex._pending_failure.get("message", "")[:160])
        await page.close()

        # ------------------------------------------------------------------
        print("\n[C] A successful save still succeeds")
        ex, page, ok = await run_save(browser, SUCCEEDED)
        r.check("returns success", ok is True)
        r.check("landed on a record URL", "zIi7AQAAAADsiX8a" in page.url, page.url)
        r.check("recorded a success log entry",
                any(l.status == "success" for l in ex._logs))
        await page.close()

        # ------------------------------------------------------------------
        print("\n[D] Disagreement: record URL present, name absent -> SUCCESS")
        ex, page, ok = await run_save(browser, URL_NO_NAME)
        r.check("succeeds on the authoritative URL signal", ok is True,
                str(ex._pending_failure.get("message", ""))[:140])
        await page.close()

        print("\n[E] Disagreement: name present, no record URL -> FAILURE")
        ex, page, ok = await run_save(browser, NAME_NO_URL)
        r.check("fails despite the name being present", ok is False)
        r.check("reason is save_failed", ex._pending_failure.get("reason") == "save_failed")
        r.check("message calls out the disagreement",
                "name IS on the page" in ex._pending_failure.get("message", ""),
                ex._pending_failure.get("message", "")[:200])
        await page.close()

        # ------------------------------------------------------------------
        print("\n[F] A slow save is NOT called refused while still in flight")
        ex, page, ok = await run_save(browser, SLOW_SUCCESS)
        r.check("waits for the verdict and succeeds", ok is True,
                str(ex._pending_failure.get("message", ""))[:140])
        await page.close()

        print("\n[G] A save that lands while an unrelated notice shows is a SUCCESS")
        ex, page, ok = await run_save(browser, SUCCESS_WITH_NOTICE)
        r.check("notice does not veto a created record", ok is True,
                str(ex._pending_failure.get("message", ""))[:140])
        await page.close()

        # ------------------------------------------------------------------
        print("\n[H] The duplicate-patient path is unchanged")
        ex, page, ok = await run_save(browser, DUPLICATE)
        r.check("returns failure", ok is False)
        r.check("keeps its own reason (patient_duplicate_detected)",
                ex._pending_failure.get("reason") == "patient_duplicate_detected",
                str(ex._pending_failure.get("reason")))
        r.check("message is the duplicate warning",
                "Duplicate patient warning" in ex._pending_failure.get("message", ""))
        await page.close()

        # ------------------------------------------------------------------
        print("\n[I] Record URL vs form URL — structural, not substring")
        rid = TNExecutorV2._patient_record_id_from_url
        r.check("a record URL yields its id", rid(RECORD_URL) == "zIi7AQAAAADsiX8a", str(rid(RECORD_URL)))
        r.check("the BLANK FORM URL yields None", rid(FORM_URL) is None, str(rid(FORM_URL)))
        r.check("form URL without trailing slash yields None",
                rid(FORM_URL.rstrip("/")) is None, str(rid(FORM_URL.rstrip("/"))))
        r.check("the patients list yields None", rid(f"{ORIGIN}/app/patients/") is None)
        r.check("empty/None yield None", rid("") is None and rid(None) is None)
        # The exact bug: the old substring test called the form a match for a record.
        r.check("REGRESSION: substring test would have passed here (proving the need)",
                FORM_URL.rstrip("/") in RECORD_URL and rid(FORM_URL) is None)

        print("\n[J] The upload navigation guard rejects a bare form URL")
        page = await browser.new_page(viewport={"width": 1200, "height": 900})
        await serve(page, SUCCEEDED)
        await page.goto(FORM_URL)
        ex = make_executor(page)
        from shared.schemas.therapy_notes_v2 import TNPhaseV2
        ok = await ex._upload_pdf_to_patient(
            FORM_URL, "/nonexistent.pdf", "Intake Referral",
            TNPhaseV2.UPLOAD_INTAKE_PDF, "intake_pdf_upload_failed",
        )
        r.check("refuses to upload against a blank form", ok is False)
        r.check("reason names the real cause (save_failed), not a missing tab",
                ex._pending_failure.get("reason") == "save_failed",
                str(ex._pending_failure.get("reason")))
        r.check("message says there is no saved record",
                "no saved patient record" in ex._pending_failure.get("message", ""),
                ex._pending_failure.get("message", "")[:160])
        await page.close()

        # ------------------------------------------------------------------
        print("\n[K] V1 parity — the same defect lived there")
        for label, html, expect_ok, expect_reason in [
            ("refused save", REFUSED, False, "save_failed"),
            ("successful save", SUCCEEDED, True, None),
            ("duplicate", DUPLICATE, False, "patient_duplicate_detected"),
        ]:
            ex1, page1, ok1 = await run_save(browser, html, TNExecutor)
            r.check(f"[V1] {label}: outcome", ok1 is expect_ok,
                    f"got {ok1}; {ex1._pending_failure.get('message','')[:100]}")
            if expect_reason:
                r.check(f"[V1] {label}: reason is {expect_reason}",
                        ex1._pending_failure.get("reason") == expect_reason,
                        str(ex1._pending_failure.get("reason")))
            await page1.close()
        r.check("[V1] record-URL helper mirrored",
                TNExecutor._patient_record_id_from_url(RECORD_URL) == "zIi7AQAAAADsiX8a"
                and TNExecutor._patient_record_id_from_url(FORM_URL) is None)

        await browser.close()

    # ----------------------------------------------------------------------
    print("\n[L] The 422 handler never lets a value reach a log line or the response")
    SENTINEL = "SENTINEL-PHI-0987654321"

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines = []

        def emit(self, record):
            try:
                self.lines.append(record.getMessage())
            except Exception:
                self.lines.append("<unformattable>")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from services.api.app import create_app

    cap = _Capture()
    root = logging.getLogger()
    root.addHandler(cap)
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        # A payload shaped like the real one, every value a sentinel.
        body = {
            "first_name": SENTINEL, "last_name": SENTINEL, "dob": SENTINEL,
            "address": SENTINEL, "zip": SENTINEL, "sex": SENTINEL,
            "email": SENTINEL, "phone": SENTINEL, "rfs_url": SENTINEL,
            "intake_pdf_url": SENTINEL, "snapshot_pdf_url": SENTINEL,
            "appointment_date": SENTINEL, "appointment_time": SENTINEL,
            "appointment_alert_text": SENTINEL, "appointment_modality": SENTINEL,
            "clinician_name": SENTINEL,
        }
        resp = client.post(
            "/api/tn/create-patient-with-schedule",
            json=body,
            headers={"X-API-Key": __import__("os").environ.get("TN_API_KEY", "")},
        )
        r.check("request was rejected as a validation error (422)", resp.status_code == 422,
                f"got {resp.status_code}")
        logged = "\n".join(cap.lines)
        r.check("NO sentinel value in any log line", SENTINEL not in logged,
                [l for l in cap.lines if SENTINEL in l][:1])
        r.check("NO sentinel value in the response body", SENTINEL not in resp.text,
                resp.text[:200])
        r.check("the response still names the failing fields",
                "sex" in resp.text and "dob" in resp.text, resp.text[:200])
        r.check("the response still carries the constraint (msg)",
                "Male" in resp.text or "should match pattern" in resp.text, resp.text[:250])
        r.check("`body_received` is gone", "body_received" not in resp.text)
        r.check("a log line still names the failing fields (diagnosable)",
                any("[VALIDATION]" in l and "sex" in l for l in cap.lines),
                [l for l in cap.lines if "[VALIDATION]" in l][:1])
    finally:
        root.removeHandler(cap)
        root.setLevel(prev_level)

    print(f"\n{'PASS' if r.failed == 0 else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    if r.failed:
        print("\nFailures:")
        for line in r.lines:
            print(f"  - {line}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
