"""
URL extraction endpoint for external callers (Wolfee).

POST /api/extract/render-text — render a job posting URL through Playwright
(skip_proxy=True, matching the TN executor's working config) and return
cleaned text suitable for an LLM cleanup pass.

Concurrency model: module-level asyncio.Lock around a single shared runtime.
At Wolfee's expected volume (50–200 extractions/day initially) sequential
service is fine. If sustained throughput grows past ~10 req/min, refactor to
per-request runtime instances or add a queue. For now, contention beyond a
short wait returns 429 so callers retry rather than block on us.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/extract", tags=["extract"])


# =============================================================================
# Singleton runtime + lock — see module docstring
# =============================================================================
_lock = asyncio.Lock()
_runtime = None  # type: ignore[var-annotated]   # PlaywrightRuntime, lazy

# 200ms wait for the lock before giving up and returning 429.
_LOCK_WAIT_SECONDS = 0.2

_MAX_URL_LEN = 2048
_DEFAULT_TIMEOUT_MS = 25_000
_PRIVATE_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

_LINKEDIN_JOB_VIEW_RE = re.compile(r"^https?://[^/]*linkedin\.com/jobs/view/(\d+)", re.IGNORECASE)
_LINKEDIN_HOST_RE = re.compile(r"(^|\.)linkedin\.com$", re.IGNORECASE)

_BLOCK_TEXT_PATTERNS = (
    "just a moment",
    "checking your browser",
    "additional verification required",
    "access denied",
    "you have been blocked",
    "are you a robot",
    "unusual traffic",
)


async def _get_runtime():
    """Lazily build a single PlaywrightRuntime tuned for guest URL fetches."""
    global _runtime
    if _runtime is None:
        # Lazy import keeps Playwright off the FastAPI startup path.
        from ..mcp_runtime import PlaywrightRuntime
        _runtime = PlaywrightRuntime(
            skip_proxy=True,             # SOCKS5 auth is broken; bypass like TN does
            skip_resource_blocking=True, # SPA pages need full CSS/JS
            skip_stealth=True,           # context.add_init_script handles fingerprint
        )
    return _runtime


# =============================================================================
# Request / response shapes
# =============================================================================
class RenderTextRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=_MAX_URL_LEN)
    wait_for_selector: Optional[str] = None
    dismiss_modal: bool = True
    timeout_ms: int = Field(_DEFAULT_TIMEOUT_MS, ge=1_000, le=60_000)


def _validate_url(raw: str) -> Optional[str]:
    """Return an error string if the URL is unsafe, else None."""
    if len(raw) > _MAX_URL_LEN:
        return f"URL exceeds {_MAX_URL_LEN} characters"
    try:
        parsed = urlparse(raw)
    except ValueError:
        return "URL is not parseable"
    if parsed.scheme not in ("http", "https"):
        return "Only http(s) URLs are supported"
    if not parsed.netloc:
        return "URL is missing a host"
    if "@" in parsed.netloc:
        return "URLs with embedded credentials are not allowed"
    host = (parsed.hostname or "").lower()
    if host in _PRIVATE_HOSTS or host.startswith("169.254."):
        return "URL targets a private host"
    return None


def _normalize(text: str) -> str:
    return re.sub(r"\s+\n", "\n", re.sub(r"[ \t]+", " ", text or "")).strip()


def _is_linkedin_job_view(url: str) -> Optional[str]:
    m = _LINKEDIN_JOB_VIEW_RE.match(url or "")
    return m.group(1) if m else None


def _looks_blocked(title: str, body_text: str) -> bool:
    haystack = (title + "\n" + body_text)[:2000].lower()
    return any(p in haystack for p in _BLOCK_TEXT_PATTERNS)


async def _dismiss_modal(page) -> Optional[str]:
    """Best-effort modal dismissal: Dismiss button → modal__dismiss → ESC."""
    for sel in ("button[aria-label='Dismiss']", "button.modal__dismiss"):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=300):
                await loc.click(timeout=1500)
                return sel
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
        return "Escape"
    except Exception:
        return None


# =============================================================================
# Endpoint
# =============================================================================
@router.post("/render-text")
async def render_text(req: RenderTextRequest):
    started = time.time()

    err = _validate_url(req.url)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Acquire the lock with a short cap so callers see a fast 429 instead
    # of stacking up behind a slow render.
    try:
        await asyncio.wait_for(_lock.acquire(), timeout=_LOCK_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=429,
            content={"error": "Service busy, retry in a moment"},
        )

    try:
        runtime = await _get_runtime()
        page = await runtime.ensure_browser()

        # Navigate. The runtime already retries with fresh contexts on
        # transient failures; we just translate its result into our shape.
        nav = await runtime.navigate(
            req.url,
            wait_until="domcontentloaded",
            timeout=req.timeout_ms,
        )

        final_url = page.url
        status = nav.get("status")

        if not nav.get("success"):
            return {
                "ok": False,
                "url": req.url,
                "reason": "navigation_failed",
                "final_url": final_url,
                "status": status,
                "title": None,
                "duration_ms": int((time.time() - started) * 1000),
            }

        # Brief settle for client-side hydration before dismiss/extract.
        await asyncio.sleep(1.5)

        if req.dismiss_modal:
            try:
                await _dismiss_modal(page)
            except Exception as e:
                logger.debug(f"modal dismiss errored (non-fatal): {e}")

        # Expired-redirect detection: LinkedIn quietly bounces stale
        # /jobs/view/<id> URLs to a search page that *also* fills the JD
        # selector with a different job's data. Refuse rather than mislead.
        original_id = _is_linkedin_job_view(req.url)
        if original_id:
            final_id = _is_linkedin_job_view(final_url)
            host_ok = _LINKEDIN_HOST_RE.search(urlparse(final_url).hostname or "") is not None
            if not host_ok or final_id != original_id:
                return {
                    "ok": False,
                    "url": req.url,
                    "reason": "expired_redirect",
                    "final_url": final_url,
                    "status": status,
                    "title": None,
                    "duration_ms": int((time.time() - started) * 1000),
                }

        # Optional selector wait. If it times out we still return the body
        # text — the wait is a best-effort cue, not a gate.
        jd_text: Optional[str] = None
        if req.wait_for_selector:
            try:
                remaining = max(2_000, req.timeout_ms - int((time.time() - started) * 1000))
                await page.wait_for_selector(req.wait_for_selector, timeout=remaining)
                jd_text = _normalize(await page.inner_text(req.wait_for_selector))
            except Exception as e:
                logger.info(f"wait_for_selector miss ({req.wait_for_selector}): {e}")
                jd_text = None

        # Title + body text
        try:
            title = (await page.title()) or ""
        except Exception:
            title = ""

        try:
            body_text = await page.inner_text("body")
        except Exception:
            body_text = ""
        body_text = _normalize(body_text)

        # Block detection: prefer the runtime helper if it flags a hard
        # provider (Cloudflare/CAPTCHA), otherwise fall back to a text
        # heuristic on the title/body.
        try:
            blk = await runtime.detect_block()
            hard_block = bool(blk.get("blocked")) and blk.get("block_type") in {
                "cloudflare", "cloudflare_challenge", "turnstile",
                "recaptcha", "hcaptcha", "rate_limited", "bot_detection",
                "access_denied",
            }
        except Exception:
            hard_block = False

        if hard_block or _looks_blocked(title, body_text):
            return {
                "ok": False,
                "url": req.url,
                "reason": "blocked",
                "final_url": final_url,
                "status": status,
                "title": title or None,
                "duration_ms": int((time.time() - started) * 1000),
            }

        return {
            "ok": True,
            "url": req.url,
            "final_url": final_url,
            "status": status,
            "title": title,
            "text": body_text,
            "jd_text": jd_text,
            "duration_ms": int((time.time() - started) * 1000),
        }

    except asyncio.TimeoutError:
        return {
            "ok": False,
            "url": req.url,
            "reason": "timeout",
            "final_url": req.url,
            "status": None,
            "title": None,
            "duration_ms": int((time.time() - started) * 1000),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"render-text failed for {req.url}: {e}")
        return {
            "ok": False,
            "url": req.url,
            "reason": "navigation_failed",
            "final_url": req.url,
            "status": None,
            "title": None,
            "duration_ms": int((time.time() - started) * 1000),
        }
    finally:
        if _lock.locked():
            _lock.release()
