from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Iterable
from uuid import uuid4

from rapidfuzz import fuzz, process

from schemas import (
    Allergy, Condition, Conflict, Edge, ExtractedAllergy, ExtractedCondition,
    ExtractedEntity, ExtractedLab, ExtractedMedication, Facility, FacilityType,
    GraphResponse, LabResult, Medication, MergeResult, Node, Patient,
    ProvenanceRecord, StateHistory,
)

LOGGER = logging.getLogger("engramic.graph")
BASE_DIR = Path(__file__).resolve().parent
GRAPH_FILE = Path(os.environ.get("ENGRAMIC_GRAPH_FILE", BASE_DIR / "graph_data.json"))

DRUG_SYNONYMS = {
    "amlodipin": "Amlodipine", "amlodipine": "Amlodipine", "norvasc": "Amlodipine",
    "lisinopril": "Lisinopril", "zestril": "Lisinopril", "prinivil": "Lisinopril",
    "captopril": "Captopril", "capoten": "Captopril", "enalapril": "Enalapril",
    "losartan": "Losartan", "cozaar": "Losartan", "valsartan": "Valsartan",
    "diovan": "Valsartan", "metformin": "Metformin", "glucophage": "Metformin",
    "glibenklamid": "Glibenclamide", "glibenclamide": "Glibenclamide",
    "simvastatin": "Simvastatin", "atorvastatin": "Atorvastatin", "lipitor": "Atorvastatin",
    "aspirin": "Aspirin", "asetosal": "Aspirin", "clopidogrel": "Clopidogrel",
    "plavix": "Clopidogrel", "furosemide": "Furosemide", "furosemid": "Furosemide",
    "lasix": "Furosemide", "bisoprolol": "Bisoprolol", "concor": "Bisoprolol",
    "paracetamol": "Paracetamol", "parasetamol": "Paracetamol", "panadol": "Paracetamol",
    "amoxicillin": "Amoxicillin", "amoksisilin": "Amoxicillin", "omeprazole": "Omeprazole",
    "omeprazol": "Omeprazole", "salbutamol": "Salbutamol", "ventolin": "Salbutamol",
}


def normalize_drug(name: str) -> str:
    return DRUG_SYNONYMS.get(name.strip().casefold(), name.strip().title())


def _node_key(node: Node) -> str:
    if isinstance(node, Medication): return node.name
    if isinstance(node, Condition): return node.name
    if isinstance(node, LabResult): return node.test_name
    if isinstance(node, Allergy): return node.substance
    return getattr(node, "name", node.id)


