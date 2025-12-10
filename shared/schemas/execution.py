from typing import Optional, List, Literal, Union
from pydantic import BaseModel, Field
from datetime import datetime
import uuid


ExecutionStatus = Literal["pending", "running", "success", "failed", "skipped"]


class FieldFillResult(BaseModel):
    """Result of filling a single form field."""
    field_name: str = Field(..., description="Name of the field that was filled")
    selector_used: str = Field(..., description="Selector that was used to find the field")
    success: bool = Field(True, description="Whether the field was successfully filled")
    error: Optional[str] = Field(None, description="Error message if field fill failed")


class StepResult(BaseModel):
    """Result of executing a single workflow step."""

    step_number: int = Field(..., ge=0, description="Zero-indexed step number")
    action: str = Field(..., description="Action type that was executed")
    status: ExecutionStatus = Field(..., description="Execution status")
    duration_ms: int = Field(..., ge=0, description="Execution duration in milliseconds")
    screenshot_base64: Optional[str] = Field(None, description="Base64-encoded screenshot after step")
    logs: List[str] = Field(default_factory=list, description="Log messages during execution")
    error: Optional[str] = Field(None, description="Error message if step failed")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="When step completed")
    # Extract action result field
    extracted_data: Optional[Union[List[str], str]] = Field(None, description="Data extracted from page (for extract action)")
    # Fill form action result field
    fields_filled: Optional[List[FieldFillResult]] = Field(None, description="Details of form fields that were filled")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class WorkflowResult(BaseModel):
    """Complete result of a workflow execution."""

    workflow_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique workflow execution ID")
    steps: List[StepResult] = Field(default_factory=list, description="Results for each step")
    total_duration_ms: int = Field(0, ge=0, description="Total execution duration in milliseconds")
    success: bool = Field(True, description="Whether all steps completed successfully")
    error: Optional[str] = Field(None, description="Overall error if workflow failed")
    started_at: datetime = Field(default_factory=datetime.utcnow, description="When execution started")
    completed_at: Optional[datetime] = Field(None, description="When execution completed")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }

    def add_step_result(self, result: StepResult) -> None:
        """Add a step result and update totals."""
        self.steps.append(result)
        self.total_duration_ms += result.duration_ms
        if result.status == "failed":
            self.success = False

    def complete(self, error: Optional[str] = None) -> None:
        """Mark workflow as completed."""
        self.completed_at = datetime.utcnow()
        if error:
            self.success = False
            self.error = error


class WorkflowJob(BaseModel):
    """Job message for Redis queue."""

    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique job ID")
    workflow_request: dict = Field(..., description="Serialized WorkflowRequest")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="When job was created")

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }
