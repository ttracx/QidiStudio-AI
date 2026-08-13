"""The parallel print-health ensemble.

Every configured provider inspects the same frame **concurrently** on a thread
pool, and the reports are fused by :func:`thox.defects.fuse`.

The concurrency is not a micro-optimization. The classical tripwire answers in
~20 ms and a warm language model in ~5-30 s; run in series the slowest member
sets the cadence, and a monitor that takes a minute per sample cannot escalate
fast enough to catch a print detaching. Threads (not processes, not an event
loop) because the work is entirely network-bound HTTP plus a little numpy, both
of which release the GIL, and because the host is already a threaded Flask app.

Three properties are load-bearing:

**No member can break a run.** Providers must not raise; the pool wraps them
anyway. A provider that times out, crashes or returns nonsense is recorded as
skipped and fusion proceeds with whoever answered.

**Skipped is not healthy.** A provider that could not look contributes nothing -
it never counts as a vote for "the print is fine". Collapsing those two would
mean a dead camera or an evicted model reads as a clean bill of health, which is
the single most dangerous failure this system could have.

**One call in flight per provider.** A single Ollama instance serializes
requests to the same model, so issuing two at once does not halve the wall clock
- it doubles the queue while both race the same deadline. Measured, two
concurrent calls blew a 180 s timeout that one clears in ~31 s.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import ThoxSettings
from ..defects import HealthVerdict, ProviderReport, fuse
from .base import FrameContext, HealthProvider
from .cv_motion import MotionTripwire
from .vlm import OllamaCloudHealth, OllamaLocalHealth, OpenAIHealth

logger = logging.getLogger(__name__)

#: Provider id -> factory. Adding a member is one line here plus the id in
#: ThoxSettings.validate().
REGISTRY = {
    "cv_motion": lambda settings: MotionTripwire(),
    "ollama_local": OllamaLocalHealth,
    "ollama_cloud": OllamaCloudHealth,
    "openai": OpenAIHealth,
}


class HealthEnsemble:
    """Runs every configured provider on each frame, in parallel, then fuses."""

    def __init__(
        self,
        settings: ThoxSettings | None = None,
        providers: list[HealthProvider] | None = None,
    ) -> None:
        self.settings = settings or ThoxSettings.from_env()
        self.providers = (
            providers if providers is not None else self._build(self.settings)
        )
        self.active: list[str] = []
        self.inactive: dict[str, str] = {}
        self._survey()
        self._gates: dict[str, threading.Lock] = {
            provider.name: threading.Lock() for provider in self.providers
        }

    @staticmethod
    def _build(settings: ThoxSettings) -> list[HealthProvider]:
        built: list[HealthProvider] = []
        for provider_id in settings.provider_ids:
            factory = REGISTRY.get(provider_id)
            if factory is None:
                logger.warning("[THOX] unknown vision provider %r", provider_id)
                continue
            try:
                built.append(factory(settings))
            except Exception as exc:
                logger.warning(
                    "[THOX] could not construct provider %r (%s)",
                    provider_id,
                    type(exc).__name__,
                )
        return built

    def _survey(self) -> None:
        self.active = []
        self.inactive = {}
        for provider in self.providers:
            try:
                ok, why = provider.available()
            except Exception as exc:
                ok, why = False, f"availability check raised {type(exc).__name__}"
            if ok:
                self.active.append(provider.name)
            else:
                self.inactive[provider.name] = why

    # -- properties ---------------------------------------------------------

    @property
    def can_run(self) -> bool:
        return bool(self.active)

    @property
    def has_classifier(self) -> bool:
        """Whether any member can actually name a defect.

        Without one, the system is a change detector and an exposure gate. That
        is genuinely useful as a tripwire, and it is not defect classification -
        the UI says so rather than implying full coverage.
        """
        return any(name != "cv_motion" for name in self.active)

    @property
    def sends_frames_offsite(self) -> list[str]:
        """Active members that transmit frames off the local network."""
        return [
            p.name
            for p in self.providers
            if p.name in set(self.active) and getattr(p, "sends_frames_offsite", False)
        ]

    def status(self) -> dict[str, object]:
        return {
            "active": list(self.active),
            "inactive": dict(self.inactive),
            "has_classifier": self.has_classifier,
            "sends_frames_offsite": self.sends_frames_offsite,
        }

    def describe(self) -> str:
        parts = [f"active: {', '.join(self.active) or 'none'}"]
        for name, reason in self.inactive.items():
            parts.append(f"skipped {name} ({reason})")
        return "; ".join(parts)

    # -- lifecycle ----------------------------------------------------------

    def warmup(self) -> None:
        """Pre-load every active provider concurrently. Never raises."""
        targets = [p for p in self.providers if p.name in set(self.active)]
        if not targets:
            return
        with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
            futures = [pool.submit(self._safe_warmup, p) for p in targets]
            for future in as_completed(futures):
                future.result()

    @staticmethod
    def _safe_warmup(provider: HealthProvider) -> None:
        try:
            provider.warmup()
        except Exception as exc:
            logger.debug(
                "[THOX] warmup of %s failed, non-fatal (%s)",
                provider.name,
                type(exc).__name__,
            )

    def reset(self) -> None:
        """Clear per-job provider state. Called when a new print starts."""
        for provider in self.providers:
            reset = getattr(provider, "reset", None)
            if callable(reset):
                try:
                    reset()
                except Exception:
                    pass

    # -- inspection ---------------------------------------------------------

    def _run_one(
        self, provider: HealthProvider, jpeg: bytes, context: FrameContext
    ) -> ProviderReport:
        """Invoke one provider, converting any escape into a skipped report."""
        gate = self._gates.get(provider.name)
        try:
            if gate is not None:
                gate.acquire()
            try:
                return provider.inspect(jpeg, context)
            finally:
                if gate is not None:
                    gate.release()
        except Exception as exc:
            # Contract violation: providers must not raise. Record and continue.
            logger.warning(
                "[THOX] provider %s raised %s", provider.name, type(exc).__name__
            )
            return ProviderReport(
                provider=provider.name,
                ok=False,
                skipped_reason=f"provider raised {type(exc).__name__}",
            )

    def inspect(
        self,
        jpeg: bytes,
        context: FrameContext,
        *,
        include_slow: bool = True,
    ) -> HealthVerdict:
        """Inspect one frame with every capable provider, in parallel.

        Args:
            include_slow: When False, only providers that answer in
                milliseconds run. The monitor uses this for routine samples and
                switches it on once the tripwire suspects something, which is
                what keeps a 45-second cadence from costing a model call every
                45 seconds for hours.
        """
        active = set(self.active)
        targets = [
            p
            for p in self.providers
            if p.name in active and (include_slow or getattr(p, "fast", False))
        ]

        reports: list[ProviderReport] = []
        if targets:
            with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
                futures = {
                    pool.submit(self._run_one, provider, jpeg, context): provider
                    for provider in targets
                }
                for future in as_completed(futures):
                    reports.append(future.result())

        # Providers excluded by include_slow are not "skipped" - they were never
        # asked. Recording them as skipped would make routine samples look like
        # degraded ones in the event log.
        return fuse(
            reports,
            frame_index=context.frame_index,
            captured_at=time.time(),
            layer=context.layer,
            min_agreement=self.settings.min_agreement,
        )
