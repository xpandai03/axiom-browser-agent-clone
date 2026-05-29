# TN V2 Phase Selectors (Recon Results)

Generated: 2026-05-28
Recon performed against: https://www.therapynotes.com/app/ (live production, practice `FamilyConnection505`, user `RaunekP`)
Existing pattern reference: `services/api/tn_executor.py:47-123` (the `SELECTORS` constant — candidate lists in priority order: data-testid > id > name > aria-label > text)
RECON patient used: **RECON Patient20260528** — `https://www.therapynotes.com/app/patients/edit/1L4JcJ5qscGPg3KTuHS86A/` (id `1L4JcJ5qscGPg3KTuHS86A`) — **needs manual deletion** (2 dummy PDFs attached, no appointment created).

> Method note: recon reused the existing `TNExecutor._phase_entry/_phase_login` to authenticate, then performed read-only DOM inspection. Two dummy-PDF uploads were performed on the RECON patient to confirm upload + sequential behavior (authorized). **No appointment was ever saved** — every scheduling pass closed the dialog via `button.DialogCloseButton` without clicking "Save New Appointment".

> Platform note: TN is an **ASP.NET WebForms SPA** (heavy `__VIEWSTATE`). Patient record uses **hash-tab navigation** (`#tab=Documents`). Element IDs on the patient form and appointment dialog are **stable** (`PatientFile__*`, `CalendarEntryEditor__*`); a few wrapper widgets use per-session GUID ids (`ApptAlertEventDescInput_<guid>`) — use attribute-prefix selectors for those.

---

## Phase: UPLOAD_INTAKE_PDF / UPLOAD_SNAPSHOT_PDF

Both uploads use the **same UI**, run on the saved patient record, and differ only in the Document Name typed. Sequence per file: open Documents tab → click "Upload Patient File" → choose file → wait for "Add Document" to enable → type Document Name → (dismiss autocomplete) → click "Add Document" → confirm success banner + new list row.

### 0. Pre-condition: reach the saved patient record + Documents tab
The patient must already be saved (the existing create flow lands here). Patient record URL pattern:
```
https://www.therapynotes.com/app/patients/edit/<PATIENT_ID>/
```
`<PATIENT_ID>` is the opaque token captured by the existing `_phase_save_patient` (e.g. `1L4JcJ5qscGPg3KTuHS86A`). The record page has hash tabs: **Info · To-Do · Schedule · Documents · Billing Settings · Clinicians · Portal · Messages**.

- Documents tab:
  ```
  "documents_tab": [
      "a[href='#tab=Documents']",
      "li:has-text('Documents') a",
  ]
  ```
