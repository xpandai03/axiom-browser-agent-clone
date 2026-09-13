"""
Read per-clinician ACTIVE CLIENT COUNTS out of TherapyNotes.

The first TherapyNotes path that only reads. One login, then the clinician
dropdown is walked and each option's displayed total is read off the page.

WHY THE COUNT IS READ FROM TEXT, NOT FROM ROWS
----------------------------------------------
The Patients page renders a running total above the table that reflects every
filter applied ("Displaying 1-20 of 47 active patients"). Reading that number
makes the table's paging irrelevant — which matters, because counting rows on
this page is actively unsafe: recon on 2026-09-13 measured 20 items per page
against 10 `tr.Row` matches, 14 against 7, 13 against 7. `tr.Row` matches only
the alternating half. Any design that counted rows would have silently halved
the denominator of a percentage the client reads as fact.
See docs/selectors/tn_v2_phases.md, "Recon: counting a provider's ACTIVE
clients (2026-09-13)".

READ-ONLY BY CONSTRUCTION, NOT BY INTENTION
-------------------------------------------
Intent is not a guarantee, so the guarantee is structural and enforced at the
point of action:

  * SELECTORS_COUNT names six elements. None of them saves, creates, schedules,
    submits a record, uploads or deletes. There is no selector in this module
    for any such control, so none can be reached by name.
  * `_click` refuses any selector outside CLICK_ALLOWLIST — the search submit
    and the pager's Next link — and raises ReadOnlyViolation otherwise.
    `_fill` refuses anything outside FILL_ALLOWLIST (the search box alone).
    Those two methods are the ONLY places this module acts on the page.
  * `_assert_on_patients_list` runs after every postback and raises if the URL
    ever leaves the Patients list — a patient record URL aborts the pass.
  * No result-row anchor is ever clicked, so no chart is ever opened. The row
    anchors are read in-browser for name matching and their hrefs are never
    followed.

PHI
---
No patient value crosses into this process. The count phrase is taken from an
element required to contain the word "Displaying", which no patient value does.
Test Anna matching happens INSIDE the browser and returns integers only.
"""

import asyncio
import logging
import re
import time
from typing import List, Optional, Tuple

from shared.schemas.active_count import (
    ActiveCountOutput,
    ActiveCountPhase,
    ActiveCountPhaseLog,
    ClinicianActiveCount,
)
from services.api.config import get_tn_credentials
from services.api.tn_executor import _execution_lock
from services.api.tn_executor_v2 import TNExecutorV2

logger = logging.getLogger(__name__)


class ReadOnlyViolation(RuntimeError):
    """Raised when this module is asked to act on anything outside its allowlist."""


# ============================================================================
# Selectors — recon-verified 2026-09-13. Nothing here is guessed.
# ============================================================================

PATIENTS_URL = "https://www.therapynotes.com/app/patients/"

SEL_ACTIVITY_FILTER = "select#ctl00_BodyContent_DropDownListSearchActivity"
SEL_CLINICIAN_FILTER = "select#ctl00_BodyContent_DropDownListSearchAssignment"
SEL_SEARCH_INPUT = "input#ctl00_BodyContent_TextBoxSearchPatientName"
SEL_SEARCH_SUBMIT = "input#ctl00_BodyContent_ButtonSearch"
SEL_RESULTS_CONTAINER = "div#DivPatientsList"
SEL_PAGER_NEXT = "a#DynamicTablePagingLink.Next"

SELECTORS_COUNT = {
    "activity_filter": SEL_ACTIVITY_FILTER,
    "clinician_filter": SEL_CLINICIAN_FILTER,
    "search_input": SEL_SEARCH_INPUT,
    "search_submit": SEL_SEARCH_SUBMIT,
    "results_container": SEL_RESULTS_CONTAINER,
    "pager_next": SEL_PAGER_NEXT,
}

# The ONLY two things this module may click. Both are navigation within the
# Patients list; neither can commit a change.
CLICK_ALLOWLIST = frozenset({SEL_SEARCH_SUBMIT, SEL_PAGER_NEXT})

