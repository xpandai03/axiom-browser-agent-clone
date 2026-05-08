"""
Pydantic schemas for the Therapy Notes patient creation executor.

Defines the input/output contracts for a deterministic, linear browser
automation workflow that creates patients in TherapyNotes.
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

class TNPhase(str, Enum):
    """Deterministic execution phases for TN patient creation."""
    ENTRY = "entry"
    LOGIN = "login"
    NAVIGATE = "navigate"
    FILL_FORM = "fill_form"
    SAVE = "save"
    INSERT_RFS = "insert_rfs"


# ============================================================================
# Input Schema
# ============================================================================

class TNPatientInput(BaseModel):
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


# ============================================================================
# Logging Schema
# ============================================================================

class TNPhaseLog(BaseModel):
    """Structured log entry for a single execution phase."""

    phase: TNPhase
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

TNFailureReason = Literal[
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
]


# ============================================================================
# Output Schema
# ============================================================================

class TNExecutorOutput(BaseModel):
    """Output from the TN patient creation executor."""

    status: Literal["success", "error"] = Field(
        ...,
        description="Overall workflow status",
    )
    patient_name: Optional[str] = Field(
        None,
        description="Full name of created patient (on success)",
    )
    failed_phase: Optional[TNPhase] = Field(
        None,
        description="Phase where execution failed (on error)",
    )
    failure_reason: Optional[TNFailureReason] = Field(
        None,
        description="Structured failure reason (on error)",
    )
    error_message: Optional[str] = Field(
        None,
        description="Human-readable error detail (on error)",
    )
    logs: List[TNPhaseLog] = Field(
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
        logs: List[TNPhaseLog],
        duration_ms: int,
        tn_patient_url: Optional[str] = None,
        tn_patient_id: Optional[str] = None,
    ) -> "TNExecutorOutput":
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
        phase: TNPhase,
        reason: TNFailureReason,
        message: str,
        logs: List[TNPhaseLog],
        duration_ms: int,
    ) -> "TNExecutorOutput":
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
        )

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
