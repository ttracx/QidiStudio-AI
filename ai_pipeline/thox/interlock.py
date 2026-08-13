"""Safety interlocks for every action that moves or heats the printer.

Deny by default. Each action declares which printer states it is legal in, and
anything else refuses with a typed reason rather than being attempted and
hoping.

Three rules here are worth stating because they are the ones that would be
"simplified" away by someone in a hurry:

**State is re-read, never cached.** A print can be started, paused or cancelled
from the printer's own touchscreen at any moment, including between two steps of
an agent's plan. Every guarded action re-queries immediately before acting.

**Cancel is never autonomous.** :class:`~thox.control.PrintController` will
pause on its own when configured to, because a pause is reversible and costs
minutes. Cancelling throws away hours of work and filament on the strength of a
model's opinion about a 640x480 JPEG, so it always requires a human. This is
enforced here, not left to the caller.

**Homing is never automated.** ``G28 Z`` probes by driving the nozzle toward the
bed. With a finished print still on the plate that is a collision, and nothing
in software can see that the plate is occupied. The interlock refuses to scan
without homing and tells the operator to home from the printer's own UI.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import Z_MACHINE_MAX_MM, Z_MACHINE_MIN_MM, ThoxSettings
from .errors import (
    ActionCooldown,
    ActionNotPermitted,
    NotHomed,
    PrinterBusy,
    PrinterNotPrinting,
    TooHot,
    UnsafePose,
)
from .moonraker import MoonrakerClient

#: States in which no job owns the machine.
IDLE_STATES = frozenset({"standby", "complete", "cancelled", "error"})

#: States in which a job is running or held.
ACTIVE_STATES = frozenset({"printing", "paused"})

#: Which printer states each control action is legal in. Positive lists: an
#: unrecognized state refuses rather than falling through.
ACTION_STATES: dict[str, frozenset[str]] = {
    "pause": frozenset({"printing"}),
    "resume": frozenset({"paused"}),
    "cancel": frozenset({"printing", "paused"}),
    "reprint": frozenset({"standby", "complete", "cancelled", "error"}),
}

#: Actions the agent may never take on its own, at any autonomy level.
HUMAN_ONLY_ACTIONS = frozenset({"cancel", "reprint"})


@dataclass
class Clearance:
    """Why an action was permitted, and within what bounds."""

    action: str
    printer_state: str
    hotend_c: float = 0.0
    bed_c: float = 0.0
    homed_axes: str = ""
    z_min_mm: float = 0.0
    z_max_mm: float = 0.0
    object_height_mm: float = 0.0
    actor: str = "human"
    detail: str = ""

    @property
    def travel_mm(self) -> float:
        return max(0.0, self.z_max_mm - self.z_min_mm)

    def describe(self) -> str:
        return (
            f"{self.action} cleared for {self.actor}: state={self.printer_state} "
            f"hotend={self.hotend_c:.0f}C homed={self.homed_axes!r}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "printer_state": self.printer_state,
            "hotend_c": self.hotend_c,
            "bed_c": self.bed_c,
            "homed_axes": self.homed_axes,
            "actor": self.actor,
            "detail": self.detail or self.describe(),
        }


@dataclass
class Interlock:
    """Gates every action that could move or heat the machine."""

    settings: ThoxSettings = field(default_factory=ThoxSettings.from_env)
    #: Monotonic timestamp of the last autonomous action, for cooldown.
    _last_agent_action: float = field(default=0.0, repr=False)

    # -- autonomy -----------------------------------------------------------

    def assert_actor_may(self, action: str, actor: str) -> None:
        """Raise unless ``actor`` is permitted to take ``action``.

        ``actor`` is ``"human"`` or ``"agent"``. A human is bounded by printer
        state only; an agent is additionally bounded by the configured autonomy
        level, the human-only list, and a cooldown.
        """
        if actor == "human":
            return
        if actor != "agent":
            raise ActionNotPermitted(action, f"unknown actor {actor!r}")

        if action in HUMAN_ONLY_ACTIONS:
            raise ActionNotPermitted(action, self.settings.autonomy)
        if not self.settings.may_act:
            raise ActionNotPermitted(action, self.settings.autonomy)
        if action == "pause" and not self.settings.may_auto_pause:
            raise ActionNotPermitted(action, self.settings.autonomy)

        elapsed = time.monotonic() - self._last_agent_action
        if self._last_agent_action and elapsed < self.settings.action_cooldown_s:
            raise ActionCooldown(self.settings.action_cooldown_s - elapsed)

    def note_agent_action(self) -> None:
        """Start the cooldown. Called after a successful autonomous action."""
        self._last_agent_action = time.monotonic()

    # -- print control ------------------------------------------------------

    def assert_can_control(
        self, client: MoonrakerClient, action: str, *, actor: str = "human"
    ) -> Clearance:
        """Raise unless ``action`` is safe and permitted right now.

        Raises:
            ActionNotPermitted: Autonomy policy forbids this actor.
            ActionCooldown: Another autonomous action ran too recently.
            PrinterNotPrinting: Printer state does not allow this action.
        """
        if action not in ACTION_STATES:
            raise ActionNotPermitted(action, "unknown action")
        self.assert_actor_may(action, actor)

        snapshot = client.job_snapshot()
        state = snapshot["state"]
        if state not in ACTION_STATES[action]:
            allowed = ", ".join(sorted(ACTION_STATES[action]))
            raise PrinterNotPrinting(
                state, f"{action} (allowed only while: {allowed})"
            )

        return Clearance(
            action=action,
            printer_state=state,
            hotend_c=snapshot["hotend_c"],
            bed_c=snapshot["bed_c"],
            homed_axes=snapshot["homed_axes"],
            actor=actor,
        )

    # -- scanning motion ----------------------------------------------------

    def assert_can_scan(
        self, client: MoonrakerClient, *, object_height_mm: float = 0.0
    ) -> Clearance:
        """Raise unless the bed may be moved for a scan.

        Raises:
            PrinterBusy: A job is running or paused.
            NotHomed: Z is not homed, so a commanded Z has no meaning.
            TooHot: Hotend above the scan ceiling - the operator's hands go
                near the bed to place an object.
            UnsafePose: No usable Z travel remains for an object this tall.
        """
        snapshot = client.job_snapshot()
        state = snapshot["state"]
        if state in ACTIVE_STATES:
            raise PrinterBusy(state, snapshot["filename"])
        if state not in IDLE_STATES:
            raise PrinterBusy(state)

        homed = snapshot["homed_axes"]
        if "z" not in homed.lower():
            raise NotHomed(homed)

        hotend = snapshot["hotend_c"]
        if hotend > self.settings.max_scan_hotend_c:
            raise TooHot(hotend, self.settings.max_scan_hotend_c)

        z_min, z_max = self.settings.z_window(object_height_mm)
        if z_max <= z_min:
            raise UnsafePose(
                f"no usable Z travel for an object {object_height_mm:.1f} mm "
                f"tall: the window collapsed to {z_min:.1f}..{z_max:.1f} mm"
            )

        return Clearance(
            action="scan",
            printer_state=state,
            hotend_c=hotend,
            bed_c=snapshot["bed_c"],
            homed_axes=homed,
            z_min_mm=z_min,
            z_max_mm=z_max,
            object_height_mm=object_height_mm,
        )

    def validate_z(self, z_mm: float, clearance: Clearance) -> float:
        """Return ``z_mm`` if inside the cleared window, else raise.

        Raises rather than clamping: silently clamping turns a planning bug into
        a duplicate frame at the window edge, which a reconstruction then treats
        as independent evidence.
        """
        if not isinstance(z_mm, (int, float)) or z_mm != z_mm:  # NaN
            raise UnsafePose(f"Z target is not a finite number: {z_mm!r}")
        if z_mm < Z_MACHINE_MIN_MM or z_mm > Z_MACHINE_MAX_MM:
            raise UnsafePose(
                f"Z={z_mm:.2f} mm is outside the machine envelope "
                f"({Z_MACHINE_MIN_MM}..{Z_MACHINE_MAX_MM} mm)"
            )
        if z_mm < clearance.z_min_mm or z_mm > clearance.z_max_mm:
            raise UnsafePose(
                f"Z={z_mm:.2f} mm is outside the cleared scan window "
                f"({clearance.z_min_mm:.2f}..{clearance.z_max_mm:.2f} mm)"
            )
        return float(z_mm)

    def move_script(self, z_mm: float, clearance: Clearance) -> str:
        """Validated G-code for one scan move: Z-only, absolute, then wait."""
        target = self.validate_z(z_mm, clearance)
        feed = float(self.settings.scan_feedrate_mm_min)
        return f"G90\nG0 Z{target:.3f} F{feed:.0f}\nM400"

    def validate_plan(self, z_targets: list[float], clearance: Clearance) -> None:
        """Validate a whole sweep before the first move is sent."""
        if not z_targets:
            raise UnsafePose("scan plan is empty")
        for z_mm in z_targets:
            self.validate_z(z_mm, clearance)
