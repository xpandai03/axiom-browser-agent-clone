"""
Therapy Notes Patient Creation Executor (V2).

Step 1 clone: behavior-identical to services/api/tn_executor.py with V2-suffixed
identifiers. This is the parallel beta surface that Step 2 will extend with PDF
upload + appointment scheduling phases. For Step 1 it is a literal clone — same
phases, same selectors, same waits, same output shape.

Coexistence notes (differ from the V1 module — intentional, see Step 1 plan):
- The concurrency lock is NOT redefined here. It is imported from
  services.api.tn_executor so that V1 and V2 share ONE lock: the agent drives the
  same TherapyNotes service account, which cannot host two concurrent sessions.
- Screenshot filenames use a `tnv2_` prefix (vs `tn_`) so beta captures are
  distinguishable in the shared screenshot directory.

Architecture:
- Single-session, no parallel execution (shared module-level asyncio.Lock).
- Every phase confirms DOM state before proceeding.
- Every phase logs success/failure with timestamps.
- On failure: capture screenshot, return structured error, stop.
- No fixed waitForTimeout. No networkidle. Poll-based waits only.

Phases:
  0. ENTRY       — Navigate to TN login SPA, fill practice code.
  1. LOGIN       — Fill credentials, confirm dashboard.
  2. NAVIGATE    — Sidebar → Patients page, confirm + New Patient button.
  3. DETECT_FORM — Click + New Patient, wait for form to render.
  4. FILL        — Fill 8 required fields with verified DOM IDs.
  5. SAVE        — Click psy-button.button-save, confirm creation.
"""

import asyncio
import logging
import os
import re
import tempfile
import time
from typing import List, Optional

from shared.schemas.therapy_notes_v2 import (
    TNPatientInputV2,
    TNExecutorOutputV2,
    TNPhaseV2,
    TNPhaseLogV2,
    TNFailureReasonV2,
)
from services.api.config import TNCredentials, get_tn_credentials

# Shared concurrency lock — V1 and V2 must serialize against the same TN account.
from services.api.tn_executor import _execution_lock

logger = logging.getLogger(__name__)


# ============================================================================
# TherapyNotes Selectors (PLACEHOLDERS — must be verified against real DOM)
# ============================================================================
# Priority order per field: data-testid > id > name > aria-label > text
# Each key maps to a list of candidate selectors tried in order.

SELECTORS = {
    # ------------------------------------------------------------------
    # PHASE 0: Entry — TN login is at /app/login/ (SPA, JS-rendered)
    # Step 1: Practice code screen
    # Step 2: Username/password screen
    # ------------------------------------------------------------------
    "practice_code_field": [
        "input#PracticeCode",                 # Actual TN selector (verified via probe)
        "input[name='practiceCode']",
        "input[placeholder*='ractice']",
        "input[type='text']",
    ],
    "practice_code_submit": [
        "button#Continue__ContinueButton",    # Actual TN selector (verified via probe)
        "button[type='submit']",
        "button:has-text('Continue')",
    ],

    # ------------------------------------------------------------------
    # PHASE 1: Login (step 2 of the SPA login flow)
    # ------------------------------------------------------------------
    "username_field": [
        "input#Login__UsernameField",      # Actual TN selector (verified via probe-step2)
        "input[name='username']",
        "input[type='text']",
    ],
    "password_field": [
        "input#Login__Password",            # Actual TN selector (verified via probe-step2)
        "input[name='Password']",
        "input[type='password']",
    ],
    "login_submit": [
        "button#Login__LogInButton",        # Actual TN selector (verified via probe-step2)
        "button[type='submit']",
        "button:has-text('Log In')",
    ],
    "login_error": [
        "text=did not match any account",   # Actual TN error (verified via probe)
        "text=account has been locked",      # TN lockout message (verified via probe)
        ".validation-summary-errors",
        ".error-message",
        "[role='alert']",
        ".alert-danger",
        "text=Invalid",
        "text=incorrect",
    ],
    "dashboard_indicator": [
        "nav",
        "#sidebar",
        "[data-testid='sidebar']",
        "a:has-text('Home')",
        "a:has-text('Dashboard')",
        "a:has-text('Calendar')",
        "a:has-text('Patients')",
        ".main-nav",
    ],

    # ------------------------------------------------------------------
    # PHASE 2: Navigate to New Patient
    # ------------------------------------------------------------------
    "patients_link": [
        "a:has-text('Patients')",
        "a[href*='patient']",
        "[data-testid='patients-nav']",
        "nav a:has-text('Patients')",
    ],
    "patients_page_indicator": [
        "h1:has-text('Patients')",
        "[data-testid='patients-list']",
        "table",
        ".patient-list",
    ],
    "new_patient_button": [
        "input#ctl00_BodyContent_ButtonCreatePatient1",            # Actual TN selector (verified via DOM dump)
        "input[type='submit'][value='+ New Patient']",             # Fallback by type+value
    ],
}


# ============================================================================
# SELECTORS_V2 — Step 3 phases (PDF upload + scheduling)
# Source of truth: docs/selectors/tn_v2_phases.md (recon 2026-05-28)
# ============================================================================

SELECTORS_V2 = {
    # ---- Documents tab + upload modal ----
    "documents_tab": [
        "a[href='#tab=Documents']",
        "li:has-text('Documents') a",
    ],
    "upload_patient_file_button": [
        "button:has-text('Upload Patient File')",
    ],
    "file_input": [
        "#InputUploader",
        "input[type=file][name='InputUploader']",
        "input[type=file]",
    ],
    "document_name_input": [
        "#PatientFile__DocumentName",
        "input[maxlength='128']",
    ],
    "add_document_button_enabled": [
        "input[value='Add Document']:not([disabled])",
    ],
    "add_document_button": [
        "input[value='Add Document']",
    ],
    "upload_success_banner": [
        "div.standard-banner-message",
    ],
    "document_list_rows": [
        "tr.Row",
        "tr.AlternateRow",
    ],
    "dialog_close_button": [
        "button.DialogCloseButton",
    ],

    # ---- Scheduling navigation ----
    "scheduling_nav": [
        "a[href='/app/scheduling/']",
        "a:has-text('Scheduling')",
    ],

    # ---- New appointment dialog ----
    "new_appointment_button": [
        "#ButtonCreateAppointment",
        "psy-button:has-text('+ New')",
    ],
    "appt_patient_search": [
        "input#CalendarEntryEditor__PatientSelect",
    ],
    # Incremental-search result bubbles (shared shape for patient + clinician)
    "appt_incremental_result": [
        ".IncrementalSearchContainerNode .ContentBubble.IncrementalSearch",
        ".ContentBubble.IncrementalSearch",
    ],
    "appt_type_select": [
        "select#CalendarEntryEditor__TypeSelect",
    ],
    "appt_telehealth_checkbox": [
        "input#CalendarEntryEditor__TelehealthCheckbox",
    ],
    "appt_start_date": [
        "input#CalendarEntryEditor__StartDateInput",
    ],
    "appt_start_time": [
        "input#CalendarEntryEditor__StartTimeInput",
    ],
    "appt_clinician_dropdown": [
        "#CalendarEntryEditor__ClinicianSelect",
    ],
    "appt_clinician_input": [
        "#CalendarEntryEditor__ClinicianSelect input.DynamicInputTextBox",
        "#CalendarEntryEditor__ClinicianSelect input",
    ],
    "appt_alert_textarea": [
        "#CalendarEntryEditor__RemindersTextArea",
        "textarea[name='CalendarEntryEditor__RemindersTextArea']",
    ],
    "appt_save_button": [
        "#CalendarEntryEditor__Create-Button",
        "input[value='Save New Appointment']",
    ],
}

# PDF download limits (Step 3, decision I12)
PDF_MAX_BYTES = 25 * 1024 * 1024  # 25 MB
PDF_DOWNLOAD_TIMEOUT_S = 30


def _name_tokens(text: str) -> List[str]:
    """Lowercase a name and split into alphanumeric tokens, dropping punctuation.

    'Amanda Davison' -> ['amanda', 'davison']
    'Davison, Amanda, LPC' -> ['davison', 'amanda', 'lpc']
    Used for order-independent clinician matching (TN renders 'Last, First').
    """
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


class PdfFormatError(Exception):
    """Raised when a downloaded file fails the %PDF magic-byte check."""


# ============================================================================
# Screenshot directory
# ============================================================================

SCREENSHOT_DIR = os.environ.get(
    "TN_SCREENSHOT_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "screenshots", "tn"),
)


# ============================================================================
# TNExecutorV2
# ============================================================================

