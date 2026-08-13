"""Visual-hull reconstruction by space carving, plus metric measurement.

Space carving starts with a solid block of voxels and deletes every voxel that
projects outside the object's silhouette in any view. What survives is the
**visual hull**: provably a superset of the true object, and never tighter than
its convex hull with respect to the viewing directions used. Concavities that do
not change any silhouette are never carved. That is a property of the method,
not a tuning parameter, and ``docs/ACCURACY.md`` says so plainly.

Two details make the difference between a hull that means something and one that
quietly lies.

**Unknown is not empty.** A voxel that projects *outside the image* has not been
observed by that view, and carving it would delete object based on the camera's
field of view rather than on evidence. Only voxels that land inside the image
*and* outside the mask are carved. Getting this backwards is the classic space-
carving bug: it silently shaves whatever drifts near the frame edge, and the
result looks plausible.

**Azimuth is a rotation of the object, not of the machine.** Nothing on this
printer turns the part. Between passes the operator does, so a view taken during
pass *a* observes the object rotated by *a* about its own vertical axis. The
carver undoes that rotation when projecting, which is the only reason multi-pass
coverage combines into one hull at all.

Measurement comes from the **carved grid**, not from the bed-contact footprint
and not from the triangulated mesh. The footprint is exact where it applies -
those pixels genuinely lie on the bed plane, so the planar homography holds
without any depth assumption - but it only ever traces the object's near face,
and sizing an object by it under-reads the axis running away from the camera
(measured: 11 mm returned for a 25 mm depth). The carve is the only product that
integrates every view, so it is what :func:`measure` reads. The footprint is
kept for locating the object and for bed contact area.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ..config import ThoxSettings as ScanSettings
from ..errors import NoObjectFound, ReconstructionFailed
from .types import (
    CaptureFrame,
    FusedObservation,
    Measurement,
    Reliability,
    ScanMeasurements,
)
from .rig import RigCalibration

logger = logging.getLogger(__name__)

#: Voxels processed per chunk. Bounds peak memory independently of grid size.
_CHUNK = 2_000_000


@dataclass
class CarveResult:
    """A carved grid and the bookkeeping needed to interpret it."""

    occupancy: np.ndarray
    origin: np.ndarray
    voxel_mm: float
    views_used: int
    carved_fraction: float

    @property
    def occupied_voxels(self) -> int:
        return int(self.occupancy.sum())

    @property
    def volume_mm3(self) -> float:
        return self.occupied_voxels * self.voxel_mm**3


def _rotation_z(degrees: float) -> np.ndarray:
    angle = np.radians(float(degrees))
    cos, sin = np.cos(angle), np.sin(angle)
    return np.array([[cos, -sin, 0.0], [sin, cos, 0.0], [0.0, 0.0, 1.0]])


def footprint_from_mask(
    mask: np.ndarray, calibration: RigCalibration, z_cmd: float
) -> np.ndarray:
    """Map a mask's bottom edge to bed-plane millimetres.

    Only the *lowest* mask pixel in each column is used. Those pixels are where
    the object meets the bed, so ``h = 0`` genuinely holds for them and the
    planar homography is exact. Mapping the whole mask would silently treat the
    object's top surface as if it lay on the bed, inflating the footprint by the
    object's own height in parallax.
    """
    if not mask.any():
        return np.empty((0, 2))
    columns = np.flatnonzero(mask.any(axis=0))
    rows = [int(np.flatnonzero(mask[:, c])[-1]) for c in columns]
    pixels = np.column_stack([columns.astype(float), np.asarray(rows, dtype=float)])
    bed = calibration.image_to_bed(pixels, z_cmd)
    return bed[np.isfinite(bed).all(axis=1)]


def estimate_bounds(
    observations: list[FusedObservation],
    frames: list[CaptureFrame],
    calibration: RigCalibration,
    *,
    margin_mm: float = 6.0,
    max_height_mm: float = 150.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Bounding box in object-frame mm to carve within.

    The grid must **contain** the object; anything outside it is unrecoverable,
    because the carve can only remove voxels, never add them. So this errs
    generously and lets the carve find the true surface.

    The subtlety is that the footprint locates the object well but sizes it
    badly. It is the bed-contact edge, which under an oblique view traces only
    the object's near face - measured, that reported 11 mm of depth on a 25 mm
    box. Deriving the grid's Y extent from it directly produced a grid too
    shallow to hold the object, and the carve then "reconstructed" a hull
    smaller than the object, which is geometrically impossible and was the
    tell.

    So the footprint is used only for the **centre**, and the half-extent is the
    largest span seen in *any* view. Across azimuth passes the widest axis faces
    the camera at some point, so that maximum bounds every axis. The box is made
    symmetric about the centre because the operator rotates the object about
    roughly that point.

    Height gets generous headroom for the same reason: starting too short clips
    the object silently, and nothing downstream can detect it.
    """
    points: list[np.ndarray] = []
    for observation, frame in zip(observations, frames, strict=False):
        bed = footprint_from_mask(observation.mask, calibration, frame.actual_z_mm)
        if len(bed):
            points.append(bed)
    if not points:
        raise NoObjectFound(
            "no frame produced a usable footprint, so the object's position on "
            "the bed is unknown"
        )

    stacked = np.vstack(points)
    # Robust centre: a single bad silhouette must not drag the grid off the
    # object, and a median is immune to a handful of stray points.
    centre = np.median(stacked, axis=0)

    # Largest span observed in any single view, over either bed axis.
    widest = 0.0
    for cloud in points:
        if len(cloud) < 2:
            continue
        spans = np.percentile(cloud, 98, axis=0) - np.percentile(cloud, 2, axis=0)
        widest = max(widest, float(np.max(spans)))
    # Also respect the overall spread across every view combined, which catches
    # the case where the object is long and no single view saw all of it.
    overall = np.percentile(stacked, 98, axis=0) - np.percentile(stacked, 2, axis=0)
    widest = max(widest, float(np.max(overall)))

    half = widest / 2.0 + margin_mm
    return (
        np.array([centre[0] - half, centre[1] - half, 0.0]),
        np.array([centre[0] + half, centre[1] + half, float(max_height_mm)]),
    )


