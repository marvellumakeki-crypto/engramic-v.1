from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from auth import authorize_patient_access, initialize_auth, require_role
from extraction import extract_clinical_entities, extract_pdf_document, get_ocr_status
from graph_engine import STORE, get_active_subgraph, get_full_graph, merge_entities
from schemas import Allergy, Condition, DocumentExtractionResponse, ExtractRequest, Facility, LabResult, Medication, MergeRequest, Patient, PatientCreate
from web import router as web_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = FastAPI(title="Engramic API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
app.include_router(web_router)
initialize_auth()
MAX_UPLOAD_BYTES = int(os.environ.get("ENGRAMIC_MAX_UPLOAD_MB", "10")) * 1024 * 1024


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ocr")
def ocr_health():
    return get_ocr_status()


@app.get("/upload", response_class=HTMLResponse, include_in_schema=False)
def upload_page(request: Request) -> str:
    require_role(request, "doctor")
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Engramic PDF Intake</title>
  <style>
    :root { color-scheme: light; font-family: Inter, system-ui, sans-serif; color: #14322b; background: #f4f8f6; }
    body { margin: 0; padding: 32px 18px; }
    main { max-width: 820px; margin: auto; }
    h1 { margin-bottom: 6px; } p { color: #536761; }
    .fields { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 24px 0; }
    label { font-size: 13px; font-weight: 700; }
    input { box-sizing: border-box; width: 100%; margin-top: 6px; padding: 11px; border: 1px solid #b8c9c3; border-radius: 9px; }
    #drop { padding: 42px 20px; text-align: center; border: 2px dashed #3b8b74; border-radius: 16px; background: white; cursor: pointer; }
    #drop.over { background: #e5f5ef; border-color: #176c55; }
    button { margin-top: 16px; padding: 12px 18px; border: 0; border-radius: 9px; background: #176c55; color: white; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .5; cursor: wait; }
    pre { white-space: pre-wrap; word-break: break-word; padding: 18px; border-radius: 12px; background: #10251f; color: #dff7ed; min-height: 44px; }
    .notice { padding: 12px 14px; border-radius: 9px; background: #fff5cf; color: #624e00; font-size: 14px; }
    @media (max-width: 650px) { .fields { grid-template-columns: 1fr; } }
  </style>
</head>
<body><main>
  <h1>Engramic PDF Intake</h1>
  <p>Drop a clinical PDF to extract a review preview. Nothing is merged into the patient graph automatically.</p>
  <div class="notice">Printed PDFs work immediately. Scanned PDFs require Tesseract OCR to report ready at <code>/health/ocr</code>.</div>
  <div class="fields">
    <label>Patient ID<input id="patient" value="patient_budi" required></label>
    <label>Note ID<input id="note" value="pdf_note_1" required></label>
    <label>Patient name<input id="name" value="Pak Budi"></label>
  </div>
  <div id="drop" role="button" tabindex="0" aria-label="Drop PDF here or choose a file">
    <strong id="fileLabel">Drop PDF here</strong><br><span>or click to choose a file (maximum 10 MB)</span>
    <input id="file" type="file" accept="application/pdf,.pdf" hidden>
  </div>
  <button id="extract" disabled>Extract for review</button>
  <h2>Preview</h2><pre id="result">No document processed yet.</pre>
  <script>
    const drop = document.querySelector('#drop'), file = document.querySelector('#file');
    const button = document.querySelector('#extract'), result = document.querySelector('#result');
    let selected;
    function choose(f) {
      if (!f || (f.type && f.type !== 'application/pdf') || !f.name.toLowerCase().endsWith('.pdf')) {
        result.textContent = 'Please choose a PDF file.'; return;
      }
      selected = f; document.querySelector('#fileLabel').textContent = f.name; button.disabled = false;
    }
    drop.onclick = () => file.click(); drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') file.click(); };
    file.onchange = () => choose(file.files[0]);
    for (const event of ['dragenter','dragover']) drop.addEventListener(event, e => { e.preventDefault(); drop.classList.add('over'); });
    for (const event of ['dragleave','drop']) drop.addEventListener(event, e => { e.preventDefault(); drop.classList.remove('over'); });
    drop.addEventListener('drop', e => choose(e.dataTransfer.files[0]));
    button.onclick = async () => {
      const patient = document.querySelector('#patient').value.trim(), note = document.querySelector('#note').value.trim();
      if (!selected || !patient || !note) { result.textContent = 'Patient ID, note ID, and PDF are required.'; return; }
      const data = new FormData(); data.append('document', selected); data.append('patient_id', patient); data.append('note_id', note);
      data.append('patient_name', document.querySelector('#name').value.trim());
      button.disabled = true; button.textContent = 'Extracting...'; result.textContent = 'Reading PDF and preparing clinical preview...';
      try {
        const response = await fetch('/documents/extract', { method: 'POST', body: data });
        const body = await response.json(); result.textContent = JSON.stringify(body, null, 2);
        if (!response.ok) result.textContent = `Error ${response.status}\n` + result.textContent;
      } catch (error) { result.textContent = 'Upload failed: ' + error; }
      finally { button.disabled = false; button.textContent = 'Extract for review'; }
    };
  </script>
</main></body></html>"""


@app.post("/patients", response_model=Patient)
def create_patient(body: PatientCreate, request: Request) -> Patient:
    require_role(request, "doctor")
    if body.id in STORE.nodes:
        raise HTTPException(409, "Patient ID already exists")
    return STORE.add_patient(Patient(id=body.id, name=body.name, age=body.age, gender=body.gender))


@app.post("/notes/extract")
def extract_note(body: ExtractRequest, request: Request):
    require_role(request, "doctor")
    return extract_clinical_entities(body.raw_text, body.note_id, body.patient_name)


@app.post("/documents/extract")
async def extract_document(
    request: Request,
    document: UploadFile = File(..., description="PDF clinical document"),
    patient_id: str = Form(...),
    note_id: str = Form(...),
    patient_name: str | None = Form(None),
) -> DocumentExtractionResponse:
    """Upload/drop a PDF and return OCR + entity previews without modifying the graph."""
    require_role(request, "doctor")
    if patient_id not in STORE.nodes or not isinstance(STORE.nodes[patient_id], Patient):
        raise HTTPException(404, "Patient not found")
    if document.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(415, "Only PDF files are supported")
    payload = await document.read(MAX_UPLOAD_BYTES + 1)
    await document.close()
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"PDF exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    try:
        return extract_pdf_document(payload, document.filename or "uploaded.pdf", note_id, patient_name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        logging.exception("PDF extraction failed safely")
        raise HTTPException(422, f"PDF extraction failed: {exc}") from exc


@app.post("/notes/merge")
def merge_note(body: MergeRequest, request: Request):
    require_role(request, "doctor")
    try:
        return merge_entities(body.patient_id, body.entities, body.note_id, body.facility_id, body.encounter_date, body.reviewed_by, body.condition_links, body.extraction_method, body.document_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        logging.exception("Merge failed safely")
        raise HTTPException(422, f"Merge rejected: {exc}") from exc


@app.get("/patients/{patient_id}/graph")
def graph(patient_id: str, request: Request):
    authorize_patient_access(request, patient_id)
    if patient_id not in STORE.nodes:
        raise HTTPException(404, "Patient not found")
    return get_full_graph(patient_id)


def _patient_view(patient_id: str) -> str:
    graph = get_full_graph(patient_id)
    active = [f"{n.name} {n.dose or ''}".strip() for n in graph.nodes if isinstance(n, Medication) and n.status.value == "active"]
    stopped = [f"{n.name} {n.dose or ''}".strip() for n in graph.nodes if isinstance(n, Medication) and n.status.value == "discontinued"]
    allergies = [n.substance for n in graph.nodes if isinstance(n, Allergy) and n.status == "active"]
    return " ".join([
        f"Obat Anda saat ini: {', '.join(active) if active else 'belum tercatat'}.",
        f"Obat yang sudah berhenti: {', '.join(stopped) if stopped else 'tidak ada yang tercatat'}.",
        f"Alergi yang tercatat: {', '.join(allergies) if allergies else 'tidak ada'}.",
        "Pastikan informasi ini bersama dokter atau petugas kesehatan Anda.",
    ])


@app.get("/patients/{patient_id}/patient-view")
def patient_view(patient_id: str, request: Request) -> dict[str, str]:
    authorize_patient_access(request, patient_id)
    if patient_id not in STORE.nodes:
        raise HTTPException(404, "Patient not found")
    return {"summary": _patient_view(patient_id)}


def _deterministic_referral(patient_id: str) -> str:
    full = get_full_graph(patient_id)
    patient = next((n for n in full.nodes if isinstance(n, Patient)), None)
    conditions = [n.name for n in full.nodes if isinstance(n, Condition) and n.status.value in {"active", "suspected"}]
    active = [f"{n.name} {n.dose or ''} {n.frequency or ''}".strip() for n in full.nodes if isinstance(n, Medication) and n.status.value == "active"]
    stopped = [f"{n.name} {n.dose or ''}".strip() for n in full.nodes if isinstance(n, Medication) and n.status.value == "discontinued"]
    conflicts = [r for n in full.nodes for r in getattr(n, "conflict_reasons", [])]
    provenance = sorted({p for n in full.nodes for p in n.provenance})
    return f"SURAT RUJUKAN\n\nPasien: {patient.name if patient else patient_id}\nMasalah aktif: {', '.join(conditions) or 'Belum tercatat'}\nObat saat ini: {', '.join(active) or 'Belum tercatat'}\nObat dihentikan: {', '.join(stopped) or 'Tidak ada yang tercatat'}\nKonflik terbuka: {'; '.join(conflicts) or 'Tidak ada'}\nSumber: {', '.join(provenance) or 'Tidak tersedia'}\n\nHarap verifikasi ringkasan ini terhadap catatan sumber dan penilaian klinis sebelum digunakan untuk keputusan medis."


async def _summary_stream(patient_id: str) -> AsyncIterator[str]:
    fallback = _deterministic_referral(patient_id)
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"), timeout=5.0, max_retries=0)
        snapshot = get_full_graph(patient_id).model_dump(mode="json")
        stream = await client.chat.completions.create(model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), messages=[{"role": "system", "content": "Write a concise Indonesian clinical referral letter using only the verified graph snapshot. Separate active and discontinued medications, cite note IDs inline, list conflicts, and end with a verification disclaimer."}, {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False)}], stream=True, store=False, temperature=0)
        async for chunk in stream:
            text = chunk.choices[0].delta.content or ""
            if text: yield text
    except Exception as exc:
        logging.warning("Summary LLM unavailable; deterministic fallback used: %s", exc)
        for line in fallback.splitlines(keepends=True):
            yield line
            await asyncio.sleep(0)


@app.get("/patients/{patient_id}/summary")
def summary(patient_id: str, request: Request):
    authorize_patient_access(request, patient_id)
    if patient_id not in STORE.nodes:
        raise HTTPException(404, "Patient not found")
    return StreamingResponse(_summary_stream(patient_id), media_type="text/plain; charset=utf-8")
