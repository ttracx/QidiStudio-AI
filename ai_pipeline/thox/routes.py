"""Flask blueprint exposing the THOX printer-agent layer.

Registered onto the **existing** ``ai_pipeline/server.py`` app rather than
running a second service, so QIDI Studio talks to one sidecar on one port and
inherits the security posture already established there: loopback-only by
default, no-store headers, a body-size ceiling, and internal errors that return
a reference id instead of a traceback.

Two conventions the UI depends on:

**409 means refused, not broken.** When the interlock declines - a print is
running, Z is unhomed, the hotend is hot - the printer was not touched and the
operator can clear the condition. That is a different thing from a server fault,
so it gets 409 with a machine-readable ``reason``. Reserving 500 for genuine
faults is what lets a panel say "finish your print first" instead of "error".

**Long work returns a job id, not a held connection.** A scan takes minutes and
a revision can rewrite a large file. Those run on a worker thread and progress
arrives over ``/thox/events``; only quick reads answer inline.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Any

from flask import Blueprint, Response, jsonify, request

from .config import ConfigError, ThoxSettings
from .control import ACTIONS, PrintController
from .defects import DefectKind, parse_kind
from .errors import MoonrakerError, ThoxError, ThoxRefused
from .events import EventKind, EventLog
from .interlock import Interlock
from .monitor import PrintHealthMonitor
from .moonraker import MoonrakerClient
from .revise import apply_revision, plan_revision, read_gcode_params
from .scan.poses import ObjectPlacement, describe_plan, plan_session
from .scan.rig import nominal_calibration
from .scan.runner import ScanRunner

logger = logging.getLogger(__name__)

thox_bp = Blueprint("thox", __name__, url_prefix="/thox")

# Process-wide singletons, created lazily so importing this module never opens a
# socket and never raises on a missing THOX_PRINTER_HOST.
#
# RLock, not Lock. The accessors compose - monitor() needs settings() and
# client() - so a plain Lock deadlocks the moment one holds it while calling
# another. That is not hypothetical: it hung the very first request to
# /thox/health, because monitor() called settings() from inside the lock.
_lock = threading.RLock()
_settings: ThoxSettings | None = None
_client: MoonrakerClient | None = None
_monitor: PrintHealthMonitor | None = None
_events: EventLog | None = None
_jobs: dict[str, dict[str, Any]] = {}


def settings() -> ThoxSettings:
    global _settings
    with _lock:
        if _settings is None:
            _settings = ThoxSettings.from_env()
        return _settings


def events() -> EventLog:
    global _events
    with _lock:
        if _events is None:
            _events = EventLog(settings().state_root)
        return _events


def client() -> MoonrakerClient:
    global _client
    with _lock:
        if _client is None:
            _client = MoonrakerClient(settings())
        return _client


def monitor() -> PrintHealthMonitor:
    global _monitor
    with _lock:
        if _monitor is None:
            _monitor = PrintHealthMonitor(settings(), client(), events=events())
        return _monitor


def reset_state() -> None:
    """Drop cached singletons. Used by tests and after a config change."""
    global _settings, _client, _monitor, _events
    with _lock:
        if _monitor is not None:
            _monitor.stop(timeout_s=2.0)
        _settings = _client = _monitor = _events = None


# -- helpers -----------------------------------------------------------------


def _fail(exc: Exception, status: int = 500) -> tuple[Response, int]:
    """Sanitized error, mirroring server.py's ``_safe_internal_error``.

    Exception text can carry local paths, model identifiers or environment
    details, so only the type is logged against a reference id and the client
    gets the id.
    """
    reference = uuid.uuid4().hex[:12]
    logger.error("[THOX] error reference=%s type=%s", reference, type(exc).__name__)
    return jsonify({"error": "THOX request failed", "reference": reference}), status


def _refused(exc: ThoxRefused) -> tuple[Response, int]:
    return (
        jsonify({"error": exc.detail, "reason": exc.reason, "refused": True}),
        409,
    )


def _body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _float(value: Any, field: str, low: float, high: float, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not low <= parsed <= high:
        raise ValueError(f"{field} must be between {low:g} and {high:g}")
    return parsed


def _int(value: Any, field: str, low: int, high: int, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if not low <= parsed <= high:
        raise ValueError(f"{field} must be between {low} and {high}")
    return parsed


def _placement(body: dict[str, Any], config: ThoxSettings) -> ObjectPlacement:
    return ObjectPlacement(
        center_xy_mm=(
            _float(body.get("center_x_mm"), "center_x_mm", 0, 270, 135.0),
            _float(body.get("center_y_mm"), "center_y_mm", 0, 270, 110.0),
        ),
        footprint_radius_mm=_float(
            body.get("footprint_radius_mm"), "footprint_radius_mm", 1, 135, 40.0
        ),
        height_mm=_float(body.get("object_height_mm"), "object_height_mm", 0.1, 200, 40.0),
    )


def _safe_state_path(candidate: str) -> str:
    """Confine a caller-supplied path to the state root.

    These endpoints accept a path so a UI can point at a previous session's
    artifact. Without this check the same field would read or write anywhere
    the server process can reach.
    """
    root = os.path.realpath(settings().state_root)
    resolved = os.path.realpath(candidate)
    if not (resolved == root or resolved.startswith(root + os.sep)):
        raise ValueError("path is outside the THOX state directory")
    return resolved


# -- status ------------------------------------------------------------------


@thox_bp.route("/health", methods=["GET"])
def thox_health() -> Any:
    """Layer status. Safe to poll; never touches the printer."""
    try:
        config = settings()
    except ConfigError as exc:
        return jsonify({"ok": False, "error": str(exc), "configured": False}), 200

    active = monitor()
    return jsonify(
        {
            "ok": True,
            "configured": bool(config.printer_host),
            "printer_url": config.base_url if config.printer_host else "",
            "autonomy": config.autonomy,
            "monitor_running": active.running,
            "vision": active.ensemble.status(),
            "reference_frames": ScanRunner(config, client(), events()).references.size,
            "settings": config.safe_dict(),
        }
    )


@thox_bp.route("/printer", methods=["GET"])
def thox_printer() -> Any:
    """Printer state plus which actions are currently legal."""
    try:
        connection = client()
        info = connection.printer_info()
        job = connection.job_snapshot()
        cameras = [camera.to_dict() for camera in connection.list_cameras()]
        controller = PrintController(connection, settings(), events=events())
        actions = controller.available_actions()
    except ConfigError as exc:
        return jsonify({"error": str(exc), "configured": False}), 409
    except MoonrakerError as exc:
        return jsonify({"error": f"printer unreachable ({type(exc).__name__})"}), 503
    except Exception as exc:
        return _fail(exc)

    scan_ready, scan_reason = True, "ready to scan"
    try:
        Interlock(settings()).assert_can_scan(connection)
    except ThoxRefused as exc:
        scan_ready, scan_reason = False, exc.detail
    except MoonrakerError:
        scan_ready, scan_reason = False, "printer unreachable"

    return jsonify(
        {
            "klipper_state": info.get("state"),
            "hostname": info.get("hostname"),
            "job": job,
            "cameras": cameras,
            "control": actions,
            "can_scan": scan_ready,
            "scan_reason": scan_reason,
        }
    )


# -- monitoring --------------------------------------------------------------


@thox_bp.route("/monitor/start", methods=["POST"])
def monitor_start() -> Any:
    try:
        started = monitor().start()
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        return _fail(exc)
    return jsonify({"started": started, "state": monitor().snapshot_state()})


@thox_bp.route("/monitor/stop", methods=["POST"])
def monitor_stop() -> Any:
    try:
        stopped = monitor().stop()
    except Exception as exc:
        return _fail(exc)
    return jsonify({"stopped": stopped})


@thox_bp.route("/monitor/state", methods=["GET"])
def monitor_state() -> Any:
    try:
        return jsonify(monitor().snapshot_state())
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        return _fail(exc)


@thox_bp.route("/monitor/frame", methods=["GET"])
def monitor_frame() -> Any:
    """The most recent analyzed frame, for the UI's overlay."""
    try:
        frame = monitor().latest_frame
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 409
    if not frame:
        return jsonify({"error": "no frame captured yet"}), 404
    return Response(frame, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@thox_bp.route("/events", methods=["GET"])
def thox_events() -> Any:
    """Recent events. ``since`` lets a UI poll without re-rendering."""
    try:
        since = _int(request.args.get("since"), "since", 0, 10**9, 0)
        limit = _int(request.args.get("limit"), "limit", 1, 500, 100)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    log = events()
    return jsonify(
        {"events": log.recent(limit=limit, since_seq=since), "last_seq": log.last_seq}
    )


# -- control -----------------------------------------------------------------


@thox_bp.route("/control/<action>", methods=["POST"])
def thox_control(action: str) -> Any:
    """Pause, resume, cancel or reprint.

    ``actor`` defaults to ``human`` because this endpoint exists to serve a UI
    button. The agent path calls the controller directly and passes
    ``actor="agent"``, which is what subjects it to the autonomy policy.
    """
    if action not in ACTIONS:
        return jsonify({"error": f"unknown action {action!r}"}), 400

    body = _body()
    actor = str(body.get("actor") or "human").lower()
    if actor not in {"human", "agent"}:
        return jsonify({"error": "actor must be 'human' or 'agent'"}), 400

    try:
        controller = PrintController(client(), settings(), events=events())
        result = controller.act(
            action,
            actor=actor,
            why=str(body.get("why") or "")[:200],
            filename=str(body.get("filename") or "")[:255],
        )
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        return _fail(exc)

    payload = result.to_dict()
    # A refusal is a legitimate outcome the operator can act on, not a fault.
    return jsonify(payload), (200 if result.ok else 409)


# -- revise and reprint ------------------------------------------------------


@thox_bp.route("/revise/plan", methods=["POST"])
def revise_plan() -> Any:
    """Propose parameter changes for a diagnosed defect. Writes nothing."""
    body = _body()
    kind = parse_kind(body.get("defect"))
    if kind is None:
        return (
            jsonify(
                {
                    "error": "unknown defect",
                    "known": sorted(k.value for k in DefectKind),
                }
            ),
            400,
        )

    current: dict[str, float] = {}
    source = str(body.get("gcode_path") or "")
    if source:
        try:
            current = read_gcode_params(_safe_state_path(source))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except ThoxError as exc:
            return jsonify({"error": str(exc)}), 400

    supplied = body.get("current")
    if isinstance(supplied, dict):
        for key, value in supplied.items():
            try:
                current[str(key)] = float(value)
            except (TypeError, ValueError):
                return jsonify({"error": f"current.{key} must be a number"}), 400

    try:
        attempt = _int(body.get("attempt"), "attempt", 1, 10, 1)
        revision = plan_revision(
            kind,
            current,
            attempt=attempt,
            source_file=os.path.basename(source),
            settings=settings(),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except ThoxError as exc:
        return jsonify({"error": str(exc), "reason": "no_revision"}), 409
    except Exception as exc:
        return _fail(exc)

    events().add(
        EventKind.REVISION,
        f"planned revision {revision.attempt} for {kind.value}",
        severity=0.4,
        defect=kind.value,
        changes=len(revision.changes),
    )
    return jsonify(revision.to_dict())


@thox_bp.route("/revise/apply", methods=["POST"])
def revise_apply() -> Any:
    """Write a revised G-code with in-place overrides injected."""
    body = _body()
    kind = parse_kind(body.get("defect"))
    if kind is None:
        return jsonify({"error": "unknown defect"}), 400

    source = str(body.get("gcode_path") or "")
    if not source:
        return jsonify({"error": "gcode_path is required"}), 400

    try:
        resolved = _safe_state_path(source)
        current = read_gcode_params(resolved)
        attempt = _int(body.get("attempt"), "attempt", 1, 10, 1)
        revision = plan_revision(
            kind,
            current,
            attempt=attempt,
            source_file=os.path.basename(resolved),
            settings=settings(),
        )
        stem, extension = os.path.splitext(resolved)
        output = f"{stem}.thox-rev{attempt}{extension or '.gcode'}"
        summary = apply_revision(resolved, revision, output)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except ThoxError as exc:
        return jsonify({"error": str(exc), "reason": "not_applicable"}), 409
    except Exception as exc:
        return _fail(exc)

    events().add(
        EventKind.REVISION,
        f"wrote revised G-code for {kind.value} (attempt {attempt})",
        severity=0.5,
        defect=kind.value,
        output=os.path.basename(summary["output_path"]),
    )
    return jsonify(summary)


# -- scan to print -----------------------------------------------------------


@thox_bp.route("/scan/plan", methods=["POST"])
def scan_plan() -> Any:
    """Preview a sweep. Moves nothing, so it is safe from a live UI."""
    body = _body()
    try:
        config = settings()
        config.stations = _int(body.get("stations"), "stations", 2, 60, config.stations)
        config.azimuths = _int(body.get("azimuths"), "azimuths", 1, 8, config.azimuths)
        placement = _placement(body, config)
        clearance = Interlock(config).assert_can_scan(
            client(), object_height_mm=placement.height_mm
        )
        calibration = nominal_calibration()
        poses, tier = plan_session(calibration, placement, clearance, config)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except ThoxRefused as exc:
        return _refused(exc)
    except MoonrakerError:
        return jsonify({"error": "printer unreachable"}), 503
    except ThoxError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:
        return _fail(exc)

    return jsonify(
        {
            "tier": tier.value,
            "tier_label": tier.label,
            "dimensionally_reliable": tier.dimensionally_reliable,
            "provisional_calibration": calibration.provisional,
            "summary": describe_plan(poses, calibration, placement),
            "poses": [p.to_dict() for p in poses],
        }
    )


def _run_background(job_id: str, work: Any) -> None:
    def target() -> None:
        try:
            _jobs[job_id] = {"state": "running", "started": time.time()}
            result = work()
            _jobs[job_id] = {
                "state": "done",
                "result": result,
                "finished": time.time(),
            }
        except Exception as exc:
            logger.exception("[THOX] background job %s failed", job_id)
            _jobs[job_id] = {
                "state": "failed",
                "error": type(exc).__name__,
                "finished": time.time(),
            }

    threading.Thread(target=target, name=f"thox-job-{job_id}", daemon=True).start()


@thox_bp.route("/scan/reference", methods=["POST"])
def scan_reference() -> Any:
    """Capture the empty-bed reference ladder. THE BED MUST BE CLEAR."""
    body = _body()
    try:
        config = settings()
        placement = _placement(body, config)
        runner = ScanRunner(config, client(), events())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 409

    job_id = uuid.uuid4().hex[:12]
    _run_background(
        job_id,
        lambda: {
            "captured": runner.capture_reference_ladder(
                placement,
                progress=lambda stage, detail: events().add(
                    EventKind.SAMPLE, f"reference {stage}", **detail
                ),
            )
        },
    )
    return jsonify({"job_id": job_id, "state": "running"}), 202


@thox_bp.route("/scan/run", methods=["POST"])
def scan_run() -> Any:
    """Run a scan. Returns a job id; progress arrives on /thox/events."""
    body = _body()
    try:
        config = settings()
        config.stations = _int(body.get("stations"), "stations", 2, 60, config.stations)
        config.azimuths = _int(body.get("azimuths"), "azimuths", 1, 8, config.azimuths)
        placement = _placement(body, config)
        make_tray_too = bool(body.get("make_tray", True))
        runner = ScanRunner(config, client(), events())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 409

    if monitor().running:
        # Both would drive the bed. Refusing beats interleaved Z moves.
        return (
            jsonify(
                {
                    "error": "stop the print-health monitor before scanning; "
                    "both drive the printer",
                    "reason": "monitor_running",
                }
            ),
            409,
        )

    job_id = uuid.uuid4().hex[:12]
    _run_background(
        job_id,
        lambda: runner.run(
            placement,
            make_tray_too=make_tray_too,
            progress=lambda stage, detail: events().add(
                EventKind.SAMPLE, f"scan {stage}", **detail
            ),
        ).to_dict(),
    )
    return jsonify({"job_id": job_id, "state": "running"}), 202


@thox_bp.route("/jobs/<job_id>", methods=["GET"])
def job_status(job_id: str) -> Any:
    """Poll a background job started by /scan/run or /scan/reference."""
    if not job_id.isalnum() or len(job_id) > 32:
        return jsonify({"error": "invalid job id"}), 400
    job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({"job_id": job_id, **job})


def register(app: Any) -> None:
    """Attach the blueprint. Called from ``server.py``."""
    app.register_blueprint(thox_bp)
    logger.info("[THOX] printer-agent routes registered under /thox")
