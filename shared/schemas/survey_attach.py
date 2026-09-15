"""
Schemas for attaching a survey PDF to an EXISTING TherapyNotes patient chart.

Deliberately separate from therapy_notes_v2: that contract belongs to the
create-and-schedule flow staff depend on daily, and this build must not be able
to affect it. Only proven MECHANICS are shared (login, the upload routine) —
never decision logic.

Design premise: a survey filed to the wrong chart is a PHI disclosure that
cannot be undone, while a refusal costs a staff member a minute. Every field is
required and every check must pass; there is no partial-confidence path.
"""

from enum import Enum
from typing import List, Optional, Literal

from pydantic import BaseModel, Field, field_validator
from datetime import datetime


class SurveyAttachPhase(str, Enum):
    ENTRY = "entry"
    LOGIN = "login"
    SEARCH = "search"
    SELECT = "select"
    VERIFY = "verify"
    ATTACH = "attach"


# Every refusal is one of these. They are the vocabulary the CRM will surface to
# staff, so each names a field or a situation a human can act on.
SurveyAttachFailureReason = Literal[
    "login_failed",
    "practice_code_rejected",
    "search_ui_not_found",
    "patient_not_found",           # zero results
    "multiple_candidates",         # >1 survives narrowing
    "result_set_possibly_truncated",  # too many rows to trust the set is complete
    "expected_chart_not_in_results",  # the CRM named a chart the search did not surface
    "chart_not_opened",            # click did not land on a patient record
    "field_unreadable",            # a verification field could not be read at all
    "name_mismatch",
    "dob_mismatch",
    "phone_mismatch",
    "clinician_mismatch",
    "clinician_unassigned",        # chart has no clinician assignments
    "pdf_download_failed",
    "pdf_unsupported_format",
    "attach_failed",
    "unknown_error",
]


class SurveyAttachInput(BaseModel):
    """
    Identifying details to verify against, plus the document to attach.

    Every identifying field is REQUIRED. A verification that skips a field it did
    not receive is not a verification.
    """

    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    dob: str = Field(
        ...,
        description="Date of birth, MM/DD/YYYY. The chart renders m/d/yyyy; both sides are normalised.",
        pattern=r"^\d{2}/\d{2}/\d{4}$",
    )
    phone: str = Field(
        ...,
        min_length=7,
        description="Patient phone as submitted. Compared on digits only.",
    )
    clinician_name: str = Field(
        ...,
        min_length=1,
        description=(
            "The clinician named on the survey. A patient may have SEVERAL "
            "assignments, so this is checked for membership, never equality."
        ),
    )

    expected_chart_id: Optional[str] = Field(
        None,
        max_length=64,
        description=(
            "The TherapyNotes chart id the CRM believes this survey belongs to, "
            "when it knows one. OPTIONAL, and absent is an ordinary case: a "
            "survey attached before matching ran, or one matched to a CRM "
            "contact that carries no chart id, arrives without it and is "
            "selected by name exactly as before.\n\n"
            "When PRESENT it decides WHICH record to open — the row whose link "
            "carries this id is opened regardless of how many rows came back, "
            "and if no row carries it the run refuses rather than falling back "
            "to name selection. It does NOT decide whether to attach: the "
            "four-field verification against the chart runs identically either "
            "way. An id says which chart; verification says whether it is the "
            "right person."
        ),
    )

    pdf_url: str = Field(..., description="HTTP(S) URL of the survey PDF to attach.")
    document_name: str = Field(
        ...,
        min_length=1,
        max_length=120,
        description="Document Name typed into TherapyNotes' upload modal.",
    )

    # Correlation, mirroring the create flow so the CRM can track a run.
    contact_id: Optional[int] = None
    run_id: Optional[str] = None
    callback_url: Optional[str] = None

    @field_validator("expected_chart_id")
    @classmethod
    def _blank_is_absent(cls, v):
        """
        "" and "   " mean the CRM had nothing, not that it wants a chart named
        by the empty string. Collapsing them here means no downstream check has
        to ask which kind of empty it is.
        """
        if v is None:
            return None
        v = str(v).strip()
        return v or None

    @field_validator("pdf_url")
    @classmethod
    def _http_url(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("pdf_url is required")
        v = v.strip()
        if not (v.lower().startswith("http://") or v.lower().startswith("https://")):
            raise ValueError("pdf_url must be an http(s) URL")
        return v

    @field_validator("phone")
    @classmethod
    def _has_digits(cls, v):
        digits = "".join(c for c in str(v) if c.isdigit())
        if len(digits) < 7:
            raise ValueError("phone must contain at least 7 digits")
        return v


class SurveyAttachPhaseLog(BaseModel):
    phase: SurveyAttachPhase
    status: Literal["success", "failure", "skipped"]
    message: str
    duration_ms: int = Field(ge=0)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SurveyAttachOutput(BaseModel):
    status: Literal["success", "error"]
    failed_phase: Optional[SurveyAttachPhase] = None
    failure_reason: Optional[SurveyAttachFailureReason] = None
    message: str = ""
    tn_patient_url: Optional[str] = None
    document_name: Optional[str] = None
    # How the record was chosen: "chart_id" when the CRM named one and the
    # search surfaced it, "name" for the name-and-date-of-birth narrowing.
    # Recorded on success AND on failure, because the question it answers —
    # "why did this refuse?" — is usually asked about a failure.
    selection_mode: Optional[Literal["chart_id", "name"]] = None
    logs: List[SurveyAttachPhaseLog] = Field(default_factory=list)
    duration_ms: int = 0

    @classmethod
    def success_result(cls, *, tn_patient_url, document_name, logs, duration_ms,
                       selection_mode=None):
        return cls(
            status="success",
            message=f"Attached '{document_name}' to the verified patient chart",
            tn_patient_url=tn_patient_url,
            document_name=document_name,
            selection_mode=selection_mode,
            logs=logs,
            duration_ms=duration_ms,
        )

    @classmethod
    def failure(cls, *, phase, reason, message, logs, duration_ms, tn_patient_url=None,
                selection_mode=None):
        return cls(
            status="error",
            failed_phase=phase,
            failure_reason=reason,
            message=message,
            tn_patient_url=tn_patient_url,
            selection_mode=selection_mode,
            logs=logs,
            duration_ms=duration_ms,
        )
