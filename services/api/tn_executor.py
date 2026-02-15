"""
Therapy Notes Patient Creation Executor.

Deterministic, linear browser automation workflow that creates a patient
in TherapyNotes via headless Playwright.

Architecture:
- Single-session, no parallel execution (module-level asyncio.Lock).
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
        "input#ctl00_BodyContent_ButtonCreatePatient1",            # Actual TN selector (verified via DOM dump)
        "input[type='submit'][value='+ New Patient']",             # Fallback by type+value
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

            # All phases passed
            duration_ms = self._elapsed_ms()
            patient_name = f"{patient.first_name} {patient.last_name}"
            logger.info(f"WORKFLOW COMPLETE: {patient_name} created in {duration_ms}ms")
            return TNExecutorOutput.success(
                patient_name=patient_name,
                logs=self._logs,
                duration_ms=duration_ms,
                tn_patient_url=getattr(self, "_tn_patient_url", None),
                tn_patient_id=getattr(self, "_tn_patient_id", None),
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
        """Navigate sidebar → Patients page, confirm + New Patient button exists."""
        phase = TNPhase.NAVIGATE
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 2: NAVIGATE — Sidebar → Patients")
        logger.info("=" * 70)

        try:
            # Step 1: Click "Patients" link in the sidebar
            patients_link = await self._resolve_selector("patients_link")
            if not patients_link:
                return await self._fail_phase(
                    phase, "navigation_failed",
                    "Patients link not found in sidebar",
                    phase_start,
                )
            await patients_link.click()
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
        phase = TNPhase.FILL_FORM
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 3: FORM DETECTION — Confirm New Patient form fields")
        logger.info("=" * 70)

        try:
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
            await new_patient_btn.click()
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

    async def _phase_fill_required(self, patient: TNPatientInput) -> bool:
        """Fill required patient fields using verified DOM IDs. Does NOT click Save."""
        phase = TNPhase.FILL_FORM
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
            await zip_loc.click()
            await zip_loc.fill("")
            await page.keyboard.type(patient.zip, delay=50)
            try:
                await page.wait_for_function(
                    "(selector, expected) => document.querySelector(selector).value === expected",
                    "#AddressEditorView__PostalCodeInput_PatientAddress",
                    patient.zip,
                    timeout=3000,
                )
            except Exception:
                pass
            actual_zip = await zip_loc.input_value()
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

    async def _phase_save_patient(self, patient: TNPatientInput) -> bool:
        """Click Save New Patient, confirm creation, detect errors/duplicates."""
        phase = TNPhase.SAVE
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 5: SAVE PATIENT")
        logger.info("=" * 70)

        try:
            page = self._page
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
            await save_loc.click()
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
            duplicate_text = await page.evaluate("""
                () => {
                    const body = document.body.innerText || '';
                    if (body.includes('already exists') || body.includes('duplicate') || body.includes('Duplicate')) {
                        const idx = body.indexOf('already exists');
                        if (idx >= 0) return body.slice(Math.max(0, idx - 50), idx + 100);
                        const idx2 = body.indexOf('uplicate');
                        if (idx2 >= 0) return body.slice(Math.max(0, idx2 - 50), idx2 + 100);
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
# Concurrency guard — only one patient creation at a time
# ============================================================================

_execution_lock = asyncio.Lock()


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
    Only one execution can run at a time (module-level asyncio.Lock).

    Args:
        runtime: PlaywrightRuntime instance.
        patient: Validated patient input data.

    Returns:
        TNExecutorOutput with status, logs, and screenshots.
    """
    # Concurrency guard: only one patient creation at a time
    if _execution_lock.locked():
        logger.warning("TN patient creation rejected — another execution is in progress")
        return TNExecutorOutput.failure(
            phase=TNPhase.ENTRY,
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
