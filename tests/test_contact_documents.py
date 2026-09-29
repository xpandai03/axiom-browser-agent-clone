"""
CRM contact documents, the zip refusal, and the no-second-copy pre-check.

Run:  <venv>/bin/python -m pytest -q tests/test_contact_documents.py

[A] Pure helpers — exact-name matching, type sniffing, the picker's accept
    attribute, and the phase summary the CRM stamps from.
[B] Zip (28 September, contact 900753): a zip TherapyNotes fills no city for is
    looked up TWICE, then refused with a sentence naming the field. The run
    never continues with a blank city. A second lookup that works, passes.
[C] The document loop: order kept, first failure stops it, the rest are
    reported not attempted, an unsupported type is skipped, nothing raises, and
    no document name reaches a log.
[D] The run: documents go LAST, after the appointment; a document failure
    leaves the run a success; a CRM that sends no documents sees no change.
[E] The pre-check, against a synthetic TherapyNotes Documents tab: a document
    already on the chart under its exact name is not uploaded again (the survey
    agent_timeout retry, and a re-sent CRM document), a longer name that merely
    starts the same is not mistaken for it, and a real upload is confirmed by
    its exact row.

All identities, names and files are synthetic.
"""

import logging
import os
import re
import tempfile
import time

import pytest

from services.api.tn_executor_v2 import (
    TNExecutorV2,
    ZIP_NOT_RECOGNISED_MESSAGE,
    accept_allows,
    document_phase_summary,
    document_row_matches,
    sniff_document_mime,
)
from shared.schemas.therapy_notes_v2 import (
    TNDocumentResultV2,
    TNDocumentV2,
    TNPatientInputV2,
    TNPhaseV2,
)

pytestmark = pytest.mark.asyncio

ORIGIN = "https://www.therapynotes.com"
FORM_URL = f"{ORIGIN}/app/patients/edit/"
CHART_URL = f"{ORIGIN}/app/patients/view/ZZTESTREC1/"

# A distinctive staff-entered name. It must never appear in a log record.
SENTINEL_NAME = "Zzsentinel Custody Order"


# ===========================================================================
# [A] Pure helpers
# ===========================================================================

@pytest.mark.parametrize("cell,name,expected", [
    ("Intake Referral\nPDF 1KB", "Intake Referral", True),
    ("Intake ReferralPDF 1KB", "Intake Referral", True),          # recon: no gap
    ("Custody order (CRM)PDF 2KB", "Custody order (CRM)", True),
    ("Custody order (CRM) JPG 80KB", "Custody order (CRM)", True),
    ("Custody order (CRM)", "Custody order (CRM)", True),
    ("Custody order (CRM 2)PDF 2KB", "Custody order (CRM)", False),  # a different document
    ("Client Survey 2026-09-28 (Sub 123)PDF", "Client Survey 2026-09-28 (Sub 12)", False),
    ("Client Survey 2026-09-28 (Sub 12)PDF", "Client Survey 2026-09-28 (Sub 12)", True),
    ("custody order (CRM)PDF", "Custody order (CRM)", False),     # TN shows what was typed
    ("Intake Referrals PDF", "Intake Referral", False),
    ("", "Intake Referral", False),
    (None, "Intake Referral", False),
    ("  Intake   Referral \n PDF", "Intake Referral", True),      # whitespace-insensitive
])
async def test_a_document_row_matches(cell, name, expected):
    assert document_row_matches(cell, name) is expected


