"""
API routes for the TherapyNotes patient creation executor.

Endpoints:
- POST /api/tn/create-patient - Create a patient in TherapyNotes
- GET  /api/tn/test           - Health check for TN executor
"""

import logging
from fastapi import APIRouter, HTTPException

from shared.schemas.therapy_notes import TNPatientInput, TNExecutorOutput

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tn", tags=["therapy-notes"])


@router.post("/create-patient", response_model=TNExecutorOutput)
async def create_patient(request: TNPatientInput):
    """
    Create a patient in TherapyNotes via headless browser automation.

    Executes a deterministic 6-phase workflow:
      0. ENTRY    — Navigate to TN, handle practice code
      1. LOGIN    — Authenticate with service account
      2. NAVIGATE — Sidebar → Patients → New Patient
      3. FILL     — Populate patient form fields
      4. SAVE     — Submit and confirm creation
      5. RFS      — Insert referral note with RFS URL

    Returns structured output with per-phase logs and failure screenshots.
    """
    try:
        logger.info(f"TN patient creation: {request.first_name} {request.last_name}")

        # Lazy import to avoid loading Playwright at startup
        from ..mcp_runtime import PlaywrightRuntime
        from ..tn_executor import run_tn_patient_creation

        runtime = PlaywrightRuntime(skip_proxy=True, skip_resource_blocking=True, skip_stealth=True)

        try:
            result = await run_tn_patient_creation(runtime, request)
        finally:
            # Always close the browser after workflow completes
            await runtime.close()

        if result.status == "success":
            logger.info(f"TN patient created: {result.patient_name}")
        else:
            logger.warning(
                f"TN patient creation failed at {result.failed_phase}: "
                f"{result.failure_reason} — {result.error_message}"
            )

        return result

    except Exception as e:
        logger.exception(f"TN create-patient endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/test-login")
async def test_login():
    """
    Login test using the full executor (Phase 0 + Phase 1 only).
    Returns structured TNExecutorOutput with per-phase logs.
    """
    try:
        logger.info("TN login test starting")

        from ..mcp_runtime import PlaywrightRuntime
        from ..tn_executor import TNExecutor
        from ..config import get_tn_credentials
        from shared.schemas.therapy_notes import TNPhase, TNExecutorOutput as Output

        import time

        # Fail fast on missing credentials
        try:
            credentials = get_tn_credentials()
        except Exception as e:
            logger.error(f"TN credential validation failed: {e}")
            return Output.failure(
                phase=TNPhase.ENTRY,
                reason="login_failed",
                message=(
                    "Missing TherapyNotes credentials. Required env vars: "
                    "THERAPYNOTES_PRACTICE_CODE, THERAPYNOTES_USERNAME, "
                    "THERAPYNOTES_PASSWORD"
                ),
                logs=[],
                duration_ms=0,
            )

        runtime = PlaywrightRuntime(skip_proxy=True, skip_resource_blocking=True, skip_stealth=True)
        executor = TNExecutor(runtime, credentials)
        executor._start_time = time.time()

        try:
            executor._page = await runtime.ensure_browser()

            # Phase 0: Entry
            entry_ok = await executor._phase_entry()
            if not entry_ok:
                return executor._build_failure_output()

            # Phase 1: Login
            login_ok = await executor._phase_login()
            if not login_ok:
                return executor._build_failure_output()

            # Both phases passed — return success
            return Output.success(
                patient_name="LOGIN_TEST",
                logs=executor._logs,
                duration_ms=executor._elapsed_ms(),
            )

        finally:
            await runtime.close()

    except Exception as e:
        logger.exception(f"TN login test failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/probe-step2")
