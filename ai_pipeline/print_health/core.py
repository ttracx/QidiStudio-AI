"""Closed-loop print-health supervisor.

Lifecycle:
    Q2 snapshot + Moonraker telemetry
        -> OpenAI + Ollama local + Ollama Cloud in parallel
        -> deterministic confidence-weighted fusion
        -> adaptive sampling
        -> policy (observe / assist / autopause / closed_loop)
        -> guarded Moonraker action
        -> deterministic bounded remediation
        -> QidiStudio/QIDISlicer re-slice + upload + reprint
        -> revision/event history
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    Action,
    AutonomyMode,
    BoundingBox,
    Detection,
    FusedAssessment,
    ProviderAssessment,
    Revision,
    Severity,
)
from .moonraker import MoonrakerClient, MoonrakerError
from .providers import ProviderError, build_providers
from .remediation import (
    RemediationError,
    SliceContext,
    diagnose_changes,
    reslice,
)

logger = logging.getLogger("thoxforge.print_health")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


@dataclass
class PrintHealthSettings:
    printer_host: str = "10.1.10.153"
    moonraker_port: int = 7125
    moonraker_api_key: str = ""
    mode: AutonomyMode = AutonomyMode.AUTOPAUSE
    healthy_interval_s: float = 15.0
    suspect_interval_s: float = 5.0
    warning_interval_s: float = 2.5
    critical_interval_s: float = 1.0
    warning_threshold: float = 0.45
    critical_threshold: float = 0.76
    auto_pause_confidence: float = 0.78
    min_consensus_providers: int = 2
    critical_frames_before_closed_loop: int = 2
    max_retries: int = 2
    allow_agent_cancel: bool = False
    allow_agent_restart: bool = False
    allow_agent_reprint: bool = False
    state_dir: str = "~/.thoxforge/print-health"
    provider_timeout_s: float = 45.0

    @classmethod
    def from_env(cls) -> "PrintHealthSettings":
        raw_mode = os.getenv("THOX_PRINT_HEALTH_MODE", "autopause").strip().lower()
        try:
            mode = AutonomyMode(raw_mode)
        except ValueError:
            mode = AutonomyMode.AUTOPAUSE
        return cls(
            printer_host=os.getenv("THOX_QIDI_HOST", "10.1.10.153").strip(),
            moonraker_port=_env_int("THOX_MOONRAKER_PORT", 7125, 1, 65535),
            moonraker_api_key=os.getenv("THOX_MOONRAKER_API_KEY", "").strip(),
            mode=mode,
            healthy_interval_s=_env_float("THOX_HEALTHY_SAMPLE_S", 15.0, 2.0, 300.0),
            suspect_interval_s=_env_float("THOX_SUSPECT_SAMPLE_S", 5.0, 1.0, 120.0),
            warning_interval_s=_env_float("THOX_WARNING_SAMPLE_S", 2.5, 1.0, 60.0),
            critical_interval_s=_env_float("THOX_CRITICAL_SAMPLE_S", 1.0, 1.0, 30.0),
            warning_threshold=_env_float("THOX_WARNING_THRESHOLD", 0.45, 0.05, 0.95),
            critical_threshold=_env_float("THOX_CRITICAL_THRESHOLD", 0.76, 0.20, 0.99),
            auto_pause_confidence=_env_float("THOX_AUTO_PAUSE_CONFIDENCE", 0.78, 0.40, 0.99),
            min_consensus_providers=_env_int("THOX_MIN_VISION_PROVIDERS", 2, 1, 3),
            critical_frames_before_closed_loop=_env_int("THOX_CRITICAL_CONFIRM_FRAMES", 2, 1, 5),
            max_retries=_env_int("THOX_MAX_REPRINTS", 2, 0, 10),
            allow_agent_cancel=_env_bool("THOX_ALLOW_AGENT_CANCEL", False),
            allow_agent_restart=_env_bool("THOX_ALLOW_AGENT_RESTART", False),
            allow_agent_reprint=_env_bool("THOX_ALLOW_AGENT_REPRINT", False),
            state_dir=os.getenv("THOX_PRINT_HEALTH_STATE_DIR", "~/.thoxforge/print-health"),
            provider_timeout_s=_env_float("THOX_VISION_TIMEOUT_S", 45.0, 5.0, 180.0),
        )


@dataclass
class RegisteredJob:
    job_id: str
    source_model: str
    base_profile: str
    slicer_bin: str
    workdir: str
    current_gcode: str = ""
    remote_filename: str = ""
    retry_count: int = 0
    registered_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RevisionStore:
    """Crash-safe JSON event/revision persistence on the Mac host."""

    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.json"
        self._lock = threading.RLock()
        self.events: list[dict[str, Any]] = []
        self.revisions: list[dict[str, Any]] = []
        self.job: RegisteredJob | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.events = data.get("events", [])[-500:]
                self.revisions = data.get("revisions", [])[-100:]
                raw_job = data.get("job")
                if isinstance(raw_job, dict):
                    self.job = RegisteredJob(**raw_job)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning("Ignoring unreadable Print Health state file %s", self.path)

    def _save(self) -> None:
        data = {
            "job": self.job.to_dict() if self.job else None,
            "events": self.events[-500:],
            "revisions": self.revisions[-100:],
        }
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def set_job(self, job: RegisteredJob | None) -> None:
        with self._lock:
            self.job = job
            self._save()

    def add_event(self, event_type: str, detail: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            event = {
                "id": f"evt-{int(time.time() * 1000)}-{len(self.events) % 1000}",
                "at": _utcnow(),
                "type": event_type,
                "detail": detail[:2000],
                "payload": payload or {},
            }
            self.events.append(event)
            self.events = self.events[-500:]
            self._save()
            return event

    def add_revision(self, revision: Revision, diff: dict[str, Any]) -> None:
        with self._lock:
            self.revisions.append({"revision": revision.to_dict(), "diff": diff})
            self.revisions = self.revisions[-100:]
            self._save()


def _average_box(items: list[tuple[BoundingBox, float]]) -> BoundingBox:
    total = sum(max(weight, 0.05) for _, weight in items) or 1.0
    return BoundingBox(
        x1=round(sum(box.x1 * max(weight, 0.05) for box, weight in items) / total),
        y1=round(sum(box.y1 * max(weight, 0.05) for box, weight in items) / total),
        x2=round(sum(box.x2 * max(weight, 0.05) for box, weight in items) / total),
        y2=round(sum(box.y2 * max(weight, 0.05) for box, weight in items) / total),
    )


def fuse_assessments(
    assessments: list[ProviderAssessment],
    failures: list[str],
    settings: PrintHealthSettings,
) -> FusedAssessment:
    """Confidence-weighted fusion with disagreement downgrade and adaptive sampling."""
    if not assessments:
        raise ProviderError("all vision providers failed")

    severity_weight = {Severity.OK: 0.0, Severity.WARNING: 0.50, Severity.CRITICAL: 1.0}
    total_weight = sum(max(0.05, item.confidence) for item in assessments)
    risk = sum(
        severity_weight[item.severity] * max(0.05, item.confidence)
        for item in assessments
    ) / total_weight
    quality = round(sum(
        item.quality_score * max(0.05, item.confidence) for item in assessments
    ) / total_weight)
    avg_confidence = sum(item.confidence for item in assessments) / len(assessments)
    critical_votes = sum(
        1 for item in assessments
        if item.severity is Severity.CRITICAL and item.confidence >= 0.65
    )
    enough = len(assessments) >= settings.min_consensus_providers

    by_defect: dict[str, list[Detection]] = {}
    for assessment in assessments:
        for detection in assessment.detections:
            if detection.confidence >= 0.30:
                by_defect.setdefault(detection.defect, []).append(detection)

    fused_detections: list[Detection] = []
    for defect, items in by_defect.items():
        combined_confidence = 1.0
        for item in items:
            combined_confidence *= 1.0 - item.confidence
        combined_confidence = 1.0 - combined_confidence
        # Single-provider claims are downweighted when other models are available.
        if len(items) == 1 and len(assessments) > 1:
            combined_confidence *= 0.72
        item_severity = max(items, key=lambda value: severity_weight[value.severity]).severity
        fused_detections.append(Detection(
            defect=defect,
            confidence=round(max(0.0, min(1.0, combined_confidence)), 3),
            severity=item_severity,
            bbox=_average_box([(item.bbox, item.confidence) for item in items]),
            note="; ".join(item.note for item in items if item.note)[:500],
        ))
    fused_detections.sort(key=lambda item: item.confidence, reverse=True)

    smoke = next((item for item in fused_detections if item.defect == "smoke_or_fire"), None)
    collision = next((item for item in fused_detections if item.defect == "collision"), None)
    if smoke and smoke.confidence >= 0.90 and enough:
        severity, action = Severity.CRITICAL, Action.EMERGENCY_STOP
    elif collision and collision.confidence >= 0.92 and enough:
        severity, action = Severity.CRITICAL, Action.PAUSE
    elif enough and (risk >= settings.critical_threshold or critical_votes >= 2):
        severity, action = Severity.CRITICAL, Action.PAUSE
    elif risk >= settings.warning_threshold or (not enough and critical_votes > 0):
        severity, action = Severity.WARNING, Action.ASK_USER
    else:
        severity, action = Severity.OK, Action.CONTINUE

    # Adaptive sampling ramps up before a failure is certain.
    top_defect_conf = max((item.confidence for item in fused_detections), default=0.0)
    if severity is Severity.CRITICAL:
        interval = settings.critical_interval_s
    elif severity is Severity.WARNING:
        interval = settings.warning_interval_s
    elif top_defect_conf >= 0.35 or risk >= settings.warning_threshold * 0.65:
        interval = settings.suspect_interval_s
    else:
        interval = settings.healthy_interval_s

    rationale = (
        f"providers={len(assessments)}, failures={len(failures)}, risk={risk:.3f}, "
        f"critical_votes={critical_votes}, min_consensus={settings.min_consensus_providers}"
    )
    if not enough:
        rationale += "; autonomous destructive actions suppressed: insufficient provider consensus"

    return FusedAssessment(
        quality_score=max(0, min(100, quality)),
        confidence=round(avg_confidence, 3),
        severity=severity,
        detections=fused_detections[:16],
        recommended_action=action,
        providers=assessments,
        provider_failures=failures,
        rationale=rationale,
        sample_interval_s=interval,
    )


class PrintHealthService:
    def __init__(self, settings: PrintHealthSettings | None = None) -> None:
        self.settings = settings or PrintHealthSettings.from_env()
        self.moonraker = MoonrakerClient(
            self.settings.printer_host,
            self.settings.moonraker_port,
            self.settings.moonraker_api_key,
        )
        self.providers = build_providers(self.settings.provider_timeout_s)
        self.store = RevisionStore(self.settings.state_dir)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_assessment: FusedAssessment | None = None
        self._last_telemetry: dict[str, Any] = {}
        self._last_frame_at = ""
        self._paused_by_vision = False
        self._critical_streak = 0
        self._next_interval_s = self.settings.healthy_interval_s
        self._last_error = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_mode(self, value: str) -> AutonomyMode:
        mode = AutonomyMode(value)
        with self._lock:
            self.settings.mode = mode
            self.store.add_event("policy", f"Autonomy mode changed to {mode.value}")
        return mode

    def register_job(self, data: dict[str, Any]) -> RegisteredJob:
        required = ["source_model", "base_profile"]
        for key in required:
            if not str(data.get(key, "")).strip():
                raise ValueError(f"{key} is required")
        job_id = str(data.get("job_id") or f"job-{int(time.time())}")[:120]
        root = Path(self.settings.state_dir).expanduser().resolve() / "jobs" / job_id
        root.mkdir(parents=True, exist_ok=True)
        job = RegisteredJob(
            job_id=job_id,
            source_model=str(Path(str(data["source_model"])).expanduser()),
            base_profile=str(Path(str(data["base_profile"])).expanduser()),
            slicer_bin=str(data.get("slicer_bin") or os.getenv("THOX_QIDI_SLICER_BIN", "qidi-slicer")),
            workdir=str(root),
            current_gcode=str(data.get("current_gcode", "")),
            remote_filename=str(data.get("remote_filename", "")),
            retry_count=0,
            registered_at=_utcnow(),
        )
        self.store.set_job(job)
        self.store.add_event("job_registered", f"Registered {job.job_id}", job.to_dict())
        return job

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="thox-print-health", daemon=True)
            self._thread.start()
            self.store.add_event("monitor", "Print Health monitor started")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None
        self.store.add_event("monitor", "Print Health monitor stopped")

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "printer": {
                    "host": self.settings.printer_host,
                    "moonraker_port": self.settings.moonraker_port,
                    "online": self.moonraker.ping(),
                },
                "mode": self.settings.mode.value,
                "providers": [getattr(provider, "name", "unknown") for provider in self.providers],
                "telemetry": self._last_telemetry,
                "assessment": self._last_assessment.to_dict() if self._last_assessment else None,
                "last_frame_at": self._last_frame_at,
                "paused_by_vision": self._paused_by_vision,
                "critical_streak": self._critical_streak,
                "next_sample_s": self._next_interval_s,
                "last_error": self._last_error,
                "job": self.store.job.to_dict() if self.store.job else None,
                "retry_budget": {
                    "used": self.store.job.retry_count if self.store.job else 0,
                    "max": self.settings.max_retries,
                },
                "safety": {
                    "allow_agent_cancel": self.settings.allow_agent_cancel,
                    "allow_agent_restart": self.settings.allow_agent_restart,
                    "allow_agent_reprint": self.settings.allow_agent_reprint,
                    "auto_pause_confidence": self.settings.auto_pause_confidence,
                    "min_consensus_providers": self.settings.min_consensus_providers,
                },
            }

    def camera_frame(self) -> tuple[bytes, str]:
        return self.moonraker.snapshot()

    def analyze_once(self) -> FusedAssessment:
        frame, mime = self.moonraker.snapshot()
        telemetry = self.moonraker.telemetry()
        context = json.dumps(telemetry, separators=(",", ":"))
        assessments: list[ProviderAssessment] = []
        failures: list[str] = []
        if not self.providers:
            raise ProviderError("no vision providers are configured")
        with ThreadPoolExecutor(max_workers=len(self.providers)) as pool:
            futures = {
                pool.submit(provider.analyze, frame, mime, context): provider
                for provider in self.providers
            }
            for future in as_completed(futures):
                provider = futures[future]
                try:
                    assessments.append(future.result())
                except Exception as exc:
                    name = getattr(provider, "name", "unknown")
                    failures.append(f"{name}: {type(exc).__name__}: {str(exc)[:300]}")
                    logger.warning("Vision provider %s failed: %s", name, exc)
        fused = fuse_assessments(assessments, failures, self.settings)
        with self._lock:
            self._last_assessment = fused
            self._last_telemetry = telemetry
            self._last_frame_at = _utcnow()
            self._next_interval_s = fused.sample_interval_s
            self._last_error = ""
        self.store.add_event(
            "assessment",
            f"{fused.severity.value}: quality={fused.quality_score} confidence={fused.confidence:.2f}",
            fused.to_dict(),
        )
        self._apply_policy(fused, telemetry)
        return fused

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                telemetry = self.moonraker.telemetry()
                state = str(telemetry.get("state", "")).lower()
                # Only spend model calls while printing or while Print Health paused it.
                if state == "printing" or (state == "paused" and self._paused_by_vision):
                    self.analyze_once()
                else:
                    with self._lock:
                        self._last_telemetry = telemetry
                        self._next_interval_s = self.settings.healthy_interval_s
                    self._critical_streak = 0
            except Exception as exc:
                logger.warning("Print Health loop iteration failed: %s", exc)
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                    self._next_interval_s = min(30.0, max(5.0, self.settings.suspect_interval_s))
                self.store.add_event("error", "Print Health iteration failed", {"error": self._last_error})
            self._stop.wait(self._next_interval_s)

    def _apply_policy(self, assessment: FusedAssessment, telemetry: dict[str, Any]) -> None:
        mode = self.settings.mode
        enough = len(assessment.providers) >= self.settings.min_consensus_providers
        high_confidence = assessment.confidence >= self.settings.auto_pause_confidence
        if assessment.severity is Severity.CRITICAL:
            self._critical_streak += 1
        else:
            self._critical_streak = 0

        if assessment.recommended_action is Action.EMERGENCY_STOP:
            # Emergency stop remains human-gated unless explicitly requested via API.
            self.store.add_event(
                "action_required",
                "Vision detected possible smoke/fire; emergency stop requires explicit confirmation.",
                assessment.to_dict(),
            )
            return

        if mode in {AutonomyMode.OBSERVE, AutonomyMode.ASSIST}:
            if assessment.severity is not Severity.OK:
                self.store.add_event(
                    "suggestion",
                    f"Suggested action: {assessment.recommended_action.value}",
                    assessment.to_dict(),
                )
            return

        if (
            assessment.severity is Severity.CRITICAL
            and enough
            and high_confidence
            and str(telemetry.get("state", "")).lower() == "printing"
        ):
            try:
                self.moonraker.pause()
                self._paused_by_vision = True
                self.store.add_event(
                    "action",
                    "Auto-paused print after high-confidence fused vision failure.",
                    assessment.to_dict(),
                )
            except MoonrakerError as exc:
                self.store.add_event("error", "Auto-pause failed", {"error": str(exc)})
                return

        if mode is not AutonomyMode.CLOSED_LOOP:
            return
        if self._critical_streak < self.settings.critical_frames_before_closed_loop:
            return
        if not (self.settings.allow_agent_cancel and self.settings.allow_agent_reprint):
            self.store.add_event(
                "action_required",
                "Closed-loop remediation is ready but agent cancel/reprint gates are disabled.",
            )
            return
        job = self.store.job
        if job is None:
            self.store.add_event("action_required", "Closed-loop remediation needs a registered source job.")
            return
        if job.retry_count >= self.settings.max_retries:
            self.store.add_event(
                "retry_gate",
                f"Max automatic reprints reached ({self.settings.max_retries}); human approval required.",
            )
            return
        try:
            self.remediate(confirm=True, auto_start=True, actor="agent")
            self._critical_streak = 0
        except Exception as exc:
            self.store.add_event("error", "Closed-loop remediation failed", {"error": str(exc)[:1000]})

    def control(self, action: str, *, confirm: bool, actor: str = "human") -> dict[str, Any]:
        try:
            requested = Action(action)
        except ValueError as exc:
            raise ValueError(f"unsupported action: {action}") from exc
        destructive = requested in {Action.CANCEL, Action.RESTART, Action.EMERGENCY_STOP}
        if destructive and not confirm:
            raise PermissionError(f"{requested.value} requires explicit confirmation")
        if actor == "agent":
            if requested is Action.CANCEL and not self.settings.allow_agent_cancel:
                raise PermissionError("agent cancel is disabled")
            if requested is Action.RESTART and not self.settings.allow_agent_restart:
                raise PermissionError("agent restart is disabled")
            if requested is Action.EMERGENCY_STOP:
                raise PermissionError("emergency stop is never autonomous")

        if requested is Action.PAUSE:
            self.moonraker.pause()
            self._paused_by_vision = actor == "agent"
        elif requested is Action.RESUME:
            self.moonraker.resume()
            self._paused_by_vision = False
        elif requested is Action.CANCEL:
            self.moonraker.cancel()
            self._paused_by_vision = False
        elif requested is Action.RESTART:
            self.moonraker.restart()
        elif requested is Action.EMERGENCY_STOP:
            self.moonraker.emergency_stop()
        else:
            raise ValueError(f"action {requested.value} is not a direct printer control")
        event = self.store.add_event(
            "action",
            f"{actor} performed {requested.value}",
            {"confirmed": confirm},
        )
        return {"ok": True, "action": requested.value, "event": event}

    def remediate(self, *, confirm: bool, auto_start: bool, actor: str = "human") -> dict[str, Any]:
        if not confirm:
            raise PermissionError("remediation requires explicit confirmation")
        if actor == "agent" and not (
            self.settings.mode is AutonomyMode.CLOSED_LOOP
            and self.settings.allow_agent_cancel
            and self.settings.allow_agent_reprint
        ):
            raise PermissionError("autonomous remediation safety gates are not enabled")
        job = self.store.job
        assessment = self._last_assessment
        if job is None:
            raise ValueError("no source job is registered")
        if assessment is None:
            raise ValueError("no vision assessment is available")
        if job.retry_count >= self.settings.max_retries:
            raise PermissionError("maximum reprint count reached; human review required")

        changes = diagnose_changes(assessment)
        if not changes.notes:
            raise RemediationError("vision assessment has no defect-specific safe remediation")
        next_revision = len(self.store.revisions) + 1
        context = SliceContext(
            source_model=job.source_model,
            base_profile=job.base_profile,
            slicer_bin=job.slicer_bin,
            workdir=job.workdir,
            previous_gcode=job.current_gcode,
        )
        defects = [item.defect for item in assessment.detections if item.confidence >= 0.45]
        reason = "; ".join(changes.notes)

        # Cancel only after a replacement plan is known to be constructible. Actual
        # slicing happens first while the failed print remains paused.
        revision, diff = reslice(context, changes, next_revision, reason, defects)
        remote_name = ""
        try:
            telemetry = self.moonraker.telemetry()
            if str(telemetry.get("state", "")).lower() in {"printing", "paused"}:
                self.moonraker.cancel()
            remote_name = self.moonraker.upload_gcode(revision.output_gcode)
            if auto_start:
                self.moonraker.start_print(remote_name)
                revision.status = "printing"
            else:
                revision.status = "uploaded"
        except Exception:
            revision.status = "upload_or_start_failed"
            self.store.add_revision(revision, diff)
            raise

        job.current_gcode = revision.output_gcode
        job.remote_filename = remote_name
        job.retry_count += 1
        self.store.set_job(job)
        self.store.add_revision(revision, diff)
        self._paused_by_vision = False
        self.store.add_event(
            "reprint",
            f"Generated revision {revision.revision}; retry {job.retry_count}/{self.settings.max_retries}",
            {"revision": revision.to_dict(), "diff": diff, "remote_filename": remote_name},
        )
        return {
            "ok": True,
            "revision": revision.to_dict(),
            "diff": diff,
            "remote_filename": remote_name,
            "auto_started": auto_start,
            "retry_count": job.retry_count,
            "max_retries": self.settings.max_retries,
        }

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        return list(reversed(self.store.events[-limit:]))

    def revisions(self) -> list[dict[str, Any]]:
        return list(reversed(self.store.revisions))

    def capture_scan_frame(self, pose: dict[str, Any] | None = None) -> dict[str, Any]:
        """Capture a calibrated scan frame using optional allowlisted toolhead pose.

        The fixed Q2 camera cannot create arbitrary new viewpoints by moving the
        nozzle. These poses provide known scale/fiducial references; true multi-view
        surface reconstruction still benefits from rotating the object between
        captures. Captures can be passed to the repo's existing /generate multi-image
        TRELLIS path.
        """
        if pose:
            self.moonraker.safe_scan_pose(
                float(pose.get("x", 135.0)),
                float(pose.get("y", 135.0)),
                float(pose["z"]) if pose.get("z") is not None else None,
            )
            time.sleep(0.35)
        frame, mime = self.moonraker.snapshot()
        scan_dir = Path(self.settings.state_dir).expanduser().resolve() / "scans"
        scan_dir.mkdir(parents=True, exist_ok=True)
        ext = ".png" if "png" in mime else ".jpg"
        filename = scan_dir / f"scan_{int(time.time() * 1000)}{ext}"
        filename.write_bytes(frame)
        event = self.store.add_event(
            "scan_capture",
            f"Captured object scan frame {filename.name}",
            {"path": str(filename), "mime": mime, "pose": pose or {}},
        )
        return {"path": str(filename), "mime": mime, "event": event}
