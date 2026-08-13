"""Revise-and-reprint: turn a diagnosed failure into a better job.

Given a confirmed defect, this produces a **revision** - a list of parameter
changes, each with the reason it was made - and, where possible, a revised
G-code file that can be reprinted immediately.

The central distinction is between changes that can be applied **in place** and
changes that require **re-slicing**, because conflating them produces a file
that looks revised and is not:

*In-place* changes are runtime overrides a printer honours mid-file: nozzle and
bed temperature, flow percentage, feedrate percentage. These are injected as
G-code into the header of a copy of the original file. No slicer needed, so a
corrected reprint can start seconds after a failure.

*Re-slice* changes alter toolpaths: brim or raft, supports, retraction geometry,
first-layer height, orientation. There is no honest way to inject those into an
existing G-code - retraction distance is baked into thousands of individual
extrusion moves. Those changes are emitted as a **config patch** for QIDI Studio
to apply on a re-slice, and the revision says plainly that it is not directly
printable.

Two guard rails on the adjustment table:

**Every change is bounded.** Adjustments are clamped to a safe absolute range
regardless of how many times the loop runs, so a repeated adhesion failure
cannot walk the bed to 150 C.

**Escalation is deliberate but finite.** A second attempt at the same defect
adjusts further than the first, and after ``max_reprint_attempts`` the loop
stops and asks a human. Printing the same failure five times with slightly
different numbers wastes filament and teaches nothing.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .config import ThoxSettings
from .defects import DefectKind
from .errors import ReviseError

logger = logging.getLogger(__name__)

#: Absolute safety bounds. Applied after every adjustment, always.
BOUNDS: dict[str, tuple[float, float]] = {
    "nozzle_temperature": (170.0, 300.0),
    "bed_temperature": (0.0, 110.0),
    "chamber_temperature": (0.0, 60.0),
    "flow_ratio": (0.85, 1.15),
    "print_speed_percent": (30.0, 150.0),
    "first_layer_speed_percent": (20.0, 100.0),
    "retraction_length": (0.0, 6.0),
    "retraction_speed": (10.0, 80.0),
    "z_offset": (-0.3, 0.3),
    "pressure_advance": (0.0, 0.3),
    "cooling_percent": (0.0, 100.0),
}

#: Parameters a printer can honour mid-file, so they can be injected without
#: re-slicing. Everything else changes toolpaths.
IN_PLACE = {
    "nozzle_temperature",
    "bed_temperature",
    "chamber_temperature",
    "flow_ratio",
    "print_speed_percent",
}

#: Sensible starting values when a G-code file does not declare one.
DEFAULTS: dict[str, float] = {
    "nozzle_temperature": 220.0,
    "bed_temperature": 60.0,
    "flow_ratio": 1.0,
    "print_speed_percent": 100.0,
    "first_layer_speed_percent": 100.0,
    "retraction_length": 0.8,
    "retraction_speed": 40.0,
    "z_offset": 0.0,
    "pressure_advance": 0.05,
    "cooling_percent": 100.0,
}


@dataclass
class ParameterChange:
    """One adjustment, with the reason it was made."""

    name: str
    old_value: float
    new_value: float
    unit: str
    reason: str

    @property
    def requires_reslice(self) -> bool:
        return self.name not in IN_PLACE

    @property
    def delta(self) -> float:
        return self.new_value - self.old_value

    def describe(self) -> str:
        arrow = f"{self.old_value:g} -> {self.new_value:g} {self.unit}".strip()
        return f"{self.name}: {arrow} ({self.reason})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "delta": round(self.delta, 4),
            "unit": self.unit,
            "reason": self.reason,
            "requires_reslice": self.requires_reslice,
        }


@dataclass
class Revision:
    """A complete proposed revision for one failure."""

    defect: DefectKind
    attempt: int
    changes: list[ParameterChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source_file: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def requires_reslice(self) -> bool:
        return any(change.requires_reslice for change in self.changes)

    @property
    def in_place_changes(self) -> list[ParameterChange]:
        return [c for c in self.changes if not c.requires_reslice]

    @property
    def reslice_changes(self) -> list[ParameterChange]:
        return [c for c in self.changes if c.requires_reslice]

    @property
    def is_empty(self) -> bool:
        return not self.changes and not self.notes

    def to_dict(self) -> dict[str, Any]:
        return {
            "defect": self.defect.value,
            "defect_label": self.defect.label,
            "attempt": self.attempt,
            "source_file": self.source_file,
            "created_at": self.created_at,
            "requires_reslice": self.requires_reslice,
            "directly_printable": bool(self.in_place_changes) and not self.requires_reslice,
            "changes": [c.to_dict() for c in self.changes],
            "notes": list(self.notes),
            "changelog": self.changelog(),
        }

    def changelog(self) -> str:
        """Markdown changelog. Written into the revised file's header too."""
        lines = [
            f"# Revision {self.attempt} - {self.defect.label}",
            "",
            f"Diagnosed defect: **{self.defect.value}** "
            f"({self.defect.urgency.value})",
            f"Source job: `{self.source_file or 'unknown'}`",
            "",
        ]
        if self.in_place_changes:
            lines += ["## Applied directly to the G-code", ""]
            lines += [f"- {c.describe()}" for c in self.in_place_changes]
            lines.append("")
        if self.reslice_changes:
            lines += [
                "## Requires re-slicing (NOT applied to the G-code)",
                "",
                "These change toolpaths, so they cannot be injected into an "
                "existing file. Apply them in QIDI Studio and re-slice.",
                "",
            ]
            lines += [f"- {c.describe()}" for c in self.reslice_changes]
            lines.append("")
        if self.notes:
            lines += ["## Notes", ""] + [f"- {n}" for n in self.notes]
        return "\n".join(lines)


