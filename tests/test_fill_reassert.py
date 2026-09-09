"""
Synthetic verification for the Address 1 fill race.

WHY THIS FIXTURE SHAPE: the bug is a component that initialises AFTER the field
has been filled and writes its empty model back over the typed value. So these
pages CLEAR a field a short time after it receives input — they do not merely
render it slowly. A "slow field" fixture would pass against the broken code and
prove nothing; the previous suite in this repo made exactly that mistake.

Reproduces the 4 September signature: read-back returns "" immediately after a
successful fill().

All values below are synthetic.

Run:  <venv>/bin/python -m tests.test_fill_reassert
"""

import asyncio
import logging
import sys
import time

from playwright.async_api import async_playwright

from services.api.tn_executor_v2 import TNExecutorV2
from services.api.tn_executor import TNExecutor
from shared.schemas.therapy_notes_v2 import TNPhaseV2

ADDR_SEL = "#AddressEditorView__Address1Input_PatientAddress"
SENTINEL = "100 Zzsentinel Way"


def page_html(clear_times: int, reformat: bool = False, present: bool = True) -> str:
    """
    A field that clears itself the first `clear_times` times it is filled —
    the late-initialising component, reproduced.
    """
    if not present:
        return "<html><body><div>no address field here</div></body></html>"
    return f"""
    <html><body>
      <input id="AddressEditorView__Address1Input_PatientAddress">
      <script>
        window.__clears = 0;
        window.__fills = 0;
        const el = document.getElementById('AddressEditorView__Address1Input_PatientAddress');
        el.addEventListener('input', () => {{
          window.__fills++;
          if ({str(reformat).lower()}) {{
            // Legitimately reformats what was typed (NOT the race).
            setTimeout(() => {{ el.value = el.value.toUpperCase() + ' #REFORMATTED'; }}, 0);
            return;
          }}
          if (window.__clears < {clear_times}) {{
            window.__clears++;
            // The component's model initialising over the typed value.
            setTimeout(() => {{ el.value = ''; }}, 0);
          }}
        }});
      </script>
    </body></html>
    """


# A full New Patient form. Address 1 clears itself the first time it is filled;
# the zip blur populates city, as TherapyNotes does.
FULL_FORM_HTML = """
<html><body>
  <input id="PatientInformationEditor__FirstNameInput">
  <input id="PatientInformationEditor__LastNameInput">
  <input id="PatientInformationEditor__DOBInput">
  <input id="AddressEditorView__Address1Input_PatientAddress">
  <input id="AddressEditorView__PostalCodeInput_PatientAddress">
  <input id="AddressEditorView__CityInput_PatientAddress">
  <input type="radio" name="Sex" value="0"><input type="radio" name="Sex" value="1">
  <input id="PatientInformationEditor__EmailInput">
  <input id="PatientInformationEditor__MobilePhoneInput">
  <script>
    window.__CLEARS__ = 0;
    const addr = document.getElementById('AddressEditorView__Address1Input_PatientAddress');
    addr.addEventListener('input', () => {
      if (window.__CLEARS__ < 1) { window.__CLEARS__++; setTimeout(() => { addr.value = ''; }, 0); }
    });
    const zip = document.getElementById('AddressEditorView__PostalCodeInput_PatientAddress');
    zip.addEventListener('blur', () => {
      document.getElementById('AddressEditorView__CityInput_PatientAddress').value = 'Testcity';
    });
  </script>
</body></html>
"""


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            self.lines.append("<unformattable>")


class Results:
    def __init__(self):
        self.passed = self.failed = 0
        self.lines = []

    def check(self, name, cond, detail=""):
        detail = "" if detail == "" else str(detail)
        if cond:
            self.passed += 1
            print(f"  ok   {name}")
        else:
            self.failed += 1
            self.lines.append(f"{name}{' — ' + detail if detail else ''}")
            print(f"  FAIL {name}{' — ' + detail if detail else ''}")


class FakePatient:
    first_name, last_name = "Zzfirst", "Zzlast"
    dob = "01/02/1990"
    address = SENTINEL
    zip = "87101"
    sex = "Male"
    email = "zz@example.test"
    phone = "5550000000"


def make_executor(page, cls=TNExecutorV2):
    ex = object.__new__(cls)
    ex._page = page
    ex._logs = []
    ex._surfaced_overlays = []
    ex._overlays_reported = set()
    ex._save_problem_text = None
    ex._start_time = time.time()
    ex._pending_failure = {}

    async def _no_shot(_l):
        return None
    ex._capture_screenshot = _no_shot
    return ex


