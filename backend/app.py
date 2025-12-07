from fastapi import FastAPI, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import Optional
import json

from backend.workflow_parser import parse_instructions_to_steps
from backend.simulated_executor import execute_workflow
from backend.resume_generator import generate_tailored_resume

app = FastAPI(title="Job Application Automation Engine")

@app.get("/health")
async def health_check():
    return {"status": "healthy", "message": "Job Application Automation Engine is running"}

@app.post("/run-workflow")
async def run_workflow(
    instructions: str = Form(...),
    job_description: str = Form(""),
    resume: Optional[UploadFile] = File(None)
):
    resume_text = ""
    if resume:
        content = await resume.read()
        resume_text = content.decode("utf-8", errors="ignore")
    
    workflow_steps = await parse_instructions_to_steps(instructions)
    
    execution_logs = execute_workflow(workflow_steps)
    
    tailored_resume = None
    if resume_text and job_description:
        tailored_resume = await generate_tailored_resume(resume_text, job_description)
    
    return {
        "workflow_steps": workflow_steps,
        "execution_logs": execution_logs,
        "tailored_resume": tailored_resume
    }

app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.get("/")
async def serve_frontend():
    return FileResponse("frontend/index.html")
