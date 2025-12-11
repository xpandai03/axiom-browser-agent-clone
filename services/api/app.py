"""
Axiom Builder API - FastAPI Application

CRITICAL FOR RAILWAY DEPLOYMENT:
- All heavy imports (Playwright, MCP runtime) are LAZY loaded
- Only lightweight imports at module level
- Health endpoints work even if other modules fail to load
"""
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Load .env file at startup (lightweight)
from dotenv import load_dotenv
load_dotenv()

# ONLY import lightweight modules at top level
# Heavy modules (mcp_runtime, mcp_client, workflow routes) loaded lazily
from .config import get_config, log_openai_key_status
from .routes.health import router as health_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup - keep this lightweight, don't load heavy modules
    config = get_config()
    logger.info("=" * 60)
    logger.info("AXIOM API STARTING")
    logger.info(f"Host: {config.host}")
    logger.info(f"Port: {config.port}")
    logger.info(f"PORT env var: {os.environ.get('PORT', 'not set')}")
    logger.info("=" * 60)
    log_openai_key_status()
    logger.info("MCP integration ready - browser will start on first workflow request")
    logger.info("Healthcheck endpoints: /health, /health/fast, /health/ready")

    yield

    # Shutdown - lazy import cleanup functions
    logger.info("Shutting down Axiom API...")
    try:
        from .mcp_client import shutdown_mcp_client
        await shutdown_mcp_client()
    except Exception as e:
        logger.warning(f"MCP client shutdown error: {e}")

    try:
        from .mcp_runtime import shutdown_runtime
        await shutdown_runtime()
    except Exception as e:
        logger.warning(f"Runtime shutdown error: {e}")

    logger.info("Cleanup complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    config = get_config()

    app = FastAPI(
        title="Axiom Builder API",
        description="Browser automation workflow execution API using Playwright MCP",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins.split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # CRITICAL: Include health router FIRST - it has zero heavy dependencies
    app.include_router(health_router)

    # Lazy load heavy routers - these import Playwright/MCP
    # Import at function call time, not module load time
    from .routes import workflow_router, resume_router
    from .routes.element_picker import router as element_picker_router

    app.include_router(workflow_router, prefix="/api")
    app.include_router(resume_router, prefix="/api")
    app.include_router(element_picker_router, prefix="/api")

    # Mount frontend static files
    frontend_path = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
    if os.path.exists(frontend_path):
        app.mount("/static", StaticFiles(directory=frontend_path), name="static")

        @app.get("/")
        async def serve_frontend():
            """Serve the frontend index.html."""
            return FileResponse(os.path.join(frontend_path, "index.html"))

    # Legacy endpoint for backward compatibility with original frontend
    @app.post("/run-workflow")
    async def legacy_run_workflow(
        instructions: str = None,
        job_description: str = None,
        resume=None,
    ):
        """
        Legacy endpoint for backward compatibility.

        Redirects to the new API structure.
        """
        from .routes.workflow import run_workflow_sync

        return await run_workflow_sync(
            instructions=instructions or "",
            job_description=job_description or "",
            resume=resume,
            user_data="{}",
        )

    return app


# Create app instance
app = create_app()


if __name__ == "__main__":
    import uvicorn

    config = get_config()

    logging.basicConfig(
        level=logging.DEBUG if config.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    uvicorn.run(
        "services.api.app:app",
        host=config.host,
        port=config.port,
        reload=config.debug,
    )
