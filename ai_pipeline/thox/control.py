"""Agentic print control: pause, resume, cancel, reprint.

This is the only module that sends a lifecycle command to the printer. Every
call goes through :class:`~thox.interlock.Interlock` first, and every outcome -
including refusals - is written to the event log.

The design rule is that **the same code path serves the agent and the human**.
``actor`` is a parameter, not a separate implementation, so a human clicking
Pause and the monitor deciding to pause traverse identical validation. That
avoids the classic split where the UI path grows a guard the agent path never
got, and it means the audit trail records who asked in one place.

What the agent may do is narrower than what a human may do, and that asymmetry
is enforced in the interlock rather than here:

* **Pause** is agent-eligible at ``auto_pause`` autonomy. It is reversible and
  costs minutes.
* **Resume** is never autonomous. Something paused the print; a model deciding
  the coast is clear from one JPEG is not a good enough reason to restart it.
* **Cancel** and **reprint** are never autonomous at any autonomy level.
  Cancelling discards hours of work and reprinting consumes filament and a
  machine slot on the strength of a model's opinion about a 640x480 image.

Note that ``resume`` after an agent pause deliberately does *not* clear the
suspicion state. If the operator resumes a print the monitor believed was
failing, the monitor keeps watching and will alert again - it does not assume
the human fixed it, because it cannot see whether they did.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import ThoxSettings
from .errors import MoonrakerError, ThoxRefused
from .events import EventKind, EventLog
from .interlock import Interlock
from .moonraker import MoonrakerClient

logger = logging.getLogger(__name__)

#: Actions this module exposes.
ACTIONS = ("pause", "resume", "cancel", "reprint")


@dataclass
class ActionResult:
    """The outcome of one control action."""

    action: str
    ok: bool
    actor: str
    message: str
    printer_state_before: str = ""
    printer_state_after: str = ""
    reason: str = ""
    at: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "ok": self.ok,
            "actor": self.actor,
            "message": self.message,
            "reason": self.reason,
            "printer_state_before": self.printer_state_before,
            "printer_state_after": self.printer_state_after,
            "at": self.at,
            **({"data": self.data} if self.data else {}),
        }


class PrintController:
    """Guarded pause / resume / cancel / reprint."""

    def __init__(
        self,
        client: MoonrakerClient,
        settings: ThoxSettings | None = None,
        interlock: Interlock | None = None,
        events: EventLog | None = None,
    ) -> None:
        self.client = client
        self.settings = settings or ThoxSettings.from_env()
        self.interlock = interlock or Interlock(self.settings)
        self.events = events or EventLog(self.settings.state_root)

    # -- core ---------------------------------------------------------------

    def act(
        self,
        action: str,
        *,
        actor: str = "human",
        why: str = "",
        filename: str = "",
    ) -> ActionResult:
        """Perform one control action, or explain why it was refused.

        Never raises for a refusal or a printer error: both come back as an
        ``ActionResult`` with ``ok=False``. A UI showing a stack trace when the
        answer is "the printer is not printing" is a worse UI.
        """
        if action not in ACTIONS:
            return self._refused(
                action, actor, "unknown_action", f"unknown action {action!r}"
            )

        try:
            clearance = self.interlock.assert_can_control(
                self.client, action, actor=actor
            )
        except ThoxRefused as exc:
            return self._refused(action, actor, exc.reason, exc.detail, why=why)
        except MoonrakerError as exc:
            # Could not determine safety. Distinct from "printer said no", and
            # equally blocking.
            return self._refused(
                action,
                actor,
                "printer_unreachable",
                f"could not read printer state ({type(exc).__name__})",
                why=why,
            )

        before = clearance.printer_state
        try:
            self._dispatch(action, filename)
        except MoonrakerError as exc:
            message = f"{action} failed ({type(exc).__name__})"
            self.events.add(
                EventKind.ERROR,
                message,
                action=action,
                actor=actor,
                printer_state=before,
            )
            return ActionResult(
                action=action,
                ok=False,
                actor=actor,
                message=message,
                reason="transport_error",
                printer_state_before=before,
            )

        if actor == "agent":
            self.interlock.note_agent_action()

        after = self._settled_state(action, before)
        message = f"{action} by {actor}" + (f": {why}" if why else "")
        self.events.add(
            EventKind.ACTION_TAKEN,
            message,
            severity=0.6 if action in {"cancel", "pause"} else 0.2,
            action=action,
            actor=actor,
            printer_state_before=before,
            printer_state_after=after,
            why=why,
        )
        return ActionResult(
            action=action,
            ok=True,
            actor=actor,
            message=message,
            printer_state_before=before,
            printer_state_after=after,
        )

    def _dispatch(self, action: str, filename: str) -> None:
        if action == "pause":
            self.client.pause()
        elif action == "resume":
            self.client.resume()
        elif action == "cancel":
            self.client.cancel()
        elif action == "reprint":
            target = filename or self._last_job_filename()
            if not target:
                raise MoonrakerError(
                    "no filename given and no previous job found to reprint"
                )
            self.client.start(target)

    def _settled_state(self, action: str, before: str) -> str:
        """Read back the state, giving the printer a moment to transition.

        Moonraker returns before Klipper has finished the transition, so an
        immediate read reports the *old* state and the event log then claims
        nothing happened. Polling briefly makes the record truthful.
        """
        deadline = time.monotonic() + 8.0
        expected = {
            "pause": {"paused"},
            "resume": {"printing"},
            "cancel": {"cancelled", "standby", "complete"},
            "reprint": {"printing"},
        }.get(action, set())

        last = before
        while time.monotonic() < deadline:
            try:
                last = self.client.print_state()
            except MoonrakerError:
                break
            if last in expected:
                return last
            time.sleep(0.4)
        return last

    def _last_job_filename(self) -> str:
        try:
            jobs = self.client.history(limit=1)
        except MoonrakerError:
            return ""
        if not jobs:
            return ""
        entry = jobs[0]
        return str(entry.get("filename") or "")

    def _refused(
        self, action: str, actor: str, reason: str, detail: str, *, why: str = ""
    ) -> ActionResult:
        try:
            state = self.client.print_state()
        except MoonrakerError:
            state = "unknown"

        # A refusal driven by autonomy policy is not a failure - it is the
        # system asking a human. Logged as a suggestion so the UI can offer the
        # button rather than showing an error.
        if reason == "not_permitted":
            self.events.add(
                EventKind.ACTION_SUGGESTED,
                f"agent suggests {action}: {why or detail}",
                severity=0.7,
                action=action,
                actor=actor,
                printer_state=state,
                detail=detail,
            )
        else:
            self.events.add(
                EventKind.ACTION_REFUSED,
                f"{action} refused ({reason}): {detail}",
                severity=0.3,
                action=action,
                actor=actor,
                printer_state=state,
            )

        return ActionResult(
            action=action,
            ok=False,
            actor=actor,
            message=detail,
            reason=reason,
            printer_state_before=state,
        )

    # -- convenience --------------------------------------------------------

    def pause(self, *, actor: str = "human", why: str = "") -> ActionResult:
        return self.act("pause", actor=actor, why=why)

    def resume(self, *, actor: str = "human", why: str = "") -> ActionResult:
        return self.act("resume", actor=actor, why=why)

    def cancel(self, *, actor: str = "human", why: str = "") -> ActionResult:
        return self.act("cancel", actor=actor, why=why)

    def reprint(
        self, filename: str = "", *, actor: str = "human", why: str = ""
    ) -> ActionResult:
        return self.act("reprint", actor=actor, why=why, filename=filename)

    def available_actions(self) -> dict[str, Any]:
        """Which actions are currently legal, for rendering buttons.

        Returned per-actor so a UI can enable a human's Cancel button while
        showing that the agent is not permitted to press it.
        """
        try:
            state = self.client.print_state()
        except MoonrakerError:
            state = "disconnected"

        from .interlock import ACTION_STATES, HUMAN_ONLY_ACTIONS

        out: dict[str, Any] = {"printer_state": state, "actions": {}}
        for action in ACTIONS:
            legal = state in ACTION_STATES[action]
            agent_allowed = (
                legal
                and action not in HUMAN_ONLY_ACTIONS
                and self.settings.may_act
                and (action != "pause" or self.settings.may_auto_pause)
            )
            out["actions"][action] = {
                "human": legal,
                "agent": agent_allowed,
                "reason": "" if legal else f"not available while {state!r}",
            }
        out["autonomy"] = self.settings.autonomy
        return out
