"""
Schemas for reading ACTIVE PATIENT IDENTITY out of TherapyNotes.

The second route that only reads, and the larger of the two: the active-count
route returns one integer per clinician, this returns roughly a thousand
identity rows. Nothing here opens a chart, clicks a result row, or touches a
control that could write.

WHY THIS EXISTS. Survey matching can only find what it can see, and it sees CRM
contacts. About half the practice's active patients predate the CRM, so a survey
from one of them matches nothing — which is what happened on the client call,
twice, with correct details. This gives the CRM a local copy of the identity
fields to match against, so no TherapyNotes login is needed during the day.

VERBATIM, ALWAYS. Every value is returned exactly as TherapyNotes rendered it.
Nothing is normalised, reformatted, split, padded or case-folded. The CRM
normalises for matching, and a value cleaned here cannot be un-cleaned there —
this project has already lost a morning to a display-corrected name being used
as a lookup key.

A MISSING FIELD IS DATA, NOT AN ERROR. A patient with no phone on file is a real
patient. The row comes back with that field empty and the pass reports how many
were empty, so the CRM knows how much of the population can be matched on four
fields rather than three.

NO DE-DUPLICATION. A patient assigned to several clinicians appears once per
clinician, because that is what the page shows. Collapsing on chart id is the
CRM's decision and it needs the occurrences to make it.
"""

from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ActivePatientsPhase(str, Enum):
    ENTRY = "entry"
    LOGIN = "login"
    NAVIGATE = "navigate"
    ENUMERATE = "enumerate"    # read the clinician dropdown's options
    READ_ROWS = "read_rows"


# Why one clinician's read failed. Each names something a human can act on.
ActivePatientsFailureReason = Literal[
    "login_failed",
    "practice_code_rejected",
    "patients_page_not_found",
    "clinician_dropdown_missing",
    "no_clinicians_found",
    "option_select_failed",       # could not apply this clinician's filter
    "rows_unreadable",            # the results table did not yield rows
    "pager_stuck",                # Next was present but the page did not advance
    "timeout",
    "unknown_error",
]


class PatientIdentityRow(BaseModel):
    """
    One patient, as one clinician's Active list renders them.

    EVERY STRING IS VERBATIM. An empty string means the page showed nothing
    there, which is different from the field being unavailable — see
    ActivePatientsOutput.columns_found for that distinction.
    """

    chart_id: str = Field(
        ...,
        description=(
            "The patient id from the result anchor's href. The one exact "
            "identifier on the row, and what makes a later attach unambiguous."
        ),
    )
    name: str = Field(..., description="Name exactly as rendered in the result link.")
    dob: str = Field(
        "",
        description="Date of birth exactly as rendered. Empty when the cell held none.",
    )
    phone: str = Field(
        "",
        description=(
            "Phone exactly as rendered, including punctuation. Empty when the "
            "cell held none OR when the table carries no phone column at all — "
            "columns_found distinguishes the two."
        ),
    )
    clinician_option_value: str = Field(
        ..., description="The dropdown option this row was read under, e.g. 'clinician-1005471'."
    )
    clinician_label: str = Field(
        ..., description="That option's text, verbatim. A staff name, not a patient value."
    )


class ClinicianPatientPage(BaseModel):
    """What one clinician's Active list produced."""

    option_value: str
    label: str = Field(..., description="Option text EXACTLY as rendered. Never normalised.")
    is_aggregate: bool = Field(
        False,
        description=(
            "True for the practice-wide 'Any Clinician' option. Returned so the "
            "CRM can cross-check totals without recognising the label, and NOT "
            "read for identity rows — every patient would appear twice."
        ),
    )

    status: Literal["success", "failure"] = "success"
    failure_reason: Optional[ActivePatientsFailureReason] = None
    message: Optional[str] = Field(
        None, description="Human-readable detail. Never contains a patient value."
    )

    row_count: int = 0
    pages_read: int = 0
    truncated: bool = Field(
        False,
        description=(
            "True when the pager was still offering more pages at MAX_PAGES. "
            "The rows returned are real; there are simply more of them."
        ),
    )

    # Per-clinician emptiness, so the CRM can see how much of the population is
    # matchable on four fields rather than three. Counts only.
    empty_dob: int = 0
    empty_phone: int = 0
    empty_name: int = 0

    rows: List[PatientIdentityRow] = Field(default_factory=list)


class ActivePatientsPhaseLog(BaseModel):
    phase: ActivePatientsPhase
    status: Literal["success", "failure"]
    message: str
    duration_ms: int


class ActivePatientsOutput(BaseModel):
    """
    The whole pass.

    `status` is 'success' only when every clinician read cleanly. 'partial' means
    some are usable and some are not, which is the outcome the CRM should expect
    rather than treat as an error.
    """

    status: Literal["success", "partial", "failure"]
    captured_at: str = Field(..., description="UTC ISO-8601 timestamp for the pass as a whole.")
    duration_ms: int = 0

    total_options: int = Field(0, description="Dropdown options attempted.")
    succeeded: int = 0
    failed: int = 0

    total_rows: int = Field(0, description="Identity rows returned across all clinicians.")
    distinct_chart_ids: int = Field(
        0,
        description=(
            "How many chart ids are distinct. total_rows minus this is the "
            "shared-care population — patients assigned to more than one "
            "clinician, which the CRM's matcher has to handle."
        ),
    )

    columns_found: Dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Which identity columns the results table actually carried, decided "
            "from its own header row on this pass. A field reported false was "
            "NOT on the page, so every row's value for it is empty by absence "
            "rather than by the patient having none."
        ),
    )
    column_headers: List[str] = Field(
        default_factory=list,
        description=(
            "The results table's header labels, verbatim, so a layout change is "
            "visible in the response instead of being inferred from empty fields. "
            "Column headings are page furniture and contain no patient value."
        ),
    )

    results: List[ClinicianPatientPage] = Field(default_factory=list)
    logs: List[ActivePatientsPhaseLog] = Field(default_factory=list)

    # Set only when the pass could not start at all (login, navigation).
    failed_phase: Optional[ActivePatientsPhase] = None
    failure_reason: Optional[ActivePatientsFailureReason] = None
    message: Optional[str] = None

    @classmethod
    def failure(cls, phase, reason, message, logs, duration_ms):
        from datetime import datetime, timezone
        return cls(
            status="failure",
            captured_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=duration_ms,
            failed_phase=phase,
            failure_reason=reason,
            message=message,
            logs=logs,
        )

    @classmethod
    def completed(cls, results, columns_found, column_headers, logs, duration_ms):
        from datetime import datetime, timezone
        ok = sum(1 for r in results if r.status == "success")
        bad = len(results) - ok
        # The aggregate option is excluded from the row totals: it repeats every
        # patient the individual clinicians already returned.
        people = [r for r in results if not r.is_aggregate]
        total_rows = sum(len(r.rows) for r in people)
        ids = {row.chart_id for r in people for row in r.rows if row.chart_id}
        return cls(
            status="success" if bad == 0 else ("failure" if ok == 0 else "partial"),
            captured_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=duration_ms,
            total_options=len(results),
            succeeded=ok,
            failed=bad,
            total_rows=total_rows,
            distinct_chart_ids=len(ids),
            columns_found=columns_found,
            column_headers=column_headers,
            results=results,
            logs=logs,
        )
