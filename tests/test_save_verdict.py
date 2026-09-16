"""
The save verdict: the URL decides, and nothing else.

WHY THESE FIXTURES LOOK LIKE THIS
---------------------------------
A save verification shipped on 4 September and was rolled back the same
afternoon. Its tests passed. They passed because the fixtures rendered the
error notice only AFTER a simulated refusal — so "an error is visible" looked
like a reliable refusal signal. On the real New Patient form that notice is
present from page load, and it satisfied the condition 3ms after the click,
failing saves that were still in flight.

So every fixture here renders the benign notice AT LOAD, before any click.
Case [C] is the one that would have caught the rolled-back build: the notice is
on screen the whole time and must not shorten the wait or decide anything.

Driven against the real `_phase_save_patient` on both executors — not a
reimplementation of it. A test that rebuilds the logic proves nothing about the
code that ships.
"""

import asyncio
import re
import time

import pytest

from services.api.tn_executor import TNExecutor, record_id_from_url
from services.api.tn_executor_v2 import TNExecutorV2

pytestmark = pytest.mark.asyncio

ORIGIN = "https://www.therapynotes.com"
FORM_URL = f"{ORIGIN}/app/patients/edit/"
RECORD_URL = f"{ORIGIN}/app/patients/edit/vEG9AQAAAADFgVEW/"

# The notice that broke the 4 September build. Present from load, on every page.
BENIGN_NOTICE = """
  <div class="alert-danger" role="alert">
    This patient has no assigned clinician. Assign one from the Clinicians tab.
  </div>
"""

PAGE = """
<html><body>
  {notice}
  <h2>New Patient</h2>
  <psy-button class="button-save" id="save">Save New Patient</psy-button>
  <script>
    document.getElementById('save').addEventListener('click', function () {{
      {on_click}
    }});
  </script>
</body></html>
"""


def _page_html(on_click: str, notice: str = BENIGN_NOTICE) -> str:
    return PAGE.format(notice=notice, on_click=on_click)


# Advance the URL after `ms`, the way a committed save does.
def _advance_after(ms: int) -> str:
    return (
        f"setTimeout(function(){{ history.pushState({{}},'','{RECORD_URL}'); "
        f"document.body.insertAdjacentHTML('beforeend','<p>Liam Blanco</p>'); }}, {ms});"
    )


class _Patient:
    """Minimal stand-in: _phase_save_patient only reads first/last name."""
    first_name = "Liam"
    last_name = "Blanco"


