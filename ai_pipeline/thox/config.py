"""Environment-only configuration for the THOX printer-agent layer.

Every value is read from the environment. Nothing is hardcoded, nothing is
committed, and :func:`redact` is applied to any settings dump that reaches a log
or an HTTP response. This repository is **public**, which raises the stakes on
that discipline: a default that embeds a real host or key would be published the
moment it lands.

There is deliberately **no default printer host**. An unset host raises a clear
error rather than silently addressing whatever machine happens to answer at some
baked-in address. That convention comes from the sibling `thox-q2-control` lane
and is worth keeping identical across both.

Two settings decide how the agent behaves when it thinks a print is failing, and
they are the ones an operator should actually think about:

``autonomy``
    ``observe`` never acts. ``suggest`` raises an alert and waits for a human.
    ``auto_pause`` may pause on its own above the confidence threshold, but can
    never cancel. Cancelling is always a human decision - a pause is reversible
    and costs minutes, a cancel throws away hours.

``confirm_frames``
    How many consecutive flagged samples are required before anything happens.
    One is far too few: a passing toolhead, a shadow, or a single bad exposure
    reads as a defect, and a monitor that pauses on those gets switched off.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any

#: Q2 build volume, canonical.
BED_SIZE_X_MM = 270.0
BED_SIZE_Y_MM = 270.0
BUILD_HEIGHT_MM = 256.0

#: Motion envelope as the machine reports it. Used only for clamping.
Z_MACHINE_MIN_MM = -2.0
Z_MACHINE_MAX_MM = 265.0

_SECRET_HINTS = ("key", "token", "secret", "password", "authorization")

AUTONOMY_LEVELS = ("observe", "suggest", "auto_pause")


def is_secret_field(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def redact(data: dict[str, Any]) -> dict[str, Any]:
    """Mask credential values, keeping keys and set/unset distinguishable.

    "No key configured" and "key present but hidden" are different operational
    states, and an operator debugging a provider needs to tell them apart, so
    ``None`` survives while a real value becomes a marker.
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[key] = redact(value)
        elif is_secret_field(key):
            out[key] = None if value in (None, "") else "***redacted***"
        else:
            out[key] = value
    return out


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class ConfigError(ValueError):
    """Configuration is missing or invalid."""


