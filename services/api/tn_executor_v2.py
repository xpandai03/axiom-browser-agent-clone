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
# Progress callbacks → CRM tn-progress endpoint (CRM v128)
# ============================================================================

async def _emit_progress(
    callback_url: Optional[str],
    api_key: str,
    contact_id: Optional[int],
    run_id: Optional[str],
    phase: str,
    status: str,
    message: str,
    metadata: Optional[dict] = None,
) -> None:
    """
    Best-effort POST to the CRM's /api/internal/tn-progress/:contactId endpoint.

    Fire-and-forget: any failure (network, non-200, bad config) is logged but
    NEVER raised — progress reporting must not be able to fail the TN workflow.
    Silently skips when callbacks aren't configured (no callback_url/run_id).
    """
    if not callback_url or not run_id or contact_id is None:
        return  # not configured → silent skip

    payload = {
        "contactId": contact_id,
        "runId": run_id,
        "phase": phase,
        "status": status,
        "message": message,
    }
    if metadata:
        payload["metadata"] = metadata

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                callback_url,
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json=payload,
            )
            if response.status_code != 200:
                logger.warning(
                    f"[CALLBACK] Progress event rejected: phase={phase} status={status} "
                    f"http={response.status_code} body={response.text[:200]}"
                )
            else:
                logger.info(f"[CALLBACK] Sent: phase={phase} status={status}")
    except Exception as e:
        logger.warning(f"[CALLBACK] Failed to send progress event phase={phase}: {e}")
    # Never raises — callbacks are best-effort.


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