# -- adjustment table --------------------------------------------------------
#
# Each entry returns the changes to try for a defect, given the current values
# and which attempt this is. Escalation is expressed as a multiplier on the
# step, so attempt 2 moves further than attempt 1 without a second table.


def _step(attempt: int) -> float:
    """Escalation multiplier. Attempt 1 = 1.0, attempt 2 = 1.8, then 2.4."""
    return {1: 1.0, 2: 1.8}.get(attempt, 2.4)


def _adhesion_changes(current: dict[str, float], attempt: int) -> list[ParameterChange]:
    scale = _step(attempt)
    changes = [
        _change(
            "bed_temperature",
            current,
            +5.0 * scale,
            "degC",
            "raise bed temperature to improve first-layer grip",
        ),
        _change(
            "first_layer_speed_percent",
            current,
            -15.0 * scale,
            "%",
            "slow the first layer so extrudate has time to bond",
        ),
    ]
    if attempt >= 2:
        changes.append(
            _change(
                "z_offset",
                current,
                -0.02 * scale,
                "mm",
                "squash the first layer slightly harder into the plate",
            )
        )
    return changes


ADJUSTMENTS: dict[DefectKind, Any] = {
    DefectKind.ADHESION: _adhesion_changes,
    DefectKind.FIRST_LAYER: _adhesion_changes,
    DefectKind.DETACHMENT: _adhesion_changes,
    DefectKind.PRINT_CAME_LOOSE: _adhesion_changes,
    DefectKind.SPAGHETTI: _adhesion_changes,
    DefectKind.WARPING: lambda current, attempt: [
        _change(
            "bed_temperature",
            current,
            +5.0 * _step(attempt),
            "degC",
            "keep the part warmer to reduce differential shrinkage",
        ),
        _change(
            "cooling_percent",
            current,
            -20.0 * _step(attempt),
            "%",
            "reduce part cooling so layers contract less abruptly",
        ),
    ],
    DefectKind.LAYER_SHIFT: lambda current, attempt: [
        _change(
            "print_speed_percent",
            current,
            -15.0 * _step(attempt),
            "%",
            "lower speed reduces the inertial load that skips steps",
        ),
    ],
    DefectKind.STRINGING: lambda current, attempt: [
        _change(
            "retraction_length",
            current,
            +0.2 * _step(attempt),
            "mm",
            "retract more to relieve nozzle pressure before travel",
        ),
        _change(
            "nozzle_temperature",
            current,
            -5.0 * _step(attempt),
            "degC",
            "cooler filament oozes less",
        ),
    ],
    DefectKind.UNDER_EXTRUSION: lambda current, attempt: [
        _change(
            "flow_ratio",
            current,
            +0.03 * _step(attempt),
            "x",
            "increase flow to fill under-extruded walls",
        ),
        _change(
            "nozzle_temperature",
            current,
            +5.0 * _step(attempt),
            "degC",
            "hotter filament flows more freely",
        ),
    ],
    DefectKind.OVER_EXTRUSION: lambda current, attempt: [
        _change(
            "flow_ratio",
            current,
            -0.03 * _step(attempt),
            "x",
            "reduce flow to stop over-filling perimeters",
        ),
    ],
    DefectKind.BLOB: lambda current, attempt: [
        _change(
            "pressure_advance",
            current,
            +0.01 * _step(attempt),
            "",
            "more pressure advance reduces pressure spikes at direction changes",
        ),
        _change(
            "retraction_length",
            current,
            +0.1 * _step(attempt),
            "mm",
            "retract slightly more to avoid deposits at travel starts",
        ),
    ],
    DefectKind.NOZZLE_CLOG: lambda current, attempt: [
        _change(
            "nozzle_temperature",
            current,
            +10.0 * _step(attempt),
            "degC",
            "raise temperature to clear partial obstruction",
        ),
        _change(
            "print_speed_percent",
            current,
            -20.0 * _step(attempt),
            "%",
            "demand less flow per second from a struggling nozzle",
        ),
    ],
}

