"""Tests for the THOX printer-agent layer. No printer, no network, no sockets.

Everything that talks to hardware goes through :class:`FakeClient`, so the whole
safety and decision surface is exercised deterministically.

The tests are weighted toward the rules that are dangerous to get wrong rather
than toward line coverage:

* an unrecognized printer state must **refuse**, not fall through;
* the agent must never be able to cancel or reprint, at any autonomy level;
* a provider that could not look must never read as "the print is fine";
* credentials must not appear in any settings dump.
"""

from __future__ import annotations

import io
import os
import sys

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from thox.config import ThoxSettings, redact  # noqa: E402
from thox.control import PrintController  # noqa: E402
from thox.defects import (  # noqa: E402
    DefectKind,
    Detection,
    ProviderReport,
    Urgency,
    fuse,
    parse_kind,
)
from thox.errors import (  # noqa: E402
    ActionCooldown,
    ActionNotPermitted,
    NotHomed,
    PrinterBusy,
    PrinterNotPrinting,
    TooHot,
    UnsafePose,
)
from thox.events import EventKind, EventLog  # noqa: E402
from thox.interlock import Interlock  # noqa: E402
from thox.revise import (  # noqa: E402
    BOUNDS,
    apply_revision,
    plan_revision,
    read_gcode_params,
)
from thox.vision.cv_motion import MotionTripwire  # noqa: E402
from thox.vision.base import FrameContext  # noqa: E402
from thox.vision.vlm import extract_json, parse_defects  # noqa: E402


def settings(**overrides) -> ThoxSettings:
    base = ThoxSettings(printer_host="10.0.0.1")
    for key, value in overrides.items():
        setattr(base, key, value)
    base.validate()
    return base


class FakeClient:
    """Stands in for MoonrakerClient. Records what it was asked to do."""

    def __init__(self, state="printing", homed="xyz", hotend=210.0, filename="job.gcode"):
        self._state = state
        self.homed = homed
        self.hotend = hotend
        self.filename = filename
        self.calls: list[str] = []

    def job_snapshot(self):
        return {
            "state": self._state,
            "filename": self.filename,
            "progress": 0.5,
            "current_layer": 40,
            "total_layer": 100,
            "print_duration_s": 1200.0,
            "filament_used_mm": 900.0,
            "hotend_c": self.hotend,
            "hotend_target_c": self.hotend,
            "bed_c": 55.0,
            "bed_target_c": 55.0,
            "homed_axes": self.homed,
            "position": [10.0, 10.0, 5.0],
        }

    def print_state(self):
        return self._state

    def pause(self):
        self.calls.append("pause")
        self._state = "paused"

    def resume(self):
        self.calls.append("resume")
        self._state = "printing"

    def cancel(self):
        self.calls.append("cancel")
        self._state = "cancelled"

    def start(self, filename):
        self.calls.append(f"start:{filename}")
        self._state = "printing"

    def history(self, limit=1):
        return [{"filename": self.filename}]

    def query(self, objects=None):
        return {"toolhead": {"position": [10.0, 10.0, 5.0]}}