@dataclass
class ThoxSettings:
    """Runtime configuration. Construct with :meth:`from_env`."""

    # -- Printer ------------------------------------------------------------
    printer_host: str = ""
    printer_port: int = 7125
    moonraker_api_key: str | None = None
    request_timeout_s: float = 10.0

    # -- Safety -------------------------------------------------------------
    max_scan_hotend_c: float = 60.0
    z_min_safe_mm: float = 5.0
    z_max_safe_mm: float = 250.0
    z_clearance_mm: float = 15.0
    scan_feedrate_mm_min: float = 1200.0
    motion_settle_timeout_s: float = 45.0
    settle_dwell_s: float = 0.6

    # -- Monitoring ---------------------------------------------------------
    #: Normal sampling interval. Deliberately slow: a print fails over minutes,
    #: not milliseconds, and every sample costs a model call.
    sample_interval_s: float = 45.0
    #: Interval used once something looks wrong. Confirming a suspicion fast is
    #: worth the extra calls; watching a healthy print closely is not.
    alert_interval_s: float = 12.0
    #: First-layer interval. Adhesion failures are both most likely and most
    #: cheaply recovered here, so the first layers earn closer attention.
    first_layer_interval_s: float = 20.0
    first_layer_count: int = 3
    confirm_frames: int = 3
    #: Minimum gap between two autonomous actions, so a flapping detector
    #: cannot pause, resume and pause again in a loop.
    action_cooldown_s: float = 300.0

    # -- Autonomy -----------------------------------------------------------
    autonomy: str = "suggest"
    auto_pause_confidence: float = 0.85
    #: Severity at or above which an alert is raised at all.
    alert_severity: float = 0.5

    # -- Vision -------------------------------------------------------------
    providers: str = "cv_motion"
    provider_timeout_s: float = 240.0
    min_agreement: float = 0.4

    ollama_base_url: str = ""
    ollama_model: str = "huihui_ai/qwen3-vl-abliterated:latest"
    ollama_num_predict: int = 6000

    ollama_cloud_base_url: str = "https://ollama.com"
    ollama_cloud_api_key: str | None = None
    ollama_cloud_model: str = "kimi-k2.5:cloud"

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    # -- Reconstruction / plating -------------------------------------------
    voxel_mm: float = 0.8
    max_voxels: int = 12_000_000
    stations: int = 12
    azimuths: int = 1
    plate_margin_mm: float = 10.0

    # -- Storage ------------------------------------------------------------
    state_root: str = field(
        default_factory=lambda: os.path.join(
            os.path.expanduser("~"), ".thox", "q2"
        )
    )
    max_reprint_attempts: int = 2

    # -- Construction -------------------------------------------------------

    @classmethod
    def from_env(cls) -> ThoxSettings:
        """Build settings from ``THOX_*`` environment variables."""
        settings = cls(
            printer_host=_strip_host(_env("THOX_PRINTER_HOST")),
            printer_port=_env_int("THOX_PRINTER_PORT", 7125),
            moonraker_api_key=_clean_secret(_env("THOX_MOONRAKER_API_KEY")),
            request_timeout_s=_env_float("THOX_REQUEST_TIMEOUT_S", 10.0),
            max_scan_hotend_c=_env_float("THOX_MAX_SCAN_HOTEND_C", 60.0),
            z_min_safe_mm=_env_float("THOX_Z_MIN_SAFE_MM", 5.0),
            z_max_safe_mm=_env_float("THOX_Z_MAX_SAFE_MM", 250.0),
            z_clearance_mm=_env_float("THOX_Z_CLEARANCE_MM", 15.0),
            sample_interval_s=_env_float("THOX_SAMPLE_INTERVAL_S", 45.0),
            alert_interval_s=_env_float("THOX_ALERT_INTERVAL_S", 12.0),
            first_layer_interval_s=_env_float("THOX_FIRST_LAYER_INTERVAL_S", 20.0),
            first_layer_count=_env_int("THOX_FIRST_LAYER_COUNT", 3),
            confirm_frames=_env_int("THOX_CONFIRM_FRAMES", 3),
            action_cooldown_s=_env_float("THOX_ACTION_COOLDOWN_S", 300.0),
            autonomy=_env("THOX_AUTONOMY", "suggest").lower() or "suggest",
            auto_pause_confidence=_env_float("THOX_AUTO_PAUSE_CONFIDENCE", 0.85),
            alert_severity=_env_float("THOX_ALERT_SEVERITY", 0.5),
            providers=_env("THOX_PROVIDERS", "cv_motion"),
            provider_timeout_s=_env_float("THOX_PROVIDER_TIMEOUT_S", 240.0),
            min_agreement=_env_float("THOX_MIN_AGREEMENT", 0.4),
            ollama_base_url=_env("THOX_OLLAMA_BASE_URL").rstrip("/"),
            ollama_model=_env(
                "THOX_OLLAMA_MODEL", "huihui_ai/qwen3-vl-abliterated:latest"
            ),
            ollama_num_predict=_env_int("THOX_OLLAMA_NUM_PREDICT", 6000),
            ollama_cloud_base_url=_env(
                "THOX_OLLAMA_CLOUD_BASE_URL", "https://ollama.com"
            ).rstrip("/"),
            ollama_cloud_api_key=_clean_secret(_env("THOX_OLLAMA_CLOUD_API_KEY")),
            ollama_cloud_model=_env("THOX_OLLAMA_CLOUD_MODEL", "kimi-k2.5:cloud"),
            openai_base_url=_env(
                "THOX_OPENAI_BASE_URL", "https://api.openai.com/v1"
            ).rstrip("/"),
            openai_api_key=_clean_secret(_env("THOX_OPENAI_API_KEY")),
            openai_model=_env("THOX_OPENAI_MODEL", "gpt-4o-mini"),
            voxel_mm=_env_float("THOX_VOXEL_MM", 0.8),
            max_voxels=_env_int("THOX_MAX_VOXELS", 12_000_000),
            stations=_env_int("THOX_STATIONS", 12),
            azimuths=_env_int("THOX_AZIMUTHS", 1),
            plate_margin_mm=_env_float("THOX_PLATE_MARGIN_MM", 10.0),
            max_reprint_attempts=_env_int("THOX_MAX_REPRINT_ATTEMPTS", 2),
        )
        root = _env("THOX_STATE_ROOT")
        if root:
            settings.state_root = root
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.autonomy not in AUTONOMY_LEVELS:
            raise ConfigError(
                f"THOX_AUTONOMY must be one of {', '.join(AUTONOMY_LEVELS)}, "
                f"got {self.autonomy!r}"
            )
        if not 0.0 <= self.auto_pause_confidence <= 1.0:
            raise ConfigError("THOX_AUTO_PAUSE_CONFIDENCE must be between 0 and 1")
        if not 0.0 <= self.alert_severity <= 1.0:
            raise ConfigError("THOX_ALERT_SEVERITY must be between 0 and 1")
        if self.confirm_frames < 1:
            raise ConfigError("THOX_CONFIRM_FRAMES must be at least 1")
        if self.sample_interval_s <= 0 or self.alert_interval_s <= 0:
            raise ConfigError("sampling intervals must be positive")
        if self.z_max_safe_mm <= self.z_min_safe_mm:
            raise ConfigError("THOX_Z_MAX_SAFE_MM must exceed THOX_Z_MIN_SAFE_MM")
        for provider in self.provider_ids:
            if provider not in {"cv_motion", "ollama_local", "ollama_cloud", "openai"}:
                raise ConfigError(f"unknown vision provider {provider!r}")

    # -- Derived ------------------------------------------------------------

    def require_host(self) -> str:
        if not self.printer_host:
            raise ConfigError(
                "No printer configured. Set THOX_PRINTER_HOST to the machine's "
                "LAN address (for example the Q2 running Moonraker). There is "
                "deliberately no default, so a misconfigured process cannot "
                "address the wrong printer."
            )
        return self.printer_host

    @property
    def base_url(self) -> str:
        return f"http://{self.require_host()}:{self.printer_port}"

    @property
    def provider_ids(self) -> list[str]:
        return [p.strip() for p in self.providers.split(",") if p.strip()]

    @property
    def may_act(self) -> bool:
        return self.autonomy != "observe"

    @property
    def may_auto_pause(self) -> bool:
        return self.autonomy == "auto_pause"

    def z_window(self, object_height_mm: float = 0.0) -> tuple[float, float]:
        low = max(self.z_min_safe_mm, Z_MACHINE_MIN_MM)
        high = min(self.z_max_safe_mm, Z_MACHINE_MAX_MM) - (
            object_height_mm + self.z_clearance_mm
        )
        return low, high

    def safe_dict(self) -> dict[str, Any]:
        """The only sanctioned serialization. Credentials are masked."""
        return redact({f.name: getattr(self, f.name) for f in fields(self)})

    def __repr__(self) -> str:
        return f"ThoxSettings({self.safe_dict()})"

    __str__ = __repr__


def _strip_host(value: str) -> str:
    """Tolerate a pasted URL; keep only the authority."""
    value = value.strip()
    for prefix in ("http://", "https://", "ws://", "wss://"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value.rstrip("/").split("/")[0].split(":")[0]


def _clean_secret(value: str) -> str | None:
    """Treat placeholder credentials as unset.

    Local OpenAI-compatible shims are commonly configured with
    ``OPENAI_API_KEY=ollama``. Accepting that as real makes the provider join
    the ensemble and fail on every single call; reporting it as unconfigured is
    both truthful and far easier to debug.
    """
    stripped = value.strip()
    if not stripped or stripped.lower() in {"ollama", "none", "null", "changeme", "x"}:
        return None
    return stripped
