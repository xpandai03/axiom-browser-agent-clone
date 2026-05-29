"""
Pydantic schemas for the Therapy Notes patient creation executor (V2).

Step 1 clone: byte-for-byte identical contract to shared/schemas/therapy_notes.py
with V2-suffixed type names. Field names and enum string values are intentionally
unchanged so the wire payload/response shape matches the existing route exactly.

V2 exists as a parallel surface that Step 2 will extend with PDF upload + appointment
scheduling phases (new fields, new enum values). Cloning now — rather than aliasing the
V1 types — is deliberate: the two schemas will diverge in Step 2.
"""

import logging
from enum import Enum
from typing import Optional, List, Literal
from pydantic import BaseModel, Field, field_validator
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================================
# Phase Enum
# ============================================================================

class TNPhaseV2(str, Enum):
    """Deterministic execution phases for TN patient creation."""
    ENTRY = "entry"
    LOGIN = "login"
    NAVIGATE = "navigate"
    FILL_FORM = "fill_form"
    SAVE = "save"
    INSERT_RFS = "insert_rfs"
    # Step 3 — extended V2 workflow phases (run after SAVE)
    UPLOAD_INTAKE_PDF = "upload_intake_pdf"
    UPLOAD_SNAPSHOT_PDF = "upload_snapshot_pdf"
    SCHEDULE_APPOINTMENT = "schedule_appointment"


# ============================================================================
# Input Schema
# ============================================================================

class TNPatientInputV2(BaseModel):
    """Input for creating a patient in TherapyNotes."""

    first_name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Patient legal first name",
    )
    last_name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Patient legal last name",
    )
    dob: str = Field(
        ...,
        description="Date of birth (MM/DD/YYYY)",
        pattern=r"^\d{2}/\d{2}/\d{4}$",
    )
    address: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Street address line 1",
    )
    zip: str = Field(
        ...,
        description=(
            "5-digit US zip code. ZIP+4 (e.g. 87507-2691) is normalized to "
            "the first 5 digits. 4-digit ZIPs are padded with a leading zero "
            "(e.g. 7031 -> 07031) to recover from upstream leading-zero strip."
        ),
        pattern=r"^\d{5}$",
    )

    @field_validator("zip", mode="before")
    @classmethod
    def normalize_zip(cls, v):
        """
        Normalize ZIP input before regex validation.

        - ZIP+4 ("87507-2691") -> "87507"
        - 4-digit numeric ("7031") -> "07031" (pad leading zero, log warning)
        - 3-digit, non-numeric, empty -> ValueError with clear message

        Reason: upstream pipelines (CSV ingest, Excel, JSON-from-numeric)
        commonly strip leading zeros from northeastern US ZIPs (00xxx-09xxx).
        Padding 4-digit ZIPs is the dominant correct fix; anything else is
        rejected loudly so bad data does not silently land.
        """
        if v is None:
            raise ValueError("ZIP is required")
        if not isinstance(v, str):
            v = str(v)
        v = v.strip()
        if not v:
            raise ValueError("ZIP is required")

        # ZIP+4 -> base 5 digits
        v = v.split("-", 1)[0].strip()

        if not v.isdigit():
            raise ValueError(
                f"ZIP must contain only digits (got: {v!r})"
            )

        if len(v) == 4:
            original = v
            v = "0" + v
            logger.warning(
                "[ZIP NORMALIZE] Padded 4-digit ZIP %r -> %r "
                "(leading zero added; likely upstream zero-strip)",
                original,
                v,
            )
        elif len(v) != 5:
            raise ValueError(
                f"ZIP must be 5 digits (got {len(v)} digits: {v!r})"
            )

        return v

    sex: Literal["Male", "Female"] = Field(
        ...,
        description="Administrative sex (radio select)",
    )
    email: str = Field(
        ...,
        description="Patient email address",
    )
    phone: str = Field(
        ...,
        description="Patient mobile phone number",
    )
    rfs_url: str = Field(
        ...,
        description="SharePoint URL to the referral form submission (RFS)",
    )

    # ------------------------------------------------------------------
    # Step 3 — extended V2 fields (PDF uploads + appointment scheduling)
    # ------------------------------------------------------------------
    intake_pdf_url: str = Field(
        ...,
        description=(
            "HTTP(S) URL to the intake/referral PDF. Uploaded to the patient "
            "record with document name 'Intake Referral'."
        ),
    )
    snapshot_pdf_url: str = Field(
        ...,
        description=(
            "HTTP(S) URL to the initial appointment confirmation snapshot PDF. "
            "Uploaded with document name 'Initial Appointment Confirmation Email'."
        ),
    )
    appointment_date: str = Field(
        ...,
        description="Appointment date, m/d/yyyy (single-digit month/day OK, e.g. 5/28/2026)",
        pattern=r"^\d{1,2}/\d{1,2}/\d{4}$",
    )
    appointment_time: str = Field(
        ...,
        description="Appointment start time, h:mm am/pm (e.g. 2:00 pm)",
        pattern=r"^\d{1,2}:\d{2}\s*[AaPp][Mm]$",
    )
    appointment_alert_text: str = Field(
        ...,
        min_length=1,
        description=(
            "Pre-formatted appointment alert text, e.g. "
            "'New Individual In-Person Therapy CRM'. Modality (In-Person/Telehealth) "
            "is inferred from this text: if it contains 'Telehealth', the Telehealth "
            "checkbox is set."
        ),
    )
    clinician_name: str = Field(
        ...,
        min_length=1,
        description=(
            "Assigned provider's full name as it appears in TN's clinician dropdown. "
            "Selected manually via the type-to-filter DynamicDropdown."
        ),
    )

    @field_validator("intake_pdf_url", "snapshot_pdf_url")
    @classmethod
    def validate_http_url(cls, v, info):
        """PDF source URLs must be HTTP(S) — they are fetched server-side."""
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{info.field_name} is required")
        v = v.strip()
        if not (v.lower().startswith("http://") or v.lower().startswith("https://")):
            raise ValueError(
                f"{info.field_name} must be an http(s) URL (got: {v[:60]!r})"
            )
        return v