def carve(
    observations: list[FusedObservation],
    frames: list[CaptureFrame],
    calibration: RigCalibration,
    *,
    settings: ScanSettings | None = None,
    bounds: tuple[np.ndarray, np.ndarray] | None = None,
    rotation_center: np.ndarray | None = None,
) -> CarveResult:
    """Carve the visual hull from a set of fused silhouettes.

    Raises:
        ReconstructionFailed: If the grid would exceed ``max_voxels``, or the
            carve removes everything.
    """
    settings = settings or ScanSettings()
    if not observations:
        raise NoObjectFound("no observations to carve from")

    low, high = bounds or estimate_bounds(observations, frames, calibration)
    voxel = float(settings.voxel_mm)
    counts = np.maximum(1, np.ceil((high - low) / voxel).astype(int))
    total = int(np.prod(counts.astype(np.int64)))
    if total > settings.max_voxels:
        suggested = voxel * (total / settings.max_voxels) ** (1 / 3)
        raise ReconstructionFailed(
            f"grid would be {counts.tolist()} = {total:,} voxels, over the "
            f"{settings.max_voxels:,} ceiling. Raise THOX_SCAN_MAX_VOXELS or "
            f"set THOX_SCAN_VOXEL_MM to about {suggested:.2f}."
        )

    # Voxel centres in object-frame millimetres.
    axes = [low[d] + (np.arange(counts[d]) + 0.5) * voxel for d in range(3)]
    grid = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1).reshape(-1, 3)
    occupancy = np.ones(len(grid), dtype=bool)

    if rotation_center is None:
        rotation_center = np.array(
            [(low[0] + high[0]) / 2, (low[1] + high[1]) / 2, 0.0]
        )

    views = 0
    for observation, frame in zip(observations, frames, strict=False):
        mask = observation.mask
        if mask is None or not mask.any():
            continue
        height_px, width_px = mask.shape
        rotation = _rotation_z(frame.pose.azimuth_deg)
        views += 1

        for start in range(0, len(grid), _CHUNK):
            stop = min(start + _CHUNK, len(grid))
            alive = occupancy[start:stop]
            if not alive.any():
                continue
            block = grid[start:stop]
            # Undo the operator's manual rotation for this pass.
            if frame.pose.azimuth_deg:
                block = (block - rotation_center) @ rotation.T + rotation_center

            pixels = calibration.project(block, frame.actual_z_mm)
            columns = np.round(pixels[:, 0]).astype(np.int64)
            rows = np.round(pixels[:, 1]).astype(np.int64)

            observed = (
                np.isfinite(pixels).all(axis=1)
                & (columns >= 0)
                & (columns < width_px)
                & (rows >= 0)
                & (rows < height_px)
            )
            # Carve only where this view actually saw the voxel and the
            # silhouette says "not object". Unobserved voxels are left alone.
            inside = np.zeros(len(block), dtype=bool)
            if observed.any():
                inside[observed] = mask[rows[observed], columns[observed]]
            occupancy[start:stop] = alive & (~observed | inside)

    if views == 0:
        raise NoObjectFound("every observation had an empty mask")

    survived = occupancy.reshape(tuple(counts))
    carved_fraction = 1.0 - float(occupancy.mean())
    if not survived.any():
        raise ReconstructionFailed(
            f"all {total:,} voxels were carved away across {views} views. The "
            "silhouettes are mutually inconsistent - most often the object was "
            "moved or rotated during a pass, or the calibration is wrong."
        )

    logger.info(
        "carved %d views: %d/%d voxels survive (%.1f%% removed)",
        views,
        int(survived.sum()),
        total,
        carved_fraction * 100.0,
    )
    return CarveResult(
        occupancy=survived,
        origin=low,
        voxel_mm=voxel,
        views_used=views,
        carved_fraction=carved_fraction,
    )


