"""Flask Blueprint exposing Print Health to QidiStudio and THOX Forger."""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request, send_file

from .core import PrintHealthService
from .models import AutonomyMode


def _loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def create_print_health_blueprint(service: PrintHealthService) -> Blueprint:
    bp = Blueprint("print_health", __name__)

    @bp.before_request
    def authenticate_remote() -> Response | None:
        """Require a bearer token for any non-loopback Print Health request.

        The existing sidecar is localhost-only by default. If it is deliberately
        exposed for THOX Forger/Tailscale, set THOX_PRINT_HEALTH_TOKEN and send it
        as ``Authorization: Bearer <token>``. Destructive routes still require their
        own explicit ``confirm`` flag after authentication.
        """
        token = os.getenv("THOX_PRINT_HEALTH_TOKEN", "").strip()
        peer = request.remote_addr or ""
        must_auth = bool(token) or not _loopback(peer)
        if not must_auth:
            return None
        if not token:
            return jsonify({"error": "remote Print Health access requires THOX_PRINT_HEALTH_TOKEN"}), 403
        supplied = request.headers.get("Authorization", "")
        expected = "Bearer " + token
        if not hmac.compare_digest(supplied, expected):
            return jsonify({"error": "unauthorized"}), 401
        return None

    def body() -> dict[str, Any]:
        value = request.get_json(silent=True)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    @bp.get("/api/print-health/state")
    def state():
        return jsonify(service.state())

    @bp.get("/api/print-health/camera")
    def camera():
        try:
            frame, mime = service.camera_frame()
            return Response(frame, mimetype=mime, headers={"Cache-Control": "no-store"})
        except Exception as exc:
            return jsonify({"error": str(exc)[:500]}), 502

    @bp.get("/api/print-health/events")
    def events():
        try:
            limit = int(request.args.get("limit", "100"))
        except ValueError:
            limit = 100
        return jsonify({"events": service.events(limit)})

    @bp.get("/api/print-health/revisions")
    def revisions():
        return jsonify({"revisions": service.revisions()})

    @bp.post("/api/print-health/start")
    def start():
        service.start()
        return jsonify({"ok": True, "state": service.state()})

    @bp.post("/api/print-health/stop")
    def stop():
        service.stop()
        return jsonify({"ok": True, "state": service.state()})

    @bp.post("/api/print-health/analyze")
    def analyze():
        try:
            assessment = service.analyze_once()
            return jsonify({"ok": True, "assessment": assessment.to_dict()})
        except Exception as exc:
            return jsonify({"error": str(exc)[:1000]}), 502

    @bp.post("/api/print-health/job")
    def register_job():
        try:
            job = service.register_job(body())
            return jsonify({"ok": True, "job": job.to_dict()})
        except (ValueError, OSError) as exc:
            return jsonify({"error": str(exc)}), 400

    @bp.post("/api/print-health/mode")
    def mode():
        try:
            value = str(body().get("mode", ""))
            selected = service.set_mode(value)
            return jsonify({"ok": True, "mode": selected.value})
        except (ValueError, KeyError) as exc:
            return jsonify({
                "error": str(exc),
                "allowed": [item.value for item in AutonomyMode],
            }), 400

    @bp.post("/api/print-health/control")
    def control():
        try:
            data = body()
            result = service.control(
                str(data.get("action", "")),
                confirm=bool(data.get("confirm", False)),
                actor="human",
            )
            return jsonify(result)
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": str(exc)[:1000]}), 502

    @bp.post("/api/print-health/remediate")
    def remediate():
        try:
            data = body()
            result = service.remediate(
                confirm=bool(data.get("confirm", False)),
                auto_start=bool(data.get("auto_start", True)),
                actor="human",
            )
            return jsonify(result)
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": str(exc)[:1000]}), 502

    @bp.post("/api/scan/capture")
    def scan_capture():
        try:
            data = body()
            pose = data.get("pose")
            if pose is not None and not isinstance(pose, dict):
                raise ValueError("pose must be an object")
            return jsonify({"ok": True, "capture": service.capture_scan_frame(pose)})
        except (ValueError, PermissionError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": str(exc)[:1000]}), 502

    @bp.get("/api/print-health/stream")
    def stream():
        """Small SSE stream for event/notification UIs.

        It sends state only when the newest event id changes, plus a heartbeat at
        least every 15 seconds. This keeps the implementation WSGI-compatible.
        """
        def generate():
            last_id = ""
            last_heartbeat = 0.0
            while True:
                events_now = service.events(1)
                current_id = events_now[0].get("id", "") if events_now else ""
                now = time.time()
                if current_id != last_id:
                    last_id = current_id
                    payload = {"event": events_now[0] if events_now else None, "state": service.state()}
                    yield "event: print-health\ndata: " + json.dumps(payload) + "\n\n"
                    last_heartbeat = now
                elif now - last_heartbeat >= 15.0:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
                time.sleep(1.0)
        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @bp.get("/print-health")
    def print_health_ui():
        ui = Path(__file__).resolve().parent / "static" / "print_health.html"
        return send_file(ui, mimetype="text/html", max_age=0)

    return bp
