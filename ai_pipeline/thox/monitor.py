"""The live print-health monitor: a background loop that watches a job.

One thread per monitor, started and stopped through the HTTP layer. It samples
the camera, runs the ensemble, decides whether anything warrants action, and
routes that through :class:`~thox.control.PrintController`.

Three ideas carry most of the design.

**Only watch an actual print.** The loop samples nothing while the printer is
idle. This is not an optimization: a finished part still sitting on the plate
looks exactly like a defect to a vision model. Verified on this machine - with
completed parts on the bed the VLM reported "white blobs on the print bed" at
0.90 confidence, which is a correct description of the pixels and a completely
wrong conclusion about a print that was not running.

**Two-speed sampling.** The classical tripwire runs on every sample at ~20 ms
and no cost. The language models, which cost seconds to minutes per call, run
only when the tripwire is suspicious, during the first layers where failures
cluster, or on a slow periodic deep check so a gradual failure the tripwire
cannot see still gets caught. Without this split, continuous monitoring means a
model call every 45 seconds for the entire print.

**Debounce before acting.** A defect must be seen on ``confirm_frames``
consecutive samples of the same kind before anything happens. A passing toolhead,
a shadow, or one bad exposure will produce a single flagged frame on any real
printer, and a monitor that pauses on those gets switched off within a day -
which protects nothing.

Cadence escalates on suspicion so confirmation is quick: the wait to confirm is
``confirm_frames x alert_interval``, about 36 s at defaults, not
``confirm_frames x sample_interval``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import ThoxSettings
from .control import PrintController
from .defects import DefectKind, HealthVerdict
from .errors import CameraUnavailable, MoonrakerError
from .events import EventKind, EventLog
from .interlock import Interlock
from .moonraker import MoonrakerClient
from .vision import FrameContext, HealthEnsemble

logger = logging.getLogger(__name__)

#: Run a full ensemble pass every N routine samples even when nothing looks
#: wrong. Catches slow failures - warping, gradual under-extrusion - that a
#: frame-to-frame change detector is structurally blind to.
DEEP_CHECK_EVERY = 10

#: Sleep between polls while the printer is idle.
IDLE_POLL_S = 20.0


@dataclass
class Suspicion:
    """Consecutive-detection tracking for one defect kind."""

    kind: DefectKind
    count: int = 0
    best_confidence: float = 0.0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    note: str = ""

    def record(self, confidence: float, note: str) -> None:
        self.count += 1
        self.best_confidence = max(self.best_confidence, confidence)
        self.last_seen = time.time()
        if note:
            self.note = note


class PrintHealthMonitor:
    """Watches the active print and escalates when something looks wrong."""

    def __init__(
        self,
        settings: ThoxSettings | None = None,
        client: MoonrakerClient | None = None,
        ensemble: HealthEnsemble | None = None,
        events: EventLog | None = None,
    ) -> None:
        self.settings = settings or ThoxSettings.from_env()
        self.client = client or MoonrakerClient(self.settings)
        self.ensemble = ensemble or HealthEnsemble(self.settings)
        self.events = events or EventLog(self.settings.state_root)
        self.interlock = Interlock(self.settings)
        self.controller = PrintController(
            self.client, self.settings, self.interlock, self.events
        )

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # Live state, read by the HTTP layer.
        self._latest_verdict: HealthVerdict | None = None
        self._latest_frame: bytes | None = None
        self._latest_frame_at: float = 0.0
        self._previous_frame: bytes | None = None
        self._suspicions: dict[DefectKind, Suspicion] = {}
        self._job_filename: str = ""
        self._sample_count: int = 0
        self._last_snapshot: dict[str, Any] = {}
        self._last_error: str = ""

    # -- lifecycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start the background loop. Returns False if already running."""
        with self._lock:
            if self.running:
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="thox-print-health", daemon=True
            )
            self._thread.start()
        self.events.add(
            EventKind.MONITOR_STARTED,
            f"print-health monitor started ({self.ensemble.describe()})",
            autonomy=self.settings.autonomy,
            providers=self.ensemble.status(),
        )
        return True

    def stop(self, timeout_s: float = 10.0) -> bool:
        """Signal the loop to stop and wait briefly for it."""
        with self._lock:
            if not self.running:
                return False
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_s)
        self.events.add(EventKind.MONITOR_STOPPED, "print-health monitor stopped")
        return True

    # -- the loop -----------------------------------------------------------

    def _run(self) -> None:
        # Pay the model cold-load cost once, up front, rather than on the first
        # frame that actually matters.
        try:
            self.ensemble.warmup()
        except Exception as exc:
            logger.warning("[THOX] warmup failed (%s)", type(exc).__name__)

        while not self._stop.is_set():
            try:
                delay = self._tick()
            except Exception as exc:
                # The loop must survive anything. A monitor that dies silently
                # is worse than no monitor: the UI still shows it as running.
                self._last_error = f"{type(exc).__name__}"
                self.events.add(
                    EventKind.ERROR,
                    f"monitor tick failed ({type(exc).__name__}); continuing",
                )
                logger.exception("[THOX] monitor tick failed")
                delay = self.settings.sample_interval_s
            self._stop.wait(delay)

    def _tick(self) -> float:
        """One sample. Returns how long to wait before the next."""
        try:
            snapshot = self.client.job_snapshot()
        except MoonrakerError as exc:
            self._last_error = f"printer unreachable ({type(exc).__name__})"
            return IDLE_POLL_S

        self._last_error = ""
        self._last_snapshot = snapshot
        state = snapshot["state"]

        if state != "printing":
            self._on_not_printing(state, snapshot)
            return IDLE_POLL_S

        self._on_job_continuity(snapshot)

        try:
            frame = self.client.snapshot()
        except CameraUnavailable as exc:
            self._last_error = str(exc)
            self.events.add(EventKind.ERROR, f"camera unavailable: {exc}")
            return self.settings.sample_interval_s

        self._sample_count += 1
        layer = snapshot.get("current_layer")
        context = FrameContext(
            frame_index=self._sample_count,
            layer=layer if isinstance(layer, int) else None,
            total_layers=snapshot.get("total_layer")
            if isinstance(snapshot.get("total_layer"), int)
            else None,
            progress=snapshot["progress"],
            print_duration_s=snapshot["print_duration_s"],
            filename=snapshot["filename"],
            previous_jpeg=self._previous_frame,
            prior_note=self._prior_note(),
            telemetry=snapshot,
        )

        deep = self._should_run_deep(context)
        verdict = self.ensemble.inspect(frame, context, include_slow=deep)

        self._previous_frame = frame
        self._latest_frame = frame
        self._latest_frame_at = time.time()
        self._latest_verdict = verdict

        self.events.add(
            EventKind.SAMPLE,
            verdict.describe(),
            severity=verdict.severity,
            deep=deep,
            layer=context.layer,
            progress=round(context.progress, 3),
            voted=verdict.voted,
        )

        return self._evaluate(verdict, context, deep)

    # -- state transitions --------------------------------------------------

    def _on_not_printing(self, state: str, snapshot: dict[str, Any]) -> None:
        if self._job_filename:
            self.events.add(
                EventKind.JOB_ENDED,
                f"job ended ({state}): {self._job_filename}",
                state=state,
            )
            self._reset_job()

    def _on_job_continuity(self, snapshot: dict[str, Any]) -> None:
        filename = snapshot.get("filename") or ""
        if filename and filename != self._job_filename:
            self._reset_job()
            self._job_filename = filename
            job_id = _job_id(filename)
            self.events.begin_job(job_id)
            self.events.add(
                EventKind.JOB_STARTED,
                f"watching job: {filename}",
                filename=filename,
                total_layers=snapshot.get("total_layer"),
            )

    def _reset_job(self) -> None:
        self._job_filename = ""
        self._suspicions.clear()
        self._previous_frame = None
        self._sample_count = 0
        self.ensemble.reset()

    # -- decisions ----------------------------------------------------------

    def _should_run_deep(self, context: FrameContext) -> bool:
        """Whether to spend a language-model call on this sample."""
        if not self.ensemble.has_classifier:
            return False
        if self._suspicions:
            return True  # something is already suspected; confirm it fast
        if context.is_first_layers:
            return True  # failures cluster here and are cheapest to recover
        return self._sample_count % DEEP_CHECK_EVERY == 1

    def _prior_note(self) -> str:
        if not self._suspicions:
            return ""
        worst = max(self._suspicions.values(), key=lambda s: s.best_confidence)
        return f"{worst.kind.label} suspected on {worst.count} recent frame(s)"

    def _evaluate(
        self, verdict: HealthVerdict, context: FrameContext, deep: bool
    ) -> float:
        """Update suspicion state and act if a defect is confirmed.

        Returns the delay before the next sample.
        """
        seen_this_frame: set[DefectKind] = set()

        for detection in verdict.detections:
            if not detection.kind.is_print_failure:
                continue
            if detection.severity < self.settings.alert_severity:
                # Below the alert floor: recorded in the sample event, but not
                # tracked toward an action. Otherwise every cosmetic blob
                # accumulates into a confirmed "defect".
                continue
            seen_this_frame.add(detection.kind)
            suspicion = self._suspicions.get(detection.kind)
            if suspicion is None:
                suspicion = Suspicion(kind=detection.kind)
                self._suspicions[detection.kind] = suspicion
                self.events.add(
                    EventKind.SUSPICION,
                    f"possible {detection.kind.label} - watching more closely",
                    severity=detection.severity,
                    kind=detection.kind.value,
                    confidence=detection.confidence,
                    note=detection.note,
                )
            suspicion.record(detection.confidence, detection.note)

        # A kind not seen this frame decays. Only decay on a sample that could
        # actually have seen it: a fast-only sample cannot observe warping, so
        # letting it clear a VLM-detected suspicion would reset the count every
        # other frame and nothing would ever confirm.
        if deep:
            for kind in list(self._suspicions):
                if kind not in seen_this_frame:
                    del self._suspicions[kind]

        confirmed = [
            s
            for s in self._suspicions.values()
            if s.count >= self.settings.confirm_frames
        ]
        if confirmed:
            worst = max(
                confirmed, key=lambda s: (s.kind.urgency.rank, s.best_confidence)
            )
            self._raise_alert(worst)
            return self.settings.alert_interval_s

        if self._suspicions:
            return self.settings.alert_interval_s
        if context.is_first_layers:
            return self.settings.first_layer_interval_s
        return self.settings.sample_interval_s

    def _raise_alert(self, suspicion: Suspicion) -> None:
        kind = suspicion.kind
        severity = max(kind.severity_floor, suspicion.best_confidence * 0.9)

        self.events.add(
            EventKind.ALERT,
            f"{kind.label} confirmed on {suspicion.count} consecutive samples "
            f"(confidence {suspicion.best_confidence:.2f})",
            severity=severity,
            kind=kind.value,
            urgency=kind.urgency.value,
            confidence=suspicion.best_confidence,
            note=suspicion.note,
            recoverable_by_pause=kind.recoverable_by_pause,
            fix_hint=list(kind.fix_hint),
        )

        if not kind.recoverable_by_pause:
            # Pausing would achieve nothing except a blob where the nozzle sat.
            # Alerting is the whole response, and the fix belongs in the next
            # slice via thox.revise.
            self.events.add(
                EventKind.ACTION_SUGGESTED,
                f"{kind.label} does not improve by pausing; review the part and "
                "consider a revised reprint",
                severity=severity,
                kind=kind.value,
                suggested="revise",
            )
            self._suspicions.pop(kind, None)
            return

        why = (
            f"{kind.label} confirmed on {suspicion.count} samples at "
            f"{suspicion.best_confidence:.2f} confidence"
        )

        if suspicion.best_confidence < self.settings.auto_pause_confidence:
            self.events.add(
                EventKind.ACTION_SUGGESTED,
                f"suggest pausing: {why} (below the "
                f"{self.settings.auto_pause_confidence:.2f} auto-pause threshold, "
                "so a human decides)",
                severity=severity,
                kind=kind.value,
                suggested="pause",
            )
            self._suspicions.pop(kind, None)
            return

        # High confidence and pausing helps. The interlock still decides whether
        # the configured autonomy permits the agent to do it.
        result = self.controller.pause(actor="agent", why=why)
        if not result.ok and result.reason == "not_permitted":
            # Already logged as a suggestion by the controller; nothing to add.
            pass
        self._suspicions.pop(kind, None)

    # -- introspection ------------------------------------------------------

    def snapshot_state(self) -> dict[str, Any]:
        """Everything the UI needs in one object."""
        verdict = self._latest_verdict
        return {
            "running": self.running,
            "autonomy": self.settings.autonomy,
            "job": self._last_snapshot,
            "watching": self._job_filename,
            "samples": self._sample_count,
            "last_error": self._last_error,
            "frame_age_s": (
                round(time.time() - self._latest_frame_at, 1)
                if self._latest_frame_at
                else None
            ),
            "verdict": verdict.to_dict() if verdict else None,
            "suspicions": [
                {
                    "kind": s.kind.value,
                    "label": s.kind.label,
                    "count": s.count,
                    "needed": self.settings.confirm_frames,
                    "confidence": round(s.best_confidence, 3),
                    "note": s.note[:300],
                }
                for s in self._suspicions.values()
            ],
            "ensemble": self.ensemble.status(),
            "thresholds": {
                "alert_severity": self.settings.alert_severity,
                "auto_pause_confidence": self.settings.auto_pause_confidence,
                "confirm_frames": self.settings.confirm_frames,
            },
        }

    @property
    def latest_frame(self) -> bytes | None:
        return self._latest_frame


def _job_id(filename: str) -> str:
    """Filesystem-safe id for a job, stable across a single print."""
    import re

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", filename)[:60] or "job"
    return f"{stamp}_{stem}"
