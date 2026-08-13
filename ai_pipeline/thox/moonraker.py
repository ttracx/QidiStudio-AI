"""Synchronous Moonraker client.

Synchronous on purpose. ``ai_pipeline/server.py`` is a threaded Flask app, and
introducing an async stack alongside it would mean either an event loop per
request or a second concurrency model in one process. ``requests`` plus a
worker thread matches what is already here.

The client is **transport only**. It applies no policy: it will happily cancel a
print if asked, because deciding whether cancelling is allowed belongs to
:mod:`thox.interlock` and :mod:`thox.control`. Keeping the two apart means the
gate cannot be bypassed by accident, and the transport stays testable without
tripping policy.

Every method either returns parsed data or raises from :mod:`thox.errors`.
Callers never see a raw ``requests`` exception, and no error message carries a
credential - the API key travels in a header that is never echoed back.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import requests

from .config import ThoxSettings
from .errors import (
    APIError,
    CameraUnavailable,
    ConnectionFailed,
    NotFound,
    RequestTimeout,
    ValidationError,
)

logger = logging.getLogger(__name__)

#: Klipper objects fetched for a telemetry snapshot.
TELEMETRY_OBJECTS: dict[str, list[str] | None] = {
    "print_stats": None,
    "virtual_sdcard": None,
    "toolhead": None,
    "extruder": None,
    "heater_bed": None,
    "gcode_move": None,
    "display_status": None,
    "idle_timeout": None,
}

LEGACY_SNAPSHOT_PATH = "/webcam/?action=snapshot"
LEGACY_STREAM_PATH = "/webcam/?action=stream"


class Camera:
    """A camera Moonraker knows about."""

    __slots__ = ("name", "snapshot_url", "stream_url", "service", "discovered")

    def __init__(
        self,
        name: str,
        snapshot_url: str,
        stream_url: str,
        service: str = "unknown",
        discovered: bool = True,
    ) -> None:
        self.name = name
        self.snapshot_url = snapshot_url
        self.stream_url = stream_url
        self.service = service
        self.discovered = discovered

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "snapshot_url": self.snapshot_url,
            "stream_url": self.stream_url,
            "service": self.service,
            "discovered": self.discovered,
        }

    def __repr__(self) -> str:
        origin = "discovered" if self.discovered else "legacy fallback"
        return f"Camera({self.name!r}, {self.service}, {origin})"


class MoonrakerClient:
    """Transport for one Moonraker instance. No policy applied here."""

    def __init__(self, settings: ThoxSettings | None = None) -> None:
        self.settings = settings or ThoxSettings.from_env()
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        if self.settings.moonraker_api_key:
            self._session.headers["X-Api-Key"] = self.settings.moonraker_api_key
        #: Origin that actually serves camera frames, learned once by probing.
        #: See :meth:`_candidate_origins` for why it cannot simply be assumed.
        self._camera_origin: str | None = None

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> MoonrakerClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        expect_json: bool = True,
        timeout_s: float | None = None,
        **kwargs: Any,
    ) -> Any:
        url = f"{self.settings.base_url}{path}"
        timeout = timeout_s or self.settings.request_timeout_s
        try:
            response = self._session.request(method, url, timeout=timeout, **kwargs)
        except requests.Timeout as exc:
            raise RequestTimeout(
                f"{method} {path} timed out after {timeout:.0f}s against "
                f"{self.settings.base_url}"
            ) from exc
        except requests.RequestException as exc:
            # Deliberately does not interpolate `exc` verbatim beyond its class:
            # urllib3 messages can embed the full URL including any query string.
            raise ConnectionFailed(
                f"cannot reach Moonraker at {self.settings.base_url} "
                f"({type(exc).__name__})"
            ) from exc

        if response.status_code == 404:
            raise NotFound(f"{method} {path} -> 404 Not Found", status=404)
        if response.status_code >= 400:
            raise APIError(
                f"{method} {path} -> HTTP {response.status_code}: "
                f"{_error_detail(response)}",
                status=response.status_code,
            )

        if not expect_json:
            return response.content
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise APIError(f"{method} {path} returned a non-JSON body") from exc

        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"] or {}
            raise APIError(
                f"Moonraker error {err.get('code', '?')}: "
                f"{err.get('message', 'unspecified')}"
            )
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    # -- health / telemetry -------------------------------------------------

    def printer_info(self) -> dict[str, Any]:
        result = self._request("GET", "/printer/info")
        return result if isinstance(result, dict) else {}

    def server_info(self) -> dict[str, Any]:
        result = self._request("GET", "/server/info")
        return result if isinstance(result, dict) else {}

    def ping(self) -> bool:
        """True only if Moonraker answers *and* Klippy reports ready."""
        try:
            return self.printer_info().get("state") == "ready"
        except (ConnectionFailed, RequestTimeout, APIError):
            return False

    def query(self, objects: dict[str, list[str] | None] | None = None) -> dict[str, Any]:
        objects = objects or TELEMETRY_OBJECTS
        query = "&".join(
            key if fields is None else f"{key}={','.join(fields)}"
            for key, fields in objects.items()
        )
        result = self._request("GET", f"/printer/objects/query?{query}")
        return (result or {}).get("status", {})

    def print_state(self) -> str:
        """Normalized print state, or ``disconnected`` if unreachable."""
        try:
            status = self.query({"print_stats": ["state"]})
        except (ConnectionFailed, RequestTimeout, APIError):
            return "disconnected"
        return str((status.get("print_stats") or {}).get("state") or "standby").lower()

    def job_snapshot(self) -> dict[str, Any]:
        """Compact view of the running job, safe to log and to return over HTTP."""
        status = self.query()
        print_stats = status.get("print_stats") or {}
        virtual_sd = status.get("virtual_sdcard") or {}
        toolhead = status.get("toolhead") or {}
        info = print_stats.get("info") or {}
        return {
            "state": str(print_stats.get("state") or "standby").lower(),
            "filename": print_stats.get("filename") or "",
            "progress": float(virtual_sd.get("progress") or 0.0),
            "current_layer": info.get("current_layer"),
            "total_layer": info.get("total_layer"),
            "print_duration_s": float(print_stats.get("print_duration") or 0.0),
            "filament_used_mm": float(print_stats.get("filament_used") or 0.0),
            "hotend_c": float((status.get("extruder") or {}).get("temperature") or 0.0),
            "hotend_target_c": float((status.get("extruder") or {}).get("target") or 0.0),
            "bed_c": float((status.get("heater_bed") or {}).get("temperature") or 0.0),
            "bed_target_c": float((status.get("heater_bed") or {}).get("target") or 0.0),
            "homed_axes": str(toolhead.get("homed_axes") or ""),
            "position": [float(v) for v in (toolhead.get("position") or [])[:3]],
        }

    # -- print lifecycle ----------------------------------------------------
    #
    # Transport-level. Policy lives in thox.control; nothing here should be
    # called directly from a route or an agent path.

    def pause(self) -> None:
        self._request("POST", "/printer/print/pause")

    def resume(self) -> None:
        self._request("POST", "/printer/print/resume")

    def cancel(self) -> None:
        self._request("POST", "/printer/print/cancel")

    def start(self, filename: str) -> None:
        if not filename or not isinstance(filename, str):
            raise ValidationError("filename must be a non-empty string")
        self._request(
            "POST", f"/printer/print/start?filename={quote(filename, safe='/')}"
        )

    def emergency_stop(self) -> None:
        """Firmware halt (M112). Requires a Klipper restart afterwards.

        Exposed for completeness and never reachable from the agent path - the
        control layer has no code path that calls it, so no detector, prompt or
        hallucinated string can trigger a firmware halt.
        """
        self._request("POST", "/printer/emergency_stop")

    def run_gcode(self, script: str) -> None:
        """Send a raw script. **Un-guarded** - call through control, not here."""
        if not script or not isinstance(script, str):
            raise ValidationError("script must be a non-empty string")
        self._request("POST", f"/printer/gcode/script?script={quote(script)}")

    # -- files --------------------------------------------------------------

    def list_files(self, root: str = "gcodes") -> list[dict[str, Any]]:
        result = self._request("GET", f"/server/files/list?root={quote(root)}")
        return result if isinstance(result, list) else []

    def file_metadata(self, filename: str) -> dict[str, Any]:
        result = self._request(
            "GET", f"/server/files/metadata?filename={quote(filename, safe='/')}"
        )
        return result if isinstance(result, dict) else {}

    def upload(self, local_path: str, *, root: str = "gcodes", start: bool = False) -> str:
        """Upload a file, returning its remote path.

        ``start`` is passed straight to Moonraker, so callers must apply policy
        before setting it - this method will begin a print if told to.
        """
        import os

        if not os.path.isfile(local_path):
            raise ValidationError(f"file not found: {os.path.basename(local_path)}")
        if os.path.getsize(local_path) == 0:
            raise ValidationError("refusing to upload an empty file")

        try:
            with open(local_path, "rb") as handle:
                response = self._session.post(
                    f"{self.settings.base_url}/server/files/upload",
                    data={"root": root, "print": "true" if start else "false"},
                    files={
                        "file": (
                            os.path.basename(local_path),
                            handle,
                            "application/octet-stream",
                        )
                    },
                    timeout=max(180.0, self.settings.request_timeout_s),
                )
        except requests.Timeout as exc:
            raise RequestTimeout("upload timed out") from exc
        except requests.RequestException as exc:
            raise ConnectionFailed(f"upload failed ({type(exc).__name__})") from exc

        if response.status_code >= 400:
            raise APIError(
                f"upload -> HTTP {response.status_code}: {_error_detail(response)}",
                status=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError:
            return os.path.basename(local_path)
        item = (payload.get("result") or payload).get("item", {})
        return str(item.get("path", os.path.basename(local_path)))

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        result = self._request("GET", f"/server/history/list?limit={int(limit)}")
        jobs = (result or {}).get("jobs", [])
        return jobs if isinstance(jobs, list) else []

    # -- cameras ------------------------------------------------------------

    def list_cameras(self) -> list[Camera]:
        """Discover cameras, falling back to the legacy path.

        Moonraker does not serve video; it reports where video lives. The URLs
        it returns are often relative and sometimes absolute on a different
        port (crowsnest/ustreamer), so both forms are resolved against the
        Moonraker origin rather than assumed.
        """
        try:
            result = self._request("GET", "/server/webcams/list")
            raw = (result or {}).get("webcams", [])
        except (NotFound, APIError):
            raw = []

        cameras: list[Camera] = []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict) or not entry.get("enabled", True):
                continue
            name = str(entry.get("name") or "camera")
            snapshot = str(entry.get("snapshot_url") or LEGACY_SNAPSHOT_PATH)
            stream = str(entry.get("stream_url") or LEGACY_STREAM_PATH)
            cameras.append(
                Camera(
                    name=name,
                    snapshot_url=self._resolve(snapshot),
                    stream_url=self._resolve(stream),
                    service=str(entry.get("service") or "unknown"),
                    discovered=True,
                )
            )

        if not cameras:
            # Labelled so an operator can see the fallback happened rather than
            # wondering why the camera name is generic.
            cameras.append(
                Camera(
                    name="webcam (legacy)",
                    snapshot_url=self._resolve(LEGACY_SNAPSHOT_PATH),
                    stream_url=self._resolve(LEGACY_STREAM_PATH),
                    service="legacy",
                    discovered=False,
                )
            )
        return cameras

    def _candidate_origins(self) -> list[str]:
        """Origins a relative camera path might actually live on, best first.

        Moonraker reports camera URLs as relative paths like
        ``/webcam/?action=snapshot`` and says nothing about which port serves
        them. Resolving those against Moonraker's own origin is the obvious
        guess and it is **wrong on this hardware**: measured on the Q2,
        ``:7125/webcam/?action=snapshot`` returns 404 while plain port 80
        returns a 26 KB JPEG, because the stock image proxies crowsnest through
        nginx on 80 rather than through Moonraker.

        So the port is probed rather than assumed. Port 80 leads because it is
        what the stock QIDI image uses; 8080 is the bare ustreamer default for
        setups without the proxy.
        """
        host = self.settings.require_host()
        return [
            f"http://{host}",
            self.settings.base_url,
            f"http://{host}:8080",
        ]

    def _resolve(self, url: str) -> str:
        """Resolve one camera URL, probing candidate origins if it is relative."""
        if url.startswith(("http://", "https://")):
            return url
        if not url.startswith("/"):
            url = "/" + url
        if self._camera_origin is not None:
            return f"{self._camera_origin}{url}"

        for origin in self._candidate_origins():
            candidate = f"{origin}{url}"
            if self._origin_serves_image(candidate):
                self._camera_origin = origin
                logger.info("[THOX] camera resolved to %s", origin)
                return candidate

        # Nothing answered with an image. Return the most likely URL anyway so
        # the caller gets a precise CameraUnavailable naming a real address
        # rather than a vague "no camera" from here.
        fallback = self._candidate_origins()[0]
        logger.warning(
            "[THOX] no candidate origin served an image for %s; using %s",
            url,
            fallback,
        )
        return f"{fallback}{url}"

    def _origin_serves_image(self, url: str) -> bool:
        """Whether this URL returns JPEG bytes. Cheap, and never raises."""
        try:
            response = self._session.get(
                url, timeout=min(5.0, self.settings.request_timeout_s), stream=True
            )
            try:
                if response.status_code != 200:
                    return False
                # Read only the magic bytes: a stream URL would otherwise never
                # finish, and a snapshot URL need not be fully downloaded here.
                for chunk in response.iter_content(chunk_size=2):
                    return chunk[:2] == b"\xff\xd8"
                return False
            finally:
                response.close()
        except requests.RequestException:
            return False

    def snapshot(self, camera: Camera | None = None, *, timeout_s: float = 10.0) -> bytes:
        """Grab one JPEG still.

        Raises:
            CameraUnavailable: The camera did not return a JPEG. Checked by
                magic bytes rather than content-type, because MJPEG streamers
                under load return an HTML error page with a 200 status.
        """
        if camera is None:
            cameras = self.list_cameras()
            if not cameras:
                raise CameraUnavailable("no camera is configured on this printer")
            camera = cameras[0]

        try:
            response = self._session.get(
                camera.snapshot_url,
                timeout=timeout_s,
                headers={"Accept": "image/jpeg, */*"},
            )
        except requests.Timeout as exc:
            raise CameraUnavailable(
                f"camera {camera.name!r} did not answer within {timeout_s:.0f}s"
            ) from exc
        except requests.RequestException as exc:
            raise CameraUnavailable(
                f"camera {camera.name!r} is unreachable ({type(exc).__name__})"
            ) from exc

        if response.status_code >= 400:
            raise CameraUnavailable(
                f"camera {camera.name!r} returned HTTP {response.status_code}"
            )
        data = response.content
        if not data[:2] == b"\xff\xd8":
            raise CameraUnavailable(
                f"camera {camera.name!r} returned {len(data)} bytes that are "
                "not a JPEG (the streamer may be overloaded)"
            )
        return data


def _error_detail(response: requests.Response) -> str:
    """Best-effort Moonraker error message, truncated and never a traceback."""
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "(empty body)")[:200]
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message", payload))[:200]
        if err:
            return str(err)[:200]
        if "message" in payload:
            return str(payload["message"])[:200]
    return str(payload)[:200]
