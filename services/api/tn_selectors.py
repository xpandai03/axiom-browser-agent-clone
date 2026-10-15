"""
TherapyNotes selectors and confirmation signals that EVERY flow shares.

Add to Schedule, survey attach, the nightly patient pull, the count pass and
the selector smoke all log in, land on the dashboard, open the Patients page,
search it and open charts through the values below. When TherapyNotes changes
its UI, this is the one file to edit. Phase-specific selectors (the New
Patient form's fields, the upload dialog, the scheduling dialog, the portal
dialog) stay next to the phase that uses them.

Verified against the live UI by the read-only recons of 2026-10-14 and
2026-10-15 (docs/selectors/tn_v2_phases.md). Nothing here is guessed; a value
that was not observed says so.
"""

import re
from typing import Optional

LOGIN_URL = "https://www.therapynotes.com/app/login/"
PATIENTS_URL = "https://www.therapynotes.com/app/patients/"

# ---------------------------------------------------------------------------
# Login (practice code, then username + password). Unchanged by the refresh.
# ---------------------------------------------------------------------------
LOGIN = {
    "practice_code_field": [
        "input#PracticeCode",
        "input[name='practiceCode']",
        "input[placeholder*='ractice']",
        "input[type='text']",
    ],
    "practice_code_submit": [
        "button#Continue__ContinueButton",
        "button[type='submit']",
        "button:has-text('Continue')",
    ],
    "username_field": [
        "input#Login__UsernameField",
        "input[name='username']",
        "input[type='text']",
    ],
    "password_field": [
        "input#Login__Password",
        "input[name='Password']",
        "input[type='password']",
    ],
    "login_submit": [
        "button#Login__LogInButton",
        "button[type='submit']",
        "button:has-text('Log In')",
    ],
    "login_error": [
        "text=did not match any account",
        "text=account has been locked",
        ".validation-summary-errors",
        ".error-message",
        "[role='alert']",
        ".alert-danger",
        "text=Invalid",
        "text=incorrect",
    ],
}

# The login form's own inputs: still visible after submit = credentials refused.
LOGIN_FORM_INPUTS = ("input#Login__UsernameField", "input#Login__Password")

# ---------------------------------------------------------------------------
# Dashboard: what proves "logged in".
#
# Three links that only an authenticated session has, matched ONLY when
# VISIBLE (DASHBOARD_SELECTOR is waited on through
# locator(...).filter(visible=True), never page.wait_for_selector: that checks
# just the FIRST match in page order, and the refreshed TherapyNotes renders a
# hidden a[href='/app/patients/'] first — login then "failed" after 30s on a
# dashboard that had loaded: Oct 13 09:30-09:35Z and the 2026-10-15 04:19Z
# recon). No generic marker: a bare `nav` matched TherapyNotes' forced
# password-change page on 14 October. A marker is still necessary, not
# sufficient: login then checks the blocking pages below
# (tn_executor_v2._phase_login), and every flow's first navigation waits for
# its destination's own element.
#
# Not observed: a dashboard-only element id. The 2026-10-15 recon was to
# capture one and stopped at this very bug.
# ---------------------------------------------------------------------------
DASHBOARD_MARKERS = [
    "a[href='/app/patients/']",
    "a[href='/app/scheduling/']",
    "a[href*='/app/todo' i]",
]
DASHBOARD_SELECTOR = ", ".join(DASHBOARD_MARKERS)

# ---------------------------------------------------------------------------
# Navigation: NO flow clicks a top-menu link. Every page a flow needs has a
# direct URL and is opened with page.goto, then confirmed by an element that
# only that page has (docs/selectors/tn_v2_phases.md, "2026-10-15 — navigation
# audit"). The menu is not clicked because the refreshed TherapyNotes renders
# hidden duplicates of its links (2026-10-15 03:56Z: a click on one timed out).
# ---------------------------------------------------------------------------
SCHEDULING_URL = "https://www.therapynotes.com/app/scheduling/"

NAV = {
    # The Patients list loaded: the same element the nightly pull waits for.
    "patients_page_indicator": [
        "select#ctl00_BodyContent_DropDownListSearchAssignment",
        "input#ctl00_BodyContent_TextBoxSearchPatientName",
    ],
    "new_patient_button": [
        "input#ctl00_BodyContent_ButtonCreatePatient1",
        "input[type='submit'][value='+ New Patient']",
    ],
}

