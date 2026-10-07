"""
Patient-portal step (services/api/portal_step.py) on a mock chart, and the
client's documents table (services/api/portal_documents.py).

The mock mirrors the recon (docs/selectors/tn_portal.md): Portal hash tab,
"Send/Resend Welcome Email" input buttons, the shared-documents table under
a[data-testid='dynamictable-document-header'], #PortalDocumentsPage__ShareDocumentsButton
opening #PortalDocRequest with the typeahead #PortalDocumentsSelector
input.inline-selector-input, the included list, and psy-button#SendButton.
The suggestion markup is invented (recon could not see it): the step finds
suggestions by exact text, which this mock exercises with a decoy elsewhere.

No real names; document names are the practice's form names.
"""
import json
import re

import pytest

from services.api.portal_documents import (
    DAS, INSURANCE, already_shared, candidates_for, portal_documents_for,
)
from services.api.portal_step import PortalStep, run_portal_phase

pytestmark = pytest.mark.asyncio

CHART_URL = "https://www.therapynotes.com/app/patients/edit/ZZTESTPORTAL1/"
FULL_LIBRARY = [
    "Client Insurance Form", "Emergency & Other Contacts Form",
    "Electronic Communication Consent by Non-secure Transmission", "PCP",
    "Informed Consent for Treatment - Minor", "Parent Interview Under 14",
    "Informed Consent - Adolescent", "Client History Form", "Informed Consent - Adult",
    "Informed Consent - Couples", "Informed Consent - Families", "DAS: Dyadic Adjustment Scale",
    "Records Request", "Informed Consent for the Use of Artificial Intelligence in Services",
]


# ---------------------------------------------------------------------------
# The table: all eight cases
# ---------------------------------------------------------------------------

E, C, P = "Emergency & Other Contacts Form", "Electronic Communication Consent by Non-secure Transmission", "PCP"
H = "Client History Form"

TABLE = [
    ("Minor", False, [INSURANCE, E, C, P, "Informed Consent for Treatment - Minor", "Parent Interview Under 14"]),
    ("Adolescent", False, [INSURANCE, E, C, P, "Informed Consent - Adolescent", H]),
    ("Individual", True, [E, C, P, "Informed Consent - Adult", H]),
    ("Individual", False, [INSURANCE, E, C, P, "Informed Consent - Adult", H]),
    ("My Partner & Myself", True, [E, C, P, "Informed Consent - Couples", H, DAS]),
    ("My Partner & Myself", False, [INSURANCE, E, C, P, "Informed Consent - Couples", H, DAS]),
    ("My Family", True, [E, C, P, "Informed Consent - Families", H]),
    ("My Family", False, [INSURANCE, E, C, P, "Informed Consent - Families", H]),
]


@pytest.mark.parametrize("row,vaccn,want", TABLE)
async def test_a_the_table(row, vaccn, want):
    assert portal_documents_for(row, vaccn) == want


async def test_a_vaccn_never_gets_the_insurance_form_on_any_row():
    for row in ("Minor", "Adolescent", "Individual", "My Partner & Myself", "My Family"):
        assert INSURANCE not in portal_documents_for(row, True)
        assert INSURANCE in portal_documents_for(row, False)


async def test_a_unknown_row_raises_and_das_has_its_tn_alias():
    with pytest.raises(ValueError):
        portal_documents_for("Couples", False)
    assert candidates_for(DAS) == [DAS, "DAS"]
    assert already_shared(DAS, ["DAS"]) and already_shared("PCP", ["  pcp "]) and not already_shared("PCP", ["PCPs"])


# ---------------------------------------------------------------------------
# The mock chart
# ---------------------------------------------------------------------------