class TNExecutorV2:
    """
    Deterministic, linear executor for TherapyNotes patient creation.

    Uses PlaywrightRuntime for browser lifecycle (startup, proxy, stealth)
    and interacts with the Playwright Page object directly for fine-grained
    control over waits and assertions.

    Single-session. No parallelism. No retries within a phase — if a phase
    fails, the entire workflow fails with a structured error.
    """

    STEP_TIMEOUT_MS = 15_000  # Max wait per selector probe
    POLL_INTERVAL_MS = 250    # Poll interval for condition checks

    def __init__(self, runtime, credentials: TNCredentials):
        """
        Args:
            runtime: PlaywrightRuntime instance (manages browser lifecycle).
            credentials: Validated TNCredentials (practice code, username, password).
        """
        self._runtime = runtime
        self._credentials = credentials
        self._page = None
        self._logs: List[TNPhaseLogV2] = []
        self._start_time: float = 0

    # ========================================================================
    # Public API
    # ========================================================================

    async def execute(self, patient: TNPatientInputV2) -> TNExecutorOutputV2:
        """
        Execute the full patient creation workflow.

        Runs phases 0-5 in strict linear order. Stops on first failure.
        Returns structured output with logs and screenshots.
        """
        self._start_time = time.time()
        self._logs = []

        try:
            self._page = await self._runtime.ensure_browser()

            # ------ Phase 0: Entry ------
            result = await self._phase_entry()
            if not result:
                return self._build_failure_output()

            # ------ Phase 1: Login ------
            result = await self._phase_login()
            if not result:
                return self._build_failure_output()

            # ------ Phase 2: Navigate to Patients page ------
            result = await self._phase_navigate()
            if not result:
                return self._build_failure_output()

            # ------ Phase 3: Detect New Patient form ------
            result = await self._phase_detect_form()
            if not result:
                return self._build_failure_output()

            # ------ Phase 4: Fill required fields ------
            result = await self._phase_fill_required(patient)
            if not result:
                return self._build_failure_output()

            # ------ Phase 5: Save patient ------
            result = await self._phase_save_patient(patient)
            if not result:
                return self._build_failure_output()

            # ====================================================================
            # Step 3 — extended phases 6-8 (run only after a successful save).
            # Decision I2: strictly sequential, halt on first failure.
            # Decision I3: on failure here the patient already exists, so
            # _build_failure_output carries tn_patient_url/tn_patient_id.
            # ====================================================================

            # ------ Phase 6: Upload intake referral PDF ------
            result = await self._phase_upload_intake_pdf(patient)
            if not result:
                return self._build_failure_output()

            # ------ Phase 7: Upload appointment-confirmation snapshot PDF ------
            result = await self._phase_upload_snapshot_pdf(patient)
            if not result:
                return self._build_failure_output()

            # ------ Phase 8: Schedule the initial appointment ------
            result = await self._phase_schedule_appointment(patient)
            if not result:
                return self._build_failure_output()

            # All phases passed
            duration_ms = self._elapsed_ms()
            patient_name = f"{patient.first_name} {patient.last_name}"
            logger.info(f"WORKFLOW COMPLETE: {patient_name} created in {duration_ms}ms")
            return TNExecutorOutputV2.success(
                patient_name=patient_name,
                logs=self._logs,
                duration_ms=duration_ms,
                tn_patient_url=getattr(self, "_tn_patient_url", None),
                tn_patient_id=getattr(self, "_tn_patient_id", None),
            )

        except Exception as e:
            logger.exception(f"Unhandled executor error: {e}")
            screenshot = None
            if self._page:
                screenshot = await self._capture_screenshot("unhandled_error")
            self._record_log(
                TNPhaseV2.ENTRY, "failure",
                f"Unhandled error: {e}",
                screenshot,
            )
            return self._build_failure_output(
                phase_override=TNPhaseV2.ENTRY,
                reason_override="unknown_error",
                message_override=str(e),
            )

    # ========================================================================
    # Phase 0: Entry
    # ========================================================================

    async def _phase_entry(self) -> bool:
        """Navigate directly to TN login SPA, fill practice code."""
        phase = TNPhaseV2.ENTRY
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 0: ENTRY — Navigate to TherapyNotes login")
        logger.info("=" * 70)

        try:
            # Go directly to the login SPA — skip homepage entirely
            await self._page.goto(
                "https://www.therapynotes.com/app/login/",
                wait_until="domcontentloaded",
                timeout=self.STEP_TIMEOUT_MS,
            )

            # TN login is a JS SPA — wait for the practice code field to render
            # Use poll-based wait since the page hydrates after domcontentloaded
            practice_field_ready = await self._poll_condition(
                condition_fn=self._check_practice_code_visible,
                description="practice code field rendered",
                timeout_ms=15000,
            )

            if not practice_field_ready:
                # Capture what the browser actually sees
                await self._capture_screenshot("entry_no_practice_field")
                return await self._fail_phase(
                    phase, "selector_not_found",
                    "Practice code field did not render on /app/login/",
                    phase_start,
                )

            # Fill practice code
            practice_field = await self._resolve_selector("practice_code_field")
            if not practice_field:
                return await self._fail_phase(
                    phase, "selector_not_found",
                    "Practice code field found by poll but not by resolve",
                    phase_start,
                )
            await practice_field.fill(self._credentials.practice_code)

            # Submit practice code
            submit = await self._resolve_selector("practice_code_submit")
            if not submit:
                return await self._fail_phase(
                    phase, "selector_not_found",
                    "Practice code submit button not found",
                    phase_start,
                )
            await self._safe_click(submit, "practice code submit")
            logger.info("[ENTRY] Practice code submitted")

            # Wait for username field to appear (confirms step 2 loaded)
            username_ready = await self._poll_condition(
                condition_fn=self._check_username_visible,
                description="username field rendered (step 2)",
                timeout_ms=10000,
            )

            # Success screenshot — proof we passed practice code step
            await self._capture_screenshot("entry_success")

            if not username_ready:
                return await self._fail_phase(
                    phase, "practice_code_rejected",
                    "Username field did not appear after practice code submit — "
                    "practice code may be wrong or page did not advance",
                    phase_start,
                )

            self._record_log(phase, "success", "Practice code accepted, login form loaded", phase_start=phase_start)
            return True

        except Exception as e:
            return await self._fail_phase(phase, "unknown_error", str(e), phase_start)

    async def _check_practice_code_visible(self) -> bool:
        """Poll helper: check if any practice code input is visible."""
        for selector in SELECTORS.get("practice_code_field", []):
            try:
                el = await self._page.query_selector(selector)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _check_username_visible(self) -> bool:
        """Poll helper: check if the username field has appeared (step 2)."""
        for selector in SELECTORS.get("username_field", []):
            try:
                el = await self._page.query_selector(selector)
                if el and await el.is_visible():
                    return True
            except Exception:
                continue
        return False

    # ========================================================================
    # Phase 1: Login
    # ========================================================================

    async def _phase_login(self) -> bool:
        """Fill credentials from TNCredentials and confirm dashboard loads."""
        phase = TNPhaseV2.LOGIN
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 1: LOGIN — Authenticate to TherapyNotes")
        logger.info("=" * 70)

        try:
            # Wait for and fill username
            username_el = await self._resolve_selector("username_field")
            if not username_el:
                return await self._fail_phase(
                    phase, "selector_not_found",
                    "Username field not found on login page",
                    phase_start,
                )
            await username_el.fill(self._credentials.username)

            # Fill password
            password_el = await self._resolve_selector("password_field")
            if not password_el:
                return await self._fail_phase(
                    phase, "selector_not_found",
                    "Password field not found on login page",
                    phase_start,
                )
            await password_el.fill(self._credentials.password)

            # Submit
            submit_el = await self._resolve_selector("login_submit")
            if not submit_el:
                return await self._fail_phase(
                    phase, "selector_not_found",
                    "Login submit button not found",
                    phase_start,
                )
            await self._safe_click(submit_el, "login submit")

            # Post-submit: wait for initial DOM, record URL + title for diagnostics.
            try:
                await self._page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass

            post_submit_url = self._page.url
            try:
                post_submit_title = await self._page.title()
            except Exception:
                post_submit_title = "<unreadable>"
            logger.info(f"[LOGIN] Post-submit URL: {post_submit_url}")
            logger.info(f"[LOGIN] Post-submit title: {post_submit_title}")

            # PHI guard: post-submit screenshot captures the authenticated
            # dashboard (patient data). Persist only when TN_DEBUG_MODE=true.
            if os.environ.get("TN_DEBUG_MODE", "false").lower() == "true":
                await self._capture_screenshot("login_postsubmit")

            # Positive dashboard detection — the ONLY reliable success signal.
            # TN's login page is a SPA: the URL stays at /app/login/ even
            # after successful authentication (verified in production logs
            # where body showed full dashboard but URL was unchanged). So
            # URL transitions CANNOT be used as a signal. Instead, wait up
            # to 30s for any marker that only exists on the authenticated
            # dashboard. CSS-only selectors — no :has-text, no URL checks.
            dashboard_selector = (
                "nav, "
                "#sidebar, "
                ".main-nav, "
                "[data-testid='sidebar'], "
                "a[href*='/app/home' i], "
                "a[href*='/app/patients' i], "
                "a[href*='/app/todo' i], "
                "a[href*='logout' i]"
            )
            try:
                await self._page.wait_for_selector(
                    dashboard_selector,
                    state="visible",
                    timeout=30_000,
                )
                logger.info("[LOGIN] Dashboard markers visible — login successful")
                self._record_log(
                    phase, "success",
                    "Logged in, dashboard confirmed",
                    phase_start=phase_start,
                )
                return True
            except Exception:
                logger.warning("[LOGIN] Dashboard markers not visible within 30s — diagnosing failure")

            # Diagnose why we didn't land on the dashboard.

            # 1. Login form still visible → credentials rejected
            form_still_visible = False
            for sel in ("input#Login__UsernameField", "input#Login__Password"):
                try:
                    loc = self._page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible(timeout=500):
                        form_still_visible = True
                        break
                except Exception:
                    continue

            if form_still_visible:
                error_text = None
                login_error_el = await self._probe_selector("login_error", timeout_ms=2000)
                if login_error_el:
                    try:
                        error_text = (await login_error_el.inner_text()).strip()[:200]
                    except Exception:
                        pass
                body_snippet = await self._get_body_snippet(300)
                return await self._fail_phase(
                    phase, "login_failed",
                    f"Login form still visible after 30s — credentials rejected. "
                    f"Error banner: {error_text or 'none'} | Body: {body_snippet}",
                    phase_start,
                )

            # 2. Interstitial (OTP challenge, password expired, CAPTCHA)
            interstitial = await self._detect_post_login_interstitial()
            if interstitial:
                reason, message = interstitial
                return await self._fail_phase(phase, reason, message, phase_start)

            # 3. Unknown state — page is neither authenticated, nor login form, nor interstitial
            body_snippet = await self._get_body_snippet(300)
            return await self._fail_phase(
                phase, "dashboard_not_loaded",
                f"No dashboard markers, no login form, no known interstitial. "
                f"URL: {post_submit_url} | Title: {post_submit_title} | Body: {body_snippet}",
                phase_start,
            )

        except Exception as e:
            return await self._fail_phase(phase, "login_failed", str(e), phase_start)

    async def _detect_post_login_interstitial(self):
        """
        Detect post-login interstitials that prevent dashboard load.

        Returns (failure_reason, message) tuple if detected, else None.

        MFA detection is deliberately narrow — URL path markers + a visible
        OTP input field only. Body-text phrases like "two-factor" are NOT
        matched: TN's dashboard exposes an "Enroll in Two-Factor Authentication"
        suggestion that would false-positive on a healthy login.
        """
        try:
            url = self._page.url.lower()
        except Exception:
            url = ""

        for pattern in ("/mfa", "/2fa", "/otp", "/verify", "/challenge"):
            if pattern in url:
                return "mfa_required", f"Interstitial URL path '{pattern}' detected: {self._page.url}"

        # Visible OTP input — strong, unambiguous MFA signal. Enrollment
        # suggestions on the dashboard are links/buttons, not inputs.
        otp_selectors = (
            'input[autocomplete="one-time-code"]',
            'input[name="otp" i]',
            'input[name="totp" i]',
            'input[name="mfaCode" i]',
            'input[name="verificationCode" i]',
            'input[name="securityCode" i]',
        )
        for sel in otp_selectors:
            try:
                loc = self._page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible(timeout=500):
                    return "mfa_required", f"OTP input field detected ({sel})"
            except Exception:
                continue

        # Body-text signals: only tight blocker phrases. No MFA text — see docstring.
        try:
            body_text = (await self._page.inner_text("body", timeout=3000)).lower()
        except Exception:
            body_text = ""

        for phrase in ("your password has expired", "password must be changed"):
            if phrase in body_text:
                return "login_failed", f"Password change required: '{phrase}'"

        for phrase in ("i'm not a robot", "prove you are human"):
            if phrase in body_text:
                return "login_failed", f"CAPTCHA challenge detected: '{phrase}'"

        return None

    async def _get_body_snippet(self, max_chars: int = 300) -> str:
        """Get a compact body-text snippet for diagnostic error messages."""
        import re
        try:
            text = await self._page.inner_text("body", timeout=3000)
        except Exception:
            return "<body unreadable>"
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]

    # ========================================================================
    # Phase 2: Navigate to New Patient Form
    # ========================================================================

    async def _phase_navigate(self) -> bool:
        """Navigate sidebar → Patients page, confirm + New Patient button exists."""
        phase = TNPhaseV2.NAVIGATE
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 2: NAVIGATE — Sidebar → Patients")
        logger.info("=" * 70)

        try:
            # Let the dashboard settle after login before grabbing sidebar links.
            # Avoids a stale-ElementHandle race where the SPA re-renders the
            # sidebar between resolve and click. Timeout is non-fatal.
            try:
                await self._page.wait_for_load_state("networkidle", timeout=1500)
            except Exception:
                logger.info("[NAVIGATE] networkidle wait timed out — continuing")

            # Pre-clear any modal overlays that may have appeared after login
            await self._dismiss_blocking_dialogs()

            # Step 1: Click "Patients" link in the sidebar.
            # Use a Locator (auto-re-resolves on DOM re-render) instead of a
            # cached ElementHandle, which detaches if the SPA repaints mid-click.
            patients_loc = None
            for sel in SELECTORS["patients_link"]:
                loc = self._page.locator(sel).first
                try:
                    if await loc.count() > 0:
                        patients_loc = loc
                        break
                except Exception:
                    continue
            if patients_loc is None:
                return await self._fail_phase(
                    phase, "navigation_failed",
                    "Patients link not found in sidebar",
                    phase_start,
                )
            await patients_loc.click(timeout=self.STEP_TIMEOUT_MS)
            logger.info("[NAVIGATE] Clicked Patients")

            # Step 2: Wait for the Patients page to load
            patients_page = await self._resolve_selector("patients_page_indicator")
            if not patients_page:
                return await self._fail_phase(
                    phase, "navigation_failed",
                    "Patients page did not load after clicking Patients link",
                    phase_start,
                )
            logger.info("[NAVIGATE] Patients page loaded")

            # Step 3: Confirm "+ New Patient" button is present (do NOT click it)
            new_patient_btn = await self._resolve_selector("new_patient_button")
            if not new_patient_btn:
                return await self._fail_phase(
                    phase, "new_patient_form_not_found",
                    "New Patient button not found on patients page",
                    phase_start,
                )
            logger.info("[NAVIGATE] New Patient button detected")

            # Step 4: Capture screenshot of the Patients page
            await self._capture_screenshot("navigate_patients_page")

            self._record_log(phase, "success", "Patients page loaded, New Patient button confirmed", phase_start=phase_start)
            return True

        except Exception as e:
            return await self._fail_phase(phase, "navigation_failed", str(e), phase_start)

    # ========================================================================
    # Phase 3 (detection): Confirm New Patient form fields exist
    # ========================================================================

    async def _phase_detect_form(self) -> bool:
        """Click + New Patient, confirm core form fields exist. Does NOT fill."""
        phase = TNPhaseV2.FILL_FORM
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 3: FORM DETECTION — Confirm New Patient form fields")
        logger.info("=" * 70)

        try:
            # Pre-clear any modal overlays before clicking New Patient
            await self._dismiss_blocking_dialogs()

            # Step 1: Click "+ New Patient" using verified CSS selector
            url_before = self._page.url
            logger.info(f"[FORM DETECT] URL before click: {url_before}")

            new_patient_btn = await self._resolve_selector("new_patient_button")
            if not new_patient_btn:
                await self._capture_screenshot("new_patient_btn_not_found")
                return await self._fail_phase(
                    phase, "new_patient_form_not_found",
                    "New Patient button not found on Patients page",
                    phase_start,
                )
            await self._safe_click(new_patient_btn, "New Patient button")
            logger.info("[FORM DETECT] Clicked New Patient")

            # Step 2: Wait for URL to change to the edit page
            try:
                await self._page.wait_for_url("**/patients/edit/**", timeout=10000)
                logger.info(f"[FORM DETECT] URL after navigation: {self._page.url}")
            except Exception:
                logger.info(f"[FORM DETECT] URL did not change to edit page. Still: {self._page.url}")
                await self._capture_screenshot("url_no_change_after_click")
                return await self._fail_phase(
                    phase, "new_patient_form_not_found",
                    f"URL did not navigate to patients/edit after click. URL: {self._page.url}",
                    phase_start,
                )

            # Step 3: Wait for form to render, confirm first name field exists
            first_name_loc = self._page.locator("#PatientInformationEditor__FirstNameInput")
            try:
                await first_name_loc.wait_for(state="visible", timeout=10000)
            except Exception:
                await self._capture_screenshot("form_not_loaded")
                return await self._fail_phase(
                    phase, "new_patient_form_not_found",
                    f"First name field not visible after 10s. URL: {self._page.url}",
                    phase_start,
                )
            logger.info("[FORM DETECT] New Patient form loaded (first name field visible)")

            await self._capture_screenshot("form_detection_success")

            self._record_log(
                phase, "success",
                "New Patient form loaded, first name field confirmed",
                phase_start=phase_start,
            )
            return True

        except Exception as e:
            return await self._fail_phase(phase, "new_patient_form_not_found", str(e), phase_start)

    # ========================================================================
    # Phase 4: Fill Required Fields (does NOT save)
    # ========================================================================

    async def _phase_fill_required(self, patient: TNPatientInputV2) -> bool:
        """Fill required patient fields using verified DOM IDs. Does NOT click Save."""
        phase = TNPhaseV2.FILL_FORM
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 4: FILL REQUIRED FIELDS")
        logger.info("=" * 70)

        try:
            page = self._page

            # Helper: fill a field by exact locator, read back, confirm
            async def fill_and_confirm(selector: str, value: str, label: str) -> bool:
                loc = page.locator(selector)
                if await loc.count() == 0:
                    logger.error(f"[FILL] {label}: selector '{selector}' not found (count=0)")
                    return False
                await loc.fill(value)
                actual = await loc.input_value()
                if actual != value:
                    logger.warning(f"[FILL] {label}: mismatch — expected '{value}', got '{actual}'")
                    return False
                logger.info(f"[FILL] {label}: '{value}' confirmed")
                return True

            # 1. First Name
            if not await fill_and_confirm(
                "#PatientInformationEditor__FirstNameInput",
                patient.first_name, "First Name",
            ):
                return await self._fail_phase(phase, "form_field_not_found", "Could not fill First Name", phase_start)

            # 2. Last Name
            if not await fill_and_confirm(
                "#PatientInformationEditor__LastNameInput",
                patient.last_name, "Last Name",
            ):
                return await self._fail_phase(phase, "form_field_not_found", "Could not fill Last Name", phase_start)

            # 3. Date of Birth
            if not await fill_and_confirm(
                "#PatientInformationEditor__DOBInput",
                patient.dob, "Date of Birth",
            ):
                return await self._fail_phase(phase, "form_field_not_found", "Could not fill Date of Birth", phase_start)

            # 4. Address 1
            if not await fill_and_confirm(
                "#AddressEditorView__Address1Input_PatientAddress",
                patient.address, "Address 1",
            ):
                return await self._fail_phase(phase, "form_field_not_found", "Could not fill Address 1", phase_start)

            # 5. Zip Code — click, clear, type char-by-char, then blur
            zip_loc = page.locator("#AddressEditorView__PostalCodeInput_PatientAddress")
            if await zip_loc.count() == 0:
                return await self._fail_phase(phase, "form_field_not_found", "Zip field not found", phase_start)
            # press_sequentially types into the focused locator (not "wherever focus is")
            # and fires real keystroke events so TN's city-autocomplete still triggers.
            # keyboard.type proved flaky here (dropped chars → partial zip), so type,
            # read back, and retry once before failing.
            async def _type_zip() -> str:
                await zip_loc.click()
                await zip_loc.fill("")
                await zip_loc.press_sequentially(patient.zip, delay=100)
                try:
                    await page.wait_for_function(
                        "(selector, expected) => document.querySelector(selector).value === expected",
                        "#AddressEditorView__PostalCodeInput_PatientAddress",
                        patient.zip,
                        timeout=3000,
                    )
                except Exception:
                    pass
                return await zip_loc.input_value()

            actual_zip = await _type_zip()
            if actual_zip != patient.zip:
                logger.warning(f"[FILL] Zip mismatch on first attempt: '{actual_zip}' != '{patient.zip}', retrying once")
                actual_zip = await _type_zip()
            if actual_zip != patient.zip:
                return await self._fail_phase(phase, "form_field_not_found", f"Zip mismatch: '{actual_zip}' != '{patient.zip}'", phase_start)
            logger.info(f"[FILL] Zip: '{patient.zip}' confirmed")
            await zip_loc.press("Tab")
            await page.wait_for_timeout(500)
            logger.info("[FILL] Zip: Tab pressed (blur)")

            # 5b. Poll for city auto-populate
            city_loc = page.locator("#AddressEditorView__CityInput_PatientAddress")
            zip_ok = await self._poll_condition(
                condition_fn=lambda: self._check_locator_has_value(city_loc),
                description="zip autocomplete → city populated",
                timeout_ms=5000,
            )
            if not zip_ok:
                return await self._fail_phase(phase, "zip_autocomplete_failed", "City did not auto-populate after zip", phase_start)
            city_val = await city_loc.input_value()
            logger.info(f"[FILL] City auto-populated: '{city_val}'")

            # 6. Sex (radio) — use check(), fallback to click(force=True)
            sex_value = "0" if patient.sex == "Male" else "1"
            sex_loc = page.locator(f'input[name="Sex"][value="{sex_value}"]')
            if await sex_loc.count() == 0:
                return await self._fail_phase(phase, "form_field_not_found", f"Sex radio value={sex_value} not found", phase_start)
            try:
                await sex_loc.check()
                logger.info(f"[FILL] Sex: {patient.sex} (value={sex_value}) selected via check()")
            except Exception:
                await sex_loc.click(force=True)
                logger.info(f"[FILL] Sex: {patient.sex} (value={sex_value}) selected via click(force=True)")

            # 7. Email
            if not await fill_and_confirm(
                "#PatientInformationEditor__EmailInput",
                patient.email, "Email",
            ):
                return await self._fail_phase(phase, "form_field_not_found", "Could not fill Email", phase_start)

            # 8. Mobile Phone
            if not await fill_and_confirm(
                "#PatientInformationEditor__MobilePhoneInput",
                patient.phone, "Mobile Phone",
            ):
                return await self._fail_phase(phase, "form_field_not_found", "Could not fill Mobile Phone", phase_start)

            # Capture screenshot of filled form
            await self._capture_screenshot("fill_required_complete")

            self._record_log(
                phase, "success",
                "All required fields filled and confirmed",
                phase_start=phase_start,
            )
            return True

        except Exception as e:
            return await self._fail_phase(phase, "form_field_not_found", str(e), phase_start)

    async def _check_locator_has_value(self, locator) -> bool:
        """Poll helper: check if a locator's input has a non-empty value."""
        try:
            val = await locator.input_value()
            return bool(val and val.strip())
        except Exception:
            return False

    # ========================================================================
    # Phase 5: Save Patient
    # ========================================================================

    async def _phase_save_patient(self, patient: TNPatientInputV2) -> bool:
        """Click Save New Patient, confirm creation, detect errors/duplicates."""
        phase = TNPhaseV2.SAVE
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 5: SAVE PATIENT")
        logger.info("=" * 70)

        try:
            page = self._page

            # Pre-clear any modal overlays before saving
            await self._dismiss_blocking_dialogs()

            url_before = page.url

            # Step 1: Locate psy-button.button-save
            save_loc = page.locator("psy-button.button-save").first
            count = await page.locator("psy-button.button-save").count()
            logger.info(f"[SAVE] Selector 'psy-button.button-save' → count={count}")
            if count == 0:
                await self._capture_screenshot("save_button_not_found")
                return await self._fail_phase(phase, "save_failed", "psy-button.button-save not found (count=0)", phase_start)

            is_visible = await save_loc.is_visible()
            bbox = await save_loc.bounding_box()
            logger.info(f"[SAVE] Button state: visible={is_visible}, bbox={bbox}")

            # Step 2: Click
            await self._safe_click(save_loc, "Save button")
            logger.info("[SAVE] Save clicked")

            # Step 3: Wait for page response
            await page.wait_for_timeout(2000)

            url_after = page.url
            logger.info(f"[SAVE] URL after save: {url_after}")

            # Step 4: Check for validation errors
            validation_errors = await page.evaluate("""
                () => {
                    const errors = [];
                    const summary = document.querySelector('.validation-summary-errors, .alert-danger, [role="alert"]');
                    if (summary && summary.offsetParent !== null) {
                        errors.push(summary.innerText.trim().slice(0, 300));
                    }
                    const redFields = document.querySelectorAll('input.input-validation-error, .field-validation-error');
                    if (redFields.length > 0) {
                        errors.push(redFields.length + ' field(s) have validation errors');
                    }
                    return errors;
                }
            """)

            if validation_errors:
                for err in validation_errors:
                    logger.warning(f"[SAVE] Validation error: {err}")
                await self._capture_screenshot("save_validation_errors")
                return await self._fail_phase(
                    phase, "save_failed",
                    f"Validation errors after save: {'; '.join(validation_errors)}",
                    phase_start,
                )

            # Step 5: Check for duplicate patient warning
            # Scoped to dialog/modal/alert containers first, falls back to body
            duplicate_text = await page.evaluate("""
                () => {
                    const containers = [
                        '.Dialog', '[role="dialog"]', '.modal',
                        '.validation-summary-errors', '.alert-danger',
                        '[role="alert"]', '#ElementDropbox .Dialog'
                    ];
                    for (const sel of containers) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetParent !== null) {
                            const text = el.innerText || '';
                            if (text.includes('duplicate') || text.includes('Duplicate') || text.includes('already exists')) {
                                return text.trim().slice(0, 200);
                            }
                        }
                    }
                    const body = document.body.innerText || '';
                    if (body.includes('already exists') || body.includes('duplicate') || body.includes('Duplicate')) {
                        const idx = body.indexOf('already exists');
                        if (idx >= 0) return body.slice(Math.max(0, idx - 30), idx + 80).trim();
                        const idx2 = body.indexOf('uplicate');
                        if (idx2 >= 0) return body.slice(Math.max(0, idx2 - 30), idx2 + 80).trim();
                    }
                    return null;
                }
            """)

            if duplicate_text:
                logger.warning(f"[SAVE] Duplicate detected: {duplicate_text}")
                await self._capture_screenshot("save_duplicate_detected")
                return await self._fail_phase(
                    phase, "patient_duplicate_detected",
                    f"Duplicate patient warning: {duplicate_text[:200]}",
                    phase_start,
                )

            # Step 6: Capture patient URL and extract ID
            self._tn_patient_url = page.url
            self._tn_patient_id = None

            # Try to extract patient ID from URL (e.g. /app/patients/view/12345)
            import re
            id_match = re.search(r'/patients/(?:view|edit|detail)/(\d+)', page.url)
            if id_match:
                self._tn_patient_id = id_match.group(1)
            else:
                # Try query param (e.g. ?patientId=12345)
                id_match = re.search(r'[?&]patientId=(\d+)', page.url)
                if id_match:
                    self._tn_patient_id = id_match.group(1)

            logger.info(f"[SAVE] tn_patient_url: {self._tn_patient_url}")
            logger.info(f"[SAVE] tn_patient_id: {self._tn_patient_id}")

            # Step 7: Confirm patient name visible
            expected_name = f"{patient.first_name} {patient.last_name}"
            name_on_page = await page.evaluate(
                "(name) => document.body.innerText.includes(name)",
                expected_name,
            )
            logger.info(f"[SAVE] Patient name '{expected_name}' on page: {name_on_page}")

            await self._capture_screenshot("save_complete")

            self._record_log(
                phase, "success",
                f"Patient '{expected_name}' saved successfully",
                phase_start=phase_start,
            )
            logger.info("[SAVE] Patient created successfully")
            return True

        except Exception as e:
            return await self._fail_phase(phase, "save_failed", str(e), phase_start)

    # ========================================================================
    # Step 3 — Phase 6/7: PDF upload (intake + snapshot)
    # ========================================================================

    async def _phase_upload_intake_pdf(self, patient: TNPatientInputV2) -> bool:
        """Download the intake PDF and upload it as 'Intake Referral'."""
        phase = TNPhaseV2.UPLOAD_INTAKE_PDF
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 6: UPLOAD INTAKE PDF")
        logger.info("=" * 70)
        return await self._run_pdf_upload_phase(
            phase=phase,
            phase_start=phase_start,
            url=patient.intake_pdf_url,
            document_name="Intake Referral",
            upload_fail_reason="intake_pdf_upload_failed",
        )

    async def _phase_upload_snapshot_pdf(self, patient: TNPatientInputV2) -> bool:
        """Download the snapshot PDF and upload it as 'Initial Appointment Confirmation Email'."""
        phase = TNPhaseV2.UPLOAD_SNAPSHOT_PDF
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 7: UPLOAD SNAPSHOT PDF")
        logger.info("=" * 70)
        return await self._run_pdf_upload_phase(
            phase=phase,
            phase_start=phase_start,
            url=patient.snapshot_pdf_url,
            document_name="Initial Appointment Confirmation Email",
            upload_fail_reason="snapshot_pdf_upload_failed",
        )

    async def _run_pdf_upload_phase(
        self,
        phase: TNPhaseV2,
        phase_start: float,
        url: str,
        document_name: str,
        upload_fail_reason: TNFailureReasonV2,
    ) -> bool:
        """Shared body for both PDF upload phases: download -> upload -> cleanup."""
        if not getattr(self, "_tn_patient_url", None):
            return await self._fail_phase(
                phase, "pdf_upload_ui_not_found",
                "No patient URL available from the save phase — cannot locate the record",
                phase_start,
            )

        pdf_path = None
        try:
            try:
                pdf_path = await self._download_pdf_to_tempfile(url)
            except PdfFormatError as e:
                return await self._fail_phase(phase, "pdf_unsupported_format", str(e), phase_start)
            except Exception as e:
                return await self._fail_phase(
                    phase, "pdf_download_failed",
                    f"PDF download failed for {document_name!r}: {e}",
                    phase_start,
                )

            return await self._upload_pdf_to_patient(
                self._tn_patient_url, pdf_path, document_name, phase, upload_fail_reason
            )
        finally:
            if pdf_path:
                try:
                    os.unlink(pdf_path)
                    logger.info(f"[PDF] Tempfile cleaned up: {pdf_path}")
                except OSError as e:
                    logger.warning(f"[PDF] Tempfile cleanup failed for {pdf_path}: {e}")

    async def _download_pdf_to_tempfile(self, url: str) -> str:
        """
        Download a PDF from `url` to a tempfile (decision I12).

        - 30s total timeout, follows redirects.
        - Aborts if the streamed body exceeds PDF_MAX_BYTES (25 MB).
        - Verifies first bytes are the %PDF magic header (raises PdfFormatError).
        - Returns the tempfile path. Caller owns cleanup (os.unlink in finally).
        - Sends X-API-Key (TN_API_KEY) so the CRM's gated intake-PDF endpoint
          authorizes the fetch. Harmless for URLs that ignore the header.
        """
        import httpx

        # Authenticate to the CRM's gated PDF endpoint with the same key the V2
        # route uses for inbound auth. Sent on every PDF fetch; targets that
        # don't require it simply ignore the unknown header.
        api_key = os.environ.get("TN_API_KEY")
        headers = {"X-API-Key": api_key} if api_key else {}

        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        path = tmp.name
        tmp.close()

        total = 0
        try:
            async with httpx.AsyncClient(
                timeout=PDF_DOWNLOAD_TIMEOUT_S, follow_redirects=True
            ) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    resp.raise_for_status()
                    with open(path, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > PDF_MAX_BYTES:
                                raise ValueError(
                                    f"PDF exceeds max size {PDF_MAX_BYTES} bytes"
                                )
                            f.write(chunk)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise

        # Magic-byte check — do not trust caller-provided URLs to be PDFs.
        with open(path, "rb") as f:
            head = f.read(5)
        if not head.startswith(b"%PDF"):
            try:
                os.unlink(path)
            except OSError:
                pass
            raise PdfFormatError(
                f"Downloaded file is not a PDF (magic bytes: {head!r}, {total} bytes)"
            )

        logger.info(f"[PDF] Downloaded {total} bytes -> {path}")
        return path

    async def _upload_pdf_to_patient(
        self,
        patient_url: str,
        pdf_path: str,
        document_name: str,
        phase: TNPhaseV2,
        upload_fail_reason: TNFailureReasonV2,
    ) -> bool:
        """
        Upload a PDF to the patient's record via the Documents tab modal.

        Pre-condition: page is on the patient record (any tab). Steps mirror the
        recon (docs/selectors/tn_v2_phases.md): Documents tab -> Upload Patient
        File -> set file -> type name -> Escape -> wait Add Document enabled ->
        click -> confirm via new list row / success banner.
        """
        phase_start = time.time()
        page = self._page

        # Defensive: ensure we're on the right patient record before uploading.
        if patient_url and patient_url.rstrip("/") not in page.url:
            try:
                await page.goto(patient_url, wait_until="domcontentloaded", timeout=self.STEP_TIMEOUT_MS)
                await asyncio.sleep(1)
            except Exception:
                pass

        await self._dismiss_blocking_dialogs()

        # Documents tab
        tab = await self._resolve_v2("documents_tab")
        if not tab:
            return await self._fail_phase(phase, "pdf_upload_ui_not_found", "Documents tab not found", phase_start)
        await self._safe_click(tab, "Documents tab")
        await asyncio.sleep(1.5)

        # Upload Patient File
        upload_btn = await self._resolve_v2("upload_patient_file_button")
        if not upload_btn:
            return await self._fail_phase(phase, "pdf_upload_ui_not_found", "'Upload Patient File' button not found", phase_start)
        await self._safe_click(upload_btn, "Upload Patient File")
        await asyncio.sleep(1.5)

        # File input (set_input_files works on the native input even if styled)
        file_in = page.locator(SELECTORS_V2["file_input"][0]).first
        if await file_in.count() == 0:
            return await self._fail_phase(phase, "pdf_upload_ui_not_found", "File input #InputUploader not found", phase_start)
        await file_in.set_input_files(pdf_path)

        # Document Name (free text, verbatim) + dismiss autocomplete (I4)
        name_in = await self._resolve_v2("document_name_input")
        if not name_in:
            return await self._fail_phase(phase, "pdf_upload_ui_not_found", "Document name input not found", phase_start)
        await name_in.fill(document_name)
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.4)

        # Wait for 'Add Document' to enable (I5 — disabled until file processed)
        enabled = await self._poll_condition(
            condition_fn=self._v2_add_document_enabled,
            description="'Add Document' enabled",
            timeout_ms=10000,
        )
        if not enabled:
            return await self._fail_phase(
                phase, upload_fail_reason,
                f"'Add Document' never enabled for {document_name!r} (file may not have processed)",
                phase_start,
            )

        add_btn = page.locator(SELECTORS_V2["add_document_button_enabled"][0]).first
        await self._safe_click(add_btn, "Add Document")

        # Confirm: a list row containing the exact document name (strong signal),
        # or the success banner (secondary).
        ok = await self._poll_condition(
            condition_fn=lambda: self._v2_upload_succeeded(document_name),
            description=f"document '{document_name}' uploaded",
            timeout_ms=15000,
        )
        if not ok:
            return await self._fail_phase(
                phase, upload_fail_reason,
                f"Upload of {document_name!r} not confirmed (no list row / banner)",
                phase_start,
            )

        await self._debug_screenshot(f"{phase.value}_uploaded")
        self._record_log(phase, "success", f"Uploaded '{document_name}'", phase_start=phase_start)
        logger.info(f"[UPLOAD] '{document_name}' confirmed")
        return True

    async def _v2_add_document_enabled(self) -> bool:
        try:
            return await self._page.locator(
                SELECTORS_V2["add_document_button_enabled"][0]
            ).count() > 0
        except Exception:
            return False

    async def _v2_upload_succeeded(self, document_name: str) -> bool:
        # Strong signal: a document list row containing the exact name.
        try:
            if await self._page.locator(f'tr:has-text("{document_name}")').count() > 0:
                return True
        except Exception:
            pass
        # Secondary: success banner visible.
        try:
            banner = self._page.locator(SELECTORS_V2["upload_success_banner"][0]).first
            if await banner.count() > 0 and await banner.is_visible():
                return True
        except Exception:
            pass
        return False

    # ========================================================================
    # Step 3 — Phase 8: Schedule appointment
    # ========================================================================

    async def _phase_schedule_appointment(self, patient: TNPatientInputV2) -> bool:
        """Navigate to scheduling and create the initial appointment via the dialog."""
        phase = TNPhaseV2.SCHEDULE_APPOINTMENT
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 8: SCHEDULE APPOINTMENT")
        logger.info("=" * 70)
        page = self._page

        try:
            # Navigate to scheduling
            await page.goto(
                "https://www.therapynotes.com/app/scheduling/",
                wait_until="domcontentloaded",
                timeout=self.STEP_TIMEOUT_MS,
            )
            await asyncio.sleep(2)
            await self._dismiss_blocking_dialogs()

            # Open the New Appointment dialog
            new_btn = await self._resolve_v2("new_appointment_button")
            if not new_btn:
                return await self._fail_phase(
                    phase, "scheduling_ui_not_found",
                    "'+ New' appointment button not found on scheduling page",
                    phase_start,
                )
            await self._safe_click(new_btn, "+ New appointment")
            await asyncio.sleep(2.5)

            # Patient search (existing patient — already created in earlier phases)
            ps = await self._resolve_v2("appt_patient_search")
            if not ps:
                return await self._fail_phase(
                    phase, "scheduling_ui_not_found",
                    "Patient search field not found in appointment dialog",
                    phase_start,
                )
            full_name = f"{patient.first_name} {patient.last_name}"
            await ps.click()
            await ps.fill("")
            await ps.press_sequentially(full_name, delay=80)
            found = await self._poll_condition(
                condition_fn=lambda: self._v2_incremental_result_visible(full_name),
                description=f"patient result '{full_name}'",
                timeout_ms=6000,
            )
            if not found:
                return await self._fail_phase(
                    phase, "appointment_creation_failed",
                    f"Patient '{full_name}' not found in scheduler search "
                    "(patient may not have persisted / search index lag)",
                    phase_start,
                )
            if not await self._click_incremental_result(full_name, "patient"):
                return await self._fail_phase(
                    phase, "appointment_creation_failed",
                    f"Could not click patient result '{full_name}'",
                    phase_start,
                )
            await asyncio.sleep(1.5)

            # Appointment Type = Therapy Intake (value 0) — I6
            type_sel = await self._resolve_v2("appt_type_select")
            if not type_sel:
                return await self._fail_phase(
                    phase, "appointment_creation_failed",
                    "Appointment Type <select> not found",
                    phase_start,
                )
            await page.select_option(SELECTORS_V2["appt_type_select"][0], value="0")
            await asyncio.sleep(1.5)

            # Modality inference — I8: check Telehealth iff alert text mentions it
            if "telehealth" in patient.appointment_alert_text.lower():
                try:
                    cb = page.locator(SELECTORS_V2["appt_telehealth_checkbox"][0]).first
                    if await cb.count() > 0 and not await cb.is_checked():
                        await cb.check()
                        logger.info("[SCHEDULE] Telehealth checkbox set (alert text contains 'Telehealth')")
                except Exception as e:
                    logger.warning(f"[SCHEDULE] Telehealth checkbox set failed: {e}")

            # Date + time, verbatim — I9
            d = await self._resolve_v2("appt_start_date")
            if not d:
                return await self._fail_phase(phase, "appointment_creation_failed", "Start date input not found", phase_start)
            await d.fill(patient.appointment_date)
            t = await self._resolve_v2("appt_start_time")
            if not t:
                return await self._fail_phase(phase, "appointment_creation_failed", "Start time input not found", phase_start)
            await t.fill(patient.appointment_time)

            # Clinician — manual select via type-to-filter DynamicDropdown (I7)
            if not await self._select_clinician(patient.clinician_name, phase, phase_start):
                return False  # _select_clinician already recorded the failure

            # Appointment Alert (free-text textarea)
            alert = await self._resolve_v2("appt_alert_textarea")
            if not alert:
                return await self._fail_phase(phase, "appointment_creation_failed", "Appointment Alert textarea not found", phase_start)
            await alert.fill(patient.appointment_alert_text)

            # Save — I14
            save = await self._resolve_v2("appt_save_button")
            if not save:
                return await self._fail_phase(phase, "appointment_creation_failed", "'Save New Appointment' button not found", phase_start)
            # TEMP DIAG: confirm Save is actually clickable. A disabled Save means a
            # required field isn't satisfied — click anyway to surface TN's behavior.
            try:
                save_disabled = await save.is_disabled()
                save_bbox = await save.bounding_box()
                logger.info(f"[SAVE] Save button pre-click: disabled={save_disabled}, bbox={save_bbox}")
                if save_disabled:
                    logger.warning("[SAVE] Save button is DISABLED — form likely incomplete. Clicking anyway to observe.")
            except Exception as e:
                logger.warning(f"[SAVE] Pre-click state check failed: {e}")
            await self._safe_click(save, "Save New Appointment")
            await asyncio.sleep(2)

            # Explicit error banner first (validation / conflict / missing field)
            err = await self._v2_scheduling_error()
            if err:
                return await self._fail_phase(
                    phase, "appointment_creation_failed",
                    f"Appointment save error: {err}",
                    phase_start,
                )

            # I15: success indicator UNVERIFIED in recon. Observed signal = dialog
            # closes. Timeout bumped 12s->20s as cheap insurance against a slow
            # WebForms save. Smoke test must confirm/refine and update recon doc.
            closed = await self._poll_condition(
                condition_fn=self._v2_appt_dialog_closed,
                description="appointment dialog closed",
                timeout_ms=20000,
            )
            await asyncio.sleep(2)  # settle
            await self._debug_screenshot("schedule_appointment_complete")

            if not closed:
                # TEMP DIAG: Save stays disabled with no surfaced error — capture
                # the full form field state so we can see which required field is
                # empty/wrong, side-by-side with what V2 intended to set.
                logger.info(
                    f"[DIAG] V2 EXPECTED VALUES: date='{patient.appointment_date}', "
                    f"time='{patient.appointment_time}', "
                    f"alert='{patient.appointment_alert_text}', "
                    f"clinician='{patient.clinician_name}'"
                )
                try:
                    field_state = await page.evaluate(
                        """() => {
                            const getById = (id) => {
                                const el = document.getElementById(id);
                                return el ? { found: true, value: el.value, disabled: el.disabled, tag: el.tagName } : { found: false };
                            };
                            const q = (sel) => { const el = document.querySelector(sel); return el ? el.value : 'NOT_FOUND'; };
                            return {
                                appointmentType: getById('CalendarEntryEditor__TypeSelect'),
                                dateInput: q('[id*="DateInput"], [class*="DateInput"]'),
                                timeStartInput: q('[id*="StartTime"], [id*="TimeInput"]'),
                                timeEndInput: q('[id*="EndTime"]'),
                                durationInput: q('[id*="Duration"]'),
                                remindersTextArea: document.getElementById('CalendarEntryEditor__RemindersTextArea') ? document.getElementById('CalendarEntryEditor__RemindersTextArea').value : 'NOT_FOUND',
                                allTextareas: Array.from(document.querySelectorAll('textarea')).map(t => ({
                                    id: t.id, name: t.name, value: (t.value || '').substring(0, 200), visible: t.offsetParent !== null
                                })),
                                allDialogInputs: Array.from(document.querySelectorAll('[role="dialog"] input')).map(i => ({
                                    id: i.id, name: i.name, type: i.type, value: i.value, disabled: i.disabled,
                                    visible: i.offsetParent !== null, placeholder: i.placeholder
                                })).filter(i => i.visible),
                                requiredEmpty: Array.from(document.querySelectorAll('[role="dialog"] [required], [role="dialog"] [aria-required="true"]'))
                                    .filter(el => !el.value || el.value.trim() === '')
                                    .map(el => ({ id: el.id, name: el.name, tag: el.tagName })),
                                saveButton: (() => {
                                    const btn = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"]'))
                                        .find(b => (b.textContent || b.value || '').includes('Save New Appointment'));
                                    return btn ? {
                                        disabled: btn.disabled,
                                        ariaDisabled: btn.getAttribute('aria-disabled'),
                                        title: btn.title,
                                        outerHTML: btn.outerHTML.substring(0, 500)
                                    } : null;
                                })()
                            };
                        }"""
                    )
                    logger.info(f"[DIAG] APPOINTMENT FORM FIELD STATE: {field_state}")
                except Exception as e:
                    logger.warning(f"[DIAG] Could not capture form field state: {e}")

                # TEMP DIAG: the dialog-closed heuristic is unverified. Dump the
                # dialog state so we can see whether save actually committed
                # (dialog gone / success marker) vs. is stuck on a validation
                # error vs. needs a different button (Done/Close).
                try:
                    dialog_state = await page.evaluate(
                        """() => {
                            const dialog = document.querySelector('[role="dialog"]')
                                || document.querySelector('.psy-dialog')
                                || document.querySelector('.CalendarEntryEditor');
                            if (!dialog) return { dialogFound: false, url: location.href };
                            return {
                                dialogFound: true,
                                url: location.href,
                                visible: dialog.offsetParent !== null,
                                classes: dialog.className,
                                outerHTML_first_3000: dialog.outerHTML.substring(0, 3000),
                                errorMessages: Array.from(dialog.querySelectorAll('.error, .validation-error, .error-message, [class*="error"], [class*="Error"]'))
                                    .map(e => ({ text: e.textContent.trim(), classes: e.className }))
                                    .filter(e => e.text.length > 0),
                                buttons: Array.from(dialog.querySelectorAll('button, input[type="button"], input[type="submit"]'))
                                    .map(b => ({
                                        text: (b.textContent || b.value || '').trim(),
                                        classes: b.className,
                                        disabled: b.disabled,
                                        visible: b.offsetParent !== null
                                    })),
                                successMarkers: Array.from(dialog.querySelectorAll('.success, [class*="success"], [class*="Success"]'))
                                    .map(e => e.textContent.trim()).filter(t => t.length > 0)
                            };
                        }"""
                    )
                    logger.info(f"[DIAG] APPOINTMENT DIALOG state at save-timeout: {dialog_state}")
                except Exception as e:
                    logger.warning(f"[DIAG] Could not capture dialog state: {e}")

                return await self._fail_phase(
                    phase, "appointment_creation_failed",
                    "Appointment dialog did not close after Save and no explicit error was shown",
                    phase_start,
                )

            self._record_log(
                phase, "success",
                f"Appointment scheduled ({patient.appointment_date} {patient.appointment_time}, "
                f"clinician '{patient.clinician_name}')",
                phase_start=phase_start,
            )
            logger.info("[SCHEDULE] Appointment created (dialog closed; verify selector in smoke test)")
            return True

        except Exception as e:
            return await self._fail_phase(phase, "appointment_creation_failed", str(e), phase_start)

    async def _select_clinician(
        self, clinician_name: str, phase: TNPhaseV2, phase_start: float
    ) -> bool:
        """
        Select a clinician via the type-to-filter DynamicDropdown
        (#CalendarEntryEditor__ClinicianSelect). Click to activate -> type name
        into the inner input -> wait for incremental-search result -> click match.
        0 results => clinician_selection_failed.
        """
        page = self._page

        # Activate the dropdown so its inner textbox appears
        dd = await self._resolve_v2("appt_clinician_dropdown")
        if dd:
            try:
                await dd.click()
            except Exception:
                pass
        await asyncio.sleep(0.3)  # widget reveal animation

        inp = await self._resolve_v2("appt_clinician_input")
        if not inp:
            return await self._fail_phase(
                phase, "clinician_selection_failed",
                "Clinician DynamicDropdown input not found",
                phase_start,
            )
        # Ensure focus lands on the inner input (not the wrapper) before typing.
        try:
            await inp.click()
            await inp.focus()  # belt-and-suspenders
            await inp.fill("")
        except Exception:
            pass
        await inp.press_sequentially(clinician_name, delay=80)

        # Confirm the characters actually landed in the input. If empty, focus
        # didn't transfer — retry once via keyboard.type after re-focusing.
        try:
            typed_value = await inp.input_value()
        except Exception:
            typed_value = ""
        logger.info(f"[CLINICIAN] Input value after typing '{clinician_name}': '{typed_value}'")
        if not typed_value:
            logger.warning("[CLINICIAN] press_sequentially did not land — retrying with keyboard.type")
            try:
                await inp.focus()
                await page.keyboard.type(clinician_name, delay=80)
                typed_value = await inp.input_value()
            except Exception:
                typed_value = ""
            logger.info(f"[CLINICIAN] After fallback retry, input value: '{typed_value}'")

        # The clinician DynamicDropdown renders results in its OWN in-widget
        # listbox — <a role="option"> links with the name in a
        # .IncrementalSearchLink-FirstText span — NOT the page-wide
        # .ContentBubble.IncrementalSearch the patient flow uses. The search
        # fires fine on synthetic input; we were polling the wrong selector.
        result_selector = "#CalendarEntryEditor__ClinicianSelect [role='listbox'] [role='option']"
        tokens = set(_name_tokens(clinician_name))

        async def _has_results() -> bool:
            try:
                return await page.locator(result_selector).count() > 0
            except Exception:
                return False

        found = await self._poll_condition(
            condition_fn=_has_results,
            description=f"clinician result '{clinician_name}'",
            timeout_ms=5000,
        )
        if not found:
            return await self._fail_phase(
                phase, "clinician_selection_failed",
                f"No clinician results rendered for '{clinician_name}'",
                phase_start,
            )

        # Token-match against each option's FirstText span (order-independent,
        # tolerant of a 'Last, First' layout and credential suffixes). Click the
        # matching <a role="option"> directly.
        results = await page.query_selector_all(result_selector)
        matched = None
        rendered: List[str] = []
        for result in results:
            span = await result.query_selector(".IncrementalSearchLink-FirstText")
            if span:
                text = (await span.text_content()) or ""
            else:
                text = (await result.text_content()) or ""
            rendered.append(text.strip())
            if tokens.issubset(set(_name_tokens(text))):
                matched = result
                break

        if matched is None:
            logger.warning(f"[CLINICIAN] No token match for '{clinician_name}'. Rendered: {rendered}")
            return await self._fail_phase(
                phase, "clinician_selection_failed",
                f"No clinician match for '{clinician_name}' in dropdown (rendered: {rendered})",
                phase_start,
            )

        logger.info(f"[CLINICIAN] Matched '{clinician_name}' among {rendered} — clicking")
        await matched.click()
        await asyncio.sleep(1)
        logger.info(f"[SCHEDULE] Clinician selected: {clinician_name}")
        return True

    async def _click_incremental_result(
        self, text: str, label: str, match_tokens: Optional[List[str]] = None
    ) -> bool:
        """Click the incremental-search result bubble matching `text`.

        Default (match_tokens=None): exact substring via Playwright has_text —
        used by the patient flow, which renders 'First Last DOB: ...'.
        match_tokens set: pick the first visible bubble whose tokens are a
        superset of match_tokens (order-independent) — used by the clinician
        flow, which renders 'Last, First[, Credential]'.
        """
        if match_tokens is not None:
            loc = await self._find_incremental_bubble_by_tokens(match_tokens)
            if loc is not None:
                await self._safe_click(loc, f"{label} result '{text}'")
                return True
            return False
        for sel in SELECTORS_V2["appt_incremental_result"]:
            try:
                loc = self._page.locator(sel).filter(has_text=text).first
                if await loc.count() > 0:
                    await self._safe_click(loc, f"{label} result '{text}'")
                    return True
            except Exception:
                continue
        return False

    async def _v2_incremental_result_visible(
        self, text: str, match_tokens: Optional[List[str]] = None
    ) -> bool:
        if match_tokens is not None:
            return (await self._find_incremental_bubble_by_tokens(match_tokens)) is not None
        for sel in SELECTORS_V2["appt_incremental_result"]:
            try:
                loc = self._page.locator(sel).filter(has_text=text)
                if await loc.count() > 0 and await loc.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _find_incremental_bubble_by_tokens(self, match_tokens: List[str]):
        """Return the first visible incremental-search bubble whose tokens are a
        superset of `match_tokens` (case-insensitive, order-independent), or None.

        Logs a warning if more than one bubble matches (picks the first).
        """
        want = set(match_tokens)
        matches = []
        for sel in SELECTORS_V2["appt_incremental_result"]:
            try:
                loc = self._page.locator(sel)
                n = await loc.count()
                for i in range(n):
                    item = loc.nth(i)
                    try:
                        if not await item.is_visible():
                            continue
                        txt = await item.inner_text()
                    except Exception:
                        continue
                    if want.issubset(set(_name_tokens(txt))):
                        matches.append((item, txt.strip()))
            except Exception:
                continue
            if matches:
                break  # first selector tier that yields matches wins
        if not matches:
            return None
        if len(matches) > 1:
            picked = matches[0][1]
            others = [m[1] for m in matches[1:]]
            logger.warning(
                f"[CLINICIAN] Multiple matches for tokens {match_tokens} — "
                f"picking first: '{picked}' (others: {others})"
            )
        return matches[0][0]

    async def _v2_appt_dialog_closed(self) -> bool:
        try:
            dlg = self._page.locator(".Dialog, [role='dialog']")
            n = await dlg.count()
            if n == 0:
                return True
            for i in range(n):
                try:
                    if await dlg.nth(i).is_visible():
                        return False
                except Exception:
                    continue
            return True
        except Exception:
            return True

    async def _v2_scheduling_error(self) -> Optional[str]:
        try:
            return await self._page.evaluate(
                """() => {
                    const sels = ['.validation-summary-errors', '.field-validation-error',
                                  '.input-validation-error', '.alert-danger', '[role="alert"]'];
                    for (const s of sels) {
                        const e = document.querySelector(s);
                        if (e && e.offsetParent !== null) {
                            const t = (e.innerText || '').trim();
                            if (t) return t.slice(0, 200);
                        }
                    }
                    return null;
                }"""
            )
        except Exception:
            return None

    async def _resolve_v2(self, key: str, state: str = "visible", timeout_ms: Optional[int] = None):
        """Resolve a SELECTORS_V2 candidate list to a Playwright Locator (first match), or None."""
        timeout_ms = timeout_ms or self.STEP_TIMEOUT_MS
        for sel in SELECTORS_V2.get(key, []):
            try:
                loc = self._page.locator(sel).first
                await loc.wait_for(state=state, timeout=timeout_ms)
                return loc
            except Exception:
                continue
        logger.warning(f"[SELECTOR_V2] All candidates failed for: {key}")
        return None

    async def _debug_screenshot(self, label: str) -> None:
        """Capture a screenshot only when TN_DEBUG_MODE=true (PHI gating per recon)."""
        if os.environ.get("TN_DEBUG_MODE", "false").lower() == "true":
            await self._capture_screenshot(label)

    # ========================================================================
    # Selector Resolution — tries candidates in order, returns first match
    # ========================================================================

    async def _resolve_selector(self, selector_key: str) -> Optional[object]:
        """
        Try each candidate selector for a given key. Return the first
        ElementHandle that is visible and attached, or None.
        """
        candidates = SELECTORS.get(selector_key, [])
        if not candidates:
            logger.error(f"[SELECTOR] No candidates defined for key: {selector_key}")
            return None

        for selector in candidates:
            try:
                element = await self._page.wait_for_selector(
                    selector,
                    state="visible",
                    timeout=self.STEP_TIMEOUT_MS,
                )
                if element:
                    logger.info(f"[SELECTOR] Resolved '{selector_key}' via: {selector}")
                    return element
            except Exception:
                continue

        logger.warning(f"[SELECTOR] All candidates failed for: {selector_key}")
        return None

    async def _probe_selector(self, selector_key: str, timeout_ms: int = 3000) -> Optional[object]:
        """
        Fast, non-blocking probe. Returns element if found within timeout,
        None otherwise. Does NOT fail the phase.
        """
        candidates = SELECTORS.get(selector_key, [])
        for selector in candidates:
            try:
                element = await self._page.wait_for_selector(
                    selector,
                    state="visible",
                    timeout=timeout_ms,
                )
                if element:
                    return element
            except Exception:
                continue
        return None

    async def _check_text_on_page(self, text: str) -> bool:
        """Check if specific text exists anywhere in the page body."""
        try:
            content = await self._page.inner_text("body")
            return text.lower() in content.lower()
        except Exception:
            return False

    # ========================================================================
    # Dialog Dismissal & Safe Click — resilient against TN modal overlays
    # ========================================================================

    async def _dismiss_blocking_dialogs(self) -> bool:
        """
        Detect and dismiss TherapyNotes modal dialogs that block pointer events.

        Targets <div class="Dialog"> inside <div id="ElementDropbox"> and
        standard [role="dialog"] overlays (session warnings, insurance alerts,
        system confirmations). Returns True if any dialog was dismissed.
        """
        dialog_close_selectors = [
            '.Dialog button:has-text("Close")',
            '.Dialog button:has-text("OK")',
            '.Dialog button:has-text("Continue")',
            '.Dialog button:has-text("Cancel")',
            '.Dialog button:has-text("Yes")',
            '.Dialog button:has-text("No")',
            '.Dialog button:has-text("Dismiss")',
            '#ElementDropbox .Dialog button',
            '[role="dialog"] button:has-text("Close")',
            '[role="dialog"] button:has-text("OK")',
            '[role="dialog"] button:has-text("Continue")',
            '.modal button:has-text("Close")',
            '.modal button:has-text("OK")',
            'button.dialog-close',
            '.Dialog .close',
        ]

        for selector in dialog_close_selectors:
            try:
                btn = self._page.locator(selector).first
                if await btn.count() > 0 and await btn.is_visible(timeout=500):
                    await btn.click(timeout=2000)
                    logger.info(f"[TN AGENT] Dialog dismissed via: {selector}")
                    await asyncio.sleep(0.3)
                    return True
            except Exception:
                continue

        for overlay_sel in ['.Dialog', '[role="dialog"]', '#ElementDropbox .Dialog']:
            try:
                overlay = self._page.locator(overlay_sel).first
                if await overlay.count() > 0 and await overlay.is_visible(timeout=500):
                    await self._page.keyboard.press("Escape")
                    logger.info(f"[TN AGENT] Dialog dismissed via Escape (overlay: {overlay_sel})")
                    await asyncio.sleep(0.3)
                    try:
                        still_visible = await overlay.is_visible(timeout=500)
                    except Exception:
                        still_visible = False
                    if not still_visible:
                        return True
            except Exception:
                continue

        return False

    async def _safe_click(self, element_or_locator, label: str = "element") -> None:
        """
        Click with automatic dialog dismissal on interception failure.

        Attempts a normal click first. If the click fails because a TN modal
        overlay intercepts pointer events, dismisses the dialog and retries.
        Falls back to Escape key if button-based dismissal doesn't work.
        """
        try:
            await element_or_locator.click(timeout=self.STEP_TIMEOUT_MS)
            return
        except Exception as first_err:
            err_str = str(first_err)
            if "intercepts pointer events" not in err_str and "timeout" not in err_str.lower():
                raise

            logger.warning(f"[TN AGENT] Click blocked on '{label}': {err_str[:200]}")
            logger.info("[TN AGENT] Attempting to dismiss blocking dialog...")

        dismissed = await self._dismiss_blocking_dialogs()
        if dismissed:
            logger.info(f"[TN AGENT] Retrying click on '{label}' after dialog dismissal")
        try:
            await element_or_locator.click(timeout=self.STEP_TIMEOUT_MS)
            return
        except Exception as second_err:
            logger.warning(f"[TN AGENT] Retry 1 failed on '{label}': {str(second_err)[:200]}")

        await self._page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        logger.info(f"[TN AGENT] Retrying click on '{label}' after Escape")
        await element_or_locator.click(timeout=self.STEP_TIMEOUT_MS)

    # ========================================================================
    # Poll-based Waiting (no fixed timeouts)
    # ========================================================================

    async def _poll_condition(
        self,
        condition_fn,
        description: str,
        timeout_ms: int = 8000,
    ) -> bool:
        """
        Poll a condition function until it returns True or timeout.

        condition_fn: async callable returning bool.
        No fixed sleep — uses short poll intervals.
        """
        deadline = time.time() + (timeout_ms / 1000)
        attempt = 0

        while time.time() < deadline:
            attempt += 1
            try:
                result = await condition_fn()
                if result:
                    logger.info(f"[POLL] '{description}' satisfied after {attempt} polls")
                    return True
            except Exception:
                pass
            await asyncio.sleep(self.POLL_INTERVAL_MS / 1000)

        logger.warning(f"[POLL] '{description}' timed out after {timeout_ms}ms ({attempt} polls)")
        return False

    # ========================================================================
    # Screenshot Capture
    # ========================================================================

    async def _capture_screenshot(self, label: str) -> Optional[str]:
        """
        Capture a screenshot on failure. Returns file path or None.
        """
        try:
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            timestamp = int(time.time() * 1000)
            filename = f"tnv2_{label}_{timestamp}.png"
            filepath = os.path.join(SCREENSHOT_DIR, filename)
            await self._page.screenshot(path=filepath, full_page=True)
            logger.info(f"[SCREENSHOT] Captured: {filepath}")
            return filepath
        except Exception as e:
            logger.error(f"[SCREENSHOT] Failed to capture '{label}': {e}")
            return None

    # ========================================================================
    # Logging & Failure Helpers
    # ========================================================================

    def _record_log(
        self,
        phase: TNPhaseV2,
        status: str,
        message: str,
        screenshot_path: Optional[str] = None,
        phase_start: Optional[float] = None,
    ) -> None:
        """Append a structured log entry."""
        duration_ms = int((time.time() - (phase_start or self._start_time)) * 1000)
        log_entry = TNPhaseLogV2(
            phase=phase,
            status=status,
            message=message,
            duration_ms=duration_ms,
            screenshot_path=screenshot_path,
        )
        self._logs.append(log_entry)
        log_prefix = "OK" if status == "success" else "FAIL"
        logger.info(f"[{log_prefix}] {phase.value}: {message} ({duration_ms}ms)")

    async def _fail_phase(
        self,
        phase: TNPhaseV2,
        reason: TNFailureReasonV2,
        message: str,
        phase_start: float,
    ) -> bool:
        """Record failure, capture screenshot, return False to halt workflow."""
        screenshot_path = await self._capture_screenshot(f"{phase.value}_failure")
        self._record_log(phase, "failure", message, screenshot_path, phase_start)
        self._pending_failure = {
            "phase": phase,
            "reason": reason,
            "message": message,
        }
        return False

    def _build_failure_output(
        self,
        phase_override: Optional[TNPhaseV2] = None,
        reason_override: Optional[TNFailureReasonV2] = None,
        message_override: Optional[str] = None,
    ) -> TNExecutorOutputV2:
        """Build failure output from the last recorded failure."""
        pending = getattr(self, "_pending_failure", {})
        phase = phase_override or pending.get("phase", TNPhaseV2.ENTRY)
        reason = reason_override or pending.get("reason", "unknown_error")
        message = message_override or pending.get("message", "Unknown failure")

        return TNExecutorOutputV2.failure(
            phase=phase,
            reason=reason,
            message=message,
            logs=self._logs,
            duration_ms=self._elapsed_ms(),
            # Partial-success (I3): patient may already exist if a post-save phase failed
            tn_patient_url=getattr(self, "_tn_patient_url", None),
            tn_patient_id=getattr(self, "_tn_patient_id", None),
        )

    def _elapsed_ms(self) -> int:
        return int((time.time() - self._start_time) * 1000)