async def probe_step2():
    """
    Navigate through practice code (step 1) and capture step 2 form elements.
    Does NOT attempt login. Use this to verify selectors for username/password.
    """
    import time
    import asyncio
    import base64
    steps = []
    start = time.time()

    def log_step(name, detail=""):
        elapsed = int((time.time() - start) * 1000)
        steps.append({"step": name, "elapsed_ms": elapsed, "detail": detail})

    try:
        from ..mcp_runtime import PlaywrightRuntime
        from ..config import get_tn_credentials

        credentials = get_tn_credentials()
        log_step("credentials_loaded")

        runtime = PlaywrightRuntime(skip_proxy=True, skip_resource_blocking=True, skip_stealth=True)
        try:
            page = await runtime.ensure_browser()
            log_step("browser_launched")

            await page.goto(
                "https://www.therapynotes.com/app/login/",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            log_step("navigated")

            # Wait for practice code field
            for _ in range(30):
                await asyncio.sleep(0.5)
                el = await page.query_selector("input#PracticeCode")
                if el and await el.is_visible():
                    break

            await page.fill("input#PracticeCode", credentials.practice_code)
            await page.click("button#Continue__ContinueButton")
            log_step("practice_code_submitted")

            # Wait for step 2 to render
            await asyncio.sleep(5)

            # Capture ALL form elements on step 2
            inputs = await page.evaluate("""
                () => {
                    const els = document.querySelectorAll('input, button, select, textarea, label');
                    return Array.from(els).map(el => ({
                        tag: el.tagName.toLowerCase(),
                        type: el.type || '',
                        name: el.name || '',
                        id: el.id || '',
                        placeholder: el.placeholder || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        forAttr: el.getAttribute('for') || '',
                        text: el.textContent?.trim().slice(0, 100) || '',
                        visible: el.offsetParent !== null,
                        className: el.className?.slice?.(0, 150) || '',
                    }));
                }
            """)
            log_step("step2_elements_captured", f"{len(inputs)} elements")

            # Capture visible text
            visible_text = await page.evaluate("""
                () => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null, false
                    );
                    const texts = [];
                    let node;
                    while (node = walker.nextNode()) {
                        const t = node.textContent.trim();
                        if (t.length > 2 && t.length < 200) texts.push(t);
                    }
                    return texts.slice(0, 50);
                }
            """)

            # Screenshot
            screenshot_bytes = await page.screenshot(full_page=True)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            return {
                "status": "success",
                "step2_url": page.url,
                "steps": steps,
                "step2_elements": inputs,
                "visible_texts": visible_text,
                "screenshot_base64": screenshot_b64,
            }

        finally:
            await runtime.close()

    except Exception as e:
        return {"status": "error", "error": str(e), "steps": steps}


@router.post("/probe-login-page")
async def probe_login_page():
    """
    Debug: Navigate to TN login page and return what the browser sees.

    Returns screenshot (base64), page title, URL, and visible input fields.
    Use this to map real selectors before testing login.
    """
    try:
        from ..mcp_runtime import PlaywrightRuntime
        import base64
        import asyncio

        runtime = PlaywrightRuntime(skip_proxy=True, skip_resource_blocking=True, skip_stealth=True)

        try:
            page = await runtime.ensure_browser()

            # Capture console logs, network errors, and ALL requests
            console_logs = []
            failed_requests = []
            all_requests = []

            page.on("console", lambda msg: console_logs.append(
                {"type": msg.type, "text": msg.text[:300]}
            ))
            page.on("requestfailed", lambda req: failed_requests.append(
                {"url": req.url[:200], "failure": req.failure or "unknown"}
            ))
            page.on("request", lambda req: all_requests.append(
                {"url": req.url[:200], "method": req.method, "type": req.resource_type}
            ))
            page.on("response", lambda res: all_requests.append(
                {"url": res.url[:200], "status": res.status, "type": "response"}
            ))

            # Navigate to login SPA — use "load" to let all resources finish
            await page.goto(
                "https://www.therapynotes.com/app/login/",
                wait_until="load",
                timeout=30000,
            )

            # Give SPA time to hydrate — wait up to 30s, checking every 2s
            spa_ready_at = None
            for wait_round in range(15):
                await asyncio.sleep(2)
                has_input = await page.evaluate(
                    "() => document.querySelectorAll('input').length"
                )
                if has_input > 0:
                    spa_ready_at = (wait_round + 1) * 2
                    logger.info(f"SPA hydrated after {spa_ready_at}s — found {has_input} inputs")
                    break

            # Check fingerprint AFTER navigation (init scripts have run)
            webdriver_check = await page.evaluate("""
                () => ({
                    webdriver: navigator.webdriver,
                    userAgent: navigator.userAgent,
                    plugins: navigator.plugins.length,
                    pluginNames: Array.from(navigator.plugins).map(p => p.name),
                    mimeTypes: navigator.mimeTypes.length,
                    languages: navigator.languages,
                    hardwareConcurrency: navigator.hardwareConcurrency,
                    platform: navigator.platform,
                    hasChrome: !!window.chrome,
                    hasChromeRuntime: !!(window.chrome && window.chrome.runtime),
                })
            """)

            # Capture current state
            title = await page.title()
            url = page.url
            screenshot_bytes = await page.screenshot(full_page=True)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            # Probe for all input elements
            inputs = await page.evaluate("""
                () => {
                    const els = document.querySelectorAll('input, button, select, textarea');
                    return Array.from(els).map(el => ({
                        tag: el.tagName.toLowerCase(),
                        type: el.type || '',
                        name: el.name || '',
                        id: el.id || '',
                        placeholder: el.placeholder || '',
                        ariaLabel: el.getAttribute('aria-label') || '',
                        text: el.textContent?.trim().slice(0, 80) || '',
                        visible: el.offsetParent !== null,
                        className: el.className?.slice?.(0, 100) || '',
                    }));
                }
            """)

            # Grab visible text blocks
            visible_text = await page.evaluate("""
                () => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null, false
                    );
                    const texts = [];
                    let node;
                    while (node = walker.nextNode()) {
                        const t = node.textContent.trim();
                        if (t.length > 2 && t.length < 200) texts.push(t);
                    }
                    return texts.slice(0, 50);
                }
            """)

            # Get outer HTML of body for structure analysis
            body_html = await page.evaluate(
                "() => document.body?.innerHTML?.slice(0, 3000) || 'empty'"
            )

            return {
                "url": url,
                "title": title,
                "spa_ready_at_seconds": spa_ready_at,
                "webdriver_check": webdriver_check,
                "inputs": inputs,
                "visible_texts": visible_text,
                "console_logs": console_logs[:30],
                "failed_requests": failed_requests[:20],
                "all_requests": all_requests[:50],
                "body_html_snippet": body_html,
                "screenshot_base64": screenshot_b64,
            }

        finally:
            await runtime.close()

    except Exception as e:
        logger.exception(f"TN probe failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/probe-after-login")
async def probe_after_login():
    """
    Full login probe: fills practice code, username, password, clicks Log In,
    then captures what the page looks like. Does NOT use the executor.
    """
    import time
    import asyncio
    import base64
    steps = []
    start = time.time()

    def log_step(name, detail=""):
        elapsed = int((time.time() - start) * 1000)
        steps.append({"step": name, "elapsed_ms": elapsed, "detail": detail})

    try:
        from ..mcp_runtime import PlaywrightRuntime
        from ..config import get_tn_credentials

        credentials = get_tn_credentials()
        log_step("credentials_loaded")

        runtime = PlaywrightRuntime(skip_proxy=True, skip_resource_blocking=True, skip_stealth=True)
        try:
            page = await runtime.ensure_browser()
            log_step("browser_launched")

            await page.goto(
                "https://www.therapynotes.com/app/login/",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            log_step("navigated")

            # Step 1: Fill practice code
            for _ in range(30):
                await asyncio.sleep(0.5)
                el = await page.query_selector("input#PracticeCode")
                if el and await el.is_visible():
                    break

            await page.fill("input#PracticeCode", credentials.practice_code)
            await page.click("button#Continue__ContinueButton")
            log_step("practice_code_submitted")

            # Wait for step 2
            for _ in range(20):
                await asyncio.sleep(0.5)
                el = await page.query_selector("input#Login__UsernameField")
                if el and await el.is_visible():
                    break
            log_step("step2_loaded")

            # Step 2: Fill credentials
            await page.fill("input#Login__UsernameField", credentials.username)
            log_step("username_filled", credentials.username)

            await page.fill("input#Login__Password", credentials.password)
            log_step("password_filled", "***")

            # Click Log In
            await page.click("button#Login__LogInButton")
            log_step("login_clicked")

            # Wait 10 seconds to see what happens
            await asyncio.sleep(10)
            log_step("waited_10s", page.url)

            # Capture everything on the resulting page
            inputs = await page.evaluate("""
                () => {
                    const els = document.querySelectorAll('input, button, select, textarea, [role="alert"], .alert, .error, .validation-summary-errors');
                    return Array.from(els).map(el => ({
                        tag: el.tagName.toLowerCase(),
                        type: el.type || '',
                        name: el.name || '',
                        id: el.id || '',
                        text: el.textContent?.trim().slice(0, 200) || '',
                        visible: el.offsetParent !== null,
                        className: el.className?.slice?.(0, 150) || '',
                        role: el.getAttribute('role') || '',
                    }));
                }
            """)

            visible_text = await page.evaluate("""
                () => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null, false
                    );
                    const texts = [];
                    let node;
                    while (node = walker.nextNode()) {
                        const t = node.textContent.trim();
                        if (t.length > 2 && t.length < 200) texts.push(t);
                    }
                    return texts.slice(0, 50);
                }
            """)

            screenshot_bytes = await page.screenshot(full_page=True)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            return {
                "status": "ok",
                "final_url": page.url,
                "steps": steps,
                "page_elements": inputs,
                "visible_texts": visible_text,
                "screenshot_base64": screenshot_b64,
            }

        finally:
            await runtime.close()

    except Exception as e:
        return {"status": "error", "error": str(e), "steps": steps}


@router.get("/test")
async def test_endpoint():
    """Health check for the TN executor route."""
    return {
        "status": "ok",
        "message": "TherapyNotes executor endpoint is active",
        "endpoints": [
            "POST /api/tn/create-patient",
            "POST /api/tn/test-login",
            "POST /api/tn/probe-login-page",
            "POST /api/tn/probe-step2",
        ],
    }
