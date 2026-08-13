"""Print-defect taxonomy, severity model, and multi-model fusion.

The taxonomy is not a flat list of names. Each defect carries three properties
that decide what the agent is allowed to do about it, and getting these right
matters more than detection accuracy does:

``urgency``
    How much damage continuing causes. Spaghetti and a detached part waste
    filament and can bury the nozzle, so continuing is actively harmful.
    Stringing is cosmetic - the part will finish, and the fix belongs in the
    *next* slice, not in an interruption.

``recoverable_by_pause``
    Whether stopping helps. Pausing a print with poor bed adhesion lets someone
    rescue it. Pausing a print with stringing achieves nothing except a blob
    where the nozzle sat, so the agent must not "helpfully" pause for it.

``fix_hint``
    Which slicing parameters plausibly address it, consumed by
    :mod:`thox.revise`. Kept next to the defect so a new defect kind cannot be
    added without someone deciding what to do about it.

**On confidence numbers.** Providers report 0..1, but the scales are *not*
comparable across providers and are not calibrated probabilities. A VLM's 0.9
means "confidently phrased", not "90% of such frames are failures". Calibration
would need labelled captures from this specific machine and camera. That is why
auto-pause ships disabled, why a confirmation count is required, and why the
severity that reaches the operator is dominated by the defect kind rather than
by a model's self-report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Urgency(str, Enum):
    """What continuing to print costs."""

    #: Continuing damages the machine or wastes the remaining print.
    CRITICAL = "critical"
    #: The part is likely lost, but nothing is being damaged.
    SERIOUS = "serious"
    #: The part will finish; quality is affected.
    COSMETIC = "cosmetic"
    #: Not about the print at all.
    OBSERVATIONAL = "observational"

    @property
    def rank(self) -> int:
        return {
            Urgency.OBSERVATIONAL: 0,
            Urgency.COSMETIC: 1,
            Urgency.SERIOUS: 2,
            Urgency.CRITICAL: 3,
        }[self]


class DefectKind(str, Enum):
    """Failure modes the ensemble can report."""

    SPAGHETTI = "spaghetti"
    DETACHMENT = "detachment"
    PRINT_CAME_LOOSE = "print_came_loose"
    FIRST_LAYER = "first_layer"
    ADHESION = "adhesion"
    WARPING = "warping"
    LAYER_SHIFT = "layer_shift"
    STRINGING = "stringing"
    UNDER_EXTRUSION = "under_extrusion"
    OVER_EXTRUSION = "over_extrusion"
    BLOB = "blob"
    NOZZLE_CLOG = "nozzle_clog"
    #: The observation itself is unusable (dark, blurred, camera down).
    CAMERA_FAULT = "camera_fault"

    @property
    def urgency(self) -> Urgency:
        return _PROFILE[self].urgency

    @property
    def recoverable_by_pause(self) -> bool:
        return _PROFILE[self].recoverable_by_pause

    @property
    def severity_floor(self) -> float:
        """Minimum severity this kind carries even at low confidence."""
        return _PROFILE[self].severity_floor

    @property
    def label(self) -> str:
        return _PROFILE[self].label

    @property
    def fix_hint(self) -> tuple[str, ...]:
        return _PROFILE[self].fix_hint

    @property
    def is_print_failure(self) -> bool:
        """False for faults about the observation rather than the print."""
        return self is not DefectKind.CAMERA_FAULT


@dataclass(frozen=True)
class _Profile:
    label: str
    urgency: Urgency
    recoverable_by_pause: bool
    severity_floor: float
    fix_hint: tuple[str, ...]


_PROFILE: dict[DefectKind, _Profile] = {
    DefectKind.SPAGHETTI: _Profile(
        "Spaghetti / extrusion into air",
        Urgency.CRITICAL,
        True,
        0.85,
        ("bed_temperature", "first_layer_speed", "adhesion_type", "supports"),
    ),
    DefectKind.DETACHMENT: _Profile(
        "Part detached from the bed",
        Urgency.CRITICAL,
        True,
        0.85,
        ("bed_temperature", "adhesion_type", "first_layer_speed", "first_layer_flow"),
    ),
    DefectKind.PRINT_CAME_LOOSE: _Profile(
        "Print came loose and is being dragged",
        Urgency.CRITICAL,
        True,
        0.9,
        ("adhesion_type", "bed_temperature", "first_layer_speed"),
    ),
    DefectKind.FIRST_LAYER: _Profile(
        "First layer is not laying down correctly",
        Urgency.SERIOUS,
        True,
        0.6,
        ("z_offset", "first_layer_flow", "first_layer_speed", "bed_temperature"),
    ),
    DefectKind.ADHESION: _Profile(
        "Poor bed adhesion",
        Urgency.SERIOUS,
        True,
        0.65,
        ("bed_temperature", "adhesion_type", "first_layer_speed", "z_offset"),
    ),
    DefectKind.WARPING: _Profile(
        "Corners lifting / warping",
        Urgency.SERIOUS,
        # Pausing does not un-warp a corner, and the nozzle will collide with it
        # either way. The value is alerting a human, not stopping.
        False,
        0.55,
        ("bed_temperature", "chamber_temperature", "adhesion_type", "cooling"),
    ),
    DefectKind.LAYER_SHIFT: _Profile(
        "Layer shift",
        Urgency.CRITICAL,
        # The geometry is already wrong; every further layer is wasted.
        True,
        0.8,
        ("print_speed", "travel_speed", "acceleration"),
    ),
    DefectKind.STRINGING: _Profile(
        "Stringing / oozing",
        Urgency.COSMETIC,
        False,
        0.25,
        ("retraction_length", "retraction_speed", "nozzle_temperature", "travel_speed"),
    ),
    DefectKind.UNDER_EXTRUSION: _Profile(
        "Under-extrusion",
        Urgency.SERIOUS,
        False,
        0.5,
        ("flow_ratio", "nozzle_temperature", "print_speed"),
    ),
    DefectKind.OVER_EXTRUSION: _Profile(
        "Over-extrusion",
        Urgency.COSMETIC,
        False,
        0.35,
        ("flow_ratio", "nozzle_temperature"),
    ),
    DefectKind.BLOB: _Profile(
        "Blob / zit on the surface",
        Urgency.COSMETIC,
        False,
        0.3,
        ("retraction_length", "pressure_advance", "nozzle_temperature"),
    ),
    DefectKind.NOZZLE_CLOG: _Profile(
        "Nozzle clog / no material coming out",
        Urgency.CRITICAL,
        True,
        0.8,
        ("nozzle_temperature", "flow_ratio", "print_speed"),
    ),
    DefectKind.CAMERA_FAULT: _Profile(
        "Frame unusable (dark, blurred, or camera down)",
        Urgency.OBSERVATIONAL,
        False,
        0.0,
        (),
    ),
}

#: Accepted by prompt parsers. Kept as a set so an unknown label from a model is
#: dropped rather than crashing a parse or inventing a defect kind.
KNOWN_KINDS = {kind.value for kind in DefectKind}


def parse_kind(value: Any) -> DefectKind | None:
    """Map a model's free-text label to a known kind, or None."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace(" ", "_").replace("-", "_")
    if normalized in KNOWN_KINDS:
        return DefectKind(normalized)
    # A few phrasings models reach for that map cleanly onto a known kind.
    aliases = {
        "spaghetti_monster": DefectKind.SPAGHETTI,
        "bed_adhesion": DefectKind.ADHESION,
        "poor_adhesion": DefectKind.ADHESION,
        "detached": DefectKind.DETACHMENT,
        "came_loose": DefectKind.PRINT_CAME_LOOSE,
        "knocked_loose": DefectKind.PRINT_CAME_LOOSE,
        "first_layer_problem": DefectKind.FIRST_LAYER,
        "shifted_layers": DefectKind.LAYER_SHIFT,
        "layer_shifting": DefectKind.LAYER_SHIFT,
        "warp": DefectKind.WARPING,
        "curling": DefectKind.WARPING,
        "strings": DefectKind.STRINGING,
        "oozing": DefectKind.STRINGING,
        "clog": DefectKind.NOZZLE_CLOG,
        "clogged_nozzle": DefectKind.NOZZLE_CLOG,
        "blobs": DefectKind.BLOB,
        "zits": DefectKind.BLOB,
        "underextrusion": DefectKind.UNDER_EXTRUSION,
        "overextrusion": DefectKind.OVER_EXTRUSION,
    }
    return aliases.get(normalized)


