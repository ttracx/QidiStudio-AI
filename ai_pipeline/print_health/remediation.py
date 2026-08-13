"""Deterministic failure remediation and safe QidiStudio/QIDISlicer reslicing."""
from __future__ import annotations

import configparser
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ChangeSet, FusedAssessment, Revision


class RemediationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SliceContext:
    source_model: str
    base_profile: str
    slicer_bin: str
    workdir: str
    previous_gcode: str = ""


def diagnose_changes(assessment: FusedAssessment) -> ChangeSet:
    """Map fused visual defects to small, bounded manufacturing changes.

    Models decide *what is visible*; this deterministic policy decides which
    machine/slicer knobs may change. That prevents an LLM from writing arbitrary
    G-code or unbounded thermal/motion parameters.
    """
    defects = {item.defect for item in assessment.detections if item.confidence >= 0.45}
    changes = ChangeSet()

    if defects & {"spaghetti", "detachment", "first_layer", "adhesion"}:
        changes.first_layer_speed_multiplier = 0.75
        changes.bed_temp_delta_c = 5
        changes.brim_add_mm = 4.0
        changes.speed_multiplier = min(changes.speed_multiplier, 0.90)
        changes.notes.append("Improve first-layer adhesion and reduce early print speed.")

    if "warping" in defects:
        changes.bed_temp_delta_c = max(changes.bed_temp_delta_c, 5)
        changes.brim_add_mm = max(changes.brim_add_mm, 6.0)
        changes.speed_multiplier = min(changes.speed_multiplier, 0.90)
        changes.notes.append("Increase bed retention and brim to reduce warping.")

    if "layer_shift" in defects:
        changes.speed_multiplier = min(changes.speed_multiplier, 0.80)
        changes.acceleration_multiplier = min(changes.acceleration_multiplier, 0.75)
        changes.notes.append("Reduce speed/acceleration after visible layer shift.")

    if "stringing" in defects:
        changes.nozzle_temp_delta_c -= 5
        changes.retraction_delta_mm += 0.20
        changes.retraction_speed_delta_mm_s += 5.0
        changes.notes.append("Lower melt temperature slightly and increase retraction.")

    if defects & {"under_extrusion", "clog"}:
        changes.nozzle_temp_delta_c += 5
        changes.flow_multiplier *= 1.03
        changes.speed_multiplier = min(changes.speed_multiplier, 0.85)
        changes.notes.append("Increase thermal/flow margin and reduce throughput demand.")

    if defects & {"over_extrusion", "blob"}:
        changes.nozzle_temp_delta_c -= 5
        changes.flow_multiplier *= 0.97
        changes.speed_multiplier = min(changes.speed_multiplier, 0.90)
        changes.notes.append("Reduce flow/temperature after excess material or blob evidence.")

    if "support_failure" in defects:
        changes.supports = True
        changes.support_density_delta_pct = 8
        changes.speed_multiplier = min(changes.speed_multiplier, 0.90)
        changes.notes.append("Force supports and modestly increase support density.")

    if "collision" in defects:
        changes.speed_multiplier = min(changes.speed_multiplier, 0.70)
        changes.acceleration_multiplier = min(changes.acceleration_multiplier, 0.65)
        changes.notes.append("Reduce motion loads after collision evidence; inspect hardware before reprint.")

    # Orientation exists in ChangeSet but is intentionally never changed from a
    # single fixed camera. A scan/human stage may explicitly request 90-degree
    # increments after verifying the new footprint and support requirements.
    changes.rotation_z_deg = 0
    return changes.bounded()


def _read_ini_value(config: configparser.ConfigParser, key: str, fallback: str = "") -> str:
    # Prusa/Qidi exported config bundles are usually sectionless key=value files.
    for section in config.sections():
        if config.has_option(section, key):
            return config.get(section, key)
    return fallback


def _parse_number(value: str, default: float) -> float:
    try:
        # Vector fields often start with one numeric value followed by commas.
        return float(str(value).split(",", 1)[0].strip().rstrip("%"))
    except (TypeError, ValueError):
        return default


