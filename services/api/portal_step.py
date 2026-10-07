"""
Patient portal, after booking: welcome email, then the intake documents for
the contact's service type (services/api/portal_documents.py).

  Portal tab -> welcome email (send + confirm, or skip if already sent / not
  available) -> Share Documents -> pick exactly the documents not yet shared
  -> Send Document Request -> confirm on the shared-documents table.

DRY RUN (the default; the CRM sends portal_dry_run=false only when PORTAL_LIVE
is set): does everything up to picking the documents, then closes the dialog
without sending. The welcome email is NOT sent in a dry run either — nothing
leaves TherapyNotes.

NEVER FAILS THE RUN. Every outcome is a verdict dict:
  portalStatus   "done" | "dry_run" | "failed" | "skipped"
  portalStep     where it stopped (failed)
  portalReason   a code
  portalDocuments  names shared (done) or that would be (dry_run), as matched in TN
  portalMissing    table names no TherapyNotes suggestion matched (the rest are shared)
  portalAlreadyShared  table names already on the portal (not shared again)
  welcomeEmail   "sent" | "already_sent" | "unavailable" | "would_send" | "not_attempted"

Selectors: docs/selectors/tn_portal.md (two read-only recons, 2026-10-07).
The typeahead's suggestion markup was not observable without typing, so a
suggestion is found by its EXACT text (whitespace/case-insensitive), never by a
guessed class. Logs carry counts and codes only.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

from .portal_documents import already_shared, candidates_for, norm, portal_documents_for

logger = logging.getLogger(__name__)

SEL = {
    "portal_tab": "a[href='#tab=Portal']",
    "welcome_send": "input[value='Send Welcome Email' i], button:text-is('Send Welcome Email')",
    "welcome_resend": "input[value='Resend Welcome Email' i], button:text-is('Resend Welcome Email')",
    "welcome_confirm": "input[value='Email address is correct' i], button:text-is('Email address is correct')",
    "shared_header": "a[data-testid='dynamictable-document-header']",
    "share_button": "#PortalDocumentsPage__ShareDocumentsButton",
    "dialog": "#PortalDocRequest",
    "request_view": "#SendNewRequestToPatientViewer__viewElem",
    "picker_input": "#PortalDocumentsSelector input.inline-selector-input",
    "send_button": "#SendButtonArea #SendButton",
    "dialog_close": "#PortalDocRequest button.DialogCloseButton",
}

# Names in the shared-documents table: the first cell of every row under the header.
_SHARED_NAMES_JS = r"""
(headerSel) => {
  const hdr = document.querySelector(headerSel);
  const table = hdr ? hdr.closest('table') : null;
  if (!table) return null;
  return [...table.querySelectorAll('tr')]
    .filter(tr => !tr.querySelector(headerSel))
    .map(tr => { const td = tr.querySelector('td'); return td ? td.innerText.replace(/\s+/g, ' ').trim() : ''; })
    .filter(Boolean);
}
"""

# Visible elements whose OWN text is exactly `want`, outside the shared table
# and outside the picker input. Marks the best one (an option-like element if
# any) with data-portal-pick so Playwright can click it. Returns the count.
_MARK_SUGGESTION_JS = r"""
([want, headerSel, viewSel]) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const target = norm(want);
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const hdr = document.querySelector(headerSel);
  const sharedTable = hdr ? hdr.closest('table') : null;
  document.querySelectorAll('[data-portal-pick]').forEach(e => e.removeAttribute('data-portal-pick'));
  const hits = [...document.querySelectorAll('body *')].filter(el => {
    if (!vis(el) || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return false;
    if (sharedTable && sharedTable.contains(el)) return false;
    // Anything that read this way BEFORE typing is page content, not a suggestion.
    if (el.hasAttribute('data-portal-pre')) return false;
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ');
    return norm(own) === target || (el.children.length === 0 && norm(el.innerText) === target);
  });
  if (!hits.length) return 0;
  const score = el => (el.getAttribute('role') === 'option' ? 3 : 0)
    + (/suggest|result|option|incremental|dropdown|menu/i.test(String(el.className) + ' ' + String((el.parentElement || {}).className)) ? 2 : 0)
    + (el.tagName === 'LI' || el.tagName === 'A' ? 1 : 0);
  hits.sort((a, b) => score(b) - score(a));
  hits[0].setAttribute('data-portal-pick', '1');
  return hits.length;
}
"""

# Tag every element that already reads exactly `want` before typing, so only
# what the typeahead adds can be picked.
_TAG_PRE_JS = r"""
(want) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const target = norm(want);
  document.querySelectorAll('[data-portal-pre]').forEach(e => e.removeAttribute('data-portal-pre'));
  for (const el of document.querySelectorAll('body *')) {
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ');
    if (norm(own) === target || (el.children.length === 0 && norm(el.innerText) === target)) el.setAttribute('data-portal-pre', '1');
  }
}
"""

# How many visible elements inside the request view read exactly `want` —
# the "Documents Included in Request" list grows by one when a pick lands.
_COUNT_IN_VIEW_JS = r"""
([want, viewSel]) => {
  const view = document.querySelector(viewSel);
  if (!view) return 0;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const target = norm(want);
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  return [...view.querySelectorAll('*')].filter(el => vis(el) && el.tagName !== 'INPUT'
    && el.tagName !== 'TEXTAREA' && el.children.length === 0 && norm(el.innerText) === target).length;
}
"""

# Leaf texts in the request view that are not the dialog's own labels/buttons:
# what is currently included in the request.
_INCLUDED_JS = r"""
(viewSel) => {
  const view = document.querySelector(viewSel);
  if (!view) return [];
  const skip = new Set(['select library documents to share:', 'documents included in request:',
    'instructions:', 'send document request']);
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const title = view.querySelector('h2');
  return [...view.querySelectorAll('*')].filter(el => vis(el) && el.children.length === 0
      && !(title && title.contains(el)) && el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA'
      && el.tagName !== 'OPTION' && el.tagName !== 'SELECT' && !el.closest('#SendButtonArea')
      && !el.closest('#PortalDocumentsSelector')
      && (el.innerText || '').trim() && !skip.has((el.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase()))
    .map(el => el.innerText.replace(/\s+/g, ' ').trim());
}
"""


def verdict(status: str, *, step: Optional[str] = None, reason: Optional[str] = None,
            documents: Optional[List[str]] = None, missing: Optional[List[str]] = None,
            already: Optional[List[str]] = None, welcome: str = "not_attempted",
            dry_run: Optional[bool] = None) -> Dict:
    return {
        "portalStatus": status,
        "portalStep": step,
        "portalReason": reason,
        "portalDocuments": list(documents or []),
        "portalMissing": list(missing or []),
        "portalAlreadyShared": list(already or []),
        "welcomeEmail": welcome,
        "portalDryRun": dry_run,
    }


def portal_message(v: Dict) -> str:
    """One line for the callback message: codes and counts only."""
    s = v.get("portalStatus")
    n = len(v.get("portalDocuments") or [])
    if s == "skipped":
        return f"Portal skipped ({v.get('portalReason')})"
    if s == "failed":
        return f"Portal failed at {v.get('portalStep')} ({v.get('portalReason')})"
    head = "Portal dry run: would share" if s == "dry_run" else "Portal done: shared"
    tail = f"; {len(v['portalMissing'])} not found in TherapyNotes" if v.get("portalMissing") else ""
    return f"{head} {n} document(s); welcome email {v.get('welcomeEmail')}{tail}"


class PortalStep:
    WAIT_S = 15

    def __init__(self, page, chart_url: Optional[str]):
        self.page = page
        self.chart_url = chart_url

    async def _visible(self, sel: str) -> bool:
        loc = self.page.locator(sel).first
        try:
            return await loc.count() > 0 and await loc.is_visible()
        except Exception:
            return False

    async def _wait_for(self, sel: str, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if await self._visible(sel):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _shared_names(self) -> Optional[List[str]]:
        try:
            return await self.page.evaluate(_SHARED_NAMES_JS, SEL["shared_header"])
        except Exception:
            return None

    async def _close_dialog(self) -> None:
        try:
            if await self._visible(SEL["dialog_close"]):
                await self.page.locator(SEL["dialog_close"]).first.click()
            else:
                await self.page.keyboard.press("Escape")
        except Exception:
            pass
        await asyncio.sleep(0.5)

    async def _blur(self) -> None:
        """Close the typeahead's dropdown by clicking the dialog's own title —
        never Escape, which may close the whole dialog and drop the picks."""
        try:
            title = self.page.locator(f"{SEL['request_view']} h2").first
            if await title.count() and await title.is_visible():
                await title.click()
        except Exception:
            pass

    async def _pick(self, name: str) -> Optional[str]:
        """Add one document to the request. Returns the TN name that matched, or None."""
        box = self.page.locator(SEL["picker_input"]).first
        for cand in candidates_for(name):
            try:
                before = await self.page.evaluate(_COUNT_IN_VIEW_JS, [cand, SEL["request_view"]])
                await self.page.evaluate(_TAG_PRE_JS, cand)
                await box.click()
                await box.fill("")
                await box.type(cand, delay=25)
                found = 0
                for _ in range(8):  # suggestions arrive asynchronously
                    await asyncio.sleep(0.5)
                    found = await self.page.evaluate(_MARK_SUGGESTION_JS, [cand, SEL["shared_header"], SEL["request_view"]])
                    if found:
                        break
                if found:
                    await self.page.locator("[data-portal-pick]").first.click()
                    await asyncio.sleep(0.8)
                # Clear the box and close any dropdown BEFORE counting, so an
                # open suggestion can never be mistaken for an included document.
                await box.fill("")
                await self._blur()
                await asyncio.sleep(0.5)
                if found:
                    after = await self.page.evaluate(_COUNT_IN_VIEW_JS, [cand, SEL["request_view"]])
                    if after > before:
                        return cand
            except Exception:
                continue
        return None

    async def run(self, service_type: Optional[str], skip_reason: Optional[str],
                  vaccn: bool, dry_run: bool) -> Dict:
        if not service_type:
            logger.info(f"[PORTAL] skipped reason={skip_reason or 'service_type_unmapped'}")
            return verdict("skipped", reason=skip_reason or "service_type_unmapped", dry_run=dry_run)
        wanted = portal_documents_for(service_type, vaccn)

        # --- Portal tab -----------------------------------------------------
        page = self.page
        if self.chart_url and self.chart_url.rstrip("/") not in (page.url or ""):
            try:
                await page.goto(self.chart_url, wait_until="domcontentloaded")
                await asyncio.sleep(2)
            except Exception:
                return verdict("failed", step="open_portal", reason="chart_not_reachable", dry_run=dry_run)
        if not await self._wait_for(SEL["portal_tab"], 10):
            return verdict("failed", step="open_portal", reason="portal_tab_not_found", dry_run=dry_run)
        await page.locator(SEL["portal_tab"]).first.click()
        ready = await self._wait_for(SEL["share_button"], self.WAIT_S)
        try:
            denied = await page.evaluate("() => /permission|not authorized|access denied/i.test(document.body.innerText || '')")
        except Exception:
            denied = False
        if denied:
            return verdict("failed", step="open_portal", reason="portal_permission_denied", dry_run=dry_run)
        if not ready:
            return verdict("failed", step="open_portal", reason="share_documents_not_found", dry_run=dry_run)

        shared = await self._shared_names() or []
        already = [d for d in wanted if already_shared(d, shared)]
        to_share = [d for d in wanted if d not in already]

        # --- Welcome email ----------------------------------------------------
        if await self._visible(SEL["welcome_resend"]):
            welcome = "already_sent"
        elif await self._visible(SEL["welcome_send"]):
            if dry_run:
                welcome = "would_send"
            else:
                await page.locator(SEL["welcome_send"]).first.click()
                if await self._wait_for(SEL["welcome_confirm"], 8):
                    await page.locator(SEL["welcome_confirm"]).first.click()
                if not await self._wait_for(SEL["welcome_resend"], self.WAIT_S):
                    logger.info("[PORTAL] welcome email not confirmed")
                    return verdict("failed", step="welcome_email", reason="welcome_not_confirmed",
                                   already=already, welcome="not_confirmed", dry_run=dry_run)
                welcome = "sent"
        else:
            welcome = "unavailable"   # e.g. no email on file: documents still shared
        logger.info(f"[PORTAL] welcome={welcome} shared_before={len(shared)} wanted={len(wanted)} "
                    f"to_share={len(to_share)} dry_run={'yes' if dry_run else 'no'}")

        if not to_share:
            return verdict("dry_run" if dry_run else "done", documents=[], already=already,
                           welcome=welcome, dry_run=dry_run)

        # --- Share documents: pick exactly the list ---------------------------
        await page.locator(SEL["share_button"]).first.click()
        if not await self._wait_for(SEL["picker_input"], self.WAIT_S):
            await self._close_dialog()
            return verdict("failed", step="share_documents", reason="picker_not_found",
                           already=already, welcome=welcome, dry_run=dry_run)
        picked: List[str] = []
        missing: List[str] = []
        for name in to_share:
            got = await self._pick(name)
            (picked.append(got) if got else missing.append(name))
        logger.info(f"[PORTAL] picked={len(picked)} missing={len(missing)}")

        if not picked:
            await self._close_dialog()
            return verdict("failed", step="tick_documents", reason="no_document_found",
                           missing=missing, already=already, welcome=welcome, dry_run=dry_run)

        # Exactly the list, nothing else, before anything is sent.
        try:
            included = await page.evaluate(_INCLUDED_JS, SEL["request_view"])
        except Exception:
            included = []
        extra = [t for t in included if norm(t) not in {norm(p) for p in picked}]
        if extra or len(included) < len(picked):
            await self._close_dialog()
            logger.info(f"[PORTAL] request list mismatch included={len(included)} picked={len(picked)} extra={len(extra)}")
            return verdict("failed", step="tick_documents", reason="request_list_mismatch",
                           documents=picked, missing=missing, already=already, welcome=welcome, dry_run=dry_run)

        if dry_run:
            await self._close_dialog()
            return verdict("dry_run", documents=picked, missing=missing, already=already,
                           welcome=welcome, dry_run=True)

        # --- Send and confirm -------------------------------------------------
        await page.locator(SEL["send_button"]).first.click()
        deadline = time.time() + self.WAIT_S
        confirmed = False
        while time.time() < deadline:
            await asyncio.sleep(1)
            now = await self._shared_names() or []
            if all(any(norm(p) == norm(n) for n in now) for p in picked):
                confirmed = True
                break
        if not confirmed:
            return verdict("failed", step="send_document_request", reason="not_confirmed",
                           documents=picked, missing=missing, already=already, welcome=welcome, dry_run=False)
        logger.info(f"[PORTAL] sent={len(picked)} confirmed=yes")
        return verdict("done", documents=picked, missing=missing, already=already, welcome=welcome, dry_run=False)


async def run_portal_phase(page, chart_url, patient) -> Dict:
    """The executor's entry point. Any exception becomes a failed verdict."""
    try:
        return await PortalStep(page, chart_url).run(
            getattr(patient, "service_type", None), getattr(patient, "portal_skip_reason", None),
            bool(getattr(patient, "payer_vaccn", False)), bool(getattr(patient, "portal_dry_run", True)))
    except Exception as e:
        logger.warning(f"[PORTAL] ended unexpectedly: {type(e).__name__}")
        return verdict("failed", step="unexpected", reason=type(e).__name__[:60],
                       dry_run=bool(getattr(patient, "portal_dry_run", True)))
