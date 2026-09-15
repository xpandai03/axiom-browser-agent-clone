"""
Read every active patient's identity out of TherapyNotes.

BUILT ON THE ACTIVE-COUNT EXECUTOR, NOT BESIDE IT. ActivePatientsExecutor
subclasses ActiveCountExecutor, so the login, the single navigation, the
clinician enumeration, the Active filter, the pager, `_assert_on_patients_list`
and both allowlists are the SAME code paths that route already uses. There is
one clinician-iteration loop in this repository and this is not a second one.

WHAT IS DIFFERENT: where the count route reads the "Displaying ..." phrase, this
reads the rows under it.

ROWS ARE LOCATED BY THE RESULT ANCHOR, NEVER BY `tr.Row`
--------------------------------------------------------
`#PatientSearchTableList tr.Row` matches only the ALTERNATING half of the table
— recon 2026-09-13 measured 20 items against 10 matches, 13 against 7, 14
against 7. A design that read `tr.Row` would silently return half the practice
and look entirely healthy doing it. Every row here is reached from
`a[data-testid='patient-search-patient-link']` and walked up to its own `<tr>`,
which is the same anchor-first rule the count route's Test Anna matcher follows.

AND THE TABLE PAGES IN TWENTIES, so every clinician is read to the end of its
pager. MAX_PAGES bounds it; hitting that bound is reported as `truncated`
rather than passed off as a complete set.

COLUMNS ARE DISCOVERED, NOT ASSUMED
-----------------------------------
The recon is explicit that "cell-index selectors are positional and should not
be used", and it records name and date of birth as mapped while "phone and
assigned clinician are NOT mapped. No selector is guessed here." So nothing is
guessed here either: the header row's labels are read once and matched to
columns, and a field with no column comes back empty with `columns_found`
saying it was absent rather than blank. A date-shaped or phone-shaped fallback
scan covers a table that renders no usable header.

READ-ONLY BY CONSTRUCTION, NOT BY INTENTION
-------------------------------------------
Inherited from ActiveCountExecutor and unchanged by this subclass:

  * `_click` raises ReadOnlyViolation for any selector outside CLICK_ALLOWLIST,
    which holds exactly two entries — the search submit and the pager's Next
    link. `_fill` refuses anything outside FILL_ALLOWLIST (the search box).
    Those two methods are the only places either class acts on the page.
  * This module adds NO selector of its own for any control. It reads the DOM
    through `page.evaluate` and clicks nothing but the inherited pager.
  * `_assert_on_patients_list` runs after every postback; a URL that leaves the
    Patients list aborts the pass. A chart URL therefore cannot be reached even
    by accident.
  * No result anchor is ever clicked and no href is ever followed. The chart id
    is PARSED out of the href as text.

PHI
---
Patient values exist in exactly one place: the response body, which is the point
of the route. Nothing is logged. Every log line here carries counts, clinician
labels (staff names) and option values only — asserted by
tests/test_active_patients_parsing.py.
"""

import logging
import time
from typing import Dict, List, Optional

from shared.schemas.active_patients import (
    ActivePatientsOutput,
    ActivePatientsPhase,
    ActivePatientsPhaseLog,
    ClinicianPatientPage,
    PatientIdentityRow,
)
from services.api.active_count_executor import (
    ACTIVITY_ACTIVE,
    OPTION_ANY,
    PATIENTS_URL,
    SEL_CLINICIAN_FILTER,
    SEL_PAGER_NEXT,
    ActiveCountExecutor,
    ReadOnlyViolation,
)
from shared.patient_row_parsing import (
    chart_id_from_href,
    classify_headers,
    extract_row,
    squeeze,
)
from services.api.config import get_tn_credentials
from services.api.tn_executor import _execution_lock
from services.api.tn_executor_v2 import TNExecutorV2

logger = logging.getLogger(__name__)


# Read the current page's header labels and every result row.
#
# ANCHOR-FIRST: rows are found from the patient link and walked up to their own
# <tr>, because `tr.Row` matches only half the table. Cells are returned as text
# and everything else — column mapping, extraction — happens in Python where it
# is testable without a browser.
_ROWS_JS = r"""
() => {
  const table = document.querySelector('#PatientSearchTableList');
  if (!table) return null;

  const headers = [...table.querySelectorAll('thead th, tr th')]
    .map(th => (th.innerText || '').trim());

  const links = [...table.querySelectorAll(
    "a[data-testid='patient-search-patient-link']")];

  const rows = links.map(a => {
    const tr = a.closest('tr');
    const cells = tr ? [...tr.children].map(td => (td.innerText || '').trim()) : [];
    return {
      name: (a.innerText || '').trim(),
      href: a.getAttribute('href') || '',
      cells,
    };
  });

  return {
    headers,
    rows,
    hasNext: !!document.querySelector('a#DynamicTablePagingLink.Next'),
  };
}
"""


