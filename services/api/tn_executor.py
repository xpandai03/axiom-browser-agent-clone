"""
Therapy Notes Patient Creation Executor.

Deterministic, linear browser automation workflow that creates a patient
in TherapyNotes via headless Playwright.

Architecture:
- Single-session, no parallel execution.
- Every phase confirms DOM state before proceeding.
- Every phase logs success/failure with timestamps.
- On failure: capture screenshot, return structured error, stop.
- No fixed waitForTimeout. No networkidle. Poll-based waits only.

Phases:
  0. ENTRY    — Navigate to TN, handle practice code if required.
  1. LOGIN    — Fill credentials, confirm dashboard.
  2. NAVIGATE — Sidebar → Patients → + New Patient.
  3. FILL     — Fill patient form fields in strict order.
  4. SAVE     — Submit form, confirm patient created.
  5. RFS      — Insert referral note with RFS URL.

IMPORTANT: Selectors are placeholders. They MUST be mapped against
the real TherapyNotes DOM before production use.
"""

import asyncio
import logging
import os
import time
from typing import List, Optional

from shared.schemas.therapy_notes import (
    TNPatientInput,
    TNExecutorOutput,
    TNPhase,
    TNPhaseLog,
    TNFailureReason,
)
from services.api.config import TNCredentials, get_tn_credentials

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
        "button:has-text('New Patient')",
        "a:has-text('New Patient')",
        "button:has-text('+ New')",
        "a:has-text('+ New')",
        "button:has-text('Add Patient')",
        "[data-testid='new-patient-btn']",
    ],

    # ------------------------------------------------------------------
    # PHASE 3: Patient Form Fields
    # ------------------------------------------------------------------
    "field_first_name": [
        "input[name='FirstName']",
        "input#FirstName",
        "input[name='firstName']",
        "input[placeholder*='First']",
        "input[aria-label*='First Name']",
    ],
    "field_last_name": [
        "input[name='LastName']",
        "input#LastName",
        "input[name='lastName']",
        "input[placeholder*='Last']",
        "input[aria-label*='Last Name']",
    ],
    "field_dob": [
        "input[name='DateOfBirth']",
        "input#DateOfBirth",
        "input[name='dob']",
        "input[type='date']",
        "input[placeholder*='Date of Birth']",
        "input[placeholder*='MM/DD/YYYY']",
    ],
    "field_address": [
        "input[name='Address1']",
        "input#Address1",
        "input[name='address']",
        "input[placeholder*='Address']",
        "input[aria-label*='Address']",
    ],
    "field_zip": [
        "input[name='Zip']",
        "input#Zip",
        "input[name='zip']",
        "input[name='ZipCode']",
        "input[placeholder*='Zip']",
    ],
    "field_city": [
        "input[name='City']",
        "input#City",
        "input[name='city']",
        "input[placeholder*='City']",
    ],
    "sex_male_radio": [
        "input[type='radio'][value='Male']",
        "input[type='radio']#Male",
        "label:has-text('Male') input[type='radio']",
        "input[name='Sex'][value='Male']",
        "input[name='sex'][value='Male']",
    ],
    "sex_female_radio": [
        "input[type='radio'][value='Female']",
        "input[type='radio']#Female",
        "label:has-text('Female') input[type='radio']",
        "input[name='Sex'][value='Female']",
        "input[name='sex'][value='Female']",
    ],
    "field_email": [
        "input[name='Email']",
        "input#Email",
        "input[name='email']",
        "input[type='email']",
        "input[placeholder*='Email']",
    ],
    "field_phone": [
        "input[name='MobilePhone']",
        "input#MobilePhone",
        "input[name='phone']",
        "input[name='Phone']",
        "input[type='tel']",
        "input[placeholder*='Phone']",
    ],

    # ------------------------------------------------------------------
    # PHASE 4: Save
    # ------------------------------------------------------------------
    "save_button": [
        "button:has-text('Save New Patient')",
        "button:has-text('Save')",
        "button[type='submit']",
        "input[type='submit'][value*='Save']",
        "[data-testid='save-patient-btn']",
    ],
    "duplicate_alert": [
        ".duplicate-warning",
        "[role='alert']:has-text('already exists')",
        ".alert:has-text('duplicate')",
        "text=Patient already exists",
    ],
    "patient_detail_indicator": [
        ".patient-detail",
        "[data-testid='patient-detail']",
        "h1.patient-name",
        "h2.patient-name",
    ],

    # ------------------------------------------------------------------
    # PHASE 5: Insert RFS Note
    # ------------------------------------------------------------------
    "notes_tab": [
        "a:has-text('Notes')",
        "button:has-text('Notes')",
        "[data-testid='notes-tab']",
        "a[href*='notes']",
    ],
    "add_note_button": [
        "button:has-text('Add Note')",
        "button:has-text('New Note')",
        "a:has-text('Add Note')",
        "[data-testid='add-note-btn']",
    ],
    "note_title_field": [
        "input[name='Title']",
        "input#NoteTitle",
        "input[name='title']",
        "input[placeholder*='Title']",
    ],
    "note_body_field": [
        "textarea[name='Body']",
        "textarea#NoteBody",
        "textarea[name='body']",
        "textarea[name='content']",
        "[contenteditable='true']",
        ".note-editor textarea",
    ],
    "note_save_button": [
        "button:has-text('Save Note')",
        "button:has-text('Save')",
        "button[type='submit']",
    ],
    "note_saved_indicator": [
        "text=Intake Referral",
        ".note-item:has-text('Intake Referral')",
        "[data-testid='note-list'] :has-text('Intake Referral')",
    ],
}


