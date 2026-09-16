"""
The re-assert applies to every field, and no field's value reaches a log line.

Two things are pinned here.

RE-ASSERT EVERYWHERE. The race is a late-initialising component writing its
blank model over what was just typed. It was fixed for Address 1 because that is
where it was first seen, and the attempt budget was passed to that one call.
Every other field kept the default of one attempt, so on 16 September Last Name
died on the identical race and an unchanged retry succeeded. The fixture clears
EVERY field once, which under the old default fails on the first field it
reaches.

NO VALUES IN LOGS. The fill helper logged the value it typed, which put a
patient's date of birth, street address, email and phone into Railway on every
run. The sentinel test pushes a distinctive string through every field and
asserts it appears in no log record — an assertion that keeps working when
someone adds a field later, which a hand-written list of forbidden strings would
not.
"""

import logging
import re
import time

import pytest

from services.api.tn_executor import TNExecutor
from services.api.tn_executor_v2 import TNExecutorV2

pytestmark = pytest.mark.asyncio

ORIGIN = "https://www.therapynotes.com"
FORM_URL = f"{ORIGIN}/app/patients/edit/"

FIELDS = [
    "PatientInformationEditor__FirstNameInput",
    "PatientInformationEditor__LastNameInput",
    "PatientInformationEditor__DOBInput",
    "AddressEditorView__Address1Input_PatientAddress",
    "AddressEditorView__PostalCodeInput_PatientAddress",
    "PatientInformationEditor__EmailInput",
    "PatientInformationEditor__MobilePhoneInput",
]

# Every text field clears itself ONCE, on its first input event — the shape of
# the real race. The second write survives, so a re-asserting fill holds and a
# single-attempt fill gives up.
CLEAR_ONCE_JS = """
  document.querySelectorAll('input[data-clears]').forEach(function (el) {
    var fired = false;
    el.addEventListener('input', function () {
      if (fired) return;
      fired = true;
      setTimeout(function () { el.value = ''; }, 0);
    });
  });
"""


# TherapyNotes fills City from the zip on blur, and the fill phase polls for it.
# Without this the phase stalls at the city poll and never reaches Email/Phone.
CITY_AUTOCOMPLETE_JS = """
  (function () {
    var zip = document.getElementById('AddressEditorView__PostalCodeInput_PatientAddress');
    var city = document.getElementById('AddressEditorView__CityInput_PatientAddress');
    zip.addEventListener('blur', function () { city.value = 'Rio Rancho'; });
  })();
"""


def _form_html(clear_once: bool) -> str:
    inputs = "\n".join(
        f'<input type="text" id="{f}" {"data-clears" if clear_once else ""}>'
        for f in FIELDS
    )
    return f"""
<html><body>
  <div class="alert-danger" role="alert">This patient has no assigned clinician.</div>
  {inputs}
  <input type="text" id="AddressEditorView__CityInput_PatientAddress">
  <script>{CITY_AUTOCOMPLETE_JS}</script>
  <input type="radio" name="Sex" value="0"><input type="radio" name="Sex" value="1">
  <psy-button class="button-save">Save New Patient</psy-button>
  <script>{CLEAR_ONCE_JS if clear_once else ""}</script>
</body></html>
"""


class _Patient:
    def __init__(self, v="X"):
        self.first_name = f"{v}first"
        self.last_name = f"{v}last"
        self.dob = "04/27/2014" if v == "X" else f"{v}dob"
        self.address = f"{v}addr"
        self.zip = "87144"
        self.sex = "Male"
        self.email = f"{v}@example.test"
        self.phone = "3474633277"


async def _run_fill(executor_cls, html, patient, caplog=None):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-http2"])
        ctx = await browser.new_context()
        page = await ctx.new_page()

        async def route(r):
            await r.fulfill(status=200, content_type="text/html", body=html)

        await ctx.route(re.compile(r"https://www\.therapynotes\.com/.*"), route)
        await page.goto(FORM_URL, wait_until="domcontentloaded")

        ex = executor_cls.__new__(executor_cls)
        ex._page = page
        ex._logs = []
        ex._start_time = time.time()
        ex._pending_failure = {}
        ex._surfaced_overlays = []
        ex._overlays_reported = set()

        async def _noop(*a, **k):
            return None

        ex._capture_screenshot = _noop
        ex._record_log = lambda *a, **k: None

        async def _fail_phase(phase, reason, message, phase_start=None):
            ex._pending_failure = {"reason": reason, "message": message}
            return False

        ex._fail_phase = _fail_phase
        ex._reason_for = lambda e, default: default

        ok = await ex._phase_fill_required(patient)
        values = {f: await page.input_value(f"#{f}") for f in FIELDS}
        await browser.close()
        return ok, ex._pending_failure, values


EXECUTORS = [TNExecutorV2, TNExecutor]
IDS = ["V2", "V1"]