async def run_fill(browser, html, cls=TNExecutorV2, max_attempts=None):
    """Drive ONLY the fill helper, via the same code path Phase 4 uses."""
    page = await browser.new_page(viewport={"width": 1200, "height": 900})
    await page.set_content(html)
    ex = make_executor(page, cls)
    attempts = cls.FILL_MAX_ATTEMPTS if max_attempts is None else max_attempts

    # Rebuild the closure exactly as _phase_fill_required defines it.
    async def fill_and_confirm(selector, value, label, max_attempts=1):
        loc = page.locator(selector)
        if await loc.count() == 0:
            logging.getLogger("services.api.tn_executor_v2").error(
                f"[FILL] {label}: selector '{selector}' not found (count=0)")
            return False
        for attempt in range(1, max_attempts + 1):
            await loc.fill(value)
            actual = await loc.input_value()
            if actual == value:
                if attempt > 1:
                    logging.getLogger("services.api.tn_executor_v2").info(
                        f"[FILL] {label}: value held after re-assert (attempt {attempt}/{max_attempts})")
                logging.getLogger("services.api.tn_executor_v2").info(
                    f"[FILL] {label}: '{value}' confirmed")
                return True
            if actual == "" and value != "":
                if attempt < max_attempts:
                    logging.getLogger("services.api.tn_executor_v2").warning(
                        f"[FILL] {label}: cleared after fill (attempt {attempt}/{max_attempts}) — re-asserting")
                    await asyncio.sleep(cls.FILL_REASSERT_PAUSE_S)
                    continue
                logging.getLogger("services.api.tn_executor_v2").warning(
                    f"[FILL] {label}: still empty after {max_attempts} attempts — giving up")
                return False
            logging.getLogger("services.api.tn_executor_v2").warning(
                f"[FILL] {label}: read-back differs from the value typed (non-empty) "
                "— not a clear-after-fill, failing")
            return False
        return False

    t0 = time.time()
    ok = await fill_and_confirm(ADDR_SEL, SENTINEL, "Address 1", max_attempts=attempts)
    elapsed = time.time() - t0
    return page, ok, elapsed