class ActivePatientsExecutor(ActiveCountExecutor):
    """One login, then every clinician's Active list, read row by row."""

    # A clinician with ~970 practice-wide patients across 30 options sits well
    # inside this; the aggregate option is skipped, so no single read approaches
    # it. Hitting it is reported rather than hidden.
    MAX_PAGES = 60

    def __init__(self, runtime, credentials):
        super().__init__(runtime, credentials)
        self._columns: Dict[str, Optional[int]] = {}
        self._headers: List[str] = []

    def _record_p(self, phase, status, message, phase_start=None) -> None:
        """Phase log in this route's own enum."""
        self._logs.append(
            ActivePatientsPhaseLog(
                phase=phase, status=status, message=message,
                duration_ms=int((time.time() - (phase_start or time.time())) * 1000),
            )
        )

    async def _read_rows_for(self, value: str, label: str) -> ClinicianPatientPage:
        """
        Every Active patient under one clinician option, across every page.

        A failure here is scoped to this clinician: it is returned as a failed
        page and the caller carries on, because one unreadable option must not
        discard the twenty-nine that read cleanly.
        """
        page = ClinicianPatientPage(
            option_value=value, label=label, is_aggregate=(value == OPTION_ANY),
        )
        try:
            await self._run_query(value)
        except ReadOnlyViolation:
            raise
        except Exception as e:
            page.status = "failure"
            page.failure_reason = "option_select_failed"
            page.message = f"could not apply this clinician's filter: {type(e).__name__}"
            return page

        seen_pages = 0
        try:
            for _ in range(self.MAX_PAGES):
                data = await self._page.evaluate(_ROWS_JS)
                if data is None:
                    # No table at all. For a clinician with no active patients
                    # TherapyNotes renders a message instead of an empty table,
                    # which is a legitimate zero rather than a failure.
                    page.status = "success"
                    page.row_count = 0
                    page.pages_read = seen_pages
                    return page

                if not self._headers and data.get("headers"):
                    self._headers = [squeeze(h) for h in data["headers"]]
                    self._columns = classify_headers(self._headers)

                for r in data.get("rows", []):
                    cells = r.get("cells") or []
                    dob, phone = extract_row(cells, self._columns)
                    name = squeeze(r.get("name"))
                    cid = chart_id_from_href(r.get("href"))
                    # A row with no id is not a patient row — a spacer, a footer,
                    # or a link that is not a patient link. Skipped rather than
                    # returned as a patient with an empty identifier.
                    if not cid:
                        continue
                    page.rows.append(
                        PatientIdentityRow(
                            chart_id=cid, name=name, dob=dob, phone=phone,
                            clinician_option_value=value, clinician_label=label,
                        )
                    )
                    if not name:
                        page.empty_name += 1
                    if not dob:
                        page.empty_dob += 1
                    if not phone:
                        page.empty_phone += 1

                seen_pages += 1
                if not data.get("hasNext"):
                    break
                await self._click_and_wait(SEL_PAGER_NEXT)
            else:
                page.truncated = True

            page.status = "success"
            page.row_count = len(page.rows)
            page.pages_read = seen_pages
            return page

        except ReadOnlyViolation:
            raise
        except Exception as e:
            page.status = "failure"
            page.failure_reason = "rows_unreadable"
            page.message = f"{type(e).__name__} while reading rows"
            page.row_count = len(page.rows)
            page.pages_read = seen_pages
            return page

    async def execute_patients(self) -> ActivePatientsOutput:
        self._start_time = time.time()
        t0 = time.time()

        self._mech = TNExecutorV2(self._runtime, self._credentials)
        self._mech._start_time = self._start_time
        self._page = await self._runtime.ensure_browser()
        self._mech._page = self._page

        # --- entry + login: ONE session for the whole pass --------------------
        if not await self._mech._phase_entry():
            self._record_p(ActivePatientsPhase.ENTRY, "failure", "practice code phase failed", t0)
            return ActivePatientsOutput.failure(
                ActivePatientsPhase.ENTRY, "practice_code_rejected",
                "Practice code phase failed", self._logs, self._elapsed_ms(),
            )
        self._record_p(ActivePatientsPhase.ENTRY, "success", "practice code accepted", t0)

        t0 = time.time()
        if not await self._mech._phase_login():
            self._record_p(ActivePatientsPhase.LOGIN, "failure", "login failed", t0)
            return ActivePatientsOutput.failure(
                ActivePatientsPhase.LOGIN, "login_failed",
                "Login failed", self._logs, self._elapsed_ms(),
            )
        self._record_p(ActivePatientsPhase.LOGIN, "success", "logged in", t0)

        # --- navigate ONCE; every later query is a postback on this page ------
        t0 = time.time()
        try:
            await self._page.goto(PATIENTS_URL, wait_until="domcontentloaded",
                                  timeout=self.STEP_TIMEOUT_MS)
            await self._settle(8000)
            await self._page.wait_for_selector(SEL_CLINICIAN_FILTER,
                                               timeout=self.STEP_TIMEOUT_MS)
        except Exception as e:
            self._record_p(ActivePatientsPhase.NAVIGATE, "failure", "Patients page not reached", t0)
            return ActivePatientsOutput.failure(
                ActivePatientsPhase.NAVIGATE, "patients_page_not_found",
                f"Patients page did not render: {type(e).__name__}",
                self._logs, self._elapsed_ms(),
            )
        self._record_p(ActivePatientsPhase.NAVIGATE, "success", "Patients page loaded", t0)

        # --- the SAME enumeration the count route uses -------------------------
        t0 = time.time()
        opts = await self._enumerate_clinicians()
        if opts is None:
            self._record_p(ActivePatientsPhase.ENUMERATE, "failure", "dropdown missing", t0)
            return ActivePatientsOutput.failure(
                ActivePatientsPhase.ENUMERATE, "clinician_dropdown_missing",
                "The 'Assigned to' select was not present", self._logs, self._elapsed_ms(),
            )
        if not opts:
            self._record_p(ActivePatientsPhase.ENUMERATE, "failure", "no options", t0)
            return ActivePatientsOutput.failure(
                ActivePatientsPhase.ENUMERATE, "no_clinicians_found",
                "The clinician dropdown rendered no options", self._logs, self._elapsed_ms(),
            )
        self._record_p(ActivePatientsPhase.ENUMERATE, "success", f"{len(opts)} options", t0)
        logger.info(f"[PATIENTS] {len(opts)} dropdown options; reading rows per clinician")

        # --- read every option -------------------------------------------------
        t0 = time.time()
        results: List[ClinicianPatientPage] = []
        for i, o in enumerate(opts, 1):
            value, label = o.get("value") or "", o.get("text") or ""
            if not value:
                continue
            # The aggregate option repeats every patient the individual options
            # already return. Recorded so the CRM sees it was considered, and
            # skipped so the row set is not doubled.
            if value == OPTION_ANY:
                results.append(ClinicianPatientPage(
                    option_value=value, label=label, is_aggregate=True,
                    status="success", row_count=0, pages_read=0,
                    message="practice-wide option: not read for identity rows",
                ))
                continue
            try:
                page = await self._read_rows_for(value, label)
            except ReadOnlyViolation:
                raise
            except Exception as e:
                page = ClinicianPatientPage(
                    option_value=value, label=label, status="failure",
                    failure_reason="unknown_error",
                    message=f"unhandled: {type(e).__name__}",
                )
            results.append(page)
            # Counts and a staff label. No patient value.
            logger.info(
                f"[PATIENTS] {i}/{len(opts)} {label!r}: "
                + (f"{page.row_count} rows over {page.pages_read} page(s)"
                   if page.status == "success"
                   else f"FAILED {page.failure_reason}")
            )

        ok = sum(1 for r in results if r.status == "success")
        self._record_p(
            ActivePatientsPhase.READ_ROWS,
            "success" if ok else "failure",
            f"{ok}/{len(results)} options read", t0,
        )

        columns_found = {
            "name": True,  # always, from the anchor rather than a column
            "dob": self._columns.get("dob") is not None,
            "phone": self._columns.get("phone") is not None,
        }
        return ActivePatientsOutput.completed(
            results, columns_found, self._headers, self._logs, self._elapsed_ms(),
        )


async def run_active_patients(runtime) -> ActivePatientsOutput:
    """
    One pass, serialised against every other TherapyNotes route.

    The practice holds a single license, so `_execution_lock` is what stops this
    running while a create-and-schedule, an attach or a count pass is in flight.
    """
    async with _execution_lock:
        credentials = get_tn_credentials()
        executor = ActivePatientsExecutor(runtime, credentials)
        return await executor.execute_patients()
