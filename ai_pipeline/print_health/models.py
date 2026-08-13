"""Typed domain objects for camera assessments, policy actions and revisions."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"


class Action(str, Enum):
    CONTINUE = "continue"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    RESTART = "restart"
    REPRINT = "reprint"
    EMERGENCY_STOP = "emergency_stop"
    ASK_USER = "ask_user"


class AutonomyMode(str, Enum):
    OBSERVE = "observe"
    ASSIST = "assist"
    AUTOPAUSE = "autopause"
    CLOSED_LOOP = "closed_loop"


DEFECTS = {
    "spaghetti",
    "detachment",
    "first_layer",
    "adhesion",
    "warping",
    "layer_shift",
    "stringing",
    "under_extrusion",
    "over_extrusion",
    "blob",
    "clog",
    "support_failure",
    "collision",
    "smoke_or_fire",
    "unknown",
}


@dataclass
class BoundingBox:
    """Normalized 0..1000 image coordinates."""

    x1: int = 0
    y1: int = 0
    x2: int = 1000
    y2: int = 1000

    @classmethod
    def from_value(cls, value: Any) -> "BoundingBox":
        if not isinstance(value, dict):
            return cls()
        vals = []
        for key, fallback in (("x1", 0), ("y1", 0), ("x2", 1000), ("y2", 1000)):
            try:
                parsed = int(value.get(key, fallback))
            except (TypeError, ValueError):
                parsed = fallback
            vals.append(max(0, min(1000, parsed)))
        x1, y1, x2, y2 = vals
        if x2 <= x1:
            x2 = min(1000, x1 + 1)
        if y2 <= y1:
            y2 = min(1000, y1 + 1)
        return cls(x1=x1, y1=y1, x2=x2, y2=y2)


@dataclass
class Detection:
    defect: str
    confidence: float
    severity: Severity
    bbox: BoundingBox = field(default_factory=BoundingBox)
    note: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "Detection | None":
        if not isinstance(value, dict):
            return None
        defect = str(value.get("defect", "unknown")).strip().lower()
        if defect not in DEFECTS:
            defect = "unknown"
        try:
            confidence = float(value.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        try:
            severity = Severity(str(value.get("severity", "warning")).lower())
        except ValueError:
            severity = Severity.WARNING
        return cls(
            defect=defect,
            confidence=confidence,
            severity=severity,
            bbox=BoundingBox.from_value(value.get("bbox")),
            note=str(value.get("note", ""))[:500],
        )


@dataclass
class ProviderAssessment:
    provider: str
    model: str
    quality_score: int
    confidence: float
    severity: Severity
    detections: list[Detection] = field(default_factory=list)
    recommended_action: Action = Action.CONTINUE
    diagnosis: str = ""

    @classmethod
    def from_json(cls, provider: str, model: str, payload: dict[str, Any]) -> "ProviderAssessment":
        try:
            quality = int(payload.get("quality_score", 100))
        except (TypeError, ValueError):
            quality = 100
        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            severity = Severity(str(payload.get("severity", "ok")).lower())
        except ValueError:
            severity = Severity.WARNING
        try:
            action = Action(str(payload.get("recommended_action", "continue")).lower())
        except ValueError:
            action = Action.CONTINUE
        detections: list[Detection] = []
        raw_detections = payload.get("detections", [])
        if isinstance(raw_detections, list):
            for item in raw_detections[:16]:
                parsed = Detection.from_value(item)
                if parsed is not None:
                    detections.append(parsed)
        return cls(
            provider=provider,
            model=model,
            quality_score=max(0, min(100, quality)),
            confidence=max(0.0, min(1.0, confidence)),
            severity=severity,
            detections=detections,
            recommended_action=action,
            diagnosis=str(payload.get("diagnosis", ""))[:2000],
        )


@dataclass
class FusedAssessment:
    quality_score: int
    confidence: float
    severity: Severity
    detections: list[Detection]
    recommended_action: Action
    providers: list[ProviderAssessment]
    provider_failures: list[str]
    rationale: str
    sample_interval_s: float

    def to_dict(self) -> dict[str, Any]:
        return _jsonify(asdict(self))


@dataclass
class ChangeSet:
    """Bounded slicer changes; values are deltas/multipliers, never raw G-code."""

    nozzle_temp_delta_c: int = 0
    bed_temp_delta_c: int = 0
    flow_multiplier: float = 1.0
    speed_multiplier: float = 1.0
    acceleration_multiplier: float = 1.0
    retraction_delta_mm: float = 0.0
    retraction_speed_delta_mm_s: float = 0.0
    first_layer_height_delta_mm: float = 0.0
    first_layer_speed_multiplier: float = 1.0
    brim_add_mm: float = 0.0
    raft_layers_add: int = 0
    supports: bool | None = None
    support_density_delta_pct: int = 0
    rotation_z_deg: int = 0
    notes: list[str] = field(default_factory=list)

    def bounded(self) -> "ChangeSet":
        return ChangeSet(
            nozzle_temp_delta_c=max(-15, min(15, int(self.nozzle_temp_delta_c))),
            bed_temp_delta_c=max(-10, min(10, int(self.bed_temp_delta_c))),
            flow_multiplier=max(0.90, min(1.10, float(self.flow_multiplier))),
            speed_multiplier=max(0.50, min(1.05, float(self.speed_multiplier))),
            acceleration_multiplier=max(0.50, min(1.00, float(self.acceleration_multiplier))),
            retraction_delta_mm=max(-0.8, min(0.8, float(self.retraction_delta_mm))),
            retraction_speed_delta_mm_s=max(-15.0, min(15.0, float(self.retraction_speed_delta_mm_s))),
            first_layer_height_delta_mm=max(-0.08, min(0.08, float(self.first_layer_height_delta_mm))),
            first_layer_speed_multiplier=max(0.50, min(1.00, float(self.first_layer_speed_multiplier))),
            brim_add_mm=max(0.0, min(10.0, float(self.brim_add_mm))),
            raft_layers_add=max(0, min(4, int(self.raft_layers_add))),
            supports=self.supports,
            support_density_delta_pct=max(-10, min(20, int(self.support_density_delta_pct))),
            rotation_z_deg=int(self.rotation_z_deg) if int(self.rotation_z_deg) in {0, 90, 180, 270} else 0,
            notes=[str(note)[:300] for note in self.notes[:20]],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.bounded())


@dataclass
class Revision:
    revision: int
    created_at: str
    reason: str
    defects: list[str]
    source_model: str
    base_profile: str
    output_gcode: str
    changes: ChangeSet
    previous_gcode: str = ""
    status: str = "generated"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["changes"] = self.changes.to_dict()
        return data


def _jsonify(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    return value
