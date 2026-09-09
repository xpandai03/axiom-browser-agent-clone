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
    "patients_page_result_row":    ["#PatientSearchTableList tr.Row"],
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
    # MEASURED 2026-09-09, not borrowed. Two searches on common surnames each
    # returned exactly 10 tr.Row entries, and the page rendered explicit
    # pagination alongside them — a[id=DynamicTablePagingLink] with "Page 1",
    # "2", "›" and "»". So the Patients table pages at TEN rows, and a full first
    # page means there are more results the agent cannot see.
    #
    # This previously held 15, imported from the appointment-dialog dropdown. That
    # value was not merely imprecise, it was INERT: the table never returns more
    # than 10 rows, so the guard could never fire, and the route would have
    # narrowed within a truncated page and opened a chart — the exact failure the
    # guard exists to prevent.
    #
    # At 10 the guard is deliberately conservative: a search returning exactly ten
    # complete results also refuses, because a full page is indistinguishable from
    # a truncated one on row count alone. The definitive signal is the presence of
    # a pager, which is a better test and is noted as a follow-up.
    RESULT_CAP_SUSPECT = 10

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
        precisely. Narrowing (in _phase_select) still requires every name token —
        first AND last — to be present in the row, plus an exact date-of-birth
        match, so a broad query costs nothing in precision. It does return more
        rows, which is what the truncation guard is for.
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

        rows = self._page.locator(SELECTORS_ATTACH["patients_page_result_row"][0])
        try:
            count = await rows.count()
        except Exception:
            count = 0
        logger.info(f"[ATTACH] search returned {count} row(s)")
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
        rows = self._page.locator(SELECTORS_ATTACH["patients_page_result_row"][0])
        total = self._row_count

        if total == 0:
            return self._refuse(phase, "patient_not_found",
                                "No patient matched this name in TherapyNotes", t0)

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

        # Match rows in the BROWSER; only indices come back, so no patient value
        # enters this process.
        want_dob = _normalize_dob(data.dob)
        want_name = _name_tokens(f"{data.first_name} {data.last_name}")
        try:
            res = await rows.evaluate_all(
                r"""
                (rows, args) => {
                  const [wantName, wantDob] = args;
                  const toks = (s) => (s || "").toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
                  const normDob = (s) => {
                    const m = (s || "").match(/\b(\d{1,2})\/(\d{1,2})\/(\d{4})\b/);
                    if (!m) return null;
                    return String(m[1]).padStart(2,"0") + "/" + String(m[2]).padStart(2,"0") + "/" + m[3];
                  };
                  const out = [];
                  let nameOnly = 0;
                  rows.forEach((tr, i) => {
                    const a = tr.querySelector("a[data-testid='patient-search-patient-link']");
                    if (!a) return;
                    const rowToks = new Set(toks(tr.innerText || ""));
                    if (!wantName.every(t => rowToks.has(t))) return;
                    nameOnly++;
                    const dobEl = tr.querySelector("a[data-testid='patient-search-dob-link']");
                    const rowDob = normDob(dobEl ? dobEl.innerText : tr.innerText);
                    if (rowDob !== null && rowDob === wantDob) out.push({ i, href: a.getAttribute('href') });
                  });
                  return { matched: out, nameOnly, total: rows.length };
                }
                """,
                [want_name, want_dob],
            )
        except Exception as e:
            return self._refuse(phase, "unknown_error",
                                f"Could not evaluate search results: {str(e)[:120]}", t0)

        matched = res.get("matched") or []
        logger.info(
            f"[ATTACH] narrowing: total={res.get('total')} name_matches={res.get('nameOnly')} "
            f"name+dob_matches={len(matched)}"
        )

        if len(matched) == 0:
            if res.get("nameOnly"):
                return self._refuse(
                    phase, "patient_not_found",
                    f"{res['nameOnly']} patient(s) matched the name but none matched the "
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

        # Charts cannot be deep-linked; click the row's own anchor.
        await self._page.click(
            f"{SELECTORS_ATTACH['patients_page_result_row'][0]} "
            f"{SELECTORS_ATTACH['patients_page_name_link'][0]}[href*='{pid}']"
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
        chart_name = await self._read_text("chart_patient_name")
        if chart_name is None:
            return self._refuse(phase, "field_unreadable",
                                "Patient name could not be read from the chart", t0)
        want = set(_name_tokens(f"{data.first_name} {data.last_name}"))
        got = set(_name_tokens(chart_name))
        if not want.issubset(got):
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