async def test_a_sniff_document_mime():
    assert sniff_document_mime(b"%PDF-1.4\n") == "application/pdf"
    assert sniff_document_mime(b"\xff\xd8\xff\xe0\x00\x10") == "image/jpeg"
    assert sniff_document_mime(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert sniff_document_mime(b"<html>") is None
    assert sniff_document_mime(b"GIF89a") is None


@pytest.mark.parametrize("accept,mime,expected", [
    (None, "image/png", True),                 # the recon's observation: no attribute
    ("", "image/jpeg", True),
    (".pdf", "image/png", False),
    (".pdf", "application/pdf", True),
    ("application/pdf,image/*", "image/jpeg", True),
    (".pdf, .jpg, .jpeg", "image/jpeg", True),
    (".pdf, .jpg", "image/png", False),
    ("*/*", "image/png", True),
])
async def test_a_accept_allows(accept, mime, expected):
    assert accept_allows(accept, mime) is expected


async def test_a_phase_summary_stamps_only_documents_on_the_chart():
    results = [
        TNDocumentResultV2(crm_document_id=11, status="uploaded"),
        TNDocumentResultV2(crm_document_id=12, status="already_on_chart"),
        TNDocumentResultV2(crm_document_id=13, status="unsupported", reason="document_unsupported_type"),
        TNDocumentResultV2(crm_document_id=14, status="failed", reason="document_upload_failed"),
        TNDocumentResultV2(crm_document_id=15, status="not_attempted"),
    ]
    s = document_phase_summary(results)
    assert s["metadata"]["documentsUploaded"] == [11, 12]
    assert s["metadata"]["documentsPartial"] is True
    assert [r["status"] for r in s["metadata"]["documentResults"]] == [
        "uploaded", "already_on_chart", "unsupported", "failed", "not_attempted"]
    assert "2 of 5" in s["message"] and "document 14 did not file" in s["message"]
    assert "1 not attempted" in s["message"]
    full = document_phase_summary([TNDocumentResultV2(crm_document_id=1, status="uploaded")])
    assert full["metadata"]["documentsPartial"] is False
    assert full["message"] == "1 of 1 CRM document on the chart"


async def test_a_input_without_documents_is_unchanged():
    """A CRM that predates the field sends no `documents`; it validates, empty."""
    base = dict(
        first_name="Zz", last_name="Test", dob="01/02/2010", address="1 Zz Way",
        zip="87144", sex="Male", email="zz@example.test", phone="5055550100",
        rfs_url="", intake_pdf_url="https://crm.example/i", snapshot_pdf_url="https://crm.example/s",
        appointment_date="10/01/2026", appointment_time="9:00 am", appointment_alert_text="ZZTEST alert",
        clinician_name="Zz Clinician",
    )
    p = TNPatientInputV2(**base)
    assert p.documents == []
    with_docs = TNPatientInputV2(**base, documents=[
        {"crm_document_id": 3, "tn_name": "Fax referral 09/26/2026 (CRM)", "mime_type": "application/pdf",
         "url": "https://crm.example/api/internal/contact-document/1/3"},
    ])
    assert with_docs.documents[0].crm_document_id == 3
    with pytest.raises(Exception):
        TNDocumentV2(crm_document_id=1, tn_name="x" * 129, mime_type="application/pdf", url="https://a/b")


# ===========================================================================
# [B] Zip — twice, then a named refusal; never a blank city
# ===========================================================================

FIELDS = [
    "PatientInformationEditor__FirstNameInput",
    "PatientInformationEditor__LastNameInput",
    "PatientInformationEditor__DOBInput",
    "AddressEditorView__Address1Input_PatientAddress",
    "AddressEditorView__PostalCodeInput_PatientAddress",
    "PatientInformationEditor__EmailInput",
    "PatientInformationEditor__MobilePhoneInput",
]


def _zip_form(fill_on_blur_number):
    """City fills on the Nth blur of the zip field (0 = never)."""
    inputs = "\n".join(f'<input type="text" id="{f}">' for f in FIELDS)
    return f"""<html><body>
      {inputs}
      <input type="text" id="AddressEditorView__CityInput_PatientAddress">
      <select id="AddressEditorView__StateSelect_PatientAddress"><option value=""></option></select>
      <input type="radio" name="Sex" value="0"><input type="radio" name="Sex" value="1">
      <script>
        window.__blurs = 0;
        var zip = document.getElementById('AddressEditorView__PostalCodeInput_PatientAddress');
        var city = document.getElementById('AddressEditorView__CityInput_PatientAddress');
        zip.addEventListener('blur', function () {{
          window.__blurs++;
          if ({fill_on_blur_number} && window.__blurs === {fill_on_blur_number}) city.value = 'Zzcity';
        }});
      </script>
    </body></html>"""


class _Patient:
    first_name = "Zzfirst"
    last_name = "Zzlast"
    dob = "04/27/2014"
    address = "1 Zzsentinel Way"
    zip = "87144"
    sex = "Male"
    email = "zz@example.test"
    phone = "5055550100"


async def _run_fill(html):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-http2"])
        ctx = await browser.new_context()
        page = await ctx.new_page()

        async def route(r):
            await r.fulfill(status=200, content_type="text/html", body=html)

        await ctx.route(re.compile(r"https://www\.therapynotes\.com/.*"), route)
        await page.goto(FORM_URL, wait_until="domcontentloaded")
        ex = _bare_executor(page)
        ok = await ex._phase_fill_required(_Patient())
        blurs = await page.evaluate("window.__blurs")
        email = await page.input_value("#PatientInformationEditor__EmailInput")
        city = await page.input_value("#AddressEditorView__CityInput_PatientAddress")
        await browser.close()
        return ok, ex._pending_failure, blurs, email, city


def _bare_executor(page):
    ex = TNExecutorV2.__new__(TNExecutorV2)
    ex._page = page
    ex._logs = []
    ex._start_time = time.time()
    ex._pending_failure = {}
    ex._surfaced_overlays = []
    ex._overlays_reported = set()
    ex._save_problem_text = None

    async def _noop(*a, **k):
        return None

    ex._capture_screenshot = _noop
    ex._debug_screenshot = _noop
    ex._record_log = lambda *a, **k: None

    async def _fail_phase(phase, reason, message, phase_start=None):
        ex._pending_failure = {"phase": phase, "reason": reason, "message": message}
        return False

    ex._fail_phase = _fail_phase
    ex._reason_for = lambda e, default: default
    return ex


async def test_b_city_on_first_lookup_is_unchanged():
    ok, failure, blurs, email, city = await _run_fill(_zip_form(1))
    assert ok is True, failure
    assert blurs == 1, "a working lookup must not be retyped"
    assert city == "Zzcity"


async def test_b_city_on_second_lookup_passes():
    ok, failure, blurs, email, city = await _run_fill(_zip_form(2))
    assert ok is True, f"the second lookup should have been tried: {failure}"
    assert blurs == 2
    assert city == "Zzcity"


async def test_b_no_city_after_two_lookups_is_refused_by_name(caplog):
    caplog.set_level(logging.INFO)
    ok, failure, blurs, email, city = await _run_fill(_zip_form(0))
    assert ok is False
    assert failure["reason"] == "zip_not_recognised"
    assert failure["message"] == ZIP_NOT_RECOGNISED_MESSAGE
    assert "check the address on the contact" in failure["message"]
    assert blurs == 2, "exactly one retry before refusing"
    # Never proceeds with a blank city: the form stops before Email.
    assert city == "" and email == "", "the run continued past a blank city"
    # Diagnostics are logged — ids, booleans and counts — and no value.
    diag = [r.getMessage() for r in caplog.records if "Zip lookup gave no city" in r.getMessage()]
    assert diag, "no diagnostics line"
    assert "state_field_id='AddressEditorView__StateSelect_PatientAddress'" in diag[0]
    for rec in caplog.records:
        m = rec.getMessage()
        assert "87144" not in m and "Zzsentinel" not in m and "Zzcity" not in m, f"value in a log line: {m}"


async def test_b_zip_not_recognised_is_a_declared_reason():
    import typing
    from shared.schemas.therapy_notes_v2 import TNFailureReasonV2
    assert "zip_not_recognised" in typing.get_args(TNFailureReasonV2)
    assert "zip_autocomplete_failed" in typing.get_args(TNFailureReasonV2), "kept for old records"


# ===========================================================================
# [C] The document loop
# ===========================================================================

def _docs(*ids, mime="application/pdf"):
    return [TNDocumentV2(crm_document_id=i, tn_name=f"{SENTINEL_NAME} {i} (CRM)", mime_type=mime,
                         url=f"https://crm.example/api/internal/contact-document/1/{i}") for i in ids]


class _P:
    def __init__(self, documents):
        self.documents = documents


def _loop_executor(behaviour, download_fail=()):
    """behaviour: crm id -> 'uploaded' | 'already_on_chart' | 'failed' | 'unsupported' | 'raise'."""
    ex = _bare_executor(page=None)
    ex._tn_patient_url = CHART_URL
    calls = []

    async def fake_download(url, mime):
        doc_id = int(url.rsplit("/", 1)[1])
        if doc_id in download_fail:
            raise RuntimeError("download failed")
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.write(fd, b"%PDF-1.4 zz")
        os.close(fd)
        return path

    async def fake_upload(url, path, name, phase, reason, **kw):
        doc_id = int(kw["log_label"].rsplit(" ", 1)[1])
        calls.append((doc_id, kw))
        b = behaviour.get(doc_id, "uploaded")
        if b == "raise":
            raise RuntimeError("the page went away")
        ex._last_upload_outcome = b
        return b in ("uploaded", "already_on_chart")

    ex._download_document_to_tempfile = fake_download
    ex._upload_pdf_to_patient = fake_upload
    return ex, calls


async def test_c_all_documents_file_in_the_order_sent():
    ex, calls = _loop_executor({})
    res = await ex._phase_upload_documents(_P(_docs(7, 3, 9)))
    assert [r.crm_document_id for r in res] == [7, 3, 9]
    assert all(r.status == "uploaded" for r in res)
    assert [c[0] for c in calls] == [7, 3, 9], "order not kept"
    assert all(c[1]["check_existing"] is True for c in calls), "documents must be pre-checked"


async def test_c_first_failure_stops_the_loop_the_rest_not_attempted():
    ex, calls = _loop_executor({2: "failed"})
    res = await ex._phase_upload_documents(_P(_docs(1, 2, 3)))
    assert [(r.crm_document_id, r.status) for r in res] == [
        (1, "uploaded"), (2, "failed"), (3, "not_attempted")]
    assert res[1].reason == "document_upload_failed"
    assert [c[0] for c in calls] == [1, 2], "a document after the failure was attempted"


async def test_c_download_failure_also_stops():
    ex, calls = _loop_executor({}, download_fail=(1,))
    res = await ex._phase_upload_documents(_P(_docs(1, 2)))
    assert [(r.status, r.reason) for r in res] == [("failed", "document_download_failed"), ("not_attempted", None)]
    assert calls == []


async def test_c_unsupported_type_is_skipped_and_the_loop_continues():
    ex, calls = _loop_executor({1: "unsupported"})
    res = await ex._phase_upload_documents(_P(_docs(1, 2)))
    assert [(r.crm_document_id, r.status) for r in res] == [(1, "unsupported"), (2, "uploaded")]


async def test_c_already_on_chart_counts_as_on_chart():
    ex, _ = _loop_executor({1: "already_on_chart"})
    res = await ex._phase_upload_documents(_P(_docs(1)))
    assert res[0].status == "already_on_chart" and res[0].on_chart


async def test_c_an_exception_is_a_result_not_a_raise():
    ex, _ = _loop_executor({1: "raise"})
    res = await ex._phase_upload_documents(_P(_docs(1, 2)))
    assert [(r.status, r.reason) for r in res] == [("failed", "document_upload_failed"), ("not_attempted", None)]


async def test_c_no_patient_record_attempts_nothing():
    ex, calls = _loop_executor({})
    ex._tn_patient_url = FORM_URL  # no record segment
    res = await ex._phase_upload_documents(_P(_docs(1)))
    assert res[0].status == "not_attempted" and res[0].reason == "no_patient_record"
    assert calls == []


async def test_c_document_names_never_reach_a_log(caplog):
    caplog.set_level(logging.DEBUG)
    ex, calls = _loop_executor({2: "failed"})
    await ex._phase_upload_documents(_P(_docs(1, 2, 3)))
    for rec in caplog.records:
        assert SENTINEL_NAME not in rec.getMessage()
    assert all(c[1]["log_label"].startswith("CRM document ") for c in calls)


# ===========================================================================
# [D] The run — documents last, never fatal, absent means unchanged
# ===========================================================================

async def _run_execute(documents, doc_behaviour=None):
    ex = TNExecutorV2.__new__(TNExecutorV2)
    events = []

    class _Runtime:
        async def ensure_browser(self):
            return object()

    ex._runtime = _Runtime()

    async def ok(*a, **k):
        return True

    for name in ("_phase_entry", "_phase_login", "_phase_navigate", "_phase_detect_form",
                 "_phase_fill_required", "_phase_save_patient", "_phase_upload_intake_pdf",
                 "_phase_upload_snapshot_pdf", "_phase_schedule_appointment"):
        setattr(ex, name, ok)

    async def emit(phase, status, message, metadata=None):
        events.append((phase, status, message, metadata or {}))

    ex._emit = emit
    ex._tn_patient_url = CHART_URL
    ex._tn_patient_id = "ZZTESTREC1"

    async def docs_phase(patient):
        out = []
        stopped = False
        for d in patient.documents:
            b = (doc_behaviour or {}).get(d.crm_document_id, "uploaded")
            if stopped:
                out.append(TNDocumentResultV2(crm_document_id=d.crm_document_id, status="not_attempted"))
            elif b == "failed":
                out.append(TNDocumentResultV2(crm_document_id=d.crm_document_id, status="failed",
                                              reason="document_upload_failed"))
                stopped = True
            else:
                out.append(TNDocumentResultV2(crm_document_id=d.crm_document_id, status="uploaded"))
        return out

    ex._phase_upload_documents = docs_phase

    class _Pt:
        first_name = "Zz"
        last_name = "Test"
        callback_url = None
        run_id = "zz-run"
        contact_id = 1
        clinician_name = "Zz Clinician"
        appointment_date = "10/01/2026"
        appointment_time = "9:00 am"

    pt = _Pt()
    pt.documents = documents
    out = await ex.execute(pt)
    return out, events


async def test_d_documents_run_after_the_appointment():
    out, events = await _run_execute(_docs(1, 2))
    seq = [(p, s) for p, s, _, _ in events]
    i_sched = seq.index(("schedule_appointment", "ok"))
    i_docs = seq.index(("upload_documents", "started"))
    i_done = seq.index(("workflow_complete", "ok"))
    assert i_sched < i_docs < i_done
    assert seq.index(("upload_intake_pdf", "ok")) < i_docs, "documents must follow the intake PDF"
    assert out.status == "success"
    done_md = events[i_done][3]
    assert done_md["documentsUploaded"] == [1, 2] and done_md["documentsPartial"] is False


async def test_d_a_document_failure_leaves_the_run_a_success():
    out, events = await _run_execute(_docs(1, 2, 3), {2: "failed"})
    assert out.status == "success", "a custody scan must not fail the patient or the booking"
    assert not any(s == "failed" for _, s, _, _ in events), "no phase may report failed"
    docs_ok = next(e for e in events if e[0] == "upload_documents" and e[1] == "ok")
    assert docs_ok[3]["documentsUploaded"] == [1]
    assert docs_ok[3]["documentsPartial"] is True
    assert "1 of 3" in docs_ok[2] and "document 2 did not file" in docs_ok[2]
    done = next(e for e in events if e[0] == "workflow_complete")
    assert done[1] == "ok" and done[3]["documentsUploaded"] == [1]
    assert [r.status for r in out.document_results] == ["uploaded", "failed", "not_attempted"]


async def test_d_no_documents_means_no_change():
    """What the current CRM sends: no documents. No phase, no new metadata."""
    out, events = await _run_execute([])
    assert not any(p == "upload_documents" for p, _, _, _ in events)
    done = next(e for e in events if e[0] == "workflow_complete")
    assert set(done[3]) == {"tnPatientUrl", "durationMs"}
    assert done[2] == "Workflow complete — patient and appointment created in TherapyNotes"
    assert out.document_results == []


# ===========================================================================
# [E] The pre-check and the upload, on a synthetic Documents tab
# ===========================================================================

def _documents_tab(existing_names, accept=None):
    rows = "\n".join(
        f'<tr class="Row"><td class="v-align-top"><a href="#">{n}</a><br>PDF 1KB</td><td>9/28/2026</td></tr>'
        for n in existing_names
    )
    accept_attr = f' accept="{accept}"' if accept else ""
    return f"""<html><body>
      <ul><li><a href="#tab=Documents">Documents</a></li></ul>
      <button id="upload" onclick="openModal()">Upload Patient File</button>
      <table id="docs">{rows}</table>
      <div id="modal" style="display:none">
        <input type="file" id="InputUploader" name="InputUploader"{accept_attr}>
        <input id="PatientFile__DocumentName" maxlength="128">
        <input type="button" value="Add Document" disabled onclick="addDoc()">
        <button class="DialogCloseButton" onclick="closeModal()">x</button>
      </div>
      <script>
        window.__uploadClicked = 0; window.__added = [];
        function openModal() {{ window.__uploadClicked++; document.getElementById('modal').style.display = 'block'; }}
        function closeModal() {{ document.getElementById('modal').style.display = 'none'; }}
        document.getElementById('InputUploader').addEventListener('change', function () {{
          document.querySelector("input[value='Add Document']").disabled = false;
        }});
        function addDoc() {{
          var name = document.getElementById('PatientFile__DocumentName').value;
          window.__added.push(name);
          var tr = document.createElement('tr'); tr.className = 'Row';
          var td = document.createElement('td'); td.className = 'v-align-top';
          td.innerText = name + '\\nPDF 1KB'; tr.appendChild(td);
          document.getElementById('docs').appendChild(tr);
          closeModal();
        }}
      </script>
    </body></html>"""


async def _upload_on(html, name, check_existing=True, mime="application/pdf", label=None):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-http2"])
        ctx = await browser.new_context()
        page = await ctx.new_page()

        async def route(r):
            await r.fulfill(status=200, content_type="text/html", body=html)

        await ctx.route(re.compile(r"https://www\.therapynotes\.com/.*"), route)
        await page.goto(CHART_URL, wait_until="domcontentloaded")
        ex = _bare_executor(page)
        ex.STEP_TIMEOUT_MS = 2000

        async def _no_dialogs():
            return False

        ex._dismiss_blocking_dialogs = _no_dialogs

        async def _click(target, label="element"):
            await target.click()

        ex._safe_click = _click
        suffix = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}[mime]
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.write(fd, b"%PDF-1.4 zz" if mime == "application/pdf" else b"\x89PNG\r\n\x1a\nzz")
        os.close(fd)
        try:
            ok = await ex._upload_pdf_to_patient(
                CHART_URL, path, name, TNPhaseV2.UPLOAD_DOCUMENTS, "document_upload_failed",
                check_existing=check_existing, log_label=label, mime_type=mime,
            )
        finally:
            os.unlink(path)
        clicked = await page.evaluate("window.__uploadClicked")
        added = await page.evaluate("window.__added")
        await browser.close()
        return ok, getattr(ex, "_last_upload_outcome", None), clicked, added, ex._pending_failure