@dataclass
class Detection:
    """One defect reported by one provider for one frame."""

    kind: DefectKind
    confidence: float
    provider: str
    note: str = ""
    bbox_norm: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    @property
    def severity(self) -> float:
        """Blend of what the kind costs and how sure the provider is.

        Weighted toward the kind. A confident report of stringing must never
        outrank a tentative report of spaghetti, because the *consequences* are
        what an operator is deciding between, not the model's certainty.
        """
        return max(0.0, min(1.0, 0.65 * self.kind.severity_floor + 0.35 * self.confidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "label": self.kind.label,
            "urgency": self.kind.urgency.value,
            "confidence": round(self.confidence, 3),
            "severity": round(self.severity, 3),
            "provider": self.provider,
            "note": self.note[:300],
            "bbox_norm": list(self.bbox_norm) if self.bbox_norm else None,
        }


@dataclass
class ProviderReport:
    """What one provider contributed, including when it contributed nothing."""

    provider: str
    ok: bool
    elapsed_ms: float = 0.0
    detections: list[Detection] = field(default_factory=list)
    skipped_reason: str = ""
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "ok": self.ok,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "detections": [d.to_dict() for d in self.detections],
            "skipped_reason": self.skipped_reason[:300],
            "summary": self.summary[:300],
        }


