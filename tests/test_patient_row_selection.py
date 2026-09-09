"""
Synthetic verification for patient-row selection in the appointment dialog.

WHY THE FIXTURE SHAPE MATTERS: the previous synthetic suite passed while the code
was broken, because its fixtures did not reproduce the real page. These build the
structure recon actually verified —

  div.ContentBubble.IncrementalSearch          <- ONE container per search
    div.ContentBubbleContent
      a.IncrementalSearchLink                  <- ONE per patient, href="#",
        span[data-testid] > span                  identical data-testid on all
        span.IncrementalSearchLinkDescription > span  ("DOB: m/d/yyyy")

— so a selector that targets the container instead of the rows FAILS here.
Asserted directly in case [H].

All names/dates below are synthetic. No real patient data appears in this file.

Run:  <venv>/bin/python -m tests.test_patient_row_selection
"""

import asyncio
import sys

from playwright.async_api import async_playwright

from services.api.tn_executor_v2 import TNExecutorV2, _normalize_dob
from shared.schemas.therapy_notes_v2 import TNPhaseV2


# ---------------------------------------------------------------------------
# Fixture builder — the real container/rows shape
# ---------------------------------------------------------------------------

def row(name: str, dob: str) -> str:
    return (
        '<a tabindex="-1" class="IncrementalSearchLink" '
        'data-testid="incremental-search-link" href="#">'
        '<span data-testid="incremental-search-link-match-container">'
        f'<span data-testid="incremental-search-link-match-text">{name}</span></span>'
        '<span data-testid="incremental-search-link-match-container" '
        'class="IncrementalSearchLinkDescription" style="font-size:11px">'
        f'<span data-testid="incremental-search-link-match-text">DOB: {dob}</span></span>'
        "</a>"
    )


def page_html(rows_html: str, with_container: bool = True) -> str:
    inner = (
        '<div class="ContentBubble IncrementalSearch" id="bubble1" style="">'
        f'<div class="ContentBubbleContent" data-rum-description="x">{rows_html}</div>'
        "</div>"
    ) if with_container else ""
    return f"""
    <html><body>
      <input id="CalendarEntryEditor__PatientSelect">
      <span class="IncrementalSearchContainerNode">{inner}</span>
    </body></html>
    """


# A realistic common-surname set. Synthetic names only.
def surname_block(n, surname="Testsurname", dob="01/02/1990", start=0):
    return "".join(row(f"Given{i} {surname}", dob) for i in range(start, start + n))


class FakePatient:
    def __init__(self, first, last, dob):
        self.first_name, self.last_name, self.dob = first, last, dob


class Results:
    def __init__(self):
        self.passed = self.failed = 0
        self.lines = []

    def check(self, name, cond, detail=""):
        detail = "" if detail == "" else str(detail)
        if cond:
            self.passed += 1
            print(f"  ok   {name}")
        else:
            self.failed += 1
            self.lines.append(f"{name}{' — ' + detail if detail else ''}")
            print(f"  FAIL {name}{' — ' + detail if detail else ''}")


def make_executor(page):
    import time
    ex = object.__new__(TNExecutorV2)
    ex._page = page
    ex._logs = []
    ex._surfaced_overlays = []
    ex._overlays_reported = set()
    ex._start_time = time.time()
    ex._pending_failure = {}

    async def _no_shot(_l):
        return None
    ex._capture_screenshot = _no_shot
    return ex


async def attempt(browser, rows_html, patient, with_container=True):
    page = await browser.new_page(viewport={"width": 1200, "height": 900})
    await page.set_content(page_html(rows_html, with_container))
    # Instrument clicks so we can prove WHICH element was clicked.
    await page.evaluate("""() => {
      window.__clicked = [];
      document.addEventListener('click', (e) => {
        const t = e.target.closest('a.IncrementalSearchLink, div.ContentBubble') || e.target;
        window.__clicked.push({
          tag: t.tagName.toLowerCase(),
          cls: (typeof t.className === 'string' ? t.className : ''),
          idx: t.tagName.toLowerCase() === 'a'
            ? Array.from(document.querySelectorAll('a.IncrementalSearchLink')).indexOf(t) : -1,
        });
      }, true);
    }""")
    ex = make_executor(page)
    ok = await ex._select_patient_row(patient, TNPhaseV2.SCHEDULE_APPOINTMENT, 0.0)
    clicked = await page.evaluate("() => window.__clicked")
    return ex, page, ok, clicked