# The ONLY field this module may type into.
FILL_ALLOWLIST = frozenset({SEL_SEARCH_INPUT})

# The activity filter value whose total is the client's "active clients".
ACTIVITY_ACTIVE = "active"

# Dropdown option that is the practice-wide aggregate rather than a person.
OPTION_ANY = "any"

TEST_ANNA_SEARCH = "Test Anna"


# ============================================================================
# Count parsing — pure, so it is unit-testable without a browser
# ============================================================================
#
# Recon saw only the plural range form. The others below are defensive: the
# wording differs for one patient, for zero, and for a filtered subset, and
# guessing a number when the text is unfamiliar is the one failure mode that
# corrupts the client's percentage invisibly.

# "Displaying 1-20 of 936 active patients"  ->  936
_RE_RANGE = re.compile(
    r"displaying\s+[\d,]+\s*(?:[-–—]|to)\s*[\d,]+\s+of\s+([\d,]+)", re.I
)
# "Displaying 1 of 1 active patient"  ->  1
_RE_OF = re.compile(r"displaying\s+[\d,]+\s+of\s+([\d,]+)", re.I)
# "Displaying all 43 patients"  ->  43
_RE_ALL = re.compile(r"displaying\s+all\s+([\d,]+)\b", re.I)
# "Displaying 1 patient" / "Displaying 7 active patients"  ->  1 / 7
_RE_PLAIN = re.compile(
    r"displaying\s+([\d,]+)\s+(?:[a-z][a-z\-]*\s+){0,4}?patients?\b", re.I
)
# "No active patients found" / "There are no patients to display"  ->  0
_RE_ZERO = re.compile(r"\bno\s+(?:[a-z][a-z\-]*\s+){0,3}?patients?\b", re.I)
# "0 patients" -> 0
_RE_ZERO_NUM = re.compile(r"\b0\s+(?:[a-z][a-z\-]*\s+){0,3}?patients?\b", re.I)

# "Displaying all of Nona Bockius's active patients."  ->  NO NUMBER AT ALL.
#
# Found on the first live pass, not in recon: when a clinician's set fits on one
# page TherapyNotes stops printing a total and just says "all". Two of 33
# clinicians hit this, and both correctly refused to parse rather than inventing
# a number — but a refusal leaves those providers with no denominator, so the
# count has to come from somewhere.
#
# This is the ONE case where counting rows is sound. "All" is the page telling us
# the set is complete, and truncation is the only thing that makes row counting
# dangerous. It is still counted via the result ANCHOR, never `tr.Row`, and the
# caller additionally requires that no pager be present before trusting it.
_RE_ALL_NO_NUMBER = re.compile(r"displaying\s+all\s+of\b", re.I)


def is_all_without_number(text: Optional[str]) -> bool:
    """True for the "Displaying all of <name>'s active patients." wording."""
    if not text:
        return False
    t = " ".join(text.split())
    # Only when no number could be parsed — "Displaying all 43 patients" is a
    # different, numbered wording and must go down the normal path.
    return bool(_RE_ALL_NO_NUMBER.search(t)) and parse_active_count(t) is None


def parse_active_count(text: Optional[str]) -> Optional[int]:
    """
    Pull the total out of the Patients page's "Displaying ..." text.

    Returns the integer, or None when the text cannot be read confidently.
    None means FAILURE upstream — never a substituted zero. A wrong denominator
    silently inflates a percentage the client reads as fact, so an unparseable
    string must not be allowed to look like an answer.

    Ordered most-specific first: the range form appears in the same string as
    smaller numbers ("1-20 of 936"), so it must win before any single-number
    pattern gets a chance at the "1".
    """
    if not text:
        return None
    t = " ".join(text.split())

    for pattern in (_RE_RANGE, _RE_OF, _RE_ALL, _RE_PLAIN):
        m = pattern.search(t)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                return None

    # Zero LAST: only an explicit "no patients" wording counts, never an absence
    # of other matches. An unrecognised string is a failure, not a zero.
    if _RE_ZERO.search(t) or _RE_ZERO_NUM.search(t):
        return 0
    return None


