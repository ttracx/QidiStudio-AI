"""Prompts for the language-model members of the print-health ensemble.

Three rules, each from an observed failure rather than from taste.

**Give the model the layer number.** Material outside the object outline is a
raft on layer 1 and spaghetti on layer 200. A model shown only pixels flags
brims, purge lines and skirts as adhesion failures constantly, and an operator
who gets three false alarms stops reading them.

**Force a closed vocabulary.** Free-text defect names cannot be routed to a fix.
The prompt lists the exact accepted values and :func:`~thox.defects.parse_kind`
drops anything else, so an invented label degrades to "no detection" rather than
to a crash or an unhandled kind.

**Never ask for an action.** The model reports what it sees; whether that
justifies pausing a four-hour print is decided by
:mod:`thox.control` against the configured autonomy level. A model asked
"should I pause?" will say yes far too readily, and that judgement is not one it
has the context to make.

An operational note that belongs with the prompts: on the measured host,
Ollama's ``format: "json"`` option makes qwen3-vl return an **empty string**,
and its reasoning phase needs a large token budget or the answer never arrives.
JSON is therefore requested in the prompt text and parsed defensively.
"""

from __future__ import annotations

from .base import FrameContext

_KIND_LIST = (
    '"spaghetti", "detachment", "print_came_loose", "first_layer", "adhesion", '
    '"warping", "layer_shift", "stringing", "under_extrusion", '
    '"over_extrusion", "blob", "nozzle_clog"'
)

_BASE = """You are the quality-inspection stage of a 3D printer monitor.

The image is the print bed of a QIDI Q2 seen by a fixed chamber camera during a
print. Your job is to report visible print defects, and nothing else.

WHAT IS NOT A DEFECT — do not report these:
- The white extruder assembly, metal rails, the gantry, belts, or the bed itself.
- A brim, skirt, raft or purge line around the part. These are intentional.
- Support structures, which look loose and sparse on purpose.
- Normal stringing between two parts that is barely visible.
- The part simply being unfinished.

Report ONLY the defect types in this list, spelled exactly:
{kinds}

Respond with ONE JSON object and nothing else. No markdown fence, no commentary.

{{
  "print_looks_healthy": true or false,
  "defects": [
    {{
      "kind": "one of the values listed above",
      "confidence": 0.0 to 1.0,
      "bbox_norm": [x0, y0, x1, y1],
      "note": "one short sentence describing what you actually see"
    }}
  ],
  "frame_usable": true or false,
  "overall_note": "one short sentence"
}}

bbox_norm uses fractions of image width and height with the origin at top-left,
so every value is between 0 and 1. Use an empty defects list when the print
looks healthy. Do NOT recommend pausing or cancelling — that decision is made
elsewhere, from information you do not have."""


def health_prompt(context: FrameContext) -> str:
    """Build the inspection prompt for one frame."""
    prompt = _BASE.format(kinds=_KIND_LIST)

    facts: list[str] = []
    if context.layer is not None:
        total = f" of {context.total_layers}" if context.total_layers else ""
        facts.append(f"This is layer {context.layer}{total}.")
        if context.is_first_layers:
            facts.append(
                "These are the FIRST LAYERS. A brim or raft is expected here and "
                "is not a defect. Adhesion problems matter most at this stage."
            )
        else:
            facts.append(
                "The print is past its first layers, so material lying outside "
                "the part outline is suspicious rather than expected."
            )
    if context.progress:
        facts.append(f"The job is about {context.progress * 100:.0f}% complete.")
    if context.print_duration_s > 60:
        facts.append(f"It has been printing for {context.print_duration_s / 60:.0f} minutes.")
    if context.prior_note:
        facts.append(f"An earlier frame in this job was described as: {context.prior_note}")

    if not facts:
        return prompt
    return prompt + "\n\nContext for this frame:\n" + "\n".join(
        f"- {fact}" for fact in facts
    )