class GraphStore:
    def __init__(self, path: Path = GRAPH_FILE):
        self.path = path
        self.lock = RLock()
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.patient_nodes: dict[str, set[str]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._seed()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            response = GraphResponse.model_validate({"nodes": data["nodes"], "edges": data["edges"]})
            self.nodes = {node.id: node for node in response.nodes}
            self.edges = response.edges
            self.patient_nodes = {k: set(v) for k, v in data.get("patient_nodes", {}).items()}
        except Exception as exc:
            LOGGER.error("Graph load failed; starting seeded graph: %s", exc)
            self._seed()

    def _seed(self) -> None:
        patient = Patient(id="patient_budi", name="Pak Budi", age=54, gender="male")
        facilities = [
            Facility(id="facility_puskesmas", name="Puskesmas NTT", type=FacilityType.PUSKESMAS),
            Facility(id="facility_rsud", name="RSUD Kabupaten", type=FacilityType.RSUD),
            Facility(id="facility_specialist", name="Klinik Spesialis", type=FacilityType.SPECIALIST),
        ]
        self.nodes = {patient.id: patient, **{f.id: f for f in facilities}}
        self.edges = []
        self.patient_nodes = {patient.id: {patient.id}}
        self.save()

    def save(self) -> None:
        payload = GraphResponse(nodes=list(self.nodes.values()), edges=self.edges).model_dump(mode="json")
        payload["patient_nodes"] = {k: sorted(v) for k, v in self.patient_nodes.items()}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def add_patient(self, patient: Patient) -> Patient:
        with self.lock:
            self.nodes[patient.id] = patient
            self.patient_nodes.setdefault(patient.id, set()).add(patient.id)
            self.save()
        return patient


STORE = GraphStore()


def _nodes_for(patient_id: str, cls: type) -> list[Node]:
    return [STORE.nodes[n] for n in STORE.patient_nodes.get(patient_id, set()) if isinstance(STORE.nodes.get(n), cls)]


def _find_match(patient_id: str, entity: ExtractedEntity) -> tuple[Node | None, float]:
    cls = {"condition": Condition, "medication": Medication, "lab": LabResult, "allergy": Allergy}[entity.entity_type]
    candidates = _nodes_for(patient_id, cls)
    incoming = normalize_drug(entity.name) if entity.entity_type == "medication" else entity.name.strip()
    exact = next((n for n in candidates if _node_key(n).casefold() == incoming.casefold()), None)
    if exact: return exact, 100.0
    if not candidates: return None, 0.0
    result = process.extractOne(incoming, [_node_key(n) for n in candidates], scorer=fuzz.WRatio)
    if not result: return None, 0.0
    name, score, _ = result
    return next(n for n in candidates if _node_key(n) == name), float(score)


def _provenance(note_id: str, facility_id: str | None, encounter_date: date | None, evidence: str, reviewed_by: str | None, extraction_method: str, document_id: str | None, page: int | None) -> ProvenanceRecord:
    return ProvenanceRecord(note_id=note_id, document_id=document_id, page=page, facility_id=facility_id, encounter_date=encounter_date, evidence_text=evidence, extraction_method=extraction_method, reviewed_by=reviewed_by, reviewed_at=datetime.now(timezone.utc) if reviewed_by else None)


def _wire(patient_id: str, node: Node, note_id: str, condition_id: str | None = None) -> None:
    relation = {"condition": "DIAGNOSED_WITH", "lab": "RECEIVED_LAB", "allergy": "HAS_ALLERGY"}.get(node.node_type)
    source = patient_id
    if node.node_type == "medication":
        relation = "TREATED_BY"
        if not condition_id:
            LOGGER.warning("Medication %s has no unambiguous condition link; leaving it unlinked", node.id)
            return
        source = condition_id
    if relation and not any(e.source == source and e.target == node.id and e.relation == relation for e in STORE.edges):
        STORE.edges.append(Edge(source=source, relation=relation, target=node.id, provenance=[note_id]))


def _is_ambiguous_transition(existing_date: date | None, incoming_date: date | None, explicit: bool) -> bool:
    if explicit and incoming_date and (not existing_date or incoming_date >= existing_date): return False
    return not explicit or incoming_date is None


def merge_entities(patient_id: str, entities: Iterable[ExtractedEntity], note_id: str, facility_id: str | None = None, encounter_date: date | None = None, reviewed_by: str | None = None, condition_links: dict[str, str] | None = None, extraction_method: str = "manual_review", document_id: str | None = None) -> MergeResult:
    if patient_id not in STORE.nodes or not isinstance(STORE.nodes[patient_id], Patient):
        raise KeyError(f"Unknown patient: {patient_id}")
    result = MergeResult()
    condition_links = condition_links or {}
    # Conditions must exist before medications can be linked to them.
    ordered_entities = sorted(list(entities), key=lambda item: {"condition": 0, "medication": 1, "allergy": 2, "lab": 3}[item.entity_type])
    with STORE.lock:
        snapshot = deepcopy((STORE.nodes, STORE.edges, STORE.patient_nodes))
        try:
            if facility_id and facility_id in STORE.nodes:
                STORE.patient_nodes.setdefault(patient_id, set()).add(facility_id)
                _wire_visit(patient_id, facility_id, note_id)
            for entity in ordered_entities:
                if entity.negated:
                    result.skipped.append({"entity": entity.model_dump(mode="json"), "reason": "negated finding"})
                    continue
                match, score = _find_match(patient_id, entity)
                if match is not None and 85 <= score < 95:
                    result.review_required.append({"entity": entity.model_dump(mode="json"), "candidate_node_id": match.id, "candidate_name": _node_key(match), "score": score, "reason": "fuzzy match requires confirmation; edit name to canonical value to merge"})
                    continue
                prov = _provenance(note_id, facility_id, encounter_date, entity.raw_text_span, reviewed_by, extraction_method, document_id, entity.source_page)
                if match is None or score < 85:
                    node = _create_node(entity, note_id, prov, encounter_date)
                    STORE.nodes[node.id] = node
                    STORE.patient_nodes.setdefault(patient_id, set()).add(node.id)
                    condition_id = condition_links.get(entity.name)
                    if isinstance(node, Medication) and not condition_id:
                        active_conditions = _nodes_for(patient_id, Condition)
                        condition_id = active_conditions[0].id if len(active_conditions) == 1 else None
                    _wire(patient_id, node, note_id, condition_id)
                    result.created.append(node)
                else:
                    conflict = _update_node(match, entity, note_id, prov, encounter_date)
                    if conflict:
                        result.conflicts.append(conflict)
                    result.updated.append(match)
            STORE.save()
        except Exception:
            STORE.nodes, STORE.edges, STORE.patient_nodes = snapshot
            raise
    return result


def _wire_visit(patient_id: str, facility_id: str, note_id: str) -> None:
    edge = next((e for e in STORE.edges if e.source == patient_id and e.target == facility_id and e.relation == "VISITED"), None)
    if edge and note_id not in edge.provenance: edge.provenance.append(note_id)
    elif not edge: STORE.edges.append(Edge(source=patient_id, relation="VISITED", target=facility_id, provenance=[note_id]))


def _create_node(entity: ExtractedEntity, note_id: str, prov: ProvenanceRecord, encounter_date: date | None) -> Node:
    common = {"id": str(uuid4()), "provenance": [note_id], "provenance_records": [prov]}
    if isinstance(entity, ExtractedCondition):
        return Condition(**common, name=entity.name.title(), status=entity.status, date=entity.date or encounter_date)
    if isinstance(entity, ExtractedMedication):
        return Medication(**common, name=normalize_drug(entity.name), dose=entity.dose, frequency=entity.frequency, route=entity.route, reason=entity.reason, prescriber=entity.prescriber, status=entity.status, start_date=entity.start_date or (encounter_date if entity.status.value == "active" else None), end_date=entity.end_date or (encounter_date if entity.status.value == "discontinued" else None))
    if isinstance(entity, ExtractedLab):
        return LabResult(**common, test_name=entity.name.title(), value=entity.value, unit=entity.unit, date=entity.date or encounter_date)
    return Allergy(**common, substance=entity.name.title(), reaction=entity.reaction, status=entity.status, date=entity.date or encounter_date)


def _update_node(node: Node, entity: ExtractedEntity, note_id: str, prov: ProvenanceRecord, encounter_date: date | None) -> Conflict | None:
    conflict = None
    if note_id not in node.provenance: node.provenance.append(note_id)
    node.provenance_records.append(prov)
    if isinstance(node, Medication) and isinstance(entity, ExtractedMedication):
        if node.status != entity.status:
            if _is_ambiguous_transition(node.end_date or node.start_date, entity.end_date or entity.start_date or encounter_date, entity.explicit_state_change):
                reason = "Contradictory medication states without a clear, dated state-change instruction"
                node.conflict_flag = True; node.conflict_reasons.append(reason)
                conflict = Conflict(node_id=node.id, entity_name=node.name, field="status", existing_value=node.status.value, incoming_value=entity.status.value, reason=reason, note_ids=list(node.provenance))
            else:
                node.history.append(StateHistory(status=node.status.value, dose=node.dose, frequency=node.frequency, route=node.route, changed_at=encounter_date, note_id=note_id))
                node.status = entity.status
                if entity.status.value == "discontinued": node.end_date = entity.end_date or encounter_date
                else: node.start_date = entity.start_date or encounter_date; node.end_date = None
        for field in ("dose", "frequency", "route", "reason", "prescriber"):
            value = getattr(entity, field)
            if value is not None: setattr(node, field, value)
    elif isinstance(node, Condition) and isinstance(entity, ExtractedCondition):
        if node.status != entity.status and _is_ambiguous_transition(node.date, entity.date or encounter_date, entity.explicit_state_change):
            reason = "Contradictory condition states without clear temporal order"
            node.conflict_flag = True; node.conflict_reasons.append(reason)
            conflict = Conflict(node_id=node.id, entity_name=node.name, field="status", existing_value=node.status.value, incoming_value=entity.status.value, reason=reason, note_ids=list(node.provenance))
        elif node.status != entity.status: node.status = entity.status; node.date = entity.date or encounter_date
    elif isinstance(node, Allergy) and isinstance(entity, ExtractedAllergy):
        node.reaction = entity.reaction or node.reaction
    return conflict


def get_full_graph(patient_id: str) -> GraphResponse:
    ids = STORE.patient_nodes.get(patient_id, set())
    nodes = [STORE.nodes[i] for i in ids if i in STORE.nodes]
    edges = [e for e in STORE.edges if e.source in ids or e.target in ids]
    return GraphResponse(nodes=nodes, edges=edges)


def get_active_subgraph(patient_id: str) -> GraphResponse:
    full = get_full_graph(patient_id)
    nodes = [n for n in full.nodes if not (isinstance(n, Medication) and n.status.value != "active") and not (isinstance(n, Condition) and n.status.value not in {"active", "suspected"}) and not (isinstance(n, Allergy) and n.status != "active")]
    ids = {n.id for n in nodes}
    return GraphResponse(nodes=nodes, edges=[e for e in full.edges if e.source in ids and e.target in ids])
