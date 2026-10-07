"""
The client's portal-documents table: which intake documents to share on the
TherapyNotes patient portal after booking, by service type and payer.

Pure and dependency-free. Names are the CLIENT'S table names, verbatim
(2026-10-07). Rule of thumb from the table: VACCN never gets the Client
Insurance Form; everyone else does.

TN_NAME_ALIASES: what TherapyNotes renders where it differs from the table,
from the read-only recon (docs/selectors/tn_portal.md). The picker tries the
table name first, then each alias, and reports the name it actually matched.
  - "DAS: Dyadic Adjustment Scale" renders as "DAS" in the shared-documents
    table on the test chart.
  - "Client Insurance Form" was not on the test chart, so it is unverified; if
    no suggestion matches it is reported in portalMissing and the rest are
    shared (the decision rule).
"""

from typing import Dict, List, Optional, Sequence

INSURANCE = "Client Insurance Form"
EMERGENCY = "Emergency & Other Contacts Form"
ECC = "Electronic Communication Consent by Non-secure Transmission"
PCP = "PCP"
HISTORY = "Client History Form"
DAS = "DAS: Dyadic Adjustment Scale"

# Without the insurance form; it is added for every payer except VACCN.
_BASE: Dict[str, List[str]] = {
    "Minor": [EMERGENCY, ECC, PCP, "Informed Consent for Treatment - Minor", "Parent Interview Under 14"],
    "Adolescent": [EMERGENCY, ECC, PCP, "Informed Consent - Adolescent", HISTORY],
    "Individual": [EMERGENCY, ECC, PCP, "Informed Consent - Adult", HISTORY],
    "My Partner & Myself": [EMERGENCY, ECC, PCP, "Informed Consent - Couples", HISTORY, DAS],
    "My Family": [EMERGENCY, ECC, PCP, "Informed Consent - Families", HISTORY],
}

SERVICE_TYPES = tuple(_BASE.keys())

TN_NAME_ALIASES: Dict[str, List[str]] = {
    DAS: ["DAS"],
}


def portal_documents_for(service_type: str, vaccn: bool) -> List[str]:
    """
    The documents for a table row, in table order, the insurance form first as
    the table lists it — unless the payer is VACCN.

    VACCN never gets the Client Insurance Form, on EVERY row. The table splits
    only the three adult rows by VACCN (Minor and Adolescent list the form
    unconditionally), but its rule of thumb — "VACCN never gets the Client
    Insurance Form; everyone else does" — is applied to all five, so a VACCN
    child gets no insurance form either. Reported to the practice 2026-10-07.
    """
    if service_type not in _BASE:
        raise ValueError(f"unknown service type: {service_type!r}")
    base = list(_BASE[service_type])
    return base if vaccn else [INSURANCE] + base


def candidates_for(name: str) -> List[str]:
    """The strings to look for in TherapyNotes for one table name, in order."""
    return [name] + [a for a in TN_NAME_ALIASES.get(name, []) if a != name]


def norm(s: Optional[str]) -> str:
    return " ".join((s or "").split()).casefold()


def already_shared(name: str, shared_names: Sequence[str]) -> bool:
    """Is this table document (under any of its TherapyNotes names) already shared?"""
    have = {norm(n) for n in shared_names}
    return any(norm(c) in have for c in candidates_for(name))
