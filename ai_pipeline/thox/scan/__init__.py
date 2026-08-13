"""Scan-to-print: printer-driven multi-view capture into a printable plate.

Complements the photo-to-3D pipeline in ``server.py``. That path takes photos an
operator supplies and infers a mesh generatively; this one drives the printer's
own bed as a calibrated stage and measures the object. Generative inference
produces a plausible whole object from one view; this produces a measured hull
of the side the camera can see. Different tools for different jobs.

Read ``docs/THOX_SCAN_TO_PRINT.md`` before trusting a dimension.
"""

from .carve import carve, measure
from .mesh import Mesh, mesh_from_voxels
from .plate import build_plate, make_tray
from .poses import ObjectPlacement, plan_session
from .rig import RigCalibration, nominal_calibration
from .types import (
    CaptureFrame,
    FusedObservation,
    Measurement,
    Pose,
    Reliability,
    ScanMeasurements,
    ScanTier,
)

__all__ = [
    "CaptureFrame",
    "FusedObservation",
    "Measurement",
    "Mesh",
    "ObjectPlacement",
    "Pose",
    "Reliability",
    "RigCalibration",
    "ScanMeasurements",
    "ScanTier",
    "build_plate",
    "carve",
    "make_tray",
    "measure",
    "mesh_from_voxels",
    "nominal_calibration",
    "plan_session",
]
