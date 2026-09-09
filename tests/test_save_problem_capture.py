"""
Synthetic verification for capturing TherapyNotes' refusal block.

Fixtures reproduce what the production logs actually captured: a block headed
"Problems With Your Entry", inside a container from the duplicate probe's set
(that set is where all three real captures came from — 4 Sep 20:21, 8 Sep 18:33,
8 Sep 19:44 — and never from the Step-4 validation probe).

Every name/value below is synthetic.

Run:  <venv>/bin/python -m tests.test_save_problem_capture
"""

import asyncio
import sys

from playwright.async_api import async_playwright

from services.api.tn_executor_v2 import TNExecutorV2
from services.api.tn_executor import TNExecutor

HEADING = "Problems With Your Entry"
OTHER_PATIENT = "Wilhelmina Othersurname"      # a second patient's name — must never leak
SUBJECT_FIRST, SUBJECT_LAST = "Zzsubject", "Testsurname"

VALIDATION_BODY = (
    "There were some problems with this patient. "
    "Email address is not valid."
)
# The real captured wording, plus a word the EXISTING duplicate predicate
# actually matches. NOTE: the observed sentence says "also exists", which that
# predicate does NOT match (it looks for "already exists"/"duplicate"). In
# production the block evidently carries a triggering word elsewhere, since
# detection did fire three times. That fragility is PRE-EXISTING and is flagged,
# not changed, by this build — see [L].
DUPLICATE_BODY = (
    "There were some warnings for this patient. Do you want to make any changes "
    f"before you add this patient? This may be a duplicate. Another patient with "
    f"a similar name and date of birth also exists: {OTHER_PATIENT}"
)

# The observed sentence ON ITS OWN — no triggering word.
ALSO_EXISTS_ONLY = (
    "Another patient with a similar name and date of birth also exists: "
    f"{OTHER_PATIENT}"
)


def page_with(container_sel_html: str) -> str:
    return f"<html><body><div id='form'>form</div>{container_sel_html}</body></html>"


def block(body: str, cls: str = "Dialog", heading: str = HEADING, role: str = "") -> str:
    r = f' role="{role}"' if role else ""
    return f'<div class="{cls}"{r}><h2>{heading}</h2><p>{body}</p></div>'


class FakePatient:
    first_name, last_name = SUBJECT_FIRST, SUBJECT_LAST


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


async def probe(browser, html, cls=TNExecutorV2):
    """Run ONLY the probe — never the save verdict, which this build must not touch."""
    page = await browser.new_page(viewport={"width": 1200, "height": 900})
    await page.set_content(html)
    from services.api.tn_executor_v2 import _name_tokens
    toks = _name_tokens(f"{SUBJECT_FIRST} {SUBJECT_LAST}")
    return page, await page.evaluate(cls._SAVE_PROBLEM_PROBE_JS, [toks, cls.SAVE_PROBLEM_MAX_CHARS])


