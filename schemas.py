from __future__ import annotations

from datetime import date as DateType
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConditionStatus(str, Enum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    SUSPECTED = "suspected"


class MedicationStatus(str, Enum):
    ACTIVE = "active"
    DISCONTINUED = "discontinued"


class FacilityType(str, Enum):
    PUSKESMAS = "puskesmas"
    RSUD = "rsud"
    SPECIALIST = "specialist"


class ProvenanceRecord(StrictModel):
    note_id: str
    document_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    facility_id: str | None = None
    encounter_date: DateType | None = None
    evidence_text: str | None = None
    extraction_method: str = "llm"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


class StateHistory(StrictModel):
    status: str
    dose: str | None = None
    frequency: str | None = None
    route: str | None = None
    changed_at: DateType | None = None
    note_id: str


class GraphNode(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    node_type: str
    provenance: list[str] = Field(default_factory=list)
    provenance_records: list[ProvenanceRecord] = Field(default_factory=list)


class Patient(GraphNode):
    node_type: Literal["patient"] = "patient"
    name: str
    age: int | None = Field(default=None, ge=0, le=130)
    gender: str | None = None


class Condition(GraphNode):
    node_type: Literal["condition"] = "condition"
    name: str
    status: ConditionStatus
    date: DateType | None = None
    conflict_flag: bool = False
    conflict_reasons: list[str] = Field(default_factory=list)


class Medication(GraphNode):
    node_type: Literal["medication"] = "medication"
    name: str
    dose: str | None = None
    frequency: str | None = None
    route: str | None = None
    reason: str | None = None
    prescriber: str | None = None
    status: MedicationStatus
    start_date: DateType | None = None
    end_date: DateType | None = None
    history: list[StateHistory] = Field(default_factory=list)
    conflict_flag: bool = False
    conflict_reasons: list[str] = Field(default_factory=list)


class LabResult(GraphNode):
    node_type: Literal["lab"] = "lab"
    test_name: str
    value: str
    unit: str | None = None
    date: DateType | None = None


class Allergy(GraphNode):
    node_type: Literal["allergy"] = "allergy"
    substance: str
    reaction: str | None = None
    status: Literal["active", "inactive"] = "active"
    date: DateType | None = None
    conflict_flag: bool = False
    conflict_reasons: list[str] = Field(default_factory=list)


class Facility(GraphNode):
    node_type: Literal["facility"] = "facility"
    name: str
    type: FacilityType


Node = Annotated[
    Patient | Condition | Medication | LabResult | Allergy | Facility,
    Field(discriminator="node_type"),
]


class Edge(StrictModel):
    source: str
    relation: Literal[
        "DIAGNOSED_WITH", "TREATED_BY", "RECEIVED_LAB", "VISITED", "HAS_ALLERGY"
    ]
    target: str
    provenance: list[str] = Field(default_factory=list)


class ClinicalNote(StrictModel):
    note_id: str
    patient_id: str
    facility_id: str | None = None
    encounter_date: DateType | None = None
    raw_text_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ExtractedCondition(StrictModel):
    entity_type: Literal["condition"] = "condition"
    name: str
    status: ConditionStatus = ConditionStatus.ACTIVE
    date: DateType | None = None
    negated: bool = False
    discontinued: bool = False
    explicit_state_change: bool = False
    raw_text_span: str
    source_page: int | None = Field(default=None, ge=1)


class ExtractedMedication(StrictModel):
    entity_type: Literal["medication"] = "medication"
    name: str
    dose: str | None = None
    frequency: str | None = None
    route: str | None = None
    reason: str | None = None
    prescriber: str | None = None
    status: MedicationStatus = MedicationStatus.ACTIVE
    start_date: DateType | None = None
    end_date: DateType | None = None
    negated: bool = False
    discontinued: bool = False
    explicit_state_change: bool = False
    raw_text_span: str
    source_page: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def align_discontinued_status(self) -> "ExtractedMedication":
        if self.discontinued:
            self.status = MedicationStatus.DISCONTINUED
            self.explicit_state_change = True
        return self


class ExtractedLab(StrictModel):
    entity_type: Literal["lab"] = "lab"
    name: str
    value: str
    unit: str | None = None
    date: DateType | None = None
    negated: bool = False
    discontinued: bool = False
    explicit_state_change: bool = False
    raw_text_span: str
    source_page: int | None = Field(default=None, ge=1)


class ExtractedAllergy(StrictModel):
    entity_type: Literal["allergy"] = "allergy"
    name: str
    reaction: str | None = None
    status: Literal["active", "inactive"] = "active"
    date: DateType | None = None
    negated: bool = False
    discontinued: bool = False
    explicit_state_change: bool = False
    raw_text_span: str
    source_page: int | None = Field(default=None, ge=1)


ExtractedEntity = Annotated[
    ExtractedCondition | ExtractedMedication | ExtractedLab | ExtractedAllergy,
    Field(discriminator="entity_type"),
]


class ExtractionResult(StrictModel):
    entities: list[ExtractedEntity]
    warnings: list[str] = Field(default_factory=list)
    source: Literal["live_llm", "cache", "rule_based"] = "live_llm"
    scrubbed_text: str | None = None


class ExtractRequest(StrictModel):
    raw_text: str = Field(min_length=1)
    patient_id: str
    note_id: str
    patient_name: str | None = None
    facility_id: str | None = None
    encounter_date: DateType | None = None


class MergeRequest(StrictModel):
    patient_id: str
    note_id: str
    entities: list[ExtractedEntity]
    facility_id: str | None = None
    encounter_date: DateType | None = None
    reviewed_by: str | None = None
    extraction_method: Literal["llm", "cache", "rule_based", "ocr", "pdf_text", "manual_review"] = "manual_review"
    document_id: str | None = None
    condition_links: dict[str, str] = Field(default_factory=dict)


class DocumentPage(StrictModel):
    page: int = Field(ge=1)
    method: Literal["embedded_text", "tesseract_ocr"]
    text: str
    character_count: int = Field(ge=0)
    ocr_confidence: float | None = Field(default=None, ge=0, le=100)
    warnings: list[str] = Field(default_factory=list)


class DocumentExtractionResponse(StrictModel):
    document_id: str
    filename: str
    content_type: str
    size_bytes: int = Field(ge=0)
    page_count: int = Field(ge=1)
    used_ocr: bool
    pages: list[DocumentPage]
    extraction: ExtractionResult
    warnings: list[str] = Field(default_factory=list)


class Conflict(StrictModel):
    node_id: str
    entity_name: str
    field: str
    existing_value: str
    incoming_value: str
    reason: str
    note_ids: list[str]


class MergeResult(StrictModel):
    created: list[Node] = Field(default_factory=list)
    updated: list[Node] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    review_required: list[dict[str, Any]] = Field(default_factory=list)


class GraphResponse(StrictModel):
    nodes: list[Node]
    edges: list[Edge]


class PatientCreate(StrictModel):
    id: str
    name: str
    age: int | None = Field(default=None, ge=0, le=130)
    gender: str | None = None
