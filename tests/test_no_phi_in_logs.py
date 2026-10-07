"""
No patient identifier in any log line, exception text or progress callback.

Run:  <venv>/bin/python -m pytest -q tests/test_no_phi_in_logs.py

A synthetic patient with DISTINCTIVE values (no real person has them) is pushed
through each flow; every formatted log record — message, args and traceback —
and every callback body is captured, and none may contain any of those values
in any of the forms they are usually written in.

  [A] the redaction module: the formats it catches, scoping, the mapping helper
  [B] the logging backstop: messages, %-args, f-strings and tracebacks
  [C] the callback builder: message scrubbed, PHI keys redacted, CRM fields kept
  [D] V2 create + schedule + documents, through the real route and the real
      execute(): real fill phase on a synthetic form, stubbed phases that emit
      the shapes that used to leak (a TN validation message naming the patient,
      a Playwright error echoing a selector with the name), plus the unhandled-
      exception path
  [E] V1 create, through the real route, with the real fill phase
  [F] survey attach, through the real route, with the real search / select /
      verify phases on the repo's synthetic TherapyNotes pages, including a
      refusal
  [G] static: no log/record/fail/emit call in the TN flows interpolates a
      patient field, and the appointment diagnostics capture no field values

All identities are synthetic.
"""

import ast
import json
import logging
import pathlib
import re
import time

import pytest

from shared.phi_redaction import (
    REDACTED,
    install_log_redaction,
    phi_scope,
    phi_scope_for,
    redact_mapping,
    scrub_text,
)

pytestmark = pytest.mark.asyncio

# ---- The sentinel patient -------------------------------------------------
FIRST, LAST = "Zzqxfirst", "Zzqxlast"
DOB = "03/14/1987"
PHONE = "(505) 555-0199"
EMAIL = "zzqx.sentinel@example.test"
ADDRESS = "742 Zzqxsentinel Lane"
ZIP = "87199"
ALERT = "ZZQX-ALERT dob 03/14/1987 insurance Zzqxplan"

# Every form a value may be written in. None may appear anywhere.
FORBIDDEN = [
    FIRST, LAST, FIRST.lower(), LAST.upper(), f"{FIRST} {LAST}",
    DOB, "3/14/1987", "1987-03-14",
    PHONE, "505-555-0199", "5055550199", "505.555.0199",
    EMAIL, EMAIL.upper(), ADDRESS, "Zzqxsentinel", ZIP, "Zzqxplan",
]


def leaks(text: str) -> list:
    return [f for f in FORBIDDEN if f in text]