class OverlayBlockedError(RuntimeError):
    """
    A click could not land because an overlay covered the target.

    Distinct from a generic failure so the phase handler can report
    "blocked_by_overlay" instead of blaming the element it was trying to reach.
    On 4 Sept this surfaced as "new_patient_form_not_found" on a page whose form
    was present and whose button Playwright itself called "visible, enabled and
    stable" — the button was simply covered.
    """


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
        # Text of any TN overlay (e.g. account-level "Important Message"
        # broadcast) surfaced during the run — logged AND attached to output so
        # the message reaches staff/CRM, never silently swallowed.
        self._surfaced_overlays: List[str] = []
        # Subset of _surfaced_overlays already relayed to the CRM, so each
        # message rides exactly one progress callback instead of repeating on
        # every subsequent phase.
        self._overlays_reported: set = set()

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
        self._surfaced_overlays = []
        self._overlays_reported = set()
        self._patient = patient  # carry callback config (run_id/callback_url/contact_id)
        logger.info(
            f"[CALLBACK CONFIG] callback_url={patient.callback_url}, "
            f"run_id={patient.run_id}, contact_id={self._resolve_contact_id(patient)}"
        )

        try:
            self._page = await self._runtime.ensure_browser()

            full_name = f"{patient.first_name} {patient.last_name}"

            # Each _step emits started → ok/failed around its phase. Phase 3
            # (form detection) is internal and intentionally has no callback.
            # On any failure, _finish_failure emits the terminal workflow_complete.
            if not await self._step(
                "entry", "Starting TherapyNotes entry",
                self._phase_entry(), "Practice code accepted, login form loaded",
            ):
                return await self._finish_failure()

            if not await self._step(
                "login", "Authenticating to TherapyNotes",
                self._phase_login(), "Logged in, dashboard confirmed",
            ):
                return await self._finish_failure()

            if not await self._step(
                "navigate", "Navigating to Patients",
                self._phase_navigate(), "Patients page loaded",
            ):
                return await self._finish_failure()

            # ------ Phase 3: Detect New Patient form (internal, no callback) ------
            if not await self._phase_detect_form():
                return await self._finish_failure()

            if not await self._step(
                "fill_form", "Filling patient form",
                self._phase_fill_required(patient),
                f"Required fields filled for {full_name}",
            ):
                return await self._finish_failure()

            if not await self._step(
                "save", "Saving patient",
                self._phase_save_patient(patient),
                f"Patient '{full_name}' saved in TherapyNotes",
                lambda: {
                    "tnPatientUrl": getattr(self, "_tn_patient_url", None),
                    "tnPatientId": getattr(self, "_tn_patient_id", None),
                },
            ):
                return await self._finish_failure()

            # ====================================================================
            # Step 3 — extended phases 6-8 (run only after a successful save).
            # Decision I2: strictly sequential, halt on first failure.
            # Decision I3: on failure here the patient already exists, so
            # _build_failure_output carries tn_patient_url/tn_patient_id.
            # ====================================================================

            if not await self._step(
                "upload_intake_pdf", "Uploading intake referral PDF",
                self._phase_upload_intake_pdf(patient), "Intake Referral uploaded",
                lambda: {"documentName": "Intake Referral"},
            ):
                return await self._finish_failure()

            if not await self._step(
                "upload_snapshot_pdf", "Uploading appointment confirmation PDF",
                self._phase_upload_snapshot_pdf(patient),
                "Initial Appointment Confirmation Email uploaded",
                lambda: {"documentName": "Initial Appointment Confirmation Email"},
            ):
                return await self._finish_failure()

            if not await self._step(
                "schedule_appointment", "Scheduling appointment",
                self._phase_schedule_appointment(patient),
                "Appointment scheduled in TherapyNotes",
                lambda: {
                    "clinician": patient.clinician_name,
                    "appointmentDatetime": f"{patient.appointment_date} {patient.appointment_time}",
                },
            ):
                return await self._finish_failure()

            # All phases passed
            duration_ms = self._elapsed_ms()
            logger.info(f"WORKFLOW COMPLETE: {full_name} created in {duration_ms}ms")
            await self._emit(
                "workflow_complete", "ok",
                "Workflow complete — patient and appointment created in TherapyNotes",
                metadata={
                    "tnPatientUrl": getattr(self, "_tn_patient_url", None),
                    "durationMs": duration_ms,
                },
            )
            return TNExecutorOutputV2.success(
                patient_name=full_name,
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
            await self._emit(
                "workflow_complete", "failed",
                f"Workflow failed: {e}",
                metadata={"failedPhase": "entry", "failureReason": "unknown_error"},
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
            return await self._fail_phase(phase, self._reason_for(e, "navigation_failed"), str(e), phase_start)

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

    # ========================================================================
    # Save verification — did TherapyNotes actually create the record?
    # ========================================================================

    # How long to wait for TN to reach a verdict on a save before judging it.
    SAVE_VERDICT_TIMEOUT_MS = 15000

    # A TherapyNotes patient record lives at /app/patients/edit/<opaque-id>/ .
    # The BLANK New Patient form is /app/patients/edit/ — the same path with
    # nothing after it.
    #
    # This is why a substring test cannot tell them apart: ".../patients/edit" is
    # contained in the form URL AND in every record URL, so `form_url in page.url`
    # is true on both. The distinction is structural — is there a non-empty
    # segment after the action? — so match on that instead.
    _PATIENT_RECORD_URL_RE = re.compile(r"/patients/(?:edit|view|detail)/([^/?#]+)")

    @classmethod
    def _patient_record_id_from_url(cls, url: Optional[str]) -> Optional[str]:
        """
        Return the record segment when `url` names a SPECIFIC patient record, or
        None for a blank form, a list page, or anything else.

        Deliberately separate from the `_tn_patient_id` extraction in the save
        phase, which matches numeric ids only and so never fires against TN's
        opaque ones. That is a known, separately queued bug; this answers the
        different question "is this a record URL at all?" and must not be
        conflated with it.
        """
        if not url:
            return None
        match = cls._PATIENT_RECORD_URL_RE.search(url)
        if not match:
            return None
        return match.group(1).strip() or None

    # Collect visible, error-flagged text from the page.
    #
    # The probe this replaces named three specific classes
    # (.validation-summary-errors, .alert-danger, [role="alert"]) and found
    # NOTHING on a page that had just refused a save — so those are not TN's
    # error markup. Guessing three more would fail the same way the next time TN
    # changes its DOM, which it has done repeatedly.
    #
    # So: take whatever the PAGE ITSELF marks as an error — by ARIA role, by
    # aria-invalid, or by a class/id containing an error-ish word — keep only
    # what is actually rendered, and report the text found.
    #
    # Scoped to error-flagged elements on purpose. It never scrapes general page
    # text, which on this page is a patient record.
    _SAVE_ERROR_PROBE_JS = """
    () => {
      const ERRORISH = /(^|[-_ ])(error|invalid|warning|danger|required|validation)/i;

      const isShown = (n) => {
        if (typeof n.checkVisibility === "function") {
          try {
            if (!n.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return false;
          } catch (e) { /* fall through to the box check */ }
        }
        const r = n.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };

      const candidates = new Set();
      document
        .querySelectorAll('[role="alert"], [role="alertdialog"], [aria-invalid="true"], [aria-errormessage], [aria-live]')
        .forEach((n) => candidates.add(n));
      document.querySelectorAll('[class], [id]').forEach((n) => {
        const key = (n.getAttribute('class') || '') + ' ' + (n.getAttribute('id') || '');
        if (ERRORISH.test(key)) candidates.add(n);
      });

      const found = [];
      for (const n of candidates) {
        if (!isShown(n)) continue;
        const t = (n.innerText || n.textContent || '').replace(/\\s+/g, ' ').trim();
        // Skip empty nodes and whole-page containers that happen to be class-matched.
        if (!t || t.length > 300) continue;
        found.push(t);
      }
      // Drop entries wholly contained in another (a wrapper and its child).
      const kept = found.filter((t, i) => !found.some((o, j) => j !== i && o !== t && o.includes(t)));
      return Array.from(new Set(kept)).slice(0, 6);
    }
    """

    # Duplicate-patient detection. This JS is UNCHANGED — lifted verbatim from
    # the save phase into a constant so the verdict poll and the duplicate check
    # run exactly the same test, and the poll can end early on a duplicate
    # instead of waiting out its timeout.
    _DUPLICATE_PROBE_JS = """
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
    """

    async def _collect_visible_errors(self) -> List[str]:
        """Visible error text the page is showing, best effort. Never raises."""
        try:
            result = await self._page.evaluate(self._SAVE_ERROR_PROBE_JS)
            if isinstance(result, list):
                return [str(t) for t in result if t]
        except Exception:
            pass
        return []

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

            # Step 3: Wait for a VERDICT, not a fixed interval.
            #
            # A save is slow sometimes, so sleeping 2s and then judging can call a
            # still-in-flight save refused. Poll instead, and stop as soon as
            # TherapyNotes has told us something either way: a record URL, an
            # error it rendered, or a duplicate warning.
            async def _save_verdict_reached() -> bool:
                if self._patient_record_id_from_url(self._page.url):
                    return True
                if await self._page.evaluate(self._SAVE_ERROR_PROBE_JS):
                    return True
                return bool(await self._page.evaluate(self._DUPLICATE_PROBE_JS))

            await self._poll_condition(
                _save_verdict_reached,
                "save verdict (record URL, error, or duplicate warning)",
                timeout_ms=self.SAVE_VERDICT_TIMEOUT_MS,
            )

            url_after = page.url
            logger.info(f"[SAVE] URL after save: {url_after}")

            # Step 4: Duplicate patient warning — unchanged detection, unchanged
            # outcome. Checked before anything else so this existing path keeps
            # its own failure reason rather than being folded into "save refused".
            duplicate_text = await page.evaluate(self._DUPLICATE_PROBE_JS)

            if duplicate_text:
                logger.warning(f"[SAVE] Duplicate detected: {duplicate_text}")
                await self._capture_screenshot("save_duplicate_detected")
                return await self._fail_phase(
                    phase, "patient_duplicate_detected",
                    f"Duplicate patient warning: {duplicate_text[:200]}",
                    phase_start,
                )

            # Step 5: REQUIRE POSITIVE EVIDENCE that a record now exists.
            #
            # This phase used to assume success unless it hit an exception, one of
            # three specific error classes, or a duplicate warning — so a save
            # TherapyNotes simply refused was reported as "saved successfully".
            # The run then tried to upload a document to a patient that did not
            # exist and failed at the Documents tab, which only appears on a saved
            # record. The two signals below were already computed here; they were
            # logged and never read.
            #
            # The URL is authoritative. TherapyNotes cannot move from the blank
            # form to /patients/edit/<id>/ without having created the record, so a
            # record-specific URL is proof. The name check corroborates but cannot
            # veto: innerText renders the saved record's name, and TN may render it
            # in an order or format ("Last, First", with a middle name) that an
            # exact match misses, so it produces false negatives.
            record_id = self._patient_record_id_from_url(url_after)
            expected_name = f"{patient.first_name} {patient.last_name}"
            name_on_page = await page.evaluate(
                "(name) => document.body.innerText.includes(name)",
                expected_name,
            )
            # Boolean only — the patient's name is not written to the log.
            logger.info(f"[SAVE] Record URL: {bool(record_id)} | name on page: {name_on_page}")

            if not record_id:
                # Disagreement case: name present, no record URL. Still a failure —
                # without a record URL there is nothing for the upload and
                # scheduling phases to act on, and the name can appear on a form
                # that was never saved.
                visible_errors = await self._collect_visible_errors()
                detail = (
                    f' TherapyNotes displayed: "{" | ".join(visible_errors)[:300]}".'
                    if visible_errors
                    else " No error text could be read from the page."
                )
                if name_on_page:
                    detail += (
                        " (The patient name IS on the page but the URL never advanced"
                        " to a record — treating the save as refused.)"
                    )
                await self._capture_screenshot("save_refused")
                return await self._fail_phase(
                    phase, "save_failed",
                    "TherapyNotes did not create the patient record — the save was "
                    f"refused and the page stayed on the New Patient form.{detail}",
                    phase_start,
                )

            # A save that landed while TN also shows an unrelated notice is a
            # SUCCESS: the record exists. That is why error text is only consulted
            # on the no-record branch above.
            if not name_on_page:
                logger.warning(
                    "[SAVE] Record created, but the expected name was not found on "
                    "the page — TN may render it in a different format. Proceeding "
                    "on the record URL, which is authoritative."
                )

            # Step 6: Capture patient URL and extract ID
            self._tn_patient_url = page.url
            self._tn_patient_id = None

            # Try to extract patient ID from URL (e.g. /app/patients/view/12345)
            # NOTE: this numeric-only match never fires against TN's opaque ids.
            # That is a known, separately queued bug and is deliberately left as
            # it is here — `_patient_record_id_from_url` above answers a different
            # question ("is this a record URL at all?") and is what the save
            # verification and the upload navigation guard rely on.
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
                return await self._fail_phase(phase, self._reason_for(e, "pdf_unsupported_format"), str(e), phase_start)
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

        # Ensure we're on the right patient RECORD before uploading.
        #
        # This used to ask `patient_url.rstrip("/") not in page.url`. When the save
        # phase handed over the blank-form URL (.../patients/edit/), the stripped
        # form ".../patients/edit" WAS a substring of the current URL, so the guard
        # decided we were already in the right place and the run went on to hunt for
        # a Documents tab on an unsaved form. Compare record identity instead of
        # substrings: a URL is a patient record only if it names a specific record.
        target_id = self._patient_record_id_from_url(patient_url)
        if not target_id:
            # With save verification in place the save phase fails first and this is
            # unreachable in the normal flow. It stays as a backstop so no code path
            # can ever upload against a blank form: the honest reason is that no
            # record was created, not that TN's upload UI is missing.
            return await self._fail_phase(
                phase, "save_failed",
                "Cannot upload a document: there is no saved patient record to "
                "upload to (the patient URL is a blank New Patient form, not a "
                "record). The save must have been refused.",
                phase_start,
            )
        if self._patient_record_id_from_url(page.url) != target_id:
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

            # Modality — deterministic from explicit appointment_modality when
            # present; legacy alert-text substring scan as rollout fallback.
            #
            # Resolve intent:
            #   - appointment_modality present + recognized -> authoritative
            #     ("telehealth" -> True, "in person" -> False)
            #   - absent/None/unrecognized -> legacy substring scan of
            #     appointment_alert_text (preserves pre-fix behavior exactly)
            # Then drive the checkbox deterministically: Telehealth -> ensure
            # checked; In Person -> ensure UNCHECKED (new behavior — the old
            # code never unchecked).
            # `authoritative` is True only when the intent came from the explicit
            # appointment_modality field. The new explicit-uncheck behavior is
            # gated on it, so the legacy fallback path (field absent/unrecognized)
            # stays strictly one-directional — byte-identical to pre-fix behavior.
            raw_modality = patient.appointment_modality
            norm_modality = raw_modality.strip().lower() if isinstance(raw_modality, str) else None
            if norm_modality == "telehealth":
                want_telehealth = True
                authoritative = True
                modality_source = "appointment_modality field"
            elif norm_modality == "in person":
                want_telehealth = False
                authoritative = True
                modality_source = "appointment_modality field"
            else:
                if raw_modality is not None and norm_modality not in ("telehealth", "in person"):
                    # Present but unrecognized: surface the contract drift loudly,
                    # then fall back — never silently default.
                    logger.warning(
                        f"[SCHEDULE] appointment_modality={raw_modality!r} not recognized "
                        "(expected 'Telehealth' or 'In Person'); falling back to "
                        "appointment_alert_text substring scan"
                    )
                want_telehealth = "telehealth" in patient.appointment_alert_text.lower()
                authoritative = False
                modality_source = "legacy substring scan"

            logger.info(
                f"[SCHEDULE] Modality resolved: telehealth={want_telehealth} "
                f"(source={modality_source}, authoritative={authoritative})"
            )
            try:
                cb = page.locator(SELECTORS_V2["appt_telehealth_checkbox"][0]).first
                if await cb.count() > 0:
                    is_checked = await cb.is_checked()
                    if want_telehealth and not is_checked:
                        # Check for Telehealth — same action the legacy code took.
                        await cb.check()
                        logger.info("[SCHEDULE] Telehealth checkbox checked")
                    elif not want_telehealth and is_checked and authoritative:
                        # Explicit uncheck — the new half of the fix. Only when the
                        # explicit field said In Person; legacy fallback never unchecks.
                        await cb.uncheck()
                        logger.info("[SCHEDULE] Telehealth checkbox unchecked (In Person)")
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

            # TN may raise a soft warning after Save (conflict / outside the
            # clinician's availability / patient-not-assigned) and replace the
            # primary Save with a "Create Appointment Anyway" / "Don't Create
            # Appointment" pair, disabling "Save New Appointment". CRM intent =
            # force-create, so click through. (Genuine errors already failed above.)
            anyway = page.locator("input[value='Create Appointment Anyway']").first
            if await anyway.count() > 0 and await anyway.is_visible():
                # Capture the warning text for diagnostics (why TN warned).
                try:
                    warning_text = await page.evaluate(
                        """() => {
                            const dialog = document.querySelector('[role="dialog"]');
                            if (!dialog) return null;
                            const anywayBtn = Array.from(dialog.querySelectorAll('input')).find(
                                i => i.value === 'Create Appointment Anyway'
                            );
                            if (!anywayBtn) return null;
                            let container = anywayBtn.parentElement;
                            for (let i = 0; i < 5 && container; i++) {
                                const text = (container.textContent || '').trim();
                                if (text.length > 50 && text.includes('Create Appointment Anyway')) {
                                    return text.substring(0, 500);
                                }
                                container = container.parentElement;
                            }
                            return null;
                        }"""
                    )
                    if warning_text:
                        logger.info(f"[SCHEDULE] TN warning text: {warning_text}")
                except Exception as e:
                    logger.warning(f"[SCHEDULE] Could not capture warning text: {e}")

                logger.info("[SCHEDULE] Confirmation prompt detected — clicking 'Create Appointment Anyway'")
                await self._safe_click(anyway, "Create Appointment Anyway")
                await asyncio.sleep(2)
            else:
                logger.info("[SCHEDULE] No confirmation prompt (clean save path)")

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
            return await self._fail_phase(phase, self._reason_for(e, "appointment_creation_failed"), str(e), phase_start)

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
        phase: TNPhaseV2,
        reason: TNFailureReasonV2,
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

    # ========================================================================
    # Progress callbacks (CRM v128) — best-effort, never fail the workflow
    # ========================================================================

    @staticmethod
    def _resolve_contact_id(patient) -> Optional[int]:
        """Prefer the explicit contact_id; else parse the trailing id from the
        callback_url path (/api/internal/tn-progress/:contactId)."""
        cid = getattr(patient, "contact_id", None)
        if cid is not None:
            return cid
        url = getattr(patient, "callback_url", None)
        if url:
            m = re.search(r"/(\d+)/?$", url)
            if m:
                return int(m.group(1))
        return None

    async def _emit(
        self, phase: str, status: str, message: str, metadata: Optional[dict] = None
    ) -> None:
        """Emit one progress event for the current run. No-op if callbacks
        aren't configured. Never raises (delegates to _emit_progress)."""
        patient = getattr(self, "_patient", None)
        if patient is None:
            return
        await _emit_progress(
            callback_url=getattr(patient, "callback_url", None),
            api_key=os.environ.get("TN_API_KEY", ""),
            contact_id=self._resolve_contact_id(patient),
            run_id=getattr(patient, "run_id", None),
            phase=phase,
            status=status,
            message=message,
            metadata=metadata,
        )

    async def _step(
        self,
        phase_value: str,
        started_msg: str,
        coro,
        ok_msg: str,
        ok_metadata=None,
    ) -> bool:
        """Run a phase coroutine with progress callbacks: started before, then
        ok or failed after. `ok_metadata` may be a dict or a 0-arg callable
        evaluated after success (so it can read post-phase state). Returns the
        phase's bool result. The coroutine's own failure is already recorded in
        _pending_failure by _fail_phase, which we relay in the 'failed' event."""
        await self._emit(phase_value, "started", started_msg)
        ok = await coro
        if not ok:
            pending = getattr(self, "_pending_failure", {})
            md_fail = {"phase": phase_value, "failureReason": pending.get("reason")}
            if self._surfaced_overlays:
                # _fail_phase already appended every surfaced overlay to the
                # failure message; expose them structurally too, and mark them
                # relayed so a later phase does not repeat them.
                md_fail["tnOverlayMessages"] = list(self._surfaced_overlays)
                self._overlays_reported.update(self._surfaced_overlays)
            await self._emit(
                phase_value, "failed",
                pending.get("message") or f"{phase_value} failed",
                metadata=md_fail,
            )
            return False
        md = ok_metadata() if callable(ok_metadata) else ok_metadata
        # Relay any TN overlay text surfaced since the last callback. Phase 3
        # (form detection) emits no callback of its own, so an "Important
        # Message" dismissed there would otherwise reach the CRM only when the
        # run FAILED — never on the success path this handling exists to create.
        unreported = [
            t for t in self._surfaced_overlays if t not in self._overlays_reported
        ]
        if unreported:
            ok_msg = f"{ok_msg} | TN overlay(s) surfaced: " + " || ".join(unreported)
            md = dict(md or {})
            md["tnOverlayMessages"] = unreported
            self._overlays_reported.update(unreported)
        await self._emit(phase_value, "ok", ok_msg, metadata=md)
        return True

    async def _finish_failure(self) -> TNExecutorOutputV2:
        """Build the failure output AND emit the terminal workflow_complete=failed
        event (the CRM writes its terminal activity log entry on this)."""
        out = self._build_failure_output()
        failed_phase = out.failed_phase.value if out.failed_phase else "unknown"
        await self._emit(
            "workflow_complete", "failed",
            f"Workflow failed at {failed_phase}: {out.error_message}",
            metadata={"failedPhase": failed_phase, "failureReason": out.failure_reason},
        )
        return out


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