# ============================================================================
# In-browser probes — these return counts and metadata ONLY
# ============================================================================

# Read the counter phrase. Requires the element to be a LEAF and to contain
# "Displaying" (or an explicit zero wording), which is what guarantees a patient
# name can never be returned by this probe.
_COUNT_TEXT_JS = r"""
() => {
  const wanted = /displaying/i;
  const zero   = /\bno\s+(?:[a-z][a-z\-]*\s+){0,3}?patients?\b/i;
  const scope  = document.querySelector('div#DivPatientsList') || document.body;
  const leaves = [...scope.querySelectorAll('span,div,td,p,b,strong,h1,h2,h3')]
                   .filter(e => e.children.length === 0);
  for (const el of leaves) {
    const t = (el.innerText || '').trim();
    if (wanted.test(t)) return { found: true, text: t.slice(0, 200) };
  }
  for (const el of leaves) {
    const t = (el.innerText || '').trim();
    if (zero.test(t)) return { found: true, text: t.slice(0, 200) };
  }
  return { found: false, text: null };
}
"""

# Count Test Anna rows. Name comparison happens HERE; only integers are returned.
# Rows are located by the result anchor rather than by `tr.Row`, which matches
# only every other row (recon 2026-09-13).
_TEST_ANNA_JS = r"""
() => {
  const links = [...document.querySelectorAll(
    "#PatientSearchTableList a[data-testid='patient-search-patient-link']")];
  let exact = 0, both = 0;
  for (const a of links) {
    const n = ((a.innerText || '').trim().toLowerCase()
                .replace(/,/g, ' ').replace(/\s+/g, ' ')).trim();
    if (n === 'test anna' || n === 'anna test') exact++;
    else if (n.includes('test') && n.includes('anna')) both++;
  }
  return { rows: links.length, exact, both,
           hasNext: !!document.querySelector('a#DynamicTablePagingLink.Next') };
}
"""

# Count result rows via the ANCHOR (never `tr.Row`, which matches only every other
# row) and report whether a pager exists. Used ONLY for the "Displaying all ..."
# wording, where the page has told us the set is complete.
_ROW_COUNT_JS = r"""
() => ({
  rows: document.querySelectorAll(
    "#PatientSearchTableList a[data-testid='patient-search-patient-link']").length,
  hasPager: !!document.querySelector('a#DynamicTablePagingLink'),
})
"""

# Enumerate the clinician dropdown. Values and labels only.
_OPTIONS_JS = r"""
(sel) => {
  const s = document.querySelector(sel);
  if (!s) return null;
  return [...s.options].map(o => ({ value: o.value, text: (o.text || '').trim() }));
}
"""