async def run():
    r = Results()
    TARGET_DOB = "03/04/1985"
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # ---------------------------------------------------------------
        print("\n[A] 15 rows, one matching name AND date of birth -> that row selected")
        # 14 same-surname decoys + our target. Keep total under the cap-suspect
        # threshold by using 13 decoys + 1 target = 14.
        rows_html = surname_block(13) + row("Target Person", TARGET_DOB)
        ex, page, ok, clicked = await attempt(
            browser, rows_html, FakePatient("Target", "Person", TARGET_DOB))
        r.check("selection succeeded", ok is True, ex._pending_failure.get("message", "")[:140])
        r.check("clicked an <a> ROW, not the container",
                bool(clicked) and clicked[0]["tag"] == "a"
                and "IncrementalSearchLink" in clicked[0]["cls"], clicked)
        r.check("clicked the LAST row (index 13), i.e. not the first",
                bool(clicked) and clicked[0]["idx"] == 13, clicked)
        await page.close()

        # ---------------------------------------------------------------
        print("\n[B] Two rows match name AND date of birth -> FAILS, count reported")
        rows_html = (surname_block(10) + row("Same Person", TARGET_DOB)
                     + row("Same Person", TARGET_DOB))
        ex, page, ok, clicked = await attempt(
            browser, rows_html, FakePatient("Same", "Person", TARGET_DOB))
        msg = ex._pending_failure.get("message", "")
        r.check("failed rather than guessing", ok is False)
        r.check("nothing was clicked", clicked == [], clicked)
        r.check("reports HOW MANY were indistinguishable", "2 patients" in msg, msg[:180])
        r.check("says it is refusing to guess", "Refusing to guess" in msg, msg[:180])
        await page.close()

        # ---------------------------------------------------------------
        print("\n[C] Name matches several rows, dates differ -> the right one selected")
        rows_html = (row("Ambi Person", "01/01/1970") + row("Ambi Person", TARGET_DOB)
                     + row("Ambi Person", "12/31/1999") + surname_block(5))
        ex, page, ok, clicked = await attempt(
            browser, rows_html, FakePatient("Ambi", "Person", TARGET_DOB))
        r.check("selection succeeded on the DOB discriminator", ok is True,
                ex._pending_failure.get("message", "")[:140])
        r.check("picked index 1 (the matching DOB), not index 0",
                bool(clicked) and clicked[0]["idx"] == 1, clicked)
        await page.close()

        # ---------------------------------------------------------------
        print("\n[D] One row, date of birth mismatched -> FAILS")
        ex, page, ok, clicked = await attempt(
            browser, row("Only Person", "09/09/1991"),
            FakePatient("Only", "Person", TARGET_DOB))
        msg = ex._pending_failure.get("message", "")
        r.check("failed", ok is False)
        r.check("nothing clicked", clicked == [], clicked)
        r.check("says the name matched but the date did not",
                "matched the name" in msg and "date of birth" in msg, msg[:200])
        r.check("counts the mismatch", "1 differed" in msg, msg[:200])
        await page.close()

        # ---------------------------------------------------------------
        print("\n[E] Zero rows -> FAILS with the no-match reason")
        ex, page, ok, clicked = await attempt(
            browser, "", FakePatient("Nobody", "Here", TARGET_DOB))
        msg = ex._pending_failure.get("message", "")
        r.check("failed", ok is False)
        r.check("reason names an empty result set", "no selectable rows" in msg, msg[:180])
        await page.close()

        # ---------------------------------------------------------------
        print("\n[F] Container present but EMPTY -> FAILS (not a false success)")
        page = await browser.new_page(viewport={"width": 1200, "height": 900})
        await page.set_content(page_html("", with_container=True))
        ex = make_executor(page)
        r.check("row-presence poll reports NO rows",
                await ex._patient_result_rows_present() is False)
        ok = await ex._select_patient_row(
            FakePatient("Any", "Body", TARGET_DOB), TNPhaseV2.SCHEDULE_APPOINTMENT, 0.0)
        r.check("selection failed", ok is False)
        r.check("reason names an empty result set",
                "no selectable rows" in ex._pending_failure.get("message", ""))
        await page.close()

        # ---------------------------------------------------------------
        print("\n[G] Unexpected date format -> FAILS, never a wrong selection")
        for label, dob_text in [("ISO yyyy-mm-dd", "1985-03-04"),
                                ("written month", "March 4, 1985"),
                                ("empty date", "")]:
            ex, page, ok, clicked = await attempt(
                browser, row("Odd Person", dob_text),
                FakePatient("Odd", "Person", TARGET_DOB))
            r.check(f"{label}: failed", ok is False)
            r.check(f"{label}: nothing clicked", clicked == [], clicked)
            r.check(f"{label}: counted as unreadable, not matched",
                    "had no readable date" in ex._pending_failure.get("message", ""),
                    ex._pending_failure.get("message", "")[:180])
            await page.close()

        # ---------------------------------------------------------------
        print("\n[H] REGRESSION — the fixture reproduces container+rows, so the OLD "
              "selector would have matched the container")
        page = await browser.new_page(viewport={"width": 1200, "height": 900})
        await page.set_content(page_html(surname_block(15)))
        containers = await page.locator(".ContentBubble.IncrementalSearch").count()
        rows_n = await page.locator(".ContentBubble.IncrementalSearch a.IncrementalSearchLink").count()
        r.check("15 patients render exactly ONE container (old selector's blind spot)",
                containers == 1, containers)
        r.check("...and 15 rows under the new selector", rows_n == 15, rows_n)
        ex = make_executor(page)
        r.check("new row locator sees 15", await (await ex._patient_result_rows()).count() == 15)
        await page.close()

        # ---------------------------------------------------------------
        print("\n[I] Possibly-truncated result set (>= cap) -> FAILS rather than selecting")
        rows_html = surname_block(14) + row("Target Person", TARGET_DOB)   # 15 total
        ex, page, ok, clicked = await attempt(
            browser, rows_html, FakePatient("Target", "Person", TARGET_DOB))
        msg = ex._pending_failure.get("message", "")
        r.check("refused despite a unique match being present", ok is False, msg[:160])
        r.check("nothing clicked", clicked == [], clicked)
        r.check("explains the truncation risk", "truncating" in msg, msg[:200])
        r.check("names the threshold", "15" in msg, msg[:200])
        await page.close()

        # ---------------------------------------------------------------
        print("\n[J] Multi-match path is REACHABLE (the old warning never was)")
        # Two identical rows is the case the dead helper could never see, because
        # N patients produced exactly one container.
        page = await browser.new_page(viewport={"width": 1200, "height": 900})
        await page.set_content(page_html(row("Dup Person", TARGET_DOB) * 2))
        r.check("one container for two patients (why the old guard was dead)",
                await page.locator(".ContentBubble.IncrementalSearch").count() == 1)
        ex = make_executor(page)
        ok = await ex._select_patient_row(
            FakePatient("Dup", "Person", TARGET_DOB), TNPhaseV2.SCHEDULE_APPOINTMENT, 0.0)
        r.check("the ambiguity path fired and FAILED", ok is False)
        r.check("it is a failure, not a warning",
                ex._pending_failure.get("reason") == "appointment_creation_failed",
                str(ex._pending_failure.get("reason")))
        await page.close()

        # ---------------------------------------------------------------
        print("\n[K] No patient value in any recorded message")
        SENT_NAME, SENT_DOB = "Zzsentinelname", "07/07/1977"
        ex, page, ok, clicked = await attempt(
            browser, row(f"{SENT_NAME} Surname", SENT_DOB) + row(f"{SENT_NAME} Surname", SENT_DOB),
            FakePatient(SENT_NAME, "Surname", SENT_DOB))
        blob = " ".join([ex._pending_failure.get("message", "")]
                        + [l.message for l in ex._logs])
        r.check("failure message contains no name", SENT_NAME not in blob, blob[:200])
        r.check("failure message contains no date of birth", SENT_DOB not in blob, blob[:200])
        await page.close()

        # ---------------------------------------------------------------
        print("\n[L] _normalize_dob")
        for raw, want in [("DOB: 3/4/1985", "03/04/1985"), ("DOB: 03/04/1985", "03/04/1985"),
                          ("3/4/1985", "03/04/1985"), ("12/31/1999", "12/31/1999")]:
            r.check(f"{raw!r} -> {want}", _normalize_dob(raw) == want, _normalize_dob(raw))
        for raw in ["1985-03-04", "March 4, 1985", "", None, "no date here"]:
            r.check(f"{raw!r} -> None", _normalize_dob(raw) is None, _normalize_dob(raw))

        await browser.close()

    print(f"\n{'PASS' if r.failed == 0 else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    if r.failed:
        print("\nFailures:")
        for line in r.lines:
            print(f"  - {line}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