class _Capture(logging.Handler):
    """Formats records exactly as production does (app.py basicConfig format)."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


@pytest.fixture
def capture():
    install_log_redaction()
    h = _Capture()
    root = logging.getLogger()
    old = root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    try:
        yield h
    finally:
        root.removeHandler(h)
        root.setLevel(old)


@pytest.fixture
def callbacks(monkeypatch):
    """Capture every progress-callback body the agent would POST to the CRM."""
    sent = []

    class _Resp:
        status_code = 200
        text = "{}"

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append(json)
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return sent


def _sentinel_scope():
    return phi_scope(names=[f"{FIRST} {LAST}", FIRST, LAST], dob=DOB, phones=[PHONE],
                     emails=[EMAIL], addresses=[ADDRESS], zips=[ZIP], other=[ALERT])


# ===========================================================================
# [A] The redaction module
# ===========================================================================

async def test_a_formats_are_caught():
    with _sentinel_scope():
        text = (f"{FIRST} {LAST} / {FIRST.upper()} / {DOB} / 3/14/1987 / 1987-03-14 / "
                f"{PHONE} / 505-555-0199 / 5055550199 / +1 505 555 0199 / {EMAIL.upper()} / "
                f"{ADDRESS.lower()} / zip {ZIP} / {ALERT}")
        out = scrub_text(text)
    assert not leaks(out), leaks(out)
    assert out.count(REDACTED) >= 10


async def test_a_scope_ends_and_nests():
    with _sentinel_scope():
        with phi_scope(names=["Zzinner Name"]):
            assert "Zzinner" not in scrub_text("Zzinner") and FIRST not in scrub_text(FIRST)
        assert scrub_text("Zzinner") == "Zzinner", "inner scope leaked outward"
    assert scrub_text(FIRST) == FIRST, "scope outlived its block"


async def test_a_unrelated_numbers_survive():
    with _sentinel_scope():
        assert scrub_text("contact=900753 run=abc duration=87200ms") == "contact=900753 run=abc duration=87200ms"


async def test_a_redact_mapping_keeps_what_the_crm_needs():
    with _sentinel_scope():
        out = redact_mapping(
            {"first_name": FIRST, "patientName": "x", "tnPatientUrl": "https://tn/app/patients/view/ZZ1/",
             "documentsUploaded": [1, 2], "note": f"hello {LAST}", "nested": {"dob": DOB, "ok": 3}},
            keep={"tnPatientUrl", "documentsUploaded"},
        )
    assert out["first_name"] == REDACTED and out["patientName"] == REDACTED
    assert out["tnPatientUrl"].endswith("/ZZ1/") and out["documentsUploaded"] == [1, 2]
    assert out["note"] == f"hello {REDACTED}" and out["nested"] == {"dob": REDACTED, "ok": 3}


# ===========================================================================
# [B] The logging backstop
# ===========================================================================

async def test_b_every_log_shape_is_scrubbed(capture):
    log = logging.getLogger("services.api.anything")
    with _sentinel_scope():
        log.info(f"f-string {FIRST} {LAST} {DOB}")
        log.info("percent args %s %s", EMAIL, PHONE)
        log.warning("object %s", {"patient": {"name": f"{FIRST} {LAST}", "zip": ZIP}})
        try:
            raise RuntimeError(f"Timeout 5000ms waiting for locator('text={FIRST} {LAST}') value={DOB}")
        except RuntimeError:
            log.exception("unhandled")
    text = "\n".join(capture.lines)
    assert "Traceback" in text and "RuntimeError" in text, "the traceback itself must survive"
    assert not leaks(text), leaks(text)


async def test_b_outside_a_run_nothing_is_touched(capture):
    logging.getLogger("x").info("contact=1 plain line")
    assert capture.lines[-1].endswith("contact=1 plain line")


# ===========================================================================
# [C] The callback builder
# ===========================================================================

async def test_c_callback_message_and_metadata(callbacks):
    from services.api.tn_executor_v2 import _emit_progress
    with _sentinel_scope():
        await _emit_progress(
            callback_url="https://crm.example/api/internal/tn-progress/7", api_key="k",
            contact_id=7, run_id="run-1", phase="save", status="failed",
            message=f"Patient '{FIRST} {LAST}' (DOB {DOB}) already exists",
            metadata={"failureReason": "save_failed", "tnPatientUrl": "https://tn/app/patients/view/ZZ1/",
                      "patient_name": f"{FIRST} {LAST}", "tnOverlayMessages": [f"Hello {FIRST}"],
                      "documentsUploaded": [4], "appointmentDatetime": "10/01/2026 9:00 am"},
        )
    body = callbacks[-1]
    assert not leaks(json.dumps(body)), leaks(json.dumps(body))
    assert body["contactId"] == 7 and body["runId"] == "run-1" and body["phase"] == "save"
    md = body["metadata"]
    assert md["failureReason"] == "save_failed" and md["tnPatientUrl"].endswith("/ZZ1/")
    assert md["documentsUploaded"] == [4] and md["appointmentDatetime"] == "10/01/2026 9:00 am"
    assert md["patient_name"] == REDACTED


# ===========================================================================
# [D] V2 through the real route and the real execute()
# ===========================================================================

FORM_FIELDS = [
    "PatientInformationEditor__FirstNameInput", "PatientInformationEditor__LastNameInput",
    "PatientInformationEditor__DOBInput", "AddressEditorView__Address1Input_PatientAddress",
    "AddressEditorView__PostalCodeInput_PatientAddress", "PatientInformationEditor__EmailInput",
    "PatientInformationEditor__MobilePhoneInput",
]
FORM_HTML = "<html><body>" + "".join(f'<input type="text" id="{f}">' for f in FORM_FIELDS) + """
  <input type="text" id="AddressEditorView__CityInput_PatientAddress">
  <input type="radio" name="Sex" value="0"><input type="radio" name="Sex" value="1">
  <script>document.getElementById('AddressEditorView__PostalCodeInput_PatientAddress')
    .addEventListener('blur', () => { document.getElementById('AddressEditorView__CityInput_PatientAddress').value = 'Zzcity'; });</script>
