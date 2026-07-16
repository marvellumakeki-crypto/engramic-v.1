from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import SESSION_COOKIE, authenticate, create_session, create_user, get_current_user, require_role
from graph_engine import STORE, get_full_graph
from schemas import Allergy, Condition, Facility, LabResult, Medication, Patient

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _redirect_for(role: str) -> str:
    return "/doctor/dashboard" if role == "doctor" else "/patient/dashboard"


def _context(request: Request, **values):
    return {"request": request, "user": get_current_user(request), **values}


def _render(request: Request, name: str, status_code: int = 200, **values):
    return templates.TemplateResponse(request=request, name=name, context=_context(request, **values), status_code=status_code)


def _all_patients() -> list[Patient]:
    return sorted((node for node in STORE.nodes.values() if isinstance(node, Patient)), key=lambda p: p.name)


def _patient_data(patient_id: str) -> dict:
    graph = get_full_graph(patient_id)
    patient = next((n for n in graph.nodes if isinstance(n, Patient)), None)
    medications = [n for n in graph.nodes if isinstance(n, Medication)]
    conditions = [n for n in graph.nodes if isinstance(n, Condition)]
    labs = sorted((n for n in graph.nodes if isinstance(n, LabResult)), key=lambda n: n.date or date.min, reverse=True)
    allergies = [n for n in graph.nodes if isinstance(n, Allergy)]
    facilities = [n for n in graph.nodes if isinstance(n, Facility)]
    records = sorted(
        [record for node in graph.nodes for record in node.provenance_records],
        key=lambda record: record.encounter_date or date.min,
        reverse=True,
    )
    return {"patient": patient, "medications": medications, "conditions": conditions, "labs": labs,
            "allergies": allergies, "facilities": facilities, "records": records}


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    return _render(request, "home.html")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(_redirect_for(user.role), status_code=303)
    return _render(request, "auth.html", mode="login", error=None)


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    user = authenticate(email, password)
    if not user:
        return _render(request, "auth.html", status_code=400, mode="login", error="Email or password is incorrect.")
    response = RedirectResponse(_redirect_for(user.role), status_code=303)
    response.set_cookie(SESSION_COOKIE, create_session(user), httponly=True, samesite="lax", secure=os.environ.get("ENGRAMIC_SECURE_COOKIES") == "true", max_age=604800)
    return response


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return _render(request, "auth.html", mode="register", error=None)


@router.post("/register")
def register(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...), role: str = Form("patient"), invite_code: str = Form("")):
    error = None
    if role not in {"doctor", "patient"}:
        error = "Choose a valid account type."
    elif len(password) < 8:
        error = "Use at least 8 characters for your password."
    elif role == "doctor" and invite_code != os.environ.get("ENGRAMIC_DOCTOR_INVITE_CODE", "ENGRAMIC-DEMO"):
        error = "The clinician invite code is invalid."
    if error:
        return _render(request, "auth.html", status_code=400, mode="register", error=error)
    patient_id = None
    if role == "patient":
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "patient"
        patient_id = f"patient_{slug}_{uuid4().hex[:5]}"
    try:
        user = create_user(name, email, password, role, patient_id)
    except ValueError as exc:
        return _render(request, "auth.html", status_code=400, mode="register", error=str(exc))
    if role == "patient" and patient_id:
        STORE.add_patient(Patient(id=patient_id, name=name))
    response = RedirectResponse(_redirect_for(user.role), status_code=303)
    response.set_cookie(SESSION_COOKIE, create_session(user), httponly=True, samesite="lax", secure=os.environ.get("ENGRAMIC_SECURE_COOKIES") == "true", max_age=604800)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/doctor/dashboard", response_class=HTMLResponse)
def doctor_dashboard(request: Request):
    user = require_role(request, "doctor")
    patients = _all_patients()
    conflicts = sum(1 for node in STORE.nodes.values() if getattr(node, "conflict_flag", False))
    active_medications = sum(1 for node in STORE.nodes.values() if isinstance(node, Medication) and node.status.value == "active")
    return _render(request, "doctor_dashboard.html", user=user, patients=patients, conflicts=conflicts, active_medications=active_medications)


@router.get("/doctor/patients", response_class=HTMLResponse)
def doctor_patients(request: Request, q: str = ""):
    user = require_role(request, "doctor")
    patients = [p for p in _all_patients() if q.lower() in p.name.lower() or q.lower() in p.id.lower()]
    return _render(request, "patients.html", user=user, patients=patients, query=q)


@router.get("/doctor/patients/{patient_id}", response_class=HTMLResponse)
def doctor_patient_detail(request: Request, patient_id: str):
    user = require_role(request, "doctor")
    return _render(request, "patient_detail.html", user=user, **_patient_data(patient_id))


@router.get("/doctor/notes/new", response_class=HTMLResponse)
def doctor_note_new(request: Request, patient_id: str = ""):
    user = require_role(request, "doctor")
    return _render(request, "note_new.html", user=user, patients=_all_patients(), selected_patient=patient_id)


@router.get("/doctor/referral-summary", response_class=HTMLResponse)
def doctor_referral(request: Request, patient_id: str = ""):
    user = require_role(request, "doctor")
    return _render(request, "referral.html", user=user, patients=_all_patients(), selected_patient=patient_id)


def _patient_page(request: Request, template: str, title: str):
    user = require_role(request, "patient")
    return _render(request, template, user=user, title=title, **_patient_data(user.patient_id))


@router.get("/patient/dashboard", response_class=HTMLResponse)
def patient_dashboard(request: Request): return _patient_page(request, "patient_dashboard.html", "Your health overview")


@router.get("/patient/medications", response_class=HTMLResponse)
def patient_medications(request: Request): return _patient_page(request, "patient_section.html", "Medications")


@router.get("/patient/conditions", response_class=HTMLResponse)
def patient_conditions(request: Request): return _patient_page(request, "patient_section.html", "Conditions")


@router.get("/patient/history", response_class=HTMLResponse)
def patient_history(request: Request): return _patient_page(request, "patient_section.html", "Health history")
