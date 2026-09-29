"""
PHI redaction — the backstop that keeps patient identifiers out of Railway.

Railway is a third-party log store. The rule for this agent: no patient name,
date of birth, phone, email or address in any log line, exception text or
progress callback. Contact ids, submission ids, chart ids, run ids and step
names are the identifiers a log line may carry; the CRM maps ids to people.

Call sites are written to that rule directly. This module is the SECOND line,
for what a call site cannot see coming: an exception whose text echoes a typed
value, a TherapyNotes validation message that names the patient, a whole object
logged in a hurry, a log line someone adds next year.

HOW IT WORKS
  * phi_scope(...) — for the length of one run, remembers THIS patient's
    identifying values (as regexes that tolerate the usual formatting: a date
    of birth written 04/07/2014 or 2014-04-07, a phone with or without
    punctuation, a name in any case). It is a ContextVar, so it follows the
    run's coroutines and no other request's.
  * install_log_redaction() — wraps the logging record factory once, so EVERY
    record from every logger is scrubbed of the current scope's values before
    any handler sees it: the message, its args, and a traceback's text.
  * scrub_text(s) — the same scrub, for strings that leave by another route
    (the progress callback's message).
  * redact_mapping(obj) — replaces the VALUES of known PHI keys in a dict (at
    any depth) and scrubs the remaining strings. Used on callback metadata.

Over-redaction is accepted: a patient whose first name is also an ordinary word
turns that word into [redacted] in that run's logs. A leak is the failure this
exists to prevent; a less readable log line is not.

IMPORTS: standard library only.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import traceback
from typing import Iterable, Iterator, Optional, Pattern, Sequence, Tuple

REDACTED = "[redacted]"

# Keys whose VALUES are patient identifiers wherever they appear in a mapping.
# snake_case (the agent's schemas) and camelCase (callback/CRM shapes).
PHI_KEYS = frozenset({
    "first_name", "last_name", "full_name", "patient_name", "name",
    "dob", "date_of_birth", "birth_date",
    "email", "phone", "phone_number", "mobile_phone",
    "address", "street_address", "zip", "zip_code", "postal_code",
    "appointment_alert_text",
    "firstName", "lastName", "fullName", "patientName",
    "dateOfBirth", "birthDate", "phoneNumber", "mobilePhone",
    "streetAddress", "zipCode", "postalCode", "alertText",
})

_patterns: contextvars.ContextVar[Tuple[Pattern[str], ...]] = contextvars.ContextVar(
    "phi_patterns", default=()
)


# ---------------------------------------------------------------------------
# Building the per-run patterns
# ---------------------------------------------------------------------------

def _name_patterns(names: Iterable[Optional[str]]) -> list:
    out = []
    for raw in names:
        full = " ".join(str(raw or "").split())
        if not full:
            continue
        out.append(re.compile(r"\s+".join(re.escape(t) for t in full.split()), re.I))
        for tok in re.split(r"[\s,]+", full):
            tok = tok.strip(".'\"()")
            if len(tok) >= 2:
                out.append(re.compile(rf"(?<!\w){re.escape(tok)}(?!\w)", re.I))
    return out


def _dob_patterns(dob: Optional[str]) -> list:
    """04/07/2014, 4/7/2014, 2014-04-07, 04-07-2014, 04.07.2014 — whichever was sent."""
    v = str(dob or "").strip()
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", v)
    y = mo = d = None
    if m:
        mo, d, y = m.group(1), m.group(2), m.group(3)
    else:
        m = re.fullmatch(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", v)
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3)
    if not y:
        return [re.compile(re.escape(v))] if len(v) >= 6 else []
    mo_re = rf"0?{int(mo)}"
    d_re = rf"0?{int(d)}"
    sep = r"[/\-.]"
    return [
        re.compile(rf"(?<!\d){mo_re}{sep}{d_re}{sep}{y}(?!\d)"),
        re.compile(rf"(?<!\d){y}{sep}{mo_re}{sep}{d_re}(?!\d)"),
    ]


def _phone_patterns(phones: Iterable[Optional[str]]) -> list:
    out = []
    for raw in phones:
        digits = re.sub(r"\D", "", str(raw or ""))
        if len(digits) >= 7:
            # The last ten digits, punctuation allowed between any two.
            core = digits[-10:]
            out.append(re.compile(r"(?<!\d)" + r"[\s.\-()]{0,3}".join(core) + r"(?!\d)"))
    return out


def _literal_patterns(values: Iterable[Optional[str]], min_len: int = 3) -> list:
    out = []
    for raw in values:
        v = " ".join(str(raw or "").split())
        if len(v) >= min_len:
            out.append(re.compile(r"\s+".join(re.escape(t) for t in v.split()), re.I))
    return out


def _zip_patterns(zips: Iterable[Optional[str]]) -> list:
    out = []
    for raw in zips:
        z = re.sub(r"\D", "", str(raw or ""))[:5]
        if len(z) == 5:
            out.append(re.compile(rf"(?<!\d){z}(?!\d)"))
    return out


@contextlib.contextmanager
def phi_scope(
    *,
    names: Sequence[Optional[str]] = (),
    dob: Optional[str] = None,
    phones: Sequence[Optional[str]] = (),
    emails: Sequence[Optional[str]] = (),
    addresses: Sequence[Optional[str]] = (),
    zips: Sequence[Optional[str]] = (),
    other: Sequence[Optional[str]] = (),
) -> Iterator[None]:
    """Redact this patient's identifiers from every log record inside the block."""
    pats = (
        *_patterns.get(),
        *_literal_patterns(emails),
        *_literal_patterns(addresses),
        *_literal_patterns(other, min_len=6),
        *_phone_patterns(phones),
        *_dob_patterns(dob),
        *_zip_patterns(zips),
        *_name_patterns(names),   # last: most general, runs after the longer strings
    )
    token = _patterns.set(tuple(pats))
    try:
        yield
    finally:
        _patterns.reset(token)