# ============================================================================
# Concurrency guard — only one patient creation at a time
# ============================================================================
# NOTE: _execution_lock is imported from services.api.tn_executor (see top of
# file). V1 and V2 deliberately share ONE lock: both drive the same TherapyNotes
# service account, which cannot host two concurrent authenticated sessions.


# ============================================================================
# Module-level entry point (matches food_delivery_executor pattern)
# ============================================================================

async def run_tn_v2_patient_creation(
    runtime, patient: TNPatientInputV2
) -> TNExecutorOutputV2:
    """
    Execute the TN patient creation workflow.

    Loads credentials from environment BEFORE launching browser.
    Fails fast with a structured error if any credential is missing.
    Only one execution can run at a time (shared module-level asyncio.Lock).

    Args:
        runtime: PlaywrightRuntime instance.
        patient: Validated patient input data.

    Returns:
        TNExecutorOutputV2 with status, logs, and screenshots.
    """
    # Concurrency guard: only one patient creation at a time
    if _execution_lock.locked():
        logger.warning("TN patient creation rejected — another execution is in progress")
        return TNExecutorOutputV2.failure(
            phase=TNPhaseV2.ENTRY,
            reason="unknown_error",
            message="Another patient creation is already in progress",
            logs=[],
            duration_ms=0,
        )

    async with _execution_lock:
        # Fail fast: validate credentials before spending time on browser launch
        try:
            credentials = get_tn_credentials()
        except Exception as e:
            logger.error(f"TN credential validation failed: {e}")
            return TNExecutorOutputV2.failure(
                phase=TNPhaseV2.ENTRY,
                reason="login_failed",
                message=(
                    "Missing TherapyNotes credentials. Required env vars: "
                    "THERAPYNOTES_PRACTICE_CODE, THERAPYNOTES_USERNAME, THERAPYNOTES_PASSWORD"
                ),
                logs=[],
                duration_ms=0,
            )

        logger.info(f"TN credentials validated: {credentials.safe_display}")
        executor = TNExecutorV2(runtime, credentials)
        return await executor.execute(patient)
