"""Turning a reconstructed mesh into something the Q2 can print.

Three outputs, because "print the thing you scanned" is rarely what anyone
actually wants:

``replica``
    The scanned hull itself. Honest about what it is - a visual hull, so convex-
    biased and missing every concavity. Useful as a shape reference.

``tray``
    A block with the object's shape subtracted from it, leaving a pocket the
    object drops into. This is the output the THOX workflow actually needs, and
    it is also the one this rig is *best* suited to produce: a pocket depends on
    the object's bounding geometry and footprint, which are the two things a
    silhouette-based method measures most reliably.

``cradle``
    A tray cut down to a shallow saddle, for parts that only need locating
    rather than enclosing.

Orientation is deliberately **not** optimized. A scanned object was sitting on
the bed when it was measured, so the mesh already has a flat, downward-facing
base at Z=0 by construction - which is exactly the orientation a slicer wants.
Rotating it to "minimize supports" would discard that guarantee and, on a hull
whose top surface is an over-estimate, would usually make things worse. The one
transform applied is a translation to centre the part on the plate.

Clearance is the load-bearing parameter for trays. The hull already over-states
the object by 1-2 mm at Tier H, so a pocket cut directly from it is *already*
loose; the configured clearance is added on top of a hull that is itself biased
outward, and the docstring on :func:`make_tray` explains how to reason about it.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import (
    BED_SIZE_X_MM,
    BED_SIZE_Y_MM,
    BUILD_HEIGHT_MM,
    ThoxSettings as ScanSettings,
)
from ..errors import ObjectTooLarge, PlateError
from .types import ScanTier
from .mesh import Mesh, mesh_from_voxels

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlateResult:
    """A plated model and where it sits on the bed."""

    mesh: Mesh
    kind: str
    position_mm: tuple[float, float]
    dimensions_mm: tuple[float, float, float]
    warnings: tuple[str, ...] = ()

    def describe(self) -> str:
        x, y, z = self.dimensions_mm
        return (
            f"{self.kind}: {x:.1f} x {y:.1f} x {z:.1f} mm at "
            f"({self.position_mm[0]:.0f}, {self.position_mm[1]:.0f})"
        )


def check_fits(dimensions: np.ndarray, margin_mm: float = 0.0) -> None:
    """Raise if the part cannot be printed on a Q2."""
    x, y, z = (float(v) for v in dimensions)
    usable_x = BED_SIZE_X_MM - 2 * margin_mm
    usable_y = BED_SIZE_Y_MM - 2 * margin_mm
    if x > usable_x or y > usable_y:
        raise ObjectTooLarge(
            (x, y, z),
            f"the usable plate ({usable_x:.0f} x {usable_y:.0f} mm with a "
            f"{margin_mm:.0f} mm margin)",
        )
    if z > BUILD_HEIGHT_MM:
        raise ObjectTooLarge((x, y, z), f"the {BUILD_HEIGHT_MM:.0f} mm build height")


def centre_on_plate(mesh: Mesh, margin_mm: float = 10.0) -> PlateResult:
    """Sit the mesh on Z=0 and centre it on the plate."""
    seated = mesh.sitting_on_bed()
    dimensions = seated.dimensions()
    check_fits(dimensions, margin_mm)

    low, high = seated.bounds()
    centre_xy = (low[:2] + high[:2]) / 2.0
    target = np.array([BED_SIZE_X_MM / 2.0, BED_SIZE_Y_MM / 2.0])
    placed = seated.translated(
        np.array([target[0] - centre_xy[0], target[1] - centre_xy[1], 0.0])
    )

    warnings: list[str] = []
    if dimensions[2] < 1.0:
        warnings.append(
            f"part is only {dimensions[2]:.2f} mm tall; below ~1 mm a scan is "
            "mostly measuring the bed, not an object"
        )
    return PlateResult(
        mesh=placed,
        kind="replica",
        position_mm=(float(target[0]), float(target[1])),
        dimensions_mm=tuple(float(v) for v in dimensions),
        warnings=tuple(warnings),
    )


def make_tray(
    occupancy: np.ndarray,
    origin: np.ndarray,
    voxel_mm: float,
    *,
    clearance_mm: float = 0.6,
    wall_mm: float = 2.4,
    floor_mm: float = 1.6,
    depth_fraction: float = 1.0,
) -> Mesh:
    """Build a block with the object's pocket subtracted, in voxel space.

    Boolean subtraction is done on the **voxel grid**, not on meshes. Mesh
    booleans need robust predicates and careful degeneracy handling to avoid
    producing non-manifold output; on a grid the same operation is
    ``block & ~dilated_object``, which is exact, trivially correct, and yields a
    solid the existing boundary extractor turns into a watertight mesh for free.

    On clearance: the pocket is cut from the *visual hull*, which already
    over-states the object by roughly 1-2 mm at Tier H. So the effective gap is
    the hull's own bias plus ``clearance_mm``. The default is small for that
    reason - adding a "safe" 1.5 mm on top of a hull that is already 2 mm loose
    gives a part that rattles. If the fit must be snug, scan at Tier H, print
    the tray, and adjust from the measured fit rather than guessing upward.

    Args:
        depth_fraction: How much of the object's height the pocket covers.
            1.0 encloses it fully; ~0.4 gives a cradle.
    """
    occupancy = np.asarray(occupancy, dtype=bool)
    if not occupancy.any():
        raise PlateError("cannot build a tray from an empty object")
    if voxel_mm <= 0:
        raise PlateError("voxel size must be positive")

    pad = int(np.ceil((clearance_mm + wall_mm) / voxel_mm)) + 1
    floor_voxels = max(1, int(round(floor_mm / voxel_mm)))

    # Trim the object to the requested pocket depth before dilating, so a
    # cradle's walls follow the part only as high as the pocket goes.
    occupied_z = np.flatnonzero(occupancy.any(axis=(0, 1)))
    z_low, z_high = int(occupied_z[0]), int(occupied_z[-1])
    keep_to = z_low + max(1, int(round((z_high - z_low + 1) * float(depth_fraction))))
    trimmed = occupancy.copy()
    trimmed[:, :, keep_to:] = False

    # Grow in XY and upward only. Growing downward would eat the floor.
    grown = _dilate(trimmed, int(np.ceil(clearance_mm / voxel_mm)))

    shape = (
        occupancy.shape[0] + 2 * pad,
        occupancy.shape[1] + 2 * pad,
        keep_to + floor_voxels + 1,
    )
    block = np.ones(shape, dtype=bool)
    pocket = np.zeros(shape, dtype=bool)
    pocket[
        pad : pad + occupancy.shape[0],
        pad : pad + occupancy.shape[1],
        floor_voxels : floor_voxels + grown.shape[2],
    ] = grown[:, :, : shape[2] - floor_voxels]

    solid = block & ~pocket
    if not solid.any():
        raise PlateError("tray subtraction removed the whole block")

    tray_origin = np.array(
        [origin[0] - pad * voxel_mm, origin[1] - pad * voxel_mm, 0.0]
    )
    mesh = mesh_from_voxels(solid, tray_origin, voxel_mm, keep_largest=True)
    mesh.note = (
        f"tray, clearance {clearance_mm:.2f} mm on top of the hull's own "
        f"outward bias, wall {wall_mm:.1f} mm, floor {floor_mm:.1f} mm"
    )
    return mesh


def _dilate(grid: np.ndarray, radius: int) -> np.ndarray:
    """Dilate in X and Y and upward in Z, by shifting. Never downward."""
    if radius <= 0:
        return grid
    out = grid.copy()
    for _ in range(radius):
        shifted = out.copy()
        shifted[1:, :, :] |= out[:-1, :, :]
        shifted[:-1, :, :] |= out[1:, :, :]
        shifted[:, 1:, :] |= out[:, :-1, :]
        shifted[:, :-1, :] |= out[:, 1:, :]
        shifted[:, :, 1:] |= out[:, :, :-1]
        out = shifted
    return out


def build_plate(
    mesh: Mesh,
    *,
    kind: str = "replica",
    settings: ScanSettings | None = None,
    tier: ScanTier = ScanTier.PROFILE,
    output_path: Path | None = None,
) -> tuple[PlateResult, Path | None]:
    """Place a mesh on the plate and optionally write a 3MF.

    The 3MF carries the tier and the caveat as metadata, so the provenance
    survives the handoff to a slicer. A file that says only "scan.3mf" invites
    someone to treat a Tier-P hull as a measured part three weeks later.
    """
    settings = settings or ScanSettings()
    result = centre_on_plate(mesh, settings.plate_margin_mm)
    result = PlateResult(
        mesh=result.mesh,
        kind=kind,
        position_mm=result.position_mm,
        dimensions_mm=result.dimensions_mm,
        warnings=result.warnings,
    )

    path: Path | None = None
    if output_path is not None:
        caveat = (
            "Visual hull from a single-azimuth scan: convex-biased and NOT "
            "dimensionally reliable."
            if tier is ScanTier.PROFILE
            else "Visual hull from a multi-azimuth scan; concave features are "
            "not recovered."
        )
        path = result.mesh.write_3mf(
            output_path,
            name=f"thox-scan-{kind}",
            metadata={
                "Title": f"THOX scan-to-print ({kind})",
                "Designer": "thox-q2-vision-scan",
                "Description": caveat,
                "Application": "thox-scan",
                "ScanTier": tier.value,
            },
        )
    return result, path


def slice_with_orca(
    model_path: Path,
    output_dir: Path,
    *,
    settings: ScanSettings | None = None,
    profile: Path | None = None,
    timeout_s: float = 300.0,
) -> Path:
    """Invoke OrcaSlicer/QIDI Studio's CLI to produce G-code.

    Optional by design. A valid 3MF is already a deliverable a human can open,
    inspect and slice with their own profile - which is the safer default, since
    a plate sliced with the wrong profile looks exactly like one sliced with the
    right profile until it fails on the machine.

    Raises:
        PlateError: If no slicer is configured, or slicing fails.
    """
    settings = settings or ScanSettings()
    executable = settings.orca_executable
    if executable is None:
        raise PlateError(
            "no slicer configured; set THOX_SCAN_ORCA_EXECUTABLE to the "
            "OrcaSlicer or QIDI Studio binary, or slice the 3MF by hand"
        )
    executable = Path(executable)
    if not executable.is_file():
        raise PlateError(f"configured slicer does not exist: {executable}")

    output_dir.mkdir(parents=True, exist_ok=True)
    command = [str(executable), "--slice", "0", "--outputdir", str(output_dir)]
    if profile is not None:
        command += ["--load-settings", str(profile)]
    command.append(str(model_path))

    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise PlateError(f"slicer timed out after {timeout_s:.0f}s") from exc
    except OSError as exc:
        raise PlateError(f"could not run slicer: {exc}") from exc

    if completed.returncode != 0:
        raise PlateError(
            f"slicer exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout or '')[:400]}"
        )

    produced = sorted(output_dir.glob("*.gcode")) + sorted(output_dir.glob("*.3mf"))
    if not produced:
        raise PlateError(
            f"slicer reported success but produced no output in {output_dir}"
        )
    return produced[0]