SURVEY = "Client Survey 2026-09-28 (Sub 12)"


async def test_e_survey_timeout_retry_does_not_file_a_second_copy():
    """
    The agent_timeout case: the first run uploaded, the CRM gave up waiting and
    recorded a failure, the batch retries the next night. The survey is already
    on the chart under its exact name, so nothing is uploaded and the attach
    reports success.
    """
    ok, outcome, clicked, added, _ = await _upload_on(_documents_tab(["Intake Referral", SURVEY]), SURVEY)
    assert ok is True and outcome == "already_on_chart"
    assert clicked == 0 and added == [], "a second copy was uploaded"


async def test_e_a_longer_name_is_not_mistaken_for_it():
    ok, outcome, clicked, added, _ = await _upload_on(
        _documents_tab(["Client Survey 2026-09-28 (Sub 123)"]), SURVEY)
    assert clicked == 1 and added == [SURVEY], "a different survey blocked this one"
    assert ok is True and outcome == "uploaded"


async def test_e_crm_document_resent_after_a_lost_verdict_is_not_duplicated(caplog):
    caplog.set_level(logging.INFO)
    name = f"{SENTINEL_NAME} (CRM)"
    ok, outcome, clicked, added, _ = await _upload_on(
        _documents_tab([name]), name, label="CRM document 5")
    assert ok and outcome == "already_on_chart" and clicked == 0
    assert not any(SENTINEL_NAME in r.getMessage() for r in caplog.records)
    assert any("CRM document 5 already on the chart" in r.getMessage() for r in caplog.records)


