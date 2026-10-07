"""
Attach a survey PDF to an EXISTING TherapyNotes patient chart.

Separate from the create-and-schedule executor by design. That flow is what
staff depend on daily; this must not be able to affect it. Only proven
MECHANICS are borrowed from TNExecutorV2 — entry, login, the PDF download and
the upload routine — by COMPOSITION. No decision logic is shared.

Sequence: entry -> login -> search -> select -> verify -> attach.

The verification is the point of the build. A survey filed to the wrong chart is
a PHI disclosure that cannot be undone; a refusal costs a staff member a minute.
So all four identity checks must pass, an unreadable field is a refusal, and
there is no tiebreak, scoring or closest-match anywhere in this file.

Selectors are those verified by recon on 2026-09-08 —
docs/selectors/tn_v2_phases.md, "Chart header selectors". Nothing here is
guessed at.
"""

import asyncio
import logging
from shared.phi_redaction import phi_scope_for, scrub_text
import os
import re
import time
from typing import List, Optional

from shared.name_keys import names_agree
from shared.schemas.survey_attach import (
    SurveyAttachInput,
    SurveyAttachOutput,
    SurveyAttachPhase,
    SurveyAttachPhaseLog,
)
from shared.schemas.therapy_notes_v2 import TNPhaseV2
from services.api.config import get_tn_credentials
from services.api.active_count_executor import (
    ACTIVITY_ACTIVE,
    OPTION_ANY,
    SEL_ACTIVITY_FILTER,
    SEL_CLINICIAN_FILTER,
    SEL_PAGER_NEXT,
    SEL_RESULTS_CONTAINER,
)
from services.api.tn_executor_v2 import (
    TNExecutorV2,
    _execution_lock,
    _name_tokens,
    _normalize_dob,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Selectors — all recon-verified. See docs/selectors/tn_v2_phases.md.
# ============================================================================

SELECTORS_ATTACH = {
    "patients_page_search_input":  ["input#ctl00_BodyContent_TextBoxSearchPatientName"],
    "patients_page_search_submit": ["input#ctl00_BodyContent_ButtonSearch"],
    # ROWS ARE LOCATED BY THE RESULT ANCHOR, NEVER BY `tr.Row`.
    #
    # `#PatientSearchTableList tr.Row` matches only the ALTERNATING half of the
    # table — recon measured 20 items against 10 `tr.Row`, 14 against 7, 13
    # against 7 (docs/selectors/tn_v2_phases.md, "tr.Row sees only HALF the
    # results"). On 21 September that cost a real attach: nine records came back
    # for the surname, five were evaluated, and the target sat on an even row and
    # was never looked at. The run refused with "No patient matched this name"
    # against a table that was showing the patient.
    #
    # The nightly pull was rewritten anchor-first for exactly this reason
    # (services/api/active_patients_executor.py:13). This route now matches it:
    # every anchor is a row, and the <tr> is reached by walking UP from the
    # anchor rather than by trusting a class that alternates.
    "patients_page_result_anchor": [
        "#PatientSearchTableList a[data-testid='patient-search-patient-link']"],
    "patients_page_name_link":     ["a[data-testid='patient-search-patient-link']"],
    # Chart, present on first load (no render delay observed):
    "chart_patient_name": ["div#PatientInformation__PatientName",
                           "span[data-testid='patientheaderview-patientname-container'] span"],
    "chart_dob":          ["span#PatientInformation__DOBElem",
                           "span[data-testid='patientheaderview-dob-container']"],
    # Phone: BOTH numbers are checked, and a match against either passes.
    # Verified on the chart 2026-09-09 — every phone id present is
    # PatientInformation__{Mobile,Home,Work,Other,Preferred}PhoneElem. When a
    # number is present its value sits in an <a> inside the container; when it is
    # absent the container EXISTS but is EMPTY. So reading the container's text
    # yields the number or "", and an absent field simply contributes nothing.
    #
    # Work/Other/Preferred also exist and are deliberately NOT checked: each extra
    # field widens what can satisfy the phone test, and a therapy intake gives a
    # mobile or a home number.
    "chart_phone_fields": ["div#PatientInformation__MobilePhoneElem",
                           "div#PatientInformation__HomePhoneElem"],
    # Chart, one hash-tab click away:
    "chart_clinicians_tab": ["a[href='#tab=Clinicians']"],
    "chart_clinicians":     [".clinician-assignments .clinician-assignment a"],
}

PATIENTS_URL = "https://www.therapynotes.com/app/patients/"

# A patient record URL names a specific record; the bare form URL does not.
_RECORD_URL_RE = re.compile(r"/patients/(?:edit|view|detail)/([^/?#]+)")


def clinician_for_comparison(name: Optional[str]) -> str:
    """
    The clinician name as compared: any parenthesised group dropped ("Tyra Jones
    (ABQ)" -> "Tyra Jones"), whitespace collapsed. The CRM already sends the
    TherapyNotes form (location dropped, scheduling alias applied); this keeps an
    older CRM's roster label from failing on its location code.
    """
    return " ".join(re.sub(r"\([^)]*\)", " ", name or "").split())


def clinician_on_chart(sent: Optional[str], assignments: List[str]) -> bool:
    """
    Scheduling's word rule, as membership: every word of the sent name appears
    in ONE of the chart's clinician assignments — case and punctuation
    insensitive, order-free, so "Ty Jones" is found in "Ty Jones", "Jones, Ty"
    or "Jones, Ty, LMHC". An empty name is never found.
    """
    want = set(_name_tokens(clinician_for_comparison(sent)))
    if not want:
        return False
    return any(want <= set(_name_tokens(a or "")) for a in assignments)


def clinician_refusal_message(sent: Optional[str], assignments: List[str]) -> str:
    """Both sides of the comparison, so a refusal can be judged real or not. Staff names only."""
    shown = ", ".join(f"'{a}'" for a in assignments) or "none"
    return (
        f"The clinician named on the survey ('{clinician_for_comparison(sent) or 'none'}') "
        f"is not among this patient's {len(assignments)} chart assignment(s): {shown}."
    )


def _digits(value: Optional[str]) -> str:
    """Phone comparison is on digits only — formatting varies on both sides."""
    return "".join(c for c in (value or "") if c.isdigit())


class SurveyAttachExecutor:
    STEP_TIMEOUT_MS = 15_000
    CLINICIAN_TAB_TIMEOUT_MS = 10_000

    # TherapyNotes pages the patient list at TWENTY rows (measured 2026-09-21:
    # "Displaying 1-20 of 936"). Truncation is no longer guessed from a full
    # page: the pager link is the signal, and pages are followed.
    PAGE_SIZE = 20

    # Result pages read before a search is treated as possibly truncated. Five
    # pages of 20 is 100 patients sharing a surname, well past any real one.
    MAX_SEARCH_PAGES = 5
    # The nightly pull's timings (ActiveCountExecutor): a postback may take a
    # while, and a settle after the results container appears.
    NAV_TIMEOUT_MS = 20_000
    SETTLE_MS = 900

    def __init__(self, runtime, credentials):
        self._runtime = runtime
        self._credentials = credentials
        self._page = None
        self._logs: List[SurveyAttachPhaseLog] = []
        self._start_time: float = 0.0
        self._pending: dict = {}
        # Borrowed mechanics live here. Composition, not inheritance: this class
        # never inherits the create flow's decision logic.
        self._mech: Optional[TNExecutorV2] = None

    # ------------------------------------------------------------------
    # Logging / result plumbing (own, not the create flow's)
    # ------------------------------------------------------------------

    def _elapsed_ms(self) -> int:
        return int((time.time() - self._start_time) * 1000)

    def _record(self, phase, status, message, phase_start=None) -> None:
        self._logs.append(SurveyAttachPhaseLog(
            phase=phase, status=status, message=scrub_text(message),
            duration_ms=int((time.time() - (phase_start or self._start_time)) * 1000),
        ))

    def _refuse(self, phase, reason, message, phase_start=None) -> bool:
        """Record a refusal. Messages name the FIELD, never the values."""
        logger.warning(f"[ATTACH] REFUSED at {phase.value}: {reason} — {message}")
        self._record(phase, "failure", message, phase_start)
        self._pending = {"phase": phase, "reason": reason, "message": message}
        return False

    def _build_failure(self, tn_patient_url=None) -> SurveyAttachOutput:
        return SurveyAttachOutput.failure(
            phase=self._pending.get("phase", SurveyAttachPhase.ENTRY),
            reason=self._pending.get("reason", "unknown_error"),
            message=scrub_text(self._pending.get("message", "Unknown failure")),
            logs=self._logs,
            duration_ms=self._elapsed_ms(),
            tn_patient_url=tn_patient_url,
            selection_mode=getattr(self, "_selection_mode", None),
        )

    async def _first(self, key: str):
        """First selector tier that matches something. Never raises."""
        for sel in SELECTORS_ATTACH[key]:
            try:
                loc = self._page.locator(sel)
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                continue
        return None

    async def _read_text(self, key: str) -> Optional[str]:
        """
        Read a chart field's text. Returns None when it cannot be read — and a
        field that cannot be read is a refusal, never a pass.
        """
        loc = await self._first(key)
        if loc is None:
            return None
        try:
            txt = (await loc.inner_text(timeout=3000) or "").strip()
        except Exception:
            return None
        return txt or None

    async def _collect_phone_digits(self) -> set:
        """
        Last-10 digits of every number the chart shows in a VERIFIED phone
        container. An empty container contributes nothing, so a patient who has
        one number and not the other can never fail on the missing one — absent
        is not a mismatch.

        Iterates every element matching each selector, not just the first: the
        chart carries a DUPLICATE PatientInformation__MobilePhoneElem id (invalid
        markup, but real), and querySelector would silently see only one of them.
        """
        found = set()
        for sel in SELECTORS_ATTACH["chart_phone_fields"]:
            try:
                loc = self._page.locator(sel)
                n = await loc.count()
            except Exception:
                continue
            for i in range(n):
                try:
                    txt = (await loc.nth(i).inner_text(timeout=2000) or "").strip()
                except Exception:
                    continue
                d = _digits(txt)
                if len(d) >= 10:
                    found.add(d[-10:])
        return found

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    async def _phase_search(self, data: SurveyAttachInput) -> bool:
        """
        Search the Patients page by SURNAME, with the filters set, and read every
        result page up to MAX_SEARCH_PAGES.

        SURNAME, NOT THE FULL NAME, and this is empirical. A live run searching
        "First Last" for a patient stored as "First Middle Last" returned four
        rows, none of them the target; the same record comes back on its surname
        alone. TherapyNotes' search is not a substring test, so a more specific
        query is not a narrower one, and it can miss WITHOUT returning zero rows,
        which is why there is no "full name first, surname if empty" fallback.
        Narrowing in _phase_select is exact (name reading + date of birth), so a
        broad query costs nothing in precision.

        THE FILTERS ARE SET, not inherited: Activity = Active and Assigned To =
        any clinician, the same values the nightly pull uses.

        THE RELOAD IS WAITED FOR. Search and the pager are WebForms postbacks,
        real navigations. This used to sleep 3.5s and count whatever was on
        screen, which reads a page that has not reloaded yet as "no results".
        It now waits the way the nightly pull does: the navigation, then the
        results container, then a short settle.

        EVERY PAGE, up to MAX_SEARCH_PAGES. A common surname used to refuse at a
        full first page. Now the pages are read and the decision is made over
        all of them; only a search that still has more pages after the cap is
        refused, as possibly truncated.
        """
        phase, t0 = SurveyAttachPhase.SEARCH, time.time()
        page = self._page
        await page.goto(PATIENTS_URL, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector(
                SELECTORS_ATTACH["patients_page_search_input"][0],
                state="attached", timeout=15_000,
            )
        except Exception:
            pass

        box = await self._first("patients_page_search_input")
        btn = await self._first("patients_page_search_submit")
        if box is None or btn is None:
            return self._refuse(phase, "search_ui_not_found",
                                "Patients-page search controls not found", t0)

        try:
            await page.select_option(SEL_ACTIVITY_FILTER, value=ACTIVITY_ACTIVE,
                                     timeout=self.NAV_TIMEOUT_MS)
            await self._settle(4000)
            await page.select_option(SEL_CLINICIAN_FILTER, value=OPTION_ANY,
                                     timeout=self.NAV_TIMEOUT_MS)
            await self._settle(4000)
        except Exception:
            return self._refuse(phase, "search_ui_not_found",
                                "Patients-page filters (Activity, Assigned To) not found", t0)

        await self._run_search(data.last_name)
        total = sum(len(p) for p in self._pages)
        self._row_count = total
        # Counts only.
        logger.info(
            f"[ATTACH] search mode=surname activity=active pages={len(self._pages)} "
            f"rows={total} more_pages={'yes' if self._truncated else 'no'}"
        )
        self._record(phase, "success",
                     f"Search returned {total} row(s) over {len(self._pages)} page(s)", t0)
        return True

    async def _settle(self, timeout_ms: int = 6000) -> None:
        """Wait for the results container, then a short settle. Never networkidle:
        TherapyNotes holds connections open (see ActiveCountExecutor._settle)."""
        try:
            await self._page.wait_for_selector(SEL_RESULTS_CONTAINER, state="attached",
                                               timeout=timeout_ms)
        except Exception:
            pass
        await self._page.wait_for_timeout(self.SETTLE_MS)

    async def _click_and_wait(self, selector: str) -> None:
        """Click a postback control and wait for the new document, as the nightly
        pull does (ActiveCountExecutor._click_and_wait)."""
        try:
            async with self._page.expect_navigation(wait_until="domcontentloaded",
                                                    timeout=self.NAV_TIMEOUT_MS):
                await self._page.click(selector, timeout=self.NAV_TIMEOUT_MS)
        except Exception:
            # The click raced the navigation, or there was none.
            pass
        await self._settle(self.NAV_TIMEOUT_MS)

    async def _read_page(self) -> dict:
        """
        This page's results: per row, the name cell's text, the date-of-birth
        cell's text and the href, plus whether a Next pager link exists.

        Three SCOPED strings per row and no decision: every decision is made in
        Python, once. The name read is the ANCHOR's text, never the row's, so a
        survey name cannot pass because its tokens appear in the clinician or
        payer column. Values are held in locals, compared and dropped; nothing
        is logged.
        """
        try:
            res = await self._page.evaluate(
                r"""
                ([anchorSel, nextSel]) => ({
                  rows: [...document.querySelectorAll(anchorSel)].map(a => {
                    const tr = a.closest('tr');
                    const dobEl = tr ? tr.querySelector("a[data-testid='patient-search-dob-link']") : null;
                    // Fallback when the row carries no date LINK: the first
                    // date-shaped run in the row. A date, never the row itself.
                    let fallback = "";
                    if (!dobEl && tr) {
                      const m = (tr.innerText || "").match(/\b\d{1,2}\/\d{1,2}\/\d{4}\b/);
                      fallback = m ? m[0] : "";
                    }
                    return {
                      name: (a.innerText || "").trim(),
                      dob:  dobEl ? (dobEl.innerText || "").trim() : fallback,
                      href: a.getAttribute('href') || "",
                    };
                  }),
                  hasNext: !!document.querySelector(nextSel),
                })
                """,
                [SELECTORS_ATTACH["patients_page_result_anchor"][0], SEL_PAGER_NEXT],
            )
        except Exception:
            res = None
        return res or {"rows": [], "hasNext": False}

    async def _run_search(self, term: str, stop_at_page: Optional[int] = None) -> List[dict]:
        """
        Submit the search and read result pages.

        With stop_at_page, stops ON that page (0-based) and returns its rows,
        leaving the browser there so a row on it can be clicked. Without it, reads
        up to MAX_SEARCH_PAGES into self._pages and sets self._truncated when a
        further page remains.
        """
        box = await self._first("patients_page_search_input")
        await box.click()
        await box.fill("")
        await box.fill(term)
        # A SEARCH submit, not a save-family control.
        await self._click_and_wait(SELECTORS_ATTACH["patients_page_search_submit"][0])

        pages: List[List[dict]] = []
        truncated = False
        while True:
            data = await self._read_page()
            if stop_at_page is not None and len(pages) == stop_at_page:
                self._current_page = stop_at_page
                return data["rows"]
            pages.append(data["rows"])
            if not data["hasNext"]:
                break
            if len(pages) >= self.MAX_SEARCH_PAGES:
                truncated = True
                break
            await self._click_and_wait(SEL_PAGER_NEXT)

        self._pages = pages
        self._truncated = truncated
        self._current_page = len(pages) - 1
        return pages[-1] if pages else []

    @staticmethod
    def _narrow(cells: List[dict], want_name: str, want_dob: Optional[str]):
        """(rows agreeing on name AND date of birth, count agreeing on name)."""
        matched, name_only = [], 0
        for c in cells:
            # ONE name rule, the same one the chart-header check and the CRM's
            # matcher use: every reading of a parenthesised name, matched on
            # intersection. "Minor (Rowan) Thistlewood" reads as both
            # "minor thistlewood" and "rowan thistlewood", so a survey carrying
            # the legal name agrees with it, and a middle name on one side only
            # still does not.
            if not names_agree(want_name, c.get("name")):
                continue
            name_only += 1
            row_dob = _normalize_dob(c.get("dob"))
            # Both sides must be READABLE and equal. `None == None` is not a
            # match: a row whose date cannot be read is never selected.
            if row_dob is not None and row_dob == want_dob:
                matched.append(c)
        return matched, name_only

    async def _phase_select(self, data: SurveyAttachInput) -> bool:
        """
        Narrow to EXACTLY ONE row on name + date of birth across every page read,
        then open that chart.

        BY NAME AND DATE OF BIRTH ONLY. A chart id the CRM sends is IGNORED.
        TherapyNotes' record id (the token in /app/patients/edit/<id>/) changes
        between page loads: the id the nightly pull captured is never the id a
        later search shows for the same patient, so selecting by it refused every
        time it was tried (2026-09-22 to 2026-10-01). Which chart is decided
        here; whether to attach is decided by _phase_verify, unchanged.
        """
        phase, t0 = SurveyAttachPhase.SELECT, time.time()
        self._selection_mode = "name"
        if data.expected_chart_id:
            logger.info("[ATTACH] expected_chart_id received and ignored; "
                        "selecting by name and date of birth")
        total = self._row_count

        if total == 0:
            return self._refuse(phase, "patient_not_found",
                                "No patient matched this name in TherapyNotes", t0)

        if self._truncated:
            return self._refuse(
                phase, "result_set_possibly_truncated",
                f"Search returned more than {self.MAX_SEARCH_PAGES} pages of results "
                f"({total} rows read), so there may be another identical match the "
                "agent did not see. Attach this document by hand.",
                t0,
            )

        want_dob = _normalize_dob(data.dob)
        want_name = f"{data.first_name} {data.last_name}"
        found = []          # (page index, row)
        name_only = 0
        for i, cells in enumerate(self._pages):
            m, n = self._narrow(cells, want_name, want_dob)
            found += [(i, c) for c in m]
            name_only += n

        # Counts only: no names, no dates.
        logger.info(
            f"[ATTACH] narrowing: total={total} pages={len(self._pages)} "
            f"name_matches={name_only} name+dob_matches={len(found)}"
        )

        if len(found) == 0:
            if name_only:
                return self._refuse(
                    phase, "patient_not_found",
                    f"{name_only} patient(s) matched the name but none matched the "
                    "date of birth given on the survey.", t0)
            return self._refuse(phase, "patient_not_found",
                                "No patient matched this name in TherapyNotes", t0)

        if len(found) > 1:
            return self._refuse(
                phase, "multiple_candidates",
                f"{len(found)} patients share this name AND date of birth. The "
                "search results carry nothing else to separate them; attach this "
                "document by hand after confirming which record is correct.", t0)

        page_index, row = found[0]
        if page_index != self._current_page:
            # The row is on an earlier page. Its href is from THAT page load and
            # will not be valid now, so return to the page and find the row again
            # by the same rule. It must still be the only match there.
            cells = await self._run_search(data.last_name, stop_at_page=page_index)
            again, _ = self._narrow(cells, want_name, want_dob)
            if len(again) != 1:
                return self._refuse(phase, "chart_not_opened",
                                    "The search results changed while the agent was "
                                    "reading them; attach this document by hand.", t0)
            row = again[0]

        m = _RECORD_URL_RE.search(row.get("href") or "")
        if not m:
            return self._refuse(phase, "chart_not_opened",
                                "Search result did not carry a patient record link", t0)
        pid = m.group(1)

        # Charts cannot be deep-linked; click the row's own anchor. Selected by
        # the anchor itself, so a target on an even-indexed row is clickable.
        await self._page.click(
            f"{SELECTORS_ATTACH['patients_page_result_anchor'][0]}[href*='{pid}']"
        )
        await asyncio.sleep(4)

        landed = _RECORD_URL_RE.search(self._page.url or "")
        if not landed or landed.group(1) != pid:
            return self._refuse(phase, "chart_not_opened",
                                "Clicking the result did not land on the intended patient chart", t0)

        self._chart_url = self._page.url
        self._record(phase, "success", "Opened the single matching patient chart", t0)
        return True

    async def _phase_verify(self, data: SurveyAttachInput) -> bool:
        """
        Four checks, all of which must pass. Any unreadable field refuses.

        Messages name the field and the outcome, never the values compared.
        """
        phase, t0 = SurveyAttachPhase.VERIFY, time.time()

        # --- name -------------------------------------------------------
        # SAME RULE AS THE RESULTS TABLE, AND AS THE CRM'S MATCHER: every reading
        # of a parenthesised name, matched on intersection. The chart header
        # renders a patient with a preferred name as "Preferred (Legal) Last", so
        # it reads as both, and a survey carrying either agrees with it.
        #
        # The element is already the NAME on its own — recon confirmed
        # div#PatientInformation__PatientName holds the value in one text node
        # with no children — so nothing more needs scoping here. What changes is
        # the test: this was a token SUBSET, which passed "Thistlewood" against
        # "Thistlewood-Smith" and passed a survey name that was missing a middle name
        # the chart carries. It is now an equality on a reading.
        chart_name = await self._read_text("chart_patient_name")
        if chart_name is None:
            return self._refuse(phase, "field_unreadable",
                                "Patient name could not be read from the chart", t0)
        if not names_agree(f"{data.first_name} {data.last_name}", chart_name):
            return self._refuse(phase, "name_mismatch",
                                "The name on the chart does not match the name on the survey", t0)

        # --- date of birth ----------------------------------------------
        chart_dob_raw = await self._read_text("chart_dob")
        if chart_dob_raw is None:
            return self._refuse(phase, "field_unreadable",
                                "Date of birth could not be read from the chart", t0)
        chart_dob = _normalize_dob(chart_dob_raw)
        want_dob = _normalize_dob(data.dob)
        if chart_dob is None:
            return self._refuse(phase, "field_unreadable",
                                "Date of birth on the chart is not in a readable date format", t0)
        if chart_dob != want_dob:
            return self._refuse(phase, "dob_mismatch",
                                "The date of birth on the chart does not match the survey", t0)

        # --- phone: mobile OR home -------------------------------------
        # Matching either is deliberate. Only the mobile was mapped before, so a
        # survey carrying a home number refused — safe, but wrong, and staff hit
        # it. An empty field contributes nothing to the set below, so having one
        # number and not the other cannot fail.
        chart_phones = await self._collect_phone_digits()
        if not chart_phones:
            return self._refuse(
                phase, "field_unreadable",
                "No phone number could be read from the chart (neither mobile nor "
                "home), so the survey's number cannot be checked against it", t0)
        if _digits(data.phone)[-10:] not in chart_phones:
            return self._refuse(
                phase, "phone_mismatch",
                f"The phone number on the survey matches neither number on the "
                f"chart ({len(chart_phones)} number(s) present)", t0)

        # --- clinician: membership, behind a tab -------------------------
        tab = await self._first("chart_clinicians_tab")
        if tab is None:
            return self._refuse(phase, "field_unreadable",
                                "Clinicians tab not found on the chart", t0)
        await tab.click()          # hash tab: a content swap, not a navigation or a write
        await asyncio.sleep(3)

        assignments = self._page.locator(SELECTORS_ATTACH["chart_clinicians"][0])
        try:
            n_assign = await assignments.count()
        except Exception:
            n_assign = 0
        if n_assign == 0:
            return self._refuse(
                phase, "clinician_unassigned",
                "This patient has no clinician assignments on the chart, so the "
                "therapist named on the survey cannot be confirmed.", t0)

        # DELIBERATELY NOT names_agree(). This is not one of the three PATIENT
        # name comparisons; it is a clinician, and TherapyNotes renders an
        # assignment as "Last, First, Credential". The credential is an extra
        # token that the survey never carries, so an equality on a reading would
        # refuse every correctly-assigned patient. Subset is the right test here
        # and is left exactly as it was.
        # The chart renders each assignment as the clinician's name (recon,
        # docs/selectors/tn_v2_phases.md); the word rule also covers "Last,
        # First[, Credential]". Staff names only — read, compared, and quoted
        # in a refusal so it can be judged real.
        try:
            rendered = [" ".join((t or "").split())[:80]
                        for t in await assignments.all_inner_texts()][:10]
        except Exception:
            rendered = []
        hit = clinician_on_chart(data.clinician_name, rendered)
        logger.info(
            f"[ATTACH] clinician check: sent='{clinician_for_comparison(data.clinician_name)}' "
            f"chart={rendered} present={hit}"
        )
        if not hit:
            return self._refuse(
                phase, "clinician_mismatch",
                clinician_refusal_message(data.clinician_name, rendered), t0)

        logger.info("[ATTACH] verification passed: name, date of birth, mobile phone, clinician")
        self._record(phase, "success",
                     "Verified name, date of birth, mobile phone and clinician", t0)
        return True

    async def _phase_attach(self, data: SurveyAttachInput) -> bool:
        """Download the PDF and upload it via the create flow's proven routine."""
        phase, t0 = SurveyAttachPhase.ATTACH, time.time()
        pdf_path = None
        try:
            try:
                pdf_path = await self._mech._download_pdf_to_tempfile(data.pdf_url)
            except Exception as e:
                reason = ("pdf_unsupported_format"
                          if e.__class__.__name__ == "PdfFormatError" else "pdf_download_failed")
                return self._refuse(phase, reason,
                                    f"Could not fetch the survey PDF: {str(e)[:140]}", t0)

            # Reused UNCHANGED. Its own failure detail lands in the mechanics
            # executor's logs; the outcome is what this route acts on.
            ok = await self._mech._upload_pdf_to_patient(
                self._chart_url, pdf_path, data.document_name,
                TNPhaseV2.UPLOAD_INTAKE_PDF, "intake_pdf_upload_failed",
            )
            if not ok:
                # TNExecutorV2 only sets _pending_failure inside _fail_phase, so
                # read it the way that class reads it — defensively.
                detail = (getattr(self._mech, "_pending_failure", None) or {}).get("message", "")
                return self._refuse(phase, "attach_failed",
                                    f"Upload did not complete: {str(detail)[:200]}", t0)
        finally:
            if pdf_path:
                try:
                    os.unlink(pdf_path)
                except Exception:
                    pass

        self._already_on_chart = getattr(self._mech, "_last_upload_outcome", None) == "already_on_chart"
        self._record(phase, "success",
                     f"Already filed: '{data.document_name}' — not uploaded again"
                     if self._already_on_chart else f"Attached '{data.document_name}'", t0)
        return True

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    async def execute(self, data: SurveyAttachInput) -> SurveyAttachOutput:
        self._start_time = time.time()
        self._logs = []
        self._pending = {}
        self._chart_url = None
        self._row_count = 0
        self._pages = []
        self._truncated = False
        self._current_page = 0
        self._selection_mode = None
        self._already_on_chart = False

        self._mech = TNExecutorV2(self._runtime, self._credentials)
        self._mech._start_time = self._start_time
        self._page = await self._runtime.ensure_browser()
        self._mech._page = self._page

        logger.info(
            f"[ATTACH] start run_id={data.run_id} contact_id={data.contact_id} "
            f"document='{data.document_name}'"
        )

        # Entry + login: borrowed mechanics, unchanged.
        if not await self._mech._phase_entry():
            return self._build_failure_from_mech(SurveyAttachPhase.ENTRY, "practice_code_rejected")
        self._record(SurveyAttachPhase.ENTRY, "success", "Reached the TherapyNotes login")
        if not await self._mech._phase_login():
            return self._build_failure_from_mech(SurveyAttachPhase.LOGIN, "login_failed")
        self._record(SurveyAttachPhase.LOGIN, "success", "Logged in")

        for step in (self._phase_search, self._phase_select,
                     self._phase_verify, self._phase_attach):
            if not await step(data):
                return self._build_failure(self._chart_url)

        return SurveyAttachOutput.success_result(
            tn_patient_url=self._chart_url,
            document_name=data.document_name,
            selection_mode=self._selection_mode,
            already_on_chart=self._already_on_chart,
            logs=self._logs,
            duration_ms=self._elapsed_ms(),
        )

    def _build_failure_from_mech(self, phase, reason) -> SurveyAttachOutput:
        detail = (getattr(self._mech, "_pending_failure", None) or {}).get("message", "") if self._mech else ""
        self._pending = {"phase": phase, "reason": reason,
                         "message": f"{phase.value} failed: {str(detail)[:200]}"}
        self._record(phase, "failure", self._pending["message"])
        return self._build_failure()


async def _run_survey_attach_unscoped(runtime, data: SurveyAttachInput) -> SurveyAttachOutput:
    """
    Entry point. Shares the create flow's module-level lock, so an attach and a
    scheduling run queue behind one another rather than fighting over the single
    TherapyNotes licence.
    """
    if _execution_lock.locked():
        logger.warning("[ATTACH] rejected — another TherapyNotes execution is in progress")
        return SurveyAttachOutput.failure(
            phase=SurveyAttachPhase.ENTRY, reason="unknown_error",
            message="Another TherapyNotes execution is already in progress",
            logs=[], duration_ms=0,
        )
    async with _execution_lock:
        credentials = get_tn_credentials()
        executor = SurveyAttachExecutor(runtime, credentials)
        return await executor.execute(data)



async def run_survey_attach(runtime, data: SurveyAttachInput) -> SurveyAttachOutput:
    """
    Entry point. Every log record written during the run — including exception
    text and TherapyNotes messages echoed into logs — is scrubbed of this
    patient's identifiers (shared/phi_redaction.py).
    """
    with phi_scope_for(data):
        return await _run_survey_attach_unscoped(runtime, data)
