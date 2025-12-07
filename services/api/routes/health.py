from fastapi import APIRouter
from datetime import datetime

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "axiom-api",
    }


@router.get("/ready")
async def readiness_check():
    """Readiness check for Kubernetes/Docker."""
    # TODO: Check Redis connection
    return {
        "status": "ready",
        "timestamp": datetime.utcnow().isoformat(),
    }