# ============================================================================
# Screenshot directory
# ============================================================================

SCREENSHOT_DIR = os.environ.get(
    "TN_SCREENSHOT_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "screenshots", "tn"),
)


# ============================================================================
# TNExecutor
# ============================================================================

class TNExecutor:
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
        self._logs: List[TNPhaseLog] = []
        self._start_time: float = 0

    # ========================================================================
    # Public API
    # ========================================================================

    async def execute(self, patient: TNPatientInput) -> TNExecutorOutput:
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

            # ------ Phase 2: Navigate to New Patient ------
            result = await self._phase_navigate()
            if not result:
                return self._build_failure_output()

            # ------ Phase 3: Fill Patient Form ------
            result = await self._phase_fill_form(patient)
            if not result:
                return self._build_failure_output()

            # ------ Phase 4: Save Patient ------
            result = await self._phase_save(patient)
            if not result:
                return self._build_failure_output()

            # ------ Phase 5: Insert RFS Note ------
            result = await self._phase_insert_rfs(patient.rfs_url)
            if not result:
                return self._build_failure_output()

            # All phases passed
            duration_ms = self._elapsed_ms()
            patient_name = f"{patient.first_name} {patient.last_name}"
            logger.info(f"WORKFLOW COMPLETE: {patient_name} created in {duration_ms}ms")
            return TNExecutorOutput.success(
                patient_name=patient_name,
                logs=self._logs,
                duration_ms=duration_ms,
            )

        except Exception as e:
            logger.exception(f"Unhandled executor error: {e}")
            self._record_log(
                TNPhase.ENTRY, "failure",
                f"Unhandled error: {e}",
                await self._capture_screenshot("unhandled_error"),
            )
            return self._build_failure_output(
                phase_override=TNPhase.ENTRY,
                reason_override="unknown_error",
                message_override=str(e),
            )

    # ========================================================================
    # Phase 0: Entry
    # ========================================================================

    async def _phase_entry(self) -> bool:
        """Navigate directly to TN login SPA, fill practice code."""
        phase = TNPhase.ENTRY
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
            await submit.click()
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
        phase = TNPhase.LOGIN
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
            await submit_el.click()

            # Check for login error (fast probe — don't wait long)
            login_error = await self._probe_selector("login_error", timeout_ms=3000)
            if login_error:
                error_text = await login_error.inner_text()
                return await self._fail_phase(
                    phase, "login_failed",
                    f"Login error displayed: {error_text.strip()[:200]}",
                    phase_start,
                )

            # Confirm dashboard loaded
            dashboard = await self._resolve_selector("dashboard_indicator")
            if not dashboard:
                return await self._fail_phase(
                    phase, "dashboard_not_loaded",
                    "Dashboard indicator not found after login",
                    phase_start,
                )

            # Success screenshot — proof we hit the dashboard
            await self._capture_screenshot("login_success")

            self._record_log(phase, "success", "Logged in, dashboard confirmed", phase_start=phase_start)
            return True

        except Exception as e:
            return await self._fail_phase(phase, "login_failed", str(e), phase_start)

    # ========================================================================
    # Phase 2: Navigate to New Patient Form
    # ========================================================================

    async def _phase_navigate(self) -> bool:
        """Navigate sidebar → Patients → New Patient."""
        phase = TNPhase.NAVIGATE
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 2: NAVIGATE — Patients → New Patient")
        logger.info("=" * 70)

        try:
            # Click Patients link
            patients_link = await self._resolve_selector("patients_link")
            if not patients_link:
                return await self._fail_phase(
                    phase, "navigation_failed",
                    "Patients link not found in sidebar",
                    phase_start,
                )
            await patients_link.click()

            # Confirm patients page loaded
            patients_page = await self._resolve_selector("patients_page_indicator")
            if not patients_page:
                return await self._fail_phase(
                    phase, "navigation_failed",
                    "Patients page did not load after clicking Patients link",
                    phase_start,
                )

            # Click New Patient
            new_patient_btn = await self._resolve_selector("new_patient_button")
            if not new_patient_btn:
                return await self._fail_phase(
                    phase, "new_patient_form_not_found",
                    "New Patient button not found on patients page",
                    phase_start,
                )
            await new_patient_btn.click()

            # Confirm form loaded by waiting for first name field
            form_ready = await self._resolve_selector("field_first_name")
            if not form_ready:
                return await self._fail_phase(
                    phase, "new_patient_form_not_found",
                    "New patient form did not load (first name field not found)",
                    phase_start,
                )

            self._record_log(phase, "success", "New patient form loaded", phase_start=phase_start)
            return True

        except Exception as e:
            return await self._fail_phase(phase, "navigation_failed", str(e), phase_start)

    # ========================================================================
    # Phase 3: Fill Patient Form
    # ========================================================================

    async def _phase_fill_form(self, patient: TNPatientInput) -> bool:
        """Fill form fields in strict order with DOM confirmation."""
        phase = TNPhase.FILL_FORM
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 3: FILL FORM — Populate patient fields")
        logger.info("=" * 70)

        try:
            # 3.1 First Name
            if not await self._fill_field("field_first_name", patient.first_name, "First Name"):
                return await self._fail_phase(
                    phase, "form_field_not_found",
                    "Could not fill First Name field",
                    phase_start,
                )

            # 3.2 Last Name
            if not await self._fill_field("field_last_name", patient.last_name, "Last Name"):
                return await self._fail_phase(
                    phase, "form_field_not_found",
                    "Could not fill Last Name field",
                    phase_start,
                )

            # 3.3 Date of Birth
            if not await self._fill_field("field_dob", patient.dob, "Date of Birth"):
                return await self._fail_phase(
                    phase, "form_field_not_found",
                    "Could not fill Date of Birth field",
                    phase_start,
                )

            # 3.4 Address
            if not await self._fill_field("field_address", patient.address, "Address"):
                return await self._fail_phase(
                    phase, "form_field_not_found",
                    "Could not fill Address field",
                    phase_start,
                )

            # 3.5 Zip Code + autocomplete confirmation
            if not await self._fill_field("field_zip", patient.zip, "Zip"):
                return await self._fail_phase(
                    phase, "form_field_not_found",
                    "Could not fill Zip field",
                    phase_start,
                )

            # Poll for city auto-populate (no fixed timeout)
            zip_ok = await self._poll_condition(
                condition_fn=self._check_city_populated,
                description="zip autocomplete → city populated",
                timeout_ms=8000,
            )
            if not zip_ok:
                return await self._fail_phase(
                    phase, "zip_autocomplete_failed",
                    "City field did not auto-populate after entering zip code",
                    phase_start,
                )

            # 3.6 Administrative Sex (radio button)
            sex_selector_key = (
                "sex_male_radio" if patient.sex == "Male" else "sex_female_radio"
            )
            sex_radio = await self._resolve_selector(sex_selector_key)
            if not sex_radio:
                return await self._fail_phase(
                    phase, "form_field_not_found",
                    f"Could not find {patient.sex} radio button",
                    phase_start,
                )
            await sex_radio.click()
            logger.info(f"[FILL] Sex: {patient.sex} selected")

            # 3.7 Email
            if not await self._fill_field("field_email", patient.email, "Email"):
                return await self._fail_phase(
                    phase, "form_field_not_found",
                    "Could not fill Email field",
                    phase_start,
                )

            # 3.8 Phone
            if not await self._fill_field("field_phone", patient.phone, "Phone"):
                return await self._fail_phase(
                    phase, "form_field_not_found",
                    "Could not fill Phone field",
                    phase_start,
                )

            self._record_log(phase, "success", "All patient fields filled", phase_start=phase_start)
            return True

        except Exception as e:
            return await self._fail_phase(phase, "form_field_not_found", str(e), phase_start)

    # ========================================================================
    # Phase 4: Save Patient
    # ========================================================================

    async def _phase_save(self, patient: TNPatientInput) -> bool:
        """Click save, confirm patient creation, handle duplicates."""
        phase = TNPhase.SAVE
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 4: SAVE — Submit new patient form")
        logger.info("=" * 70)

        try:
            save_btn = await self._resolve_selector("save_button")
            if not save_btn:
                return await self._fail_phase(
                    phase, "save_failed",
                    "Save button not found",
                    phase_start,
                )
            await save_btn.click()

            # Check for duplicate alert (fast probe)
            duplicate = await self._probe_selector("duplicate_alert", timeout_ms=3000)
            if duplicate:
                return await self._fail_phase(
                    phase, "patient_duplicate_detected",
                    "TherapyNotes reported a duplicate patient",
                    phase_start,
                )

            # Confirm redirect to patient detail page
            detail = await self._resolve_selector("patient_detail_indicator")
            if not detail:
                return await self._fail_phase(
                    phase, "patient_confirmation_failed",
                    "Patient detail page did not load after save",
                    phase_start,
                )

            # Confirm patient name on detail page
            expected_name = f"{patient.first_name} {patient.last_name}"
            name_confirmed = await self._poll_condition(
                condition_fn=lambda: self._check_text_on_page(expected_name),
                description=f"patient name '{expected_name}' visible on detail page",
                timeout_ms=8000,
            )
            if not name_confirmed:
                return await self._fail_phase(
                    phase, "patient_confirmation_failed",
                    f"Patient name '{expected_name}' not found on detail page",
                    phase_start,
                )

            self._record_log(
                phase, "success",
                f"Patient '{expected_name}' saved and confirmed",
                phase_start=phase_start,
            )
            return True

        except Exception as e:
            return await self._fail_phase(phase, "save_failed", str(e), phase_start)

    # ========================================================================
    # Phase 5: Insert RFS Note
    # ========================================================================

    async def _phase_insert_rfs(self, rfs_url: str) -> bool:
        """Navigate to notes, create Intake Referral note with RFS URL."""
        phase = TNPhase.INSERT_RFS
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 5: INSERT RFS — Add referral note")
        logger.info("=" * 70)

        try:
            # Navigate to Notes section
            notes_tab = await self._resolve_selector("notes_tab")
            if not notes_tab:
                return await self._fail_phase(
                    phase, "rfs_note_creation_failed",
                    "Notes tab not found on patient detail page",
                    phase_start,
                )
            await notes_tab.click()

            # Click Add Note
            add_note = await self._resolve_selector("add_note_button")
            if not add_note:
                return await self._fail_phase(
                    phase, "rfs_note_creation_failed",
                    "Add Note button not found",
                    phase_start,
                )
            await add_note.click()

            # Fill title
            title_field = await self._resolve_selector("note_title_field")
            if not title_field:
                return await self._fail_phase(
                    phase, "rfs_note_creation_failed",
                    "Note title field not found",
                    phase_start,
                )
            await title_field.fill("Intake Referral")

            # Fill body with RFS URL
            body_field = await self._resolve_selector("note_body_field")
            if not body_field:
                return await self._fail_phase(
                    phase, "rfs_note_creation_failed",
                    "Note body field not found",
                    phase_start,
                )
            await body_field.fill(rfs_url)

            # Save note
            save_note = await self._resolve_selector("note_save_button")
            if not save_note:
                return await self._fail_phase(
                    phase, "rfs_note_creation_failed",
                    "Note save button not found",
                    phase_start,
                )
            await save_note.click()

            # Confirm note appears
            note_confirmed = await self._resolve_selector("note_saved_indicator")
            if not note_confirmed:
                return await self._fail_phase(
                    phase, "rfs_note_creation_failed",
                    "Note 'Intake Referral' not found in notes list after save",
                    phase_start,
                )

            self._record_log(phase, "success", "RFS note created and confirmed", phase_start=phase_start)
            return True

        except Exception as e:
            return await self._fail_phase(phase, "rfs_note_creation_failed", str(e), phase_start)

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

    # ========================================================================
    # Form Helpers
    # ========================================================================

    async def _fill_field(
        self, selector_key: str, value: str, label: str
    ) -> bool:
        """Resolve a field selector, fill it, confirm the value took."""
        element = await self._resolve_selector(selector_key)
        if not element:
            logger.error(f"[FILL] Field not found: {label} ({selector_key})")
            return False

        await element.fill(value)

        # Confirm value was set by reading it back
        actual = await element.input_value()
        if actual != value:
            logger.warning(
                f"[FILL] Value mismatch for {label}: expected '{value}', got '{actual}'"
            )
            # Try once more with click + clear + type
            await element.click()
            await self._page.keyboard.press("Control+a")
            await self._page.keyboard.type(value)
            actual = await element.input_value()
            if actual != value:
                logger.error(f"[FILL] Retry failed for {label}: '{actual}' != '{value}'")
                return False

        logger.info(f"[FILL] {label}: '{value}' confirmed")
        return True

    async def _check_city_populated(self) -> bool:
        """Check if the city field has a non-empty value (zip autocomplete)."""
        candidates = SELECTORS.get("field_city", [])
        for selector in candidates:
            try:
                el = await self._page.query_selector(selector)
                if el:
                    val = await el.input_value()
                    if val and val.strip():
                        logger.info(f"[FILL] Zip autocomplete confirmed: city='{val.strip()}'")
                        return True
            except Exception:
                continue
        return False

    async def _check_text_on_page(self, text: str) -> bool:
        """Check if specific text exists anywhere in the page body."""
        try:
            content = await self._page.inner_text("body")
            return text.lower() in content.lower()
        except Exception:
            return False

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
            filename = f"tn_{label}_{timestamp}.png"
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
        phase: TNPhase,
        status: str,
        message: str,
        screenshot_path: Optional[str] = None,
        phase_start: Optional[float] = None,
    ) -> None:
        """Append a structured log entry."""
        duration_ms = int((time.time() - (phase_start or self._start_time)) * 1000)
        log_entry = TNPhaseLog(
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
        phase: TNPhase,
        reason: TNFailureReason,
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
        phase_override: Optional[TNPhase] = None,
        reason_override: Optional[TNFailureReason] = None,
        message_override: Optional[str] = None,
    ) -> TNExecutorOutput:
        """Build failure output from the last recorded failure."""
        pending = getattr(self, "_pending_failure", {})
        phase = phase_override or pending.get("phase", TNPhase.ENTRY)
        reason = reason_override or pending.get("reason", "unknown_error")
        message = message_override or pending.get("message", "Unknown failure")

        return TNExecutorOutput.failure(
            phase=phase,
            reason=reason,
            message=message,
            logs=self._logs,
            duration_ms=self._elapsed_ms(),
        )

    def _elapsed_ms(self) -> int:
        return int((time.time() - self._start_time) * 1000)


# ============================================================================
# Module-level entry point (matches food_delivery_executor pattern)
# ============================================================================

async def run_tn_patient_creation(
    runtime, patient: TNPatientInput
) -> TNExecutorOutput:
    """
    Execute the TN patient creation workflow.

    Loads credentials from environment BEFORE launching browser.
    Fails fast with a structured error if any credential is missing.

    Args:
        runtime: PlaywrightRuntime instance.
        patient: Validated patient input data.

    Returns:
        TNExecutorOutput with status, logs, and screenshots.
    """
    # Fail fast: validate credentials before spending time on browser launch
    try:
        credentials = get_tn_credentials()
    except Exception as e:
        logger.error(f"TN credential validation failed: {e}")
        return TNExecutorOutput.failure(
            phase=TNPhase.ENTRY,
            reason="login_failed",
            message=(
                "Missing TherapyNotes credentials. Required env vars: "
                "THERAPYNOTES_PRACTICE_CODE, THERAPYNOTES_USERNAME, THERAPYNOTES_PASSWORD"
            ),
            logs=[],
            duration_ms=0,
        )

    logger.info(f"TN credentials validated: {credentials.safe_display}")
    executor = TNExecutor(runtime, credentials)
    return await executor.execute(patient)