# ============================================================================
# Logging Schema
# ============================================================================

class TNPhaseLogV2(BaseModel):
    """Structured log entry for a single execution phase."""

    phase: TNPhaseV2
    status: Literal["success", "failure", "skipped"]
    message: str
    duration_ms: int = Field(ge=0)
    screenshot_path: Optional[str] = Field(
        None,
        description="Path to screenshot captured on failure",
    )
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Failure Reasons
# ============================================================================

TNFailureReasonV2 = Literal[
    "login_failed",
    "practice_code_rejected",
    "dashboard_not_loaded",
    "mfa_required",
    "navigation_failed",
    "new_patient_form_not_found",
    "form_field_not_found",
    "zip_autocomplete_failed",
    "save_failed",
    "patient_duplicate_detected",
    "patient_confirmation_failed",
    "rfs_note_creation_failed",
    "session_expired",
    "selector_not_found",
    "unknown_error",
    # Step 3 — extended V2 workflow failure reasons
    "pdf_download_failed",
    "pdf_unsupported_format",
    "pdf_upload_ui_not_found",
    "intake_pdf_upload_failed",
    "snapshot_pdf_upload_failed",
    "clinician_selection_failed",
    "scheduling_ui_not_found",
    "appointment_creation_failed",
]


# ============================================================================
# Output Schema
# ============================================================================

class TNExecutorOutputV2(BaseModel):
    """Output from the TN patient creation executor."""

    status: Literal["success", "error"] = Field(
        ...,
        description="Overall workflow status",
    )
    patient_name: Optional[str] = Field(
        None,
        description="Full name of created patient (on success)",
    )
    failed_phase: Optional[TNPhaseV2] = Field(
        None,
        description="Phase where execution failed (on error)",
    )
    failure_reason: Optional[TNFailureReasonV2] = Field(
        None,
        description="Structured failure reason (on error)",
    )
    error_message: Optional[str] = Field(
        None,
        description="Human-readable error detail (on error)",
    )
    logs: List[TNPhaseLogV2] = Field(
        default_factory=list,
        description="Ordered log entries for every phase executed",
    )
    screenshot_paths: List[str] = Field(
        default_factory=list,
        description="Paths to all failure screenshots captured",
    )
    duration_ms: int = Field(
        default=0,
        ge=0,
        description="Total execution time in milliseconds",
    )
    tn_patient_url: Optional[str] = Field(
        None,
        description="TherapyNotes URL for the created patient (on success)",
    )
    tn_patient_id: Optional[str] = Field(
        None,
        description="TherapyNotes patient ID extracted from URL (on success)",
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
    )

    @classmethod
    def success(
        cls,
        patient_name: str,
        logs: List[TNPhaseLogV2],
        duration_ms: int,
        tn_patient_url: Optional[str] = None,
        tn_patient_id: Optional[str] = None,
    ) -> "TNExecutorOutputV2":
        return cls(
            status="success",
            patient_name=patient_name,
            logs=logs,
            screenshot_paths=[
                log.screenshot_path for log in logs
                if log.screenshot_path is not None
            ],
            duration_ms=duration_ms,
            tn_patient_url=tn_patient_url,
            tn_patient_id=tn_patient_id,
        )

    @classmethod
    def failure(
        cls,
        phase: TNPhaseV2,
        reason: TNFailureReasonV2,
        message: str,
        logs: List[TNPhaseLogV2],
        duration_ms: int,
        tn_patient_url: Optional[str] = None,
        tn_patient_id: Optional[str] = None,
    ) -> "TNExecutorOutputV2":
        # Partial-success (Step 3, decision I3): when a post-save phase fails the
        # patient already exists in TN, so the caller still needs the patient
        # URL/ID for manual follow-up. These are populated when available.
        return cls(
            status="error",
            failed_phase=phase,
            failure_reason=reason,
            error_message=message,
            logs=logs,
            screenshot_paths=[
                log.screenshot_path for log in logs
                if log.screenshot_path is not None
            ],
            duration_ms=duration_ms,
            tn_patient_url=tn_patient_url,
            tn_patient_id=tn_patient_id,
        )

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
