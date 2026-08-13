"""Classical change-detection tripwire. No credentials, no network, ~20 ms.

This provider is deliberately **honest about being a detector, not a
classifier**. Frame differencing can tell you that a lot of the image changed
suddenly, or that the frame is too dark to judge. It cannot tell spaghetti from
warping - those look nothing alike to a human and identical to a difference
image.

So it reports exactly two things it can actually support:

``CAMERA_FAULT``
    Exposure and focus, measured directly. Fully reliable, and the single most
    useful signal in the whole system: a monitor that keeps "detecting nothing"
    on a black frame is worse than no monitor, because it manufactures
    confidence that the print is fine.

A gross-change signal
    A large, sudden rise in image change maps to ``PRINT_CAME_LOOSE`` or
    ``SPAGHETTI`` at **modest confidence**, with a note saying it is a change
    detector. Its job is not to be right about which failure it is - it is to
    escalate the sampling cadence so the language models get asked sooner. On a
    printer whose camera watches a moving gantry, this is the cheap always-on
    layer that makes a 45-second polling interval acceptable.

Why the toolhead does not trip it: a normal print has the gantry sweeping
through frame constantly, which is a large change every time. The heuristic
therefore compares against a **rolling baseline of recent change**, not against
an absolute threshold - it fires when change is far above what this print has
been doing, not when change is merely large.

numpy + Pillow only, so it installs wherever the existing pipeline does.
"""

from __future__ import annotations

import io
import time
from collections import deque

import numpy as np
from PIL import Image, ImageFilter

from ..defects import Detection, DefectKind, ProviderReport
from .base import FrameContext

#: Frames of history used for the rolling baseline. About 8 samples at the
#: normal cadence is ~6 minutes of print - long enough to describe "normal" for
#: this job, short enough to adapt as the part grows.
_BASELINE_WINDOW = 8

#: How many times above baseline a change must be to count as gross.
_GROSS_CHANGE_FACTOR = 3.5

#: Absolute floor, so a print with a near-static camera view (baseline ~0)
#: cannot make any tiny flicker look like a 10x spike.
_MIN_ABSOLUTE_CHANGE = 0.06


def _decode_gray(jpeg: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(jpeg)) as image:
        return np.asarray(image.convert("L"), dtype=np.float32)


def _laplacian_variance(gray: np.ndarray) -> float:
    """Focus measure. Low variance means blurred or featureless."""
    kernel = np.array([[0.0, 1.0, 0.0], [1.0, -4.0, 1.0], [0.0, 1.0, 0.0]])
    padded = np.pad(gray, 1, mode="edge")
    response = np.zeros_like(gray)
    for dy in range(3):
        for dx in range(3):
            weight = kernel[dy, dx]
            if weight:
                response += weight * padded[
                    dy : dy + gray.shape[0], dx : dx + gray.shape[1]
                ]
    return float(response.var())


class MotionTripwire:
    """Cheap always-on change detector and frame-quality gate."""

    name = "cv_motion"
    sends_frames_offsite = False
    #: Answers in ~20 ms, so it runs on every routine sample.
    fast = True

    def __init__(self) -> None:
        self._baseline: deque[float] = deque(maxlen=_BASELINE_WINDOW)

    def available(self) -> tuple[bool, str]:
        return True, "classical CV, no configuration required"

    def warmup(self) -> None:
        return None

    def reset(self) -> None:
        """Clear per-job state. Called when a new print starts."""
        self._baseline.clear()

    def inspect(self, jpeg: bytes, context: FrameContext) -> ProviderReport:
        started = time.perf_counter()
        try:
            gray = _decode_gray(jpeg)
        except Exception as exc:
            return ProviderReport(
                provider=self.name,
                ok=False,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                skipped_reason=f"could not decode frame ({type(exc).__name__})",
            )

        detections: list[Detection] = []
        notes: list[str] = []

        # -- frame quality ---------------------------------------------------
        mean = float(gray.mean())
        focus = _laplacian_variance(gray)
        blown = float((gray > 251).mean())

        problems: list[str] = []
        if mean < 25:
            problems.append(
                f"frame is nearly black (mean {mean:.0f}/255); turn on the "
                "chamber LED or the monitor is watching nothing"
            )
        if focus < 8.0:
            problems.append(f"frame is blurred or featureless (focus {focus:.1f})")
        if blown > 0.25:
            problems.append(f"{blown * 100:.0f}% of the frame is blown out")

        if problems:
            detections.append(
                Detection(
                    kind=DefectKind.CAMERA_FAULT,
                    confidence=0.9,
                    provider=self.name,
                    note="; ".join(problems),
                )
            )
            # A frame this bad cannot support a change judgement either, so the
            # baseline is left untouched rather than poisoned.
            return ProviderReport(
                provider=self.name,
                ok=True,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                detections=detections,
                summary="; ".join(problems),
            )

        # -- change against the rolling baseline -----------------------------
        change = float("nan")
        if context.previous_jpeg:
            try:
                previous = _decode_gray(context.previous_jpeg)
                if previous.shape == gray.shape:
                    # Blur both before differencing: without it, JPEG block
                    # noise and the camera's auto-exposure hunting dominate the
                    # signal and every frame looks like a big change.
                    smooth_now = np.asarray(
                        Image.fromarray(gray.astype(np.uint8)).filter(
                            ImageFilter.GaussianBlur(2)
                        ),
                        dtype=np.float32,
                    )
                    smooth_prev = np.asarray(
                        Image.fromarray(previous.astype(np.uint8)).filter(
                            ImageFilter.GaussianBlur(2)
                        ),
                        dtype=np.float32,
                    )
                    change = float((np.abs(smooth_now - smooth_prev) > 28).mean())
            except Exception:
                change = float("nan")

        if np.isfinite(change):
            baseline = float(np.median(self._baseline)) if self._baseline else None
            notes.append(f"frame change {change * 100:.1f}%")

            if (
                baseline is not None
                and len(self._baseline) >= 3
                and change > _MIN_ABSOLUTE_CHANGE
                and change > max(baseline * _GROSS_CHANGE_FACTOR, _MIN_ABSOLUTE_CHANGE)
            ):
                # Confidence stays modest by design. This says "something big
                # happened", not "I know what it was"; the VLMs classify it.
                ratio = change / max(baseline, 1e-6)
                confidence = float(min(0.6, 0.25 + 0.05 * ratio))
                kind = (
                    DefectKind.PRINT_CAME_LOOSE
                    if not context.is_first_layers
                    else DefectKind.ADHESION
                )
                detections.append(
                    Detection(
                        kind=kind,
                        confidence=confidence,
                        provider=self.name,
                        note=(
                            f"change detector only (not a classifier): "
                            f"{change * 100:.1f}% of the frame changed, "
                            f"{ratio:.1f}x this print's recent baseline of "
                            f"{baseline * 100:.1f}%. Escalating for a model to "
                            "classify."
                        ),
                    )
                )
            self._baseline.append(change)

        return ProviderReport(
            provider=self.name,
            ok=True,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            detections=detections,
            summary="; ".join(notes) if notes else "frame usable, no gross change",
        )