#: Advice that is not a numeric parameter. Surfaced as notes, because pretending
#: "add a brim" is a number would be dishonest about what the system can apply.
QUALITATIVE_NOTES: dict[DefectKind, tuple[str, ...]] = {
    DefectKind.ADHESION: (
        "Consider enabling a brim in QIDI Studio; it is usually more effective "
        "than any temperature change for adhesion.",
        "Clean the build plate with isopropyl alcohol. A contaminated plate "
        "defeats every parameter change on this list.",
    ),
    DefectKind.DETACHMENT: (
        "Consider a brim or raft. Tall narrow parts often need one regardless "
        "of temperature.",
    ),
    DefectKind.PRINT_CAME_LOOSE: (
        "Check the part's footprint. If it is small relative to its height, a "
        "brim or a different orientation matters more than temperature.",
    ),
    DefectKind.SPAGHETTI: (
        "Spaghetti usually follows a detachment that happened earlier. Inspect "
        "the first layers of the failed print before reprinting.",
    ),
    DefectKind.WARPING: (
        "An enclosed chamber helps far more than any slicer setting for "
        "warp-prone materials such as ABS or ASA.",
    ),
    DefectKind.LAYER_SHIFT: (
        "Check belt tension and that nothing obstructs the gantry. A layer "
        "shift is often mechanical, and no parameter change fixes a loose belt.",
    ),
    DefectKind.NOZZLE_CLOG: (
        "If reprinting does not clear it, cold-pull the nozzle. A true clog is "
        "not a slicing problem.",
    ),
}


def _change(
    name: str,
    current: dict[str, float],
    delta: float,
    unit: str,
    reason: str,
) -> ParameterChange:
    """Build one bounded change relative to the current value."""
    old = float(current.get(name, DEFAULTS.get(name, 0.0)))
    new = old + delta
    low, high = BOUNDS.get(name, (float("-inf"), float("inf")))
    clamped = max(low, min(high, new))
    if clamped != new:
        reason = f"{reason} (clamped to the safe range {low:g}-{high:g})"
    return ParameterChange(
        name=name, old_value=old, new_value=round(clamped, 4), unit=unit, reason=reason
    )