def measure(
    observations: list[FusedObservation],
    frames: list[CaptureFrame],
    calibration: RigCalibration,
    carve_result: CarveResult | None = None,
    *,
    azimuths: int = 1,
) -> ScanMeasurements:
    """Measure the object's extent, with honest per-axis tolerances.

    All three axes are taken from the **carve**, because the carve is the only
    product that integrates every view. The footprint is used for the bed
    contact area only, and deliberately not for width or depth.

    That split is the result of a measured failure. ``footprint_from_mask``
    returns the lowest mask pixel per image column - the object's bed contact
    edge - which is exactly right for locating the object and exactly wrong for
    sizing it. Seen obliquely, a box's bottom edge traces only its *front* face,
    so the axis running away from the camera is invisible to it: on a synthetic
    40 x 25 x 15 mm box the footprint reported 11.2 mm of depth against 25 mm of
    truth. The carve, which fuses all elevations and azimuths, does not have
    that blind spot.

    Tolerance is derived, not asserted: the pixel scale at the poses used, times
    an assumed +/-1.5 px edge-localization error, doubled because two opposing
    edges each carry it, and floored at the voxel size since nothing carved can
    be sharper than one voxel. A provisional calibration or a low-agreement
    observation degrades the reliability grade rather than the number.
    """
    usable = [
        (o, f)
        for o, f in zip(observations, frames, strict=False)
        if o.mask is not None and o.mask.any()
    ]
    if not usable:
        raise NoObjectFound("no usable silhouettes to measure")

    footprints = []
    scales = []
    for observation, frame in usable:
        bed = footprint_from_mask(observation.mask, calibration, frame.actual_z_mm)
        if len(bed) >= 3:
            footprints.append(bed)
            scales.append(calibration.mm_per_px(frame.actual_z_mm))
    if not footprints:
        raise NoObjectFound("no frame produced a measurable footprint")

    stacked = np.vstack(footprints)
    scale = float(np.nanmedian(scales)) if scales else float("nan")
    edge_tolerance = 2.0 * 1.5 * (scale if np.isfinite(scale) else 0.6)

    if carve_result is None:
        raise ReconstructionFailed(
            "measurement requires a carve result; the bed-contact footprint "
            "alone cannot size the axis running away from the camera"
        )

    extents = _carve_extents(carve_result)
    span_x, span_y, height = extents
    edge_tolerance = max(edge_tolerance, carve_result.voxel_mm)
    height_tolerance = max(carve_result.voxel_mm, edge_tolerance)
    height_method = "visual hull along the calibrated Z stage"

    worst = min(
        (o.reliability for o, _ in usable),
        key=lambda r: [
            Reliability.GOOD,
            Reliability.MARGINAL,
            Reliability.UNRELIABLE,
        ].index(r),
    )
    grade = worst
    if calibration.provisional:
        # A nominal calibration can frame and plan, but any millimetre it
        # produces is an estimate. Never report GOOD on top of one.
        grade = Reliability.MARGINAL if worst is Reliability.GOOD else worst

    # Single-azimuth scans are not a dimensioning mode, and the code must not
    # imply otherwise. Measured on a synthetic 40 x 25 x 15 mm box with one
    # pass, the hull came back 1.66x true volume and depth read 52 mm against
    # 25 mm of truth - the axis running away from the camera is essentially
    # unconstrained when nothing ever turns the object. Height over-read by
    # 8 mm for the same reason: the hull is free to extend upward behind the
    # object. Only width (+4 mm) was near-usable.
    #
    # So every Tier-P measurement is graded UNRELIABLE regardless of how clean
    # the silhouettes were. Quoting a +/-4.55 mm tolerance next to a 27 mm error
    # would be worse than quoting nothing.
    multi_azimuth = azimuths >= 3
    if not multi_azimuth:
        grade = Reliability.UNRELIABLE
    depth_grade = grade if multi_azimuth else Reliability.UNRELIABLE
    depth_note = (
        "visual hull, multi-azimuth"
        if multi_azimuth
        else "visual hull, SINGLE azimuth - this axis is unconstrained; rotate "
        "the object and rescan for a usable number"
    )

    return ScanMeasurements(
        width_mm=Measurement(
            name="width",
            value_mm=span_x,
            tolerance_mm=edge_tolerance,
            method="visual hull",
            reliability=grade,
        ),
        depth_mm=Measurement(
            name="depth",
            value_mm=span_y,
            tolerance_mm=edge_tolerance * (1.0 if azimuths >= 3 else 2.0),
            method=depth_note,
            reliability=depth_grade,
        ),
        height_mm=Measurement(
            name="height",
            value_mm=height,
            tolerance_mm=height_tolerance,
            method=height_method,
            reliability=grade,
        ),
        footprint_area_mm2=float(_polygon_area(stacked)),
    )