- After the create flow the page may already be on `#tab=Info`; click the Documents tab (no full navigation needed — it's a hash tab, content swaps in ~1-2s).

### 1. Upload trigger
Visible text: **"Upload Patient File"** (blue button, top-right of the Documents pane). Opens a modal (not a new page).
```
"upload_patient_file_button": [
    "button:has-text('Upload Patient File')",
    "*:has-text('Upload Patient File')",
]
```
Pre-conditions: patient saved; Documents tab active. Always visible on the Documents tab. (There is also a "Create Note" split-button next to it — do not confuse.)

### 2. Upload modal
Clicking the trigger opens a modal titled **"Upload a Patient File"** (a `.Dialog`-style overlay; close via `button.DialogCloseButton`). Modal contains, in order: Patient (read-only label), Date, File (Choose File), Document Name, Staff Access (radios), and the action buttons.

### 3. File picker
Real native file input (works with Playwright `set_input_files` even though it renders as a "Choose File" button — it is `visible`):
```
"file_input": [
    "#InputUploader",
    "input[type=file][name='InputUploader']",
    "input[type=file]",
]
```
- Visible "Choose File" is the browser-native rendering of `#InputUploader` (no separate styled button to click — just `set_input_files` on the input).
- **Accepted formats:** no `accept` attribute on the input (native picker accepts anything); TN validates server-side. PDFs accepted (confirmed).
- **Max file size:** not surfaced in the UI (no visible text / attribute). Unknown — mark for server-side handling.
- **Drag-and-drop:** present in UI but **not used** (Lane: bugs out ~20%). Use `set_input_files` on `#InputUploader`.
- After a file is chosen, the modal shows `<icon> <filename>` with a remove "✕"; a remove control exists (`✕` next to the filename) if a wrong file needs clearing.

### 4. Document Name field — **FREE-TEXT INPUT (resolved)**
This was the key open question. **It is a free-text `<input>`**, `maxlength=128`, **with a non-binding autocomplete dropdown** of common names (Driver's License, HIPAA NPP, ID Card, PCP Release Consent Form, Patient Photo, Registration Form, Registration Packet, Release of Information, Reminder Call Release, Service Agreement, + the uploaded file's basename). You may type **any** value; the suggestions do **not** constrain input.
```
"document_name_input": [
    "#PatientFile__DocumentName",
    "input[maxlength='128']",
]
```
- Values needed by V2 (type verbatim — neither is in the suggestion list):
  - `"Intake Referral"` for the intake PDF
  - `"Initial Appointment Confirmation Email"` for the snapshot PDF
- **Quirk:** typing opens the autocomplete overlay, which can intercept the click on "Add Document". After `.fill()`, press **Escape** (or click a neutral area) to dismiss the overlay before clicking submit.

### 5. Staff Access (this is the "category"/`documentType`) — radios
The field Lane called "Category → Admin" is actually labeled **"Staff Access"** with three radios (`name="documentType"`). **Default selected = Administrative** (confirmed checked). No separate "Category" field exists.
```
"staff_access_admin_radio":    ["#InputAdminDocumentType",    "input[name='documentType']#InputAdminDocumentType"],
"staff_access_billing_radio":  ["#InputBillingDocumentType"],
"staff_access_clinical_radio": ["#InputClinicalDocumentType"],
```
- Default verification (no action needed): `input[name='documentType']:checked` → `#InputAdminDocumentType`.

### 6. Date field
**Defaults to today** (observed `5/28/2026` on 2026-05-28). Format `m/d/yyyy`, `maxlength=20`. No action needed.
```
"document_date_input": ["#PatientFile__Date", "input[placeholder='m/d/yyyy']"]
```

### 7. Submit ("Add Document") button
Visible text: **"Add Document"** (green). It is an `<input type=button>`. **It is `disabled` until the chosen file finishes processing** — this is the main timing gotcha (a too-early click times out on "element is not enabled"). There can be a disabled duplicate in the DOM; target the **visible + enabled** one.
```
"add_document_button": [
    "input[value='Add Document']:not([disabled])",
    "input[value='Add Document']",
]
```
- **Recommended wait:** poll for the enabled button before clicking, e.g. `_poll_condition` on `input[value='Add Document']` having no `disabled` property (observed enable within ~0.5-1s of `set_input_files`).
- Cancel: `input[value='Cancel Upload']`. Modal close: `button.DialogCloseButton`.

### 8. Success indicator
After "Add Document": modal closes, a green banner appears, and a row is added to the list.
```
"upload_success_banner": ["div.standard-banner-message"]   # text: "Document <name> (PDF <size>) has been added"
```
Poll target for `_poll_condition`: either the banner above, OR the new row appearing in the document list (see §9). The "Notes and Documents for this Patient:" header changes from **"None"** to listing the docs.

### 9. Document list rows (for verification)
```
"document_list_rows":      ["tr.Row", "tr.AlternateRow"]            # one per document
"document_name_link":      ["a.documentNameLinkIcon"]               # clickable doc name
"document_row_cell":       ["td.v-align-top"]                       # holds "<Name>PDF <size>"
```
Each row text looks like: `Intake Referral` · `PDF 1KB` · `5/28/2026` · `Administrative`. To verify a specific upload landed, check for a row whose text contains the Document Name.

### 10. Error states
Not all could be force-triggered read-only. Selectors to watch (mirror the existing executor's validation scraping):
- Generic validation / alert containers: `.validation-summary-errors`, `.alert-danger`, `[role='alert']`, `.standard-banner-message` (also used for success — check text).
- File too large / unsupported format: surfaced as a banner/inline message after "Add Document" (exact text not captured — TN validates server-side). Step 3 should treat "no success banner AND no new row within timeout" as failure (`pdf_upload_ui_not_found` / `intake_pdf_upload_failed` / `snapshot_pdf_upload_failed`).

### 11. Sequential upload behavior — **works, no reload**
After the first upload completes (modal closes, banner shows), the second upload can start **immediately**: click **"Upload Patient File"** again on the same Documents tab. No page navigation/reload required. Confirmed both files present in the list after back-to-back uploads.

---

## Phase: SCHEDULE_APPOINTMENT

> ⚠️ PHI: the Scheduling page defaults to **"Agenda for All Clinicians"** and shows *all* patients' appointments and free-text agenda notes. This is **unavoidable PHI exposure** on this surface. See "PHI considerations" below — recommend gating any scheduling screenshots behind `TN_DEBUG_MODE`.

### 1. Navigate to Scheduling
URL: `https://www.therapynotes.com/app/scheduling/`. Sidebar link:
```
"scheduling_nav": ["a[href='/app/scheduling/']", "a:has-text('Scheduling')"]
```
(Reliable to `page.goto` the URL directly after login.)

### 2. Calendar view
View is selected by tab links (`<li>` tabs, each an `<a href="#">`). Default = **Agenda**. Options: **Agenda · Day · Week · Month**.
```
"calendar_view_day":    ["a:has-text('Day')"],
"calendar_view_week":   ["a:has-text('Week')"],
"calendar_view_month":  ["a:has-text('Month')"],
"calendar_view_agenda": ["a:has-text('Agenda')"],
```
- **Recommended view for the agent:** view is largely irrelevant because appointment creation uses the global "+ New" dialog (below) rather than clicking a grid slot. If clinician auto-assignment is wanted (see §6), use **Week** or **Day** and click the target clinician's time slot instead. Default Agenda is fine for the dialog-driven approach.

### 3. Visible-clinicians / clinician calendar selection
Lane's "little calendar next to someone" = the **"Set Calendar View"** control, which chooses which clinicians' calendars are displayed.
```
"set_calendar_view_button": ["#ctl00_BodyContent_SchedulingCalendarControl_ButtonEditVisibleCalendars",
                             "input[value='Set Calendar View']"]
"displayed_clinicians_link": ["a:has-text('for All Clinicians')"]   # shows current scope ("Agenda for All Clinicians")
```
This controls *display scope*, not the appointment's clinician. The appointment's clinician is set inside the dialog (§6).

### 4. Time-slot navigation
Not required for the dialog-driven flow (date/time are typed in the dialog — §6). If a grid-slot approach is used later: date headers render as `a.calendar-agendaView-dateLink` ("Thursday 5/28 Today", "Friday 5/29 Tomorrow", …); the custom calendar root is `.tca-calendar*`. No native prev/next `<select>`; navigation is via these custom links. **Recommendation: prefer typing date/time in the dialog over grid-slot clicking** (far more deterministic).

### 5. New-appointment trigger — global "+ New"
```
"new_appointment_button": ["#ButtonCreateAppointment", "psy-button:has-text('+ New')"]
```
Opens the **"Create New Appointment"** dialog (overlay; close via `button.DialogCloseButton`). This is a single global button — there is no per-row "+" needed for the dialog flow.

### 6. Appointment form fields (dialog)
All core fields have stable `CalendarEntryEditor__*` ids.

| Field | Type | Selector | Required | Default / Notes |
|---|---|---|---|---|
| Patient | search autocomplete | `input#CalendarEntryEditor__PatientSelect` (placeholder "name or ID of existing patient") | yes | none — **existing patients only** (see §12) |
| Patient result item | dropdown item | `.IncrementalSearchContainerNode .ContentBubble.IncrementalSearch` (text: "`<Name>` DOB: `m/d/yyyy`") | — | click the item matching the patient |
| New-patient (inline) | button | `input#CalendarEntryEditor__NewPatient-Button` ("+ New") | no | not used by V2 (patient already created) |
| Appointment Type | `<select>` | `select#CalendarEntryEditor__TypeSelect` | yes | **"Therapy Intake" = value `"0"`** (first option) |
| Telehealth (Modality) | checkbox | `input#CalendarEntryEditor__TelehealthCheckbox` | no | unchecked = In-Person; check = Telehealth |
| Service Code | `<select>` | `select#CalendarEntryEditor__ServiceCodeSelect` | auto | auto-set from Appointment Type |
| Start Date | text | `input#CalendarEntryEditor__StartDateInput` (`m/d/yyyy`) | yes | — |
| Start Time | text | `input#CalendarEntryEditor__StartTimeInput` (`h:mm am`) | yes | — |
| Duration | text | `input#CalendarEntryEditor__DurationInput` (`maxlength=4`) | auto/yes | minutes; defaults from type |
| Frequency | `<select>` | `select#CalendarEntryEditor__FrequencySelect` | no | default **"One time" = value `"0"`** |
| Clinician | DynamicDropdown | `#CalendarEntryEditor__ClinicianSelect` (inner `input.DynamicInputTextBox`) | yes | **does NOT auto-fill via global "+ New" for an unassigned patient** — see ⚠️ below |
| Location | DynamicDropdown | row containing label "Location" (`input.DynamicInputTextBox`) | auto | **auto-populated** ("Corporate IN PERSON" observed) |
| **Appointment Alert** | **textarea** | **`textarea#CalendarEntryEditor__RemindersTextArea`** | no | free-text + autocomplete; label is "Appointment Alert / Event Description" (id is misleadingly named "Reminders") |

### 7. "Therapy Intake" selection mechanic
Native `<select>` → select by value:
```
page.select_option("#CalendarEntryEditor__TypeSelect", value="0")   # "Therapy Intake"
```
Exact label as displayed: **"Therapy Intake"** (value `0`). Full option list: Therapy Intake(0), Therapy Session(1), Consultation(7), Group Therapy(4), Psychological Evaluation(5), Scheduled Event(6), Unavailable(8), Extra Availability(9).

### 8. Appointment Alert text field
Plain `<textarea>` — `.fill()` works directly. Free-text with non-binding incremental-search suggestions (past alerts like "Checked in", "Advance cxl - patient is out of town", etc.).
```
"appointment_alert_textarea": ["#CalendarEntryEditor__RemindersTextArea",
                               "textarea[name='CalendarEntryEditor__RemindersTextArea']"]
```
- Character limit: none observed (no `maxlength`).
- Format to write: `"New {Type} {Modality} Therapy CRM"` (CRM passes this pre-formatted per the Step-2 spec).

### 9. Save button
Visible text: **"Save New Appointment"** (`<input type=button>`).
```
"save_appointment_button": ["#CalendarEntryEditor__Create-Button", "input[value='Save New Appointment']"]
```
(Not exercised in recon — no appointment was created.)

### 10. Success indicator (inferred — NOT verified, no appointment saved)
By analogy to the upload flow and TN conventions: dialog closes + the appointment bubble (`.EntryBubble` / `.calendarentry-text-primary`) appears on the calendar, possibly a `.standard-banner-message`. **Step 3 must confirm the exact success selector at runtime** (first real save).

### 11. Error states
- Required-field / validation: `.validation-summary-errors`, `.field-validation-error`, `.input-validation-error`, `[role='alert']` (same families as the patient-save flow).
- Time-slot conflict (double-book) and missing-field messages: not triggered in recon — **mark unverified**; capture on first Step-3 run.

### 12. Patient existence pre-check — **confirmed**
The patient search is explicitly **"name or ID of existing patient"**; a new patient must be created via the inline "+ New". Since V2 creates the patient in the earlier phases, the patient **will** exist and be searchable by the time scheduling runs (RECON patient was found by typing its name → exactly 1 result). Recommendation: if the patient search returns **0 results** for the just-created patient, fail with `scheduling_ui_not_found`/`appointment_creation_failed` rather than proceeding (indicates the patient didn't persist or a search-index delay).

---

## Phase ordering implications

- **Patient creation (existing) → Document upload 1:** can proceed directly. After save, the page is on the patient record (`/patients/edit/<id>/`); just click the **Documents** hash-tab. No navigation/login needed. ✅
- **Document upload 1 → Document upload 2:** direct, **no reload**. Re-click "Upload Patient File" on the same tab. ✅
- **Document upload 2 → Scheduling:** requires a context switch to `/app/scheduling/` (sidebar link or `page.goto`). Same session — no re-login. The patient is now searchable in the appointment dialog. ✅

---

## UI quirks / observations

- **ASP.NET WebForms / `__VIEWSTATE`:** heavy hidden state; rely on stable element ids, not DOM position.
- **Hash-tab navigation** on the patient record (`#tab=Documents`) — content swaps without a full nav; wait ~1-2s after clicking the tab.
- **"Add Document" disabled until file processed** — the #1 timing trap. Poll for enabled before clicking.
- **Autocomplete overlays** on both Document Name and Appointment Alert can intercept subsequent clicks — dismiss with Escape after filling.
- **Custom DynamicDropdown widgets** (Clinician, Location): not native `<select>`; they render view-mode text and reveal an inner `input.DynamicInputTextBox` on interaction. Location auto-fills; clinician may not (see below).
- **Per-session GUID ids** on some wrappers (`ApptAlertEventDescInput_<guid>`) — the actual editable element underneath is the stable `#CalendarEntryEditor__RemindersTextArea`, so target that, not the GUID wrapper.
- **Clinician auto-assignment is conditional (IMPORTANT):** with the global **"+ New"** dialog on an *unassigned* patient, the Clinician field did **not** auto-populate (stayed "clinician for selected appointment type"). Lane's "clinician auto-assigns" behavior appears to require either (a) initiating the appointment from a specific clinician's calendar slot, or (b) the patient already having an Assigned Clinician. **Step 3 must decide:** set the clinician explicitly in the dialog, or initiate from the target clinician's calendar grid. This diverges from the verbal walkthrough and is the biggest scheduling unknown.
- **No iframes** on either the Documents modal or the Scheduling dialog (good — no frame-switching needed).
- Session-warning/blocking dialogs: the existing `_dismiss_blocking_dialogs()` helper is still relevant; reuse it before clicks on both surfaces.

---

## PHI considerations for screenshots

| Surface | PHI in screenshot? | Recommendation for Step 3 |
|---|---|---|
| Patient record / Documents tab | Single current patient only (same as existing save screenshots) | Same gating as existing executor (`TN_DEBUG_MODE` for authenticated post-save shots) |
| Upload modal | Current patient name + uploaded filename | Low risk; gate with `TN_DEBUG_MODE` to be safe |
| **Scheduling agenda/calendar** | **YES — all patients' appointments + free-text notes** | **Gate behind `TN_DEBUG_MODE`. Do not screenshot the calendar grid/agenda by default.** |
| Appointment dialog | Selected patient + (calendar visible behind) | Gate behind `TN_DEBUG_MODE`; prefer screenshotting just the dialog, not full_page |
| Patient search dropdown | Could list other patients matching the query | Use exact/unique search terms; gate screenshots |

Recon screenshots were written to the gitignored `screenshots/recon/` directory and are **not** committed.

---

## Recommendations for Step 3

- **New `TNPhaseV2` values** (per Step-2 spec): `UPLOAD_INTAKE_PDF`, `UPLOAD_SNAPSHOT_PDF`, `SCHEDULE_APPOINTMENT`. **New `TNFailureReasonV2` values:** `pdf_download_failed`, `pdf_unsupported_format`, `pdf_upload_ui_not_found`, `intake_pdf_upload_failed`, `snapshot_pdf_upload_failed`, `appointment_slot_unavailable`, `scheduling_ui_not_found`, `appointment_creation_failed`. (Do not add in Step 1; this is Step 3.)
- **Add a `SELECTORS_V2` block** in `tn_executor_v2.py` mirroring the existing `SELECTORS` dict shape (`tn_executor.py:47-123`). The candidate lists above drop in directly.
- **Upload helper:** download each PDF (from `intake_pdf_url`/`snapshot_pdf_url`) to a temp path, then `set_input_files("#InputUploader", path)`. **Poll for `input[value='Add Document']` to be enabled** before clicking. Press Escape after filling Document Name. Verify via `div.standard-banner-message` or a new `tr.Row`/`tr.AlternateRow` containing the name.
- **Document names are exact strings:** `"Intake Referral"`, `"Initial Appointment Confirmation Email"` (free-text — type verbatim, do not rely on suggestions).
- **Staff Access / Date defaults are correct** (Administrative / today) — no action needed, but Step 3 may assert them for safety (`input[name='documentType']:checked == #InputAdminDocumentType`).
- **Scheduling:** use the dialog-driven flow (global "+ New" + typed date/time) over grid-slot clicking. Select type via `select_option(value="0")`. Set Telehealth checkbox per modality. Fill `#CalendarEntryEditor__RemindersTextArea` with the alert text.
- **Resolve clinician assignment first thing in Step 3** — it's the one behavior that didn't match the walkthrough. Either select the clinician in `#CalendarEntryEditor__ClinicianSelect` explicitly (needs the DynamicDropdown interaction mapped), or pre-assign the clinician on the patient record, or initiate from the clinician's calendar slot. Recommend a short spike on this before building the full phase.
- **Success/error selectors for scheduling are unverified** (no appointment was saved) — capture them on the first real Step-3 run and update this doc.
- **Pure utilities** in `tn_executor.py` (`_dismiss_blocking_dialogs`, `_safe_click`, `_poll_condition`, `_resolve_selector`, `_capture_screenshot`) are all directly reusable for the new phases — they were exercised throughout recon and work on both surfaces. (Per Step-1 lock B5 these remain cloned inside `TNExecutorV2`, not shared.)
- **`set_input_files` works on hidden/native file inputs** — no need to handle the visual "Choose File" button or drag-and-drop.
