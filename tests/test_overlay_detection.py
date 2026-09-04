"""
Synthetic reproduction of the 4 September TherapyNotes overlay failure.

WHY SYNTHETIC: the TherapyNotes broadcast that caused the incident was dismissed
by hand before this fix was written, so the real overlay can no longer be
observed and MUST NOT be reproduced by driving the live EHR. These pages
reconstruct the shape of the failure from the only hard evidence available —
Playwright's own interception log:

    <h2>Important Message</h2> from <div id="ElementDropbox">…</div>
    subtree intercepts pointer events

...while locator("#ElementDropbox").is_visible() returned False, which is what
made every guard in the agent bail.

Because the exact reason the container reported not-visible is unknown, each
plausible reason is built here as its own variant. The fix must hold for all of
them, not for the one we happen to think is likeliest.

Run:  <venv>/bin/python -m tests.test_overlay_detection
"""

import asyncio
import sys

from playwright.async_api import async_playwright

from services.api.tn_executor_v2 import TNExecutorV2, OverlayBlockedError
from services.api.tn_executor import TNExecutor, OverlayBlockedError as V1OverlayBlockedError


# ---------------------------------------------------------------------------
# Page fixtures
# ---------------------------------------------------------------------------

BASE_CSS = """
  body { margin: 0; font: 14px system-ui; }
  #target { position: absolute; top: 300px; left: 60px; width: 220px; height: 44px; }
"""

def page_html(overlay_style: str, overlay_inner: str, extra_css: str = "") -> str:
    """A page with a New-Patient-style button and an overlay on top of it."""
    return f"""
    <html><head><style>
      {BASE_CSS}
      {extra_css}
    </style></head>
    <body>
      <h1>Patients</h1>
      <input id="target" type="submit" value="+ New Patient"
             onclick="document.title='CLICKED'">
      <div id="ElementDropbox" style="{overlay_style}">{overlay_inner}</div>
    </body></html>
    """

ANNOUNCEMENT = """
  <div class="panel" style="position:fixed; inset:0; background:#fff;
       display:flex; align-items:center; justify-content:center; flex-direction:column;">
    <h2>Important Message</h2>
    <p>Scheduled maintenance this weekend. Billing exports may be delayed.</p>
    <button id="ack" onclick="document.getElementById('ElementDropbox').remove()">OK</button>
  </div>
"""

ANNOUNCEMENT_NO_CONTROL = ANNOUNCEMENT[:ANNOUNCEMENT.index("<button")] + ANNOUNCEMENT[ANNOUNCEMENT.index("</button>") + len("</button>"):]

DECISION_DIALOG = """
  <div class="panel" style="position:fixed; inset:0; background:#fff;
       display:flex; align-items:center; justify-content:center; flex-direction:column;">
    <h2>Possible Duplicate</h2>
    <p>A patient with this name already exists. Create anyway?</p>
    <button id="anyway">Create Patient Anyway</button>
    <button id="cancel">Cancel</button>
  </div>
"""

