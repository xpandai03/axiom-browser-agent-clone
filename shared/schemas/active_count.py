"""
Schemas for reading per-clinician ACTIVE CLIENT COUNTS out of TherapyNotes.

This is the first route that only READS. Every other TherapyNotes path creates a
patient, schedules, uploads, or verifies a chart it navigated to. Nothing here
opens a chart, clicks a result row, or touches a control that could write.

Design premise, and the reason the shapes below look the way they do: the number
this returns becomes the DENOMINATOR of a completion percentage on the client's
headline sheet. A count that is wrong is worse than a count that is missing,
because a missing one is visible and a wrong one is not. So:

  - every value is returned RAW, exactly as TherapyNotes rendered it;
  - nothing is subtracted, normalised, cleaned or interpreted here;
  - a count that cannot be parsed is returned as a FAILURE, never as a guess;
  - one clinician failing never discards the ones that succeeded.

Storage, scheduling, provider matching and any decision about Test Anna belong
to the CRM. This contract deliberately gives the CRM raw material and no
opinions.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ActiveCountPhase(str, Enum):
    ENTRY = "entry"
    LOGIN = "login"
    NAVIGATE = "navigate"
    ENUMERATE = "enumerate"   # read the clinician dropdown's options
    READ_COUNTS = "read_counts"


# Why a single clinician's read failed. Each names something a human can act on.
ActiveCountFailureReason = Literal[
    "login_failed",
    "practice_code_rejected",
    "patients_page_not_found",     # the Patients list never rendered
    "clinician_dropdown_missing",  # the "Assigned to" select was not present
    "no_clinicians_found",         # the select rendered but held no options
    "option_select_failed",        # could not apply this clinician's filter
    "count_text_not_found",        # no "Displaying ..." element on the page
    "count_unparseable",           # text present but no total could be read
    "timeout",
    "unknown_error",
]


class ClinicianActiveCount(BaseModel):
    """
    One row of the pass: what TherapyNotes says about one dropdown option.

    `label` is returned VERBATIM. Matching it to a CRM provider is the CRM's job,
    and a label mangled here cannot be un-mangled there.
    """

    option_value: str = Field(
        ...,
        description="The <option> value, e.g. 'clinician-1005471'. Stable id, safe to key on.",
    )
    label: str = Field(
        ...,
        description="The <option> text EXACTLY as TherapyNotes renders it. Never normalised.",
    )
    is_aggregate: bool = Field(
        False,
        description=(
            "True for the 'Any Clinician' option, which is a practice-wide total "
            "rather than a person. Returned so the CRM can cross-check the sum "
            "without having to recognise the label."
        ),
    )

    status: Literal["success", "failure"] = "success"
    active_count: Optional[int] = Field(
        None,
        description="Active patients for this option. None when status == 'failure'.",
    )
    failure_reason: Optional[ActiveCountFailureReason] = None
    message: Optional[str] = Field(
        None, description="Human-readable detail. Never contains a patient value."
    )

    count_text: Optional[str] = Field(
        None,
        description=(
            "The raw phrase the count was parsed from, e.g. "
            "'Displaying 1-20 of 47 active patients'. Kept as evidence for the "
            "number. Contains digits and fixed words only — the element it is "
            "read from is required to contain 'Displaying', which no patient "
            "value does."
        ),
    )

    # --- Test Anna, reported but NEVER subtracted ---------------------------
    # Whether these should be excluded, and from whose number, is the client's
    # call. He raised the record himself and may prefer to clean it up in
    # TherapyNotes instead.
    test_anna_exact: Optional[int] = Field(
        None,
        description=(
            "Rows under this clinician whose name is exactly 'Test Anna' / "
            "'Anna Test', counted within the SAME active filter as active_count, "
            "so the two are directly comparable. Matched inside the browser; "
            "only the count crosses into the process."
        ),
    )
    test_anna_token_match: Optional[int] = Field(
        None,
        description=(
            "Rows containing BOTH tokens but not matching exactly (e.g. a "
            "trailing digit). Reported alongside the strict count so an "
            "undercount by the strict matcher is visible rather than silent."
        ),
    )
    test_anna_status: Literal["success", "failure", "skipped"] = "skipped"


class ActiveCountPhaseLog(BaseModel):
    phase: ActiveCountPhase
    status: Literal["success", "failure"]
    message: str
    duration_ms: int


class ActiveCountOutput(BaseModel):
    """
    The whole pass.

    `status` is 'success' only when every option read cleanly. 'partial' means
    some clinicians are usable and some are not — which is the useful outcome the
    CRM should expect, not an error.
    """

    status: Literal["success", "partial", "failure"]
    captured_at: str = Field(
        ..., description="UTC ISO-8601 timestamp for the pass as a whole."
    )
    duration_ms: int

    total_options: int = Field(0, description="Dropdown options attempted.")
    succeeded: int = 0
    failed: int = 0

    results: List[ClinicianActiveCount] = Field(default_factory=list)
    logs: List[ActiveCountPhaseLog] = Field(default_factory=list)

    # Set only when the pass could not start at all (login, navigation).
    failed_phase: Optional[ActiveCountPhase] = None
    failure_reason: Optional[ActiveCountFailureReason] = None
    message: Optional[str] = None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @classmethod
    def failure(
        cls,
        phase: ActiveCountPhase,
        reason: ActiveCountFailureReason,
        message: str,
        logs: List[ActiveCountPhaseLog],
        duration_ms: int,
    ) -> "ActiveCountOutput":
        """The pass never got going. No per-clinician rows exist."""
        return cls(
            status="failure",
            captured_at=cls._now(),
            duration_ms=duration_ms,
            total_options=0,
            succeeded=0,
            failed=0,
            results=[],
            logs=logs,
            failed_phase=phase,
            failure_reason=reason,
            message=message,
        )

    @classmethod
    def completed(
        cls,
        results: List[ClinicianActiveCount],
        logs: List[ActiveCountPhaseLog],
        duration_ms: int,
    ) -> "ActiveCountOutput":
        """
        A pass that ran. 'partial' when any option failed — deliberately NOT
        'failure', because 28 of 30 with two named failures is a usable result
        and the CRM should treat it as one.
        """
        ok = sum(1 for r in results if r.status == "success")
        bad = len(results) - ok
        return cls(
            status="success" if bad == 0 else ("partial" if ok else "failure"),
            captured_at=cls._now(),
            duration_ms=duration_ms,
            total_options=len(results),
            succeeded=ok,
            failed=bad,
            results=results,
            logs=logs,
        )