async def _run_save(executor_cls, html, monkeypatch_sleep=True):
    """Serve `html` from TherapyNotes' real origin and drive the shipped phase."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-http2"])
        ctx = await browser.new_context()
        page = await ctx.new_page()

        async def route(r):
            await r.fulfill(status=200, content_type="text/html", body=html)

        # Serve from the REAL origin so relative URLs and history.pushState
        # resolve exactly as they do live. set_content() would leave the page on
        # about:blank and every URL assertion would be meaningless.
        await ctx.route(re.compile(r"https://www\.therapynotes\.com/.*"), route)
        await page.goto(FORM_URL, wait_until="domcontentloaded")

        ex = executor_cls.__new__(executor_cls)
        ex._page = page
        ex._logs = []
        ex._start_time = time.time()
        ex._save_problem_text = None
        ex._pending_failure = {}
        ex._surfaced_overlays = []
        ex._overlays_reported = set()
        ex._screenshots = []

        async def _noop(*a, **k):
            return None

        ex._dismiss_blocking_dialogs = _noop
        ex._capture_screenshot = _noop
        ex._safe_click = lambda loc, label: loc.click()

        def _record_log(*a, **k):
            ex._logs.append(a)

        ex._record_log = _record_log

        async def _fail_phase(phase, reason, message, phase_start=None):
            ex._pending_failure = {"reason": reason, "message": message}
            return False

        ex._fail_phase = _fail_phase
        ex._reason_for = lambda e, default: default

        t0 = time.time()
        ok = await ex._phase_save_patient(_Patient())
        elapsed = time.time() - t0
        await browser.close()
        return ok, ex._pending_failure, elapsed


EXECUTORS = [TNExecutorV2, TNExecutor]
IDS = ["V2", "V1"]


# ---------------------------------------------------------------------------
# [A] A successful save: URL advances at ~2s (the observed figure)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", EXECUTORS, ids=IDS)
async def test_a_successful_save_continues(cls):
    ok, failure, elapsed = await _run_save(cls, _page_html(_advance_after(2000)))
    assert ok is True, f"a committed save was refused: {failure}"
    assert elapsed < 10, "should return as soon as the URL advances, not at the ceiling"


# ---------------------------------------------------------------------------
# [B] A slow but genuine save landing at eight seconds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", EXECUTORS, ids=IDS)
async def test_b_slow_save_still_succeeds(cls):
    ok, failure, elapsed = await _run_save(cls, _page_html(_advance_after(8000)))
    assert ok is True, f"an 8s save was wrongly refused: {failure}"
    assert elapsed >= 7.5, "must actually have waited for it"


# ---------------------------------------------------------------------------
# [C] A refused save — THE CASE THE ROLLED-BACK BUILD GOT WRONG
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", EXECUTORS, ids=IDS)
async def test_c_refused_save_fails_after_the_ceiling_not_before(cls):
    """
    Notice on screen from load, URL never advances, no error block.

    Two assertions, and the second is the point: it must fail, AND it must not
    fail early. An early failure means page content ended the wait — the exact
    defect that took the 4 September build down.
    """
    ok, failure, elapsed = await _run_save(cls, _page_html("/* no navigation */"))
    assert ok is False, "a save that never committed was reported as success"
    assert failure["reason"] == "save_failed"
    assert "URL never advanced" in failure["message"]
    ceiling_s = cls.SAVE_RECORD_URL_TIMEOUT_MS / 1000.0
    assert elapsed >= ceiling_s - 1.0, (
        f"failed after only {elapsed:.1f}s — something on the page shortened "
        f"the wait; the ceiling is {ceiling_s}s"
    )


# ---------------------------------------------------------------------------
# [D] Name-on-page never decides
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", EXECUTORS, ids=IDS)
async def test_d_name_absent_on_a_successful_save_still_continues(cls):
    """URL advances but the name is never written. Must still succeed."""
    on_click = (
        f"setTimeout(function(){{ history.pushState({{}},'','{RECORD_URL}'); }}, 1500);"
    )
    ok, failure, _ = await _run_save(cls, _page_html(on_click))
    assert ok is True, f"name-on-page gated the verdict: {failure}"


@pytest.mark.parametrize("cls", EXECUTORS, ids=IDS)
async def test_d_name_present_on_an_uncommitted_save_still_fails(cls):
    """
    The mirror case, and the more dangerous one: the name is on the page but the
    URL never advanced. That is precisely the unsaved New Patient form with the
    typed value still in the DOM. It must fail.
    """
    on_click = "document.body.insertAdjacentHTML('beforeend','<p>Liam Blanco</p>');"
    ok, failure, _ = await _run_save(cls, _page_html(on_click))
    assert ok is False, "name-on-page was allowed to stand in for a save"
    assert failure["reason"] == "save_failed"


# ---------------------------------------------------------------------------
# [E] No page content shortens the wait
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", EXECUTORS, ids=IDS)
async def test_e_an_error_block_appearing_mid_wait_does_not_shorten_it(cls):
    """
    A "Problems With Your Entry" block appears 200ms after the click and the URL
    advances at 6 seconds anyway. The block must not end the wait, and the save
    must succeed — a real refusal is identified by the URL, not by text.
    """
    on_click = (
        "setTimeout(function(){ document.body.insertAdjacentHTML('beforeend',"
        "'<div class=\"alert-danger\">Problems With Your Entry</div>'); }, 200);"
        + _advance_after(6000)
    )
    ok, failure, elapsed = await _run_save(cls, _page_html(on_click))
    assert ok is True, f"an error block decided the verdict: {failure}"
    assert elapsed >= 5.5, "the block shortened the wait"


# ---------------------------------------------------------------------------
# [F] The record-URL helper itself
# ---------------------------------------------------------------------------

def test_f_form_url_is_not_a_record():
    assert record_id_from_url(FORM_URL) is None
    assert record_id_from_url(f"{ORIGIN}/app/patients/") is None
    assert record_id_from_url(f"{ORIGIN}/app/patients/edit/?x=1") is None
    assert record_id_from_url("") is None
    assert record_id_from_url(None) is None


def test_f_record_url_yields_its_opaque_id():
    assert record_id_from_url(RECORD_URL) == "vEG9AQAAAADFgVEW"
    # Opaque, never numeric — which is why the old numeric extractor never matched.
    assert record_id_from_url(f"{ORIGIN}/app/patients/edit/Si29AQAAAADn2yHs/") == "Si29AQAAAADn2yHs"


# ---------------------------------------------------------------------------
# [G] A refused save must reach the CRM as `save failed`
# ---------------------------------------------------------------------------
# The CRM derives `patientCreated` from whether the save phase emitted ok:
# client/src/lib/tn-run-state.ts — `patientCreated = okPhases.has("save")`.
# So the amber "the record already exists, do not re-create" card is driven by
# this one event. On 16 September it said ok for a save that never committed.

async def test_g_refused_save_emits_failed_not_ok():
    emitted = []

    ex = TNExecutorV2.__new__(TNExecutorV2)
    ex._surfaced_overlays = []
    ex._overlays_reported = set()
    ex._pending_failure = {"reason": "save_failed", "message": "Save was not committed — ..."}

    async def _emit(phase, status, message, metadata=None):
        emitted.append((phase, status))

    ex._emit = _emit

    async def _refused():
        return False

    ok = await ex._step("save", "Saving patient", _refused(), "saved")
    assert ok is False
    assert ("save", "started") in emitted
    assert ("save", "failed") in emitted
    assert ("save", "ok") not in emitted, (
        "a refused save emitted ok — the CRM would set patientCreated true and "
        "tell staff not to re-create a patient that does not exist"
    )


async def test_g_committed_save_still_emits_ok():
    """The mirror: a genuine save must keep reporting ok, unchanged."""
    emitted = []
    ex = TNExecutorV2.__new__(TNExecutorV2)
    ex._surfaced_overlays = []
    ex._overlays_reported = set()

    async def _emit(phase, status, message, metadata=None):
        emitted.append((phase, status))

    ex._emit = _emit

    async def _committed():
        return True

    ok = await ex._step("save", "Saving patient", _committed(), "saved")
    assert ok is True
    assert ("save", "ok") in emitted