# Each variant is a different reason the CONTAINER can report not-visible while
# its subtree paints over the page.
# (style, inner, extra_css, container_reports_invisible)
VARIANTS = {
    # The incident's own shape: a 0x0 portal mount whose fixed child paints.
    "zero-size container":
        ("width:0; height:0; overflow:visible;", ANNOUNCEMENT, "", True),
    # Container has no box of its own at all.
    "display:contents container":
        ("display:contents;", ANNOUNCEMENT, "", False),
    # Container parked off-viewport; the child is fixed and onscreen.
    "offscreen container":
        ("position:absolute; top:-9999px; left:-9999px;", ANNOUNCEMENT, "", True),
    # Container clipped to a 1px box; the fixed child escapes overflow clipping.
    # (Playwright still calls a 1x1 box "visible", so this one does NOT reproduce
    # the not-visible premise — it is kept because the SHAPE is what matters: the
    # container tells you nothing useful about what its subtree is covering.)
    "clipped container":
        ("position:absolute; width:1px; height:1px; overflow:hidden;", ANNOUNCEMENT, "", False),
    # Invisible but still swallowing clicks — paints nothing, blocks everything.
    "transparent (opacity:0) overlay":
        ("width:0; height:0;", ANNOUNCEMENT.replace("background:#fff", "background:#fff; opacity:0"), "", True),
    # The ordinary case, for regression: a plainly visible overlay.
    "plainly visible overlay":
        ("", ANNOUNCEMENT, "", False),
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class Results:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.lines = []

    def check(self, name, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"    ok   {name}")
        else:
            self.failed += 1
            self.lines.append(f"{name}{' — ' + detail if detail else ''}")
            print(f"    FAIL {name}{' — ' + detail if detail else ''}")


def make_executor(page, cls=TNExecutorV2):
    """
    An executor with only what the click path touches. Bypasses __init__ so the
    test needs no credentials, no config and no network.
    """
    ex = object.__new__(cls)
    ex._page = page
    ex._surfaced_overlays = []
    ex._overlays_reported = set()
    return ex


async def run():
    r = Results()

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # -------------------------------------------------------------------
        print("\n[A] Overlay variants — detect, surface, dismiss, click through")
        for name, (style, inner, extra, expect_invisible) in VARIANTS.items():
            print(f"\n  variant: {name}")
            page = await browser.new_page(viewport={"width": 900, "height": 700})
            await page.set_content(page_html(style, inner, extra))
            ex = make_executor(page)
            target = page.locator("#target")

            # The premise: the container reports not-visible, exactly as on 4 Sept.
            container_visible = await page.locator("#ElementDropbox").is_visible()
            if expect_invisible:
                # The 4 Sept premise: the guard that used to gate everything here
                # would have returned False on this container.
                r.check(f"[{name}] premise: container reports NOT visible",
                        container_visible is False,
                        f"is_visible()={container_visible}")
            else:
                print(f"      (container reports visible={container_visible}; "
                      f"the fix must not care either way)")

            # 1b: hit-test finds the blocker regardless of markup.
            blocker = await ex._find_blocking_overlay(target)
            r.check(f"[{name}] hit-test finds a blocker", blocker is not None)

            # The whole click path.
            try:
                await ex._safe_click(target, "New Patient button")
                clicked = await page.title() == "CLICKED"
            except OverlayBlockedError as e:
                clicked = False
                print(f"      (raised OverlayBlockedError: {str(e)[:80]}...)")

            r.check(f"[{name}] click lands after dismissal", clicked)
            r.check(f"[{name}] overlay text surfaced to run output",
                    any("Important Message" in s for s in ex._surfaced_overlays),
                    f"surfaced={ex._surfaced_overlays}")
            await page.close()

        # -------------------------------------------------------------------
        print("\n[B] Blocker with NO dismiss control — must fail, and say why")
        page = await browser.new_page(viewport={"width": 900, "height": 700})
        await page.set_content(page_html("width:0; height:0;", ANNOUNCEMENT_NO_CONTROL))
        ex = make_executor(page)
        raised = None
        try:
            await ex._safe_click(page.locator("#target"), "New Patient button")
        except OverlayBlockedError as e:
            raised = e
        except Exception as e:  # noqa: BLE001
            raised = e
        r.check("raises OverlayBlockedError (not a bare timeout)",
                isinstance(raised, OverlayBlockedError), f"got {type(raised).__name__}")
        msg = str(raised or "")
        r.check("message says it was blocked by an overlay", "blocked by an overlay" in msg)
        r.check("message quotes the overlay's own words", "Important Message" in msg, msg[:120])
        r.check("does NOT claim the form/button was missing", "not_found" not in msg)
        r.check("overlay text still surfaced for the failure path",
                any("Important Message" in s for s in ex._surfaced_overlays))
        await page.close()

        # -------------------------------------------------------------------
        print("\n[C] Consequential-label guard — a duplicate-patient warning")
        page = await browser.new_page(viewport={"width": 900, "height": 700})
        await page.set_content(page_html("width:0; height:0;", DECISION_DIALOG))
        ex = make_executor(page)
        dismissed = await ex._clear_overlay_blocking(page.locator("#target"), "New Patient button")
        r.check("refuses to dismiss a decision dialog", dismissed is False)
        r.check("'Create Patient Anyway' was NOT clicked",
                await page.title() != "CLICKED-ANYWAY")
        still_there = await page.locator("#anyway").count() > 0
        r.check("the dialog is left standing for a human", still_there)
        r.check("decision-dialog text is NOT copied into run output (it can name a patient)",
                ex._surfaced_overlays == [], f"surfaced={ex._surfaced_overlays}")
        await page.close()

        # -------------------------------------------------------------------
        print("\n[D] Label classification")
        consequential = [
            "Create Appointment Anyway", "Create Patient Anyway", "Save Anyway",
            "Yes", "No", "Confirm", "Delete", "Discard changes", "Merge records",
            "Overwrite", "Cancel", "Submit", "Sign", "I Agree", "Accept",
            "Proceed", "Override", "Replace", "Update", "Schedule", "Send",
        ]
        for lab in consequential:
            r.check(f"consequential: {lab!r}", ex._label_is_consequential(lab) is not None)
            r.check(f"never treated as ack: {lab!r}", ex._label_is_acknowledgement(lab) is False)

        acks = ["OK", "Okay", "Got it", "Acknowledge", "Close", "Dismiss",
                "Continue", "Understood", "Done"]
        for lab in acks:
            r.check(f"acknowledgement: {lab!r}", ex._label_is_acknowledgement(lab) is True)

        # Word boundaries, not substrings.
        r.check("'Notice' does not trip the 'no' token",
                ex._label_is_consequential("Notice") is None)
        r.check("'Nothing to report' does not trip 'no'",
                ex._label_is_consequential("Nothing to report") is None)
        r.check("a long paragraph is never an ack label",
                ex._label_is_acknowledgement("OK " + "x" * 60) is False)

        # -------------------------------------------------------------------
        print("\n[E] The deliberate confirmation elsewhere in the flow is unaffected")
        # "Create Appointment Anyway" is clicked ON PURPOSE by _phase_schedule via
        # a direct _safe_click on that button. The generic guard must not touch it,
        # and must not stop the agent from clicking it itself.
        page = await browser.new_page(viewport={"width": 900, "height": 700})
        await page.set_content("""
          <html><body>
            <div class="Dialog" style="position:fixed; inset:0; background:#fff;
                 display:flex; align-items:center; justify-content:center;">
              <div>
                <p>This appointment overlaps another.</p>
                <button id="anyway" onclick="document.title='ANYWAY-CLICKED'">Create Appointment Anyway</button>
              </div>
            </div>
          </body></html>
        """)
        ex = make_executor(page)
        await ex._safe_click(page.locator("#anyway"), "Create Appointment Anyway")
        r.check("the agent can still click it deliberately",
                await page.title() == "ANYWAY-CLICKED")
        await page.close()

        # ...including when it is wrapped in a .Dialog, which the pre-existing
        # selector list matched blindly ('#ElementDropbox .Dialog button').
        page = await browser.new_page(viewport={"width": 900, "height": 700})
        await page.set_content("""
          <html><body>
            <div id="ElementDropbox">
              <div class="Dialog" style="position:fixed; inset:0; background:#fff;">
                <p>A patient with this name already exists.</p>
                <button id="anyway" onclick="document.title='ANYWAY-CLICKED'">Create Patient Anyway</button>
              </div>
            </div>
          </body></html>
        """)
        ex = make_executor(page)
        await ex._dismiss_blocking_dialogs()
        r.check("the .Dialog catch-all selector does NOT press it either",
                await page.title() != "ANYWAY-CLICKED")
        await page.close()

        # A generic dismissal sweep must not press it by itself.
        page = await browser.new_page(viewport={"width": 900, "height": 700})
        await page.set_content("""
          <html><body>
            <div id="ElementDropbox">
              <div class="panel" style="position:fixed; inset:0; background:#fff;">
                <p>This appointment overlaps another.</p>
                <button id="anyway" onclick="document.title='ANYWAY-CLICKED'">Create Appointment Anyway</button>
              </div>
            </div>
          </body></html>
        """)
        ex = make_executor(page)
        await ex._dismiss_blocking_dialogs()
        r.check("a blind dismissal sweep does NOT press it",
                await page.title() != "ANYWAY-CLICKED")
        await page.close()

        # -------------------------------------------------------------------
        print("\n[F] No overlay present — normal runs are untouched")
        page = await browser.new_page(viewport={"width": 900, "height": 700})
        # TN keeps #ElementDropbox mounted and empty when there is no message.
        await page.set_content("""
          <html><head><style>%s</style></head><body>
            <h1>Patients</h1>
            <input id="target" type="submit" value="+ New Patient" onclick="document.title='CLICKED'">
            <div id="ElementDropbox"></div>
          </body></html>
        """ % BASE_CSS)
        ex = make_executor(page)
        r.check("empty mount container is not treated as a blocker",
                await ex._find_blocking_overlay(page.locator("#target")) is None)
        r.check("speculative probe of the empty mount does nothing",
                await ex._handle_element_dropbox_overlay() is False)
        await ex._safe_click(page.locator("#target"), "New Patient button")
        r.check("click still lands", await page.title() == "CLICKED")
        r.check("nothing surfaced on a clean run", ex._surfaced_overlays == [])
        await page.close()

        # -------------------------------------------------------------------
        print("\n[G] 1c — the blocker parsed out of Playwright's own error")
        parse = TNExecutorV2._selector_from_interception_error
        real_error = (
            "ElementHandle.click: Timeout 15000ms exceeded.\nCall log:\n"
            '  - <h2>Important Message</h2> from <div id="ElementDropbox">…</div> '
            "subtree intercepts pointer events\n"
        )
        r.check("extracts #ElementDropbox from the real 4 Sept error",
                parse(real_error) == "#ElementDropbox", str(parse(real_error)))
        r.check("falls back to a class when there is no id",
                parse('x from <div class="Dialog modal">…</div> subtree intercepts pointer events')
                == "div.Dialog")
        r.check("returns None for an unrelated error",
                parse("Timeout 15000ms exceeded waiting for selector") is None)

        # -------------------------------------------------------------------
        # V1 carries a byte-identical copy of these helpers (the two executors are
        # deliberate parallel clones), so the same defect lived there too. Prove
        # the mirror actually works rather than assuming the copy was faithful.
        print("\n[H] V1 executor parity — the same fix, mirrored")
        for name in ("zero-size container", "transparent (opacity:0) overlay"):
            style, inner, extra, _ = VARIANTS[name]
            page = await browser.new_page(viewport={"width": 900, "height": 700})
            await page.set_content(page_html(style, inner, extra))
            ex1 = make_executor(page, TNExecutor)
            target = page.locator("#target")
            r.check(f"[V1/{name}] hit-test finds a blocker",
                    await ex1._find_blocking_overlay(target) is not None)
            await ex1._safe_click(target, "New Patient button")
            r.check(f"[V1/{name}] click lands after dismissal",
                    await page.title() == "CLICKED")
            r.check(f"[V1/{name}] overlay text surfaced",
                    any("Important Message" in t for t in ex1._surfaced_overlays))
            await page.close()

        # V1 must also refuse a decision dialog.
        page = await browser.new_page(viewport={"width": 900, "height": 700})
        await page.set_content(page_html("width:0; height:0;", DECISION_DIALOG))
        ex1 = make_executor(page, TNExecutor)
        r.check("[V1] refuses to dismiss a decision dialog",
                await ex1._clear_overlay_blocking(page.locator("#target"), "New Patient button") is False)
        r.check("[V1] decision-dialog text not copied into run output",
                ex1._surfaced_overlays == [])
        await page.close()

        # V1 must raise the same typed error, not a bare timeout.
        page = await browser.new_page(viewport={"width": 900, "height": 700})
        await page.set_content(page_html("width:0; height:0;", ANNOUNCEMENT_NO_CONTROL))
        ex1 = make_executor(page, TNExecutor)
        v1_raised = None
        try:
            await ex1._safe_click(page.locator("#target"), "New Patient button")
        except Exception as e:  # noqa: BLE001
            v1_raised = e
        r.check("[V1] raises OverlayBlockedError",
                isinstance(v1_raised, V1OverlayBlockedError), f"got {type(v1_raised).__name__}")
        r.check("[V1] message names the overlay, not a missing form",
                "blocked by an overlay" in str(v1_raised) and "not_found" not in str(v1_raised))
        await page.close()

        r.check("[V1] parses the blocker from Playwright's error",
                TNExecutor._selector_from_interception_error(real_error) == "#ElementDropbox")

        await browser.close()

    print(f"\n{'PASS' if r.failed == 0 else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    if r.failed:
        print("\nFailures:")
        for line in r.lines:
            print(f"  - {line}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
