# Job Application Automation Engine

## Overview
A demo browser automation orchestrator (Axiom-style) that converts natural language instructions into simulated browser workflow steps. Built as a 2-day MVP to demonstrate job application automation concepts.

## Current State
V0 MVP - Fully functional with:
- Natural language to JSON workflow conversion (OpenAI)
- Simulated browser action execution
- AI-powered resume tailoring

## Project Architecture

### File Structure
```
/backend
  __init__.py           # Package init
  app.py                # FastAPI main application
  workflow_parser.py    # OpenAI-powered instruction parser
  simulated_executor.py # Simulated browser execution
  resume_generator.py   # OpenAI-powered resume tailoring
/frontend
  index.html            # Main UI page
  styles.css            # Styling
  main.js               # Frontend logic
```

### Tech Stack
- **Backend**: Python 3.11, FastAPI, Uvicorn
- **Frontend**: Vanilla HTML/CSS/JavaScript
- **AI**: OpenAI via Replit AI Integrations (no API key needed, billed to credits)

### Key Endpoints
- `GET /` - Serves the frontend
- `GET /health` - Health check
- `POST /run-workflow` - Main workflow endpoint
  - Accepts: instructions (form), job_description (form), resume (file)
  - Returns: workflow_steps, execution_logs, tailored_resume

## Running the Project
```bash
uvicorn backend.app:app --host 0.0.0.0 --port 5000
```

## Features

### 1. Workflow Parser
Converts natural language like "Go to URL → click Apply → fill name" into structured JSON steps.

### 2. Simulated Executor
Runs through each workflow step and returns execution logs (no real browser automation in V0).

### 3. Resume Tailoring
Takes a resume + job description and generates an optimized version using OpenAI.

## Out of Scope for V0
- Real Playwright/browser automation
- Database persistence
- Authentication
- Multi-user support
- Payment processing