def portal_page(welcome="send", shared=(), library=FULL_LIBRARY, preincluded=()):
    welcome_ctl = {
        "send": '<input type="button" value="Send Welcome Email" onclick="openWelcome()">',
        "resend": '<input type="button" value="Resend Welcome Email">',
        "none": "",
    }[welcome]
    return f"""<html><body>
      <ul><li><a href="#tab=Documents">Documents</a></li><li><a href="#tab=Portal" onclick="showPortal()">Portal</a></li></ul>
      <div id="sidebar">Recent: <span>PCP</span></div>
      <div id="portalPane" style="display:none">
        <span id="welcomeSlot">{welcome_ctl}</span>
        <div id="welcomeDialog" style="display:none">
          <input type="button" value="Email address is correct" onclick="confirmWelcome()">
        </div>
        <button id="PortalDocumentsPage__ShareDocumentsButton" onclick="openShare()">Share Documents</button>
        <table id="shared"><tr><th><a data-testid="dynamictable-document-header" href="#">Document</a></th></tr></table>
      </div>
      <div id="PortalDocRequest" style="display:none">
        <button class="DialogCloseButton" onclick="closeShare()">x</button>
        <div id="PortalDocRequestView"><div id="SendNewRequestToPatientViewer__viewElem">
          <h2><div><span>ZZTEST Portal</span></div></h2>
          <div class="basic-form"><div>Select Library Documents to Share:</div></div>
          <div id="PortalDocumentsSelector"><span class="IncrementalSearchContainerNode">
            <input type="search" class="inline-selector-input" oninput="suggest(this.value)"></span>
            <ul id="sugg" class="IncrementalSearchResults"></ul></div>
          <span class="form-label">Documents Included in Request:</span>
          <div id="included"></div>
          <div class="basic-form"><label>Instructions:</label><textarea id="documentRequestInstructionsInputArea"></textarea></div>
          <div id="SendButtonArea"><psy-button id="SendButton" onclick="sendRequest()">Send Document Request</psy-button></div>
        </div></div>
      </div>
      <script>
        window.__welcomeSent = 0; window.__sent = 0; window.__shareOpened = 0; window.__typed = 0;
        const LIB = {json.dumps(list(library))};
        function row(n) {{ const tr = document.createElement('tr'); tr.innerHTML = '<td></td><td>10/7/26</td><td></td><td>Pending Submission</td>'; tr.firstChild.textContent = n; document.getElementById('shared').appendChild(tr); }}
        {json.dumps(list(shared))}.forEach(row);
        function inc(n) {{ const d = document.createElement('div'); d.className = 'included-item'; d.textContent = n; document.getElementById('included').appendChild(d); }}
        function showPortal() {{ setTimeout(() => document.getElementById('portalPane').style.display = 'block', 300); }}
        function openWelcome() {{ document.getElementById('welcomeDialog').style.display = 'block'; }}
        function confirmWelcome() {{
          window.__welcomeSent++; document.getElementById('welcomeDialog').style.display = 'none';
          setTimeout(() => document.getElementById('welcomeSlot').innerHTML = '<input type="button" value="Resend Welcome Email">', 400);
        }}
        function openShare() {{
          window.__shareOpened++; document.getElementById('PortalDocRequest').style.display = 'block';
          document.getElementById('included').innerHTML = ''; {json.dumps(list(preincluded))}.forEach(inc);
        }}
        function closeShare() {{ document.getElementById('PortalDocRequest').style.display = 'none'; document.getElementById('sugg').innerHTML = ''; }}
        function suggest(v) {{
          window.__typed++;
          const ul = document.getElementById('sugg'); ul.innerHTML = '';
          if (!v || v.length < 2) return;
          setTimeout(() => {{
            ul.innerHTML = '';
            LIB.filter(n => n.toLowerCase().includes(v.toLowerCase())).forEach(n => {{
              const li = document.createElement('li'); li.setAttribute('role', 'option'); li.textContent = n;
              li.onclick = () => {{ inc(n); ul.innerHTML = ''; }};
              ul.appendChild(li);
            }});
          }}, 250);
        }}
        function sendRequest() {{
          window.__sent++;
          [...document.querySelectorAll('#included .included-item')].forEach(d => row(d.textContent));
          setTimeout(closeShare, 300);
        }}
      </script>
    </body></html>"""


async def _run(html, service_type="Individual", vaccn=False, dry_run=True, skip=None):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-http2"])
        ctx = await browser.new_context()
        page = await ctx.new_page()

        async def route(r):
            await r.fulfill(status=200, content_type="text/html", body=html)

        await ctx.route(re.compile(r"https://www\.therapynotes\.com/.*"), route)
        await page.goto(CHART_URL, wait_until="domcontentloaded")
        step = PortalStep(page, CHART_URL)
        step.WAIT_S = 6
        v = await step.run(service_type, skip, vaccn, dry_run)
        state = await page.evaluate("""() => ({welcome: window.__welcomeSent, sent: window.__sent,
            opened: window.__shareOpened, typed: window.__typed,
            dialogOpen: getComputedStyle(document.getElementById('PortalDocRequest')).display !== 'none',
            shared: [...document.querySelectorAll('#shared tr td:first-child')].map(td => td.textContent)})""")
        await browser.close()
        return v, state


