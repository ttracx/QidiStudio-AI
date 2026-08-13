"""Scan-to-print session runner: bed sweep to printable plate.

Synchronous, run on a worker thread by the HTTP layer. Sequence:

1. **Interlock first**, before anything moves. If the machine is busy, hot, or
   unhomed the session ends having touched nothing.
2. Plan Z stations spread uniformly in **elevation angle** rather than in Z,
   dropping any station where the object would leave the camera's view.
3. Move, wait for the bed to actually settle, capture, segment. The settle wait
   matters more than it looks: ``M400`` returns when Klipper has finished
   *planning*, while a 250 mm bed on lead screws is still ringing. A frame
   grabbed mid-ring is geometrically wrong in a way nothing downstream can
   detect.
4. Carve the visual hull, measure, mesh, plate.

The reference ladder deserves a note. Background subtraction needs an empty-bed
frame at the same Z, and such a frame depends only on Z - not on the object, not
on the session. So it is captured once and cached on disk by Z, and every later
scan reuses it. Without that, every scan would require clearing the bed and
running a second full sweep.

Heavy compute runs wherever this server runs - a Mac or workstation. The
printer's own 487 MB board does capture and motion only.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from ..config import ThoxSettings
from ..errors import CaptureFailed, MotionTimeout, NoObjectFound, ThoxRefused
from ..events import EventKind, EventLog
from ..interlock import Clearance, Interlock
from ..moonraker import MoonrakerClient
from .carve import carve, measure
from .mesh import mesh_from_voxels
from .plate import build_plate, make_tray
from .poses import ObjectPlacement, describe_plan, plan_session, rotation_prompt
from .rig import RigCalibration, nominal_calibration
from .segment import segment_standalone, segment_with_reference
from .types import CaptureFrame, FusedObservation, Pose, Reliability, ScanTier

logger = logging.getLogger(__name__)

ProgressFn = Callable[[str, dict], None]

#: Caveats true of this rig on every scan, regardless of how well it went.
ALWAYS_CAVEATS = (
    "This printer has one fixed camera and a bed that moves only in Z, so no "
    "automated pass can see the far side or underside of the object.",
    "Reconstruction is a visual hull: concave features, pockets and undercuts "
    "are not recovered.",
)


def _decode_gray(jpeg: bytes) -> np.ndarray:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as image:
        return np.asarray(image.convert("L"), dtype=np.float32)


class ReferenceLibrary:
    """Disk cache of empty-bed frames, keyed by commanded Z.

    Invalidated by moving the camera, changing the plate, or changing chamber
    lighting - none of which are detectable from a JPEG. So the library records
    when it was built and exposes an explicit rebuild rather than pretending it
    can tell.
    """

    def __init__(self, root: str) -> None:
        self.root = os.path.join(root, "scan", "_references")
        os.makedirs(self.root, exist_ok=True)

    @staticmethod
    def key(z_mm: float) -> str:
        # 0.1 mm is far below what the camera can resolve, so stations within
        # that distance safely share a reference.
        return f"z{round(float(z_mm), 1):.1f}"

    def path_for(self, z_mm: float) -> str:
        return os.path.join(self.root, f"{self.key(z_mm)}.jpg")

    def get(self, z_mm: float) -> bytes | None:
        path = self.path_for(z_mm)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def put(self, z_mm: float, jpeg: bytes) -> None:
        try:
            with open(self.path_for(z_mm), "wb") as handle:
                handle.write(jpeg)
        except OSError as exc:
            logger.warning(
                "[THOX] could not cache reference frame (%s)", type(exc).__name__
            )

    @property
    def size(self) -> int:
        try:
            return len([n for n in os.listdir(self.root) if n.endswith(".jpg")])
        except OSError:
            return 0

    def clear(self) -> int:
        removed = 0
        try:
            for name in os.listdir(self.root):
                if name.endswith(".jpg"):
                    os.remove(os.path.join(self.root, name))
                    removed += 1
        except OSError:
            pass
        return removed


@dataclass
class ScanResult:
    """Everything one session produced."""

    session_id: str
    state: str = "created"
    tier: str = "P"
    poses: list[dict[str, Any]] = field(default_factory=list)
    frames: list[dict[str, Any]] = field(default_factory=list)
    measurements: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    error: str = ""
    refusal_reason: str = ""
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "tier": self.tier,
            "poses": self.poses,
            "frames": self.frames,
            "measurements": self.measurements,
            "artifacts": self.artifacts,
            "caveats": self.caveats,
            "error": self.error,
            "refusal_reason": self.refusal_reason,
            "elapsed_s": round(self.elapsed_s, 1),
        }


class ScanRunner:
    """Runs one scan-to-print session end to end."""

    def __init__(
        self,
        settings: ThoxSettings | None = None,
        client: MoonrakerClient | None = None,
        events: EventLog | None = None,
        calibration: RigCalibration | None = None,
    ) -> None:
        self.settings = settings or ThoxSettings.from_env()
        self.client = client or MoonrakerClient(self.settings)
        self.events = events or EventLog(self.settings.state_root)
        self.interlock = Interlock(self.settings)
        self.calibration = calibration or nominal_calibration()
        self.references = ReferenceLibrary(self.settings.state_root)

    # -- motion -------------------------------------------------------------

    def goto_z(self, z_mm: float, clearance: Clearance) -> float:
        """Move to Z and return the Z the machine actually reports.

        Re-runs the interlock first: a print can be started from the
        touchscreen between two stations of a sweep, and a check that only ran
        at session start would not see it.
        """
        self.interlock.assert_can_scan(
            self.client, object_height_mm=clearance.object_height_mm
        )
        self.client.run_gcode(self.interlock.move_script(z_mm, clearance))
        return self._await_settle(z_mm)

    def _await_settle(self, target_z: float) -> float:
        deadline = time.monotonic() + self.settings.motion_settle_timeout_s
        last = float("nan")
        while time.monotonic() < deadline:
            status = self.client.query({"toolhead": ["position"]})
            position = (status.get("toolhead") or {}).get("position") or []
            if len(position) >= 3:
                last = float(position[2])
                if abs(last - target_z) <= 0.05:
                    # Queue drained and position matches. The dwell covers
                    # mechanical ring-down and camera auto-exposure, neither of
                    # which Klipper knows anything about.
                    if self.settings.settle_dwell_s > 0:
                        time.sleep(self.settings.settle_dwell_s)
                    return last
            time.sleep(0.2)
        raise MotionTimeout(target_z, last, self.settings.motion_settle_timeout_s)

    # -- reference ladder ---------------------------------------------------

    def capture_reference_ladder(
        self, placement: ObjectPlacement, progress: ProgressFn | None = None
    ) -> int:
        """Capture empty-bed frames for each planned Z. The bed MUST be clear.

        Nothing here can verify the bed is empty - at 640x480 an empty plate and
        one holding a flat dark object look alike, and a check would manufacture
        confidence. It is stated as a precondition instead.
        """
        clearance = self.interlock.assert_can_scan(
            self.client, object_height_mm=placement.height_mm
        )
        poses, _ = plan_session(self.calibration, placement, clearance, self.settings)
        seen: set[str] = set()
        captured = 0
        for pose in poses:
            key = self.references.key(pose.z_mm)
            if key in seen:
                continue
            seen.add(key)
            actual = self.goto_z(pose.z_mm, clearance)
            self.references.put(actual, self.client.snapshot())
            captured += 1
            if progress:
                progress("reference", {"captured": captured, "z": round(actual, 2)})
        self.events.add(
            EventKind.SAMPLE, f"captured {captured} empty-bed reference frames"
        )
        return captured

    # -- the session --------------------------------------------------------

    def run(
        self,
        placement: ObjectPlacement,
        *,
        session_id: str = "",
        make_tray_too: bool = True,
        progress: ProgressFn | None = None,
    ) -> ScanResult:
        """Execute a scan. Never raises for expected conditions."""
        started = time.monotonic()
        session_id = session_id or time.strftime("scan_%Y%m%dT%H%M%SZ", time.gmtime())
        result = ScanResult(session_id=session_id, caveats=list(ALWAYS_CAVEATS))
        if self.calibration.provisional:
            result.caveats.append(
                "Rig calibration is provisional (never measured on this "
                "machine), so every dimension is an estimate."
            )

        def report(stage: str, **detail: Any) -> None:
            if progress:
                progress(stage, detail)

        directory = os.path.join(self.settings.state_root, "scan", session_id)
        try:
            os.makedirs(os.path.join(directory, "frames"), exist_ok=True)
        except OSError as exc:
            result.state = "failed"
            result.error = f"cannot create session directory ({type(exc).__name__})"
            return result

        try:
            report("interlock")
            clearance = self.interlock.assert_can_scan(
                self.client, object_height_mm=placement.height_mm
            )

            poses, tier = plan_session(
                self.calibration, placement, clearance, self.settings
            )
            self.interlock.validate_plan([p.z_mm for p in poses], clearance)
            result.poses = [p.to_dict() for p in poses]
            result.tier = tier.value
            result.state = "capturing"
            if tier is ScanTier.PROFILE:
                result.caveats.append(
                    "Single-pass scan: depth and height are NOT dimensionally "
                    "reliable. Rotate the object between four passes for usable "
                    "numbers."
                )
            report(
                "planned",
                summary=describe_plan(poses, self.calibration, placement),
                tier=tier.value,
                stations=len(poses),
            )

            observations: list[FusedObservation] = []
            frames: list[CaptureFrame] = []
            last_azimuth = -1

            for pose in poses:
                if pose.azimuth_index != last_azimuth:
                    last_azimuth = pose.azimuth_index
                    if pose.azimuth_index > 0:
                        report(
                            "await_rotation",
                            prompt=rotation_prompt(
                                pose.azimuth_index,
                                pose.azimuth_deg,
                                self.settings.azimuths,
                            ),
                        )

                frame, observation = self._capture_and_segment(
                    pose, clearance, directory
                )
                frames.append(frame)
                if observation is not None:
                    observations.append(observation)
                result.frames.append(frame.to_dict())
                report(
                    "frame",
                    index=len(frames),
                    total=len(poses),
                    z=round(frame.actual_z_mm, 2),
                    found=observation is not None,
                )

            if not observations:
                raise NoObjectFound(
                    "no frame produced a usable silhouette. Check the object is "
                    "on the bed, the chamber LED is on, and that a reference "
                    "ladder has been captured."
                )

            result.state = "reconstructing"
            report("reconstructing", views=len(observations))
            carved = carve(
                observations, frames, self.calibration, settings=self.settings
            )
            measurements = measure(
                observations,
                frames,
                self.calibration,
                carved,
                azimuths=self.settings.azimuths,
            )
            result.measurements = measurements.to_dict()

            mesh = mesh_from_voxels(carved.occupancy, carved.origin, carved.voxel_mm)
            mesh = mesh.smoothed(2)
            watertight, why = mesh.is_watertight()
            if not watertight:
                result.caveats.append(f"mesh is not watertight: {why}")

            stl_path = os.path.join(directory, "scan.stl")
            mesh.write_stl(stl_path)
            result.artifacts.append(
                {
                    "kind": "mesh_stl",
                    "path": stl_path,
                    "bytes": os.path.getsize(stl_path),
                }
            )

            result.state = "plating"
            _, plate_path = build_plate(
                mesh,
                kind="replica",
                settings=self.settings,
                tier=tier,
                output_path=os.path.join(directory, "plate_replica.3mf"),
            )
            if plate_path:
                result.artifacts.append(
                    {
                        "kind": "plate_replica",
                        "path": str(plate_path),
                        "bytes": os.path.getsize(plate_path),
                    }
                )

            if make_tray_too:
                try:
                    tray = make_tray(carved.occupancy, carved.origin, carved.voxel_mm)
                    _, tray_path = build_plate(
                        tray,
                        kind="tray",
                        settings=self.settings,
                        tier=tier,
                        output_path=os.path.join(directory, "plate_tray.3mf"),
                    )
                    if tray_path:
                        result.artifacts.append(
                            {
                                "kind": "plate_tray",
                                "path": str(tray_path),
                                "bytes": os.path.getsize(tray_path),
                            }
                        )
                except Exception as exc:
                    # A tray is a bonus output; failing to build one must not
                    # discard a good scan.
                    result.caveats.append(
                        f"tray could not be generated ({type(exc).__name__})"
                    )

            result.state = "complete"
            report("complete", artifacts=len(result.artifacts))

        except ThoxRefused as exc:
            result.state = "refused"
            result.refusal_reason = exc.reason
            result.error = exc.detail
            report("refused", reason=exc.reason, detail=exc.detail)
        except Exception as exc:
            result.state = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            logger.exception("[THOX] scan %s failed", session_id)
            report("failed", error=result.error)

        result.elapsed_s = time.monotonic() - started
        return result

    def _capture_and_segment(
        self, pose: Pose, clearance: Clearance, directory: str
    ) -> tuple[CaptureFrame, FusedObservation | None]:
        actual_z = self.goto_z(pose.z_mm, clearance)
        jpeg = self.client.snapshot()

        filename = f"{pose.index:04d}_az{pose.azimuth_index}_z{actual_z:.1f}.jpg"
        try:
            with open(os.path.join(directory, "frames", filename), "wb") as handle:
                handle.write(jpeg)
        except OSError as exc:
            raise CaptureFailed(
                f"could not write frame ({type(exc).__name__})"
            ) from exc

        gray = _decode_gray(jpeg)
        reference = self.references.get(actual_z)
        if reference is not None:
            reference_gray = _decode_gray(reference)
            if reference_gray.shape == gray.shape:
                mask, confidence = segment_with_reference(gray, reference_gray)
            else:
                mask, confidence = segment_standalone(gray)
        else:
            mask, confidence = segment_standalone(gray)

        frame = CaptureFrame(
            pose=pose,
            filename=filename,
            actual_z_mm=actual_z,
            width=int(gray.shape[1]),
            height=int(gray.shape[0]),
            captured_at=time.time(),
        )
        if not mask.any():
            return frame, None

        return frame, FusedObservation(
            frame_index=pose.index,
            mask=mask,
            reliability=Reliability.GOOD if reference is not None else Reliability.MARGINAL,
            agreement_iou=float(confidence),
        )
