"""Minimal Moonraker + Qidi webcam adapter used by Print Health.

The AI never receives a generic raw-G-code tool. Motion is only exposed through
``safe_scan_pose`` with a hard allowlist, while print lifecycle operations map to
Moonraker's dedicated endpoints.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class MoonrakerError(RuntimeError):
    pass


class MoonrakerClient:
    def __init__(self, host: str, port: int = 7125, api_key: str = "") -> None:
        self.host = host.strip()
        self.port = int(port)
        self.api_key = api_key.strip()
        self.base_url = f"http://{self.host}:{self.port}"
        self.ui_base_url = os.getenv("THOX_QIDI_UI_URL", f"http://{self.host}").rstrip("/")
        self.snapshot_override = os.getenv("THOX_QIDI_CAMERA_SNAPSHOT_URL", "").strip()

    def _request(
        self,
        path_or_url: str,
        *,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        timeout: float = 12.0,
        expect_json: bool = True,
    ) -> Any:
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else self.base_url + path_or_url
        payload = None
        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        if data is not None:
            payload = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read(16 * 1024 * 1024)
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise MoonrakerError(f"Moonraker HTTP {exc.code}: {detail[:400]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MoonrakerError(f"Moonraker request failed: {exc}") from exc
        if not expect_json:
            return body, content_type
        try:
            return json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MoonrakerError("Moonraker returned invalid JSON") from exc

    def ping(self) -> bool:
        try:
            info = self._request("/printer/info")
            return info.get("result", {}).get("state") == "ready"
        except MoonrakerError:
            return False

    def telemetry(self) -> dict[str, Any]:
        query = urllib.parse.urlencode({
            "print_stats": "",
            "virtual_sdcard": "",
            "extruder": "temperature,target",
            "heater_bed": "temperature,target",
            "toolhead": "position,homed_axes",
            "display_status": "progress,message",
        })
        result = self._request("/printer/objects/query?" + query)
        status = result.get("result", {}).get("status", {})
        stats = status.get("print_stats", {})
        sd = status.get("virtual_sdcard", {})
        display = status.get("display_status", {})
        progress = sd.get("progress", display.get("progress", 0.0))
        return {
            "state": stats.get("state", "unknown"),
            "filename": stats.get("filename", ""),
            "progress": float(progress or 0.0),
            "print_duration": float(stats.get("print_duration", 0.0) or 0.0),
            "extruder": status.get("extruder", {}),
            "heater_bed": status.get("heater_bed", {}),
            "toolhead": status.get("toolhead", {}),
            "message": display.get("message", ""),
        }

    def list_webcams(self) -> list[dict[str, Any]]:
        result = self._request("/server/webcams/list")
        webcams = result.get("result", {}).get("webcams", [])
        return webcams if isinstance(webcams, list) else []

    def snapshot_url(self) -> str:
        if self.snapshot_override:
            return self.snapshot_override
        for camera in self.list_webcams():
            if not isinstance(camera, dict) or not camera.get("enabled", True):
                continue
            raw = str(camera.get("snapshot_url", "")).strip()
            if not raw:
                continue
            if raw.startswith(("http://", "https://")):
                return raw
            return urllib.parse.urljoin(self.ui_base_url + "/", raw.lstrip("/"))
        # Common reverse-proxy fallback. This is attempted only after Moonraker
        # discovery; operators can override it with THOX_QIDI_CAMERA_SNAPSHOT_URL.
        return self.ui_base_url + "/webcam/?action=snapshot"

    def snapshot(self) -> tuple[bytes, str]:
        body, content_type = self._request(
            self.snapshot_url(), expect_json=False, timeout=20.0
        )
        if not body:
            raise MoonrakerError("camera snapshot was empty")
        mime = content_type.split(";", 1)[0].strip().lower() or "image/jpeg"
        if not mime.startswith("image/"):
            raise MoonrakerError(f"camera returned unexpected content type: {mime}")
        return body, mime

    def _post(self, path: str, data: dict[str, Any] | None = None) -> None:
        self._request(path, method="POST", data=data or {})

    def pause(self) -> None:
        self._post("/printer/print/pause")

    def resume(self) -> None:
        self._post("/printer/print/resume")

    def cancel(self) -> None:
        self._post("/printer/print/cancel")

    def restart(self) -> None:
        self._post("/printer/restart")

    def firmware_restart(self) -> None:
        self._post("/printer/firmware_restart")

    def emergency_stop(self) -> None:
        self._post("/printer/emergency_stop")

    def start_print(self, filename: str) -> None:
        safe_name = str(filename).replace("\\", "/").split("/")[-1]
        if not safe_name:
            raise MoonrakerError("invalid remote G-code filename")
        query = urllib.parse.urlencode({"filename": safe_name})
        self._request("/printer/print/start?" + query, method="POST", data={})

    def upload_gcode(self, local_path: str, remote_name: str | None = None) -> str:
        """Upload G-code using Moonraker's multipart endpoint.

        Uses stdlib multipart construction to avoid a runtime dependency on
        requests/httpx in the AI sidecar.
        """
        from pathlib import Path
        import mimetypes
        import uuid

        path = Path(local_path)
        if not path.is_file():
            raise MoonrakerError(f"G-code not found: {local_path}")
        name = (remote_name or path.name).replace("\\", "/").split("/")[-1]
        boundary = "----thox" + uuid.uuid4().hex
        chunks: list[bytes] = []

        def field(key: str, value: str) -> None:
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                value.encode(), b"\r\n",
            ])

        field("root", "gcodes")
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode(),
            f"Content-Type: {mimetypes.guess_type(name)[0] or 'application/octet-stream'}\r\n\r\n".encode(),
            path.read_bytes(), b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        body = b"".join(chunks)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        req = urllib.request.Request(
            self.base_url + "/server/files/upload",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90.0) as response:
                response.read(4096)
        except urllib.error.HTTPError as exc:
            raise MoonrakerError(f"G-code upload failed with HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MoonrakerError(f"G-code upload failed: {exc}") from exc
        return name

    def safe_scan_pose(self, x: float, y: float, z: float | None = None) -> None:
        """Move to a bounded, interlocked calibration pose for object scanning.

        Safety invariants:
        * disabled unless ``THOX_ALLOW_SCAN_MOTION=true``;
        * never moves during printing or while a job is paused;
        * XYZ must already be homed;
        * Z raises to a conservative clearance height before any XY movement;
        * lowering Z below clearance requires a second explicit opt-in;
        * no caller can submit arbitrary G-code.
        """
        if os.getenv("THOX_ALLOW_SCAN_MOTION", "false").lower() != "true":
            raise MoonrakerError("scan motion is disabled")

        telemetry = self.telemetry()
        state = str(telemetry.get("state", "unknown")).lower()
        if state in {"printing", "paused"}:
            raise MoonrakerError(f"scan motion is blocked while printer state is {state}")

        toolhead = telemetry.get("toolhead", {})
        homed_axes = str(toolhead.get("homed_axes", "")).lower()
        if not all(axis in homed_axes for axis in "xyz"):
            raise MoonrakerError("scan motion requires XYZ to be homed first")

        x = max(10.0, min(260.0, float(x)))
        y = max(10.0, min(260.0, float(y)))
        clearance_z = max(120.0, min(220.0, float(os.getenv("THOX_SCAN_CLEARANCE_Z", "220"))))

        commands = [
            "G90",
            f"G1 Z{clearance_z:.2f} F600",
            f"G1 X{x:.2f} Y{y:.2f} F3000",
        ]

        if z is not None:
            requested_z = max(5.0, min(220.0, float(z)))
            if requested_z < clearance_z:
                if os.getenv("THOX_ALLOW_SCAN_Z_LOWERING", "false").lower() != "true":
                    raise MoonrakerError(
                        "scan Z lowering is disabled; keep the toolhead at clearance height "
                        "or explicitly set THOX_ALLOW_SCAN_Z_LOWERING=true after verifying object height"
                    )
                minimum_z = max(5.0, min(220.0, float(os.getenv("THOX_SCAN_MIN_Z", "50"))))
                if requested_z < minimum_z:
                    raise MoonrakerError(
                        f"requested scan Z {requested_z:.1f} is below configured safe minimum {minimum_z:.1f}"
                    )
            commands.append(f"G1 Z{requested_z:.2f} F600")

        script = "\n".join(commands)
        query = urllib.parse.urlencode({"script": script})
        self._request("/printer/gcode/script?" + query, method="POST", data={})
