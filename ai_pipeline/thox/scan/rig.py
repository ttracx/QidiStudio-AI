"""Rig geometry: a fixed camera watching a calibrated vertical stage.

The whole scan lane rests on one modelling decision, so it is worth stating
precisely.

The object sits on the bed. The bed moves **only in Z**. The camera is bolted to
the frame. So in the object's own frame, commanding Z translates the camera
along a single known axis by a known distance. Formally, for an object point
``O = (x, y, h)`` in bed coordinates (millimetres, ``h`` measured up from the
bed surface) observed while the bed is commanded to ``z``::

    P_camera = R @ (O - e3 * s * z) + t
    u = fx * Xc / Zc + cx
    v = fy * Yc / Zc + cy

where ``s = +1`` if the bed rises with increasing Z and ``s = -1`` if it falls.
On the Q2 the bed falls as Z increases, so ``s = -1`` and the sign is carried by
:attr:`RigCalibration.bed_moves_down`.

Two properties follow, and they are why this rig is worth using at all:

1. **Scale is not a free parameter.** Ordinary structure-from-motion recovers
   geometry up to an unknown scale and needs a ruler in shot. Here the baseline
   between any two views is a Klipper Z command, known far more precisely than
   the camera can resolve. Millimetres come out because millimetres went in.

2. **The bed plane has an exact homography at every Z.** Setting ``h = 0``
   collapses the projection to a 3x3 map, so image pixels convert to bed
   millimetres in closed form — no iteration, no depth assumption. That is what
   makes footprint measurement trustworthy, and it is the one measurement on
   this rig that genuinely deserves confidence.

What the model does *not* buy: the camera only ever sees one side. See
``docs/ACCURACY.md``.

Deliberately numpy-only. No OpenCV: the calibration solved here has ten
parameters, which a plain Levenberg-Marquardt handles in a few milliseconds,
and adding a wheel-heavy binary dependency to reach ``cv2.calibrateCamera``
would make this package harder to install than the library it extends.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..config import BED_SIZE_X_MM, BED_SIZE_Y_MM
from ..errors import ReconstructionFailed as CalibrationError, ValidationError

# -- Rotation helpers --------------------------------------------------------


def rodrigues(rvec: np.ndarray) -> np.ndarray:
    """Rotation vector -> 3x3 rotation matrix.

    The small-angle branch avoids a 0/0 at ``theta = 0``; without it the solver
    produces NaNs the moment it trials an identity rotation, which it does on
    the very first iteration when seeded from a nominal calibration.
    """
    rvec = np.asarray(rvec, dtype=float).reshape(3)
    theta = float(np.linalg.norm(rvec))
    if theta < 1e-12:
        return np.eye(3)
    k = rvec / theta
    kx, ky, kz = k
    kmat = np.array([[0.0, -kz, ky], [kz, 0.0, -kx], [-ky, kx, 0.0]])
    return np.eye(3) + np.sin(theta) * kmat + (1.0 - np.cos(theta)) * (kmat @ kmat)


def inverse_rodrigues(matrix: np.ndarray) -> np.ndarray:
    """3x3 rotation matrix -> rotation vector."""
    matrix = np.asarray(matrix, dtype=float)
    cos_theta = (np.trace(matrix) - 1.0) / 2.0
    cos_theta = float(np.clip(cos_theta, -1.0, 1.0))
    theta = float(np.arccos(cos_theta))
    if theta < 1e-9:
        return np.zeros(3)
    if abs(theta - np.pi) < 1e-6:
        # Near 180 degrees the skew-symmetric part vanishes; recover the axis
        # from the symmetric part instead.
        sym = (matrix + np.eye(3)) / 2.0
        axis = np.sqrt(np.maximum(np.diag(sym), 0.0))
        if matrix[2, 1] < matrix[1, 2]:
            axis[0] = -axis[0]
        norm = np.linalg.norm(axis)
        if norm < 1e-12:
            return np.zeros(3)
        return axis / norm * theta
    axis = np.array(
        [
            matrix[2, 1] - matrix[1, 2],
            matrix[0, 2] - matrix[2, 0],
            matrix[1, 0] - matrix[0, 1],
        ]
    ) / (2.0 * np.sin(theta))
    return axis * theta


# -- Calibration -------------------------------------------------------------


@dataclass
class RigCalibration:
    """Intrinsics plus the camera's pose relative to the bed.

    ``provisional`` is load-bearing. A nominal calibration is good enough to
    plan poses and frame an object, and nowhere near good enough to quote a
    dimension. Every measurement derived from a provisional calibration is
    downgraded rather than silently reported at full confidence.
    """

    fx: float
    fy: float
    cx: float
    cy: float
    rvec: np.ndarray
    tvec: np.ndarray
    image_width: int = 640
    image_height: int = 480
    bed_moves_down: bool = True
    residual_px: float = float("nan")
    n_observations: int = 0
    provisional: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        self.rvec = np.asarray(self.rvec, dtype=float).reshape(3)
        self.tvec = np.asarray(self.tvec, dtype=float).reshape(3)
        if self.fx <= 0 or self.fy <= 0:
            raise CalibrationError(f"focal lengths must be positive: {self}")
        if self.image_width <= 0 or self.image_height <= 0:
            raise CalibrationError("image dimensions must be positive")

    # -- derived ------------------------------------------------------------

    @property
    def z_sign(self) -> float:
        """``s`` in the projection equation."""
        return -1.0 if self.bed_moves_down else 1.0

    @property
    def K(self) -> np.ndarray:  # noqa: N802 - conventional name in vision
        return np.array(
            [[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]]
        )

    @property
    def R(self) -> np.ndarray:  # noqa: N802 - conventional name in vision
        return rodrigues(self.rvec)

    def camera_center(self, z_cmd: float) -> np.ndarray:
        """Camera position expressed in the object's frame at commanded Z.

        The camera does not move; the *object* does. Re-expressing that as a
        moving camera over a static object is what lets the carver treat the
        sequence as an ordinary multi-view problem.
        """
        centre = -self.R.T @ self.tvec
        return centre + np.array([0.0, 0.0, self.z_sign * z_cmd])

    # -- projection ---------------------------------------------------------

    def project(self, points_mm: np.ndarray, z_cmd: float) -> np.ndarray:
        """Project object-frame points ``(N, 3)`` in mm to pixels ``(N, 2)``.

        Points behind the camera come back as NaN rather than as the mirrored
        coordinates a naive divide produces. A silently mirrored point is worse
        than a missing one: it lands inside the image and votes in the carver.
        """
        pts = np.asarray(points_mm, dtype=float)
        if pts.ndim != 2 or pts.shape[1] != 3:
            raise ValidationError(f"expected (N, 3) points, got {pts.shape}")

        shifted = pts - np.array([0.0, 0.0, self.z_sign * z_cmd])
        cam = shifted @ self.R.T + self.tvec
        depth = cam[:, 2]
        out = np.full((pts.shape[0], 2), np.nan)
        valid = depth > 1e-6
        out[valid, 0] = self.fx * cam[valid, 0] / depth[valid] + self.cx
        out[valid, 1] = self.fy * cam[valid, 1] / depth[valid] + self.cy
        return out

    def bed_homography(self, z_cmd: float) -> np.ndarray:
        """3x3 homography mapping bed-plane ``(x, y, 1)`` to image ``(u, v, 1)``.

        Exact for points on the bed surface (``h = 0``). This is the closed form
        that makes footprint measurement possible without any depth estimate.
        """
        rot = self.R
        translation = self.tvec + rot[:, 2] * (-self.z_sign * z_cmd)
        return self.K @ np.column_stack([rot[:, 0], rot[:, 1], translation])

    def bed_to_image(self, xy_mm: np.ndarray, z_cmd: float) -> np.ndarray:
        """Bed-plane points ``(N, 2)`` in mm to pixels ``(N, 2)``."""
        xy = np.asarray(xy_mm, dtype=float).reshape(-1, 2)
        homogeneous = np.column_stack([xy, np.ones(len(xy))])
        projected = homogeneous @ self.bed_homography(z_cmd).T
        with np.errstate(divide="ignore", invalid="ignore"):
            out = projected[:, :2] / projected[:, 2:3]
        out[~np.isfinite(out).all(axis=1)] = np.nan
        return out

    def image_to_bed(self, px: np.ndarray, z_cmd: float) -> np.ndarray:
        """Pixels ``(N, 2)`` to bed-plane mm ``(N, 2)``, assuming ``h = 0``.

        The height assumption is the catch: a pixel on top of a 20 mm-tall
        object maps to a bed point offset by parallax. Use this for footprint
        and mat work, not for the object's upper surface.
        """
        pixels = np.asarray(px, dtype=float).reshape(-1, 2)
        homography = self.bed_homography(z_cmd)
        try:
            inverse = np.linalg.inv(homography)
        except np.linalg.LinAlgError as exc:
            raise CalibrationError(
                f"bed homography is singular at Z={z_cmd:.2f} mm"
            ) from exc
        homogeneous = np.column_stack([pixels, np.ones(len(pixels))])
        mapped = homogeneous @ inverse.T
        with np.errstate(divide="ignore", invalid="ignore"):
            out = mapped[:, :2] / mapped[:, 2:3]
        out[~np.isfinite(out).all(axis=1)] = np.nan
        return out

    def backproject_rays(self, px: np.ndarray, z_cmd: float) -> np.ndarray:
        """Unit ray directions ``(N, 3)`` in the object frame for given pixels."""
        pixels = np.asarray(px, dtype=float).reshape(-1, 2)
        homogeneous = np.column_stack([pixels, np.ones(len(pixels))])
        directions = homogeneous @ np.linalg.inv(self.K).T @ self.R
        norms = np.linalg.norm(directions, axis=1, keepdims=True)
        norms[norms < 1e-12] = 1.0
        return directions / norms

    # -- scale --------------------------------------------------------------

    def mm_per_px(
        self, z_cmd: float, at_xy_mm: tuple[float, float] | None = None
    ) -> float:
        """Local bed-plane scale in mm per pixel, by finite difference.

        Perspective means this varies across the image; it is sampled at the
        bed centre unless a point is given. Quoted tolerances use this, so an
        honest number here is what keeps the tolerances honest.
        """
        centre = np.array(
            at_xy_mm
            if at_xy_mm is not None
            else (BED_SIZE_X_MM / 2, BED_SIZE_Y_MM / 2),
            dtype=float,
        )
        delta = 1.0
        probes = np.array(
            [centre, centre + [delta, 0.0], centre + [0.0, delta]], dtype=float
        )
        pixels = self.bed_to_image(probes, z_cmd)
        if not np.isfinite(pixels).all():
            return float("nan")
        dx_px = float(np.linalg.norm(pixels[1] - pixels[0]))
        dy_px = float(np.linalg.norm(pixels[2] - pixels[0]))
        mean_px_per_mm = (dx_px + dy_px) / 2.0
        if mean_px_per_mm < 1e-9:
            return float("nan")
        return delta / mean_px_per_mm

    def visible_bed_window(self, z_cmd: float, samples: int = 24) -> dict[str, float]:
        """Axis-aligned bed rectangle visible at this Z, in mm.

        Used by the pose planner to reject stations where the object would leave
        frame. Sampling a grid and keeping what lands inside the image is crude
        but robust; inverting the frustum analytically breaks down when the bed
        plane is near-parallel to a frustum face.
        """
        xs = np.linspace(0.0, BED_SIZE_X_MM, samples)
        ys = np.linspace(0.0, BED_SIZE_Y_MM, samples)
        grid = np.array([(x, y) for y in ys for x in xs], dtype=float)
        pixels = self.bed_to_image(grid, z_cmd)
        inside = (
            np.isfinite(pixels).all(axis=1)
            & (pixels[:, 0] >= 0)
            & (pixels[:, 0] < self.image_width)
            & (pixels[:, 1] >= 0)
            & (pixels[:, 1] < self.image_height)
        )
        if not inside.any():
            return {"x_min": 0.0, "x_max": 0.0, "y_min": 0.0, "y_max": 0.0, "area": 0.0}
        visible = grid[inside]
        x_min, y_min = visible.min(axis=0)
        x_max, y_max = visible.max(axis=0)
        return {
            "x_min": float(x_min),
            "x_max": float(x_max),
            "y_min": float(y_min),
            "y_max": float(y_max),
            "area": float((x_max - x_min) * (y_max - y_min)),
        }

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "fx": self.fx,
            "fy": self.fy,
            "cx": self.cx,
            "cy": self.cy,
            "rvec": self.rvec.tolist(),
            "tvec": self.tvec.tolist(),
            "image_width": self.image_width,
            "image_height": self.image_height,
            "bed_moves_down": self.bed_moves_down,
            "residual_px": self.residual_px,
            "n_observations": self.n_observations,
            "provisional": self.provisional,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RigCalibration:
        return cls(
            fx=float(data["fx"]),
            fy=float(data["fy"]),
            cx=float(data["cx"]),
            cy=float(data["cy"]),
            rvec=np.asarray(data["rvec"], dtype=float),
            tvec=np.asarray(data["tvec"], dtype=float),
            image_width=int(data.get("image_width", 640)),
            image_height=int(data.get("image_height", 480)),
            bed_moves_down=bool(data.get("bed_moves_down", True)),
            residual_px=float(data.get("residual_px", float("nan"))),
            n_observations=int(data.get("n_observations", 0)),
            provisional=bool(data.get("provisional", True)),
            note=str(data.get("note", "")),
        )


def nominal_calibration(
    image_width: int = 640, image_height: int = 480
) -> RigCalibration:
    """A provisional calibration for a stock Q2 chamber camera.

    Derived from the observed geometry rather than measured: the camera sits
    ahead of and above the plate looking back and down across it, with a field
    of view typical of the 640x480 modules these machines ship (~65 degrees
    horizontal, giving ``fx = (w/2) / tan(fov/2)``).

    This is enough to frame an object and plan a sweep. It is **not** enough to
    quote a dimension, which is why ``provisional`` is True and every downstream
    measurement built on it is downgraded to MARGINAL.
    """
    horizontal_fov_deg = 65.0
    fx = (image_width / 2.0) / np.tan(np.radians(horizontal_fov_deg / 2.0))
    fy = fx  # square pixels, the usual case for these modules

    # Camera ahead of the plate in -Y, raised above it, pitched down ~30 deg.
    pitch_deg = 30.0
    pitch = np.radians(pitch_deg)
    # Camera axes relative to bed axes: x right, y down-ish, z into the scene.
    rotation = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.0, -np.sin(pitch), -np.cos(pitch)],
            [0.0, np.cos(pitch), -np.sin(pitch)],
        ]
    )
    centre = np.array([BED_SIZE_X_MM / 2.0, -110.0, 190.0])
    tvec = -rotation @ centre

    return RigCalibration(
        fx=float(fx),
        fy=float(fy),
        cx=image_width / 2.0,
        cy=image_height / 2.0,
        rvec=inverse_rodrigues(rotation),
        tvec=tvec,
        image_width=image_width,
        image_height=image_height,
        bed_moves_down=True,
        residual_px=float("nan"),
        n_observations=0,
        provisional=True,
        note=(
            "nominal geometry, never measured; run 'thox-scan calibrate' with the "
            "printed target before trusting any dimension"
        ),
    )


# -- Solver ------------------------------------------------------------------


@dataclass
class TargetObservation:
    """One detected calibration-target corner.

    ``bed_xy_mm`` is where the corner is on the printed target, in bed
    millimetres; ``pixel`` is where it was found in the frame captured at
    ``z_cmd``.
    """

    bed_xy_mm: tuple[float, float]
    pixel: tuple[float, float]
    z_cmd: float


def _pack(calibration: RigCalibration) -> np.ndarray:
    return np.concatenate(
        [
            [calibration.fx, calibration.fy, calibration.cx, calibration.cy],
            calibration.rvec,
            calibration.tvec,
        ]
    )


def _unpack(theta: np.ndarray, template: RigCalibration) -> RigCalibration:
    return RigCalibration(
        fx=float(theta[0]),
        fy=float(theta[1]),
        cx=float(theta[2]),
        cy=float(theta[3]),
        rvec=theta[4:7],
        tvec=theta[7:10],
        image_width=template.image_width,
        image_height=template.image_height,
        bed_moves_down=template.bed_moves_down,
    )


def _residuals(
    theta: np.ndarray, obs: list[TargetObservation], template: RigCalibration
) -> np.ndarray:
    try:
        candidate = _unpack(theta, template)
    except CalibrationError:
        return np.full(2 * len(obs), 1e6)
    out = np.empty((len(obs), 2))
    # Group by Z so each homography is built once instead of per corner; a
    # calibration sweep is typically a dozen Z values and hundreds of corners.
    by_z: dict[float, list[int]] = {}
    for index, item in enumerate(obs):
        by_z.setdefault(item.z_cmd, []).append(index)
    for z_cmd, indices in by_z.items():
        bed = np.array([obs[i].bed_xy_mm for i in indices], dtype=float)
        seen = np.array([obs[i].pixel for i in indices], dtype=float)
        predicted = candidate.bed_to_image(bed, z_cmd)
        predicted = np.nan_to_num(predicted, nan=1e6, posinf=1e6, neginf=-1e6)
        out[indices] = predicted - seen
    return out.reshape(-1)


def calibrate(
    observations: list[TargetObservation],
    *,
    initial: RigCalibration | None = None,
    max_iterations: int = 120,
    image_width: int = 640,
    image_height: int = 480,
) -> RigCalibration:
    """Solve the ten-parameter rig calibration by Levenberg-Marquardt.

    Args:
        observations: Detected target corners across at least two distinct Z
            stations. Two are the analytic minimum; a real sweep should use the
            full ladder, because the parameters that trade off against each
            other (focal length against camera distance) are only separated by
            the Z motion.
        initial: Starting point. Defaults to :func:`nominal_calibration`.

    Returns:
        A calibration with ``provisional=False`` and a measured RMS residual.

    Raises:
        CalibrationError: If the data is insufficient or the solve diverges.
    """
    if len(observations) < 8:
        raise CalibrationError(
            f"need at least 8 corner observations, got {len(observations)}"
        )
    distinct_z = {round(o.z_cmd, 3) for o in observations}
    if len(distinct_z) < 2:
        raise CalibrationError(
            "all observations share one Z station; the sweep is what separates "
            "focal length from camera distance, so at least two are required"
        )

    template = initial or nominal_calibration(image_width, image_height)
    theta = _pack(template)
    lam = 1e-3
    current = _residuals(theta, observations, template)
    cost = float(current @ current)

    for _ in range(max_iterations):
        # Numeric Jacobian: ten parameters, so a forward difference costs ten
        # residual evaluations. Analytic derivatives would be faster and are not
        # worth the surface area for a solve that finishes in milliseconds.
        jacobian = np.empty((current.size, theta.size))
        for j in range(theta.size):
            step = max(1e-6, abs(theta[j]) * 1e-6)
            bumped = theta.copy()
            bumped[j] += step
            jacobian[:, j] = (
                _residuals(bumped, observations, template) - current
            ) / step

        jtj = jacobian.T @ jacobian
        jtr = jacobian.T @ current
        improved = False
        for _ in range(12):
            try:
                delta = np.linalg.solve(jtj + lam * np.diag(np.diag(jtj) + 1e-12), -jtr)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue
            candidate_theta = theta + delta
            candidate_res = _residuals(candidate_theta, observations, template)
            candidate_cost = float(candidate_res @ candidate_res)
            if candidate_cost < cost:
                theta, current, cost = candidate_theta, candidate_res, candidate_cost
                lam = max(lam * 0.3, 1e-9)
                improved = True
                break
            lam *= 10.0
        if not improved or np.linalg.norm(delta) < 1e-9:
            break

    solved = _unpack(theta, template)
    rms = float(np.sqrt(cost / len(observations)))
    if not np.isfinite(rms):
        raise CalibrationError("calibration diverged; residuals are not finite")
    if rms > 25.0:
        raise CalibrationError(
            f"calibration did not converge (RMS {rms:.1f} px). Check that the "
            "target is flat on the bed and fully visible at every Z station."
        )
    solved.residual_px = rms
    solved.n_observations = len(observations)
    solved.provisional = False
    solved.note = (
        f"solved from {len(observations)} corners over {len(distinct_z)} Z stations"
    )
    return solved