def jpeg(value: int = 128, size=(64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(np.full((size[1], size[0]), value, np.uint8)).save(
        buffer, format="JPEG"
    )
    return buffer.getvalue()


def noisy_jpeg(seed: int = 0, size=(64, 48)) -> bytes:
    rng = np.random.default_rng(seed)
    array = rng.integers(60, 200, (size[1], size[0]), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


# -- interlock ---------------------------------------------------------------


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_scan_refuses_while_a_job_owns_the_machine(state):
    with pytest.raises(PrinterBusy):
        Interlock(settings()).assert_can_scan(FakeClient(state=state))


def test_scan_refuses_an_unknown_state():
    """A Klipper state nobody anticipated must refuse, not slip through."""
    with pytest.raises(PrinterBusy):
        Interlock(settings()).assert_can_scan(FakeClient(state="some_future_state"))


@pytest.mark.parametrize("homed", ["", "xy", "x"])
def test_scan_refuses_unhomed_z(homed):
    with pytest.raises(NotHomed):
        Interlock(settings()).assert_can_scan(
            FakeClient(state="standby", homed=homed, hotend=25.0)
        )


def test_scan_refuses_a_hot_nozzle():
    with pytest.raises(TooHot):
        Interlock(settings()).assert_can_scan(
            FakeClient(state="standby", hotend=210.0)
        )


def test_scan_clears_when_idle_cool_and_homed():
    clearance = Interlock(settings()).assert_can_scan(
        FakeClient(state="standby", hotend=25.0)
    )
    assert clearance.travel_mm > 0


def test_object_height_is_reserved_from_the_top_of_the_window():
    interlock = Interlock(settings())
    client = FakeClient(state="standby", hotend=25.0)
    short = interlock.assert_can_scan(client, object_height_mm=0.0)
    tall = interlock.assert_can_scan(client, object_height_mm=80.0)
    assert tall.z_max_mm == pytest.approx(short.z_max_mm - 80.0)


def test_impossibly_tall_object_refuses():
    with pytest.raises(UnsafePose):
        Interlock(settings()).assert_can_scan(
            FakeClient(state="standby", hotend=25.0), object_height_mm=400.0
        )


@pytest.mark.parametrize("z", [-50.0, 999.0, float("nan")])
def test_out_of_window_z_raises_rather_than_clamping(z):
    interlock = Interlock(settings())
    clearance = interlock.assert_can_scan(FakeClient(state="standby", hotend=25.0))
    with pytest.raises(UnsafePose):
        interlock.validate_z(z, clearance)


def test_move_script_is_z_only_absolute_and_waits():
    interlock = Interlock(settings())
    clearance = interlock.assert_can_scan(FakeClient(state="standby", hotend=25.0))
    script = interlock.move_script(42.5, clearance)
    assert script.startswith("G90")
    assert "G0 Z42.500" in script
    assert script.strip().endswith("M400")
    for forbidden in ("M112", " X", " Y", " E"):
        assert forbidden not in script.replace("G90", "")


# -- autonomy ----------------------------------------------------------------


@pytest.mark.parametrize("action", ["cancel", "reprint"])
@pytest.mark.parametrize("autonomy", ["observe", "suggest", "auto_pause"])
def test_agent_may_never_cancel_or_reprint(action, autonomy):
    """The most important rule in the system, asserted at every autonomy level."""
    interlock = Interlock(settings(autonomy=autonomy))
    with pytest.raises(ActionNotPermitted):
        interlock.assert_actor_may(action, "agent")


def test_agent_may_pause_only_at_auto_pause():
    for autonomy, allowed in [("observe", False), ("suggest", False), ("auto_pause", True)]:
        interlock = Interlock(settings(autonomy=autonomy))
        if allowed:
            interlock.assert_actor_may("pause", "agent")
        else:
            with pytest.raises(ActionNotPermitted):
                interlock.assert_actor_may("pause", "agent")


def test_a_human_may_always_cancel():
    Interlock(settings(autonomy="observe")).assert_actor_may("cancel", "human")


def test_agent_actions_are_rate_limited():
    interlock = Interlock(settings(autonomy="auto_pause", action_cooldown_s=300.0))
    interlock.assert_actor_may("pause", "agent")
    interlock.note_agent_action()
    with pytest.raises(ActionCooldown):
        interlock.assert_actor_may("pause", "agent")


def test_cooldown_does_not_restrain_a_human():
    interlock = Interlock(settings(autonomy="auto_pause"))
    interlock.note_agent_action()
    interlock.assert_actor_may("pause", "human")


# -- control -----------------------------------------------------------------


def test_pause_while_printing_reaches_the_printer():
    client = FakeClient(state="printing")
    result = PrintController(client, settings(), events=EventLog()).pause()
    assert result.ok
    assert client.calls == ["pause"]
    assert result.printer_state_after == "paused"


def test_pause_while_idle_is_refused_not_attempted():
    client = FakeClient(state="standby")
    result = PrintController(client, settings(), events=EventLog()).pause()
    assert not result.ok
    assert result.reason == "not_printing"
    assert client.calls == []


def test_agent_pause_is_refused_and_logged_as_a_suggestion():
    """A policy refusal is the system asking a human, not an error."""
    log = EventLog()
    client = FakeClient(state="printing")
    result = PrintController(client, settings(autonomy="suggest"), events=log).pause(
        actor="agent", why="spaghetti confirmed"
    )
    assert not result.ok
    assert result.reason == "not_permitted"
    assert client.calls == []
    kinds = [e["kind"] for e in log.recent()]
    assert EventKind.ACTION_SUGGESTED.value in kinds


def test_agent_pause_succeeds_at_auto_pause():
    client = FakeClient(state="printing")
    result = PrintController(
        client, settings(autonomy="auto_pause"), events=EventLog()
    ).pause(actor="agent", why="detachment")
    assert result.ok
    assert client.calls == ["pause"]


def test_available_actions_separate_human_from_agent():
    controller = PrintController(
        FakeClient(state="printing"), settings(autonomy="auto_pause"), events=EventLog()
    )
    actions = controller.available_actions()["actions"]
    assert actions["pause"]["human"] and actions["pause"]["agent"]
    assert actions["cancel"]["human"] and not actions["cancel"]["agent"]


# -- defects and fusion ------------------------------------------------------


def test_consequences_outrank_model_certainty():
    """A confident cosmetic finding must not outrank a tentative critical one."""
    cosmetic = Detection(DefectKind.STRINGING, 0.99, "a")
    critical = Detection(DefectKind.SPAGHETTI, 0.45, "b")
    assert critical.severity > cosmetic.severity


def test_urgency_ordering_is_total():
    ranks = [u.rank for u in Urgency]
    assert len(set(ranks)) == len(ranks)
    assert Urgency.CRITICAL.rank > Urgency.SERIOUS.rank > Urgency.COSMETIC.rank


def test_only_pause_recoverable_defects_are_marked_so():
    # Pausing does not un-warp a corner or un-string a travel move.
    assert not DefectKind.WARPING.recoverable_by_pause
    assert not DefectKind.STRINGING.recoverable_by_pause
    assert DefectKind.SPAGHETTI.recoverable_by_pause
    assert DefectKind.DETACHMENT.recoverable_by_pause


def test_every_defect_has_a_profile():
    """A new defect kind cannot be added without deciding what to do about it."""
    for kind in DefectKind:
        assert kind.label
        assert isinstance(kind.urgency, Urgency)
        assert 0.0 <= kind.severity_floor <= 1.0


def test_fusion_takes_the_max_not_the_mean():
    """A member that cannot see a defect must not veto one that can."""
    verdict = fuse(
        [
            ProviderReport("a", True, detections=[Detection(DefectKind.SPAGHETTI, 0.9, "a")]),
            ProviderReport("b", True, detections=[]),
        ]
    )
    assert verdict.worst is not None
    assert verdict.worst.confidence == pytest.approx(0.9)


def test_skipped_providers_never_read_as_healthy():
    """"I could not look" and "I looked and it is fine" must stay distinct."""
    verdict = fuse(
        [ProviderReport("ollama_local", False, skipped_reason="timed out")]
    )
    assert verdict.skipped == ["ollama_local"]
    assert verdict.voted == []
    assert not verdict.flagged


def test_agreement_reflects_how_many_members_saw_it():
    verdict = fuse(
        [
            ProviderReport("a", True, detections=[Detection(DefectKind.WARPING, 0.7, "a")]),
            ProviderReport("b", True, detections=[Detection(DefectKind.WARPING, 0.6, "b")]),
            ProviderReport("c", True, detections=[]),
        ]
    )
    assert verdict.agreement == pytest.approx(2 / 3)


def test_camera_fault_is_not_a_print_failure():
    verdict = fuse(
        [ProviderReport("cv", True, detections=[Detection(DefectKind.CAMERA_FAULT, 0.9, "cv")])]
    )
    assert not verdict.flagged
    assert verdict.camera_fault


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("spaghetti", DefectKind.SPAGHETTI),
        ("Layer Shift", DefectKind.LAYER_SHIFT),
        ("bed_adhesion", DefectKind.ADHESION),
        ("clogged nozzle", DefectKind.NOZZLE_CLOG),
        ("underextrusion", DefectKind.UNDER_EXTRUSION),
        ("something invented", None),
        (None, None),
    ],
)
def test_defect_label_parsing(raw, expected):
    assert parse_kind(raw) is expected


# -- vision providers --------------------------------------------------------


def test_tripwire_flags_a_black_frame():
    report = MotionTripwire().inspect(jpeg(2), FrameContext())
    assert report.ok
    assert any(d.kind is DefectKind.CAMERA_FAULT for d in report.detections)


def test_tripwire_stays_quiet_without_a_baseline():
    """It must not fire on the first frames, before "normal" is known."""
    tripwire = MotionTripwire()
    report = tripwire.inspect(
        noisy_jpeg(1), FrameContext(previous_jpeg=noisy_jpeg(2))
    )
    assert report.ok
    assert not [d for d in report.detections if d.kind.is_print_failure]


def test_tripwire_never_raises_on_garbage():
    report = MotionTripwire().inspect(b"not a jpeg", FrameContext())
    assert not report.ok
    assert report.skipped_reason


@pytest.mark.parametrize(
    "text",
    [
        '{"print_looks_healthy": true, "defects": []}',
        'Sure!\n```json\n{"print_looks_healthy": true, "defects": []}\n```',
        'Here: {"print_looks_healthy": true, "defects": []} hope that helps',
        '{"print_looks_healthy": true, "defects": [], "overall_note": "a } brace"}',
    ],
)
def test_json_extraction_survives_model_chatter(text):
    assert extract_json(text)["print_looks_healthy"] is True


@pytest.mark.parametrize("text", ["", "no json", "{unterminated", "[1,2]"])
def test_json_extraction_rejects_garbage(text):
    with pytest.raises(ValueError):
        extract_json(text)


def test_unknown_defect_labels_are_dropped_not_guessed():
    detections, _ = parse_defects(
        {"defects": [{"kind": "gremlins", "confidence": 0.9}]}, "test"
    )
    assert detections == []


def test_pixel_bboxes_are_normalized():
    detections, _ = parse_defects(
        {"defects": [{"kind": "spaghetti", "confidence": 0.8, "bbox_norm": [0, 0, 640, 480]}]},
        "test",
    )
    assert detections[0].bbox_norm is not None
    assert all(0.0 <= v <= 1.0 for v in detections[0].bbox_norm)


# -- revise ------------------------------------------------------------------


def test_revision_escalates_on_the_second_attempt():
    first = plan_revision(DefectKind.ADHESION, {"bed_temperature": 55.0}, attempt=1)
    second = plan_revision(DefectKind.ADHESION, {"bed_temperature": 55.0}, attempt=2)
    bed = lambda r: [c for c in r.changes if c.name == "bed_temperature"][0]  # noqa: E731
    assert bed(second).new_value > bed(first).new_value


def test_every_adjustment_is_bounded():
    """A repeated failure must never be able to walk the bed to 150 C.

    Uses a raised retry budget so the bound, not the retry gate, is what stops
    it - otherwise this passes for the wrong reason.
    """
    config = settings(max_reprint_attempts=5)
    revision = plan_revision(
        DefectKind.ADHESION, {"bed_temperature": 108.0}, attempt=3, settings=config
    )
    bed = [c for c in revision.changes if c.name == "bed_temperature"][0]
    assert bed.new_value <= BOUNDS["bed_temperature"][1]
    assert "clamped" in bed.reason


def test_retry_budget_stops_the_loop():
    from thox.errors import ReviseError

    config = settings(max_reprint_attempts=2)
    with pytest.raises(ReviseError):
        plan_revision(DefectKind.ADHESION, {}, attempt=3, settings=config)


def test_defects_without_a_parameter_remedy_are_refused():
    from thox.errors import ReviseError

    with pytest.raises(ReviseError):
        plan_revision(DefectKind.CAMERA_FAULT, {})


def test_revision_separates_in_place_from_reslice():
    revision = plan_revision(DefectKind.ADHESION, {"bed_temperature": 55.0})
    assert any(not c.requires_reslice for c in revision.changes)
    assert any(c.requires_reslice for c in revision.changes)
    assert "Requires re-slicing" in revision.changelog()


def test_overrides_land_after_the_start_sequence(tmp_path):
    """Injected before M109, the slicer's own wait would overwrite them."""
    source = tmp_path / "part.gcode"
    source.write_text(
        "; generated\nM190 S55\nM109 S215\nG28\nG1 X10 Y10 E1.5 F1200\n"
        "; bed_temperature = 55\n",
        encoding="utf-8",
    )
    revision = plan_revision(DefectKind.ADHESION, read_gcode_params(str(source)))
    summary = apply_revision(str(source), revision, str(tmp_path / "rev.gcode"))
    text = (tmp_path / "rev.gcode").read_text(encoding="utf-8")
    assert summary["injected_after_start"]
    assert text.index("THOX overrides begin") > text.index("M109")


def test_reslice_only_revision_cannot_be_applied_in_place(tmp_path):
    from thox.errors import ReviseError
    from thox.revise import Revision, ParameterChange

    source = tmp_path / "part.gcode"
    source.write_text("G1 X1 E1\n", encoding="utf-8")
    revision = Revision(
        defect=DefectKind.STRINGING,
        attempt=1,
        changes=[
            ParameterChange("retraction_length", 0.8, 1.0, "mm", "test")
        ],
    )
    with pytest.raises(ReviseError):
        apply_revision(str(source), revision, str(tmp_path / "rev.gcode"))


def test_gcode_params_read_from_slicer_comments(tmp_path):
    source = tmp_path / "p.gcode"
    source.write_text(
        "; nozzle_temperature = 240\n; bed_temperature = 70\n; flow_ratio = 0.97\n",
        encoding="utf-8",
    )
    params = read_gcode_params(str(source))
    assert params["nozzle_temperature"] == 240.0
    assert params["flow_ratio"] == pytest.approx(0.97)


# -- config and secrets ------------------------------------------------------


def test_secrets_never_appear_in_a_settings_dump():
    config = settings()
    config.openai_api_key = "sk-real-secret"
    config.moonraker_api_key = "mr-secret"
    serialized = repr(config.safe_dict()) + repr(config)
    assert "sk-real-secret" not in serialized
    assert "mr-secret" not in serialized


def test_redact_distinguishes_unset_from_hidden():
    out = redact({"api_key": None, "token": "abc", "host": "10.0.0.1"})
    assert out["api_key"] is None
    assert out["token"] == "***redacted***"
    assert out["host"] == "10.0.0.1"


def test_placeholder_keys_are_treated_as_unset():
    from thox.config import _clean_secret

    assert _clean_secret("ollama") is None
    assert _clean_secret("  ") is None
    assert _clean_secret("sk-real") == "sk-real"


def test_missing_host_raises_a_useful_error():
    from thox.config import ConfigError

    with pytest.raises(ConfigError):
        ThoxSettings().require_host()


def test_invalid_autonomy_is_rejected():
    from thox.config import ConfigError

    with pytest.raises(ConfigError):
        settings(autonomy="yolo")


# -- events ------------------------------------------------------------------


def test_routine_samples_do_not_notify():
    """Otherwise the operator mutes the channel and misses the one that matters."""
    assert not EventKind.SAMPLE.is_notable
    assert EventKind.ALERT.is_notable
    assert EventKind.ACTION_TAKEN.is_notable


def test_event_log_supports_incremental_polling():
    log = EventLog()
    log.add(EventKind.SAMPLE, "one")
    mark = log.last_seq
    log.add(EventKind.ALERT, "two", severity=0.9)
    fresh = log.recent(since_seq=mark)
    assert [e["message"] for e in fresh] == ["two"]


def test_event_log_persists_to_disk(tmp_path):
    log = EventLog(str(tmp_path))
    log.begin_job("job1")
    log.add(EventKind.ALERT, "spaghetti", severity=0.9)
    path = tmp_path / "health" / "job1" / "events.jsonl"
    assert path.is_file()
    assert "spaghetti" in path.read_text(encoding="utf-8")
