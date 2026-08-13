"""Pose planning: choosing which Z stations to capture from.

The naive plan is to divide the Z window into ``N`` equal steps. That is the
wrong plan, and the reason is worth spelling out because it is the difference
between a sweep that carves well and one that mostly repeats itself.

What a station is *worth* to reconstruction is the **viewing direction** it
contributes, not the Z value it sits at. Because the camera is at a fixed point
and the object rides past it, the elevation angle from object to camera changes
rapidly when the object is near the camera's height and barely at all when it is
far away. Uniform steps in Z therefore cluster many stations into nearly the
same viewing direction while leaving genuinely different angles unsampled.

So the planner samples **uniformly in elevation angle** and solves back for the
Z values that produce those angles. The result covers the available angular
range with the fewest moves, which also makes it faster: each move costs a
settle, a dwell, and a frame.

Stations where the object would leave the camera's field of view are dropped
before they are ever commanded — see :meth:`RigCalibration.visible_bed_window`.
Azimuth "poses" are not machine motion. Nothing on this printer rotates the
part; they record a manual re-placement the operator is asked to perform between
passes, and exist so the carver knows the views are not all from one side.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..config import ThoxSettings as ScanSettings
from ..errors import UnsafePose
from ..interlock import Clearance
from .types import Pose, ScanTier
from .rig import RigCalibration

#: Bed-plane margin required between the object's footprint and the edge of the
#: visible window, so a station is not accepted with the object half-cropped.
VISIBILITY_MARGIN_MM = 5.0


@dataclass(frozen=True)
class ObjectPlacement:
    """Where the object is on the bed and how big we currently think it is.

    Before segmentation this is an assumption (bed centre, unknown size); after
    the first frames it is measured and the plan can be refined. Both cases use
    the same type so the planner has one code path.
    """

    center_xy_mm: tuple[float, float]
    footprint_radius_mm: float = 40.0
    height_mm: float = 0.0
    measured: bool = False

    @property
    def center_3d(self) -> np.ndarray:
        """Object centroid in the object frame, at mid-height."""
        return np.array(
            [self.center_xy_mm[0], self.center_xy_mm[1], self.height_mm / 2.0]
        )


@dataclass(frozen=True)
class StationCandidate:
    """One evaluated Z value."""

    z_mm: float
    elevation_deg: float
    visible: bool
    mm_per_px: float
    reason: str = ""


def elevation_at(
    calibration: RigCalibration, placement: ObjectPlacement, z_mm: float
) -> float:
    """Elevation angle in degrees from the object's centroid up to the camera.

    90 degrees would be straight down onto the object; 0 would be exactly
    edge-on. On this rig the achievable band is narrow, which is precisely the
    limit the planner is trying to spend wisely.
    """
    camera = calibration.camera_center(z_mm)
    direction = camera - placement.center_3d
    norm = float(np.linalg.norm(direction))
    if norm < 1e-9:
        return float("nan")
    return float(np.degrees(np.arcsin(np.clip(direction[2] / norm, -1.0, 1.0))))


def evaluate_candidates(
    calibration: RigCalibration,
    placement: ObjectPlacement,
    clearance: Clearance,
    *,
    resolution: int = 200,
) -> list[StationCandidate]:
    """Score a fine Z grid across the cleared window for visibility and angle."""
    if clearance.travel_mm <= 0:
        return []
    grid = np.linspace(clearance.z_min_mm, clearance.z_max_mm, resolution)
    out: list[StationCandidate] = []
    cx, cy = placement.center_xy_mm
    radius = placement.footprint_radius_mm + VISIBILITY_MARGIN_MM

    for z_mm in grid:
        window = calibration.visible_bed_window(float(z_mm))
        visible = (
            window["area"] > 0
            and window["x_min"] <= cx - radius
            and window["x_max"] >= cx + radius
            and window["y_min"] <= cy - radius
            and window["y_max"] >= cy + radius
        )
        scale = calibration.mm_per_px(float(z_mm), placement.center_xy_mm)
        out.append(
            StationCandidate(
                z_mm=float(z_mm),
                elevation_deg=elevation_at(calibration, placement, float(z_mm)),
                visible=bool(visible),
                mm_per_px=float(scale),
                reason="" if visible else "object outside visible bed window",
            )
        )
    return out


def plan_sweep(
    calibration: RigCalibration,
    placement: ObjectPlacement,
    clearance: Clearance,
    settings: ScanSettings | None = None,
    *,
    azimuth_index: int = 0,
    azimuth_deg: float = 0.0,
    start_index: int = 0,
) -> list[Pose]:
    """Plan one azimuth pass: Z stations spread uniformly in elevation angle.

    Raises:
        UnsafePose: If no station in the cleared window can see the object. That
            is a real, actionable condition - the object is outside the camera's
            reachable window and needs repositioning, and reporting it beats
            capturing a sequence of empty frames.
    """
    settings = settings or ScanSettings()
    candidates = [
        c
        for c in evaluate_candidates(calibration, placement, clearance)
        if c.visible and np.isfinite(c.elevation_deg)
    ]
    if not candidates:
        raise UnsafePose(
            "no Z station in the cleared window keeps the object inside the "
            f"camera's view (object at {placement.center_xy_mm}, radius "
            f"{placement.footprint_radius_mm:.0f} mm). Move it toward the "
            "front-centre of the bed and retry."
        )

    wanted = min(int(settings.stations), len(candidates))
    angles = np.array([c.elevation_deg for c in candidates])
    targets = np.linspace(angles.min(), angles.max(), wanted)

    # Nearest-candidate lookup per target angle, de-duplicated. Two target
    # angles can resolve to the same station when the angular range is small,
    # and a duplicated station is a wasted move that also biases the carver by
    # double-counting one viewing direction.
    chosen: list[StationCandidate] = []
    used: set[int] = set()
    for target in targets:
        order = np.argsort(np.abs(angles - target))
        for idx in order:
            index = int(idx)
            if index not in used:
                used.add(index)
                chosen.append(candidates[index])
                break

    chosen.sort(key=lambda c: c.z_mm)
    return [
        Pose(
            index=start_index + i,
            z_mm=round(c.z_mm, 3),
            azimuth_index=azimuth_index,
            azimuth_deg=azimuth_deg,
        )
        for i, c in enumerate(chosen)
    ]


def plan_session(
    calibration: RigCalibration,
    placement: ObjectPlacement,
    clearance: Clearance,
    settings: ScanSettings | None = None,
) -> tuple[list[Pose], ScanTier]:
    """Plan every pass for a session, and report the tier it can support.

    The tier is derived from the plan rather than requested: a session that only
    ever sees one azimuth cannot claim hull coverage no matter what was asked
    for, and deciding that here keeps the claim tied to the evidence.
    """
    settings = settings or ScanSettings()
    azimuth_count = max(1, int(settings.azimuths))
    poses: list[Pose] = []
    for azimuth_index in range(azimuth_count):
        azimuth_deg = 360.0 * azimuth_index / azimuth_count
        poses.extend(
            plan_sweep(
                calibration,
                placement,
                clearance,
                settings,
                azimuth_index=azimuth_index,
                azimuth_deg=azimuth_deg,
                start_index=len(poses),
            )
        )
    tier = ScanTier.HULL if azimuth_count >= 3 else ScanTier.PROFILE
    return poses, tier


def rotation_prompt(azimuth_index: int, azimuth_deg: float, total: int) -> str:
    """Operator instruction shown between azimuth passes.

    Included here rather than in the UI because the wording is part of the
    measurement procedure: the carver assumes rotation about the object's own
    vertical axis with the footprint staying put, and an operator who slides the
    object across the bed instead invalidates the pass without any error firing.
    """
    if azimuth_index == 0:
        return "Place the object near the centre-front of the bed, then start."
    return (
        f"Pass {azimuth_index + 1} of {total}: rotate the object "
        f"{360.0 / total:.0f} degrees clockwise (now facing {azimuth_deg:.0f} "
        "degrees), keeping it on the same spot on the bed. Do not slide it."
    )


def describe_plan(
    poses: list[Pose], calibration: RigCalibration, placement: ObjectPlacement
) -> str:
    """One-line human summary of a plan, for logs and the CLI."""
    if not poses:
        return "empty plan"
    zs = [p.z_mm for p in poses]
    elevations = [elevation_at(calibration, placement, z) for z in zs]
    azimuths = len({p.azimuth_index for p in poses})
    return (
        f"{len(poses)} stations across {azimuths} azimuth(s), "
        f"Z {min(zs):.1f}..{max(zs):.1f} mm, "
        f"elevation {min(elevations):.1f}..{max(elevations):.1f} deg"
    )