def plan_revision(
    defect: DefectKind,
    current: dict[str, float] | None = None,
    *,
    attempt: int = 1,
    source_file: str = "",
    settings: ThoxSettings | None = None,
) -> Revision:
    """Propose a revision for a diagnosed defect.

    Raises:
        ReviseError: If the defect has no parameter remedy, or the retry budget
            is exhausted.
    """
    settings = settings or ThoxSettings.from_env()
    current = dict(current or {})

    if attempt > settings.max_reprint_attempts:
        raise ReviseError(
            f"already attempted {attempt - 1} revision(s) for this job, which "
            f"is the configured limit ({settings.max_reprint_attempts}). "
            "Stopping and asking a human rather than burning more filament on "
            "the same failure."
        )

    builder = ADJUSTMENTS.get(defect)
    if builder is None:
        raise ReviseError(
            f"{defect.label} has no parameter remedy in the adjustment table. "
            "It is most likely mechanical or material, not a slicing problem."
        )

    changes = [c for c in builder(current, attempt) if c.delta != 0.0]
    revision = Revision(
        defect=defect,
        attempt=attempt,
        changes=changes,
        notes=list(QUALITATIVE_NOTES.get(defect, ())),
        source_file=source_file,
    )
    if not changes:
        revision.notes.insert(
            0,
            "Every numeric adjustment for this defect is already at its safe "
            "limit; only the qualitative changes below remain.",
        )
    return revision


# -- G-code reading and writing ----------------------------------------------

#: Slicer settings appear as trailing comments. Names differ between QIDI
#: Studio, Orca and PrusaSlicer lineages, so several spellings map to one key.
_GCODE_KEYS: dict[str, tuple[str, ...]] = {
    "nozzle_temperature": (
        "nozzle_temperature",
        "temperature",
        "first_layer_temperature",
    ),
    "bed_temperature": (
        "bed_temperature",
        "hot_plate_temp",
        "first_layer_bed_temperature",
        "textured_plate_temp",
    ),
    "flow_ratio": ("flow_ratio", "extrusion_multiplier"),
    "retraction_length": ("retraction_length", "retract_length"),
    "retraction_speed": ("retraction_speed", "retract_speed"),
    "pressure_advance": ("pressure_advance",),
}

_COMMENT_RE = re.compile(r"^\s*;\s*([A-Za-z0-9_ ]+?)\s*=\s*(.+?)\s*$")


def read_gcode_params(path: str, *, max_bytes: int = 512 * 1024) -> dict[str, float]:
    """Read slicer parameters from a G-code file's comment blocks.

    Only the head and tail are scanned. Slicers write their settings at one end
    or the other, and a sliced file is routinely hundreds of megabytes - reading
    all of it to find a temperature would stall the request that asked.
    """
    if not os.path.isfile(path):
        raise ReviseError(f"G-code file not found: {os.path.basename(path)}")

    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            head = handle.read(min(max_bytes, size))
            if size > max_bytes * 2:
                handle.seek(-max_bytes, os.SEEK_END)
                tail = handle.read(max_bytes)
            else:
                tail = b""
    except OSError as exc:
        raise ReviseError(f"could not read G-code ({type(exc).__name__})") from exc

    text = (head + b"\n" + tail).decode("utf-8", errors="replace")
    found: dict[str, float] = {}
    for line in text.splitlines():
        match = _COMMENT_RE.match(line)
        if not match:
            continue
        raw_key = match.group(1).strip().lower().replace(" ", "_")
        raw_value = match.group(2).split(",")[0].strip().rstrip("%")
        for canonical, aliases in _GCODE_KEYS.items():
            if canonical in found or raw_key not in aliases:
                continue
            try:
                found[canonical] = float(raw_value)
            except ValueError:
                pass
    return found