# ---------------------------------------------------------------------------
# Patients page: filters, search, results, pager. Unchanged by the refresh.
# ---------------------------------------------------------------------------
PATIENTS = {
    "activity_filter": "select#ctl00_BodyContent_DropDownListSearchActivity",
    "clinician_filter": "select#ctl00_BodyContent_DropDownListSearchAssignment",
    "search_input": "input#ctl00_BodyContent_TextBoxSearchPatientName",
    "search_submit": "input#ctl00_BodyContent_ButtonSearch",
    # Attached before any search; visible once a search has rendered rows.
    "results_container": "div#DivPatientsList",
    "pager_next": "a#DynamicTablePagingLink.Next",
    # Rows are located by their anchor, never by tr.Row (which alternates).
    "result_anchor": "#PatientSearchTableList a[data-testid='patient-search-patient-link']",
    "name_link": "a[data-testid='patient-search-patient-link']",
    "dob_link": "a[data-testid='patient-search-dob-link']",
}

# ---------------------------------------------------------------------------
# Chart: header fields and hash tabs. Unchanged by the refresh; all eight tabs
# present: Patient Info, To-Do, Schedule, Documents, Billing Settings,
# Clinicians, Portal, Messages.
# ---------------------------------------------------------------------------
CHART = {
    "patient_name": ["div#PatientInformation__PatientName",
                     "span[data-testid='patientheaderview-patientname-container'] span"],
    "dob": ["span#PatientInformation__DOBElem",
            "span[data-testid='patientheaderview-dob-container']"],
    "phone_fields": ["div#PatientInformation__MobilePhoneElem",
                     "div#PatientInformation__HomePhoneElem"],
    "clinicians": [".clinician-assignments .clinician-assignment a"],
}


def chart_tab(name: str) -> str:
    """A chart hash tab: 'Clinicians', 'Documents', 'Portal', 'Patient+Info' …"""
    return f"a[href='#tab={name}']"


# ---------------------------------------------------------------------------
# Pages and notices that appear AFTER a successful credential check.
# ---------------------------------------------------------------------------

# BLOCKING: TherapyNotes' forced password change. Seen from 2026-10-13 20:06Z:
#   "Settings: Change Your Password — Your password has been in use for more
#    than 120 days, and has expired. Please provide a new password."
# The agent never changes a password (a settings change); it stops and says so.
PASSWORD_EXPIRED_PATTERNS = [
    re.compile(r"your password has expired", re.I),
    re.compile(r"password must be changed", re.I),
    re.compile(r"password has been in use for more than \d+ days", re.I),
    re.compile(r"has expired\.?\s*please provide a new password", re.I),
]

# WARNING ONLY: "Your password will expire in less than 1 day on 10/13."
# Seen 2026-10-13 alongside the dashboard; login still worked.
_PASSWORD_EXPIRING = re.compile(
    r"your password will expire in\s+(less than\s+)?(\d+)\s+days?", re.I)


def password_expired_text(body_text: Optional[str]) -> Optional[str]:
    """The matched phrase if this page is TherapyNotes' forced password change."""
    text = " ".join((body_text or "").split())
    for pat in PASSWORD_EXPIRED_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


def password_expires_in_days(body_text: Optional[str]) -> Optional[int]:
    """Days left from the 'will expire in N days' notice; 'less than N' -> N-1."""
    m = _PASSWORD_EXPIRING.search(" ".join((body_text or "").split()))
    if not m:
        return None
    n = int(m.group(2))
    return max(n - 1, 0) if m.group(1) else n


# NOTICES that sit on top of the dashboard and only need closing:
#   2026-10-13 "What's New: A Better Way to Navigate — TherapyNotes® navigation
#   has been refreshed …" (markup NOT observed: it was gone by the 10-14 recon).
# Closed only through a control whose whole label (or aria-label) is one of
# NOTICE_CLOSE_LABELS. Anything else — "Try it", "Turn on", "Change …",
# "Update …" — is left alone and the notice's text is logged instead.
NOTICE_CONTAINERS = (
    "[role='dialog'], [aria-modal='true'], .Dialog, .modal, "
    "[class*='whats-new' i], [class*='whatsnew' i], [class*='announcement' i], "
    "[class*='popover' i], [class*='walkme' i], [id*='walkme' i], "
    "[class*='pendo' i], [id*='pendo' i]"
)
NOTICE_CLOSE_LABELS = frozenset({
    "close", "x", "×", "✕", "got it", "dismiss", "not now", "maybe later",
    "no thanks", "skip", "skip tour", "ok", "okay", "done",
})
NOTICE_PHRASES = ("what's new", "a better way to navigate", "navigation has been refreshed")


def is_notice_close_label(label: Optional[str]) -> bool:
    norm = " ".join((label or "").replace("’", "'").split()).strip().lower()
    return norm in NOTICE_CLOSE_LABELS