async def main():
    r = Results()
    cap = _Capture()
    root = logging.getLogger()
    root.addHandler(cap)
    prev = root.level
    root.setLevel(logging.DEBUG)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()

            # ---------------------------------------------------------
            print("\n[A] Cleared once, holds on the re-assert -> SUCCESS")
            cap.lines.clear()
            page, ok, _ = await run_fill(browser, page_html(clear_times=1))
            r.check("fill succeeded", ok is True)
            r.check("field actually holds the value",
                    await page.input_value(ADDR_SEL) == SENTINEL)
            r.check("exactly one re-assert logged",
                    sum("re-asserting" in l for l in cap.lines) == 1,
                    [l for l in cap.lines if "re-assert" in l])
            r.check("logs which attempt it held on",
                    any("value held after re-assert (attempt 2/3)" in l for l in cap.lines),
                    [l for l in cap.lines if "held" in l])
            await page.close()

            # ---------------------------------------------------------
            print("\n[B] Cleared on EVERY attempt -> fails after the bounded retries")
            cap.lines.clear()
            page, ok, _ = await run_fill(browser, page_html(clear_times=99))
            r.check("fill failed", ok is False)
            r.check("stopped at the budget (2 re-asserts, then give up)",
                    sum("re-asserting" in l for l in cap.lines) == 2,
                    [l for l in cap.lines if "re-assert" in l])
            r.check("says it gave up",
                    any("still empty after 3 attempts" in l for l in cap.lines),
                    [l for l in cap.lines if "giving up" in l])
            await page.close()

            # ---------------------------------------------------------
            print("\n[C] Never cleared -> succeeds first time, NO re-assert, no extra delay")
            cap.lines.clear()
            page, ok, elapsed_clean = await run_fill(browser, page_html(clear_times=0))
            r.check("fill succeeded", ok is True)
            r.check("no re-assert logged",
                    not any("re-assert" in l for l in cap.lines),
                    [l for l in cap.lines if "re-assert" in l])
            r.check(f"no added delay (took {elapsed_clean*1000:.0f} ms, budget pause is "
                    f"{TNExecutorV2.FILL_REASSERT_PAUSE_S*1000:.0f} ms)",
                    elapsed_clean < TNExecutorV2.FILL_REASSERT_PAUSE_S,
                    f"{elapsed_clean:.3f}s")
            await page.close()

            # ---------------------------------------------------------
            print("\n[D] Read-back DIFFERS (non-empty) -> not this bug, fails immediately")
            cap.lines.clear()
            page, ok, _ = await run_fill(browser, page_html(clear_times=0, reformat=True))
            r.check("fill failed", ok is False)
            r.check("NO re-assert attempted",
                    not any("re-asserting" in l for l in cap.lines),
                    [l for l in cap.lines if "re-assert" in l])
            r.check("distinguishes it from a clear-after-fill",
                    any("not a clear-after-fill" in l for l in cap.lines),
                    [l for l in cap.lines if "differs" in l])
            await page.close()

            # ---------------------------------------------------------
            print("\n[E] Field absent -> fails promptly, retries never entered")
            cap.lines.clear()
            page, ok, elapsed_absent = await run_fill(browser, page_html(0, present=False))
            r.check("fill failed", ok is False)
            r.check("no re-assert attempted", not any("re-assert" in l for l in cap.lines))
            r.check(f"returned promptly ({elapsed_absent*1000:.0f} ms)",
                    elapsed_absent < TNExecutorV2.FILL_REASSERT_PAUSE_S, f"{elapsed_absent:.3f}s")
            r.check("logged as not-found",
                    any("not found (count=0)" in l for l in cap.lines))
            await page.close()

            # ---------------------------------------------------------
            print("\n[F] Cleared twice, holds on the third -> SUCCESS at the budget edge")
            cap.lines.clear()
            page, ok, _ = await run_fill(browser, page_html(clear_times=2))
            r.check("fill succeeded on the last allowed attempt", ok is True)
            r.check("two re-asserts logged",
                    sum("re-asserting" in l for l in cap.lines) == 2)
            r.check("held on attempt 3/3",
                    any("attempt 3/3" in l for l in cap.lines),
                    [l for l in cap.lines if "held" in l])
            await page.close()

            # ---------------------------------------------------------
            print("\n[G] Sentinel — the re-assert path logs no field value")
            cap.lines.clear()
            page, ok, _ = await run_fill(browser, page_html(clear_times=99))
            reassert_lines = [l for l in cap.lines
                              if "re-assert" in l or "giving up" in l or "still empty" in l
                              or "differs" in l]
            r.check("re-assert/give-up lines carry no value",
                    all(SENTINEL not in l for l in reassert_lines), reassert_lines)
            r.check("they do name the field", all("Address 1" in l for l in reassert_lines),
                    reassert_lines)
            await page.close()

            # ---------------------------------------------------------
            print("\n[H] V1 parity")
            cap.lines.clear()
            page, ok, _ = await run_fill(browser, page_html(clear_times=1), TNExecutor)
            r.check("[V1] recovers from a single clear", ok is True)
            r.check("[V1] same budget constant",
                    TNExecutor.FILL_MAX_ATTEMPTS == TNExecutorV2.FILL_MAX_ATTEMPTS == 3)
            await page.close()

            # ---------------------------------------------------------
            # THE IMPORTANT ONE. Cases [A]-[H] drive a reconstruction of the
            # helper, which would pass even if the shipped closure were wrong —
            # exactly how the previous suite in this repo passed against broken
            # code. This case drives the REAL _phase_fill_required end to end
            # against a full synthetic New Patient form whose Address 1 clears
            # itself once, so it exercises the code that actually ships.
            print("\n[I] REAL _phase_fill_required against a full form, Address 1 clearing once")
            cap.lines.clear()
            page = await browser.new_page(viewport={"width": 1200, "height": 900})
            await page.set_content(FULL_FORM_HTML)
            ex = make_executor(page)
            ok = await ex._phase_fill_required(FakePatient())
            r.check("the SHIPPED phase completed despite the clear", ok is True,
                    ex._pending_failure.get("message", "")[:160])
            r.check("Address 1 actually holds the value",
                    await page.input_value(ADDR_SEL) == SENTINEL)
            r.check("a re-assert was logged by the shipped code",
                    any("cleared after fill" in l and "re-asserting" in l for l in cap.lines),
                    [l for l in cap.lines if "re-assert" in l])
            r.check("phase recorded success", any(l.status == "success" for l in ex._logs))
            await page.close()

            print("\n[J] REAL phase, Address 1 clearing forever -> still fails, same reason code")
            cap.lines.clear()
            page = await browser.new_page(viewport={"width": 1200, "height": 900})
            await page.set_content(FULL_FORM_HTML.replace("__CLEARS__ < 1", "__CLEARS__ < 99"))
            ex = make_executor(page)
            ok = await ex._phase_fill_required(FakePatient())
            r.check("phase failed", ok is False)
            r.check("reason code unchanged (form_field_not_found)",
                    ex._pending_failure.get("reason") == "form_field_not_found",
                    str(ex._pending_failure.get("reason")))
            r.check("message unchanged (Could not fill Address 1)",
                    "Could not fill Address 1" in ex._pending_failure.get("message", ""),
                    ex._pending_failure.get("message", "")[:120])
            await page.close()

            await browser.close()
    finally:
        root.removeHandler(cap)
        root.setLevel(prev)

    print(f"\n{'PASS' if r.failed == 0 else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    if r.failed:
        print("\nFailures:")
        for line in r.lines:
            print(f"  - {line}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