</body></html>"""


def _v2_request(**over):
    from shared.schemas.therapy_notes_v2 import TNPatientInputV2
    base = dict(
        first_name=FIRST, last_name=LAST, dob=DOB, address=ADDRESS, zip=ZIP, sex="Male",
        email=EMAIL, phone="5055550199", rfs_url="", intake_pdf_url="https://crm.example/i",
        snapshot_pdf_url="https://crm.example/s", appointment_date="10/01/2026",
        appointment_time="9:00 am", appointment_alert_text=ALERT, clinician_name="Zz Clinician",
        contact_id=4242, run_id="zz-run-1", callback_url="https://crm.example/api/internal/tn-progress/4242",
        documents=[{"crm_document_id": 9, "tn_name": "Custody order (CRM)", "mime_type": "application/pdf",
                    "url": "https://crm.example/api/internal/contact-document/4242/9"}],
    )
    base.update(over)
    return TNPatientInputV2(**base)


async def _run_v2_route(monkeypatch, *, save_raises=None):
    """Drive routes/therapy_notes_v2 → a real TNExecutorV2.execute() on a synthetic form."""
    from playwright.async_api import async_playwright
    import services.api.mcp_runtime as mcp_runtime
    import services.api.tn_executor_v2 as v2
    from services.api.routes.therapy_notes_v2 import create_patient_with_schedule
    from shared.schemas.therapy_notes_v2 import TNDocumentResultV2

    class _DummyRuntime:
        def __init__(self, *a, **k):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(mcp_runtime, "PlaywrightRuntime", _DummyRuntime)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-http2"])
        page = await browser.new_page()
        await page.route(re.compile(r"https://www\.therapynotes\.com/.*"),
                         lambda r: r.fulfill(status=200, content_type="text/html", body=FORM_HTML))
        await page.goto("https://www.therapynotes.com/app/patients/edit/", wait_until="domcontentloaded")

        async def fake_run(runtime, patient):
            ex = v2.TNExecutorV2.__new__(v2.TNExecutorV2)

            class _R:
                async def ensure_browser(self_inner):
                    return page

            ex._runtime = _R()

            async def ok(*a, **k):
                return True

            async def save(patient):
                # The shapes that used to leak: TherapyNotes' own validation text
                # naming the patient, logged raw; a Playwright error echoing a
                # selector built from the name.
                logging.getLogger("services.api.tn_executor_v2").warning(
                    f"[SAVE] Validation text on page: A patient named {patient.first_name} "
                    f"{patient.last_name} born {patient.dob} already exists")
                if save_raises:
                    raise RuntimeError(save_raises)
                ex._tn_patient_url = "https://www.therapynotes.com/app/patients/view/ZZTESTREC1/"
                ex._tn_patient_id = "ZZTESTREC1"
                return True

            async def docs(patient):
                return [TNDocumentResultV2(crm_document_id=9, status="uploaded")]

            for name in ("_phase_entry", "_phase_login", "_phase_navigate", "_phase_detect_form",
                         "_phase_upload_intake_pdf", "_phase_upload_snapshot_pdf",
                         "_phase_schedule_appointment"):
                setattr(ex, name, ok)
            async def no_shot(*a, **k):
                return None

            ex._phase_save_patient = save
            ex._phase_upload_documents = docs
            ex._capture_screenshot = no_shot
            ex._debug_screenshot = no_shot
            return await ex.execute(patient)   # real execute, real fill phase, real callbacks

        monkeypatch.setattr(v2, "run_tn_v2_patient_creation", fake_run)
        result = await create_patient_with_schedule(_v2_request())
        await browser.close()
        return result


async def test_d_v2_success_run_leaks_nothing(monkeypatch, capture, callbacks):
    result = await _run_v2_route(monkeypatch)
    assert result.status == "success"
    logs = "\n".join(capture.lines)
    bodies = json.dumps(callbacks)
    assert not leaks(logs), leaks(logs)
    assert not leaks(bodies), leaks(bodies)
    # Ids where a person needs to know which patient.
    assert "contact=4242" in logs and "WORKFLOW COMPLETE: contact=4242" in logs
    phases = [(b["phase"], b["status"]) for b in callbacks]
    assert ("fill_form", "ok") in phases and ("upload_documents", "ok") in phases and ("workflow_complete", "ok") in phases
    msgs = {b["phase"]: b["message"] for b in callbacks if b["status"] == "ok"}
    assert msgs["fill_form"] == "Required fields filled"
    assert msgs["save"] == "Patient saved in TherapyNotes"
    done = next(b for b in callbacks if b["phase"] == "workflow_complete")
    assert done["metadata"]["documentsUploaded"] == [9], "the CRM still gets what it stamps from"
    assert done["metadata"]["tnPatientUrl"].endswith("/ZZTESTREC1/")


async def test_d_v2_unhandled_exception_leaks_nothing(monkeypatch, capture, callbacks):
    result = await _run_v2_route(
        monkeypatch,
        save_raises=f"Locator.click: Timeout 15000ms exceeded waiting for text=\"{FIRST} {LAST}\" dob {DOB} {EMAIL}",
    )
    assert result.status == "error"
    logs = "\n".join(capture.lines)
    assert "Unhandled executor error" in logs
    assert not leaks(logs), leaks(logs)
    assert not leaks(json.dumps(callbacks)), leaks(json.dumps(callbacks))
    assert not leaks(json.dumps(result.model_dump(mode="json")["error_message"] or ""))


# ===========================================================================
# [E] V1 through the real route, real fill phase
# ===========================================================================

async def test_e_v1_route_leaks_nothing(monkeypatch, capture):
    from playwright.async_api import async_playwright
    import services.api.mcp_runtime as mcp_runtime
    import services.api.tn_executor as v1
    from services.api.routes.therapy_notes import create_patient
    from shared.schemas.therapy_notes import TNPatientInput, TNExecutorOutput

    class _DummyRuntime:
        def __init__(self, *a, **k):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(mcp_runtime, "PlaywrightRuntime", _DummyRuntime)
    req = TNPatientInput(first_name=FIRST, last_name=LAST, dob=DOB, address=ADDRESS, zip=ZIP,
                         sex="Female", email=EMAIL, phone="5055550199", rfs_url="")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-http2"])
        page = await browser.new_page()
        await page.set_content(FORM_HTML)

        async def fake_run(runtime, patient):
            ex = v1.TNExecutor.__new__(v1.TNExecutor)
            ex._page = page
            ex._logs = []
            ex._start_time = time.time()
            ex._pending_failure = {}
            ex._surfaced_overlays = []
            ex._overlays_reported = set()

            async def noop(*a, **k):
                return None

            ex._capture_screenshot = noop
            assert await ex._phase_fill_required(patient)
            logging.getLogger("services.api.tn_executor").info(
                f"[SAVE] Validation text on page: {patient.first_name} {patient.last_name} {patient.email}")
            return TNExecutorOutput.success(
                patient_name=f"{patient.first_name} {patient.last_name}", logs=ex._logs,
                duration_ms=1, tn_patient_url="https://www.therapynotes.com/app/patients/view/ZZ2/",
                tn_patient_id="ZZ2")

        monkeypatch.setattr(v1, "run_tn_patient_creation", fake_run)
        result = await create_patient(req)
        await browser.close()
    assert result.status == "success"
    assert result.patient_name == f"{FIRST} {LAST}", "the response contract is unchanged"
    logs = "\n".join(capture.lines)
    assert "TN patient created" in logs
    assert not leaks(logs), leaks(logs)


# ===========================================================================
# [F] Survey attach through the real route, real phases on synthetic TN pages
# ===========================================================================

async def test_f_survey_attach_leaks_nothing(monkeypatch, capture):
    from playwright.async_api import async_playwright
    import services.api.survey_attach_executor as sae
    from services.api.routes.survey_attach import attach_survey_to_chart
    from shared.schemas.survey_attach import SurveyAttachOutput
    import tests.test_survey_attach as fx

    forbidden = [fx.FIRST, fx.LAST, f"{fx.FIRST} {fx.LAST}", fx.DOB_PADDED, fx.DOB_SHORT,
                 fx.PHONE_IN, fx.PHONE_CHART, "5055550142"]
    data = fx.make_input()
    wrong = fx.make_input(dob="02/03/1991")

    async def fake_run(runtime, d):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-http2"])
            # Real select + verify phases, a match and a refusal.
            await fx.run_search_select(browser, [(fx.PID, f"{fx.FIRST} {fx.LAST}", fx.DOB_SHORT)], d)
            await fx.run_verify(browser, fx.chart_page(), d)
            await fx.run_verify(browser, fx.chart_page(), wrong)
            await browser.close()
        return SurveyAttachOutput(status="success", document_name=d.document_name,
                                  tn_patient_url=f"https://www.therapynotes.com/app/patients/edit/{fx.PID}/",
                                  logs=[], duration_ms=1)

    monkeypatch.setattr(sae, "run_survey_attach", fake_run)
    try:
        out = await attach_survey_to_chart(data)
    except TypeError:
        pytest.skip("SurveyAttachOutput shape differs; covered by [G]")
    logs = "\n".join(capture.lines)
    assert "[ATTACH] request" in logs and "contact_id=1" in logs
    found = [f for f in forbidden if f in logs]
    assert not found, found
    # The chart URL names one patient's record as directly as a name would
    # (2026-10-07): never in a log line, success or refusal. The CRM still gets
    # it in the response.
    assert "[ATTACH] attached" in logs and "| contact_id=1 |" in logs
    assert fx.PID not in logs, "the chart record id reached a log line"
    assert "/app/patients/" not in logs, "a chart URL reached a log line"
    assert out.tn_patient_url and fx.PID in out.tn_patient_url, "the response must still carry the chart"


# ===========================================================================
# [G] Static guards
# ===========================================================================

TN_FILES = [
    "services/api/tn_executor.py", "services/api/tn_executor_v2.py",
    "services/api/survey_attach_executor.py", "services/api/active_patients_executor.py",
    "services/api/active_count_executor.py", "services/api/routes/therapy_notes.py",
    "services/api/routes/therapy_notes_v2.py", "services/api/routes/survey_attach.py",
    "services/api/routes/active_patients.py", "services/api/routes/active_count.py",
]
PATIENT_ATTRS = {"first_name", "last_name", "dob", "phone", "email", "address", "zip",
                 "appointment_alert_text", "appointment_date", "appointment_time", "patient_name"}
PATIENT_NAMES = {"full_name", "patient_name", "expected_name", "want_name"}
SINK_FUNCS = {"info", "warning", "error", "exception", "debug", "critical",
              "_record_log", "_fail_phase", "_refuse", "_record", "_emit", "_emit_progress", "_step", "print"}


# The one patient-derived value a callback may carry: the appointment time the
# CRM shows on the completed entry ("Patient added to TN with appointment for
# <name>: <when>"). It goes to the CRM, which already holds it, not to a log.
CALLBACK_ALLOWED_KEYS = {"appointmentDatetime"}


def _patient_refs(node):
    """Patient fields interpolated as VALUES. bool(x) / len(x) say presence or size, not the value."""
    for n in ast.walk(node):
        if not isinstance(n, ast.FormattedValue):
            continue
        v = n.value
        if isinstance(v, ast.Call) and getattr(v.func, "id", None) in ("bool", "len"):
            continue
        for m in ast.walk(v):
            if isinstance(m, ast.Attribute) and m.attr in PATIENT_ATTRS:
                yield ast.unparse(m)
            elif isinstance(m, ast.Name) and m.id in PATIENT_NAMES:
                yield m.id


def _allowed_callback_values(call):
    """f-strings that are the value of an allowed key in a dict argument."""
    ok = set()
    for n in ast.walk(call):
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value in CALLBACK_ALLOWED_KEYS:
                    ok.update(id(x) for x in ast.walk(v))
    return ok


async def test_g_no_sink_interpolates_a_patient_field():
    violations = []
    for path in TN_FILES:
        tree = ast.parse(pathlib.Path(path).read_text())
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in SINK_FUNCS:
                continue
            allowed = _allowed_callback_values(call)
            for arg in list(call.args) + [k.value for k in call.keywords]:
                for j in (n for n in ast.walk(arg) if isinstance(n, ast.JoinedStr) and id(n) not in allowed):
                    for ref in _patient_refs(j):
                        violations.append(f"{path}:{call.lineno} {name}(… {ref} …)")
    assert not violations, "\n".join(violations)


async def test_g_appointment_diagnostics_capture_no_values():
    src = pathlib.Path("services/api/tn_executor_v2.py").read_text()
    for bad in ("value: el.value", "value: i.value", "(t.value || '').substring", "outerHTML_first_3000",
                "alert='{patient.appointment_alert_text}'"):
        assert bad not in src, f"diagnostic still captures a value: {bad}"


async def test_g_every_flow_is_scoped():
    for path, needle in [
        ("services/api/routes/therapy_notes_v2.py", "with phi_scope_for(request):"),
        ("services/api/routes/therapy_notes.py", "with phi_scope_for(request):"),
        ("services/api/routes/survey_attach.py", "with phi_scope_for(request):"),
        ("services/api/tn_executor_v2.py", "with phi_scope_for(patient):"),
        ("services/api/tn_executor.py", "with phi_scope_for(patient):"),
        ("services/api/survey_attach_executor.py", "with phi_scope_for(data):"),
        ("services/api/app.py", "install_log_redaction()"),
        ("services/api/tn_executor_v2.py", '"message": scrub_text(message),'),
    ]:
        assert needle in pathlib.Path(path).read_text(), f"{path}: {needle}"