def _carve_extents(result: CarveResult) -> tuple[float, float, float]:
    """Bounding-box dimensions of the carved hull, in mm, per axis."""
    out: list[float] = []
    for axis in range(3):
        others = tuple(a for a in range(3) if a != axis)
        occupied = np.flatnonzero(result.occupancy.any(axis=others))
        out.append(
            float((occupied[-1] + 1 - occupied[0]) * result.voxel_mm)
            if len(occupied)
            else 0.0
        )
    return out[0], out[1], out[2]


def _polygon_area(points: np.ndarray) -> float:
    """Convex-hull area of a 2-D point set, via monotone chain.

    Convex hull rather than a concave outline: the footprint points are the
    object's bed contact edge sampled per image column, which is already a
    silhouette-derived (hence convex-biased) quantity. Claiming a concave area
    from it would overstate what was measured.
    """
    if len(points) < 3:
        return 0.0
    pts = np.unique(points, axis=0)
    if len(pts) < 3:
        return 0.0
    order = np.lexsort((pts[:, 1], pts[:, 0]))
    pts = pts[order]

    def cross2(a: np.ndarray, b: np.ndarray) -> float:
        # Explicit 2-D cross product. numpy 2.0 deprecated np.cross on
        # 2-vectors, and this is clearer than padding to 3-D anyway.
        return float(a[0] * b[1] - a[1] * b[0])

    def half(seq: np.ndarray) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        for point in seq:
            while len(out) >= 2:
                if cross2(out[-1] - out[-2], point - out[-2]) > 0:
                    break
                out.pop()
            out.append(point)
        return out

    hull = np.array(half(pts)[:-1] + half(pts[::-1])[:-1])
    if len(hull) < 3:
        return 0.0
    x, y = hull[:, 0], hull[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)
