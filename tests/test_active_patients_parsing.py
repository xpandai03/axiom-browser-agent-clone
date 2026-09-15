"""
Synthetic verification for the active-patient identity parser.

No browser, no network, no TherapyNotes, no PHI — every name below is invented
for this file.

WHAT THIS COVERS. The pure half: column discovery from header labels, row
extraction with and without those columns, chart-id parsing, and the read-only
properties that can be asserted from source. The browser half is a live run.

Run: python3 tests/test_active_patients_parsing.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The PURE module, deliberately: it imports nothing beyond the standard library,
# so these checks run without FastAPI, pydantic or Playwright installed.
from shared.patient_row_parsing import (  # noqa: E402
    chart_id_from_href,
    classify_headers,
    extract_row,
    squeeze,
)

PASS = FAIL = 0
failures = []


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        failures.append(name)
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def eq(name, actual, expected):
    ok(name, actual == expected, f"got {actual!r}, want {expected!r}")


# ===========================================================================
print("\n[1] Column discovery from the header row")
HEADERS = ["", "Name", "Date of Birth", "Phone", "Clinicians", "Balance"]
cols = classify_headers(HEADERS)
eq("the name column is found", cols["name"], 1)
eq("the date-of-birth column is found", cols["dob"], 2)
eq("the phone column is found", cols["phone"], 3)

eq("'DOB' is recognised too", classify_headers(["Patient", "DOB"])["dob"], 1)
eq("'Mobile' counts as a phone column", classify_headers(["Name", "Mobile"])["phone"], 1)
# The whole reason this route discovers rather than assumes.
missing = classify_headers(["Name", "Date of Birth", "Balance"])
eq("a table with NO phone column reports none", missing["phone"], None)
eq("...while still finding the others", (missing["name"], missing["dob"]), (0, 1))
eq("an empty header row finds nothing",
   classify_headers([]), {"name": None, "dob": None, "phone": None})
# "Phone" must not be claimed by the name matcher first.
ph = classify_headers(["Patient Name", "Phone"])
eq("a name column does not swallow the phone column", (ph["name"], ph["phone"]), (0, 1))


# ===========================================================================
print("\n[2] A normal row")
cells = ["", "Wendell Puffin", "4/12/1990", "(505) 555-0143", "Provider A", "0.00"]
dob, phone = extract_row(cells, cols)
eq("the date of birth is read verbatim", dob, "4/12/1990")
eq("the phone is read verbatim, punctuation and all", phone, "(505) 555-0143")


# ===========================================================================
print("\n[3] Missing fields are data, not errors")
no_phone = ["", "Marigold Thistleby", "11/2/1985", "", "Provider A", "0.00"]
dob, phone = extract_row(no_phone, cols)
eq("a row with no phone still yields its date of birth", dob, "11/2/1985")
eq("...and an empty phone rather than a guess", phone, "")

no_dob = ["", "Barnaby Quillfeather", "", "505-555-0177", "Provider A", "0.00"]
dob, phone = extract_row(no_dob, cols)
eq("a row with no date of birth yields empty", dob, "")
eq("...and still yields the phone", phone, "505-555-0177")

neither = ["", "Odette Marchbanks", "", "", "Provider A", "0.00"]
eq("a row with neither yields two empties", extract_row(neither, cols), ("", ""))


# ===========================================================================
print("\n[4] A table with no usable header falls back to shape, not to an index")
none_cols = {"name": None, "dob": None, "phone": None}
dob, phone = extract_row(cells, none_cols)
eq("the date-shaped cell is found by shape", dob, "4/12/1990")
eq("the phone-shaped cell is found by shape", phone, "(505) 555-0143")
# The date is phone-shaped under a digit-count test; it must not be taken twice.
ok("the date is not also returned as the phone", phone != dob)

only_date = ["", "Casimir Underhill", "7/1/1979", "", "Provider A"]
eq("with no phone present the fallback returns empty",
   extract_row(only_date, none_cols), ("7/1/1979", ""))

# A balance column is digits but must not become a phone.
short_digits = ["", "Perpetua Glimmerwick", "", "0.00", "Provider A"]
eq("a short numeric cell is not mistaken for a phone",
   extract_row(short_digits, none_cols), ("", ""))


# ===========================================================================
print("\n[5] Unusual names and formats survive untouched")
odd = ["", "Renée O'Brien-Smith Jr.", "1/1/2001", "+1 (505) 555-0100 ext. 12", "Provider B"]
dob, phone = extract_row(odd, cols)
eq("a non-ASCII, hyphenated, suffixed name is not the parser's business", dob, "1/1/2001")
eq("an extension and a country code are kept verbatim", phone, "+1 (505) 555-0100 ext. 12")
eq("squeeze collapses whitespace and nothing else",
   squeeze("  Renée   O'Brien-Smith   Jr.  "), "Renée O'Brien-Smith Jr.")
eq("squeeze does not case-fold", squeeze("McDonald"), "McDonald")
eq("squeeze handles None", squeeze(None), "")
# Dates the page might render in other shapes.
for raw in ("12/31/1999", "1-2-2003", "2003.04.05"):
    d, _ = extract_row(["", "X Y", raw, ""], none_cols)
    eq(f"{raw} is recognised as a date", d, raw)


# ===========================================================================
print("\n[6] Chart ids out of hrefs")
eq("the edit form", chart_id_from_href("/app/patients/edit/1005471/"), "1005471")
eq("without edit", chart_id_from_href("/app/patients/1005471/"), "1005471")
eq("an absolute url", chart_id_from_href("https://www.therapynotes.com/app/patients/edit/abc123/"), "abc123")
eq("a query string does not confuse it",
   chart_id_from_href("/app/patients/edit/1005471/?tab=overview"), "1005471")
eq("an unrecognisable href yields empty", chart_id_from_href("/app/scheduling/"), "")
eq("None yields empty", chart_id_from_href(None), "")
eq("empty yields empty", chart_id_from_href(""), "")


# ===========================================================================
print("\n[7] A zero-result clinician, and rows that are not patients")
# TherapyNotes renders a message rather than an empty table. The executor
# returns success with row_count 0; asserted here at the level this file can
# reach — that nothing in the parser invents a row from no cells.
eq("no cells yields two empties", extract_row([], cols), ("", ""))
eq("...and no chart id means the row is skipped upstream", chart_id_from_href(""), "")


# ===========================================================================
print("\n[8] Read-only and no-PHI, asserted from source")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(root, "services/api/active_patients_executor.py")).read()
code = re.sub(r'"""[\s\S]*?"""', "", src)
code = re.sub(r"^\s*#.*$", "", code, flags=re.M)

ok("it subclasses the active-count executor rather than copying it",
   "class ActivePatientsExecutor(ActiveCountExecutor)" in src)
ok("it reuses that enumeration", "_enumerate_clinicians()" in code)
ok("it defines no clinician loop of its own beyond the reused one",
   code.count("_enumerate_clinicians") == 1)
ok("rows are located by the anchor, never by tr.Row",
   "patient-search-patient-link" in src and "tr.Row" not in code)
ok("the only click is the inherited pager",
   re.findall(r"_click_and_wait\(([A-Z_]+)\)", code) == ["SEL_PAGER_NEXT"])
ok("no selector for a write control is defined here",
   not re.search(r"(?i)(save|submit|create|schedule|delete|upload)\s*=\s*[\"']", code))
ok("no chart url is ever navigated to", "patients/edit" not in code)
ok("no href is followed — the id is parsed as text",
   "goto(" in code and code.count("goto(") == 1 and "PATIENTS_URL" in code)
ok("the pass is serialised on the shared execution lock", "_execution_lock" in code)

log_lines = re.findall(r"logger\.(?:info|warning|error)\(([\s\S]*?)\)\n", code)
blob = "\n".join(log_lines)
for forbidden in ("row.name", "chart_id", ".dob", ".phone", "rows[", "PatientIdentityRow"):
    ok(f"no log line carries {forbidden}", forbidden not in blob)
ok("log lines carry counts and the clinician label only",
   "page.row_count" in blob and "label!r" in blob)


print(f"\n{'PASS' if FAIL == 0 else 'FAIL'} — {PASS} passed, {FAIL} failed")
if FAIL:
    print("\n".join(f"  - {f}" for f in failures))
    sys.exit(1)
