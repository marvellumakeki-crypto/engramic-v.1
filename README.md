# Engramic full-stack clinical record

Engramic is now a role-aware web application built around the original review-first, stateful clinical knowledge graph. The LLM proposes structured facts; a clinician reviews them before the deterministic merge endpoint changes graph state.

## Run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
$env:ENGRAMIC_FORCE_CACHE="true"
uvicorn app:app --reload
```

Then open `http://127.0.0.1:8000`.

### Demo accounts

| Role | Email | Password |
| --- | --- | --- |
| Doctor | `doctor@engramic.id` | `Doctor123!` |
| Patient | `patient@engramic.id` | `Patient123!` |

Replace the session secret and clinician invite code in `.env` before any shared or production deployment. Set `ENGRAMIC_SECURE_COOKIES=true` behind HTTPS.

## Web pages

- Public: `/`, `/login`, `/register`
- Doctor: `/doctor/dashboard`, `/doctor/patients`, `/doctor/patients/{patientId}`, `/doctor/notes/new`, `/doctor/referral-summary`
- Patient: `/patient/dashboard`, `/patient/medications`, `/patient/conditions`, `/patient/history`

Authentication uses signed, HTTP-only session cookies and scrypt password hashing. Role and patient ownership checks run server-side. Patients can only access the graph assigned to their account; clinical extraction, merging, PDF intake, and patient creation require a doctor session.

Set `OPENAI_API_KEY` and change `ENGRAMIC_FORCE_CACHE=false` for live extraction. Demo cache mode is recommended on stage.

## Demo flow

Extract Note 1 without merging:

```bash
curl -X POST http://127.0.0.1:8000/notes/extract -H "Content-Type: application/json" -d '{"patient_id":"patient_budi","note_id":"note_1","patient_name":"Pak Budi","raw_text":"Pak Budi, 54 tahun. Didiagnosis hipertensi. Mulai Amlodipin 5 mg sekali sehari oral. Pasien menyangkal nyeri dada."}'
```

Review/edit that response in the UI, then send its `entities` to merge:

```bash
curl -X POST http://127.0.0.1:8000/notes/merge -H "Content-Type: application/json" -d '{"patient_id":"patient_budi","note_id":"note_1","facility_id":"facility_puskesmas","encounter_date":"2026-07-14","reviewed_by":"demo_clinician","entities":[{"entity_type":"condition","name":"Hypertension","status":"active","date":"2026-07-14","negated":false,"discontinued":false,"explicit_state_change":false,"raw_text_span":"hipertensi"},{"entity_type":"medication","name":"Amlodipin","dose":"5 mg","frequency":"once daily","route":"oral","reason":"Hypertension","prescriber":null,"status":"active","start_date":"2026-07-14","end_date":null,"negated":false,"discontinued":false,"explicit_state_change":true,"raw_text_span":"Mulai Amlodipin 5 mg sekali sehari"}]}'
```

Inspect the result:

```bash
curl http://127.0.0.1:8000/patients/patient_budi/graph
curl http://127.0.0.1:8000/patients/patient_budi/patient-view
curl -N http://127.0.0.1:8000/patients/patient_budi/summary
```

Interactive API documentation is available at `http://127.0.0.1:8000/docs`.

## PDF upload and OCR

`POST /documents/extract` accepts a PDF as multipart form data and returns a review preview. It never mutates the graph. The pipeline first extracts embedded PDF text; pages with fewer than 40 characters automatically fall back to Tesseract OCR. The returned `document_id`, page method, OCR confidence, warnings, scrubbed page text, and `extraction.entities` can be shown in the doctor correction screen.

With the backend running, open `http://127.0.0.1:8000/upload` for the built-in drag-and-drop PDF intake page.

Install the Tesseract executable separately for scanned PDFs, then verify it is available:

```powershell
tesseract --version
```

If it is not on `PATH`, set `TESSERACT_CMD` in `.env`. Install Indonesian trained data for `eng+ind`; otherwise the backend automatically retries OCR with English.

Check OCR readiness at `GET /health/ocr`. A `ready: false` response does not affect typed PDFs; it only means scanned pages cannot be read until Tesseract is installed.

Upload a PDF in Swagger at `/docs`, or use:

```bash
curl -X POST http://127.0.0.1:8000/documents/extract \
  -F "patient_id=patient_budi" \
  -F "note_id=pdf_note_1" \
  -F "patient_name=Pak Budi" \
  -F "document=@clinical-note.pdf;type=application/pdf"
```

After the clinician edits and approves `extraction.entities`, send those entities to `POST /notes/merge`. Include the returned `document_id` and set `extraction_method` to `pdf_text` or `ocr` so graph provenance remains accurate.

## Important boundaries

- Regex PII scrubbing is a demo safeguard, not by itself UU PDP compliance.
- Fuzzy scores from 85â€“94 require user confirmation; only exact/canonical or very-high-confidence matches auto-merge.
- Contradictory undated states are flagged instead of resolved by AI.
- Original history and rich provenance are retained; state updates never delete prior state.
- Advanced patient matching, consent, deletion/correction workflows, FHIR, and production terminology services remain future work.
