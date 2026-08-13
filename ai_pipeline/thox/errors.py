"""Typed errors for the THOX printer-agent layer.

The distinction that drives the HTTP layer is **refused** versus **failed**:

``ThoxRefused``
    We declined to act. Nothing was sent to the printer and the machine is
    untouched. The operator can clear the condition and retry. These map to
    HTTP 409, not 500, because a UI must be able to say "finish your print
    first" rather than "server error".

``ThoxFailed``
    We acted and something went wrong afterwards. The printer may be mid-
    sequence, so the operator needs to look at it.

Everything raised here is safe to show a user: no paths, no credentials, no
tracebacks. That mirrors ``_safe_internal_error`` in ``server.py``, which
deliberately never serializes exception text.
"""

from __future__ import annotations


class ThoxError(Exception):
    """Base for everything this package raises."""


# -- Refusals: the printer was not touched -----------------------------------


class ThoxRefused(ThoxError):
    """We declined to act.

    Attributes:
        reason: Machine-readable code, e.g. ``printer_busy``.
        detail: Human-readable explanation safe to display.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class PrinterBusy(ThoxRefused):
    def __init__(self, state: str, filename: str = "") -> None:
        job = f" ({filename})" if filename else ""
        super().__init__(
            "printer_busy",
            f"printer state is {state!r}{job}; this action requires an idle machine",
        )


class PrinterNotPrinting(ThoxRefused):
    """A print-control action was requested with no job running."""

    def __init__(self, state: str, action: str) -> None:
        super().__init__(
            "not_printing",
            f"cannot {action}: printer state is {state!r}, not printing",
        )


class NotHomed(ThoxRefused):
    def __init__(self, homed: str) -> None:
        super().__init__(
            "not_homed",
            f"homed axes are {homed!r}; Z must be homed first. Homing drives the "
            "nozzle down to probe the bed, so it stays a deliberate human "
            "action - run G28 from the printer's own UI.",
        )


class TooHot(ThoxRefused):
    def __init__(self, temp_c: float, limit_c: float) -> None:
        super().__init__(
            "too_hot",
            f"hotend is {temp_c:.0f}C, limit is {limit_c:.0f}C; let it cool first",
        )


class UnsafePose(ThoxRefused):
    def __init__(self, detail: str) -> None:
        super().__init__("unsafe_pose", detail)


class ActionNotPermitted(ThoxRefused):
    """The configured autonomy level forbids this action.

    Raised when the agent wants to act but policy says a human decides. Carries
    the level so a UI can offer the human the same button.
    """

    def __init__(self, action: str, autonomy: str) -> None:
        super().__init__(
            "not_permitted",
            f"autonomy level {autonomy!r} does not permit the agent to {action} "
            "on its own; a human must confirm",
        )


class ActionCooldown(ThoxRefused):
    """Another autonomous action happened too recently."""

    def __init__(self, remaining_s: float) -> None:
        super().__init__(
            "cooldown",
            f"another autonomous action ran recently; {remaining_s:.0f}s of "
            "cooldown remain. This prevents a flapping detector from pausing "
            "and resuming in a loop.",
        )


# -- Transport ---------------------------------------------------------------


class MoonrakerError(ThoxError):
    """Base for Moonraker transport and protocol failures."""


class ConnectionFailed(MoonrakerError):
    pass


class RequestTimeout(MoonrakerError):
    pass


class APIError(MoonrakerError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotFound(APIError):
    pass


# -- Operation failures ------------------------------------------------------


class ThoxFailed(ThoxError):
    """We acted and it did not complete."""


class CameraUnavailable(ThoxFailed):
    pass


class CaptureFailed(ThoxFailed):
    pass


class MotionTimeout(ThoxFailed):
    def __init__(self, target_z: float, actual_z: float, waited_s: float) -> None:
        super().__init__(
            f"Z did not settle at {target_z:.2f} mm after {waited_s:.1f}s "
            f"(last reading {actual_z:.2f} mm)"
        )


class ReconstructionFailed(ThoxFailed):
    pass


class NoObjectFound(ThoxFailed):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            detail
            or "no object detected on the bed; is it inside the camera's view?"
        )


class PlateError(ThoxError):
    pass


class ObjectTooLarge(PlateError):
    def __init__(self, dims_mm: tuple[float, float, float], limit: str) -> None:
        x, y, z = dims_mm
        super().__init__(
            f"object measures {x:.1f} x {y:.1f} x {z:.1f} mm, which exceeds {limit}"
        )


class ReviseError(ThoxError):
    """A revised job could not be produced."""


class MaxRetriesReached(ThoxRefused):
    def __init__(self, attempts: int) -> None:
        super().__init__(
            "max_retries",
            f"already retried {attempts} time(s); stopping and asking a human "
            "rather than burning more filament on the same failure",
        )


# -- Vision ------------------------------------------------------------------


class ProviderError(ThoxError):
    """Never aborts an ensemble on its own."""


class ProviderUnavailable(ProviderError):
    pass


class ValidationError(ThoxError):
    """Caller supplied an invalid argument."""
