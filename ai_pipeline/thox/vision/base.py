"""Provider protocol for print-health vision.

Synchronous, because the host is a threaded Flask app; the ensemble runs
providers concurrently on a thread pool rather than an event loop.

A provider must **never raise** for ordinary failure. Every failure path returns
a :class:`~thox.defects.ProviderReport` with ``ok=False`` and a reason, because
the entire value of an ensemble is continuing with the members that did answer.
The ensemble wraps providers anyway, in case one violates the contract.

``FrameContext`` carries what the frame alone cannot say: which layer is being
printed, how far in, and the previous frame. That context is what separates a
useful judgement from a guess - "there is material outside the object outline"
means something very different on layer 1 than on layer 300, and a model given
no layer number will happily flag a raft as spaghetti.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..defects import ProviderReport


@dataclass
class FrameContext:
    """Everything a provider knows about a frame beyond its pixels."""

    frame_index: int = 0
    layer: int | None = None
    total_layers: int | None = None
    progress: float = 0.0
    print_duration_s: float = 0.0
    filename: str = ""
    #: The previously captured frame, for change detection.
    previous_jpeg: bytes | None = None
    #: Seconds between this frame and the previous one.
    since_previous_s: float = 0.0
    #: Running hypothesis from earlier frames, surfaced to VLM prompts so a
    #: judgement is made against what the print looked like before, not in
    #: isolation.
    prior_note: str = ""
    telemetry: dict[str, Any] = field(default_factory=dict)

    @property
    def is_first_layers(self) -> bool:
        return self.layer is not None and self.layer <= 3

    def describe(self) -> str:
        parts = []
        if self.layer is not None:
            total = f"/{self.total_layers}" if self.total_layers else ""
            parts.append(f"layer {self.layer}{total}")
        if self.progress:
            parts.append(f"{self.progress * 100:.0f}% complete")
        if self.print_duration_s:
            parts.append(f"{self.print_duration_s / 60:.0f} min in")
        return ", ".join(parts)


@runtime_checkable
class HealthProvider(Protocol):
    """A member of the parallel print-health ensemble."""

    #: Stable id used in settings, event records and the UI.
    name: str
    #: Whether this provider needs network egress with a frame attached.
    sends_frames_offsite: bool
    #: Whether this provider answers in milliseconds. Fast providers run on
    #: every routine sample; slow ones are held back until something looks
    #: wrong, which is what makes continuous monitoring affordable.
    fast: bool

    def available(self) -> tuple[bool, str]:
        """Whether this provider can run, and why not if it cannot."""
        ...

    def warmup(self) -> None:
        """Best-effort pre-load. Must not raise."""
        ...

    def inspect(self, jpeg: bytes, context: FrameContext) -> ProviderReport:
        """Assess one frame. Must not raise for ordinary failures."""
        ...
