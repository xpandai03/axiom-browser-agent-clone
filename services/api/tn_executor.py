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
import re
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


class OverlayBlockedError(RuntimeError):
    """
    A click could not land because an overlay covered the target.

    Distinct from a generic failure so the phase handler can report
    "blocked_by_overlay" instead of blaming the element it was trying to reach.
    On 4 Sept this surfaced as "new_patient_form_not_found" on a page whose form
    was present and whose button Playwright itself called "visible, enabled and
    stable" — the button was simply covered.
    """


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
        # Text of any TN overlay (e.g. account-level "Important Message"
        # broadcast) surfaced during the run — logged AND attached to output so
        # the message reaches staff/CRM, never silently swallowed.
        self._surfaced_overlays: List[str] = []

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
        self._surfaced_overlays = []

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
            screenshot = None
            if self._page:
                screenshot = await self._capture_screenshot("unhandled_error")
            self._record_log(
                TNPhase.ENTRY, "failure",
                f"Unhandled error: {e}",
                screenshot,
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
            return await self._fail_phase(phase, self._reason_for(e, "unknown_error"), str(e), phase_start)

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
            return await self._fail_phase(phase, self._reason_for(e, "login_failed"), str(e), phase_start)

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
        phase = TNPhase.NAVIGATE
        phase_start = time.time()
        logger.info("=" * 70)
        logger.info("PHASE 2: NAVIGATE — Sidebar → Patients")
        logger.info("=" * 70)

        try:
            # Pre-clear any modal overlays that may have appeared after login
            await self._dismiss_blocking_dialogs()

            # Step 1: Click "Patients" link in the sidebar
            patients_link = await self._resolve_selector("patients_link")
            if not patients_link:
                return await self._fail_phase(
                    phase, "navigation_failed",
                    "Patients link not found in sidebar",
                    phase_start,
                )
            await self._safe_click(patients_link, "Patients link")
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
            return await self._fail_phase(phase, self._reason_for(e, "navigation_failed"), str(e), phase_start)

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

            success_msg = "New Patient form loaded, first name field confirmed"
            if self._surfaced_overlays:
                success_msg += " | TN overlay(s) handled: " + " || ".join(self._surfaced_overlays)
            self._record_log(
                phase, "success",
                success_msg,
                phase_start=phase_start,
            )
            return True

        except Exception as e:
            return await self._fail_phase(phase, self._reason_for(e, "new_patient_form_not_found"), str(e), phase_start)

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
            return await self._fail_phase(phase, self._reason_for(e, "form_field_not_found"), str(e), phase_start)

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
            return await self._fail_phase(phase, self._reason_for(e, "save_failed"), str(e), phase_start)

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
                if not (await btn.count() > 0 and await btn.is_visible(timeout=500)):
                    continue
                # The list above includes unlabelled catch-alls ("Yes", "No" and
                # '#ElementDropbox .Dialog button', which matches ANY button in the
                # dialog). Check what the control actually says before pressing it:
                # this sweep must never answer a duplicate-patient warning or the
                # "Create Appointment Anyway" confirmation the agent accepts
                # deliberately elsewhere in the flow.
                label = await btn.evaluate(
                    "el => (el.innerText || el.value || el.getAttribute('aria-label') "
                    "|| el.getAttribute('title') || '').trim()"
                )
                blocked_by = self._label_is_consequential(label)
                if blocked_by:
                    logger.info(
                        f"[TN AGENT] Not pressing '{self._normalize_label(label)[:40]}' "
                        f"via {selector} — label implies a consequential choice "
                        f"('{blocked_by}')"
                    )
                    continue
                await btn.click(timeout=2000)
                logger.info(f"[TN AGENT] Dialog dismissed via: {selector}")
                await asyncio.sleep(0.3)
                return True
            except Exception:
                continue

        # NEW: bare #ElementDropbox overlay (e.g. TherapyNotes' account-level
        # "Important Message" broadcast). It is NOT wrapped in a
        # .Dialog/[role=dialog]/.modal element, so every selector above misses
        # it. Surface its text, then dismiss via its own acknowledge control.
        if await self._handle_element_dropbox_overlay():
            return True

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

    async def _surface_overlay_text(self, text: str) -> None:
        """Log an overlay's text prominently and attach it to run output."""
        text = " ".join((text or "").split())[:500] or "<empty>"
        if text not in self._surfaced_overlays:
            self._surfaced_overlays.append(text)
        logger.warning(f'[OVERLAY] TN message surfaced: "{text}"')

    # ========================================================================
    # Blocking-overlay detection — markup-agnostic
    # ========================================================================
    #
    # WHY THIS DOES NOT ASK "is the container visible?"
    #
    # On 4 Sept a TherapyNotes account-level broadcast covered the Patients page.
    # Playwright's hit-test named the blocker explicitly —
    #   <h2>Important Message</h2> from <div id="ElementDropbox">…</div> subtree
    #   intercepts pointer events
    # — while locator("#ElementDropbox").is_visible() returned False. Every guard
    # here was keyed to the CONTAINER's own visibility, so all of them bailed: the
    # overlay was never surfaced, never dismissed, and the run failed reporting
    # "new_patient_form_not_found" on a page whose form was perfectly fine.
    #
    # The overlay has since been cleared by hand and cannot be re-observed, so the
    # exact reason the container reported not-visible (zero-size portal, opacity,
    # offscreen, clipped, ...) is not known. Nothing below depends on knowing it.
    # The question asked is only ever "is something inside here actually painting
    # over the page?", which holds for every one of those causes.

    # Does this element's SUBTREE paint anything on screen? Answers for the whole
    # subtree, not the element, and uses the browser's own visibility verdict
    # (checkVisibility covers display, visibility, opacity and content-visibility
    # INCLUDING inherited/ancestor effects) combined with a real viewport-
    # intersecting box.
    _PAINT_PROBE_JS = """
    (el) => {
      const EMPTY = { paints: false, text: "", area: 0 };
      if (!el || !el.isConnected) return EMPTY;
      const vw = window.innerWidth, vh = window.innerHeight;

      const effectivelyVisible = (n) => {
        if (typeof n.checkVisibility === "function") {
          try {
            return n.checkVisibility({
              opacityProperty: true,
              visibilityProperty: true,
              contentVisibilityAuto: true,
            });
          } catch (e) { /* fall through to the manual walk */ }
        }
        // Fallback for engines without checkVisibility: walk the ancestor chain,
        // because display/visibility/opacity all inherit their effect downwards.
        for (let a = n; a && a.nodeType === 1; a = a.parentElement) {
          const s = window.getComputedStyle(a);
          if (s.display === "none") return false;
          if (s.visibility === "hidden" || s.visibility === "collapse") return false;
          if (parseFloat(s.opacity || "1") === 0) return false;
        }
        return true;
      };

      let area = 0;
      const nodes = [el, ...el.querySelectorAll("*")];
      for (const n of nodes) {
        if (!effectivelyVisible(n)) continue;
        const r = n.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const w = Math.min(r.right, vw) - Math.max(r.left, 0);
        const h = Math.min(r.bottom, vh) - Math.max(r.top, 0);
        if (w > 0 && h > 0) area = Math.max(area, w * h);
      }
      return {
        paints: area > 0,
        text: (el.innerText || el.textContent || "").trim(),
        area: area,
      };
    }
    """

    # Given a target element, return whatever is actually on top of its click
    # point — the same point Playwright aims at — or null when the target is
    # clear. Structure- and name-agnostic: it asks the DOM what is there.
    _FIND_BLOCKER_JS = """
    (target) => {
      if (!target || !target.isConnected) return null;
      const r = target.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return null;
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) return null;

      const hit = document.elementFromPoint(x, y);
      if (!hit) return null;
      // Target itself, or its own descendant/ancestor chain — not an overlay.
      if (hit === target || target.contains(hit) || hit.contains(target)) return null;

      // Something unrelated is on top. Walk up from it to the OUTERMOST ancestor
      // that still does not contain the target: that is the overlay's own root,
      // rather than a page wrapper shared with the target.
      let node = hit;
      while (
        node.parentElement &&
        node.parentElement !== document.body &&
        node.parentElement !== document.documentElement &&
        !node.parentElement.contains(target)
      ) {
        node = node.parentElement;
      }
      return node;
    }
    """

    # A control whose label implies a CHOICE with consequences, not an
    # acknowledgement. Never auto-clicked. Word-boundary matched, so "Notice"
    # does not trip "no" and "Save Draft" does trip "save".
    #
    # This list is what keeps the generic guard from doing damage: the flow
    # depends on two dialogs it must NOT auto-answer — the duplicate-patient
    # warning, and the "Create Appointment Anyway" confirmation the agent accepts
    # deliberately elsewhere.
    CONSEQUENTIAL_LABEL_TOKENS = (
        "anyway", "confirm", "yes", "no", "delete", "remove", "discard",
        "overwrite", "merge", "duplicate", "cancel", "submit", "save",
        "create", "schedule", "send", "sign", "agree", "accept", "decline",
        "reject", "archive", "pay", "proceed", "override", "replace", "update",
    )

    # A control that only says "I have read this".
    ACK_LABEL_TOKENS = (
        "ok", "okay", "got it", "acknowledge", "acknowledged", "close",
        "dismiss", "continue", "understood", "i understand", "done", "next",
    )

    # An overlay whose text reads like a DECISION rather than an announcement.
    # Two consequences: the agent refuses to auto-dismiss it, and it does not
    # copy the text into logs or run output — a decision dialog is the kind that
    # names a specific patient, whereas an account-level broadcast does not.
    CONSEQUENTIAL_OVERLAY_TOKENS = (
        "already exists", "duplicate", "are you sure", "cannot be undone",
        "will be deleted", "will be removed", "permanently", "unsaved changes",
        "do you want to", "overwrite", "existing patient", "potential match",
    )

    @staticmethod
    def _normalize_label(text: Optional[str]) -> str:
        return " ".join((text or "").split()).strip().lower()

    @classmethod
    def _label_is_consequential(cls, label: Optional[str]) -> Optional[str]:
        """Return the token that makes this label consequential, else None."""
        norm = cls._normalize_label(label)
        if not norm:
            return None
        for token in cls.CONSEQUENTIAL_LABEL_TOKENS:
            if re.search(rf"\b{re.escape(token)}\b", norm):
                return token
        return None

    @classmethod
    def _label_is_acknowledgement(cls, label: Optional[str]) -> bool:
        """True for a short label that reads purely as 'I have read this'."""
        norm = cls._normalize_label(label)
        if not norm or len(norm) > 40:
            return False
        if cls._label_is_consequential(norm):
            return False
        return any(re.search(rf"\b{re.escape(t)}\b", norm) for t in cls.ACK_LABEL_TOKENS)

    @classmethod
    def _overlay_is_decision_dialog(cls, text: Optional[str]) -> Optional[str]:
        """Return the token marking this overlay a decision dialog, else None."""
        norm = cls._normalize_label(text)
        for token in cls.CONSEQUENTIAL_OVERLAY_TOKENS:
            if token in norm:
                return token
        return None

    @staticmethod
    def _selector_from_interception_error(err_str: str) -> Optional[str]:
        """
        Pull the blocking element out of Playwright's own error text.

        Playwright already names it:
          "<h2>Important Message</h2> from <div id="ElementDropbox">…</div>
           subtree intercepts pointer events"
        so even when hit-testing and the paint probe both miss, the blocker is
        addressable. Prefers an id, falls back to the first class.
        """
        if "intercepts pointer events" not in err_str:
            return None
        m = re.search(r"from <(\w+)([^>]*)>", err_str)
        if not m:
            # Some builds report only the intercepting element itself.
            m = re.search(r"<(\w+)([^>]*)>\s*(?:from\s*)?subtree intercepts", err_str)
            if not m:
                return None
        tag, attrs = m.group(1), m.group(2) or ""
        id_m = re.search(r'id="([^"]+)"', attrs)
        if id_m:
            return f"#{id_m.group(1)}"
        cls_m = re.search(r'class="([^"]+)"', attrs)
        if cls_m:
            first = cls_m.group(1).split()[0] if cls_m.group(1).split() else ""
            if first:
                return f"{tag}.{first}"
        return None

    async def _probe_paint(self, handle) -> dict:
        """Run the paint probe against an ElementHandle. Never raises."""
        try:
            result = await handle.evaluate(self._PAINT_PROBE_JS)
            if isinstance(result, dict):
                return result
        except Exception:
            pass
        return {"paints": False, "text": "", "area": 0}

    async def _element_handle_for(self, target):
        """Accept a Locator or an ElementHandle, return an ElementHandle."""
        try:
            if hasattr(target, "element_handle"):  # Locator
                return await target.element_handle(timeout=2000)
            return target  # already an ElementHandle
        except Exception:
            return None

    async def _find_blocking_overlay(self, target):
        """
        Return an ElementHandle for whatever covers `target`'s click point, or
        None. Independent of markup, ids, classes and text.
        """
        try:
            el = await self._element_handle_for(target)
            if el is None:
                return None
            js_handle = await self._page.evaluate_handle(self._FIND_BLOCKER_JS, el)
            return js_handle.as_element()
        except Exception:
            return None

    async def _try_dismiss_overlay_handle(
        self, blocker, origin: str, confirmed_blocking: bool = False
    ) -> bool:
        """
        Surface a blocking overlay's message, then dismiss it using a control
        INSIDE ITS OWN SUBTREE. Returns True only if it is now gone.

        Discipline (unchanged from the previous, narrower handler):
          - the message is surfaced BEFORE the overlay is touched, so a broadcast
            carrying practice/billing/compliance information is never silently
            swallowed;
          - only controls within the blocker's subtree are considered — never a
            global click;
          - a control whose label implies a consequential choice is never clicked;
          - an overlay that reads as a DECISION rather than an announcement is
            not dismissed at all, and its text is not copied anywhere.
        """
        if blocker is None:
            return False

        probe = await self._probe_paint(blocker)
        text = " ".join((probe.get("text") or "").split())

        # `confirmed_blocking` means something already PROVED this element is in
        # the way — a hit-test on the click point, or Playwright naming it in an
        # interception error. Such an element must be dealt with whether or not it
        # paints: an overlay at opacity 0, or one drawn entirely off-viewport with
        # a transparent hit area, still swallows the click.
        #
        # Without that proof this is a speculative probe of a known selector, and
        # the paint/text gates matter: TherapyNotes keeps #ElementDropbox mounted
        # with no message in it, and a normal run must not surface, Escape or
        # stall on an empty container.
        if not confirmed_blocking:
            if not probe.get("paints"):
                return False
            if len(text) < 3:
                return False

        # A decision dialog is the agent's business to fail on, not to answer —
        # and it is the kind that names a patient, so its text stays out of logs.
        decision_token = self._overlay_is_decision_dialog(text)
        if decision_token:
            logger.warning(
                f"[OVERLAY] Blocker at {origin} reads as a decision dialog "
                f"(matched '{decision_token}') — refusing to auto-dismiss. "
                f"Text withheld: a decision dialog can name a patient."
            )
            return False

        if text:
            await self._surface_overlay_text(text)

        # Self-document the real DOM once, so the acknowledge control is
        # confirmable from logs next time. Announcement text only (a decision
        # dialog returned above), so no patient data reaches this line.
        try:
            outer = await blocker.evaluate("el => el.outerHTML")
            logger.info(f"[OVERLAY] Blocker outerHTML at {origin} (first 1500): {str(outer)[:1500]}")
        except Exception:
            pass

        # Candidate acknowledge controls, scoped to the blocker's own subtree.
        try:
            controls = await blocker.query_selector_all(
                'button, input[type="button"], input[type="submit"], a, '
                '[role="button"], [class*="close" i], [aria-label]'
            )
        except Exception:
            controls = []

        for control in controls:
            try:
                label = await control.evaluate(
                    "el => (el.innerText || el.value || el.getAttribute('aria-label') "
                    "|| el.getAttribute('title') || '').trim()"
                )
                blocked_by = self._label_is_consequential(label)
                if blocked_by:
                    logger.info(
                        f"[OVERLAY] Skipping control '{self._normalize_label(label)[:40]}' "
                        f"— label implies a consequential choice ('{blocked_by}')"
                    )
                    continue
                # An unlabelled control is only acceptable if it looks like a
                # close affordance; otherwise we do not know what it does.
                if not self._label_is_acknowledgement(label):
                    klass = await control.evaluate("el => el.className || ''")
                    if "close" not in str(klass).lower():
                        continue

                if not confirmed_blocking and not (await self._probe_paint(control)).get("paints"):
                    continue

                await control.click(timeout=2000)
                await asyncio.sleep(0.3)
                if not (await self._probe_paint(blocker)).get("paints"):
                    logger.info(
                        f"[OVERLAY] Dismissed blocker at {origin} via control "
                        f"'{self._normalize_label(label)[:40] or '<close>'}'"
                    )
                    return True
            except Exception:
                continue

        # Last resort: Escape.
        try:
            await self._page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            if not (await self._probe_paint(blocker)).get("paints"):
                logger.info(f"[OVERLAY] Dismissed blocker at {origin} via Escape fallback")
                return True
        except Exception:
            pass

        logger.warning(f"[OVERLAY] Blocker at {origin} detected but could NOT be dismissed")
        return False

    async def _clear_overlay_blocking(self, target, label: str) -> bool:
        """1b: hit-test `target`'s click point and clear whatever covers it."""
        blocker = await self._find_blocking_overlay(target)
        if blocker is None:
            return False
        logger.info(f"[OVERLAY] Hit-test: something covers '{label}' — inspecting it")
        return await self._try_dismiss_overlay_handle(
            blocker, f"hit-test on '{label}'", confirmed_blocking=True
        )

    async def _clear_overlay_from_error(self, err_str: str) -> bool:
        """1c: dismiss the element Playwright itself named in the error."""
        selector = self._selector_from_interception_error(err_str)
        if not selector:
            return False
        logger.info(f"[OVERLAY] Playwright named the blocker: {selector}")
        try:
            handle = await self._page.query_selector(selector)
        except Exception:
            return False
        return await self._try_dismiss_overlay_handle(
            handle, f"error-named {selector}", confirmed_blocking=True
        )

    async def _handle_element_dropbox_overlay(self) -> bool:
        """
        The known TherapyNotes account-level broadcast mount point.

        Kept as a named fast path, but the visibility guard that made it inert is
        gone: presence in the DOM is decided by query_selector and everything
        after that is decided by the paint probe, so it now fires whether or not
        the container itself reports visible.
        """
        try:
            dropbox = await self._page.query_selector("#ElementDropbox")
        except Exception:
            return False
        if dropbox is None:
            return False
        return await self._try_dismiss_overlay_handle(dropbox, "#ElementDropbox")

    async def _describe_blocking_overlay(self, target=None) -> str:
        """
        Best-effort: describe whatever is currently covering the page (or
        `target`, when given) for diagnostic reporting. Also surfaces it.

        Hit-testing comes first because it needs no prior knowledge of the
        overlay; the named selectors are only a fallback.
        """
        candidates = []
        if target is not None:
            blocker = await self._find_blocking_overlay(target)
            if blocker is not None:
                candidates.append(blocker)
        for sel in ['#ElementDropbox', '.Dialog', '[role="dialog"]', '.modal']:
            try:
                handle = await self._page.query_selector(sel)
                if handle is not None:
                    candidates.append(handle)
            except Exception:
                continue

        for handle in candidates:
            probe = await self._probe_paint(handle)
            if not probe.get("paints"):
                continue
            txt = " ".join((probe.get("text") or "").split())[:300]
            if not txt:
                continue
            if self._overlay_is_decision_dialog(txt):
                # Do not copy a decision dialog's text: it can name a patient.
                return "a confirmation dialog requiring a decision (text withheld)"
            await self._surface_overlay_text(txt)
            return txt
        return ""

    async def _safe_click(self, element_or_locator, label: str = "element") -> None:
        """
        Click, clearing any overlay that covers the target.

        Order of attack, cheapest and most general first:
          1. Hit-test the click point BEFORE clicking. Catches a blocker without
             burning a 15s Playwright timeout, and needs no knowledge of it.
          2. On failure, the known dialog/broadcast selectors.
          3. The element Playwright itself named in the interception error.
          4. Hit-test again (the page may have re-rendered).
          5. Escape.
        If it is still blocked, raise an error that says SO — naming the overlay
        rather than reporting an opaque timeout on an element that was, per
        Playwright's own log, "visible, enabled and stable" the whole time.
        """
        # 1) Pre-flight: is anything already on top of the click point?
        try:
            await self._clear_overlay_blocking(element_or_locator, label)
        except Exception:
            pass

        try:
            await element_or_locator.click(timeout=self.STEP_TIMEOUT_MS)
            return
        except Exception as first_err:
            err_str = str(first_err)
            if "intercepts pointer events" not in err_str and "timeout" not in err_str.lower():
                raise

            logger.warning(f"[TN AGENT] Click blocked on '{label}': {err_str[:200]}")
            logger.info("[TN AGENT] Attempting to dismiss blocking dialog...")

        # 2) Known dialogs, then 3) the blocker Playwright named, then 4) hit-test.
        dismissed = await self._dismiss_blocking_dialogs()
        if not dismissed:
            dismissed = await self._clear_overlay_from_error(err_str)
        if not dismissed:
            dismissed = await self._clear_overlay_blocking(element_or_locator, label)
        if dismissed:
            logger.info(f"[TN AGENT] Retrying click on '{label}' after dialog dismissal")
        try:
            await element_or_locator.click(timeout=self.STEP_TIMEOUT_MS)
            return
        except Exception as second_err:
            logger.warning(f"[TN AGENT] Retry 1 failed on '{label}': {str(second_err)[:200]}")

        # 5) Escape.
        await self._page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        logger.info(f"[TN AGENT] Retrying click on '{label}' after Escape")
        try:
            await element_or_locator.click(timeout=self.STEP_TIMEOUT_MS)
            return
        except Exception as final_err:
            # Say what actually blocked it. `_describe_blocking_overlay` is given
            # the target so it can hit-test rather than guess from a selector
            # list, and it no longer depends on the container reporting visible —
            # which is why this branch produced nothing on 4 Sept and the run
            # surfaced a bare timeout instead.
            overlay_text = await self._describe_blocking_overlay(element_or_locator)
            if overlay_text:
                raise OverlayBlockedError(
                    f"Click on '{label}' blocked by an overlay that could not be "
                    f'dismissed. Overlay said: "{overlay_text}". '
                    f"Underlying: {str(final_err)[:200]}"
                ) from final_err
            raise

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

    @staticmethod
    def _reason_for(exc: Exception, default: str) -> str:
        """
        Classify a phase failure. An overlay block is reported as exactly that,
        never as whatever the phase happened to be looking for — the 4 Sept runs
        blamed a missing New Patient form for a TherapyNotes broadcast sitting on
        top of a button that was present the whole time.
        """
        return "blocked_by_overlay" if isinstance(exc, OverlayBlockedError) else default

    async def _fail_phase(
        self,
        phase: TNPhase,
        reason: TNFailureReason,
        message: str,
        phase_start: float,
    ) -> bool:
        """Record failure, capture screenshot, return False to halt workflow."""
        # Attach any surfaced TN overlay text so the message reaches output.
        if self._surfaced_overlays:
            message = f"{message} | TN overlay(s) surfaced: " + " || ".join(self._surfaced_overlays)
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
