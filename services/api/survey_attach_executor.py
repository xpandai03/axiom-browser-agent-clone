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
import os
import re
import time
from typing import List, Optional

from shared.name_keys import names_agree
from shared.patient_row_parsing import chart_id_from_href
from shared.schemas.survey_attach import (
    SurveyAttachInput,
    SurveyAttachOutput,
    SurveyAttachPhase,
    SurveyAttachPhaseLog,
)
from shared.schemas.therapy_notes_v2 import TNPhaseV2
from services.api.config import get_tn_credentials
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


def _digits(value: Optional[str]) -> str:
    """Phone comparison is on digits only — formatting varies on both sides."""
    return "".join(c for c in (value or "") if c.isdigit())


class SurveyAttachExecutor:
    STEP_TIMEOUT_MS = 15_000
    CLINICIAN_TAB_TIMEOUT_MS = 10_000

    # Result count at or above which the set is treated as TRUNCATED and refused
    # without opening any chart.
    #
    # RE-MEASURED 2026-09-21 AGAINST THE FULL TABLE. The previous value, 10, was
    # measured through `tr.Row` and was therefore half of a page: recon read the
    # page's own total phrase as "Displaying 1-20 of 936" while `tr.Row` returned
    # 10, and 14 items returned 7, and 13 returned 7. The table pages at TWENTY.
    #
    # Counting by anchor now sees all twenty, so the threshold has to be the real
    # page size or the guard fires on a complete set of eleven and refuses work it
    # should do. A live search on 2026-09-21 returned 9 anchors with no pager
    # rendered — under the cap, correctly not refused.
    #
    # At 20 the guard stays deliberately conservative: a search returning exactly
    # twenty COMPLETE results also refuses, because a full page is
    # indistinguishable from a truncated one on row count alone. The definitive
    # signal is the pager (`a#DynamicTablePagingLink.Next`), which is a better
    # test; it is logged beside the count here and remains a follow-up rather than
    # a behaviour change bundled into this fix.
    RESULT_CAP_SUSPECT = 20

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
            phase=phase, status=status, message=message,
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
            message=self._pending.get("message", "Unknown failure"),
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
        Search the Patients page by SURNAME ALONE.

        Not the full name, and this is empirical rather than a preference. A live
        run searching "First Last" for a patient stored as "First Middle Last"
        returned four rows, none of them the target; the same record is returned
        by its surname alone. TherapyNotes' matching does not behave like a
        substring test, and a more specific query is not a narrower one — it can
        simply miss.

        Not phone either: the field accepts "Name, Acct #, Phone, or Ins ID" and
        phone would be narrower, but the shape TherapyNotes stores a number in is
        unknown, so a digit-string query could return nothing for a patient who
        is present.

        So: cast the query wide enough to CONTAIN the target, then narrow
        precisely. Narrowing (in _phase_select) still requires the result's NAME
        CELL to agree with the survey name on a full reading, plus an exact
        date-of-birth match, so a broad query costs nothing in precision. It does
        return more rows, which is what the truncation guard is for.
        """
        phase, t0 = SurveyAttachPhase.SEARCH, time.time()
        page = self._page
        await page.goto(PATIENTS_URL, wait_until="domcontentloaded")
        await asyncio.sleep(2.5)

        box = await self._first("patients_page_search_input")
        btn = await self._first("patients_page_search_submit")
        if box is None or btn is None:
            return self._refuse(phase, "search_ui_not_found",
                                "Patients-page search controls not found", t0)

        await box.click()
        await box.fill("")
        await box.fill(data.last_name)
        await btn.click()          # a SEARCH submit — not a save-family control
        await asyncio.sleep(3.5)

        rows = self._page.locator(SELECTORS_ATTACH["patients_page_result_anchor"][0])
        try:
            count = await rows.count()
        except Exception:
            count = 0
        # The pager is the definitive truncation signal and the row count is a
        # proxy for it. Logged together so the two can be compared in the field
        # before the guard is switched over to the pager.
        try:
            pager = await self._page.locator("a#DynamicTablePagingLink.Next").count() > 0
        except Exception:
            pager = False
        logger.info(f"[ATTACH] search returned {count} row(s), pager={'yes' if pager else 'no'}")
        self._record(phase, "success", f"Search returned {count} row(s)", t0)
        self._row_count = count
        return True

    async def _phase_select(self, data: SurveyAttachInput) -> bool:
        """
        Narrow to EXACTLY ONE candidate, then open that chart.

        Narrowing uses the date of birth the result row already shows, so no
        chart is opened in order to decide which chart to open.
        """
        phase, t0 = SurveyAttachPhase.SELECT, time.time()
        rows = self._page.locator(SELECTORS_ATTACH["patients_page_result_anchor"][0])
        total = self._row_count

        if total == 0:
            return self._refuse(phase, "patient_not_found",
                                "No patient matched this name in TherapyNotes", t0)

        # WHICH RECORD, WHEN THE CRM ALREADY KNOWS.
        #
        # Everything below this branch chooses a record by NAME, and refuses
        # whenever name and date of birth cannot single one out — fifteen rows on
        # a common surname, or two people genuinely sharing both. Those refusals
        # are correct while the agent is guessing. They are unnecessary when it
        # is not: a chart id names the record, so ambiguity is resolved by a fact
        # and the truncation and multiple-candidate guards below have nothing
        # left to protect against.
        #
        # It decides WHICH chart, never WHETHER to attach. _phase_verify runs
        # identically on both paths.
        self._selection_mode = "chart_id" if data.expected_chart_id else "name"
        if data.expected_chart_id:
            return await self._select_by_chart_id(data, t0)

        if total >= self.RESULT_CAP_SUSPECT:
            return self._refuse(
                phase, "result_set_possibly_truncated",
                f"Search returned {total} rows, filling the "
                f"{self.RESULT_CAP_SUSPECT}-row page the TherapyNotes patient list "
                "pages at, so there are very likely more results the agent cannot "
                "see. Refusing to select from a list that could be hiding another "
                "identical match — attach this document by hand.",
                t0,
            )

        # WHAT COMES BACK FROM THE BROWSER, AND WHY IT IS NOT A VERDICT.
        #
        # The browser returns three SCOPED strings per result — the name cell's
        # own text, the date-of-birth cell's own text, and the href — and makes no
        # decision. Every decision is made here, once, in Python.
        #
        # This is a deliberate reversal. The previous version matched in the
        # browser so that "no patient value enters this process", and paid for it
        # by having to carry a SECOND implementation of the name rule and the date
        # rule in JavaScript. Two implementations of one rule is how the agent and
        # the CRM came to disagree about who a person is. The values that cross
        # back are held in locals, compared, and dropped: nothing is logged, and
        # _refuse() messages name the field and never the value — the [L] sentinel
        # in tests/test_survey_attach.py asserts exactly that.
        #
        # SCOPED, which is the other half of the fix. The name read is the
        # ANCHOR's text, not the row's. Reading the row meant a survey name passed
        # because its tokens happened to appear in the clinician or payer column.
        want_dob = _normalize_dob(data.dob)
        want_name = f"{data.first_name} {data.last_name}"
        try:
            cells = await rows.evaluate_all(
                r"""
                (anchors) => anchors.map(a => {
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
                })
                """
            )
        except Exception as e:
            return self._refuse(phase, "unknown_error",
                                f"Could not evaluate search results: {str(e)[:120]}", t0)

        cells = cells or []
        matched = []
        name_only = 0
        for c in cells:
            # ONE name rule, the same one the chart-header check and the CRM's
            # matcher use: every reading of a parenthesised name, matched on
            # intersection. "Minor (Rowan) Thistlewood" reads as both "minor thistlewood"
            # and "rowan thistlewood", so a survey carrying the legal name agrees with
            # it — and a middle name on one side only still does not.
            if not names_agree(want_name, c.get("name")):
                continue
            name_only += 1
            row_dob = _normalize_dob(c.get("dob"))
            # Both sides must be READABLE and equal. `None == None` is not a
            # match: a row whose date cannot be read is never selected, and a
            # survey whose date cannot be read must not match every row.
            if row_dob is not None and row_dob == want_dob:
                matched.append(c)

        # Counts only — no names, no dates.
        logger.info(
            f"[ATTACH] narrowing: total={len(cells)} name_matches={name_only} "
            f"name+dob_matches={len(matched)}"
        )

        if len(matched) == 0:
            if name_only:
                return self._refuse(
                    phase, "patient_not_found",
                    f"{name_only} patient(s) matched the name but none matched the "
                    "date of birth given on the survey.", t0)
            return self._refuse(phase, "patient_not_found",
                                "No patient matched this name in TherapyNotes", t0)

        if len(matched) > 1:
            return self._refuse(
                phase, "multiple_candidates",
                f"{len(matched)} patients share this name AND date of birth. The "
                "search results carry nothing else to separate them — attach this "
                "document by hand after confirming which record is correct.", t0)

        href = matched[0]["href"]
        m = _RECORD_URL_RE.search(href or "")
        if not m:
            return self._refuse(phase, "chart_not_opened",
                                "Search result did not carry a patient record link", t0)
        pid = m.group(1)

        # Charts cannot be deep-linked; click the row's own anchor. Selected by
        # the anchor itself, so a target on an even-indexed row is clickable —
        # the old form was prefixed with `tr.Row`, which could not reach one.
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

    async def _select_by_chart_id(self, data: SurveyAttachInput, t0: float) -> bool:
        """
        Open the row whose link carries the chart id the CRM supplied.

        NO FALLBACK. If the expected id is not among the results, this refuses
        rather than reverting to name selection. The CRM believed a specific
        record existed and the search did not surface it — the patient may have
        been merged, discharged or renumbered — and that is a situation for a
        person, not for a second guess. Falling back would also be the one way
        this build could make things WORSE than yesterday: it would take a run
        that had a precise expectation and quietly downgrade it to the guess the
        expectation was meant to replace.

        THE ID SPACE IS NOT ASSUMED TO MATCH. The hrefs are parsed with
        chart_id_from_href — the same function the nightly active-patients pull
        used to produce the ids the CRM stores — rather than with a second regex
        written here that could drift from it.
        """
        phase = SurveyAttachPhase.SELECT
        rows = self._page.locator(SELECTORS_ATTACH["patients_page_result_anchor"][0])
        want = data.expected_chart_id
        total = self._row_count

        # Hrefs only. A record id is not a patient value, and nothing else about
        # the rows crosses back into this process.
        try:
            hrefs = await rows.evaluate_all(
                r"""(anchors) => anchors.map(a => a.getAttribute('href') || "")"""
            )
        except Exception as e:
            return self._refuse(phase, "unknown_error",
                                f"Could not evaluate search results: {str(e)[:120]}", t0)

        matched = [i for i, h in enumerate(hrefs or []) if chart_id_from_href(h) == want]
        logger.info(
            f"[ATTACH] id-directed selection: {total} row(s) searched, "
            f"{len(matched)} carrying the expected chart id"
        )

        if not matched:
            # Truncation does not change the verdict, but it does change what a
            # human should go and look at, so the message says which it was.
            truncated = total >= self.RESULT_CAP_SUSPECT
            extra = (
                f" The search also filled the {self.RESULT_CAP_SUSPECT}-row page "
                "the patient list pages at, so the record may simply be on a page "
                "the agent cannot see."
                if truncated else
                " The search returned results, but none of them was that record."
            )
            return self._refuse(
                phase, "expected_chart_not_in_results",
                "The patient record the CRM expected did not appear in the "
                "TherapyNotes search results. The patient may have been merged, "
                "discharged or renumbered since the last nightly pull." + extra +
                " Attach this document by hand after confirming which record is "
                "correct.",
                t0,
            )

        if len(matched) > 1:
            # Two rows carrying the SAME id are two links to one record, so
            # either opens the same chart and there is nothing to choose between
            # them. Logged because it should not happen and a human may want to
            # know the list rendered a duplicate.
            logger.warning(
                f"[ATTACH] the expected chart id appeared on {len(matched)} rows; "
                "they address one record, opening it"
            )

        # Same click as the name path: charts cannot be deep-linked.
        await self._page.click(
            f"{SELECTORS_ATTACH['patients_page_result_anchor'][0]}[href*='{want}']"
        )
        await asyncio.sleep(4)

        landed = _RECORD_URL_RE.search(self._page.url or "")
        if not landed or landed.group(1) != want:
            return self._refuse(phase, "chart_not_opened",
                                "Clicking the expected record did not land on that "
                                "patient chart", t0)

        self._chart_url = self._page.url
        self._record(phase, "success",
                     "Opened the patient chart the CRM identified by chart id", t0)
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
        want_clin = _name_tokens(data.clinician_name)
        try:
            hit = await assignments.evaluate_all(
                r"""
                (nodes, want) => {
                  const toks = (s) => (s || "").toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
                  return nodes.some(n => {
                    const t = new Set(toks(n.innerText || n.textContent || ""));
                    return want.every(w => t.has(w));
                  });
                }
                """,
                want_clin,
            )
        except Exception:
            hit = False
        logger.info(f"[ATTACH] clinician check: {n_assign} assignment(s), expected present={hit}")
        if not hit:
            return self._refuse(
                phase, "clinician_mismatch",
                f"The clinician named on the survey is not among this patient's "
                f"{n_assign} chart assignment(s).", t0)

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

        self._record(phase, "success", f"Attached '{data.document_name}'", t0)
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
        self._selection_mode = None

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
            logs=self._logs,
            duration_ms=self._elapsed_ms(),
        )

    def _build_failure_from_mech(self, phase, reason) -> SurveyAttachOutput:
        detail = (getattr(self._mech, "_pending_failure", None) or {}).get("message", "") if self._mech else ""
        self._pending = {"phase": phase, "reason": reason,
                         "message": f"{phase.value} failed: {str(detail)[:200]}"}
        self._record(phase, "failure", self._pending["message"])
        return self._build_failure()


async def run_survey_attach(runtime, data: SurveyAttachInput) -> SurveyAttachOutput:
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