async def run():
    r = Results()
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # -------------------------------------------------------------
        print("\n[A] Validation refusal, NO duplicate wording -> captured (the old blind spot)")
        page, res = await probe(browser, page_with(block(VALIDATION_BODY)))
        r.check("problem block captured", bool(res["problem"]), res)
        r.check("carries the actual reason", "Email address is not valid" in (res["problem"] or ""), res["problem"])
        r.check("container reported", res["problemSelector"] == ".Dialog", res["problemSelector"])
        r.check("duplicate correctly NOT triggered", res["duplicate"] is None, res["duplicate"])
        await page.close()

        # -------------------------------------------------------------
        print("\n[B] Duplicate wording -> captured AND duplicate detection unchanged")
        page, res = await probe(browser, page_with(block(DUPLICATE_BODY)))
        r.check("duplicate still detected", bool(res["duplicate"]), res)
        r.check("duplicate container reported", res["duplicateSelector"] == ".Dialog", res["duplicateSelector"])
        r.check("problem block also captured", bool(res["problem"]))
        r.check("OTHER patient's name redacted from duplicate text",
                OTHER_PATIENT not in (res["duplicate"] or ""), res["duplicate"])
        r.check("OTHER patient's name redacted from problem text",
                OTHER_PATIENT not in (res["problem"] or ""), res["problem"])
        await page.close()

        # -------------------------------------------------------------
        print("\n[C] Both a validation message and duplicate wording")
        page, res = await probe(browser, page_with(block(VALIDATION_BODY + " " + DUPLICATE_BODY)))
        r.check("duplicate detected", bool(res["duplicate"]))
        r.check("validation reason preserved in the problem block",
                "Email address is not valid" in (res["problem"] or ""), res["problem"])
        r.check("no other-patient name anywhere",
                OTHER_PATIENT not in ((res["problem"] or "") + (res["duplicate"] or "")))
        await page.close()

        # -------------------------------------------------------------
        print("\n[D] Subject patient's OWN name in the block -> redacted too")
        page, res = await probe(
            browser, page_with(block(f"{SUBJECT_FIRST} {SUBJECT_LAST} could not be saved. ZIP is not valid.")))
        r.check("own first name redacted", SUBJECT_FIRST not in (res["problem"] or ""), res["problem"])
        r.check("own last name redacted", SUBJECT_LAST not in (res["problem"] or ""), res["problem"])
        r.check("the REASON survives redaction", "ZIP is not valid" in (res["problem"] or ""), res["problem"])
        await page.close()

        # -------------------------------------------------------------
        print("\n[E] No block at all -> identical to today (nothing captured, nothing logged)")
        page, res = await probe(browser, page_with("<div id='ok'>Patient record</div>"))
        r.check("no problem captured", res["problem"] is None, res)
        r.check("no duplicate captured", res["duplicate"] is None, res)
        r.check("no selectors reported", res["problemSelector"] is None and res["duplicateSelector"] is None)
        await page.close()

        # -------------------------------------------------------------
        print("\n[F] Different heading wording -> degrades gracefully (no capture, no crash)")
        page, res = await probe(browser, page_with(block(VALIDATION_BODY, heading="There Were Some Problems")))
        r.check("no problem captured (heading not recognised)", res["problem"] is None, res["problem"])
        r.check("probe still returns cleanly", isinstance(res, dict))
        r.check("duplicate path unaffected", res["duplicate"] is None)
        await page.close()

        # -------------------------------------------------------------
        print("\n[G] Which container matched — each candidate in the set")
        for cls_attr, role, expect in [("Dialog", "", ".Dialog"),
                                       ("modal", "", ".modal"),
                                       ("somebox", "dialog", '[role="dialog"]'),
                                       ("alert-danger", "", ".alert-danger")]:
            page, res = await probe(
                browser, page_with(block(VALIDATION_BODY, cls=cls_attr, role=role)))
            r.check(f"{expect}: captured and reported",
                    res["problem"] is not None and res["problemSelector"] == expect,
                    f"{res['problemSelector']}")
            await page.close()

        print("\n[H] Body-text fallback when the block is in no known container")
        page, res = await probe(
            browser, page_with(f"<section><h2>{HEADING}</h2><p>{VALIDATION_BODY}</p></section>"))
        r.check("captured via body fallback", bool(res["problem"]), res)
        r.check("fallback reported as such", res["problemSelector"] == "body (fallback)", res["problemSelector"])
        await page.close()

        # -------------------------------------------------------------
        print("\n[I] Duplicate DECISION is bit-for-bit unchanged vs the old predicate")
        # The old probe: first visible container containing duplicate/Duplicate/
        # already exists, sliced to 200. Assert the new one agrees on truthiness
        # across a spread of pages.
        cases = [
            ("duplicate word", block("This looks like a duplicate record."), True),
            ("already exists", block("A patient already exists with this name."), True),
            ("capital Duplicate", block("Duplicate detected."), True),
            ("neither word", block("Email address is not valid."), False),
            ("no container", "<div>nothing</div>", False),
        ]
        for label, html, expect_dup in cases:
            page, res = await probe(browser, page_with(html))
            r.check(f"duplicate decision — {label}: {expect_dup}",
                    bool(res["duplicate"]) is expect_dup, res["duplicate"])
            await page.close()

        # -------------------------------------------------------------
        print("\n[L] PRE-EXISTING gap, documented not fixed: \"also exists\" alone "
              "does not trigger duplicate detection")
        page, res = await probe(browser, page_with(block(ALSO_EXISTS_ONLY)))
        r.check("duplicate NOT detected on 'also exists' alone (unchanged behaviour)",
                res["duplicate"] is None, res["duplicate"])
        r.check("...but the block IS now captured, so the reason is no longer lost",
                bool(res["problem"]), res["problem"])
        r.check("...and the other patient's name is still redacted",
                OTHER_PATIENT not in (res["problem"] or ""), res["problem"])

        # -------------------------------------------------------------
        print("\n[J] Sentinel — no patient name in anything the probe returns")
        page, res = await probe(browser, page_with(block(DUPLICATE_BODY)))
        blob = " ".join(str(v) for v in res.values())
        for token in OTHER_PATIENT.split() + [SUBJECT_FIRST, SUBJECT_LAST]:
            r.check(f"'{token}' absent from probe output", token not in blob, blob[:200])
        await page.close()

        # -------------------------------------------------------------
        print("\n[K] V1 parity — same probe, same behaviour")
        page, res = await probe(browser, page_with(block(VALIDATION_BODY)), TNExecutor)
        r.check("[V1] problem captured", bool(res["problem"]), res)
        r.check("[V1] container reported", res["problemSelector"] == ".Dialog")
        await page.close()
        page, res = await probe(browser, page_with(block(DUPLICATE_BODY)), TNExecutor)
        r.check("[V1] duplicate still detected", bool(res["duplicate"]))
        r.check("[V1] other patient's name redacted", OTHER_PATIENT not in (res["duplicate"] or ""))
        await page.close()

        await browser.close()

    print(f"\n{'PASS' if r.failed == 0 else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    if r.failed:
        print("\nFailures:")
        for line in r.lines:
            print(f"  - {line}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