def _scaled_value(raw: str, multiplier: float, *, minimum: float, maximum: float) -> str | None:
    """Scale one numeric Prusa/Qidi value while preserving percent syntax."""
    text = str(raw).strip()
    if not text:
        return None
    is_percent = text.endswith("%")
    value = _parse_number(text, float("nan"))
    if value != value:  # NaN
        return None
    scaled = max(minimum, min(maximum, value * multiplier))
    rendered = f"{scaled:.3f}".rstrip("0").rstrip(".")
    return rendered + ("%" if is_percent else "")


def _delta_value(raw: str, delta: float, *, minimum: float, maximum: float) -> str | None:
    text = str(raw).strip()
    if not text or text.endswith("%"):
        return None
    value = _parse_number(text, float("nan"))
    if value != value:
        return None
    changed = max(minimum, min(maximum, value + delta))
    return f"{changed:.3f}".rstrip("0").rstrip(".")


def build_override_ini(
    base_profile: str,
    changes: ChangeSet,
    output_path: str,
) -> dict[str, dict[str, Any]]:
    """Create a concrete Qidi/Prusa override INI and before/after changelog.

    AI never writes arbitrary G-code. Vision yields a bounded ChangeSet and this
    deterministic adapter converts it into a small set of known slicer keys. If a
    profile lacks a key or stores it in an unsupported format, that change is
    recorded as skipped instead of guessed.
    """
    profile_path = Path(base_profile)
    base_text = profile_path.read_text(encoding="utf-8", errors="ignore") if profile_path.is_file() else ""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string("[profile]\n" + base_text)
    except configparser.Error:
        parser = configparser.ConfigParser(interpolation=None)
        parser.add_section("profile")

    bounded = changes.bounded()
    diff: dict[str, dict[str, Any]] = {}
    overrides: dict[str, str] = {}

    def set_change(key: str, before: Any, after: Any, reason: str) -> None:
        overrides[key] = str(after)
        diff[key] = {"before": before, "after": after, "reason": reason}

    def scale_existing(
        key: str,
        multiplier: float,
        *,
        minimum: float,
        maximum: float,
        reason: str,
    ) -> None:
        before = _read_ini_value(parser, key, "")
        after = _scaled_value(before, multiplier, minimum=minimum, maximum=maximum)
        if after is not None and after != before:
            set_change(key, before, after, reason)

    def delta_existing(
        key: str,
        delta: float,
        *,
        minimum: float,
        maximum: float,
        reason: str,
    ) -> None:
        before = _read_ini_value(parser, key, "")
        after = _delta_value(before, delta, minimum=minimum, maximum=maximum)
        if after is not None and after != before:
            set_change(key, before, after, reason)

    # Thermal and material flow.
    if bounded.nozzle_temp_delta_c:
        before = _parse_number(_read_ini_value(parser, "temperature", "220"), 220.0)
        after = max(150, min(320, round(before + bounded.nozzle_temp_delta_c)))
        set_change("temperature", before, after, "vision remediation: nozzle temperature")
        # First layer may have a dedicated temperature in many Qidi profiles.
        first_before_raw = _read_ini_value(parser, "first_layer_temperature", "")
        if first_before_raw:
            first_before = _parse_number(first_before_raw, before)
            first_after = max(150, min(320, round(first_before + bounded.nozzle_temp_delta_c)))
            set_change(
                "first_layer_temperature",
                first_before_raw,
                first_after,
                "vision remediation: first-layer nozzle temperature",
            )

    if bounded.bed_temp_delta_c:
        before = _parse_number(_read_ini_value(parser, "bed_temperature", "60"), 60.0)
        after = max(0, min(130, round(before + bounded.bed_temp_delta_c)))
        set_change("bed_temperature", before, after, "vision remediation: bed temperature")
        first_before_raw = _read_ini_value(parser, "first_layer_bed_temperature", "")
        if first_before_raw:
            first_before = _parse_number(first_before_raw, before)
            first_after = max(0, min(130, round(first_before + bounded.bed_temp_delta_c)))
            set_change(
                "first_layer_bed_temperature",
                first_before_raw,
                first_after,
                "vision remediation: first-layer bed temperature",
            )

    if abs(bounded.flow_multiplier - 1.0) > 1e-6:
        before = _parse_number(_read_ini_value(parser, "extrusion_multiplier", "1.0"), 1.0)
        after = round(max(0.85, min(1.15, before * bounded.flow_multiplier)), 4)
        set_change("extrusion_multiplier", before, after, "vision remediation: flow")

    # Print speeds: scale concrete profile values so revised G-code changes rather
    # than only carrying THOX metadata.
    if abs(bounded.speed_multiplier - 1.0) > 1e-6:
        for key in (
            "perimeter_speed",
            "small_perimeter_speed",
            "external_perimeter_speed",
            "infill_speed",
            "solid_infill_speed",
            "top_solid_infill_speed",
            "support_material_speed",
            "support_material_interface_speed",
            "bridge_speed",
            "gap_fill_speed",
        ):
            scale_existing(
                key,
                bounded.speed_multiplier,
                minimum=5.0,
                maximum=500.0,
                reason="vision remediation: global speed reduction",
            )

    # Acceleration: reduce only existing, numeric profile fields.
    if abs(bounded.acceleration_multiplier - 1.0) > 1e-6:
        for key in (
            "perimeter_acceleration",
            "infill_acceleration",
            "bridge_acceleration",
            "first_layer_acceleration",
            "default_acceleration",
        ):
            scale_existing(
                key,
                bounded.acceleration_multiplier,
                minimum=100.0,
                maximum=30000.0,
                reason="vision remediation: acceleration reduction",
            )

    # Retraction changes are only applied when the base profile exposes the key.
    if abs(bounded.retraction_delta_mm) > 1e-6:
        delta_existing(
            "retract_length",
            bounded.retraction_delta_mm,
            minimum=0.0,
            maximum=8.0,
            reason="vision remediation: retraction distance",
        )
    if abs(bounded.retraction_speed_delta_mm_s) > 1e-6:
        delta_existing(
            "retract_speed",
            bounded.retraction_speed_delta_mm_s,
            minimum=5.0,
            maximum=100.0,
            reason="vision remediation: retraction speed",
        )

    # First-layer controls preserve % syntax when present.
    if abs(bounded.first_layer_speed_multiplier - 1.0) > 1e-6:
        scale_existing(
            "first_layer_speed",
            bounded.first_layer_speed_multiplier,
            minimum=5.0,
            maximum=150.0,
            reason="vision remediation: first-layer speed",
        )
    if abs(bounded.first_layer_height_delta_mm) > 1e-6:
        delta_existing(
            "first_layer_height",
            bounded.first_layer_height_delta_mm,
            minimum=0.05,
            maximum=0.6,
            reason="vision remediation: first-layer height",
        )

    # Adhesion and support strategy.
    if bounded.supports is True:
        before = _read_ini_value(parser, "support_material", "0") or "0"
        set_change("support_material", before, 1, "vision remediation: support failure")
    if bounded.brim_add_mm > 0:
        before = _parse_number(_read_ini_value(parser, "brim_width", "0"), 0.0)
        after = round(min(20.0, before + bounded.brim_add_mm), 2)
        set_change("brim_width", before, after, "vision remediation: adhesion/warping")
    if bounded.raft_layers_add > 0:
        before = int(_parse_number(_read_ini_value(parser, "raft_layers", "0"), 0.0))
        after = min(6, before + bounded.raft_layers_add)
        set_change("raft_layers", before, after, "vision remediation: raft request")
    if bounded.support_density_delta_pct > 0:
        # Prusa/Qidi support spacing is inverse density: a modest positive density
        # request safely reduces spacing. Only modify it when the profile supplies
        # an absolute numeric spacing value.
        spacing_raw = _read_ini_value(parser, "support_material_spacing", "")
        spacing = _parse_number(spacing_raw, float("nan"))
        if spacing == spacing and spacing > 0:
            multiplier = max(0.60, 1.0 - bounded.support_density_delta_pct / 100.0)
            after = round(max(0.25, spacing * multiplier), 3)
            set_change(
                "support_material_spacing",
                spacing_raw,
                after,
                "vision remediation: increase support density",
            )

    # Orientation is a supported revision concept, but automatic vision diagnosis
    # currently leaves it at 0. If a human/scan stage explicitly sets one of the
    # allowed 90-degree values, the CLI adapter below applies it.
    if bounded.rotation_z_deg:
        diff["rotation_z_deg"] = {
            "before": 0,
            "after": bounded.rotation_z_deg,
            "reason": "explicit scan/human orientation revision",
        }

    lines = [
        "# Generated by THOX Print Health. Apply after the base Qidi profile.",
        "# AI did not write raw G-code; these are bounded known slicer settings.",
    ]
    lines += [f"{key} = {value}" for key, value in overrides.items()]
    Path(output_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return diff


def _build_cli_overrides(changes: ChangeSet) -> list[str]:
    bounded = changes.bounded()
    args: list[str] = []
    # Most revisions are applied through the generated config override. Keep only
    # geometry/action overrides here that are stable across Prusa-derived CLIs.
    if bounded.rotation_z_deg:
        args += ["--rotate", str(bounded.rotation_z_deg)]
    return args


def reslice(
    context: SliceContext,
    changes: ChangeSet,
    revision_number: int,
    reason: str,
    defects: list[str],
) -> tuple[Revision, dict[str, dict[str, Any]]]:
    source = Path(context.source_model).expanduser().resolve()
    profile = Path(context.base_profile).expanduser().resolve()
    slicer = context.slicer_bin or os.getenv("THOX_QIDI_SLICER_BIN", "qidi-slicer")
    if not source.is_file():
        raise RemediationError(f"source model not found: {source}")
    if not profile.is_file():
        raise RemediationError(f"base profile not found: {profile}")

    workdir = Path(context.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    override_path = workdir / f"revision_{revision_number:02d}_override.ini"
    output_gcode = workdir / f"revision_{revision_number:02d}.gcode"
    diff = build_override_ini(str(profile), changes, str(override_path))

    # QidiStudio/QIDISlicer are PrusaSlicer-derived and support headless export.
    # A command template can replace this default for vendor builds whose binary
    # name or arguments differ without changing code.
    command_template = os.getenv("THOX_QIDI_SLICE_COMMAND", "").strip()
    if command_template:
        values = {
            "slicer": shlex.quote(slicer),
            "profile": shlex.quote(str(profile)),
            "override": shlex.quote(str(override_path)),
            "output": shlex.quote(str(output_gcode)),
            "model": shlex.quote(str(source)),
        }
        command = shlex.split(command_template.format(**values))
    else:
        command = [
            slicer,
            "--load", str(profile),
            "--load", str(override_path),
            *_build_cli_overrides(changes),
            "--export-gcode",
            "--output", str(output_gcode),
            str(source),
        ]

    try:
        completed = subprocess.run(
            command,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=float(os.getenv("THOX_QIDI_SLICE_TIMEOUT_S", "900")),
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RemediationError(f"failed to run slicer: {exc}") from exc
    if completed.returncode != 0:
        raise RemediationError(
            f"slicer exited {completed.returncode}: {completed.stdout[-1200:]}"
        )
    if not output_gcode.is_file() or output_gcode.stat().st_size == 0:
        raise RemediationError("slicer produced no G-code")

    revision = Revision(
        revision=revision_number,
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        reason=reason[:2000],
        defects=sorted(set(defects)),
        source_model=str(source),
        base_profile=str(profile),
        output_gcode=str(output_gcode),
        previous_gcode=context.previous_gcode,
        changes=changes.bounded(),
        status="generated",
    )
    (workdir / f"revision_{revision_number:02d}_diff.json").write_text(
        json.dumps({"revision": revision.to_dict(), "diff": diff}, indent=2),
        encoding="utf-8",
    )
    return revision, diff