class ActiveCountExecutor:
    """One login, then a read per clinician. Nothing is written."""

    STEP_TIMEOUT_MS = 20_000
    SETTLE_MS = 900
    # Hard stop on pager following, so a misbehaving page cannot loop forever.
    MAX_PAGES = 25

    def __init__(self, runtime, credentials):
        self._runtime = runtime
        self._credentials = credentials
        self._page = None
        self._logs: List[ActiveCountPhaseLog] = []
        self._start_time: float = 0.0
        # Borrowed mechanics only — entry and login. Composition, never
        # inheritance: none of the create flow's decision logic is reachable.
        self._mech: Optional[TNExecutorV2] = None

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _elapsed_ms(self) -> int:
        return int((time.time() - self._start_time) * 1000)

    def _record(self, phase, status, message, phase_start=None) -> None:
        self._logs.append(
            ActiveCountPhaseLog(
                phase=phase,
                status=status,
                message=message,
                duration_ms=int((time.time() - (phase_start or self._start_time)) * 1000),
            )
        )

    # ------------------------------------------------------------------
    # The two act-on-the-page primitives. Everything else only reads.
    # ------------------------------------------------------------------

    async def _click(self, selector: str) -> None:
        """
        Click, but only something on the allowlist.

        This is what makes "no writes" structural rather than a promise: a future
        edit that tries to click a save, create or delete control raises here
        instead of doing it.
        """
        if selector not in CLICK_ALLOWLIST:
            raise ReadOnlyViolation(
                f"refusing to click {selector!r}: not in the read-only click allowlist"
            )
        await self._page.click(selector, timeout=self.STEP_TIMEOUT_MS)

    async def _fill(self, selector: str, value: str) -> None:
        if selector not in FILL_ALLOWLIST:
            raise ReadOnlyViolation(
                f"refusing to type into {selector!r}: not in the read-only fill allowlist"
            )
        await self._page.fill(selector, value, timeout=self.STEP_TIMEOUT_MS)

    def _assert_on_patients_list(self) -> None:
        """
        A patient RECORD url means a chart was opened. That must be impossible on
        this path, so treat it as a hard abort rather than carrying on.
        """
        url = self._page.url or ""
        if re.search(r"/patients/(?:edit|view|detail)/", url):
            raise ReadOnlyViolation(
                "navigation left the Patients list and reached a patient record"
            )

    async def _settle(self, timeout_ms: int = 6000) -> None:
        """
        Wait for the results list to be present again.

        Deliberately NOT "networkidle": TherapyNotes holds connections open, so
        networkidle never fires and every wait burns its entire timeout. A first
        live pass built that way spent 33.5s per clinician — ~18 minutes for the
        roster — while doing nothing. Waiting on the thing actually being read is
        both faster and a better test that the postback landed.
        """
        try:
            await self._page.wait_for_selector(
                SEL_RESULTS_CONTAINER, state="attached", timeout=timeout_ms
            )
        except Exception:
            pass
        await self._page.wait_for_timeout(self.SETTLE_MS)
        self._assert_on_patients_list()

    async def _click_and_wait(self, selector: str, timeout_ms: int = 20_000) -> None:
        """
        Click a navigation control and wait for the resulting document.

        A WebForms postback is a real navigation, so waiting for it is what makes
        the read deterministic: without it the counter phrase from the PREVIOUS
        clinician can still be on screen, and two clinicians with the same count
        would make that impossible to notice. Falls back to a plain settle if the
        page turns out not to navigate, so this is safe either way.
        """
        try:
            async with self._page.expect_navigation(
                wait_until="domcontentloaded", timeout=timeout_ms
            ):
                await self._click(selector)
        except ReadOnlyViolation:
            raise
        except Exception:
            # Either the click raced the navigation or there wasn't one.
            pass
        await self._settle(timeout_ms)

    # ------------------------------------------------------------------
    # Query mechanics
    # ------------------------------------------------------------------

    async def _run_query(self, clinician_value: str, search_term: str = "") -> None:
        """
        Apply the filters and submit.

        The explicit submit is REQUIRED, not belt-and-braces: setting a dropdown
        to the value it already holds fires no change event, so no postback runs
        and the page keeps showing its previous (or unsubmitted) state. A recon
        pass that omitted this read zeros and would have concluded no total
        exists. Search text is set LAST because a select-triggered postback can
        land after it otherwise.
        """
        await self._page.select_option(
            SEL_ACTIVITY_FILTER, value=ACTIVITY_ACTIVE, timeout=self.STEP_TIMEOUT_MS
        )
        await self._settle(4000)
        await self._page.select_option(
            SEL_CLINICIAN_FILTER, value=clinician_value, timeout=self.STEP_TIMEOUT_MS
        )
        await self._settle(4000)
        await self._fill(SEL_SEARCH_INPUT, search_term)
        await self._click_and_wait(SEL_SEARCH_SUBMIT)

    async def _read_count(self) -> Tuple[Optional[int], Optional[str]]:
        """(count, raw phrase). count is None when the phrase is absent or unreadable."""
        try:
            res = await self._page.evaluate(_COUNT_TEXT_JS)
        except Exception:
            return None, None
        if not res or not res.get("found"):
            return None, None
        text = res.get("text")
        return parse_active_count(text), text

    async def _count_test_anna(self) -> Tuple[Optional[int], Optional[int]]:
        """
        (exact, token-match) across every result page.

        Paged because the free-text search is an OR over tokens — "Test Anna"
        returns everyone named Test OR Anna — so the exact matches can sit beyond
        the first page. Returns (None, None) if anything goes wrong; the caller
        reports that as a Test Anna failure without touching the active count.
        """
        exact = both = 0
        try:
            for _ in range(self.MAX_PAGES):
                res = await self._page.evaluate(_TEST_ANNA_JS)
                exact += int(res.get("exact", 0))
                both += int(res.get("both", 0))
                if not res.get("hasNext"):
                    break
                await self._click_and_wait(SEL_PAGER_NEXT)
            return exact, both
        except Exception:
            return None, None

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    async def _enumerate_clinicians(self) -> Optional[List[dict]]:
        try:
            opts = await self._page.evaluate(_OPTIONS_JS, SEL_CLINICIAN_FILTER)
        except Exception:
            return None
        return opts

    async def _read_one(self, value: str, label: str) -> ClinicianActiveCount:
        """
        One option, fully isolated.

        Every failure mode here returns a row rather than raising, so one bad
        clinician cannot end the pass. ReadOnlyViolation is the deliberate
        exception: it means the page took us somewhere this route must never be,
        and that MUST abort rather than be recorded and continued past.
        """
        row = ClinicianActiveCount(
            option_value=value, label=label, is_aggregate=(value == OPTION_ANY)
        )
        try:
            await self._run_query(value, search_term="")
        except ReadOnlyViolation:
            raise
        except Exception as e:
            row.status = "failure"
            row.failure_reason = "option_select_failed"
            row.message = f"could not apply this clinician filter: {type(e).__name__}"
            return row

        count, text = await self._read_count()
        row.count_text = text
        if text is None:
            row.status = "failure"
            row.failure_reason = "count_text_not_found"
            row.message = "no 'Displaying ...' element on the page after the search"
            return row

        # "Displaying all of <name>'s active patients." carries no number. The
        # page is asserting the set is complete, so counting it is safe — but
        # only if there is genuinely no pager contradicting that.
        if count is None and is_all_without_number(text):
            try:
                res = await self._page.evaluate(_ROW_COUNT_JS)
            except Exception:
                res = None
            if res is not None and not res.get("hasPager"):
                count = int(res.get("rows", 0))
                row.message = "count taken from rows; page reported 'all' with no total"

        if count is None:
            row.status = "failure"
            row.failure_reason = "count_unparseable"
            row.message = "count text present but no total could be parsed from it"
            return row

        row.active_count = count
        row.status = "success"

        # Test Anna, within the SAME active filter so the two numbers compare.
        try:
            await self._run_query(value, search_term=TEST_ANNA_SEARCH)
            ex, bo = await self._count_test_anna()
            if ex is None:
                row.test_anna_status = "failure"
            else:
                row.test_anna_exact, row.test_anna_token_match = ex, bo
                row.test_anna_status = "success"
        except ReadOnlyViolation:
            raise
        except Exception:
            row.test_anna_status = "failure"

        return row

    async def execute(self) -> ActiveCountOutput:
        self._start_time = time.time()
        t0 = time.time()

        self._mech = TNExecutorV2(self._runtime, self._credentials)
        self._mech._start_time = self._start_time
        self._page = await self._runtime.ensure_browser()
        self._mech._page = self._page

        # --- entry + login: borrowed mechanics, one session for the whole pass -
        if not await self._mech._phase_entry():
            self._record(ActiveCountPhase.ENTRY, "failure", "practice code phase failed", t0)
            return ActiveCountOutput.failure(
                ActiveCountPhase.ENTRY, "practice_code_rejected",
                "Practice code phase failed", self._logs, self._elapsed_ms(),
            )
        self._record(ActiveCountPhase.ENTRY, "success", "practice code accepted", t0)

        t0 = time.time()
        if not await self._mech._phase_login():
            self._record(ActiveCountPhase.LOGIN, "failure", "login failed", t0)
            return ActiveCountOutput.failure(
                ActiveCountPhase.LOGIN, "login_failed",
                "Login failed", self._logs, self._elapsed_ms(),
            )
        self._record(ActiveCountPhase.LOGIN, "success", "logged in", t0)

        # --- navigate: ONCE. Every subsequent query is a postback on this page --
        t0 = time.time()
        try:
            await self._page.goto(PATIENTS_URL, wait_until="domcontentloaded",
                                  timeout=self.STEP_TIMEOUT_MS)
            await self._settle(8000)
            await self._page.wait_for_selector(SEL_CLINICIAN_FILTER,
                                               timeout=self.STEP_TIMEOUT_MS)
        except Exception as e:
            self._record(ActiveCountPhase.NAVIGATE, "failure", "Patients page not reached", t0)
            return ActiveCountOutput.failure(
                ActiveCountPhase.NAVIGATE, "patients_page_not_found",
                f"Patients page did not render: {type(e).__name__}",
                self._logs, self._elapsed_ms(),
            )
        self._record(ActiveCountPhase.NAVIGATE, "success", "Patients page loaded", t0)

        # --- enumerate the dropdown -------------------------------------------
        t0 = time.time()
        opts = await self._enumerate_clinicians()
        if opts is None:
            self._record(ActiveCountPhase.ENUMERATE, "failure", "dropdown missing", t0)
            return ActiveCountOutput.failure(
                ActiveCountPhase.ENUMERATE, "clinician_dropdown_missing",
                "The 'Assigned to' select was not present", self._logs, self._elapsed_ms(),
            )
        if not opts:
            self._record(ActiveCountPhase.ENUMERATE, "failure", "no options", t0)
            return ActiveCountOutput.failure(
                ActiveCountPhase.ENUMERATE, "no_clinicians_found",
                "The clinician dropdown rendered no options", self._logs, self._elapsed_ms(),
            )
        self._record(ActiveCountPhase.ENUMERATE, "success", f"{len(opts)} options", t0)
        logger.info(f"[COUNT] {len(opts)} dropdown options to read")

        # --- read every option -------------------------------------------------
        t0 = time.time()
        results: List[ClinicianActiveCount] = []
        for i, o in enumerate(opts, 1):
            value, label = o.get("value") or "", o.get("text") or ""
            if not value:
                continue
            try:
                row = await self._read_one(value, label)
            except ReadOnlyViolation:
                raise
            except Exception as e:
                row = ClinicianActiveCount(
                    option_value=value, label=label,
                    is_aggregate=(value == OPTION_ANY), status="failure",
                    failure_reason="unknown_error",
                    message=f"unhandled: {type(e).__name__}",
                )
            results.append(row)
            # Labels are staff names, not patient values.
            logger.info(
                f"[COUNT] {i}/{len(opts)} {label!r}: "
                f"{row.active_count if row.status == 'success' else 'FAILED ' + str(row.failure_reason)}"
            )

        ok = sum(1 for r in results if r.status == "success")
        self._record(
            ActiveCountPhase.READ_COUNTS,
            "success" if ok else "failure",
            f"{ok}/{len(results)} options read", t0,
        )
        return ActiveCountOutput.completed(results, self._logs, self._elapsed_ms())


async def run_active_count(runtime) -> ActiveCountOutput:
    """
    Entry point. Serialised against every other TherapyNotes run.

    The shared lock is the point: the practice holds ONE license and this job is
    meant to run overnight without competing with staff. If a staff-triggered run
    is in progress this declines immediately rather than queueing behind it.
    """
    if _execution_lock.locked():
        logger.warning("[COUNT] declined — another TherapyNotes execution is in progress")
        return ActiveCountOutput.failure(
            ActiveCountPhase.ENTRY, "unknown_error",
            "Another TherapyNotes execution is already in progress",
            [], 0,
        )

    async with _execution_lock:
        try:
            credentials = get_tn_credentials()
        except Exception as e:
            logger.error(f"[COUNT] credential validation failed: {e}")
            return ActiveCountOutput.failure(
                ActiveCountPhase.ENTRY, "login_failed",
                "Missing TherapyNotes credentials", [], 0,
            )
        executor = ActiveCountExecutor(runtime, credentials)
        return await executor.execute()
