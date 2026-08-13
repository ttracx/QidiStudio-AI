"""Deterministic failure remediation and safe QidiStudio/QIDISlicer reslicing."""
from __future__ import annotations

import configparser
import json
import os
import shlex
import subprocess
import tempfile
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

    # Orientation is represented in the schema but never changed automatically
    # from a single fixed camera. A human/scan stage may set a 90-degree Z rotation.
    changes.rotation_z_deg = 0
    return changes.bounded()


_CONFIG_MAPPING = {
    "nozzle_temp_delta_c": "temperature",
    "bed_temp_delta_c": "bed_temperature",
    "flow_multiplier": "extrusion_multiplier",
    "speed_multiplier": "thox_speed_multiplier",
    "acceleration_multiplier": "thox_acceleration_multiplier",
    "retraction_delta_mm": "thox_retraction_delta_mm",
    "retraction_speed_delta_mm_s": "thox_retraction_speed_delta_mm_s",
    "first_layer_height_delta_mm": "thox_first_layer_height_delta_mm",
    "first_layer_speed_multiplier": "thox_first_layer_speed_multiplier",
    "brim_add_mm": "thox_brim_add_mm",
    "raft_layers_add": "thox_raft_layers_add",
    "support_density_delta_pct": "thox_support_density_delta_pct",
}


def _read_ini_value(config: configparser.ConfigParser, key: str, fallback: str = "") -> str:
    # Prusa/Qidi exported config bundles are often key=value without sections.
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


def build_override_ini(base_profile: str, changes: ChangeSet, output_path: str) -> dict[str, dict[str, Any]]:
    """Create a small override INI plus a machine-readable before/after diff.

    Standard Prusa/Qidi keys are used where they are stable. THOX-only multiplier
    keys are also emitted as metadata for the wrapper/Forger integration; the
    command adapter converts them to concrete CLI flags when possible.
    """
    base_text = Path(base_profile).read_text(encoding="utf-8", errors="ignore") if Path(base_profile).is_file() else ""
    # ConfigParser requires a section. This is read-only and tolerates duplicate keys.
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

    if bounded.nozzle_temp_delta_c:
        before = _parse_number(_read_ini_value(parser, "temperature", "220"), 220.0)
        after = max(150, min(320, round(before + bounded.nozzle_temp_delta_c)))
        set_change("temperature", before, after, "vision remediation: nozzle temperature delta")
    if bounded.bed_temp_delta_c:
        before = _parse_number(_read_ini_value(parser, "bed_temperature", "60"), 60.0)
        after = max(0, min(130, round(before + bounded.bed_temp_delta_c)))
        set_change("bed_temperature", before, after, "vision remediation: bed temperature delta")
    if abs(bounded.flow_multiplier - 1.0) > 1e-6:
        before = _parse_number(_read_ini_value(parser, "extrusion_multiplier", "1.0"), 1.0)
        after = round(max(0.85, min(1.15, before * bounded.flow_multiplier)), 4)
        set_change("extrusion_multiplier", before, after, "vision remediation: flow multiplier")
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

    # These are consumed by the command wrapper below and included in the diff.
    metadata = {
        "thox_speed_multiplier": bounded.speed_multiplier,
        "thox_acceleration_multiplier": bounded.acceleration_multiplier,
        "thox_retraction_delta_mm": bounded.retraction_delta_mm,
        "thox_retraction_speed_delta_mm_s": bounded.retraction_speed_delta_mm_s,
        "thox_first_layer_height_delta_mm": bounded.first_layer_height_delta_mm,
        "thox_first_layer_speed_multiplier": bounded.first_layer_speed_multiplier,
        "thox_support_density_delta_pct": bounded.support_density_delta_pct,
        "thox_rotation_z_deg": bounded.rotation_z_deg,
    }
    for key, value in metadata.items():
        if value not in (0, 0.0, 1, 1.0):
            diff[key] = {"before": "base profile", "after": value, "reason": "bounded THOX remediation"}

    lines = ["# Generated by THOX Print Health. Apply after the base Qidi profile."]
    lines += [f"{key} = {value}" for key, value in overrides.items()]
    Path(output_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return diff


def _build_cli_overrides(changes: ChangeSet) -> list[str]:
    bounded = changes.bounded()
    args: list[str] = []
    # Prefer stable CLI overrides. More profile-sensitive controls remain in the
    # generated override file/changelog and can be consumed by THOX Forger.
    if bounded.supports is True:
        args += ["--support-material", "1"]
    if bounded.brim_add_mm > 0:
        args += ["--brim-width", f"{bounded.brim_add_mm:.2f}"]
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