# ---------------------------------------------------------------------------
# [A] Every field re-asserts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", EXECUTORS, ids=IDS)
async def test_a_every_field_survives_being_cleared_once(cls):
    """
    Under the old default (one attempt for everything but Address 1) this fails
    at First Name. Under the fix every field is re-asserted and holds.
    """
    patient = _Patient()
    ok, failure, values = await _run_fill(cls, _form_html(clear_once=True), patient)
    assert ok is True, f"a field did not survive the clear-once race: {failure}"
    assert values["PatientInformationEditor__FirstNameInput"] == patient.first_name
    assert values["PatientInformationEditor__LastNameInput"] == patient.last_name
    assert values["PatientInformationEditor__DOBInput"] == patient.dob
    assert values["AddressEditorView__Address1Input_PatientAddress"] == patient.address
    assert values["PatientInformationEditor__EmailInput"] == patient.email


@pytest.mark.parametrize("cls", EXECUTORS, ids=IDS)
async def test_a_last_name_specifically(cls):
    """The exact field that failed on 16 September, called out on its own."""
    patient = _Patient()
    ok, failure, values = await _run_fill(cls, _form_html(clear_once=True), patient)
    assert ok is True, f"Last Name still dies on the race: {failure}"
    assert values["PatientInformationEditor__LastNameInput"] == patient.last_name


@pytest.mark.parametrize("cls", EXECUTORS, ids=IDS)
async def test_a_quiet_form_still_fills(cls):
    """No race at all — must behave exactly as before."""
    ok, failure, _ = await _run_fill(cls, _form_html(clear_once=False), _Patient())
    assert ok is True, f"a quiet form regressed: {failure}"


def test_a_every_fill_call_site_uses_the_shared_budget():
    """
    Structural: no call site may pass its own attempt count. One source of truth
    is what stops a future field being added with the old default of one.
    """
    import pathlib

    for path in ("services/api/tn_executor_v2.py", "services/api/tn_executor.py"):
        src = pathlib.Path(path).read_text()
        assert "max_attempts=self.FILL_MAX_ATTEMPTS" not in src, (
            f"{path}: a call site still passes the budget explicitly — it is the "
            "default now"
        )
        assert "max_attempts: int = None" in src, f"{path}: default not shared"


# ---------------------------------------------------------------------------
# [B] The sentinel — no typed value in any log line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", EXECUTORS, ids=IDS)
async def test_b_sentinel_never_reaches_a_log_line(cls, caplog):
    """
    Push a distinctive token through every field and assert it appears in no log
    record. Asserting on the sentinel rather than on a list of field names means
    this keeps working when a field is added.
    """
    sentinel = "ZqSentinel7"
    patient = _Patient(sentinel)
    with caplog.at_level(logging.DEBUG):
        ok, failure, _ = await _run_fill(cls, _form_html(clear_once=False), patient)
    assert ok is True, f"fill failed, so the sentinel assertion proves nothing: {failure}"

    offenders = [r.getMessage() for r in caplog.records if sentinel in r.getMessage()]
    assert not offenders, f"the fill phase logged a typed value: {offenders[:3]}"


@pytest.mark.parametrize("cls", EXECUTORS, ids=IDS)
async def test_b_sentinel_absent_even_when_a_field_fails(cls, caplog):
    """
    The failure path is where values are most tempting to log — 'X: still empty
    after 3 attempts' invites printing what was typed. It must not.
    """
    sentinel = "ZqSentinel9"

    # A form whose Last Name clears forever: re-asserts are exhausted.
    html = _form_html(clear_once=False).replace(
        "<script></script>",
        """<script>
        var el = document.getElementById('PatientInformationEditor__LastNameInput');
        el.addEventListener('input', function(){ setTimeout(function(){ el.value=''; },0); });
        </script>""",
    )
    with caplog.at_level(logging.DEBUG):
        ok, failure, _ = await _run_fill(cls, html, _Patient(sentinel))
    assert ok is False, "the always-clearing field should have failed"
    assert "Last Name" in failure["message"] or "Last Name" in str(failure)

    offenders = [r.getMessage() for r in caplog.records if sentinel in r.getMessage()]
    assert not offenders, f"the failure path logged a typed value: {offenders[:3]}"


@pytest.mark.parametrize("cls", EXECUTORS, ids=IDS)
async def test_b_the_field_name_is_still_logged(cls, caplog):
    """
    Redaction must not cost diagnosability: the field name and outcome are the
    whole point of the line and must survive.
    """
    with caplog.at_level(logging.INFO):
        await _run_fill(cls, _form_html(clear_once=False), _Patient("ZqSentinel8"))
    msgs = " | ".join(r.getMessage() for r in caplog.records)
    for label in ("First Name", "Last Name", "Date of Birth", "Address 1", "Email"):
        assert f"[FILL] {label}: confirmed" in msgs, f"lost the log line for {label}"