def phi_scope_for(obj) -> contextlib.AbstractContextManager:
    """phi_scope from any patient-shaped payload (V1, V2, survey attach)."""
    g = lambda k: getattr(obj, k, None)  # noqa: E731
    first, last = g("first_name"), g("last_name")
    return phi_scope(
        names=[f"{first or ''} {last or ''}".strip(), first, last],
        dob=g("dob"),
        phones=[g("phone")],
        emails=[g("email")],
        addresses=[g("address")],
        zips=[g("zip")],
        other=[g("appointment_alert_text")],
    )


# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------

def scrub_text(text: Optional[str]) -> Optional[str]:
    """Remove the current run's patient identifiers from a string."""
    if not text:
        return text
    pats = _patterns.get()
    if not pats:
        return text
    out = str(text)
    for p in pats:
        out = p.sub(REDACTED, out)
    return out


def redact_mapping(obj, keep: Iterable[str] = ()):
    """
    A copy of `obj` with every PHI_KEYS value replaced and every other string
    scrubbed. Keys in `keep` pass through untouched (ids, codes, chart URLs the
    CRM needs). Lists and nested dicts are handled; other values are returned
    as they are.
    """
    keep = set(keep)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in keep:
                out[k] = v
            elif k in PHI_KEYS:
                out[k] = REDACTED
            else:
                out[k] = redact_mapping(v, keep)
        return out
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact_mapping(v, keep) for v in obj)
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj


# ---------------------------------------------------------------------------
# The logging backstop
# ---------------------------------------------------------------------------

_installed = False


def install_log_redaction() -> None:
    """
    Scrub every log record, from every logger, before any handler formats it.
    Installed once at import of the API app; idempotent.
    """
    global _installed
    if _installed:
        return
    base_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base_factory(*args, **kwargs)
        if not _patterns.get():
            return record
        try:
            msg = record.getMessage()
            clean = scrub_text(msg)
            if clean != msg:
                record.msg, record.args = clean, None
            if record.exc_info and record.exc_info[1] is not None:
                text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
                # Formatter.format uses exc_text when set, instead of re-rendering.
                record.exc_text = scrub_text(text)
        except Exception:
            # A record that cannot be rendered is left for logging to report;
            # redaction must never be the reason a log line is lost.
            pass
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
