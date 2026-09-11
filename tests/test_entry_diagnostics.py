"""
Entry-phase diagnostics: an entry failure must explain itself.

On 11 September four runs failed at Phase 0 and the logs said only that a field
had not rendered — no URL, no title, no page text. _phase_login had logged its
URL and title all along. These fixtures pin the closed asymmetry AND pin that a
successful entry is byte-for-byte as chatty as before.

Observability only: no selector, poll count or timeout is exercised or changed
here, and the success path must emit no new lines.

Run:  <venv>/bin/python -m tests.test_entry_diagnostics
"""

import asyncio
import logging
import sys
import time

from playwright.async_api import async_playwright

from services.api.tn_executor_v2 import TNExecutorV2
from services.api.tn_executor import TNExecutor


# --- fixture pages ---------------------------------------------------------

def blocked_page(title="Access Denied", text="Request blocked. Reference ID 1234."):
    t = f"<title>{title}</title>" if title is not None else ""
    return f"<html><head>{t}</head><body><h1>{text}</h1></body></html>"

NO_TITLE = "<html><body><h1>Scheduled maintenance. Back soon.</h1></body></html>"
NO_TEXT = "<html><head><title>Nothing</title></head><body><script>var x=1;</script></body></html>"
LOGIN_OK = """<html><head><title>Log In | TherapyNotes</title></head><body>
  <form><input id="PracticeCode" type="text"><button id="Continue__ContinueButton">Continue</button></form>
</body></html>"""


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(); self.lines = []
    def emit(self, r):
        try: self.lines.append(r.getMessage())
        except Exception: self.lines.append("<?>")


class Results:
    def __init__(self): self.passed = self.failed = 0; self.lines = []
    def check(self, name, cond, detail=""):
        detail = "" if detail == "" else str(detail)
        if cond:
            self.passed += 1; print(f"  ok   {name}")
        else:
            self.failed += 1; self.lines.append(f"{name} — {detail}"); print(f"  FAIL {name} — {detail}")


def make_ex(page, cls=TNExecutorV2):
    ex = object.__new__(cls)
    ex._page = page
    ex._logs = []
    ex._surfaced_overlays = []
    ex._overlays_reported = set()
    ex._save_problem_text = None
    ex._start_time = time.time()
    ex._pending_failure = {}
    return ex


async def diag(browser, html, cls=TNExecutorV2, cap=None):
    page = await browser.new_page(viewport={"width": 1200, "height": 900})
    await page.set_content(html)
    ex = make_ex(page, cls)
    if cap is not None:
        cap.lines.clear()
    await ex._log_entry_page_state("test reason")
    return ex, page


async def main():
    r = Results()
    cap = _Cap(); root = logging.getLogger(); root.addHandler(cap); prev = root.level
    root.setLevel(logging.DEBUG)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()

            # ---------------------------------------------------------
            print("\n[A] No text inputs, with a title and visible text -> both logged")
            ex, page = await diag(browser, blocked_page(), cap=cap)
            joined = " | ".join(cap.lines)
            r.check("logs the landed URL", "landed on" in joined, joined[:160])
            r.check("logs the page title", 'title: "Access Denied"' in joined, joined[:160])
            r.check("logs the page text", "Request blocked" in joined, joined[:200])
            r.check("logs an element census", "Element census" in joined, joined[:200])
            r.check("census reports ZERO text inputs (the 11 Sept signature)",
                    "'textInputs': 0" in joined or '"textInputs": 0' in joined, joined[:240])
            r.check("carries the reason it was called with", "test reason" in joined)
            await page.close()

            # ---------------------------------------------------------
            print("\n[B] No title -> degrades cleanly, still logs the rest")
            ex, page = await diag(browser, NO_TITLE, cap=cap)
            joined = " | ".join(cap.lines)
            r.check("did not raise", True)
            r.check("still logs URL", "landed on" in joined)
            r.check("empty title renders as empty, not a crash", 'title: ""' in joined, joined[:160])
            r.check("still logs the text", "Scheduled maintenance" in joined, joined[:200])
            await page.close()

            # ---------------------------------------------------------
            print("\n[C] No visible text at all -> logged AS SUCH (itself a finding)")
            ex, page = await diag(browser, NO_TEXT, cap=cap)
            joined = " | ".join(cap.lines)
            r.check("says the page rendered no visible text",
                    "rendered NO visible text" in joined, joined[:200])
            r.check("still logs URL and title", "landed on" in joined and "title:" in joined)
            await page.close()

            # ---------------------------------------------------------
            print("\n[D] Bounded: a huge page is truncated, not dumped")
            CAP = TNExecutorV2.ENTRY_PAGE_TEXT_MAX_CHARS
            huge = "<html><head><title>Big</title></head><body><p>" + ("A" * 20000) + "</p></body></html>"
            ex, page = await diag(browser, huge, cap=cap)
            textline = [l for l in cap.lines if "Page text (" in l]
            r.check("text line emitted", bool(textline), cap.lines[-3:])
            r.check(f"snippet bounded to {CAP} chars", len(textline[0]) < CAP + 200, len(textline[0]))
            r.check("marks truncation with an ellipsis", "…" in textline[0])
            r.check("reports the FULL length so the truncation is visible",
                    "20000 chars" in textline[0], textline[0][:80])
            await page.close()

            # ---------------------------------------------------------
            print("\n[E] Never raises, even with a dead page object")
            class DeadPage:
                @property
                def url(self): raise RuntimeError("gone")
                async def title(self): raise RuntimeError("gone")
                async def evaluate(self, *a, **k): raise RuntimeError("gone")
            ex = make_ex(DeadPage())
            cap.lines.clear()
            try:
                await ex._log_entry_page_state("dead page")
                raised = None
            except Exception as e:
                raised = e
            r.check("no exception escapes", raised is None, str(raised))
            joined = " | ".join(cap.lines)
            r.check("reports url/title unreadable", "<unreadable>" in joined, joined[:160])
            r.check("reports text unreadable", "could not be read" in joined, joined[:200])

            # ---------------------------------------------------------
            print("\n[F] A NORMAL login page logs NOTHING new (success path untouched)")
            page = await browser.new_page(viewport={"width": 1200, "height": 900})
            await page.set_content(LOGIN_OK)
            ex = make_ex(page)
            cap.lines.clear()
            visible = await ex._check_practice_code_visible()
            r.check("the practice-code field is found, as before", visible is True)
            r.check("NO [ENTRY] diagnostic line emitted on the success path",
                    not any("[ENTRY]" in l for l in cap.lines),
                    [l for l in cap.lines if "[ENTRY]" in l])
            r.check("no log lines at all from the check", cap.lines == [], cap.lines[:3])
            await page.close()

            # ---------------------------------------------------------
            print("\n[G] V1 parity")
            ex, page = await diag(browser, blocked_page(), TNExecutor, cap=cap)
            joined = " | ".join(cap.lines)
            r.check("[V1] logs URL, title, text and census",
                    all(k in joined for k in ("landed on", "title:", "Request blocked", "Element census")),
                    joined[:200])
            r.check("[V1] same bound constant",
                    TNExecutor.ENTRY_PAGE_TEXT_MAX_CHARS == TNExecutorV2.ENTRY_PAGE_TEXT_MAX_CHARS == 400)
            await page.close()

            await browser.close()
    finally:
        root.removeHandler(cap); root.setLevel(prev)

    print(f"\n{'PASS' if r.failed == 0 else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    if r.failed:
        print("\nFailures:"); [print(f"  - {l}") for l in r.lines]
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
