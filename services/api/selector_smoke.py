"""
Selector smoke: five signals that prove TherapyNotes still has the layout every
flow depends on, checked with ONE login a day at 03:00 Mountain.

Why: on 13-14 October a post-login page (an expired password) stopped every
flow, and staff saw a day of unrelated-looking refusals — dashboard_not_loaded,
navigation_failed, search_ui_not_found, login_failed. One line a day that says
"TN layout ok" or "TN layout changed: <signal>" turns that into one fact.

THE FIVE SIGNALS, in order (a signal after the first failure is not reached):
  dashboard         login reached the dashboard (tn_selectors.DASHBOARD_MARKERS,
                    no blocking page) — the shared _phase_login, unchanged
  patients_page     the Patients link was found and the list loaded — the
                    shared _phase_navigate, unchanged (it also proves the
                    "+ New Patient" button is there)
  search_box        the Patients-page search box is visible
  chart_tab         the practice's test record opens and its Clinicians tab is
                    visible
  new_patient_form  "+ New Patient" opens the form (first-name field visible)

READ-ONLY. It types only the test record's name into the search box. It
clicks the search button, the test record's own result link and "+ New
Patient", which opens an empty form that is never filled or saved; the
browser closes on it. It saves nothing, opens no other chart and never touches
the password page beyond reading it.

PHI. Logs and results carry signal names, counts and TherapyNotes' own UI
words. The test record is chosen by comparing names inside the page; no name
leaves it.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Literal, Optional

from pydantic import BaseModel, Field

from services.api import tn_selectors as S

logger = logging.getLogger(__name__)

SIGNALS = ("dashboard", "patients_page", "search_box", "chart_tab", "new_patient_form")

# The practice's test record (also used by the count pass: TEST_ANNA_SEARCH).
TEST_RECORD_SEARCH = "Test Anna"
_TEST_RECORD_KEY = "anna test"  # its name's words, sorted, lower-case

SMOKE_HOUR_MT = 3  # 03:00 America/Denver, daily
NEW_PATIENT_FIRST_NAME = "#PatientInformationEditor__FirstNameInput"

_PICK_TEST_RECORD_JS = r"""
([sel, key]) => {
  const norm = s => (s || '').toLowerCase().replace(/[^a-z ]/g, ' ').split(/\s+/).filter(Boolean).sort().join(' ');
  const hits = [...document.querySelectorAll(sel)].map((a, i) => norm(a.innerText) === key ? i : -1).filter(i => i >= 0);
  return hits.length === 1 ? hits[0] : -hits.length - 1;
}
"""


class SelectorSmokeResult(BaseModel):
    status: Literal["pass", "fail"]
    checked_at: str
    signals: Dict[str, Optional[bool]] = Field(
        ..., description="True passed, False failed, None not reached (an earlier signal failed).")
    first_failed: Optional[str] = None
    failure_reason: Optional[str] = None
    password_expires_in_days: Optional[int] = None
    message: str
    duration_ms: int = 0


def summarise(signals: Dict[str, Optional[bool]], *, failure_reason: Optional[str] = None,
              password_expires_in_days: Optional[int] = None,
              duration_ms: int = 0) -> SelectorSmokeResult:
    """The one line staff read. Pure, so it is unit-tested without a browser."""
    full = {s: signals.get(s) for s in SIGNALS}
    first_failed = next((s for s in SIGNALS if full[s] is not True), None)
    if failure_reason == "password_expired":
        message = "TherapyNotes password expired — set a new one; every flow is stopped"
    elif first_failed:
        message = f"TN layout changed: {first_failed.replace('_', ' ')} not found"
    else:
        message = f"TN layout ok ({len(SIGNALS)}/{len(SIGNALS)})"
    if password_expires_in_days is not None:
        message += f"; TherapyNotes password expires in {password_expires_in_days} day(s)"
    return SelectorSmokeResult(
        status="fail" if first_failed else "pass",
        checked_at=datetime.now(timezone.utc).isoformat(),
        signals=full, first_failed=first_failed, failure_reason=failure_reason,
        password_expires_in_days=password_expires_in_days, message=message,
        duration_ms=duration_ms,
    )


LAST_RESULT: Optional[SelectorSmokeResult] = None


async def check_signals(mech, page) -> SelectorSmokeResult:
    """Run the five checks on a fresh page. `mech` is a TNExecutorV2 on `page`."""
    t0 = time.time()
    sig: Dict[str, Optional[bool]] = {}

    def done(reason: Optional[str] = None) -> SelectorSmokeResult:
        return summarise(sig, failure_reason=reason,
                         password_expires_in_days=getattr(mech, "password_expires_in_days", None),
                         duration_ms=int((time.time() - t0) * 1000))

    if not await mech._phase_entry() or not await mech._phase_login():
        sig["dashboard"] = False
        pending = getattr(mech, "_pending_failure", None) or {}
        return done(pending.get("reason"))
    sig["dashboard"] = True

    sig["patients_page"] = bool(await mech._phase_navigate())
    if not sig["patients_page"]:
        return done()

    sig["search_box"] = await _visible(page, S.PATIENTS["search_input"])
    if not sig["search_box"]:
        return done()

    sig["chart_tab"] = await _open_test_record_and_check_tab(page)
    if not sig["chart_tab"]:
        return done()

    sig["new_patient_form"] = await _new_patient_form_opens(page)
    return done()


async def _visible(page, selector: str, timeout_ms: int = 10_000) -> bool:
    try:
        await page.wait_for_selector(selector, state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def _open_test_record_and_check_tab(page) -> bool:
    try:
        await page.select_option(S.PATIENTS["activity_filter"], value="active", timeout=10_000)
        await page.select_option(S.PATIENTS["clinician_filter"], value="any", timeout=10_000)
        await page.fill(S.PATIENTS["search_input"], TEST_RECORD_SEARCH)
        await page.click(S.PATIENTS["search_submit"], timeout=10_000)
        await page.wait_for_load_state("domcontentloaded", timeout=20_000)
        await page.wait_for_selector(S.PATIENTS["result_anchor"], state="attached", timeout=20_000)
        await asyncio.sleep(1.0)
        idx = await page.evaluate(_PICK_TEST_RECORD_JS, [S.PATIENTS["result_anchor"], _TEST_RECORD_KEY])
        if idx < 0:
            logger.warning(f"[SMOKE] test record: {-idx - 1} exact match(es), expected 1")
            return False
        await page.locator(S.PATIENTS["result_anchor"]).nth(idx).click(timeout=10_000)
        await page.wait_for_load_state("domcontentloaded", timeout=20_000)
        if not await _visible(page, S.CHART["patient_name"][0], 20_000):
            return False
        return await _visible(page, S.chart_tab("Clinicians"))
    except Exception as e:
        logger.warning(f"[SMOKE] chart check failed: {type(e).__name__}")
        return False


async def _new_patient_form_opens(page) -> bool:
    try:
        await page.goto(S.PATIENTS_URL, wait_until="domcontentloaded", timeout=20_000)
        button = S.NAV["new_patient_button"][0]
        if not await _visible(page, button):
            return False
        await page.locator(button).first.click(timeout=10_000)
        return await _visible(page, NEW_PATIENT_FIRST_NAME, 20_000)
    except Exception as e:
        logger.warning(f"[SMOKE] new patient form check failed: {type(e).__name__}")
        return False


async def run_selector_smoke() -> SelectorSmokeResult:
    """One login, five signals, one log line. Serialised with every TN route."""
    global LAST_RESULT
    from services.api.config import get_tn_credentials
    from services.api.mcp_runtime import PlaywrightRuntime
    from services.api.tn_executor import _execution_lock
    from services.api.tn_executor_v2 import TNExecutorV2

    async with _execution_lock:
        runtime = PlaywrightRuntime(skip_proxy=True, skip_resource_blocking=True, skip_stealth=True)
        try:
            mech = TNExecutorV2(runtime, get_tn_credentials())
            mech._start_time = time.time()
            page = await runtime.ensure_browser()
            mech._page = page
            result = await check_signals(mech, page)
        except Exception as e:
            result = summarise({}, failure_reason="unknown_error")
            result.message = f"TN smoke could not run: {type(e).__name__}"
        finally:
            await runtime.close()

    log = logger.info if result.status == "pass" else logger.error
    log(f"[SMOKE] {result.message} | signals="
        + ",".join(f"{k}={'ok' if v else ('FAIL' if v is False else '-')}" for k, v in result.signals.items())
        + f" | {result.duration_ms}ms")
    LAST_RESULT = result
    return result


def seconds_until_next_run(now_utc: Optional[datetime] = None, hour: int = SMOKE_HOUR_MT) -> float:
    """Seconds from now to the next `hour`:00 America/Denver (DST-aware)."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Denver")
    now = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        # Next calendar day at the same wall-clock hour (re-localised for DST).
        nxt = (now + timedelta(days=1)).date()
        target = datetime(nxt.year, nxt.month, nxt.day, hour, tzinfo=tz)
    return (target - now).total_seconds()


async def daily_smoke_loop() -> None:
    """Started from the app lifespan. Never raises; a failed run waits for tomorrow."""
    while True:
        try:
            wait = seconds_until_next_run()
        except Exception as e:
            # 2026-10-15: no time zone data in the image killed this task
            # silently at startup. Say so, and retry in an hour.
            logger.error(f"[SMOKE] cannot schedule the daily smoke: {type(e).__name__}: {e}")
            await asyncio.sleep(3600)
            continue
        logger.info(f"[SMOKE] next selector smoke in {int(wait // 60)} min (03:00 America/Denver)")
        await asyncio.sleep(wait)
        try:
            await run_selector_smoke()
        except Exception as e:
            logger.error(f"[SMOKE] run crashed: {type(e).__name__}")
        await asyncio.sleep(60)  # never fire twice inside the same minute
