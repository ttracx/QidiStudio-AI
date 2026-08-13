"""Value types for scan-to-print.

Dataclasses rather than pydantic models. The host pipeline's ``requirements.txt``
does not carry pydantic, and adding a validation framework to express six data
holders would be a poor trade against this package's install story - it needs to
work anywhere the existing AI pipeline already runs.

The one thing every measurement carries is **how it was obtained**. A dimension
derived from four rotated passes and one derived from a single pass are both
floats, and a UI that cannot tell them apart will present a guess as a
measurement. On this rig that difference is not academic: single-pass depth was
measured at 52 mm against 25 mm of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np


class ScanTier(str, Enum):
    """How much of the object a session can legitimately claim to know."""

    PROFILE = "P"
    HULL = "H"
    FIT = "F"

    @property
    def describes_full_surface(self) -> bool:
        return self is ScanTier.FIT

    @property
    def label(self) -> str:
        return {
            ScanTier.PROFILE: "Profile (single pass, shape preview only)",
            ScanTier.HULL: "Visual hull (multi-pass, ~2 mm)",
            ScanTier.FIT: "Parametric fit (identified part)",
        }[self]

    @property
    def dimensionally_reliable(self) -> bool:
        """Whether dimensions from this tier are worth quoting at all."""
        return self is not ScanTier.PROFILE


class Reliability(str, Enum):
    """Deliberately coarse. A continuous score invites a UI to render 0.83 as
    if it were calibrated; these three map to actions an operator can take."""

    GOOD = "good"
    MARGINAL = "marginal"
    UNRELIABLE = "unreliable"

    @property
    def rank(self) -> int:
        return {Reliability.GOOD: 0, Reliability.MARGINAL: 1, Reliability.UNRELIABLE: 2}[
            self
        ]


@dataclass
class Pose:
    """One planned observation station.

    ``azimuth_deg`` is **not** a machine axis. Nothing on this printer rotates
    the object; it records how far the operator was asked to turn the part by
    hand between passes. Metadata for the carver, never a command.
    """

    index: int
    z_mm: float
    azimuth_index: int = 0
    azimuth_deg: float = 0.0

    def __str__(self) -> str:
        return f"pose[{self.index}] Z{self.z_mm:.2f} az{self.azimuth_deg:.0f}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "z_mm": round(self.z_mm, 3),
            "azimuth_index": self.azimuth_index,
            "azimuth_deg": self.azimuth_deg,
        }


@dataclass
class CaptureFrame:
    """One acquired frame and the machine state that produced it."""

    pose: Pose
    filename: str
    #: Z the toolhead actually reported, not the commanded value. Geometry uses
    #: this; a 0.3 mm discrepancy silently biases every measurement.
    actual_z_mm: float
    width: int = 640
    height: int = 480
    captured_at: float = 0.0
    telemetry: dict[str, Any] = field(default_factory=dict)

    @property
    def z_error_mm(self) -> float:
        return abs(self.actual_z_mm - self.pose.z_mm)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pose": self.pose.to_dict(),
            "filename": self.filename,
            "actual_z_mm": round(self.actual_z_mm, 3),
            "z_error_mm": round(self.z_error_mm, 3),
            "width": self.width,
            "height": self.height,
            "captured_at": self.captured_at,
        }


@dataclass
class FusedObservation:
    """The fused silhouette for one frame. Holds a numpy mask, so it never
    serializes directly."""

    frame_index: int
    mask: np.ndarray
    reliability: Reliability = Reliability.GOOD
    agreement_iou: float = 0.0
    bbox_px: tuple[int, int, int, int] | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if self.mask.dtype != np.bool_:
            self.mask = self.mask.astype(bool)
        if self.mask.ndim != 2:
            raise ValueError(f"mask must be 2-D, got {self.mask.shape}")

    @property
    def area_px(self) -> int:
        return int(self.mask.sum())


@dataclass
class Measurement:
    """One dimensional result, with provenance and an honest tolerance.

    ``tolerance_mm`` is propagated from the pixel scale at the pose that
    produced it, not asserted, and the UI is expected to render it.
    """

    name: str
    value_mm: float
    tolerance_mm: float
    method: str
    reliability: Reliability = Reliability.GOOD

    def __str__(self) -> str:
        return f"{self.name} = {self.value_mm:.2f} +/- {self.tolerance_mm:.2f} mm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value_mm": round(self.value_mm, 2),
            "tolerance_mm": round(self.tolerance_mm, 2),
            "method": self.method,
            "reliability": self.reliability.value,
        }


@dataclass
class ScanMeasurements:
    """The measured extent of the object."""

    width_mm: Measurement
    depth_mm: Measurement
    height_mm: Measurement
    footprint_area_mm2: float = 0.0

    @property
    def bbox(self) -> tuple[float, float, float]:
        return (self.width_mm.value_mm, self.depth_mm.value_mm, self.height_mm.value_mm)

    @property
    def worst_reliability(self) -> Reliability:
        return max(
            (self.width_mm, self.depth_mm, self.height_mm),
            key=lambda m: m.reliability.rank,
        ).reliability

    def to_dict(self) -> dict[str, Any]:
        return {
            "width_mm": self.width_mm.to_dict(),
            "depth_mm": self.depth_mm.to_dict(),
            "height_mm": self.height_mm.to_dict(),
            "footprint_area_mm2": round(self.footprint_area_mm2, 1),
            "worst_reliability": self.worst_reliability.value,
        }
