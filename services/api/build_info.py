"""
Build provenance.

Answers one question from runtime logs alone: WHICH COMMIT IS LIVE?

This exists because a fix sat committed on a branch while production ran older
code, and nothing in the runtime logs could distinguish the two — the failure
had to be inferred from which log lines were *absent*. The stamp below is
emitted as the first line of application startup so that inference is never
needed again.

Resolution order:
  1. RAILWAY_GIT_COMMIT_SHA / GIT_COMMIT_SHA env var — git-triggered builds
  2. BUILD_INFO.json written into the image during the Docker build
  3. UNKNOWN — reported at WARNING, because unknown provenance IS the defect
     this module exists to prevent. Never silently degrade to a blank value.
"""
import json
import logging
import os
from pathlib import Path
from typing import Dict

# Written by the Dockerfile after `COPY . .` (see the build-provenance stanza).
BUILD_INFO_PATH = Path(__file__).resolve().parents[2] / "BUILD_INFO.json"

_UNKNOWN = "unknown"


def get_build_info() -> Dict[str, str]:
    """
    Resolve the deployed commit SHA and image build time.

    Returns a dict with keys: commit, built_at, source. `source` records HOW the
    value was resolved, so a reader can judge how much to trust it.
    """
    # 1) Runtime env (Railway sets this on git-triggered deploys).
    for env_var in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA"):
        sha = (os.environ.get(env_var) or "").strip()
        if sha:
            return {
                "commit": sha,
                "built_at": _read_file_field("built_at"),
                "source": f"env:{env_var}",
            }

    # 2) Baked-in build file.
    commit = _read_file_field("commit")
    if commit and commit != _UNKNOWN:
        return {
            "commit": commit,
            "built_at": _read_file_field("built_at"),
            "source": "file:BUILD_INFO.json",
        }

    # 3) Nothing usable.
    return {"commit": _UNKNOWN, "built_at": _read_file_field("built_at"), "source": "none"}


def _read_file_field(field: str) -> str:
    """Best-effort single field read from BUILD_INFO.json. Never raises."""
    try:
        with open(BUILD_INFO_PATH, "r", encoding="utf-8") as fh:
            value = json.load(fh).get(field) or _UNKNOWN
            return str(value).strip() or _UNKNOWN
    except Exception:
        return _UNKNOWN


def log_build_stamp(logger: logging.Logger) -> Dict[str, str]:
    """
    Emit the provenance stamp. Call this FIRST at startup, before any other
    application logging, so it anchors the top of every deployment's logs.
    """
    info = get_build_info()
    if info["commit"] == _UNKNOWN:
        logger.warning(
            "[BUILD] commit=UNKNOWN built_at=%s source=%s "
            "— BUILD PROVENANCE UNAVAILABLE: the running commit cannot be "
            "confirmed from logs. Redeploy via a git-triggered build, or pass "
            "--build-arg GIT_COMMIT_SHA=<sha>.",
            info["built_at"],
            info["source"],
        )
    else:
        logger.info(
            "[BUILD] commit=%s built_at=%s source=%s",
            info["commit"],
            info["built_at"],
            info["source"],
        )
    return info