def _override_gcode(change: ParameterChange) -> list[str]:
    """G-code that applies one in-place change."""
    if change.name == "nozzle_temperature":
        return [f"M104 S{change.new_value:.0f} ; THOX revision: nozzle temperature"]
    if change.name == "bed_temperature":
        return [f"M140 S{change.new_value:.0f} ; THOX revision: bed temperature"]
    if change.name == "chamber_temperature":
        return [f"M141 S{change.new_value:.0f} ; THOX revision: chamber temperature"]
    if change.name == "flow_ratio":
        return [f"M221 S{change.new_value * 100:.0f} ; THOX revision: flow"]
    if change.name == "print_speed_percent":
        return [f"M220 S{change.new_value:.0f} ; THOX revision: feedrate"]
    return []


def apply_revision(
    source_path: str,
    revision: Revision,
    output_path: str,
) -> dict[str, Any]:
    """Write a revised G-code file with in-place overrides injected.

    The overrides are inserted **after** the original start G-code rather than
    at the top of the file. Start sequences normally set temperatures and wait
    on them (``M109``/``M190``); an override placed before that is simply
    overwritten by the slicer's own commands and silently does nothing.

    Detecting "after the start sequence" uses the first extruding move, which is
    the first real printing the file does.

    Returns:
        A summary including the output path and how many overrides landed.

    Raises:
        ReviseError: If the source is missing, or the revision has nothing that
            can be applied in place.
    """
    if not os.path.isfile(source_path):
        raise ReviseError(f"source G-code not found: {os.path.basename(source_path)}")

    in_place = revision.in_place_changes
    if not in_place:
        raise ReviseError(
            "this revision contains no in-place changes; it requires re-slicing "
            "in QIDI Studio and cannot be applied to an existing G-code file"
        )

    overrides: list[str] = []
    for change in in_place:
        overrides.extend(_override_gcode(change))
    if not overrides:
        raise ReviseError("no G-code override could be generated for this revision")

    header = [
        ";",
        "; ==== THOX print-health revision ====",
        f"; defect      : {revision.defect.value} ({revision.defect.label})",
        f"; attempt     : {revision.attempt}",
        f"; source      : {os.path.basename(source_path)}",
        "; changes applied in place:",
    ]
    header += [f";   - {c.describe()}" for c in in_place]
    if revision.reslice_changes:
        header.append("; changes NOT applied (need re-slicing):")
        header += [f";   - {c.describe()}" for c in revision.reslice_changes]
    header += [
        "; Overrides are injected after the start sequence so the slicer's own",
        "; M109/M190 waits cannot overwrite them.",
        "; ====================================",
        ";",
    ]

    extrude_re = re.compile(r"^G[01]\b.*\bE-?\d")
    injected = False
    try:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(source_path, encoding="utf-8", errors="replace") as src, open(
            output_path, "w", encoding="utf-8", newline="\n"
        ) as dst:
            dst.write("\n".join(header) + "\n")
            for line in src:
                if not injected and extrude_re.match(line.strip()):
                    dst.write("; THOX overrides begin\n")
                    dst.write("\n".join(overrides) + "\n")
                    dst.write("; THOX overrides end\n")
                    injected = True
                dst.write(line)
            if not injected:
                # No extruding move found. Rather than silently producing a file
                # with no overrides, append them and say so.
                dst.write("; THOX overrides (appended: no extrusion move found)\n")
                dst.write("\n".join(overrides) + "\n")
    except OSError as exc:
        raise ReviseError(
            f"could not write revised G-code ({type(exc).__name__})"
        ) from exc

    changelog_path = os.path.splitext(output_path)[0] + ".revision.md"
    try:
        with open(changelog_path, "w", encoding="utf-8") as handle:
            handle.write(revision.changelog())
    except OSError:
        changelog_path = ""

    return {
        "output_path": output_path,
        "changelog_path": changelog_path,
        "overrides_applied": len(overrides),
        "injected_after_start": injected,
        "requires_reslice": revision.requires_reslice,
        "revision": revision.to_dict(),
    }
