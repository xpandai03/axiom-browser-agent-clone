"""
Sentinel assertions for the 422 validation path — no patient value may reach a
log line or the HTTP response.

WHY A SENTINEL AND NOT A READING: when this fix was first written, a visual
review of the handler passed while two leaks were still live. The sentinel test
caught both —

  1. the response echoed the whole request body back as `body_received`;
  2. the schemas' OWN validators embedded the rejected value in `msg`
     ("ZIP must contain only digits (got: '<value>')"), which survives any
     stripping of Pydantic's `input`/`ctx`.

So this asserts on captured output, never on the shape of the code.

Scope: the 422 handler and the schema validator messages ONLY. It does not touch
the save phase; the save verification from the same original commit was reverted
on 4 Sept and is deliberately NOT part of this.

Run:  <venv>/bin/python -m tests.test_phi_validation_logging
"""

import logging
import os
import sys

from fastapi.testclient import TestClient

from services.api.app import create_app

# Distinctive, obviously synthetic, and not a real value of any kind.
SENTINEL = "SENTINEL-PHI-0987654321"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            self.lines.append("<unformattable>")


class Results:
    def __init__(self):
        self.passed = 0
        self.failed = 0
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


def run():
    r = Results()
    cap = _Capture()
    root = logging.getLogger()
    root.addHandler(cap)
    prev = root.level
    root.setLevel(logging.DEBUG)

    try:
        client = TestClient(create_app(), raise_server_exceptions=False)

        # Every value is the sentinel, so ANY echo of the payload shows up.
        # The shape mirrors a real TN request; none of the content is real.
        body = {
            "first_name": SENTINEL, "last_name": SENTINEL, "dob": SENTINEL,
            "address": SENTINEL, "zip": SENTINEL, "sex": SENTINEL,
            "email": SENTINEL, "phone": SENTINEL, "rfs_url": SENTINEL,
            "intake_pdf_url": SENTINEL, "snapshot_pdf_url": SENTINEL,
            "appointment_date": SENTINEL, "appointment_time": SENTINEL,
            "appointment_alert_text": SENTINEL, "appointment_modality": SENTINEL,
            "clinician_name": SENTINEL,
        }
        resp = client.post(
            "/api/tn/create-patient-with-schedule",
            json=body,
            headers={"X-API-Key": os.environ.get("TN_API_KEY", "")},
        )

        print("\n[1] The request is rejected as a validation error")
        r.check("HTTP 422", resp.status_code == 422, f"got {resp.status_code}")

        print("\n[2] Self-check 3 — no sentinel in ANY log line")
        logged = "\n".join(cap.lines)
        offenders = [l for l in cap.lines if SENTINEL in l]
        r.check("no sentinel in any log line", not offenders, offenders[:1])

        print("\n[3] Self-check 4 — no sentinel in the response body")
        r.check("no sentinel in the response", SENTINEL not in resp.text, resp.text[:200])
        r.check("`body_received` is gone entirely", "body_received" not in resp.text)

        print("\n[4] Still diagnosable — field and constraint survive")
        r.check("response names the failing fields",
                "dob" in resp.text and "sex" in resp.text and "zip" in resp.text, resp.text[:200])
        r.check("response carries the constraint (msg)",
                "Male" in resp.text or "should match pattern" in resp.text, resp.text[:250])
        r.check("a log line names the failing fields",
                any("[VALIDATION]" in l and "sex" in l for l in cap.lines),
                [l for l in cap.lines if "[VALIDATION]" in l][:1])
        r.check("the old value-dumping log lines are gone",
                not any("[TN DEBUG]" in l for l in cap.lines),
                [l for l in cap.lines if "[TN DEBUG]" in l][:1])

        # ------------------------------------------------------------------
        # Self-check 5 — the validator messages themselves. This is what the
        # original sentinel run caught after the handler already looked clean.
        print("\n[5] Self-check 5 — validator messages carry the constraint, not the value")
        from shared.schemas.therapy_notes_v2 import TNPatientInputV2
        from shared.schemas.therapy_notes import TNPatientInput
        import pydantic

        for label, model, field, value in [
            ("V2 zip non-digit",  TNPatientInputV2, "zip", SENTINEL),
            ("V1 zip non-digit",  TNPatientInput,   "zip", SENTINEL),
            ("V2 zip wrong len",  TNPatientInputV2, "zip", "123"),
            ("V1 zip wrong len",  TNPatientInput,   "zip", "123"),
            ("V2 pdf url scheme", TNPatientInputV2, "intake_pdf_url", f"file:///{SENTINEL}.pdf"),
        ]:
            payload = dict(body)
            payload[field] = value
            try:
                model(**payload)
                msgs = "<no error raised>"
            except pydantic.ValidationError as e:
                msgs = "; ".join(err.get("msg", "") for err in e.errors())
            r.check(f"{label}: message excludes the value",
                    SENTINEL not in msgs and "123" not in msgs.replace("5 digits", ""), msgs[:160])
            r.check(f"{label}: message still states the constraint",
                    ("ZIP" in msgs or "http(s) URL" in msgs), msgs[:160])

        # ------------------------------------------------------------------
        # Self-check 6 — the ZIP-padding warning. A 4-digit ZIP is padded and
        # must be accepted, while the warning must not name it.
        print("\n[6] Self-check 6 — the ZIP-padding warning does not log the ZIP")
        cap.lines.clear()
        payload = dict(body)
        payload["zip"] = "7031"          # pads to 07031
        try:
            TNPatientInputV2(**payload)
        except Exception:
            pass                          # other fields still fail; zip is what matters
        zip_lines = [l for l in cap.lines if "ZIP NORMALIZE" in l]
        r.check("the pad warning fired", bool(zip_lines), cap.lines[-3:])
        r.check("it does not contain the 4-digit ZIP",
                not any("7031" in l for l in zip_lines), zip_lines[:1])
        r.check("it does not contain the padded ZIP",
                not any("07031" in l for l in zip_lines), zip_lines[:1])
        r.check("it still says a pad happened",
                any("Padded" in l for l in zip_lines), zip_lines[:1])

    finally:
        root.removeHandler(cap)
        root.setLevel(prev)

    print(f"\n{'PASS' if r.failed == 0 else 'FAIL'} — {r.passed} passed, {r.failed} failed")
    if r.failed:
        print("\nFailures:")
        for line in r.lines:
            print(f"  - {line}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(run())