@dataclass
class HealthVerdict:
    """The fused assessment of one frame."""

    detections: list[Detection] = field(default_factory=list)
    reports: list[ProviderReport] = field(default_factory=list)
    agreement: float = 0.0
    frame_index: int = 0
    captured_at: float = 0.0
    layer: int | None = None

    @property
    def worst(self) -> Detection | None:
        """Highest-severity detection that is about the print itself."""
        real = [d for d in self.detections if d.kind.is_print_failure]
        return max(real, key=lambda d: d.severity, default=None)

    @property
    def severity(self) -> float:
        worst = self.worst
        return worst.severity if worst else 0.0

    @property
    def confidence(self) -> float:
        worst = self.worst
        return worst.confidence if worst else 0.0

    @property
    def urgency(self) -> Urgency:
        worst = self.worst
        return worst.kind.urgency if worst else Urgency.OBSERVATIONAL

    @property
    def flagged(self) -> bool:
        return self.worst is not None

    @property
    def voted(self) -> list[str]:
        return [r.provider for r in self.reports if r.ok]

    @property
    def skipped(self) -> list[str]:
        return [r.provider for r in self.reports if not r.ok]

    @property
    def camera_fault(self) -> str:
        for detection in self.detections:
            if detection.kind is DefectKind.CAMERA_FAULT:
                return detection.note or "frame unusable"
        return ""

    def describe(self) -> str:
        worst = self.worst
        if worst is None:
            return "no defects flagged"
        return (
            f"{worst.kind.label} ({worst.kind.urgency.value}, "
            f"severity {worst.severity:.2f}, confidence {worst.confidence:.2f})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "captured_at": self.captured_at,
            "layer": self.layer,
            "severity": round(self.severity, 3),
            "confidence": round(self.confidence, 3),
            "urgency": self.urgency.value,
            "flagged": self.flagged,
            "agreement": round(self.agreement, 3),
            "summary": self.describe(),
            "camera_fault": self.camera_fault,
            "detections": [d.to_dict() for d in self.detections],
            "voted": self.voted,
            "skipped": self.skipped,
            "reports": [r.to_dict() for r in self.reports],
        }


def fuse(
    reports: list[ProviderReport],
    *,
    frame_index: int = 0,
    captured_at: float = 0.0,
    layer: int | None = None,
    min_agreement: float = 0.4,
) -> HealthVerdict:
    """Fuse per-provider reports into one verdict.

    Detections are grouped **by kind**. Within a kind, confidence is the maximum
    any provider gave, not the mean.

    Taking the max is deliberate and asymmetric. Averaging lets a provider that
    cannot see a defect - because it is a motion heuristic and the defect is a
    colour artifact, or because the object is outside its crop - silently veto a
    provider that can. These members have genuinely different sensitivities, so
    a non-detection is usually weak evidence of absence. The protection against
    a single provider crying wolf is not averaging: it is
    ``confirm_frames`` consecutive samples, plus the agreement figure reported
    alongside so an operator can see the vote was 1-of-3.
    """
    by_kind: dict[DefectKind, list[Detection]] = {}
    for report in reports:
        if not report.ok:
            continue
        for detection in report.detections:
            by_kind.setdefault(detection.kind, []).append(detection)

    voters = sum(1 for r in reports if r.ok)
    fused: list[Detection] = []
    agreements: list[float] = []

    for kind, group in by_kind.items():
        strongest = max(group, key=lambda d: d.confidence)
        providers = sorted({d.provider for d in group})
        agreement = len(providers) / voters if voters else 0.0
        if kind.is_print_failure:
            agreements.append(agreement)
        note = strongest.note
        if len(providers) > 1:
            note = f"{note} [agreed by {', '.join(providers)}]".strip()
        fused.append(
            Detection(
                kind=kind,
                confidence=strongest.confidence,
                provider="+".join(providers),
                note=note,
                bbox_norm=strongest.bbox_norm,
            )
        )

    fused.sort(key=lambda d: d.severity, reverse=True)
    # Agreement is reported for the defect that will drive any action, since a
    # mean across unrelated kinds would not describe anything an operator cares
    # about.
    agreement = max(agreements) if agreements else 0.0

    return HealthVerdict(
        detections=fused,
        reports=reports,
        agreement=agreement,
        frame_index=frame_index,
        captured_at=captured_at,
        layer=layer,
    )
