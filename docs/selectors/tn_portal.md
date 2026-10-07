# Portal tab — recon (2026-10-07, one read-only login, the practice's test chart)

Read only. Nothing was sent, typed or ticked. No patient data recorded here.

## Reaching it
- Chart tabs (hash tabs): `#tab=Patient+Info`, `#tab=To-Do`, `#tab=Schedule`,
  `#tab=Documents`, `#tab=Billing+Settings`, `#tab=Clinicians`, `#tab=Portal`,
  `#tab=Messages`.
- Portal tab: `a[href='#tab=Portal']`. After the swap the URL hash reads
  `tab=Portal&pdMode=5&pdPage=1&pdSort=60` (the portal documents table's paging/sort).
- No permission text on the tab for the agent's TherapyNotes user.

## Welcome email
- On a chart whose welcome email has ALREADY been sent, the control is an
  `input` button with value **"Resend Welcome Email"** (visible, enabled). No id.
- A hidden `input` with value **"Email address is correct"** is on the page —
  presumably the confirmation inside the welcome-email dialog.
- NOT SEEN: the never-sent state (expected "Send Welcome Email"), the dialog the
  button opens, and the post-send confirmation. The test chart had already been
  invited and the recon did not click it.

## Share documents
- Button: `#PortalDocumentsPage__ShareDocumentsButton` (`button.tn-button.tn-button-primary`),
  text **"Share Documents"**.
- Opens a dialog `#PortalDocRequest` (`.base-dialog-styles`), titled
  "Share Documents on <patient>'s Portal", close button `button.DialogCloseButton`.
- Inside it at +3s: `select#DocumentTypeSelect` (single-select) with options
  **All Documents · Library Files · Portal Forms · Patient Documents · Outcome Measures**,
  and one `textarea` (presumably the free-text message — never used: no free text).
- Second read-only look (2026-10-07, approved): polled 15s, then the
  "Portal Forms" and "All Documents" filters. Nothing ticked, typed or sent.
  - **Selection is a typeahead, not checkboxes.** `#PortalDocumentsSelector
    span.IncrementalSearchContainerNode input.inline-selector-input`
    (type=search), under the label "Select Library Documents to Share:".
    Documents are added by typing into it and choosing a suggestion; chosen ones
    appear under "Documents Included in Request:". No list is rendered until
    the box is typed in, so the suggestion markup was NOT observed (typing was
    not allowed in recon).
  - Instructions box: `textarea#documentRequestInstructionsInputArea` — never
    used (no free text).
  - Send control: `#SendButtonArea psy-button#SendButton` ("Send Document
    Request", a web component, `.tn-button-save`).
  - Close: `button.DialogCloseButton` inside `#PortalDocRequest`.
  - The dialog lives in `#PortalDocRequestView > #SendNewRequestToPatientViewer__viewElem`.
  - No iframes.

## Portal documents table (already-shared documents)
- Header link `a[data-testid='dynamictable-document-header']` ("Document").
- Rows link shared items; one seen links to `/app/library/customportalform/<id>/`,
  i.e. shared portal forms come from the practice's custom portal form library.
- Cells per row: [document name, date shared, date completed, status, ""].
  Status values seen: "Pending Submission", "Pending Signature",
  "Acknowledged by Practice on <date>".
- Names on the test chart (13): DAS · Parent Interview Under 14 · Informed Consent
  - Couples · Informed Consent - Families · Informed Consent - Adolescent ·
  Informed Consent for Treatment - Minor · Client History Form · Records Request ·
  PCP · Informed Consent for the Use of Artificial Intelligence in Services ·
  Emergency & Other Contacts Form · Electronic Communication Consent by
  Non-secure Transmission · Informed Consent - Adult.
- Idempotency compares the table's first cell to the document name (exact,
  whitespace-collapsed). Whether the picker's suggestions render the same
  string (notably "DAS") is unconfirmed until the first dry run.