INDIVIDUAL = [INSURANCE, E, C, P, "Informed Consent - Adult", H]


async def test_b_unmappable_type_skips_with_its_reason():
    v = await PortalStep(None, CHART_URL).run(None, "self_requested_under_18", False, True)
    assert v["portalStatus"] == "skipped" and v["portalReason"] == "self_requested_under_18"
    v = await PortalStep(None, CHART_URL).run(None, None, False, True)
    assert v["portalReason"] == "service_type_unmapped"


async def test_c_dry_run_picks_everything_and_sends_nothing():
    v, s = await _run(portal_page(welcome="send"), dry_run=True)
    assert v["portalStatus"] == "dry_run", v
    assert v["portalDocuments"] == INDIVIDUAL and v["portalMissing"] == []
    assert v["welcomeEmail"] == "would_send"
    assert s["welcome"] == 0 and s["sent"] == 0, "a dry run sent something"
    assert s["shared"] == [] and s["dialogOpen"] is False


async def test_d_live_sends_the_welcome_email_and_exactly_the_list():
    v, s = await _run(portal_page(welcome="send"), dry_run=False)
    assert v["portalStatus"] == "done", v
    assert v["welcomeEmail"] == "sent" and s["welcome"] == 1
    assert s["sent"] == 1 and sorted(s["shared"]) == sorted(INDIVIDUAL)
    assert v["portalDocuments"] == INDIVIDUAL


async def test_d_vaccn_gets_no_insurance_form():
    v, s = await _run(portal_page(), vaccn=True, dry_run=False)
    assert v["portalStatus"] == "done" and INSURANCE not in s["shared"] and len(s["shared"]) == 5


async def test_e_idempotent_welcome_already_sent_and_documents_already_shared():
    v, s = await _run(portal_page(welcome="resend", shared=[P, "Informed Consent - Adult", "Records Request"]), dry_run=False)
    assert v["welcomeEmail"] == "already_sent" and s["welcome"] == 0
    assert v["portalAlreadyShared"] == [P, "Informed Consent - Adult"]
    assert v["portalDocuments"] == [INSURANCE, E, C, H]
    assert s["shared"].count(P) == 1, "shared a second copy"


async def test_e_everything_already_shared_opens_nothing():
    v, s = await _run(portal_page(welcome="resend", shared=INDIVIDUAL), dry_run=False)
    assert v["portalStatus"] == "done" and v["portalDocuments"] == [] and s["opened"] == 0 and s["sent"] == 0


async def test_f_a_document_missing_from_tn_is_named_and_the_rest_shared():
    lib = [n for n in FULL_LIBRARY if n != INSURANCE]
    v, s = await _run(portal_page(library=lib), dry_run=False)
    assert v["portalStatus"] == "done" and v["portalMissing"] == [INSURANCE]
    assert sorted(s["shared"]) == sorted([E, C, P, "Informed Consent - Adult", H])


async def test_f_das_is_found_under_its_tn_name():
    lib = [n for n in FULL_LIBRARY if n != DAS] + ["DAS"]
    v, s = await _run(portal_page(library=lib), service_type="My Partner & Myself", vaccn=True, dry_run=True)
    assert v["portalStatus"] == "dry_run" and "DAS" in v["portalDocuments"] and v["portalMissing"] == []


async def test_g_no_welcome_control_skips_to_documents_and_says_so():
    v, s = await _run(portal_page(welcome="none"), dry_run=False)
    assert v["welcomeEmail"] == "unavailable" and v["portalStatus"] == "done" and s["sent"] == 1


async def test_h_an_unexpected_document_in_the_request_stops_before_send():
    v, s = await _run(portal_page(preincluded=["Records Request"]), dry_run=False)
    assert v["portalStatus"] == "failed" and v["portalStep"] == "tick_documents"
    assert v["portalReason"] == "request_list_mismatch" and s["sent"] == 0


async def test_h_the_decoy_outside_the_picker_is_never_clicked():
    # "PCP" sits in a sidebar on the page; the pick must come from the typeahead.
    v, s = await _run(portal_page(), dry_run=True)
    assert P in v["portalDocuments"]


async def test_i_an_exception_becomes_a_failed_verdict_never_a_crash():
    class Patient:
        service_type = "Individual"; portal_skip_reason = None; payer_vaccn = False; portal_dry_run = True
    v = await run_portal_phase(None, CHART_URL, Patient())
    assert v["portalStatus"] == "failed" and v["portalStep"] == "unexpected"