async def test_e_a_new_document_uploads_and_is_confirmed_by_its_exact_row(caplog):
    caplog.set_level(logging.INFO)
    name = f'{SENTINEL_NAME} "front page" (CRM)'   # a quote breaks :has-text; exact match must not
    ok, outcome, clicked, added, failure = await _upload_on(
        _documents_tab(["Intake Referral", f"{SENTINEL_NAME} (CRM 2)"]), name, label="CRM document 6")
    assert ok is True and outcome == "uploaded", failure
    assert added == [name]
    assert not any(SENTINEL_NAME in r.getMessage() for r in caplog.records)


async def test_e_image_upload_goes_through_when_the_picker_allows_it():
    ok, outcome, clicked, added, failure = await _upload_on(
        _documents_tab([]), "Custody photo (CRM)", mime="image/png", label="CRM document 7")
    assert ok and outcome == "uploaded" and added == ["Custody photo (CRM)"], failure


async def test_e_picker_that_refuses_the_type_is_respected():
    ok, outcome, clicked, added, failure = await _upload_on(
        _documents_tab([], accept=".pdf"), "Custody photo (CRM)", mime="image/png", label="CRM document 8")
    assert ok is False and outcome == "unsupported"
    assert added == [], "an unsupported file was submitted"
    assert failure["reason"] == "document_unsupported_type"


async def test_e_create_flow_skips_the_check_on_its_brand_new_chart():
    ok, outcome, clicked, added, _ = await _upload_on(
        _documents_tab([]), "Intake Referral", check_existing=False)
    assert ok and outcome == "uploaded" and clicked == 1


async def test_e_the_survey_attach_path_keeps_the_pre_check():
    """Structural: survey attach must not opt out of the check."""
    import pathlib
    src = pathlib.Path("services/api/survey_attach_executor.py").read_text()
    call = src[src.index("self._mech._upload_pdf_to_patient("):]
    call = call[: call.index(")") + 1]
    assert "check_existing=False" not in call
    v2 = pathlib.Path("services/api/tn_executor_v2.py").read_text()
    assert "check_existing: bool = True" in v2, "the default must be to check"
